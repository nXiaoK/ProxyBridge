#!/usr/bin/env python3
"""ProxyBridge desktop tray host for Linux / Windows (and macOS fallback).

Starts the headless `app.py` backend, keeps it alive, and exposes a system-tray
menu that opens the web console in the default browser.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    from desktop import autostart
except ImportError:  # pragma: no cover - frozen layout fallback
    import autostart  # type: ignore


DEFAULT_BACKEND_PORT = 18787
BACKEND_INSTANCE_HEADER = "X-ProxyBridge-Instance"
READINESS_TIMEOUT = 12.0
READINESS_POLL_INTERVAL = 0.2
TERMINATE_TIMEOUT = 8.0
STATUS_REFRESH_INTERVAL = 5.0
BACKEND_ONLY_FLAG = "--backend-only"
TRAY_LOG_MAX_BYTES = 4 * 1024 * 1024
TRAY_LOG_BACKUP_COUNT = 3
TRAY_LOG_LOCK = threading.RLock()


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False)) or hasattr(sys, "_MEIPASS")


def resource_root() -> Path:
    """Bundled read-only assets (app.py, static/, icons) root."""
    meipass = getattr(sys, "_MEIPASS", None)
    if is_frozen() and meipass:
        return Path(str(meipass))
    return Path(__file__).resolve().parent.parent


def install_root() -> Path:
    """Directory containing the running executable or source checkout."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


# Backward-compatible alias used by tests and docs.
ROOT_DIR = resource_root()


def host_os_token() -> str:
    system = sys.platform
    if system.startswith("win"):
        return "windows"
    if system == "darwin":
        return "darwin"
    return "linux"


def default_work_root() -> Path:
    env = os.environ.get("PROXY_BRIDGE_WORKDIR")
    if env:
        return Path(env).expanduser()
    os_token = host_os_token()
    if os_token == "windows":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "ProxyBridge"
    if os_token == "darwin":
        return Path.home() / "Library" / "Application Support" / "ProxyBridge"
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg).expanduser() / "ProxyBridge"
    return Path.home() / ".local" / "share" / "ProxyBridge"


def resolve_app_script(explicit: str | None = None) -> Path:
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"backend script not found: {path}")
        return path
    for candidate in (
        resource_root() / "app.py",
        install_root() / "app.py",
        Path(__file__).resolve().parent.parent / "app.py",
    ):
        if candidate.is_file():
            return candidate.resolve()
    raise FileNotFoundError("backend script not found: app.py")


def resolve_python_executable(explicit: str | None = None) -> str:
    if explicit:
        path = Path(explicit).expanduser()
        if not path.exists():
            raise FileNotFoundError(f"python executable not found: {path}")
        return str(path)
    return sys.executable


def backend_command(python_executable: str, app_script: Path) -> list[str]:
    """Build the command used to launch the backend process."""
    if is_frozen():
        # Packaged Windows/Linux binary re-enters itself in backend-only mode.
        return [sys.executable, BACKEND_ONLY_FLAG]
    return [python_executable, str(app_script)]


def single_instance_lock_path(work_root: Path) -> Path:
    return work_root / "tray-host.lock"


def acquire_single_instance_lock(work_root: Path) -> Any:
    """Best-effort single-instance lock. Returns an open lock handle."""
    ensure_dirs(work_root)
    lock_path = single_instance_lock_path(work_root)
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            handle.write("0")
            handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        handle.seek(0)
        handle.truncate()
        handle.write(f"pid={os.getpid()}\n")
        handle.flush()
        return handle
    except OSError as error:
        handle.close()
        raise RuntimeError(f"ProxyBridge tray is already running (lock: {lock_path})") from error


def release_single_instance_lock(handle: Any) -> None:
    if handle is None:
        return
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        handle.close()
    except Exception:
        pass


