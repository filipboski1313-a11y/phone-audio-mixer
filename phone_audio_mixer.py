#!/usr/bin/env python3
"""Phone Audio Mixer - Universal Linux Bluetooth & AirPlay audio receiver and mixer."""

import json
import os
import re
import shutil
import subprocess
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk


APP_ID = "io.github.filipboski.PhoneAudioMixer"
AGENT_PATH = "/io/github/filipboski/PhoneAudioMixer/agent"
POLL_INTERVAL_MS = 2500

AGENT_XML = """
<node>
  <interface name="org.bluez.Agent1">
    <method name="Release"/>
    <method name="RequestPinCode">
      <arg type="o" name="device" direction="in"/>
      <arg type="s" name="pincode" direction="out"/>
    </method>
    <method name="DisplayPinCode">
      <arg type="o" name="device" direction="in"/>
      <arg type="s" name="pincode" direction="in"/>
    </method>
    <method name="RequestPasskey">
      <arg type="o" name="device" direction="in"/>
      <arg type="u" name="passkey" direction="out"/>
    </method>
    <method name="DisplayPasskey">
      <arg type="o" name="device" direction="in"/>
      <arg type="u" name="passkey" direction="in"/>
      <arg type="q" name="entered" direction="in"/>
    </method>
    <method name="RequestConfirmation">
      <arg type="o" name="device" direction="in"/>
      <arg type="u" name="passkey" direction="in"/>
    </method>
    <method name="RequestAuthorization">
      <arg type="o" name="device" direction="in"/>
    </method>
    <method name="AuthorizeService">
      <arg type="o" name="device" direction="in"/>
      <arg type="s" name="uuid" direction="in"/>
    </method>
    <method name="Cancel"/>
  </interface>
</node>
"""

A2DP_SOURCE_UUID = "0000110a-0000-1000-8000-00805f9b34fb"
A2DP_SINK_UUID = "0000110b-0000-1000-8000-00805f9b34fb"


def run_cmd(args: list[str], timeout: float = 5.0) -> tuple[int, str, str]:
    """Execute a subprocess with LC_ALL=C and return (returncode, stdout, stderr)."""
    env = os.environ.copy()
    env["LC_ALL"] = "C"
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except Exception as exc:
        return 1, "", str(exc)


def get_pactl_json(resource: str) -> list[dict]:
    """Query PulseAudio/PipeWire via `pactl -f json list <resource>`."""
    if not shutil.which("pactl"):
        return []
    rc, out, _ = run_cmd(["pactl", "-f", "json", "list", resource])
    if rc != 0 or not out.strip():
        return []
    try:
        data = json.loads(out)
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        return []


def get_default_sink() -> str:
    if not shutil.which("pactl"):
        return ""
    rc, out, _ = run_cmd(["pactl", "get-default-sink"])
    return out.strip() if rc == 0 else ""


def parse_volume_pct(obj: dict) -> int:
    """Compute average channel volume percentage from a pactl JSON object."""
    vol = obj.get("volume", {})
    pcts = []
    for ch in vol.values():
        vp = ch.get("value_percent", "")
        if vp.endswith("%"):
            try:
                pcts.append(int(vp[:-1]))
            except ValueError:
                pass
    return round(sum(pcts) / len(pcts)) if pcts else 100


def is_phone_stream(sink_input: dict) -> bool:
    """Return True if a PipeWire/PulseAudio sink-input originates from a phone."""
    props = sink_input.get("properties", {})
    node_name = props.get("node.name", "").lower()
    app_name = props.get("application.name", "").lower()
    media_name = props.get("media.name", "").lower()
    return (
        node_name.startswith("bluez_input.")
        or "shairport" in node_name
        or "shairport" in app_name
        or "airplay" in app_name
        or "uxplay" in node_name
        or "uxplay" in app_name
        or "iphone airplay" in media_name
    )


def find_loopback_modules() -> list[dict]:
    """Return active module-loopback instances."""
    return [
        {"index": mod.get("index"), "argument": mod.get("argument", "")}
        for mod in get_pactl_json("modules")
        if mod.get("name") == "module-loopback"
    ]


