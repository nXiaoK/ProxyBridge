#!/usr/bin/env python3
"""Login autostart helpers for ProxyBridge tray host."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


AUTOSTART_APP_NAME = "ProxyBridge"
AUTOSTART_ARGS = ["--no-open"]


def host_os_token() -> str:
    system = sys.platform
    if system.startswith("win"):
        return "windows"
    if system == "darwin":
        return "darwin"
    return "linux"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False)) or hasattr(sys, "_MEIPASS")


def resolve_python_executable() -> str:
    return sys.executable


def current_launch_command() -> list[str]:
    if is_frozen():
        return [str(Path(sys.executable).resolve()), *AUTOSTART_ARGS]
    return [resolve_python_executable(), "-m", "desktop.tray_host", *AUTOSTART_ARGS]


def windows_startup_shortcut_path() -> Path:
    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return (
        Path(appdata)
        / "Microsoft"
        / "Windows"
        / "Start Menu"
        / "Programs"
        / "Startup"
        / f"{AUTOSTART_APP_NAME}.lnk"
    )


def linux_autostart_desktop_path() -> Path:
    config_home = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(config_home) / "autostart" / f"{AUTOSTART_APP_NAME.lower()}.desktop"


def darwin_launch_agent_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / "com.proxybridge.tray.plist"


def quote_windows_cmd(parts: list[str]) -> str:
    def quote(part: str) -> str:
        if not part or re.search(r'[\s"]', part):
            escaped = part.replace('"', '\\"')
            return f'"{escaped}"'
        return part

    return " ".join(quote(part) for part in parts)


def write_windows_shortcut(path: Path, command: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    target = command[0]
    arguments = quote_windows_cmd(command[1:]) if len(command) > 1 else ""
    workdir = str(Path(target).resolve().parent)

    # Escape for PowerShell double-quoted strings.
    def ps_escape(value: str) -> str:
        return (
            value.replace("`", "``")
            .replace('"', '`"')
            .replace("$", "`$")
        )

    ps = "\n".join(
        [
            '$ErrorActionPreference = "Stop"',
            "$ws = New-Object -ComObject WScript.Shell",
            f'$sc = $ws.CreateShortcut("{ps_escape(str(path))}")',
            f'$sc.TargetPath = "{ps_escape(target)}"',
            f'$sc.Arguments = "{ps_escape(arguments)}"',
            f'$sc.WorkingDirectory = "{ps_escape(workdir)}"',
            "$sc.WindowStyle = 7",
            '$sc.Description = "ProxyBridge tray host"',
            "$sc.Save()",
        ]
    )
    completed = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            ps,
        ],
        capture_output=True,
        text=True,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "unknown error").strip()
        raise RuntimeError(f"failed to create Windows startup shortcut: {detail}")


def is_autostart_enabled() -> bool:
    os_token = host_os_token()
    if os_token == "windows":
        return windows_startup_shortcut_path().is_file()
    if os_token == "linux":
        path = linux_autostart_desktop_path()
        if not path.is_file():
            return False
        try:
            text_value = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False
        return "Hidden=true" not in text_value
    if os_token == "darwin":
        return darwin_launch_agent_path().is_file()
    return False


def set_autostart_enabled(enabled: bool, command: list[str] | None = None) -> dict[str, Any]:
    os_token = host_os_token()
    launch_command = list(command or current_launch_command())
    if os_token == "windows":
        shortcut = windows_startup_shortcut_path()
        if enabled:
            write_windows_shortcut(shortcut, launch_command)
        elif shortcut.exists():
            shortcut.unlink()
        return {"ok": True, "enabled": enabled, "path": str(shortcut), "os": os_token}

    if os_token == "linux":
        desktop = linux_autostart_desktop_path()
        if enabled:
            desktop.parent.mkdir(parents=True, exist_ok=True)
            exec_line = " ".join(part if " " not in part else f'"{part}"' for part in launch_command)
            content = "\n".join(
                [
                    "[Desktop Entry]",
                    "Type=Application",
                    f"Name={AUTOSTART_APP_NAME}",
                    f"Exec={exec_line}",
                    "X-GNOME-Autostart-enabled=true",
                    "Hidden=false",
                    "NoDisplay=false",
                    "Terminal=false",
                    "Comment=ProxyBridge tray host",
                    "",
                ]
            )
            desktop.write_text(content, encoding="utf-8")
            try:
                desktop.chmod(0o644)
            except OSError:
                pass
        elif desktop.exists():
            desktop.unlink()
        return {"ok": True, "enabled": enabled, "path": str(desktop), "os": os_token}

    if os_token == "darwin":
        plist = darwin_launch_agent_path()
        if enabled:
            plist.parent.mkdir(parents=True, exist_ok=True)
            args_xml = "\n".join(f"    <string>{part}</string>" for part in launch_command)
            content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.proxybridge.tray</string>
  <key>ProgramArguments</key>
  <array>
{args_xml}
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <false/>
</dict>
</plist>
"""
            plist.write_text(content, encoding="utf-8")
        elif plist.exists():
            plist.unlink()
        return {"ok": True, "enabled": enabled, "path": str(plist), "os": os_token}

    raise RuntimeError(f"autostart is not supported on {os_token}")