def is_port_free(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def choose_backend_port(preferred: int = DEFAULT_BACKEND_PORT) -> int:
    if preferred > 0 and is_port_free(preferred):
        return preferred
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def console_url(port: int, host: str = "127.0.0.1") -> str:
    return f"http://{host}:{port}"


def console_access_url(base_url: str, token: str) -> str:
    return f"{base_url.rstrip('/')}/#{urllib.parse.urlencode({'token': token})}"


def ensure_dirs(work_root: Path) -> None:
    work_root.mkdir(parents=True, exist_ok=True)
    try:
        work_root.chmod(0o700)
    except OSError:
        pass


def log_backup_path(path: Path, index: int) -> Path:
    return path.with_name(f"{path.name}.{index}")


def rotate_log_for_append(
    path: Path,
    incoming_bytes: int,
    *,
    max_bytes: int = TRAY_LOG_MAX_BYTES,
    backup_count: int = TRAY_LOG_BACKUP_COUNT,
) -> bool:
    if max_bytes <= 0 or backup_count <= 0:
        return False
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return False
    if size + max(0, incoming_bytes) <= max_bytes:
        return False
    log_backup_path(path, backup_count).unlink(missing_ok=True)
    for index in range(backup_count - 1, 0, -1):
        source = log_backup_path(path, index)
        if source.exists():
            source.replace(log_backup_path(path, index + 1))
    backup = log_backup_path(path, 1)
    path.replace(backup)
    if size > max_bytes:
        with backup.open("rb") as handle:
            handle.seek(size - max_bytes, os.SEEK_SET)
            tail = handle.read(max_bytes)
        temp_path = backup.with_suffix(backup.suffix + ".tmp")
        with temp_path.open("wb") as handle:
            handle.write(tail)
        temp_path.replace(backup)
    try:
        backup.chmod(0o600)
    except OSError:
        pass
    return True


def append_bounded_log(
    path: Path,
    data: bytes,
    *,
    max_bytes: int = TRAY_LOG_MAX_BYTES,
    backup_count: int = TRAY_LOG_BACKUP_COUNT,
) -> None:
    if len(data) > max_bytes:
        data = data[-max_bytes:]
    with TRAY_LOG_LOCK:
        rotate_log_for_append(
            path,
            len(data),
            max_bytes=max_bytes,
            backup_count=backup_count,
        )
        with path.open("ab") as handle:
            if hasattr(os, "fchmod"):
                try:
                    os.fchmod(handle.fileno(), 0o600)
                except OSError:
                    pass
            handle.write(data)
            handle.flush()


def pump_bounded_log(
    stream: Any,
    path: Path,
    *,
    max_bytes: int = TRAY_LOG_MAX_BYTES,
    backup_count: int = TRAY_LOG_BACKUP_COUNT,
) -> None:
    chunk_size = max(1, min(65536, max_bytes))
    try:
        while True:
            chunk = stream.read(chunk_size)
            if not chunk:
                break
            if isinstance(chunk, str):
                chunk = chunk.encode("utf-8", errors="replace")
            append_bounded_log(
                path,
                chunk,
                max_bytes=max_bytes,
                backup_count=backup_count,
            )
    except Exception as error:
        _write_tray_host_log(f"backend log pump failed: {error}")
    finally:
        try:
            stream.close()
        except Exception:
            pass


@dataclass
class BackendConfig:
    python_executable: str
    app_script: Path
    work_root: Path
    port: int
    host: str = "127.0.0.1"
    open_console_on_ready: bool = True


class BackendProcess:
    def __init__(self, config: BackendConfig) -> None:
        self.config = config
        self.process: subprocess.Popen[Any] | None = None
        self.instance_token = secrets.token_urlsafe(18)
        self.log_path = config.work_root / "tray-backend.log"
        self._log_stream: Any | None = None
        self._log_thread: threading.Thread | None = None
        self._lock = threading.RLock()

    @property
    def is_running(self) -> bool:
        proc = self.process
        return proc is not None and proc.poll() is None

    @property
    def base_url(self) -> str:
        return console_url(self.config.port, self.config.host)

    @property
    def access_url(self) -> str:
        return console_access_url(self.base_url, self.instance_token)

    def start(self) -> None:
        with self._lock:
            if self.is_running:
                return
            ensure_dirs(self.config.work_root)
            env = os.environ.copy()
            env["PROXY_BRIDGE_PORT"] = str(self.config.port)
            env["PROXY_BRIDGE_HOST"] = self.config.host
            env["PROXY_BRIDGE_WORKDIR"] = str(self.config.work_root)
            env["PROXY_BRIDGE_PARENT_PID"] = str(os.getpid())
            env["PROXY_BRIDGE_INSTANCE_TOKEN"] = self.instance_token
            env["PROXY_BRIDGE_SHELL_MODE"] = "tray"
            # Avoid inheriting a broken PYTHONPATH from the tray launcher.
            env.pop("PYTHONPATH", None)

            popen_kwargs: dict[str, Any] = {}
            if os.name == "nt":
                creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                if creationflags:
                    popen_kwargs["creationflags"] = creationflags
            else:
                popen_kwargs["start_new_session"] = True

            command = backend_command(self.config.python_executable, self.config.app_script)
            cwd = str(install_root() if is_frozen() else self.config.app_script.parent)
            self.process = subprocess.Popen(
                command,
                cwd=cwd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=0,
                **popen_kwargs,
            )
            self._log_stream = getattr(self.process, "stdout", None)
            if self._log_stream is not None:
                self._log_thread = threading.Thread(
                    target=pump_bounded_log,
                    args=(self._log_stream, self.log_path),
                    name="tray-backend-log",
                    daemon=True,
                )
                try:
                    self._log_thread.start()
                except BaseException:
                    self.stop()
                    raise

    def wait_until_ready(self, timeout: float = READINESS_TIMEOUT) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        last_error = "backend not ready"
        while time.monotonic() < deadline:
            if not self.is_running:
                raise RuntimeError(f"backend exited before ready (see {self.log_path})")
            try:
                return self.get_status(timeout=0.75)
            except Exception as error:  # noqa: BLE001 - readiness probe aggregates errors
                last_error = str(error)
                time.sleep(READINESS_POLL_INTERVAL)
        raise TimeoutError(f"backend readiness timeout: {last_error}")

    def get_status(self, timeout: float = 2.0) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{self.base_url}/api/status",
            headers={
                "User-Agent": "ProxyBridgeTray/0.1",
                "Accept": "application/json",
                BACKEND_INSTANCE_HEADER: self.instance_token,
            },
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            echoed = str(getattr(response, "headers", {}).get(BACKEND_INSTANCE_HEADER) or "")
            if echoed != self.instance_token:
                raise RuntimeError("backend instance token mismatch")
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
        if not isinstance(payload, dict):
            raise RuntimeError("invalid status payload")
        return payload

    def stop_all_bridges(self, timeout: float = 5.0) -> dict[str, Any]:
        data = json.dumps({}).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/api/bridges/stop-all",
            data=data,
            method="POST",
            headers={
                "User-Agent": "ProxyBridgeTray/0.1",
                "Content-Type": "application/json",
                "Accept": "application/json",
                BACKEND_INSTANCE_HEADER: self.instance_token,
            },
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            echoed = str(getattr(response, "headers", {}).get(BACKEND_INSTANCE_HEADER) or "")
            if echoed != self.instance_token:
                raise RuntimeError("backend instance token mismatch")
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
        return payload if isinstance(payload, dict) else {"ok": True}

    def stop(self, timeout: float = TERMINATE_TIMEOUT) -> None:
        with self._lock:
            proc = self.process
            if proc is None:
                self._close_log()
                return
            if proc.poll() is None:
                try:
                    if os.name == "nt":
                        proc.terminate()
                    else:
                        os.killpg(proc.pid, signal.SIGTERM)
                except Exception:
                    try:
                        proc.terminate()
                    except Exception:
                        pass
                try:
                    proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    try:
                        if os.name == "nt":
                            proc.kill()
                        else:
                            os.killpg(proc.pid, signal.SIGKILL)
                    except Exception:
                        try:
                            proc.kill()
                        except Exception:
                            pass
                    try:
                        proc.wait(timeout=3)
                    except Exception:
                        pass
            self.process = None
            self._close_log()

    def restart(self) -> dict[str, Any]:
        self.stop()
        # Re-pick port only if previous preferred port is busy for another owner.
        if not is_port_free(self.config.port, self.config.host):
            self.config.port = choose_backend_port(self.config.port)
        self.instance_token = secrets.token_urlsafe(18)
        self.start()
        return self.wait_until_ready()

    def _close_log(self) -> None:
        thread = self._log_thread
        stream = self._log_stream
        self._log_thread = None
        self._log_stream = None
        if thread is not None:
            if thread.is_alive():
                thread.join(timeout=3)
        if thread is not None and thread.is_alive() and stream is not None:
            try:
                stream.close()
            except Exception:
                pass
            thread.join(timeout=1)
        elif stream is not None:
            try:
                stream.close()
            except Exception:
                pass


def load_tray_icon(icon_path: Path | None = None):
    try:
        from PIL import Image, ImageDraw
    except ImportError as error:  # pragma: no cover - dependency hint
        raise RuntimeError(
            "缺少 Pillow。请先安装: pip install -r desktop/requirements.txt"
        ) from error

    path = icon_path or (resource_root() / "static" / "assets" / "app-icon.png")
    if path.is_file():
        image = Image.open(path).convert("RGBA")
        # Tray icons are small; keep a sharp square crop/resize.
        image.thumbnail((64, 64))
        canvas = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        offset = ((64 - image.width) // 2, (64 - image.height) // 2)
        canvas.paste(image, offset, image)
        return canvas

    # Fallback geometric icon when asset is missing.
    image = Image.new("RGBA", (64, 64), (20, 24, 33, 255))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((8, 8, 56, 56), radius=12, fill=(88, 166, 255, 255))
    draw.rectangle((20, 28, 44, 36), fill=(20, 24, 33, 255))
    return image


class TrayApp:
    def __init__(self, config: BackendConfig) -> None:
        self.config = config
        self.backend = BackendProcess(config)
        self._icon = None
        self._status_text = "starting"
        self._bridge_count = 0
        self._error: str | None = None
        self._stop_event = threading.Event()
        self._worker: threading.Thread | None = None
        self._autostart_enabled = autostart.is_autostart_enabled()
        self._instance_lock = None

    def run(self) -> int:
        try:
            import pystray
            from pystray import Menu, MenuItem
        except ImportError as error:
            raise SystemExit(
                "缺少 pystray。请先安装托盘依赖:\n"
                "  pip install -r desktop/requirements.txt"
            ) from error

        ensure_dirs(self.config.work_root)
        self._set_status("starting backend")
        try:
            self.backend.start()
            self.backend.wait_until_ready()
            self._set_status("ready")
            if self.config.open_console_on_ready:
                self.open_console()
        except Exception as error:  # noqa: BLE001
            self._error = str(error)
            self._set_status("backend failed")
            print(f"ProxyBridge tray backend failed: {error}", file=sys.stderr)

        icon = pystray.Icon(
            name="ProxyBridge",
            title=self._title(),
            icon=load_tray_icon(),
            menu=Menu(
                MenuItem(lambda item: self._title(), None, enabled=False),
                Menu.SEPARATOR,
                MenuItem("打开控制台", lambda icon, item: self.open_console()),
                MenuItem("重启后端", lambda icon, item: self.restart_backend()),
                MenuItem("停止全部代理", lambda icon, item: self.stop_all_bridges()),
                MenuItem(
                    "开机自启",
                    lambda icon, item: self.toggle_autostart(),
                    checked=lambda item: self._autostart_enabled,
                ),
                Menu.SEPARATOR,
                MenuItem("退出", lambda icon, item: self.quit(icon)),
            ),
        )
        self._icon = icon
        self._worker = threading.Thread(target=self._status_loop, name="tray-status", daemon=True)
        self._worker.start()
        print(f"ProxyBridge tray ready: {self.backend.base_url}", flush=True)
        print(f"Work dir: {self.config.work_root}", flush=True)
        print(f"Backend log: {self.backend.log_path}", flush=True)
        icon.run()
        return 0

    def _title(self) -> str:
        if self._error:
            return f"PB · error · {self._error[:48]}"
        if self._status_text == "ready":
            return f"PB · ready · {self._bridge_count} bridges"
        return f"PB · {self._status_text}"

    def _set_status(self, text: str) -> None:
        self._status_text = text
        self._refresh_icon_title()

    def _refresh_icon_title(self) -> None:
        icon = self._icon
        if icon is not None:
            icon.title = self._title()

    def open_console(self) -> None:
        webbrowser.open(self.backend.access_url)

    def restart_backend(self) -> None:
        self._error = None
        self._set_status("restarting")
        try:
            self.backend.restart()
            self._set_status("ready")
            self.open_console()
        except Exception as error:  # noqa: BLE001
            self._error = str(error)
            self._set_status("backend failed")

    def stop_all_bridges(self) -> None:
        try:
            self.backend.stop_all_bridges()
            self._bridge_count = 0
            self._refresh_icon_title()
        except Exception as error:  # noqa: BLE001
            self._error = str(error)
            self._refresh_icon_title()

    def toggle_autostart(self) -> None:
        try:
            desired = not self._autostart_enabled
            result = autostart.set_autostart_enabled(desired)
            self._autostart_enabled = bool(result.get("enabled"))
            self._error = None
            _write_tray_host_log(
                f"autostart enabled={self._autostart_enabled} path={result.get('path')}"
            )
        except Exception as error:  # noqa: BLE001
            self._error = f"autostart failed: {error}"
            _write_tray_host_log(self._error)
        self._refresh_icon_title()
        icon = self._icon
        if icon is not None:
            try:
                icon.update_menu()
            except Exception:
                pass

    def quit(self, icon=None) -> None:
        self._stop_event.set()
        try:
            self.backend.stop()
        finally:
            release_single_instance_lock(self._instance_lock)
            self._instance_lock = None
            target = icon or self._icon
            if target is not None:
                target.stop()

    def _status_loop(self) -> None:
        while not self._stop_event.wait(STATUS_REFRESH_INTERVAL):
            if not self.backend.is_running:
                if self._status_text != "backend failed":
                    self._error = "backend process exited"
                    self._set_status("backend failed")
                continue
            try:
                payload = self.backend.get_status(timeout=2.0)
                bridges = payload.get("bridges") if isinstance(payload, dict) else None
                running = 0
                if isinstance(bridges, list):
                    running = sum(
                        1
                        for bridge in bridges
                        if isinstance(bridge, dict) and bridge.get("status") in {"running", "starting"}
                    )
                self._bridge_count = running
                self._error = None
                self._set_status("ready")
            except Exception:
                # Keep previous title; transient network blips are fine.
                pass


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="proxybridge-tray",
        description="ProxyBridge system-tray host for Linux / Windows",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_BACKEND_PORT, help="backend/console port")
    parser.add_argument("--host", default="127.0.0.1", help="backend bind host")
    parser.add_argument("--workdir", default=None, help="data directory")
    parser.add_argument("--python", default=None, help="python executable used to launch app.py")
    parser.add_argument("--app", default=None, help="path to app.py")
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="do not open the web console automatically on ready",
    )
    parser.add_argument(
        "--enable-autostart",
        action="store_true",
        help="enable login autostart and exit",
    )
    parser.add_argument(
        "--disable-autostart",
        action="store_true",
        help="disable login autostart and exit",
    )
    parser.add_argument(
        "--autostart-status",
        action="store_true",
        help="print whether login autostart is enabled and exit",
    )
    parser.add_argument(
        "--allow-multiple",
        action="store_true",
        help="allow multiple tray host instances",
    )
    return parser.parse_args(argv)


