#!/usr/bin/env bash
set -euo pipefail

APP_DST="${1:-/data/data/com.termux/files/home/dtk-alpr/app}"
VENV="$APP_DST/.venv"

echo "Ubuntu setup for $APP_DST"
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y \
  ca-certificates \
  ffmpeg \
  libgomp1 \
  python3 \
  python3-numpy \
  python3-pil \
  python3-pip \
  python3-venv

python3 -m venv --system-site-packages "$VENV"
"$VENV/bin/python" -m pip install --upgrade pip

# Optional. If the wheel is unavailable on the phone, the app still runs using
# DTK plate boxes as zoom targets. YOLO car targeting activates only when both
# onnxruntime and models/car_yolo.onnx exist.
"$VENV/bin/python" -m pip install onnxruntime || true

chmod +x "$APP_DST/termux/"*.sh "$APP_DST/ubuntu/"*.sh
echo "Ubuntu setup complete"
