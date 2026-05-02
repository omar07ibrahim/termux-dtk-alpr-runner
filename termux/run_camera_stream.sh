#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail

SCRIPT_APP="$(cd "$(dirname "$0")/.." && pwd)"
APP_HOME="${APP_HOME:-$HOME/dtk-alpr}"
APP_DST="${APP_DST:-$APP_HOME/app}"
if [ ! -x "$APP_DST/ubuntu/run_video.sh" ] && [ -x "$SCRIPT_APP/ubuntu/run_video.sh" ]; then
  APP_DST="$SCRIPT_APP"
fi
if [ ! -x "$APP_DST/ubuntu/run_video.sh" ]; then
  cat >&2 <<EOF
ERROR: installed runner not found.

Run installer first:
  cd $SCRIPT_APP
  bash termux/install.sh

Then start:
  bash $APP_HOME/app/termux/run_camera_stream.sh
EOF
  exit 2
fi

URL="${1:-${CAMERA_STREAM_URL:-rtsp://127.0.0.1:8554/live}}"
if [ -z "$URL" ]; then
  cat >&2 <<'EOF'
ERROR: camera stream URL is required.

Use a real video stream from the phone camera. The companion Android APK
starts this stream locally:

  rtsp://127.0.0.1:8554/live

So the normal command is simply:

  bash ~/dtk-alpr/app/termux/run_camera_stream.sh

For an external RTSP/IP-camera app, run:

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
CAPTURE_BACKEND="${CAPTURE_BACKEND:-ffmpeg}"
STREAM_WIDTH="${STREAM_WIDTH:-1280}"
STREAM_HEIGHT="${STREAM_HEIGHT:-720}"
STREAM_FPS="${STREAM_FPS:-20}"

echo "Starting DTK video mode from camera stream:"
echo "  $URL"
echo "Profile:"
echo "  backend=$CAPTURE_BACKEND fps_limit=$FPS_LIMIT confirmations=$CONFIRMATIONS accumulation_ms=$ACCUMULATION_MS duplicate_timeout_ms=$DUPLICATE_TIMEOUT_MS"
echo

if [ "$CAPTURE_BACKEND" = "dtkvid" ]; then
  proot-distro login ubuntu --shared-tmp -- bash "$APP_DST/ubuntu/run_video.sh" \
    --rtsp "$URL" \
    --fps-limit "$FPS_LIMIT" \
    --confirmations "$CONFIRMATIONS" \
    --accumulation-ms "$ACCUMULATION_MS" \
    --duplicate-timeout-ms "$DUPLICATE_TIMEOUT_MS" \
    --preview-every "$PREVIEW_EVERY" \
    --max-zoom "$MAX_ZOOM"
else
  proot-distro login ubuntu --shared-tmp -- bash "$APP_DST/ubuntu/run_ffmpeg_video.sh" \
    --rtsp "$URL" \
    --width "$STREAM_WIDTH" \
    --height "$STREAM_HEIGHT" \
    --fps "$STREAM_FPS" \
    --fps-limit "$FPS_LIMIT" \
    --confirmations "$CONFIRMATIONS" \
    --accumulation-ms "$ACCUMULATION_MS" \
    --duplicate-timeout-ms "$DUPLICATE_TIMEOUT_MS" \
    --preview-every "${PREVIEW_EVERY:-20}" \
    --max-zoom "$MAX_ZOOM"
fi