def build_config(args: argparse.Namespace) -> BackendConfig:
    work_root = Path(args.workdir).expanduser() if args.workdir else default_work_root()
    port = choose_backend_port(args.port)
    return BackendConfig(
        python_executable=resolve_python_executable(args.python),
        app_script=resolve_app_script(args.app),
        work_root=work_root.resolve(),
        port=port,
        host=str(args.host or "127.0.0.1"),
        open_console_on_ready=not bool(args.no_open),
    )


def run_backend_only(argv: list[str] | None = None) -> int:
    """Entry used by packaged executables to host app.py in a child process."""
    # Keep only flags that app.main understands; strip our private flag.
    cleaned = [item for item in (argv if argv is not None else sys.argv[1:]) if item != BACKEND_ONLY_FLAG]
    # Ensure bundled resources resolve when frozen.
    if is_frozen():
        os.environ.setdefault("PROXY_BRIDGE_SHELL_MODE", "tray-backend")
        # Point workdir remains caller-controlled via env.
    import app as backend_app

    backend_app.main(cleaned)
    return 0


def _write_tray_host_log(message: str) -> None:
    try:
        work = Path(os.environ.get("PROXY_BRIDGE_WORKDIR") or default_work_root())
        ensure_dirs(work)
        log_path = work / "tray-host.log"
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n".encode("utf-8", errors="replace")
        append_bounded_log(log_path, line)
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if BACKEND_ONLY_FLAG in raw_argv:
        return run_backend_only(raw_argv)

    args = parse_args(raw_argv)

    if args.enable_autostart and args.disable_autostart:
        print("--enable-autostart and --disable-autostart are mutually exclusive", file=sys.stderr)
        return 2
    if args.autostart_status:
        print("enabled" if autostart.is_autostart_enabled() else "disabled")
        return 0
    if args.enable_autostart or args.disable_autostart:
        result = autostart.set_autostart_enabled(bool(args.enable_autostart))
        state = "enabled" if result.get("enabled") else "disabled"
        print(f"autostart {state}: {result.get('path')}")
        return 0

    # On Linux, pystray may need an AppIndicator/dbus session; fail with a clear message.
    if host_os_token() == "linux" and not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        print("未检测到图形会话（DISPLAY/WAYLAND_DISPLAY）。托盘壳需要桌面环境。", file=sys.stderr)
        return 2
    config = build_config(args)
    os.environ["PROXY_BRIDGE_WORKDIR"] = str(config.work_root)
    _write_tray_host_log(f"starting tray host frozen={is_frozen()} port={config.port} workdir={config.work_root}")
    app = TrayApp(config)
    if not args.allow_multiple:
        try:
            app._instance_lock = acquire_single_instance_lock(config.work_root)
        except RuntimeError as error:
            _write_tray_host_log(str(error))
            print(str(error), file=sys.stderr)
            return 1

    def _handle_signal(signum: int, _frame: Any) -> None:
        del signum
        app.quit()

    if hasattr(signal, "SIGINT"):
        signal.signal(signal.SIGINT, _handle_signal)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _handle_signal)
    try:
        return app.run()
    except Exception as error:  # noqa: BLE001
        _write_tray_host_log(f"tray host failed: {error}")
        release_single_instance_lock(app._instance_lock)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
