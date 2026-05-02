#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail

APP_HOME="${APP_HOME:-$HOME/dtk-alpr}"
APP_DST="$APP_HOME/app"

URL="${1:-${CAMERA_STREAM_URL:-}}"
if [ -z "$URL" ]; then
  cat >&2 <<'EOF'
ERROR: camera stream URL is required.

Use a real video stream from the phone camera, for example from an Android
RTSP/IP-camera app, then run:

  bash ~/dtk-alpr/app/termux/run_camera_stream.sh rtsp://127.0.0.1:8554/live

or:

  CAMERA_STREAM_URL=rtsp://127.0.0.1:8554/live bash ~/dtk-alpr/app/termux/run_camera_stream.sh

For maximum performance, use 1280x720, 15-25 FPS, H.264, fixed focus if available.
EOF
  exit 2
fi

FPS_LIMIT="${FPS_LIMIT:-0}"
CONFIRMATIONS="${CONFIRMATIONS:-1}"
ACCUMULATION_MS="${ACCUMULATION_MS:-0}"
DUPLICATE_TIMEOUT_MS="${DUPLICATE_TIMEOUT_MS:-600}"
PREVIEW_EVERY="${PREVIEW_EVERY:-0}"
MAX_ZOOM="${MAX_ZOOM:-4.0}"

echo "Starting DTK video mode from camera stream:"
echo "  $URL"
echo "Profile:"
echo "  fps_limit=$FPS_LIMIT confirmations=$CONFIRMATIONS accumulation_ms=$ACCUMULATION_MS duplicate_timeout_ms=$DUPLICATE_TIMEOUT_MS"
echo

proot-distro login ubuntu --shared-tmp -- bash "$APP_DST/ubuntu/run_video.sh" \
  --rtsp "$URL" \
  --fps-limit "$FPS_LIMIT" \
  --confirmations "$CONFIRMATIONS" \
  --accumulation-ms "$ACCUMULATION_MS" \
  --duplicate-timeout-ms "$DUPLICATE_TIMEOUT_MS" \
  --preview-every "$PREVIEW_EVERY" \
  --max-zoom "$MAX_ZOOM"
