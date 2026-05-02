#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail

APP_HOME="${APP_HOME:-$HOME/dtk-alpr}"
APP_DST="$APP_HOME/app"

URL1="${1:-${CAMERA1_STREAM_URL:-}}"
URL2="${2:-${CAMERA2_STREAM_URL:-}}"
URL3="${3:-${CAMERA3_STREAM_URL:-}}"

if [ -z "$URL1" ] || [ -z "$URL2" ] || [ -z "$URL3" ]; then
  cat >&2 <<'EOF'
ERROR: three camera stream URLs are required.

Run with arguments:

  bash ~/dtk-alpr/app/termux/run_multi_camera_streams.sh \
    rtsp://camera1/live \
    rtsp://camera2/live \
    rtsp://camera3/live

or environment variables:

  CAMERA1_STREAM_URL=rtsp://camera1/live \
  CAMERA2_STREAM_URL=rtsp://camera2/live \
  CAMERA3_STREAM_URL=rtsp://camera3/live \
  bash ~/dtk-alpr/app/termux/run_multi_camera_streams.sh

For maximum performance, set each camera stream to 1280x720, 15-25 FPS,
H.264, fixed focus or continuous video focus, and no beauty/HDR filters.
EOF
  exit 2
fi

FPS_LIMIT="${FPS_LIMIT:-0}"
CONFIRMATIONS="${CONFIRMATIONS:-1}"
ACCUMULATION_MS="${ACCUMULATION_MS:-0}"
DUPLICATE_TIMEOUT_MS="${DUPLICATE_TIMEOUT_MS:-600}"
PREVIEW_EVERY="${PREVIEW_EVERY:-0}"
MAX_ZOOM="${MAX_ZOOM:-4.0}"
THREADS_PER_ENGINE="${THREADS_PER_ENGINE:-1}"
PRINT_EVERY="${PRINT_EVERY:-10}"
PRINT_MIN_SECONDS="${PRINT_MIN_SECONDS:-15}"

echo "Starting DTK multi-camera video mode:"
echo "  cam1: $URL1"
echo "  cam2: $URL2"
echo "  cam3: $URL3"
echo "Profile:"
echo "  threads_per_engine=$THREADS_PER_ENGINE fps_limit=$FPS_LIMIT confirmations=$CONFIRMATIONS"
echo "  accumulation_ms=$ACCUMULATION_MS duplicate_timeout_ms=$DUPLICATE_TIMEOUT_MS"
echo "  duplicate console report: every $PRINT_EVERY recognitions or $PRINT_MIN_SECONDS sec"
echo

proot-distro login ubuntu --shared-tmp -- bash "$APP_DST/ubuntu/run_multi_video.sh" \
  --stream-name cam1 --stream-name cam2 --stream-name cam3 \
  --rtsp "$URL1" \
  --rtsp "$URL2" \
  --rtsp "$URL3" \
  --threads "$THREADS_PER_ENGINE" \
  --fps-limit "$FPS_LIMIT" \
  --confirmations "$CONFIRMATIONS" \
  --accumulation-ms "$ACCUMULATION_MS" \
  --duplicate-timeout-ms "$DUPLICATE_TIMEOUT_MS" \
  --preview-every "$PREVIEW_EVERY" \
  --max-zoom "$MAX_ZOOM" \
  --print-every "$PRINT_EVERY" \
  --print-min-seconds "$PRINT_MIN_SECONDS"
