# -*- mode: python ; coding: utf-8 -*-
# Generated/maintained for Windows tray packaging.
# Prefer scripts/build_windows_exe.ps1 which can regenerate and build this.

import sys
from pathlib import Path

block_cipher = None
root = Path(SPECPATH).resolve()

datas = [
    (str(root / "app.py"), "."),
    (str(root / "webdav_sync.py"), "."),
    (str(root / "static"), "static"),
    (str(root / "desktop"), "desktop"),
]

hiddenimports = [
    "app",
    "webdav_sync",
    "desktop",
    "desktop.tray_host",
    "desktop.autostart",
    "pystray",
    "pystray._base",
    "pystray._win32",
    "PIL",
    "PIL.Image",
    "PIL.ImageDraw",
]

a = Analysis(
    [str(root / "desktop" / "tray_host.py")],
    pathex=[str(root)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="ProxyBridge",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(root / "static" / "assets" / "app-icon.png") if (root / "static" / "assets" / "app-icon.png").exists() else None,
)
