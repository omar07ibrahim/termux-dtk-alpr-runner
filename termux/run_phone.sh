#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail

APP_HOME="${APP_HOME:-$HOME/dtk-alpr}"
APP_DST="$APP_HOME/app"
SHARED="$APP_HOME/shared"
LATEST="$SHARED/latest.jpg"
CAMERA_ID="${CAMERA_ID:-0}"
INTERVAL="${INTERVAL:-0.35}"
PORT="${PORT:-8765}"
MAX_ZOOM="${MAX_ZOOM:-4.0}"

mkdir -p "$SHARED"

echo "Starting Termux camera capture in background"
CAMERA_ID="$CAMERA_ID" INTERVAL="$INTERVAL" bash "$APP_DST/termux/capture_loop.sh" "$LATEST" &
CAPTURE_PID=$!

cleanup() {
  kill "$CAPTURE_PID" 2>/dev/null || true
}
trap cleanup EXIT

echo "Starting Ubuntu DTK runner"
proot-distro login ubuntu --shared-tmp -- bash "$APP_DST/ubuntu/run_ubuntu.sh" \
  --source watch-file \
  --input "$LATEST" \
  --serve "$PORT" \
  --max-zoom "$MAX_ZOOM"
