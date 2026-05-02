#!/usr/bin/env bash
set -euo pipefail

APP_DST="$(cd "$(dirname "$0")/.." && pwd)"
VENV="$APP_DST/.venv"

if [ ! -x "$VENV/bin/python" ]; then
  echo "Missing venv. Run termux/install.sh first."
  exit 2
fi

cd "$APP_DST"
export LD_LIBRARY_PATH="$APP_DST/vendor/arm64:${LD_LIBRARY_PATH:-}"
exec "$VENV/bin/python" -m alpr_runner.multi_video \
  --dtk-dir "$APP_DST/vendor/arm64" \
  --out "$APP_DST/runtime-multi" \
  "$@"
