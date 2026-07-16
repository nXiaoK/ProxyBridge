#!/usr/bin/env python3
from __future__ import annotations

import io
import json
import os
import plistlib
import socket
import subprocess
import tempfile
import threading
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
    def test_backend_access_url_uses_instance_token_fragment(self) -> None:
        config = tray_host.BackendConfig(
            python_executable="python3",
            app_script=Path("/tmp/ProxyBridge/app.py"),
            work_root=Path("/tmp/ProxyBridge/work"),
            port=18787,
        )
        backend = tray_host.BackendProcess(config)
        backend.instance_token = "01234567-89AB-CDEF-0123-456789ABCDEF"

        self.assertEqual(
            backend.access_url,
            "http://127.0.0.1:18787/#token=01234567-89AB-CDEF-0123-456789ABCDEF",
        )

    def test_bounded_log_pump_rotates_during_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "tray.log"
            tray_host.pump_bounded_log(io.BytesIO(b"abcdefghijk"), log_path, max_bytes=5, backup_count=2)

            self.assertEqual(log_path.read_bytes(), b"k")
            self.assertEqual(tray_host.log_backup_path(log_path, 1).read_bytes(), b"fghij")
            self.assertEqual(tray_host.log_backup_path(log_path, 2).read_bytes(), b"abcde")

    def test_bounded_log_caps_legacy_backup_and_serializes_writers(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "tray.log"
            log_path.write_bytes(b"x" * 100)
            barrier = threading.Barrier(5)
            failures: list[BaseException] = []

            def writer(marker: bytes) -> None:
                try:
                    barrier.wait()
                    tray_host.append_bounded_log(log_path, marker * 3, max_bytes=5, backup_count=2)
                except BaseException as error:
                    failures.append(error)

            threads = [threading.Thread(target=writer, args=(bytes([65 + index]),)) for index in range(4)]
            for thread in threads:
                thread.start()
            barrier.wait()
            for thread in threads:
                thread.join(timeout=2)

            self.assertEqual(failures, [])
            paths = [log_path, tray_host.log_backup_path(log_path, 1), tray_host.log_backup_path(log_path, 2)]
            self.assertTrue(all(path.stat().st_size <= 5 for path in paths if path.exists()))

    def test_bounded_log_pump_reports_stream_failure_and_closes_stream(self) -> None:
        class FailingStream:
            def __init__(self) -> None:
                self.closed = False

            def read(self, _size: int) -> bytes:
                raise OSError("backend pipe failed")

            def close(self) -> None:
                self.closed = True

        stream = FailingStream()
        with tempfile.TemporaryDirectory() as temp_dir:
            with mock.patch.object(tray_host, "_write_tray_host_log") as logger:
                tray_host.pump_bounded_log(stream, Path(temp_dir) / "tray.log")

        self.assertTrue(stream.closed)
        logger.assert_called_once_with("backend log pump failed: backend pipe failed")

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
                def __init__(self, body: bytes, token: str) -> None:
                    self._body = body
                    self.headers = {tray_host.BACKEND_INSTANCE_HEADER: token}

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
                    return FakeResponse(status_payload, backend.instance_token)
                if url.endswith("/api/bridges/stop-all"):
                    return FakeResponse(stop_payload, backend.instance_token)
                raise AssertionError(f"unexpected url: {url}")

            with mock.patch.object(tray_host.urllib.request, "urlopen", side_effect=fake_urlopen):
                status = backend.wait_until_ready(timeout=2.0)
                self.assertTrue(status.get("ok"))
                result = backend.stop_all_bridges()
                self.assertTrue(result.get("ok"))

    def test_backend_start_rolls_back_when_log_thread_cannot_start(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = tray_host.BackendConfig(
                python_executable="python3",
                app_script=Path(temp_dir) / "app.py",
                work_root=Path(temp_dir),
                port=18787,
            )
            backend = tray_host.BackendProcess(config)
            stream = mock.Mock()
            process = mock.Mock()
            process.pid = 7654
            process.stdout = stream
            process.poll.return_value = None
            process.wait.return_value = 0

            with (
                mock.patch.object(tray_host.subprocess, "Popen", return_value=process),
                mock.patch.object(threading.Thread, "start", side_effect=RuntimeError("thread start failed")),
            ):
                with self.assertRaisesRegex(RuntimeError, "thread start failed"):
                    backend.start()

            self.assertIsNone(backend.process)
            self.assertIsNone(backend._log_stream)
            self.assertIsNone(backend._log_thread)
            self.assertTrue(stream.close.called)
            self.assertTrue(process.wait.called)

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
    def test_source_launch_command_is_independent_of_current_directory(self) -> None:
        with (
            mock.patch.object(autostart, "is_frozen", return_value=False),
            mock.patch.object(autostart, "resolve_python_executable", return_value="/usr/bin/python3"),
        ):
            command = autostart.current_launch_command()

        self.assertEqual(command[0], "/usr/bin/python3")
        self.assertEqual(Path(command[1]), Path(autostart.__file__).resolve().with_name("tray_host.py"))
        self.assertTrue(Path(command[1]).is_absolute())
        self.assertEqual(command[2:], ["--no-open"])

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
                        result = autostart.set_autostart_enabled(
                            True,
                            working_directory=Path(temp_dir) / "source root",
                        )
                        self.assertTrue(result["enabled"])
                        desktop = Path(result["path"])
                        self.assertTrue(desktop.is_file())
                        content = desktop.read_text(encoding="utf-8")
                        self.assertIn("Exec=/usr/bin/proxybridge --no-open", content)
                        self.assertIn(f"Path={Path(temp_dir).resolve() / 'source root'}", content)
                        self.assertTrue(autostart.is_autostart_enabled())
                        disabled = autostart.set_autostart_enabled(False)
                        self.assertFalse(disabled["enabled"])
                        self.assertFalse(desktop.exists())

    def test_linux_autostart_escapes_special_exec_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_home = Path(temp_dir) / "config"
            command = ["/tmp/Proxy & Bridge/python", "/tmp/100%/tray\"host.py", "$HOME"]
            with (
                mock.patch.object(autostart, "host_os_token", return_value="linux"),
                mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(config_home)}, clear=False),
            ):
                result = autostart.set_autostart_enabled(
                    True,
                    command,
                    working_directory=Path(temp_dir) / "repo & source",
                )

            content = Path(result["path"]).read_text(encoding="utf-8")
            self.assertIn('Exec="/tmp/Proxy & Bridge/python" "/tmp/100%%/tray\\"host.py" "\\$HOME"', content)
            self.assertIn(f"Path={Path(temp_dir).resolve() / 'repo & source'}", content)

    def test_darwin_autostart_serializes_special_paths_with_plistlib(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            plist_path = Path(temp_dir) / "LaunchAgents" / "proxy&bridge.plist"
            command = ["/tmp/Proxy & Bridge/python", "/tmp/tray<host>.py", "--no-open"]
            workdir = Path(temp_dir) / "repo & source"
            with (
                mock.patch.object(autostart, "host_os_token", return_value="darwin"),
                mock.patch.object(autostart, "darwin_launch_agent_path", return_value=plist_path),
            ):
                autostart.set_autostart_enabled(True, command, working_directory=workdir)

            with plist_path.open("rb") as handle:
                payload = plistlib.load(handle)
            self.assertEqual(payload["ProgramArguments"], command)
            self.assertEqual(payload["WorkingDirectory"], str(workdir.resolve()))
            self.assertTrue(payload["RunAtLoad"])

    def test_windows_shortcut_uses_source_working_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            shortcut = Path(temp_dir) / "ProxyBridge.lnk"
            completed = mock.Mock(returncode=0, stderr="", stdout="")
            with mock.patch.object(autostart.subprocess, "run", return_value=completed) as runner:
                autostart.write_windows_shortcut(
                    shortcut,
                    [r"C:\Python\python.exe", r"C:\Repo\desktop\tray_host.py"],
                    working_directory=Path(temp_dir) / "repo",
                )

            powershell = runner.call_args.args[0][-1]
            self.assertIn(f'$sc.WorkingDirectory = "{Path(temp_dir).resolve() / "repo"}"', powershell)

    def test_windows_argument_quoting_preserves_trailing_backslashes_and_quotes(self) -> None:
        parts = ["C:\\Program Files\\ProxyBridge\\", 'value with "quote"', ""]
        self.assertEqual(autostart.quote_windows_cmd(parts), subprocess.list2cmdline(parts))

    def test_single_instance_lock_blocks_second_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first = tray_host.acquire_single_instance_lock(root)
            try:
                with self.assertRaises(RuntimeError):
                    tray_host.acquire_single_instance_lock(root)
            finally:
                tray_host.release_single_instance_lock(first)
