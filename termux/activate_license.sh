#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail

SCRIPT_APP="$(cd "$(dirname "$0")/.." && pwd)"
APP_HOME="${APP_HOME:-$HOME/dtk-alpr}"
APP_DST="${APP_DST:-$APP_HOME/app}"
if [ ! -x "$APP_DST/ubuntu/activate_license.sh" ] && [ -x "$SCRIPT_APP/ubuntu/activate_license.sh" ]; then
  APP_DST="$SCRIPT_APP"
fi

if [ ! -x "$APP_DST/ubuntu/activate_license.sh" ]; then
  echo "ERROR: installed runner not found. Run termux/install.sh first."
  exit 2
fi

proot-distro login ubuntu --shared-tmp -- bash "$APP_DST/ubuntu/activate_license.sh" "$@"
