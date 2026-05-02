#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail

SCRIPT_APP="$(cd "$(dirname "$0")/.." && pwd)"
APP_HOME="${APP_HOME:-$HOME/dtk-alpr}"
APP_DST="${APP_DST:-$APP_HOME/app}"
if [ ! -x "$APP_DST/ubuntu/run_ubuntu.sh" ] && [ -x "$SCRIPT_APP/ubuntu/run_ubuntu.sh" ]; then
  APP_DST="$SCRIPT_APP"
fi

if [ ! -x "$APP_DST/ubuntu/run_ubuntu.sh" ]; then
  echo "ERROR: installed runner not found. Run termux/install.sh first."
  exit 2
fi

PHOTO_SRC="${1:-${PHOTO_TEST_PATH:-/sdcard/Download/alpr-samples/sample1.jpg}}"
PHOTO_DST="$APP_DST/runtime/photo_test_input.jpg"

if [ ! -f "$PHOTO_SRC" ]; then
  cat >&2 <<EOF
ERROR: photo not found:
  $PHOTO_SRC

Put a JPEG/PNG on the phone and run:
  bash $APP_DST/termux/run_photo_test.sh /sdcard/Download/your_photo.jpg
EOF
  exit 2
fi

mkdir -p "$APP_DST/runtime"
cp "$PHOTO_SRC" "$PHOTO_DST"

echo "Starting DTK still-photo test:"
echo "  $PHOTO_SRC"
echo

proot-distro login ubuntu --shared-tmp -- bash "$APP_DST/ubuntu/run_ubuntu.sh" \
  --source file \
  --input "$PHOTO_DST" \
  --once \
  --serve 0
