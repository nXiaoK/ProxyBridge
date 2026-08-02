#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

python3 -m pip install -r desktop/requirements.txt >/dev/null
python3 -m pip install "pyinstaller>=6.0" >/dev/null

OUT_DIR="$ROOT_DIR/outputs/linux"
mkdir -p "$OUT_DIR"

# 主模块会导入远程浏览器 Broker 客户端；二进制包需同时收集源码与隐藏导入。
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
  --add-data "remote_browser.py:." \
  --add-data "static:static" \
  --add-data "desktop:desktop" \
  --hidden-import app \
  --hidden-import webdav_sync \
  --hidden-import remote_browser \
  --hidden-import desktop \
  --hidden-import desktop.tray_host \
  --hidden-import desktop.autostart \
  --hidden-import pystray \
  --hidden-import PIL \
  --hidden-import PIL.Image \
  desktop/tray_host.py

echo "Linux binary: $OUT_DIR/dist/ProxyBridge"
