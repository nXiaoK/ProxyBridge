#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

if ! python3 -c "import pystray, PIL" >/dev/null 2>&1; then
  echo "Installing tray dependencies into the current Python environment..."
  python3 -m pip install -r desktop/requirements.txt
fi

exec python3 -m desktop.tray_host "$@"
