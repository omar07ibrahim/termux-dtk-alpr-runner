#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail

OUT="${1:-$HOME/dtk-alpr/shared/latest.jpg}"
CAMERA_ID="${CAMERA_ID:-0}"
INTERVAL="${INTERVAL:-0.35}"

mkdir -p "$(dirname "$OUT")"
echo "Termux camera loop -> $OUT"
echo "Camera id: $CAMERA_ID"

while true; do
  TMP="$OUT.tmp.jpg"
  if termux-camera-photo -c "$CAMERA_ID" "$TMP" >/dev/null 2>&1; then
    mv "$TMP" "$OUT"
  else
    rm -f "$TMP"
    echo "termux-camera-photo failed; is Termux:API installed and camera permission granted?"
    sleep 1
  fi
  sleep "$INTERVAL"
done
