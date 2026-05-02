#!/usr/bin/env bash
set -euo pipefail

APP_DST="$(cd "$(dirname "$0")/.." && pwd)"
DTK_DIR="$APP_DST/vendor/arm64"

if [ ! -x "$DTK_DIR/DTKLPRActivate" ]; then
  chmod +x "$DTK_DIR/DTKLPRActivate" 2>/dev/null || true
fi

if [ ! -x "$DTK_DIR/DTKLPRActivate" ]; then
  echo "ERROR: missing $DTK_DIR/DTKLPRActivate"
  echo "Copy DTK ARM64 files to $DTK_DIR first."
  exit 2
fi

cd "$DTK_DIR"
export LD_LIBRARY_PATH="$DTK_DIR:${LD_LIBRARY_PATH:-}"

case "${1:-}" in
  "")
    ./DTKLPRActivate /licinfo || true
    ./DTKLPRActivate /systemid || true
    ;;
  /licinfo|licinfo)
    ./DTKLPRActivate /licinfo
    ;;
  /systemid|systemid)
    ./DTKLPRActivate /systemid
    ;;
  /getactlink|getactlink)
    shift
    ./DTKLPRActivate /getactlink "$@"
    ;;
  /setactcode|setactcode)
    shift
    ./DTKLPRActivate /setactcode "$@"
    ;;
  /activate|activate)
    shift
    ./DTKLPRActivate /activate "$@"
    ;;
  *)
    ./DTKLPRActivate /activate "$@"
    ;;
esac
