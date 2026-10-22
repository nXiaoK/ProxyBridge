#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

python3 -m pip install -r desktop/requirements.txt >/dev/null
python3 -m pip install "pyinstaller>=6.0" >/dev/null

OUT_DIR="$ROOT_DIR/outputs/linux"
mkdir -p "$OUT_DIR"

# 主模块会导入远程浏览器 Broker 客户端；VERSION 与解析模块也必须进入单文件产物。
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
  --add-data "browser_fingerprint.py:." \
  --add-data "VERSION:." \
  --add-data "proxybridge_version.py:." \
  --add-data "app_payload.py:." \
  --add-data "app_update.py:." \
  --add-data "static:static" \
  --add-data "desktop:desktop" \
  --hidden-import app \
  --hidden-import webdav_sync \
  --hidden-import remote_browser \
  --hidden-import browser_fingerprint \
  --hidden-import proxybridge_version \
  --hidden-import app_payload \
  --hidden-import app_update \
  --hidden-import desktop \
  --hidden-import desktop.tray_host \
  --hidden-import desktop.autostart \
  --hidden-import pystray \
  --hidden-import PIL \
  --hidden-import PIL.Image \
  desktop/tray_host.py

echo "Linux binary: $OUT_DIR/dist/ProxyBridge"
