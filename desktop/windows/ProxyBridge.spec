# -*- mode: python ; coding: utf-8 -*-
# Generated/maintained for Windows tray packaging.
# Prefer scripts/build_windows_exe.ps1 which can regenerate and build this.

import sys
from pathlib import Path

block_cipher = None
# spec 位于 desktop/windows；向上两级才是包含 VERSION 与应用源码的仓库根目录。
root = Path(SPECPATH).resolve().parents[1]

datas = [
    (str(root / "VERSION"), "."),
    (str(root / "app.py"), "."),
    (str(root / "proxybridge_version.py"), "."),
    # 软件更新：载荷启动器与更新模块；SHELL_REVISION 由 scripts/build_windows_exe.ps1 生成。
    (str(root / "app_payload.py"), "."),
    (str(root / "app_update.py"), "."),
    (str(root / "webdav_sync.py"), "."),
    # 主模块会静态导入 Broker 客户端；桌面包也必须包含该模块以保证启动一致。
    (str(root / "remote_browser.py"), "."),
    # 指纹浏览器配置、Cookie 格式与 CDP 客户端模块，由主模块静态导入。
    (str(root / "browser_fingerprint.py"), "."),
    (str(root / "static"), "static"),
    (str(root / "desktop"), "desktop"),
]

hiddenimports = [
    "app",
    "proxybridge_version",
    "app_payload",
    "app_update",
    "webdav_sync",
    # PyInstaller 的隐藏导入用于收集远程浏览器客户端，不会在桌面模式启用 Docker worker。
    "remote_browser",
    "browser_fingerprint",
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
