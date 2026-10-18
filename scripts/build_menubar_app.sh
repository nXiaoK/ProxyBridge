#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
OUTPUT_DIR="$ROOT_DIR/outputs"
FINAL_APP_DIR="$OUTPUT_DIR/ProxyBridgeMenuBar.app"
# VERSION 保存人工维护的 SemVer 基础版本；下面两个环境变量只用于发布构建注入完整版本与构建号。
VERSION_FILE="$ROOT_DIR/VERSION"
BASE_VERSION=""
APP_VERSION=""
BUILD_NUMBER="${PROXY_BRIDGE_BUILD_NUMBER:-1}"
STAGING_ROOT=""
APP_DIR=""
BUILD_LOCK_DIR="$OUTPUT_DIR/.ProxyBridgeMenuBar.build.lock"
BUILD_LOCK_HELD=0
# TARGET_ARCH 默认只构建项目支持的 Apple Silicon 版本；需要交叉编译时可显式覆盖。
TARGET_ARCH="${TARGET_ARCH:-arm64}"
SWIFT_TARGET="${TARGET_ARCH}-apple-macos13.0"
REQUIREMENTS_LOCK="$ROOT_DIR/scripts/requirements.lock"
VENDOR_PYTHON="${PYTHON_FOR_VENDOR:-}"
ICON_PATH="$ROOT_DIR/macos/ProxyBridgeMenuBar/AppIcon.icns"
WEB_ICON_PATH="$ROOT_DIR/static/assets/app-icon.png"
PREVIEW_ICON_PATH="$ROOT_DIR/outputs/ProxyBridgeAppIcon.png"
ICON_TOOL="$ROOT_DIR/work/generate_app_icon"
ATOMIC_REPLACE_TOOL="$ROOT_DIR/work/atomic_replace_app"
BUNDLE_CORES=0
# 完整版（离线包）使用：指向 scripts/fetch_bundled_packages.py 的输出目录，其中的 sing-box / xray /
# fingerprint-chromium 官方安装包会复制到 App 内 bridge/bundled/，首次启动时自动安装；为空表示构建轻量版。
BUNDLED_PACKAGES_DIR="${PROXY_BRIDGE_BUNDLED_PACKAGES:-}"

cleanup() {
  local status=$?
  trap - EXIT
  if [[ -n "$STAGING_ROOT" && -d "$STAGING_ROOT" ]]; then
    rm -rf "$STAGING_ROOT"
  fi
  if [[ "$BUILD_LOCK_HELD" == "1" && -d "$BUILD_LOCK_DIR" ]]; then
    rm -rf "$BUILD_LOCK_DIR"
  fi
  exit "$status"
}
trap cleanup EXIT

while [[ $# -gt 0 ]]; do
  arg="$1"
  shift
  case "$arg" in
    --bundle-cores|--with-cores)
      BUNDLE_CORES=1
      ;;
    --bundle-packages)
      if [[ $# -eq 0 ]]; then
        echo "--bundle-packages 需要指定目录" >&2
        exit 1
      fi
      BUNDLED_PACKAGES_DIR="$1"
      shift
      ;;
    --bundle-packages=*)
      BUNDLED_PACKAGES_DIR="${arg#*=}"
      ;;
    -h|--help)
      cat <<'EOF'
Usage: scripts/build_menubar_app.sh [--bundle-cores] [--bundle-packages DIR]

默认构建轻量 App,不内置 Python/sing-box/xray。首次运行时会自动下载 Python
运行时到 ~/Library/Application Support/ProxyBridge/python-runtime,并下载代理核心到
~/Library/Application Support/ProxyBridge/proxy-cores。

Options:
  --bundle-cores, --with-cores  从构建机复制 sing-box/xray 到 App 内部。
  --bundle-packages DIR         构建完整版：把 DIR 下 sing-box/、xray/、fingerprint-chromium/ 中的
                                官方安装包放入 App，首次启动自动安装（DIR 由
                                scripts/fetch_bundled_packages.py 生成）。
  -h, --help                   显示帮助。

