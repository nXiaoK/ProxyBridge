#!/usr/bin/env python3
"""Login autostart helpers for ProxyBridge tray host."""

from __future__ import annotations

import os
import plistlib
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
    tray_script = Path(__file__).resolve().with_name("tray_host.py")
    return [str(Path(resolve_python_executable()).resolve()), str(tray_script), *AUTOSTART_ARGS]


def current_launch_working_directory() -> Path:
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


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
    return subprocess.list2cmdline(parts)


def write_windows_shortcut(
    path: Path,
    command: list[str],
    *,
    working_directory: Path | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    target = command[0]
    arguments = quote_windows_cmd(command[1:]) if len(command) > 1 else ""
    workdir = str((working_directory or Path(target).resolve().parent).resolve())

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
        return not any(line.strip().lower() == "hidden=true" for line in text_value.splitlines())
    if os_token == "darwin":
        return darwin_launch_agent_path().is_file()
    return False


def _quote_desktop_exec_arg(value: str) -> str:
    if not value or "\x00" in value or "\n" in value or "\r" in value:
        if value:
            raise ValueError("autostart command contains an invalid character")
        return '""'
    if re.fullmatch(r"[A-Za-z0-9_@+=:,./-]+", value):
        return value
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("`", "\\`")
        .replace("$", "\\$")
        .replace("%", "%%")
    )
    return f'"{escaped}"'


def _escape_desktop_value(value: str) -> str:
    if "\x00" in value or "\n" in value or "\r" in value:
        raise ValueError("autostart path contains an invalid character")
    return value.replace("\\", "\\\\")


def set_autostart_enabled(
    enabled: bool,
    command: list[str] | None = None,
    *,
    working_directory: Path | None = None,
) -> dict[str, Any]:
    os_token = host_os_token()
    launch_command = list(command or current_launch_command())
    workdir = (working_directory or current_launch_working_directory()).expanduser().resolve()
    if os_token == "windows":
        shortcut = windows_startup_shortcut_path()
        if enabled:
            write_windows_shortcut(shortcut, launch_command, working_directory=workdir)
        elif shortcut.exists():
            shortcut.unlink()
        return {"ok": True, "enabled": enabled, "path": str(shortcut), "os": os_token}

    if os_token == "linux":
        desktop = linux_autostart_desktop_path()
        if enabled:
            desktop.parent.mkdir(parents=True, exist_ok=True)
            exec_line = " ".join(_quote_desktop_exec_arg(part) for part in launch_command)
            content = "\n".join(
                [
                    "[Desktop Entry]",
                    "Type=Application",
                    f"Name={AUTOSTART_APP_NAME}",
                    f"Exec={exec_line}",
                    f"Path={_escape_desktop_value(str(workdir))}",
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
            payload = {
                "Label": "com.proxybridge.tray",
                "ProgramArguments": launch_command,
                "WorkingDirectory": str(workdir),
                "RunAtLoad": True,
                "KeepAlive": False,
            }
            plist.write_bytes(plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=False))
        elif plist.exists():
            plist.unlink()
        return {"ok": True, "enabled": enabled, "path": str(plist), "os": os_token}

    raise RuntimeError(f"autostart is not supported on {os_token}")
