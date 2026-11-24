#!/usr/bin/env bash
set -euo pipefail

BIN_DIR="${HOME}/.local/bin"
APP_DIR="${HOME}/.local/share/applications"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

mkdir -p "${BIN_DIR}" "${APP_DIR}"

install -m 755 "${SCRIPT_DIR}/phone_audio_mixer.py" "${BIN_DIR}/phone-audio-mixer"
ln -sf "${BIN_DIR}/phone-audio-mixer" "${BIN_DIR}/bt-audio-mixer"

sed "s|^Exec=.*|Exec=${BIN_DIR}/phone-audio-mixer|" \
    "${SCRIPT_DIR}/phone-audio-mixer.desktop" > "${APP_DIR}/phone-audio-mixer.desktop"

rm -f "${APP_DIR}/bt-audio-mixer.desktop"

if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "${APP_DIR}" || true
fi

echo "Installed phone-audio-mixer to ${BIN_DIR}/phone-audio-mixer"