Environment:
  PYTHON_FOR_VENDOR=/path/python3          用于构建 bridge/vendor 的 Python。
  PROXY_BRIDGE_VENDOR_WHEELHOUSE=/path     从离线 wheelhouse 安装锁定依赖。
  PROXY_BRIDGE_VERSION=0.3.0-build.12      写入 App 和内嵌后端的完整版本。
  PROXY_BRIDGE_BUILD_NUMBER=12             写入 CFBundleVersion 的纯数字构建号。
  TARGET_ARCH=arm64                        目标 CPU 架构；当前发布仅支持 Apple Silicon。
  PROXY_BRIDGE_BUNDLED_PACKAGES=/path      等同 --bundle-packages，用于构建完整版。
EOF
      exit 0
      ;;
    *)
      echo "未知参数：$arg" >&2
      exit 1
      ;;
  esac
done

resolve_build_version() {
  if [[ ! -f "$VERSION_FILE" ]]; then
    echo "缺少版本文件：$VERSION_FILE" >&2
    exit 1
  fi
  IFS= read -r BASE_VERSION < "$VERSION_FILE" || true
  BASE_VERSION="${BASE_VERSION//$'\r'/}"
  APP_VERSION="${PROXY_BRIDGE_VERSION:-$BASE_VERSION}"

  if [[ ! "$BASE_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "VERSION 必须是三段数字 SemVer：$BASE_VERSION" >&2
    exit 1
  fi
  if [[ ! "$APP_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?(\+[0-9A-Za-z.-]+)?$ ]]; then
    echo "PROXY_BRIDGE_VERSION 格式无效：$APP_VERSION" >&2
    exit 1
  fi
  if [[ ! "$BUILD_NUMBER" =~ ^[0-9]+$ ]]; then
    echo "PROXY_BRIDGE_BUILD_NUMBER 必须是纯数字：$BUILD_NUMBER" >&2
    exit 1
  fi
}

resolve_build_version

acquire_build_lock() {
  local owner_pid=""
  if ! mkdir "$BUILD_LOCK_DIR" 2>/dev/null; then
    if [[ -r "$BUILD_LOCK_DIR/pid" ]]; then
      IFS= read -r owner_pid < "$BUILD_LOCK_DIR/pid" || true
    fi
    if [[ "$owner_pid" =~ ^[0-9]+$ ]] && kill -0 "$owner_pid" 2>/dev/null; then
      echo "已有 ProxyBridge 构建正在运行 (PID $owner_pid)" >&2
      exit 1
    fi
    rm -rf "$BUILD_LOCK_DIR"
    if ! mkdir "$BUILD_LOCK_DIR" 2>/dev/null; then
      echo "无法获取 ProxyBridge 构建锁：$BUILD_LOCK_DIR" >&2
      exit 1
    fi
  fi
  printf '%s\n' "$$" > "$BUILD_LOCK_DIR/pid"
  BUILD_LOCK_HELD=1
}

mkdir -p "$OUTPUT_DIR" "$ROOT_DIR/static/assets"
mkdir -p "$ROOT_DIR/work/clang-module-cache" "$ROOT_DIR/work/swift-module-cache"
acquire_build_lock
STAGING_ROOT="$(mktemp -d "$OUTPUT_DIR/.ProxyBridgeMenuBar.build.XXXXXX")"
APP_DIR="$STAGING_ROOT/ProxyBridgeMenuBar.app"
CONTENTS_DIR="$APP_DIR/Contents"
MACOS_DIR="$CONTENTS_DIR/MacOS"
RESOURCES_DIR="$CONTENTS_DIR/Resources"
BRIDGE_DIR="$RESOURCES_DIR/bridge"
CORE_DIR="$BRIDGE_DIR/bin"
VENDOR_DIR="$BRIDGE_DIR/vendor"

mkdir -p "$APP_DIR" "$CONTENTS_DIR" "$MACOS_DIR" "$RESOURCES_DIR" "$BRIDGE_DIR"

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

verify_mach_o_arch() {
  local label="$1"
  local binary_path="$2"
  local required_arch="$3"
  local kind=""

  kind="$(file -b "$binary_path")"
  if [[ "$kind" != *"Mach-O"* || "$kind" != *"$required_arch"* ]]; then
    echo "$label 架构不匹配，需要 $required_arch：$binary_path ($kind)" >&2
    exit 1
  fi
}

vendor_python_dependencies() {
  local wheelhouse="${PROXY_BRIDGE_VENDOR_WHEELHOUSE:-}"
  # macOS 自带 Bash 3.2 在 `set -u` 下展开空数组会报 unbound variable。
  # 保持数组至少有一个通用参数，兼容系统 Bash 与新版 Bash。
  local pip_args=(--disable-pip-version-check)

  if [[ ! -f "$REQUIREMENTS_LOCK" ]]; then
    echo "缺少 Python 依赖锁文件：$REQUIREMENTS_LOCK" >&2
    exit 1
  fi
  if [[ -z "$VENDOR_PYTHON" ]]; then
    VENDOR_PYTHON="$(command -v python3 || true)"
  fi
  if [[ -z "$VENDOR_PYTHON" || ! -x "$VENDOR_PYTHON" ]]; then
    echo "找不到用于 vendor 依赖的 python3，请设置 PYTHON_FOR_VENDOR=/path/to/python3。" >&2
    exit 1
  fi
  if ! "$VENDOR_PYTHON" -m pip --version >/dev/null 2>&1; then
    echo "指定的 Python 不包含 pip：$VENDOR_PYTHON" >&2
    exit 1
  fi

  if [[ -n "$wheelhouse" ]]; then
    if [[ ! -d "$wheelhouse" ]]; then
      echo "离线 wheelhouse 不存在：$wheelhouse" >&2
      exit 1
    fi
    pip_args+=(--no-index --find-links "$wheelhouse")
  fi

  rm -rf "$VENDOR_DIR"
  mkdir -p "$VENDOR_DIR"
  "$VENDOR_PYTHON" -m pip install \
    "${pip_args[@]}" \
    --no-deps \
    --no-compile \
    --require-hashes \
    --only-binary=:all: \
    --platform macosx_11_0_arm64 \
    --python-version 39 \
    --implementation cp \
    --abi cp39 \
    --target "$VENDOR_DIR" \
    -r "$REQUIREMENTS_LOCK"

  # PyYAML's Python modules work across runtime minor versions; discard any
  # build-machine-specific extension so the downloaded standalone runtime can import it.
  find "$VENDOR_DIR" -type f \( -name '*.so' -o -name '*.dylib' \) -delete
  find "$VENDOR_DIR" -type d -name '__pycache__' -prune -exec rm -rf {} +
  PYTHONPATH="$VENDOR_DIR" "$VENDOR_PYTHON" -c 'import yaml; assert yaml.safe_load("ok: true") == {"ok": True}'
}

bundle_proxy_cores() {
  local sing_box_source=""
  local sing_box_binary=""
  local xray_source=""
  local xray_binary=""

  sing_box_source="$(find_required_binary "sing-box" "SING_BOX_BIN" "sing-box")"
  sing_box_binary="$(resolve_mach_o_binary "sing-box" "$sing_box_source")"
  xray_source="$(find_required_binary "xray" "XRAY_BIN" "xray")"
  xray_binary="$(resolve_mach_o_binary "xray" "$xray_source")"
  verify_mach_o_arch "sing-box" "$sing_box_binary" "$TARGET_ARCH"
  verify_mach_o_arch "xray" "$xray_binary" "$TARGET_ARCH"

  mkdir -p "$CORE_DIR"
  install -m 0755 "$sing_box_binary" "$CORE_DIR/sing-box"
  install -m 0755 "$xray_binary" "$CORE_DIR/xray-core"

  cat > "$CORE_DIR/xray" <<'EOS'
#!/bin/sh
HERE="$(cd "$(dirname "$0")" && pwd)"
exec "$HERE/xray-core" "$@"
EOS
  chmod +x "$CORE_DIR/xray"

  echo "Bundled sing-box: $("$CORE_DIR/sing-box" version | sed -n '1p')"
  echo "Bundled xray: $("$CORE_DIR/xray" version | sed -n '1p')"
}

bundle_offline_packages() {
  local kind=""
  local count=0
  if [[ ! -d "$BUNDLED_PACKAGES_DIR" ]]; then
    echo "离线包目录不存在：$BUNDLED_PACKAGES_DIR" >&2
    exit 1
  fi
  for kind in sing-box xray fingerprint-chromium; do
    count="$(find "$BUNDLED_PACKAGES_DIR/$kind" -maxdepth 1 -type f ! -name '.*' 2>/dev/null | wc -l | tr -d ' ')"
    if [[ "$count" != "1" ]]; then
      echo "离线包目录 $BUNDLED_PACKAGES_DIR/$kind 必须恰好包含一个安装包（当前 $count 个）" >&2
      exit 1
    fi
    mkdir -p "$BRIDGE_DIR/bundled/$kind"
    find "$BUNDLED_PACKAGES_DIR/$kind" -maxdepth 1 -type f ! -name '.*' -exec cp {} "$BRIDGE_DIR/bundled/$kind/" \;
    echo "Bundled package ($kind): $(ls "$BRIDGE_DIR/bundled/$kind")"
  done
}

# 这两个工具只在构建机上执行，必须编译为 runner 本机架构；最终 App 仍按 SWIFT_TARGET 交叉编译。
swiftc \
  -O \
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
  -module-cache-path "$ROOT_DIR/work/swift-module-cache" \
  "$ROOT_DIR/scripts/atomic_replace_app.swift" \
  -o "$ATOMIC_REPLACE_TOOL"

swiftc \
  -O \
  -target "$SWIFT_TARGET" \
  -module-cache-path "$ROOT_DIR/work/swift-module-cache" \
  -framework AppKit \
  -framework Foundation \
  -framework WebKit \
  "$ROOT_DIR/macos/ProxyBridgeMenuBar/main.swift" \
  -o "$MACOS_DIR/ProxyBridgeMenuBar"

cp "$ROOT_DIR/macos/ProxyBridgeMenuBar/Info.plist" "$CONTENTS_DIR/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $BASE_VERSION" "$CONTENTS_DIR/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleVersion $BUILD_NUMBER" "$CONTENTS_DIR/Info.plist"
cp "$ICON_PATH" "$RESOURCES_DIR/AppIcon.icns"
# Backend Python modules (app.py plus any sibling packages it imports at startup).
cp "$ROOT_DIR/app.py" "$BRIDGE_DIR/app.py"
cp "$ROOT_DIR/webdav_sync.py" "$BRIDGE_DIR/webdav_sync.py"
# 主模块会导入远程浏览器 Broker 客户端；菜单栏包需保留该同级模块。
cp "$ROOT_DIR/remote_browser.py" "$BRIDGE_DIR/remote_browser.py"
# 指纹浏览器配置、Cookie 格式与本机 CDP 客户端模块。
cp "$ROOT_DIR/browser_fingerprint.py" "$BRIDGE_DIR/browser_fingerprint.py"
# 完整构建版本写入包内，运行时 API 与 macOS/Windows 文件元数据使用同一来源。
cp "$ROOT_DIR/proxybridge_version.py" "$BRIDGE_DIR/proxybridge_version.py"
printf '%s\n' "$APP_VERSION" > "$BRIDGE_DIR/VERSION"
ditto "$ROOT_DIR/static" "$BRIDGE_DIR/static"
vendor_python_dependencies
if [[ "$BUNDLE_CORES" == "1" ]]; then
  bundle_proxy_cores
else
  echo "Skipping bundled proxy cores; runtime will auto-download Python and sing-box/xray on first launch."
fi
if [[ -n "$BUNDLED_PACKAGES_DIR" ]]; then
  bundle_offline_packages
fi

chmod +x "$MACOS_DIR/ProxyBridgeMenuBar"
codesign --force --deep --sign - "$APP_DIR"
codesign --verify --deep --strict "$APP_DIR"

"$ATOMIC_REPLACE_TOOL" "$APP_DIR" "$FINAL_APP_DIR"
echo "ProxyBridge version: $APP_VERSION (build $BUILD_NUMBER)"
echo "$FINAL_APP_DIR"
