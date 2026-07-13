#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from desktop import autostart, tray_host


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", 0))
        except PermissionError as exc:
            raise unittest.SkipTest("当前沙箱不允许绑定本地测试端口") from exc
        return int(sock.getsockname()[1])


class TrayHostHelperTests(unittest.TestCase):
    def test_default_work_root_uses_platform_locations(self) -> None:
        old = os.environ.pop("PROXY_BRIDGE_WORKDIR", None)
        try:
            with mock.patch.object(tray_host, "host_os_token", return_value="windows"):
                with mock.patch.dict(os.environ, {"APPDATA": r"C:\\Users\\demo\\AppData\\Roaming"}, clear=False):
                    self.assertEqual(
                        tray_host.default_work_root(),
                        Path(r"C:\\Users\\demo\\AppData\\Roaming") / "ProxyBridge",
                    )
            with mock.patch.object(tray_host, "host_os_token", return_value="linux"):
                with mock.patch.dict(os.environ, {"XDG_DATA_HOME": "/var/data"}, clear=False):
                    os.environ.pop("PROXY_BRIDGE_WORKDIR", None)
                    self.assertEqual(tray_host.default_work_root(), Path("/var/data/ProxyBridge"))
            with mock.patch.object(tray_host, "host_os_token", return_value="darwin"):
                root = tray_host.default_work_root()
                self.assertTrue(str(root).endswith("Library/Application Support/ProxyBridge"))
        finally:
            if old is None:
                os.environ.pop("PROXY_BRIDGE_WORKDIR", None)
            else:
                os.environ["PROXY_BRIDGE_WORKDIR"] = old

    def test_choose_backend_port_prefers_free_preferred_port(self) -> None:
        free = free_port()
        self.assertEqual(tray_host.choose_backend_port(free), free)

    def test_resolve_app_script_finds_repo_app(self) -> None:
        path = tray_host.resolve_app_script()
        self.assertTrue(path.is_file())
        self.assertEqual(path.name, "app.py")

    def test_backend_wait_until_ready_and_stop_all(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = tray_host.BackendConfig(
                python_executable="python3",
                app_script=Path("app.py"),
                work_root=Path(temp_dir),
                port=18787,
                host="127.0.0.1",
            )
            backend = tray_host.BackendProcess(config)
            backend.process = mock.Mock(poll=mock.Mock(return_value=None))

            status_payload = json.dumps({"ok": True, "bridges": [{"status": "running"}]}).encode("utf-8")
            stop_payload = json.dumps({"ok": True, "stopped": 1}).encode("utf-8")

            class FakeResponse:
                def __init__(self, body: bytes) -> None:
                    self._body = body

                def read(self) -> bytes:
                    return self._body

                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc, tb) -> None:
                    return None

            def fake_urlopen(request, timeout=0):  # noqa: ANN001
                del timeout
                url = str(getattr(request, "full_url", request))
                if url.endswith("/api/status"):
                    return FakeResponse(status_payload)
                if url.endswith("/api/bridges/stop-all"):
                    return FakeResponse(stop_payload)
                raise AssertionError(f"unexpected url: {url}")

            with mock.patch.object(tray_host.urllib.request, "urlopen", side_effect=fake_urlopen):
                status = backend.wait_until_ready(timeout=2.0)
                self.assertTrue(status.get("ok"))
                result = backend.stop_all_bridges()
                self.assertTrue(result.get("ok"))

    def test_cli_parser_defaults(self) -> None:
        args = tray_host.parse_args(["--port", "19001", "--no-open", "--workdir", "/tmp/pb"])
        self.assertEqual(args.port, 19001)
        self.assertTrue(args.no_open)
        self.assertEqual(args.workdir, "/tmp/pb")

    def test_backend_command_uses_reentry_when_frozen(self) -> None:
        with mock.patch.object(tray_host, "is_frozen", return_value=True):
            with mock.patch.object(tray_host.sys, "executable", "C:/ProxyBridge/ProxyBridge.exe"):
                command = tray_host.backend_command("python", Path("app.py"))
        self.assertEqual(command, ["C:/ProxyBridge/ProxyBridge.exe", tray_host.BACKEND_ONLY_FLAG])

    def test_backend_command_uses_python_script_when_not_frozen(self) -> None:
        with mock.patch.object(tray_host, "is_frozen", return_value=False):
            command = tray_host.backend_command("/usr/bin/python3", Path("/repo/app.py"))
        self.assertEqual(command, ["/usr/bin/python3", "/repo/app.py"])

    def test_resource_root_prefers_meipass_when_frozen(self) -> None:
        with mock.patch.object(tray_host, "is_frozen", return_value=True):
            with mock.patch.object(tray_host.sys, "_MEIPASS", "/tmp/meipass", create=True):
                self.assertEqual(tray_host.resource_root(), Path("/tmp/meipass"))


if __name__ == "__main__":
    unittest.main()


class AutostartTests(unittest.TestCase):
    def test_linux_autostart_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_home = Path(temp_dir) / "config"
            with mock.patch.object(autostart, "host_os_token", return_value="linux"):
                with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(config_home)}, clear=False):
                    with mock.patch.object(
                        autostart,
                        "current_launch_command",
                        return_value=["/usr/bin/proxybridge", "--no-open"],
                    ):
                        self.assertFalse(autostart.is_autostart_enabled())
                        result = autostart.set_autostart_enabled(True)
                        self.assertTrue(result["enabled"])
                        desktop = Path(result["path"])
                        self.assertTrue(desktop.is_file())
                        content = desktop.read_text(encoding="utf-8")
                        self.assertIn("Exec=/usr/bin/proxybridge --no-open", content)
                        self.assertTrue(autostart.is_autostart_enabled())
                        disabled = autostart.set_autostart_enabled(False)
                        self.assertFalse(disabled["enabled"])
                        self.assertFalse(desktop.exists())

    def test_single_instance_lock_blocks_second_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first = tray_host.acquire_single_instance_lock(root)
            try:
                with self.assertRaises(RuntimeError):
                    tray_host.acquire_single_instance_lock(root)
            finally:
                tray_host.release_single_instance_lock(first)