def is_direct_linked(source_name: str, sink_name: str) -> bool:
    """Check via pw-link if PipeWire already links source_name directly to sink_name."""
    if not shutil.which("pw-link"):
        return False
    rc, out, _ = run_cmd(["pw-link", "-l"])
    if rc != 0:
        return False
    in_source = False
    for line in out.splitlines():
        if not line.startswith(" "):
            in_source = line.startswith(f"{source_name}:")
        elif in_source and f"|-> {sink_name}:" in line:
            return True
    return False


def get_airplay_status() -> dict:
    """Inspect shairport-sync availability and systemd user unit state."""
    installed = shutil.which("shairport-sync") is not None
    active = False
    enabled = False
    if installed and shutil.which("systemctl"):
        rc_a, out_a, _ = run_cmd(["systemctl", "--user", "is-active", "shairport-sync.service"])
        active = rc_a == 0 and out_a.strip() == "active"
        rc_e, out_e, _ = run_cmd(["systemctl", "--user", "is-enabled", "shairport-sync.service"])
        enabled = rc_e == 0 and out_e.strip() == "enabled"
    return {"installed": installed, "active": active, "enabled": enabled}


class BluezManager:
    """Direct D-Bus client for BlueZ (adapter, devices, and Agent1 pairing)."""

    def __init__(self, on_confirm_cb, on_changed_cb):
        self._on_confirm_cb = on_confirm_cb
        self._on_changed_cb = on_changed_cb
        self._bus: Gio.DBusConnection | None = None
        self._reg_id = 0
        self._watch_id = 0
        self.agent_registered = False

        try:
            self._bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
            self._watch_id = Gio.bus_watch_name_on_connection(
                self._bus,
                "org.bluez",
                Gio.BusNameWatcherFlags.NONE,
                self._on_bluez_appeared,
                self._on_bluez_vanished,
            )
        except Exception:
            self._bus = None

    def _on_bluez_appeared(self, _conn, _name, _owner):
        self._register_agent()
        GLib.idle_add(self._on_changed_cb)

    def _on_bluez_vanished(self, _conn, _name):
        self.agent_registered = False
        GLib.idle_add(self._on_changed_cb)

    def _register_agent(self):
        if not self._bus:
            return
        try:
            if not self._reg_id:
                node_info = Gio.DBusNodeInfo.new_for_xml(AGENT_XML)
                self._reg_id = self._bus.register_object(
                    AGENT_PATH,
                    node_info.interfaces[0],
                    self._handle_method_call,
                    None,
                    None,
                )
            self._bus.call_sync(
                "org.bluez",
                "/org/bluez",
                "org.bluez.AgentManager1",
                "RegisterAgent",
                GLib.Variant("(os)", (AGENT_PATH, "DisplayYesNo")),
                None,
                Gio.DBusCallFlags.NONE,
                -1,
                None,
            )
            self._bus.call_sync(
                "org.bluez",
                "/org/bluez",
                "org.bluez.AgentManager1",
                "RequestDefaultAgent",
                GLib.Variant("(o)", (AGENT_PATH,)),
                None,
                Gio.DBusCallFlags.NONE,
                -1,
                None,
            )
            self.agent_registered = True
        except Exception:
            self.agent_registered = False

    def unregister(self):
        if self._watch_id:
            Gio.bus_unwatch_name(self._watch_id)
            self._watch_id = 0
        if self._bus and self.agent_registered:
            try:
                self._bus.call_sync(
                    "org.bluez",
                    "/org/bluez",
                    "org.bluez.AgentManager1",
                    "UnregisterAgent",
                    GLib.Variant("(o)", (AGENT_PATH,)),
                    None,
                    Gio.DBusCallFlags.NONE,
                    -1,
                    None,
                )
            except Exception:
                pass
            self.agent_registered = False
        if self._bus and self._reg_id:
            self._bus.unregister_object(self._reg_id)
            self._reg_id = 0

    def _device_name_from_path(self, device_path: str) -> str:
        if not self._bus:
            return device_path
        try:
            res = self._bus.call_sync(
                "org.bluez",
                device_path,
                "org.freedesktop.DBus.Properties",
                "Get",
                GLib.Variant("(ss)", ("org.bluez.Device1", "Alias")),
                GLib.VariantType("(v)"),
                Gio.DBusCallFlags.NONE,
                -1,
                None,
            )
            return str(res.unpack()[0])
        except Exception:
            return device_path.split("/")[-1].replace("dev_", "").replace("_", ":")

    def set_prop(self, obj_path: str, iface: str, prop: str, variant: GLib.Variant):
        if not self._bus:
            return
        self._bus.call_sync(
            "org.bluez",
            obj_path,
            "org.freedesktop.DBus.Properties",
            "Set",
            GLib.Variant("(ssv)", (iface, prop, variant)),
            None,
            Gio.DBusCallFlags.NONE,
            -1,
            None,
        )

    def call_method(self, obj_path: str, iface: str, method: str, timeout_ms: int = 15000):
        if not self._bus:
            return
        self._bus.call_sync(
            "org.bluez",
            obj_path,
            iface,
            method,
            None,
            None,
            Gio.DBusCallFlags.NONE,
            timeout_ms,
            None,
        )

    def get_state(self) -> tuple[dict, list[dict]]:
        """Query all BlueZ adapters and devices via ObjectManager."""
        default_adapter = {
            "available": False,
            "path": "",
            "alias": "Linux PC",
            "powered": False,
            "discoverable": False,
            "pairable": False,
            "discovering": False,
        }
        if not self._bus:
            return default_adapter, []
        try:
            res = self._bus.call_sync(
                "org.bluez",
                "/",
                "org.freedesktop.DBus.ObjectManager",
                "GetManagedObjects",
                None,
                GLib.VariantType("(a{oa{sa{sv}}})"),
                Gio.DBusCallFlags.NONE,
                3000,
                None,
            )
            objects = res.unpack()[0]
        except Exception:
            return default_adapter, []

        adapter = dict(default_adapter)
        devices = []
        mac_re = re.compile(r"^([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$")

        for path, ifaces in objects.items():
            if "org.bluez.Adapter1" in ifaces and not adapter["available"]:
                props = ifaces["org.bluez.Adapter1"]
                adapter = {
                    "available": True,
                    "path": path,
                    "alias": str(props.get("Alias", props.get("Name", "Linux PC"))),
                    "powered": bool(props.get("Powered", False)),
                    "discoverable": bool(props.get("Discoverable", False)),
                    "pairable": bool(props.get("Pairable", False)),
                    "discovering": bool(props.get("Discovering", False)),
                }
            elif "org.bluez.Device1" in ifaces:
                props = ifaces["org.bluez.Device1"]
                addr = str(props.get("Address", ""))
                name = str(props.get("Alias", props.get("Name", addr)))
                paired = bool(props.get("Paired", False))
                connected = bool(props.get("Connected", False))
                trusted = bool(props.get("Trusted", False))
                uuids = [u.lower() for u in props.get("UUIDs", [])]
                icon = str(props.get("Icon", ""))

                # Filter out anonymous BLE beacons unless paired
                if not paired and mac_re.match(name.replace("-", ":")):
                    continue

                is_phone_or_audio = (
                    icon in ("phone", "audio-headphones", "audio-headset", "audio-card")
                    or A2DP_SOURCE_UUID in uuids
                    or A2DP_SINK_UUID in uuids
                )
                devices.append(
                    {
                        "path": path,
                        "mac": addr,
                        "name": name,
                        "paired": paired,
                        "connected": connected,
                        "trusted": trusted,
                        "is_audio": is_phone_or_audio,
                        "is_phone": icon == "phone" or A2DP_SOURCE_UUID in uuids,
                    }
                )

        devices.sort(key=lambda d: (not d["connected"], not d["paired"], not d["is_audio"], d["name"].lower()))
        return adapter, devices

    def _handle_method_call(
        self, _conn, _sender, _obj_path, _iface_name, method_name, parameters, invocation
    ):
        if method_name in ("Release", "Cancel", "DisplayPinCode", "DisplayPasskey"):
            invocation.return_value(None)
        elif method_name == "RequestPinCode":
            invocation.return_value(GLib.Variant("(s)", ("0000",)))
        elif method_name == "RequestPasskey":
            invocation.return_value(GLib.Variant("(u)", (0,)))
        elif method_name == "RequestConfirmation":
            device_path, passkey = parameters.unpack()
            dev_name = self._device_name_from_path(device_path)
            GLib.idle_add(self._on_confirm_cb, dev_name, passkey, device_path, invocation)
        elif method_name == "RequestAuthorization":
            (device_path,) = parameters.unpack()
            try:
                self.set_prop(device_path, "org.bluez.Device1", "Trusted", GLib.Variant("b", True))
            except Exception:
                pass
            invocation.return_value(None)
        elif method_name == "AuthorizeService":
            device_path, _uuid = parameters.unpack()
            try:
                self.set_prop(device_path, "org.bluez.Device1", "Trusted", GLib.Variant("b", True))
            except Exception:
                pass
            invocation.return_value(None)
        else:
            invocation.return_dbus_error(
                "org.bluez.Error.Rejected", f"Unsupported method {method_name}"
            )


