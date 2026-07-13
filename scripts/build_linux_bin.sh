#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

python3 -m pip install -r desktop/requirements.txt >/dev/null
python3 -m pip install "pyinstaller>=6.0" >/dev/null

OUT_DIR="$ROOT_DIR/outputs/linux"
mkdir -p "$OUT_DIR"

python3 -m PyInstaller \
  --noconfirm \
  --clean \
  --onefile \
  --name ProxyBridge \
  --distpath "$OUT_DIR/dist" \
  --workpath "$OUT_DIR/build" \
  --specpath "$OUT_DIR" \
  --add-data "app.py:." \
  --add-data "webdav_sync.py:." \
  --add-data "static:static" \
  --add-data "desktop:desktop" \
  --hidden-import app \
  --hidden-import webdav_sync \
  --hidden-import desktop \
  --hidden-import desktop.tray_host \
  --hidden-import desktop.autostart \
  --hidden-import pystray \
  --hidden-import PIL \
  --hidden-import PIL.Image \
  desktop/tray_host.py

echo "Linux binary: $OUT_DIR/dist/ProxyBridge"
