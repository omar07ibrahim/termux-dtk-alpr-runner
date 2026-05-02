#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail

APP_SRC="$(cd "$(dirname "$0")/.." && pwd)"
APP_HOME="$HOME/dtk-alpr"
APP_DST="$APP_HOME/app"
VENDOR_DST="$APP_DST/vendor/arm64"

echo "[1/5] Installing Termux packages"
pkg update -y
pkg install -y proot-distro termux-api rsync unzip

echo "[2/5] Installing Ubuntu proot"
if ! proot-distro list | grep -A2 '^  ubuntu' | grep -q installed; then
  proot-distro install ubuntu
fi

echo "[3/5] Copying runner to $APP_DST"
mkdir -p "$APP_HOME"
rsync -a --delete "$APP_SRC/" "$APP_DST/"

find_arm64_dir() {
  for candidate in \
    "$APP_SRC/vendor/arm64" \
    "$APP_SRC/arm64" \
    "$HOME/arm64" \
    "/sdcard/Download/arm64" \
    "/sdcard/Download/Telegram/arm64" \
    "/storage/emulated/0/Download/arm64" \
    "/storage/emulated/0/Download/Telegram/arm64"; do
    if [ -f "$candidate/libDTKLPR.so" ] && [ -f "$candidate/DTKLPR.dat" ]; then
      echo "$candidate"
      return 0
    fi
  done
  return 1
}

find_arm64_zip() {
  for candidate in \
    "$APP_SRC/arm64.zip" \
    "$HOME/arm64.zip" \
    "/sdcard/Download/arm64.zip" \
    "/sdcard/Download/Telegram/arm64.zip" \
    "/storage/emulated/0/Download/arm64.zip" \
    "/storage/emulated/0/Download/Telegram/arm64.zip"; do
    if [ -f "$candidate" ]; then
      echo "$candidate"
      return 0
    fi
  done
  return 1
}

echo "[4/5] Locating DTK ARM64 package"
mkdir -p "$VENDOR_DST"
if ARM_DIR="$(find_arm64_dir)"; then
  rsync -a "$ARM_DIR/" "$VENDOR_DST/"
elif ARM_ZIP="$(find_arm64_zip)"; then
  rm -rf "$VENDOR_DST"
  mkdir -p "$VENDOR_DST"
  unzip -o "$ARM_ZIP" -d "$VENDOR_DST"
  if [ -d "$VENDOR_DST/arm64" ]; then
    rsync -a "$VENDOR_DST/arm64/" "$VENDOR_DST/"
    rm -rf "$VENDOR_DST/arm64"
  fi
else
  echo "ERROR: DTK ARM64 package not found."
  echo "Put arm64/ or arm64.zip under /sdcard/Download and re-run this installer."
  exit 2
fi
chmod +x "$VENDOR_DST/DTKLPRActivate" 2>/dev/null || true

echo "[5/5] Setting up Ubuntu packages"
proot-distro login ubuntu --shared-tmp -- bash "$APP_DST/ubuntu/setup_ubuntu.sh" "$APP_DST"

cat <<EOF

Done.

Start:
  bash $APP_DST/termux/run_phone.sh

Dashboard:
  http://127.0.0.1:8765/

EOF
