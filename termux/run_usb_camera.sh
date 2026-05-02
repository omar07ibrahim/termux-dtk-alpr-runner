#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail

APP_HOME="${APP_HOME:-$HOME/dtk-alpr}"
APP_DST="$APP_HOME/app"

DEVICE_INDEX="${1:-${DEVICE_INDEX:-0}}"
WIDTH="${WIDTH:-1280}"
HEIGHT="${HEIGHT:-720}"
FPS_LIMIT="${FPS_LIMIT:-0}"
CONFIRMATIONS="${CONFIRMATIONS:-1}"
ACCUMULATION_MS="${ACCUMULATION_MS:-0}"
DUPLICATE_TIMEOUT_MS="${DUPLICATE_TIMEOUT_MS:-600}"
PREVIEW_EVERY="${PREVIEW_EVERY:-0}"
MAX_ZOOM="${MAX_ZOOM:-4.0}"

cat <<EOF
Starting DTK video mode from Linux video device index $DEVICE_INDEX.
Requested camera size: ${WIDTH}x${HEIGHT}

Note: Android phone built-in cameras usually are NOT exposed to Ubuntu/proot as
/dev/video*. This path is mainly for USB/UVC cameras or Linux devices that DTKVID
can open directly. For the built-in phone camera, use run_camera_stream.sh.
EOF

proot-distro login ubuntu --shared-tmp -- bash "$APP_DST/ubuntu/run_video.sh" \
  --device "$DEVICE_INDEX" \
  --device-width "$WIDTH" \
  --device-height "$HEIGHT" \
  --fps-limit "$FPS_LIMIT" \
  --confirmations "$CONFIRMATIONS" \
  --accumulation-ms "$ACCUMULATION_MS" \
  --duplicate-timeout-ms "$DUPLICATE_TIMEOUT_MS" \
  --preview-every "$PREVIEW_EVERY" \
  --max-zoom "$MAX_ZOOM"