class PhoneAudioWindow(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application):
        super().__init__(application=app, title="Phone Audio Mixer")
        self.set_default_size(560, 720)

        self._updating_ui = False
        self._sinks: list[dict] = []
        self._selected_sink_name: str = get_default_sink()
        self._active_stream: dict | None = None
        self._adapter: dict = {}
        self._airplay: dict = {"installed": False, "active": False, "enabled": False}

        self._bluez = BluezManager(self._on_pairing_confirmation, self.refresh_async)

        toolbar_view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        refresh_btn = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Refresh")
        refresh_btn.connect("clicked", lambda *_: self.refresh_async())
        header.pack_end(refresh_btn)
        toolbar_view.add_top_bar(header)

        scroll = Gtk.ScrolledWindow(vexpand=True)
        clamp = Adw.Clamp(
            maximum_size=620,
            margin_top=16,
            margin_bottom=24,
            margin_start=16,
            margin_end=16,
        )
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)

        # Status / Warning Banner
        self.status_banner = Adw.Banner(
            title="Connect your phone via Bluetooth or AirPlay to mix its audio with PC audio."
        )
        self.status_banner.set_revealed(False)
        box.append(self.status_banner)

        # Section 1: Audio Mixer (Phone + PC -> Output)
        audio_group = Adw.PreferencesGroup(
            title="Audio Mixer (Phone + PC -> Headphones)",
            description="Phone audio and PC audio play simultaneously through the selected output device.",
        )

        self.stream_status_row = Adw.ActionRow(
            title="Phone Audio Stream",
            subtitle="Waiting for audio from phone...",
        )
        self.stream_badge = Gtk.Label(label="Idle", css_classes=["dim-label"])
        self.stream_status_row.add_suffix(self.stream_badge)
        audio_group.add(self.stream_status_row)

        phone_vol_row = Adw.ActionRow(title="Phone Volume")
        self.phone_mute_btn = Gtk.ToggleButton(
            icon_name="audio-volume-high-symbolic",
            valign=Gtk.Align.CENTER,
            tooltip_text="Mute phone audio",
        )
        self.phone_mute_btn.connect("toggled", self._on_phone_mute_toggled)
        self.phone_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 150, 5)
        self.phone_scale.set_size_request(210, -1)
        self.phone_scale.set_value(100)
        self.phone_scale.set_draw_value(True)
        self.phone_scale.set_value_pos(Gtk.PositionType.RIGHT)
        self.phone_scale.connect("value-changed", self._on_phone_vol_changed)
        phone_vol_row.add_suffix(self.phone_scale)
        phone_vol_row.add_suffix(self.phone_mute_btn)
        audio_group.add(phone_vol_row)

        self.sink_row = Adw.ComboRow(title="Output Device (Headphones / Speakers)")
        self.sink_model = Gtk.StringList()
        self.sink_row.set_model(self.sink_model)
        self.sink_row.connect("notify::selected", self._on_sink_selected)
        audio_group.add(self.sink_row)

        pc_vol_row = Adw.ActionRow(title="Master Output Volume (Phone + PC)")
        self.pc_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 2)
        self.pc_scale.set_size_request(210, -1)
        self.pc_scale.set_value(50)
        self.pc_scale.set_draw_value(True)
        self.pc_scale.set_value_pos(Gtk.PositionType.RIGHT)
        self.pc_scale.connect("value-changed", self._on_pc_vol_changed)
        pc_vol_row.add_suffix(self.pc_scale)
        audio_group.add(pc_vol_row)

        self.route_row = Adw.ActionRow(
            title="Routing Status",
            subtitle="Ready",
        )
        audio_group.add(self.route_row)
        box.append(audio_group)

        # Section 2: Bluetooth Receiver & Devices
        bt_group = Adw.PreferencesGroup(
            title="Bluetooth Connection",
            description="Pair a phone or manage connected Bluetooth audio devices.",
        )

        self.pairing_row = Adw.SwitchRow(
            title="Pairing Mode (Discoverable)",
            subtitle="Checking Bluetooth adapter...",
        )
        self.pairing_row.connect("notify::active", self._on_pairing_toggled)
        bt_group.add(self.pairing_row)

        self.scan_row = Adw.ActionRow(
            title="Scan for Nearby Devices",
            subtitle="Keep Bluetooth settings open on your phone, then click Scan",
        )
        self.scan_btn = Gtk.Button(label="Scan (8s)", valign=Gtk.Align.CENTER)
        self.scan_btn.connect("clicked", self._on_scan_clicked)
        self.scan_row.add_suffix(self.scan_btn)
        bt_group.add(self.scan_row)

        self.devices_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        bt_group.add(self.devices_box)
        box.append(bt_group)

        # Section 3: AirPlay Receiver (Wi-Fi)
        airplay_group = Adw.PreferencesGroup(
            title="AirPlay Receiver (Wi-Fi)",
            description="Recommended for iOS when also using Bluetooth headphones on the same adapter.",
        )
        self.airplay_row = Adw.SwitchRow(
            title="Shairport-Sync AirPlay Service",
            subtitle="Checking status...",
        )
        self.airplay_row.connect("notify::active", self._on_airplay_toggled)
        airplay_group.add(self.airplay_row)
        box.append(airplay_group)

        clamp.set_child(box)
        scroll.set_child(clamp)
        toolbar_view.set_content(scroll)
        self.set_content(toolbar_view)

        self.connect("close-request", self._on_close_request)
        self.refresh_async()
        GLib.timeout_add(POLL_INTERVAL_MS, self._on_timer)

    def _on_close_request(self, *_args):
        self._bluez.unregister()
        return False

    def _on_timer(self) -> bool:
        self.refresh_async()
        return True

    def refresh_async(self):
        threading.Thread(target=self._fetch_state, daemon=True).start()

    def _fetch_state(self):
        adapter, bt_devs = self._bluez.get_state()
        all_sinks = get_pactl_json("sinks")
        sinks = sorted(
            all_sinks,
            key=lambda s: (s.get("name", "").startswith("bluez_output."), s.get("description", "")),
        )
        sink_inputs = get_pactl_json("sink-inputs")
        phone_inputs = [si for si in sink_inputs if is_phone_stream(si)]

        sources = get_pactl_json("sources")
        bt_sources = [s for s in sources if s.get("name", "").startswith("bluez_input.")]
        loopbacks = find_loopback_modules()
        default_sink = get_default_sink()
        airplay = get_airplay_status()

        active_stream = None
        if phone_inputs:
            si = phone_inputs[0]
            props = si.get("properties", {})
            active_stream = {
                "kind": "sink-input",
                "id": si["index"],
                "name": props.get("node.name", ""),
                "label": props.get("media.name") or props.get("application.name") or props.get("node.name") or "Phone Stream",
                "volume": parse_volume_pct(si),
                "mute": bool(si.get("mute", False)),
                "sink_index": si.get("sink"),
                "is_bluetooth": props.get("node.name", "").startswith("bluez_input."),
            }
        elif bt_sources:
            src = bt_sources[0]
            active_stream = {
                "kind": "source",
                "id": src["name"],
                "name": src["name"],
                "label": src.get("description") or src["name"],
                "volume": parse_volume_pct(src),
                "mute": bool(src.get("mute", False)),
                "sink_index": None,
                "is_bluetooth": True,
                "routed": any(src["name"] in m["argument"] for m in loopbacks)
                or (default_sink and is_direct_linked(src["name"], default_sink)),
            }

        bt_out_connected = any(s.get("name", "").startswith("bluez_output.") for s in all_sinks)

        GLib.idle_add(
            self._apply_state,
            adapter,
            bt_devs,
            sinks,
            active_stream,
            default_sink,
            bt_out_connected,
            airplay,
        )

    def _apply_state(
        self,
        adapter: dict,
        bt_devs: list[dict],
        sinks: list[dict],
        active_stream: dict | None,
        default_sink: str,
        bt_out_connected: bool,
        airplay: dict,
    ):
        self._updating_ui = True
        try:
            self._adapter = adapter
            self._airplay = airplay

            # Banner logic
            if not shutil.which("pactl"):
                self.status_banner.set_title("pactl not found. Install pipewire-pulse or pulseaudio-utils.")
                self.status_banner.set_revealed(True)
            elif active_stream and active_stream["is_bluetooth"] and bt_out_connected:
                self.status_banner.set_title(
                    "Dual Bluetooth A2DP streams detected on one adapter. Use wired/USB headphones or AirPlay over Wi-Fi to prevent stuttering."
                )
                self.status_banner.set_revealed(True)
            else:
                self.status_banner.set_revealed(False)

            # Adapter state
            if adapter.get("available"):
                self.pairing_row.set_sensitive(True)
                self.scan_row.set_sensitive(True)
                vis = "Discoverable" if adapter["discoverable"] else "Hidden"
                agent_str = "Agent active" if self._bluez.agent_registered else "Agent inactive"
                self.pairing_row.set_subtitle(f"Adapter: {adapter['alias']} | {vis} | {agent_str}")
                self.pairing_row.set_active(adapter["discoverable"] and adapter["pairable"])
            else:
                self.pairing_row.set_sensitive(False)
                self.scan_row.set_sensitive(False)
                self.pairing_row.set_subtitle("No Bluetooth adapter found or bluetooth.service is stopped")
                self.pairing_row.set_active(False)

            # Devices list
            child = self.devices_box.get_first_child()
            while child:
                nxt = child.get_next_sibling()
                self.devices_box.remove(child)
                child = nxt

            if not bt_devs:
                empty_row = Adw.ActionRow(
                    title="No paired or discovered devices",
                    subtitle="Enable Pairing Mode or click Scan while Bluetooth settings are open on your phone.",
                )
                self.devices_box.append(empty_row)
            else:
                for dev in bt_devs:
                    status_parts = [dev["mac"]]
                    if dev["connected"]:
                        status_parts.append("Connected")
                    elif dev["paired"]:
                        status_parts.append("Paired")
                    else:
                        status_parts.append("Discovered")

                    row = Adw.ActionRow(
                        title=dev["name"],
                        subtitle=" | ".join(status_parts),
                    )
                    btn = Gtk.Button(
                        label="Disconnect" if dev["connected"] else "Connect",
                        valign=Gtk.Align.CENTER,
                    )
                    if dev["connected"]:
                        btn.add_css_class("destructive-action")
                    else:
                        btn.add_css_class("suggested-action")
                    btn.connect("clicked", lambda _b, d=dev: self._toggle_bt_device(d))
                    row.add_suffix(btn)
                    self.devices_box.append(row)

            # Sinks
            self._sinks = sinks
            sink_names = [s["name"] for s in sinks]
            sink_by_index = {s["index"]: s for s in sinks}
            current_labels = [
                self.sink_model.get_string(i) for i in range(self.sink_model.get_n_items())
            ]
            new_labels = [
                (s.get("description") if s.get("description") not in (None, "(null)") else s["name"])
                for s in sinks
            ]
            if current_labels != new_labels:
                self.sink_model.splice(0, self.sink_model.get_n_items(), new_labels)

            # Active Phone Stream
            self._active_stream = active_stream
            if active_stream:
                self.stream_status_row.set_subtitle(f"Active: {active_stream['label']}")
                self.stream_badge.set_label("Active")
                self.stream_badge.remove_css_class("dim-label")
                self.stream_badge.add_css_class("success")

                self.phone_scale.set_sensitive(True)
                self.phone_mute_btn.set_sensitive(True)
                self.phone_scale.set_value(active_stream["volume"])
                muted = active_stream["mute"]
                self.phone_mute_btn.set_active(muted)
                self.phone_mute_btn.set_icon_name(
                    "audio-volume-muted-symbolic" if muted else "audio-volume-high-symbolic"
                )

                if active_stream["kind"] == "sink-input":
                    sink_obj = sink_by_index.get(active_stream["sink_index"])
                    if sink_obj:
                        self._selected_sink_name = sink_obj["name"]
                        cs_desc = sink_obj.get("description") or sink_obj["name"]
                        self.route_row.set_subtitle(f"Playing on: {cs_desc}")
                    else:
                        self.route_row.set_subtitle("Playing via PipeWire")
                else:
                    self.route_row.set_subtitle(
                        "Loopback active" if active_stream.get("routed") else "Source ready (select output to route)"
                    )
            else:
                self.stream_status_row.set_subtitle("Waiting for audio playback from phone...")
                self.stream_badge.set_label("Idle")
                self.stream_badge.remove_css_class("success")
                self.stream_badge.add_css_class("dim-label")
                self.phone_scale.set_sensitive(False)
                self.phone_mute_btn.set_sensitive(False)
                self.route_row.set_subtitle("Mixed automatically into the selected output device")

            if self._selected_sink_name not in sink_names:
                self._selected_sink_name = (
                    default_sink if default_sink in sink_names else (sink_names[0] if sink_names else "")
                )

            if self._selected_sink_name in sink_names:
                idx = sink_names.index(self._selected_sink_name)
                self.sink_row.set_selected(idx)
                self.pc_scale.set_value(parse_volume_pct(sinks[idx]))

            # AirPlay
            if airplay["installed"]:
                self.airplay_row.set_sensitive(True)
                self.airplay_row.set_active(airplay["active"])
                self.airplay_row.set_subtitle(
                    "Running (select this PC in iOS Control Center)"
                    if airplay["active"]
                    else "Stopped (toggle to enable Wi-Fi AirPlay receiver)"
                )
            else:
                self.airplay_row.set_sensitive(False)
                self.airplay_row.set_active(False)
                self.airplay_row.set_subtitle("Not installed (install `shairport-sync` for Wi-Fi AirPlay)")
        finally:
            self._updating_ui = False

    def _on_pairing_confirmation(self, dev_name: str, passkey: int, device_path: str, invocation):
        dialog = Adw.AlertDialog(
            heading="Bluetooth Pairing Request",
            body=f"Device '{dev_name}' wants to pair.\n\nConfirmation code: {passkey:06d}",
        )
        dialog.add_response("reject", "Reject")
        dialog.add_response("accept", "Pair and Connect")
        dialog.set_response_appearance("accept", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("accept")

        def on_response(_dlg, response_id):
            if response_id == "accept":
                try:
                    self._bluez.set_prop(
                        device_path, "org.bluez.Device1", "Trusted", GLib.Variant("b", True)
                    )
                except Exception:
                    pass
                invocation.return_value(None)
                self.refresh_async()
            else:
                invocation.return_dbus_error(
                    "org.bluez.Error.Rejected", "Pairing rejected by user"
                )

        dialog.connect("response", on_response)
        dialog.present(self)
        return False

    def _on_pairing_toggled(self, switch_row, _pspec):
        if self._updating_ui or not self._adapter.get("available"):
            return
        active = switch_row.get_active()
        path = self._adapter["path"]

        def worker():
            try:
                if active:
                    self._bluez.set_prop(path, "org.bluez.Adapter1", "Powered", GLib.Variant("b", True))
                    self._bluez.set_prop(
                        path, "org.bluez.Adapter1", "DiscoverableTimeout", GLib.Variant("u", 0)
                    )
                    self._bluez.set_prop(path, "org.bluez.Adapter1", "Pairable", GLib.Variant("b", True))
                    self._bluez.set_prop(
                        path, "org.bluez.Adapter1", "Discoverable", GLib.Variant("b", True)
                    )
                else:
                    self._bluez.set_prop(
                        path, "org.bluez.Adapter1", "Discoverable", GLib.Variant("b", False)
                    )
                    self._bluez.set_prop(path, "org.bluez.Adapter1", "Pairable", GLib.Variant("b", False))
            except Exception:
                pass
            self.refresh_async()

        threading.Thread(target=worker, daemon=True).start()

    def _on_scan_clicked(self, _btn):
        if not self._adapter.get("available"):
            return
        self.scan_btn.set_sensitive(False)
        self.scan_btn.set_label("Scanning...")
        path = self._adapter["path"]

        def worker():
            try:
                self._bluez.set_prop(path, "org.bluez.Adapter1", "Powered", GLib.Variant("b", True))
                self._bluez.call_method(path, "org.bluez.Adapter1", "StartDiscovery", timeout_ms=5000)
                GLib.usleep(8_000_000)
                self._bluez.call_method(path, "org.bluez.Adapter1", "StopDiscovery", timeout_ms=5000)
            except Exception:
                pass
            GLib.idle_add(self._finish_scan)

        threading.Thread(target=worker, daemon=True).start()

    def _finish_scan(self):
        self.scan_btn.set_sensitive(True)
        self.scan_btn.set_label("Scan (8s)")
        self.refresh_async()
        return False

    def _toggle_bt_device(self, dev: dict):
        path = dev["path"]
        connected = dev["connected"]
        paired = dev["paired"]

        def worker():
            try:
                if connected:
                    self._bluez.call_method(path, "org.bluez.Device1", "Disconnect", timeout_ms=10000)
                else:
                    self._bluez.set_prop(path, "org.bluez.Device1", "Trusted", GLib.Variant("b", True))
                    if not paired:
                        self._bluez.call_method(path, "org.bluez.Device1", "Pair", timeout_ms=20000)
                    self._bluez.call_method(path, "org.bluez.Device1", "Connect", timeout_ms=15000)
            except Exception:
                pass
            self.refresh_async()

        threading.Thread(target=worker, daemon=True).start()

    def _on_phone_vol_changed(self, scale):
        if self._updating_ui or not self._active_stream:
            return
        val = int(scale.get_value())
        stream = self._active_stream
        cmd = (
            ["pactl", "set-sink-input-volume", str(stream["id"]), f"{val}%"]
            if stream["kind"] == "sink-input"
            else ["pactl", "set-source-volume", str(stream["id"]), f"{val}%"]
        )
        threading.Thread(target=lambda: run_cmd(cmd), daemon=True).start()

    def _on_phone_mute_toggled(self, btn):
        if self._updating_ui or not self._active_stream:
            return
        muted = btn.get_active()
        btn.set_icon_name(
            "audio-volume-muted-symbolic" if muted else "audio-volume-high-symbolic"
        )
        stream = self._active_stream
        flag = "1" if muted else "0"
        cmd = (
            ["pactl", "set-sink-input-mute", str(stream["id"]), flag]
            if stream["kind"] == "sink-input"
            else ["pactl", "set-source-mute", str(stream["id"]), flag]
        )
        threading.Thread(target=lambda: run_cmd(cmd), daemon=True).start()

    def _on_pc_vol_changed(self, scale):
        if self._updating_ui or not self._selected_sink_name:
            return
        val = int(scale.get_value())
        sink = self._selected_sink_name
        threading.Thread(
            target=lambda: run_cmd(["pactl", "set-sink-volume", sink, f"{val}%"]),
            daemon=True,
        ).start()

    def _on_sink_selected(self, combo_row, _pspec):
        if self._updating_ui:
            return
        idx = combo_row.get_selected()
        if 0 <= idx < len(self._sinks):
            sink_name = self._sinks[idx]["name"]
            self._selected_sink_name = sink_name
            stream = self._active_stream

            def worker():
                run_cmd(["pactl", "set-default-sink", sink_name])
                if stream:
                    if stream["kind"] == "sink-input":
                        run_cmd(["pactl", "move-sink-input", str(stream["id"]), sink_name])
                    else:
                        src = str(stream["id"])
                        for mod in find_loopback_modules():
                            if src in mod["argument"]:
                                run_cmd(["pactl", "unload-module", str(mod["index"])])
                        if not is_direct_linked(src, sink_name):
                            run_cmd(
                                [
                                    "pactl",
                                    "load-module",
                                    "module-loopback",
                                    f"source={src}",
                                    f"sink={sink_name}",
                                    "latency_msec=40",
                                ]
                            )
                self.refresh_async()

            threading.Thread(target=worker, daemon=True).start()

    def _on_airplay_toggled(self, switch_row, _pspec):
        if self._updating_ui or not self._airplay.get("installed"):
            return
        active = switch_row.get_active()

        def worker():
            if active:
                run_cmd(["systemctl", "--user", "enable", "--now", "shairport-sync.service"])
            else:
                run_cmd(["systemctl", "--user", "disable", "--now", "shairport-sync.service"])
            self.refresh_async()

        threading.Thread(target=worker, daemon=True).start()


class PhoneAudioApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.FLAGS_NONE)

    def do_activate(self):
        win = self.props.active_window
        if not win:
            win = PhoneAudioWindow(self)
        win.present()


if __name__ == "__main__":
    app = PhoneAudioApp()
    raise SystemExit(app.run(None))
