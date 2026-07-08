#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
APP_DIR="$ROOT_DIR/outputs/ProxyBridgeMenuBar.app"
CONTENTS_DIR="$APP_DIR/Contents"
MACOS_DIR="$CONTENTS_DIR/MacOS"
RESOURCES_DIR="$CONTENTS_DIR/Resources"
BRIDGE_DIR="$RESOURCES_DIR/bridge"
CORE_DIR="$BRIDGE_DIR/bin"
ICON_PATH="$ROOT_DIR/macos/ProxyBridgeMenuBar/AppIcon.icns"
WEB_ICON_PATH="$ROOT_DIR/static/assets/app-icon.png"
PREVIEW_ICON_PATH="$ROOT_DIR/outputs/ProxyBridgeAppIcon.png"
ICON_TOOL="$ROOT_DIR/work/generate_app_icon"
BUNDLE_CORES=0

for arg in "$@"; do
  case "$arg" in
    --bundle-cores|--with-cores)
      BUNDLE_CORES=1
      ;;
    -h|--help)
      cat <<'EOF'
Usage: scripts/build_menubar_app.sh [--bundle-cores]

默认构建轻量 App,不内置 sing-box/xray。首次运行时会自动下载代理核心到
~/Library/Application Support/ProxyBridge/proxy-cores。

Options:
  --bundle-cores, --with-cores  从构建机复制 sing-box/xray 到 App 内部。
  -h, --help                   显示帮助。
EOF
      exit 0
      ;;
    *)
      echo "未知参数：$arg" >&2
      exit 1
      ;;
  esac
done

mkdir -p "$APP_DIR" "$CONTENTS_DIR" "$MACOS_DIR" "$RESOURCES_DIR" "$BRIDGE_DIR" "$ROOT_DIR/outputs"
mkdir -p "$ROOT_DIR/static/assets"
mkdir -p "$ROOT_DIR/work/clang-module-cache" "$ROOT_DIR/work/swift-module-cache"

export CLANG_MODULE_CACHE_PATH="$ROOT_DIR/work/clang-module-cache"

find_required_binary() {
  local label="$1"
  local env_name="$2"
  local command_name="$3"
  local env_value="${!env_name:-}"
  local found=""

  if [[ -n "$env_value" ]]; then
    if [[ -x "$env_value" ]]; then
      printf '%s\n' "$env_value"
      return
    fi
    echo "指定的 $label 不可执行：$env_value" >&2
    exit 1
  fi

  found="$(command -v "$command_name" || true)"
  if [[ -n "$found" && -x "$found" ]]; then
    printf '%s\n' "$found"
    return
  fi

  echo "找不到 $label。请先在构建机安装，或通过 $env_name=/path/to/$command_name 指定。" >&2
  exit 1
}

resolve_mach_o_binary() {
  local label="$1"
  local source_path="$2"
  local kind=""
  local exec_path=""

  kind="$(file -b "$source_path")"
  if [[ "$kind" == *"Mach-O"* ]]; then
    printf '%s\n' "$source_path"
    return
  fi

  exec_path="$(sed -n 's/.*exec "\([^"]*\)".*/\1/p' "$source_path" | sed -n '1p')"
  if [[ -n "$exec_path" && -x "$exec_path" ]]; then
    printf '%s\n' "$exec_path"
    return
  fi

  echo "$label 不是可直接打包的 Mach-O 二进制，也无法从 wrapper 解析真实路径：$source_path" >&2
  exit 1
}

resolve_xray_asset_dir() {
  local wrapper_path="$1"
  local real_binary="$2"
  local parsed=""
  local wrapper_kind=""
  local candidates=()

  if [[ -n "${XRAY_ASSET_DIR:-}" ]]; then
    candidates+=("$XRAY_ASSET_DIR")
  fi

  wrapper_kind="$(file -b "$wrapper_path")"
  if [[ "$wrapper_kind" != *"Mach-O"* ]]; then
    parsed="$(sed -n 's/.*XRAY_LOCATION_ASSET="${XRAY_LOCATION_ASSET:-\([^}]*\)}".*/\1/p' "$wrapper_path" | sed -n '1p')"
  fi
  candidates+=(
    "$parsed"
    "$(dirname "$wrapper_path")/../share/xray"
    "$(dirname "$real_binary")/../share/xray"
    "/opt/homebrew/opt/xray/share/xray"
    "/usr/local/opt/xray/share/xray"
  )

  local candidate=""
  for candidate in "${candidates[@]}"; do
    if [[ -n "$candidate" && -f "$candidate/geoip.dat" && -f "$candidate/geosite.dat" ]]; then
      printf '%s\n' "$candidate"
      return
    fi
  done

  echo "找不到 Xray 资源文件 geoip.dat / geosite.dat。请通过 XRAY_ASSET_DIR=/path/to/share/xray 指定。" >&2
  exit 1
}

bundle_proxy_cores() {
  local sing_box_source=""
  local sing_box_binary=""
  local xray_source=""
  local xray_binary=""
  local xray_asset_dir=""

  sing_box_source="$(find_required_binary "sing-box" "SING_BOX_BIN" "sing-box")"
  sing_box_binary="$(resolve_mach_o_binary "sing-box" "$sing_box_source")"
  xray_source="$(find_required_binary "xray" "XRAY_BIN" "xray")"
  xray_binary="$(resolve_mach_o_binary "xray" "$xray_source")"
  xray_asset_dir="$(resolve_xray_asset_dir "$xray_source" "$xray_binary")"

  mkdir -p "$CORE_DIR" "$CORE_DIR/xray-assets"
  install -m 0755 "$sing_box_binary" "$CORE_DIR/sing-box"
  install -m 0755 "$xray_binary" "$CORE_DIR/xray-core"
  cp "$xray_asset_dir/geoip.dat" "$CORE_DIR/xray-assets/geoip.dat"
  cp "$xray_asset_dir/geosite.dat" "$CORE_DIR/xray-assets/geosite.dat"

  cat > "$CORE_DIR/xray" <<'EOS'
#!/bin/sh
HERE="$(cd "$(dirname "$0")" && pwd)"
XRAY_LOCATION_ASSET="${XRAY_LOCATION_ASSET:-$HERE/xray-assets}" exec "$HERE/xray-core" "$@"
EOS
  chmod +x "$CORE_DIR/xray"

  echo "Bundled sing-box: $("$CORE_DIR/sing-box" version | sed -n '1p')"
  echo "Bundled xray: $("$CORE_DIR/xray" version | sed -n '1p')"
}

swiftc \
  -O \
  -target arm64-apple-macos13.0 \
  -module-cache-path "$ROOT_DIR/work/swift-module-cache" \
  -framework CoreGraphics \
  -framework Foundation \
  -framework ImageIO \
  -framework UniformTypeIdentifiers \
  "$ROOT_DIR/scripts/generate_app_icon.swift" \
  -o "$ICON_TOOL"
"$ICON_TOOL" "$ICON_PATH" "$WEB_ICON_PATH" "$PREVIEW_ICON_PATH" >/dev/null

swiftc \
  -O \
  -target arm64-apple-macos13.0 \
  -module-cache-path "$ROOT_DIR/work/swift-module-cache" \
  -framework AppKit \
  -framework Foundation \
  -framework WebKit \
  "$ROOT_DIR/macos/ProxyBridgeMenuBar/main.swift" \
  -o "$MACOS_DIR/ProxyBridgeMenuBar"

cp "$ROOT_DIR/macos/ProxyBridgeMenuBar/Info.plist" "$CONTENTS_DIR/Info.plist"
cp "$ICON_PATH" "$RESOURCES_DIR/AppIcon.icns"
cp "$ROOT_DIR/app.py" "$BRIDGE_DIR/app.py"
ditto "$ROOT_DIR/static" "$BRIDGE_DIR/static"
rm -rf "$CORE_DIR"
if [[ "$BUNDLE_CORES" == "1" ]]; then
  bundle_proxy_cores
else
  echo "Skipping bundled proxy cores; runtime will auto-download sing-box/xray on first launch."
fi

chmod +x "$MACOS_DIR/ProxyBridgeMenuBar"
codesign --force --deep --sign - "$APP_DIR"
echo "$APP_DIR"
