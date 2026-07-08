import base64
import json
import os
import socket
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path

import app
from app import Socks5LogProxy, parse_link, parse_subscription_payload, read_exact, sing_box_config


def b64(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii").rstrip("=")


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", 0))
        except PermissionError as exc:
            raise unittest.SkipTest("当前沙箱不允许绑定本地测试端口") from exc
        return int(sock.getsockname()[1])


class ParserTests(unittest.TestCase):
    def test_core_paths_prefers_bundled_binaries(self):
        old_app_root = app.APP_ROOT
        old_core_dir = os.environ.pop("PROXY_BRIDGE_CORE_DIR", None)
        old_sing_box = os.environ.pop("PROXY_BRIDGE_SING_BOX", None)
        old_xray = os.environ.pop("PROXY_BRIDGE_XRAY", None)
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                bin_dir = root / "bin"
                bin_dir.mkdir()
                for name in ("sing-box", "xray"):
                    binary = bin_dir / name
                    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                    binary.chmod(0o755)
                app.APP_ROOT = root

                paths = app.core_paths()
                self.assertEqual(paths["sing-box"], str(bin_dir / "sing-box"))
                self.assertEqual(paths["xray"], str(bin_dir / "xray"))
        finally:
            app.APP_ROOT = old_app_root
            if old_core_dir is not None:
                os.environ["PROXY_BRIDGE_CORE_DIR"] = old_core_dir
            if old_sing_box is not None:
                os.environ["PROXY_BRIDGE_SING_BOX"] = old_sing_box
            if old_xray is not None:
                os.environ["PROXY_BRIDGE_XRAY"] = old_xray

    def test_core_paths_finds_runtime_downloaded_binaries(self):
        old_app_root = app.APP_ROOT
        old_work_root = app.WORK_ROOT
        old_core_dir = os.environ.pop("PROXY_BRIDGE_CORE_DIR", None)
        old_sing_box = os.environ.pop("PROXY_BRIDGE_SING_BOX", None)
        old_xray = os.environ.pop("PROXY_BRIDGE_XRAY", None)
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                app.APP_ROOT = root / "app"
                app.WORK_ROOT = root / "work"
                bin_dir = app.proxy_core_bin_dir()
                bin_dir.mkdir(parents=True)
                for name in ("sing-box", "xray"):
                    binary = bin_dir / name
                    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                    binary.chmod(0o755)

                paths = app.core_paths()
                self.assertEqual(paths["sing-box"], str(bin_dir / "sing-box"))
                self.assertEqual(paths["xray"], str(bin_dir / "xray"))
        finally:
            app.APP_ROOT = old_app_root
            app.WORK_ROOT = old_work_root
            if old_core_dir is not None:
                os.environ["PROXY_BRIDGE_CORE_DIR"] = old_core_dir
            if old_sing_box is not None:
                os.environ["PROXY_BRIDGE_SING_BOX"] = old_sing_box
            if old_xray is not None:
                os.environ["PROXY_BRIDGE_XRAY"] = old_xray

    def test_release_asset_pickers_select_macos_archives(self):
        release = {
            "assets": [
                {"name": "sing-box_1.0.0_linux_arm64.tar.gz", "browser_download_url": "https://example/linux"},
                {"name": "sing-box_1.0.0_darwin_arm64.tar.gz", "browser_download_url": "https://example/sing-arm"},
                {"name": "sing-box_1.0.0_darwin_amd64.tar.gz", "browser_download_url": "https://example/sing-amd"},
                {"name": "Xray-linux-64.zip", "browser_download_url": "https://example/xray-linux"},
                {"name": "Xray-macos-arm64-v8a.zip", "browser_download_url": "https://example/xray-arm"},
                {"name": "Xray-macos-64.zip", "browser_download_url": "https://example/xray-amd"},
            ]
        }

        self.assertEqual(app._pick_sing_box_asset(release, "arm64")["browser_download_url"], "https://example/sing-arm")
        self.assertEqual(app._pick_sing_box_asset(release, "x86_64")["browser_download_url"], "https://example/sing-amd")
        self.assertEqual(app._pick_xray_asset(release, "arm64")["browser_download_url"], "https://example/xray-arm")
        self.assertEqual(app._pick_xray_asset(release, "x86_64")["browser_download_url"], "https://example/xray-amd")

    def test_xray_runtime_install_only_requires_binary(self):
        old_work_root = app.WORK_ROOT
        old_download_asset = app._download_asset
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                app.WORK_ROOT = Path(temp_dir)
                app.ensure_dirs()

                def fake_download(_asset: dict, target: Path) -> None:
                    with zipfile.ZipFile(target, "w") as archive:
                        archive.writestr("xray", "#!/bin/sh\nexit 0\n")

                app._download_asset = fake_download
                installed = app._install_core_from_asset(
                    "xray",
                    {"name": "Xray-macos-arm64-v8a.zip", "browser_download_url": "https://example/xray.zip"},
                    "v-test",
                )

                self.assertEqual(installed, str(app.proxy_core_bin_dir() / "xray"))
                self.assertTrue((app.proxy_core_bin_dir() / "xray").is_file())
                self.assertTrue((app.proxy_core_bin_dir() / "xray-core").is_file())
                self.assertFalse((app.proxy_core_bin_dir() / "xray-assets").exists())
        finally:
            app.WORK_ROOT = old_work_root
            app._download_asset = old_download_asset

    def test_launch_browser_uses_node_profile_and_proxy(self):
        old_work_root = app.WORK_ROOT
        old_runtime_root = app.RUNTIME_ROOT
        old_state_path = app.STATE_PATH
        old_chrome = os.environ.get("PROXY_BRIDGE_CHROME")
        old_fp_kernel = os.environ.get("PROXY_BRIDGE_FP_CHROMIUM")
        old_popen = app.subprocess.Popen
        old_start_bridge = app.start_bridge

        class FakePopen:
            calls: list[tuple[list[str], dict[str, object]]] = []

            def __init__(self, command: list[str], **kwargs: object) -> None:
                self.pid = 4242
                self.command = command
                FakePopen.calls.append((command, kwargs))

        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                chrome = root / "Google Chrome"
                chrome.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                chrome.chmod(0o755)
                os.environ["PROXY_BRIDGE_CHROME"] = str(chrome)
                # 方案 B:浏览器改用指纹内核,测试用一个可执行桩文件模拟 fingerprint-chromium。
                fp_kernel = root / "Chromium"
                fp_kernel.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                fp_kernel.chmod(0o755)
                os.environ["PROXY_BRIDGE_FP_CHROMIUM"] = str(fp_kernel)
                app.WORK_ROOT = root / "work"
                app.RUNTIME_ROOT = app.WORK_ROOT / "runtime"
                app.STATE_PATH = app.WORK_ROOT / "state.json"
                app.ensure_dirs()
                node_id = "node-browser"
                app.STATE_PATH.write_text(
                    json.dumps(
                        {
                            "subscriptions": [],
                            "nodes": [
                                {
                                    "id": node_id,
                                    "name": "Browser Node",
                                    "protocol": "vless",
                                    "server": "example.com",
                                    "port": 443,
                                    "region": "HK",
                                    "source": "test",
                                    "raw": "",
                                    "preferred_core": "sing-box",
                                }
                            ],
                            "bridges": [],
                            "tests": {},
                            "request_logs": [],
                        }
                    ),
                    encoding="utf-8",
                )
                app.subprocess.Popen = FakePopen
                app.start_bridge = lambda payload: {
                    "id": "bridge-browser",
                    "node_id": node_id,
                    "node_name": "Browser Node",
                    "port": 25080,
                    "socks_url": "socks5://127.0.0.1:25080",
                    "status": "running",
                }

                result = app.launch_browser({"node_id": node_id})
                self.assertEqual(result["proxy"], "socks5://127.0.0.1:25080")
                self.assertEqual(result["pid"], 4242)
                self.assertEqual(len(FakePopen.calls), 1)
                command = FakePopen.calls[0][0]
                self.assertEqual(command[0], str(fp_kernel))
                self.assertIn("--proxy-server=socks5://127.0.0.1:25080", command)
                # 校验方案 B 的指纹伪造标志已下发。
                self.assertTrue(any(arg.startswith("--fingerprint=") for arg in command))
                self.assertIn("--fingerprint-platform=macos", command)
                self.assertIn("--webrtc-ip-handling-policy=disable_non_proxied_udp", command)
                self.assertTrue(any(arg.startswith("--timezone=") for arg in command))
                self.assertTrue(any(arg.startswith("--lang=") for arg in command))
                # 同一节点两次启动应派生完全相同的指纹种子(环境可恢复)。
                self.assertEqual(result["fingerprint_seed"], app.fingerprint_seed(node_id))
                profile_arg = next(arg for arg in command if arg.startswith("--user-data-dir="))
                profile_path = Path(profile_arg.split("=", 1)[1])
                self.assertTrue(profile_path.is_dir())
                self.assertEqual(profile_path.parent, app.browser_profiles_root())
                self.assertEqual(command[-1], "https://ip.skk.moe/")
                prefs_path = profile_path / "Default" / "Preferences"
                prefs = json.loads(prefs_path.read_text(encoding="utf-8"))
                provider = prefs["default_search_provider"]
                self.assertTrue(provider["enabled"])
                self.assertEqual(provider["name"], "Google")
                self.assertEqual(provider["search_url"], "https://www.google.com/search?q={searchTerms}")
                load_extension_args = [arg for arg in command if arg.startswith("--load-extension=")]
                self.assertEqual(len(load_extension_args), 1)
                extension_path = Path(load_extension_args[0].split("=", 1)[1])
                manifest = json.loads((extension_path / "manifest.json").read_text(encoding="utf-8"))
                search_provider = manifest["chrome_settings_overrides"]["search_provider"]
                self.assertEqual(search_provider["name"], "Google")
                self.assertEqual(search_provider["search_url"], "https://www.google.com/search?q={searchTerms}")
                self.assertTrue(search_provider["is_default"])
        finally:
            app.WORK_ROOT = old_work_root
            app.RUNTIME_ROOT = old_runtime_root
            app.STATE_PATH = old_state_path
            app.subprocess.Popen = old_popen
            app.start_bridge = old_start_bridge
            if old_chrome is None:
                os.environ.pop("PROXY_BRIDGE_CHROME", None)
            else:
                os.environ["PROXY_BRIDGE_CHROME"] = old_chrome
            if old_fp_kernel is None:
                os.environ.pop("PROXY_BRIDGE_FP_CHROMIUM", None)
            else:
                os.environ["PROXY_BRIDGE_FP_CHROMIUM"] = old_fp_kernel

    def test_stop_browser_keeps_active_socks_bridge_running(self):
        old_work_root = app.WORK_ROOT
        old_runtime_root = app.RUNTIME_ROOT
        old_state_path = app.STATE_PATH
        old_browser_table = {key: list(value) for key, value in app.BROWSER_TABLE.items()}
        old_process_table = dict(app.PROCESS_TABLE)
        old_log_proxy_table = dict(app.LOG_PROXY_TABLE)
        old_terminate_process = app.terminate_process
        old_wait_for_port = app.wait_for_port

        class FakeProcess:
            def __init__(self, pid: int) -> None:
                self.pid = pid

            def poll(self) -> None:
                return None

        class FakeProxy:
            def __init__(self) -> None:
                self.stopped = False

            def stop(self) -> None:
                self.stopped = True

            def is_alive(self) -> bool:
                return not self.stopped

        killed_pids: list[int] = []

        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                app.WORK_ROOT = Path(temp_dir)
                app.RUNTIME_ROOT = Path(temp_dir) / "runtime"
                app.STATE_PATH = Path(temp_dir) / "state.json"
                app.ensure_dirs()
                node_id = "node-browser-stop"
                bridge_id = "bridge-browser-stop"
                app.STATE_PATH.write_text(
                    json.dumps(
                        {
                            "subscriptions": [],
                            "nodes": [
                                {
                                    "id": node_id,
                                    "name": "Browser Stop Node",
                                    "protocol": "socks",
                                    "server": "127.0.0.1",
                                    "port": 1,
                                    "region": "本地",
                                    "source": "test",
                                    "raw": "",
                                    "settings": {},
                                    "tags": [],
                                    "transport": "tcp",
                                    "security": "",
                                    "preferred_core": "sing-box",
                                }
                            ],
                            "bridges": [
                                {
                                    "id": bridge_id,
                                    "node_id": node_id,
                                    "node_name": "Browser Stop Node",
                                    "core": "sing-box",
                                    "status": "running",
                                    "enabled": True,
                                    "port": 25180,
                                    "pid": 5151,
                                    "started_at": 10,
                                }
                            ],
                            "tests": {},
                            "request_logs": [],
                        }
                    ),
                    encoding="utf-8",
                )
                app.BROWSER_TABLE.clear()
                app.BROWSER_TABLE[node_id] = [FakeProcess(6161)]
                app.PROCESS_TABLE.clear()
                app.PROCESS_TABLE[bridge_id] = FakeProcess(5151)
                app.LOG_PROXY_TABLE.clear()
                app.LOG_PROXY_TABLE[bridge_id] = FakeProxy()
                app.terminate_process = lambda process: killed_pids.append(process.pid)
                app.wait_for_port = lambda port, timeout=2.0: True

                result = app.stop_browser({"node_id": node_id})
                state = app.load_state()
                bridge = state["bridges"][0]

                self.assertEqual(result["killed_browsers"], 1)
                self.assertEqual(result["stopped_bridges"], [])
                self.assertEqual(killed_pids, [6161])
                self.assertEqual(bridge["status"], "running")
                self.assertTrue(bridge["enabled"])
                self.assertEqual(bridge["pid"], 5151)
        finally:
            app.WORK_ROOT = old_work_root
            app.RUNTIME_ROOT = old_runtime_root
            app.STATE_PATH = old_state_path
            app.terminate_process = old_terminate_process
            app.wait_for_port = old_wait_for_port
            app.BROWSER_TABLE.clear()
            app.BROWSER_TABLE.update(old_browser_table)
            app.PROCESS_TABLE.clear()
            app.PROCESS_TABLE.update(old_process_table)
            app.LOG_PROXY_TABLE.clear()
            app.LOG_PROXY_TABLE.update(old_log_proxy_table)

    def test_stop_bridge_stops_browser_for_same_node(self):
        old_work_root = app.WORK_ROOT
        old_runtime_root = app.RUNTIME_ROOT
        old_state_path = app.STATE_PATH
        old_browser_table = {key: list(value) for key, value in app.BROWSER_TABLE.items()}
        old_process_table = dict(app.PROCESS_TABLE)
        old_log_proxy_table = dict(app.LOG_PROXY_TABLE)
        old_traffic_table = dict(app.TRAFFIC_TABLE)
        old_terminate_process = app.terminate_process

        class FakeProcess:
            def __init__(self, pid: int) -> None:
                self.pid = pid

            def poll(self) -> None:
                return None

        class FakeProxy:
            def __init__(self) -> None:
                self.stopped = False

            def stop(self) -> None:
                self.stopped = True

            def is_alive(self) -> bool:
                return not self.stopped

        killed_pids: list[int] = []

        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                app.WORK_ROOT = Path(temp_dir)
                app.RUNTIME_ROOT = Path(temp_dir) / "runtime"
                app.STATE_PATH = Path(temp_dir) / "state.json"
                app.ensure_dirs()
                node_id = "node-stop-bridge"
                bridge_id = "bridge-stop-bridge"
                app.STATE_PATH.write_text(
                    json.dumps(
                        {
                            "subscriptions": [],
                            "nodes": [
                                {
                                    "id": node_id,
                                    "name": "Stop Bridge Node",
                                    "protocol": "socks",
                                    "server": "127.0.0.1",
                                    "port": 1,
                                    "region": "本地",
                                    "source": "test",
                                    "raw": "",
                                    "settings": {},
                                    "tags": [],
                                    "transport": "tcp",
                                    "security": "",
                                    "preferred_core": "sing-box",
                                }
                            ],
                            "bridges": [
                                {
                                    "id": bridge_id,
                                    "node_id": node_id,
                                    "node_name": "Stop Bridge Node",
                                    "core": "sing-box",
                                    "status": "running",
                                    "enabled": True,
                                    "port": 25280,
                                    "pid": 5252,
                                    "started_at": 10,
                                }
                            ],
                            "tests": {},
                            "request_logs": [],
                        }
                    ),
                    encoding="utf-8",
                )
                app.BROWSER_TABLE.clear()
                app.BROWSER_TABLE[node_id] = [FakeProcess(6262)]
                app.PROCESS_TABLE.clear()
                app.PROCESS_TABLE[bridge_id] = FakeProcess(5252)
                app.LOG_PROXY_TABLE.clear()
                app.LOG_PROXY_TABLE[bridge_id] = FakeProxy()
                app.TRAFFIC_TABLE.clear()
                app.init_bridge_traffic(bridge_id)
                app.terminate_process = lambda process: killed_pids.append(process.pid)

                result = app.stop_bridge(bridge_id)
                state = app.load_state()
                bridge = state["bridges"][0]

                self.assertTrue(result["ok"])
                self.assertEqual(result["killed_browsers"], 1)
                self.assertEqual(killed_pids, [6262, 5252])
                self.assertNotIn(node_id, app.BROWSER_TABLE)
                self.assertEqual(bridge["status"], "stopped")
                self.assertFalse(bridge["enabled"])
                self.assertIsNone(bridge["pid"])
        finally:
            app.WORK_ROOT = old_work_root
            app.RUNTIME_ROOT = old_runtime_root
            app.STATE_PATH = old_state_path
            app.terminate_process = old_terminate_process
            app.BROWSER_TABLE.clear()
            app.BROWSER_TABLE.update(old_browser_table)
            app.PROCESS_TABLE.clear()
            app.PROCESS_TABLE.update(old_process_table)
            app.LOG_PROXY_TABLE.clear()
            app.LOG_PROXY_TABLE.update(old_log_proxy_table)
            app.TRAFFIC_TABLE.clear()
            app.TRAFFIC_TABLE.update(old_traffic_table)

    def test_stop_all_bridges_stops_all_associated_browsers(self):
        old_work_root = app.WORK_ROOT
        old_runtime_root = app.RUNTIME_ROOT
        old_state_path = app.STATE_PATH
        old_browser_table = {key: list(value) for key, value in app.BROWSER_TABLE.items()}
        old_process_table = dict(app.PROCESS_TABLE)
        old_log_proxy_table = dict(app.LOG_PROXY_TABLE)
        old_traffic_table = dict(app.TRAFFIC_TABLE)
        old_terminate_process = app.terminate_process

        class FakeProcess:
            def __init__(self, pid: int) -> None:
                self.pid = pid

            def poll(self) -> None:
                return None

        class FakeProxy:
            def __init__(self) -> None:
                self.stopped = False

            def stop(self) -> None:
                self.stopped = True

            def is_alive(self) -> bool:
                return not self.stopped

        killed_pids: list[int] = []

        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                app.WORK_ROOT = Path(temp_dir)
                app.RUNTIME_ROOT = Path(temp_dir) / "runtime"
                app.STATE_PATH = Path(temp_dir) / "state.json"
                app.ensure_dirs()
                node_a = "node-stop-all-a"
                node_b = "node-stop-all-b"
                bridge_a = "bridge-stop-all-a"
                bridge_b = "bridge-stop-all-b"
                app.STATE_PATH.write_text(
                    json.dumps(
                        {
                            "subscriptions": [],
                            "nodes": [],
                            "bridges": [
                                {
                                    "id": bridge_a,
                                    "node_id": node_a,
                                    "node_name": "Stop All A",
                                    "core": "sing-box",
                                    "status": "running",
                                    "enabled": True,
                                    "port": 25380,
                                    "pid": 5353,
                                    "started_at": 10,
                                },
                                {
                                    "id": bridge_b,
                                    "node_id": node_b,
                                    "node_name": "Stop All B",
                                    "core": "sing-box",
                                    "status": "running",
                                    "enabled": True,
                                    "port": 25381,
                                    "pid": 5354,
                                    "started_at": 11,
                                },
                            ],
                            "tests": {},
                            "request_logs": [],
                        }
                    ),
                    encoding="utf-8",
                )
                app.BROWSER_TABLE.clear()
                app.BROWSER_TABLE[node_a] = [FakeProcess(6363)]
                app.BROWSER_TABLE[node_b] = [FakeProcess(6364)]
                app.PROCESS_TABLE.clear()
                app.PROCESS_TABLE[bridge_a] = FakeProcess(5353)
                app.PROCESS_TABLE[bridge_b] = FakeProcess(5354)
                app.LOG_PROXY_TABLE.clear()
                app.LOG_PROXY_TABLE[bridge_a] = FakeProxy()
                app.LOG_PROXY_TABLE[bridge_b] = FakeProxy()
                app.TRAFFIC_TABLE.clear()
                app.init_bridge_traffic(bridge_a)
                app.init_bridge_traffic(bridge_b)
                app.terminate_process = lambda process: killed_pids.append(process.pid)

                result = app.stop_all_bridges()
                state = app.load_state()

                self.assertTrue(result["ok"])
                self.assertEqual(result["killed_browsers"], 2)
                self.assertCountEqual(killed_pids, [6363, 6364, 5353, 5354])
                self.assertEqual(app.BROWSER_TABLE, {})
                self.assertTrue(all(bridge["status"] == "stopped" for bridge in state["bridges"]))
                self.assertTrue(all(not bridge["enabled"] for bridge in state["bridges"]))
        finally:
            app.WORK_ROOT = old_work_root
            app.RUNTIME_ROOT = old_runtime_root
            app.STATE_PATH = old_state_path
            app.terminate_process = old_terminate_process
            app.BROWSER_TABLE.clear()
            app.BROWSER_TABLE.update(old_browser_table)
            app.PROCESS_TABLE.clear()
            app.PROCESS_TABLE.update(old_process_table)
            app.LOG_PROXY_TABLE.clear()
            app.LOG_PROXY_TABLE.update(old_log_proxy_table)
            app.TRAFFIC_TABLE.clear()
            app.TRAFFIC_TABLE.update(old_traffic_table)

    def test_load_state_keeps_running_bridges_enabled_for_restore(self):
        old_work_root = app.WORK_ROOT
        old_runtime_root = app.RUNTIME_ROOT
        old_state_path = app.STATE_PATH
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                app.WORK_ROOT = Path(temp_dir)
                app.RUNTIME_ROOT = Path(temp_dir) / "runtime"
                app.STATE_PATH = Path(temp_dir) / "state.json"
                app.ensure_dirs()
                app.STATE_PATH.write_text(
                    json.dumps(
                        {
                            "subscriptions": [],
                            "nodes": [],
                            "bridges": [
                                {
                                    "id": "bridge-1",
                                    "node_id": "node-1",
                                    "status": "running",
                                    "port": 21080,
                                    "pid": 123,
                                }
                            ],
                            "tests": {},
                            "request_logs": [],
                        }
                    ),
                    encoding="utf-8",
                )

                state = app.load_state()
                self.assertEqual(state["bridges"][0]["status"], "running")
                self.assertTrue(state["bridges"][0]["enabled"])
                self.assertEqual(state["bridges"][0]["socks_url"], "socks5://127.0.0.1:21080")
                self.assertEqual(state["bridges"][0]["http_url"], "http://127.0.0.1:21080")
        finally:
            app.WORK_ROOT = old_work_root
            app.RUNTIME_ROOT = old_runtime_root
            app.STATE_PATH = old_state_path

    def test_core_log_payload_filters_by_core(self):
        old_work_root = app.WORK_ROOT
        old_runtime_root = app.RUNTIME_ROOT
        old_state_path = app.STATE_PATH
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                app.WORK_ROOT = Path(temp_dir)
                app.RUNTIME_ROOT = Path(temp_dir) / "runtime"
                app.STATE_PATH = Path(temp_dir) / "state.json"
                app.ensure_dirs()
                app.STATE_PATH.write_text(
                    json.dumps(
                        {
                            "subscriptions": [],
                            "nodes": [],
                            "bridges": [
                                {
                                    "id": "bridge-sing",
                                    "node_id": "node-sing",
                                    "node_name": "Sing Node",
                                    "core": "sing-box",
                                    "status": "stopped",
                                    "port": 21080,
                                    "started_at": 10,
                                },
                                {
                                    "id": "bridge-empty",
                                    "node_id": "node-empty",
                                    "node_name": "Empty History",
                                    "core": "sing-box",
                                    "status": "stopped",
                                    "port": 21082,
                                    "started_at": 11,
                                },
                                {
                                    "id": "bridge-failed-empty",
                                    "node_id": "node-failed-empty",
                                    "node_name": "Failed Empty",
                                    "core": "sing-box",
                                    "status": "failed",
                                    "port": 21083,
                                    "started_at": 12,
                                },
                                {
                                    "id": "bridge-xray",
                                    "node_id": "node-xray",
                                    "node_name": "Xray Node",
                                    "core": "xray",
                                    "status": "stopped",
                                    "port": 21081,
                                    "started_at": 11,
                                },
                            ],
                            "tests": {},
                            "request_logs": [],
                        }
                    ),
                    encoding="utf-8",
                )
                (app.RUNTIME_ROOT / "bridge-sing.log").write_text("sing-box log line", encoding="utf-8")
                (app.RUNTIME_ROOT / "bridge-xray.log").write_text("xray log line", encoding="utf-8")

                payload = app.core_log_payload("sing-box")
                self.assertEqual(payload["core"], "sing-box")
                self.assertEqual(len(payload["entries"]), 2)
                self.assertEqual(payload["hidden_empty_count"], 1)
                entries_by_id = {entry["id"]: entry for entry in payload["entries"]}
                self.assertIn("bridge-sing", entries_by_id)
                self.assertIn("bridge-failed-empty", entries_by_id)
                self.assertEqual(entries_by_id["bridge-sing"]["node_name"], "Sing Node")
                self.assertIn("sing-box log line", entries_by_id["bridge-sing"]["log"])
                self.assertFalse(entries_by_id["bridge-failed-empty"]["has_log"])
        finally:
            app.WORK_ROOT = old_work_root
            app.RUNTIME_ROOT = old_runtime_root
            app.STATE_PATH = old_state_path

    def test_start_bridge_cleans_process_table_when_core_exits_immediately(self):
        old_work_root = app.WORK_ROOT
        old_runtime_root = app.RUNTIME_ROOT
        old_state_path = app.STATE_PATH
        old_generate_config = app.generate_config
        old_core_command = app.core_command
        old_wait_for_port = app.wait_for_port
        old_process_table = dict(app.PROCESS_TABLE)
        old_log_proxy_table = dict(app.LOG_PROXY_TABLE)
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                app.WORK_ROOT = Path(temp_dir)
                app.RUNTIME_ROOT = Path(temp_dir) / "runtime"
                app.STATE_PATH = Path(temp_dir) / "state.json"
                app.ensure_dirs()
                node_id = "node-cleanup"
                app.STATE_PATH.write_text(
                    json.dumps(
                        {
                            "subscriptions": [],
                            "nodes": [
                                {
                                    "id": node_id,
                                    "name": "cleanup",
                                    "protocol": "socks",
                                    "server": "127.0.0.1",
                                    "port": 1,
                                    "region": "本地",
                                    "source": "test",
                                    "raw": "",
                                    "settings": {},
                                    "tags": [],
                                    "transport": "tcp",
                                    "security": "",
                                    "preferred_core": "sing-box",
                                }
                            ],
                            "bridges": [],
                            "tests": {},
                            "request_logs": [],
                        }
                    ),
                    encoding="utf-8",
                )
                app.generate_config = lambda core, node, port: {"test": True}
                app.core_command = lambda core, path: [app.sys.executable, "-c", "raise SystemExit(2)"]
                app.wait_for_port = lambda port, timeout=2.0: False

                with self.assertRaises(RuntimeError):
                    app.start_bridge({"node_id": node_id, "core": "auto"})
                self.assertEqual(app.PROCESS_TABLE, {})
                self.assertEqual(app.LOG_PROXY_TABLE, {})
        finally:
            app.WORK_ROOT = old_work_root
            app.RUNTIME_ROOT = old_runtime_root
            app.STATE_PATH = old_state_path
            app.generate_config = old_generate_config
            app.core_command = old_core_command
            app.wait_for_port = old_wait_for_port
            app.PROCESS_TABLE.clear()
            app.PROCESS_TABLE.update(old_process_table)
            app.LOG_PROXY_TABLE.clear()
            app.LOG_PROXY_TABLE.update(old_log_proxy_table)

    def test_start_bridge_reuses_last_node_port_after_stop(self):
        old_work_root = app.WORK_ROOT
        old_runtime_root = app.RUNTIME_ROOT
        old_state_path = app.STATE_PATH
        old_generate_config = app.generate_config
        old_core_command = app.core_command
        old_wait_for_port = app.wait_for_port
        old_popen = app.subprocess.Popen
        old_proxy_class = app.Socks5LogProxy
        old_is_port_free = app.is_port_free
        old_process_table = dict(app.PROCESS_TABLE)
        old_log_proxy_table = dict(app.LOG_PROXY_TABLE)

        class FakePopen:
            def __init__(self, command: list[str], **kwargs: object) -> None:
                self.pid = 4343

            def poll(self) -> None:
                return None

        class FakeProxy:
            def __init__(
                self,
                *,
                bridge_id: str,
                node_id: str,
                node_name: str,
                listen_port: int,
                upstream_port: int,
            ) -> None:
                self.listen_port = listen_port
                self.started = False

            def start(self) -> None:
                self.started = True

            def stop(self) -> None:
                self.started = False

            def is_alive(self) -> bool:
                return self.started

        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                app.WORK_ROOT = Path(temp_dir)
                app.RUNTIME_ROOT = Path(temp_dir) / "runtime"
                app.STATE_PATH = Path(temp_dir) / "state.json"
                app.ensure_dirs()
                node_id = "node-port-memory"
                remembered_port = 25123
                app.STATE_PATH.write_text(
                    json.dumps(
                        {
                            "subscriptions": [],
                            "nodes": [
                                {
                                    "id": node_id,
                                    "name": "Port Memory",
                                    "protocol": "socks",
                                    "server": "127.0.0.1",
                                    "port": 1,
                                    "region": "本地",
                                    "source": "test",
                                    "raw": "",
                                    "settings": {},
                                    "tags": [],
                                    "transport": "tcp",
                                    "security": "",
                                    "preferred_core": "sing-box",
                                }
                            ],
                            "bridges": [
                                {
                                    "id": "old-bridge",
                                    "node_id": node_id,
                                    "node_name": "Port Memory",
                                    "core": "sing-box",
                                    "status": "stopped",
                                    "enabled": False,
                                    "port": remembered_port,
                                    "started_at": 10,
                                }
                            ],
                            "tests": {},
                            "request_logs": [],
                        }
                    ),
                    encoding="utf-8",
                )
                app.generate_config = lambda core, node, port: {"test": True}
                app.core_command = lambda core, path: [app.sys.executable, "-c", "pass"]
                app.wait_for_port = lambda port, timeout=2.0: True
                app.subprocess.Popen = FakePopen
                app.Socks5LogProxy = FakeProxy
                app.is_port_free = lambda port: True

                bridge = app.start_bridge({"node_id": node_id, "core": "auto"})

                self.assertEqual(bridge["port"], remembered_port)
                self.assertEqual(bridge["socks_url"], f"socks5://127.0.0.1:{remembered_port}")
                self.assertEqual(bridge["http_url"], f"http://127.0.0.1:{remembered_port}")
        finally:
            app.WORK_ROOT = old_work_root
            app.RUNTIME_ROOT = old_runtime_root
            app.STATE_PATH = old_state_path
            app.generate_config = old_generate_config
            app.core_command = old_core_command
            app.wait_for_port = old_wait_for_port
            app.subprocess.Popen = old_popen
            app.Socks5LogProxy = old_proxy_class
            app.is_port_free = old_is_port_free
            app.PROCESS_TABLE.clear()
            app.PROCESS_TABLE.update(old_process_table)
            app.LOG_PROXY_TABLE.clear()
            app.LOG_PROXY_TABLE.update(old_log_proxy_table)

    def test_parse_vless_reality_vision(self):
        raw = (
            "vless://11111111-1111-1111-1111-111111111111@example.com:443"
            "?encryption=none&security=reality&sni=www.apple.com&fp=chrome"
            "&pbk=public-key&sid=abc&type=tcp&flow=xtls-rprx-vision#HK%20Reality"
        )
        node = parse_link(raw, "demo")
        self.assertIsNotNone(node)
        self.assertEqual(node.protocol, "vless")
        self.assertEqual(node.security, "reality")
        self.assertEqual(node.settings["flow"], "xtls-rprx-vision")

    def test_parse_vmess_ws(self):
        raw_json = {
            "v": "2",
            "ps": "JP VMess",
            "add": "vmess.example.com",
            "port": "443",
            "id": "22222222-2222-2222-2222-222222222222",
            "aid": "0",
            "scy": "auto",
            "net": "ws",
            "type": "none",
            "host": "cdn.example.com",
            "path": "/ws",
            "tls": "tls",
            "sni": "cdn.example.com",
        }
        node = parse_link("vmess://" + b64(json.dumps(raw_json)), "demo")
        self.assertIsNotNone(node)
        self.assertEqual(node.protocol, "vmess")
        self.assertEqual(node.transport, "ws")
        self.assertEqual(node.security, "tls")

    def test_parse_shadowsocks_full_base64(self):
        raw = "ss://" + b64("2022-blake3-aes-128-gcm:secret@example.net:8388") + "#US%20SS2022"
        node = parse_link(raw, "demo")
        self.assertIsNotNone(node)
        self.assertEqual(node.protocol, "shadowsocks")
        self.assertEqual(node.settings["method"], "2022-blake3-aes-128-gcm")
        self.assertEqual(node.server, "example.net")

    def test_parse_trojan_hy2_tuic_and_socks(self):
        payload = "\n".join(
            [
                "trojan://secret@trojan.example.com:443?sni=edge.example.com&type=ws&path=%2Fws#JP%20Trojan",
                "hy2://secret@hy2.example.com:443?sni=hy2.example.com#SG%20HY2",
                "tuic://33333333-3333-3333-3333-333333333333:secret@tuic.example.com:443?sni=tuic.example.com#US%20TUIC",
                "socks5://user:pass@socks.example.com:1080#HK%20SOCKS",
            ]
        )
        nodes = parse_subscription_payload(payload, "demo")
        protocols = {node.protocol for node in nodes}
        self.assertEqual(protocols, {"trojan", "hysteria2", "tuic", "socks"})
        hy2 = next(node for node in nodes if node.protocol == "hysteria2")
        self.assertEqual(hy2.settings["password"], "secret")

    def test_generate_sing_box_config_for_vless(self):
        raw = (
            "vless://11111111-1111-1111-1111-111111111111@example.com:443"
            "?encryption=none&security=reality&sni=www.apple.com&fp=chrome"
            "&pbk=public-key&sid=abc&type=tcp#HK%20Reality"
        )
        node = parse_link(raw, "demo")
        config = sing_box_config(node.__dict__, 21080)
        self.assertEqual(config["inbounds"][0]["listen_port"], 21080)
        self.assertNotIn("sniff", config["inbounds"][0])
        self.assertEqual(config["route"]["rules"][0]["action"], "sniff")
        outbound = config["outbounds"][0]
        self.assertEqual(outbound["type"], "vless")
        self.assertTrue(outbound["tls"]["reality"]["enabled"])

    def test_log_proxy_address_tail_excludes_atyp(self):
        class FakeSocket:
            def __init__(self, data: bytes):
                self.data = bytearray(data)

            def recv(self, length: int) -> bytes:
                chunk = bytes(self.data[:length])
                del self.data[:length]
                return chunk

        proxy = Socks5LogProxy(
            bridge_id="bridge-test",
            node_id="node-test",
            node_name="测试节点",
            listen_port=1,
            upstream_port=2,
        )
        host = b"example.com"
        target_host, target_port, request_tail = proxy._read_address(
            FakeSocket(bytes([len(host)]) + host + (443).to_bytes(2, "big")),
            3,
        )
        self.assertEqual(target_host, "example.com")
        self.assertEqual(target_port, 443)
        self.assertEqual(request_tail, bytes([len(host)]) + host + (443).to_bytes(2, "big"))

    def test_socks5_log_proxy_records_connect(self):
        upstream_port = free_port()
        listen_port = free_port()
        logs: list[dict] = []

        def append_log(entry: dict) -> None:
            logs.append(dict(entry))

        def update_log(log_id: str, updates: dict) -> None:
            for entry in logs:
                if entry["id"] == log_id:
                    entry.update(updates)
                    break

        def upstream_server() -> None:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
                server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                server.bind(("127.0.0.1", upstream_port))
                server.listen(1)
                conn, _ = server.accept()
                with conn:
                    self.assertEqual(read_exact(conn, 3), b"\x05\x01\x00")
                    conn.sendall(b"\x05\x00")
                    head = read_exact(conn, 4)
                    self.assertEqual(head[:3], b"\x05\x01\x00")
                    self.assertEqual(head[3], 3)
                    size = read_exact(conn, 1)[0]
                    self.assertEqual(read_exact(conn, size), b"example.com")
                    self.assertEqual(int.from_bytes(read_exact(conn, 2), "big"), 443)
                    conn.sendall(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
                    self.assertEqual(conn.recv(4), b"ping")
                    conn.sendall(b"pong")

        old_append = app.append_request_log
        old_update = app.update_request_log
        old_traffic_table = dict(app.TRAFFIC_TABLE)
        app.append_request_log = append_log
        app.update_request_log = update_log
        app.TRAFFIC_TABLE.clear()
        app.init_bridge_traffic("bridge-test")
        proxy = Socks5LogProxy(
            bridge_id="bridge-test",
            node_id="node-test",
            node_name="测试节点",
            listen_port=listen_port,
            upstream_port=upstream_port,
        )
        try:
            server_thread = threading.Thread(target=upstream_server, daemon=True)
            server_thread.start()
            proxy.start()
            with socket.create_connection(("127.0.0.1", listen_port), timeout=3) as client:
                client.sendall(b"\x05\x01\x00")
                self.assertEqual(read_exact(client, 2), b"\x05\x00")
                host = b"example.com"
                client.sendall(b"\x05\x01\x00\x03" + bytes([len(host)]) + host + (443).to_bytes(2, "big"))
                self.assertEqual(read_exact(client, 10), b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
                client.sendall(b"ping")
                self.assertEqual(read_exact(client, 4), b"pong")
            server_thread.join(timeout=2)
            time.sleep(0.1)
            self.assertEqual(logs[0]["target"], "example.com:443")
            self.assertEqual(logs[0]["node_name"], "测试节点")
            self.assertEqual(logs[0]["status"], "closed")
            self.assertGreaterEqual(logs[0]["upload_bytes"], 4)
            self.assertGreaterEqual(logs[0]["download_bytes"], 4)
            traffic = app.bridge_traffic_snapshot("bridge-test")
            self.assertGreaterEqual(traffic["upload_bytes"], 4)
            self.assertGreaterEqual(traffic["download_bytes"], 4)
            self.assertEqual(traffic["total_bytes"], traffic["upload_bytes"] + traffic["download_bytes"])
        finally:
            proxy.stop()
            app.append_request_log = old_append
            app.update_request_log = old_update
            app.TRAFFIC_TABLE.clear()
            app.TRAFFIC_TABLE.update(old_traffic_table)

    def test_relay_returns_when_one_side_closes_and_peer_stays_open(self):
        proxy = Socks5LogProxy(
            bridge_id="bridge-half-close",
            node_id="node-test",
            node_name="测试节点",
            listen_port=1,
            upstream_port=2,
        )
        client_proxy, client_peer = socket.socketpair()
        upstream_proxy, upstream_peer = socket.socketpair()
        result: dict[str, dict[str, int]] = {}

        def run_relay() -> None:
            result["counters"] = proxy._relay(client_proxy, upstream_proxy)

        relay_thread = threading.Thread(target=run_relay, daemon=True)
        relay_thread.start()
        try:
            client_peer.sendall(b"hello")
            self.assertEqual(read_exact(upstream_peer, 5), b"hello")
            client_peer.close()
            relay_thread.join(timeout=0.5)
            self.assertFalse(relay_thread.is_alive())
            self.assertEqual(result["counters"]["upload"], 5)
        finally:
            proxy._stop.set()
            for sock in (client_proxy, client_peer, upstream_proxy, upstream_peer):
                try:
                    sock.close()
                except OSError:
                    pass
            relay_thread.join(timeout=1.0)

    def test_http_connect_proxy_records_connect(self):
        upstream_port = free_port()
        listen_port = free_port()
        logs: list[dict] = []

        def append_log(entry: dict) -> None:
            logs.append(dict(entry))

        def update_log(log_id: str, updates: dict) -> None:
            for entry in logs:
                if entry["id"] == log_id:
                    entry.update(updates)
                    break

        def read_http_head(sock: socket.socket) -> bytes:
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = sock.recv(1024)
                if not chunk:
                    break
                data += chunk
            return data

        def upstream_server() -> None:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
                server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                server.bind(("127.0.0.1", upstream_port))
                server.listen(1)
                conn, _ = server.accept()
                with conn:
                    self.assertEqual(read_exact(conn, 3), b"\x05\x01\x00")
                    conn.sendall(b"\x05\x00")
                    head = read_exact(conn, 4)
                    self.assertEqual(head[:3], b"\x05\x01\x00")
                    self.assertEqual(head[3], 3)
                    size = read_exact(conn, 1)[0]
                    self.assertEqual(read_exact(conn, size), b"example.com")
                    self.assertEqual(int.from_bytes(read_exact(conn, 2), "big"), 443)
                    conn.sendall(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
                    self.assertEqual(conn.recv(4), b"ping")
                    conn.sendall(b"pong")

        old_append = app.append_request_log
        old_update = app.update_request_log
        old_traffic_table = dict(app.TRAFFIC_TABLE)
        app.append_request_log = append_log
        app.update_request_log = update_log
        app.TRAFFIC_TABLE.clear()
        app.init_bridge_traffic("bridge-test")
        proxy = Socks5LogProxy(
            bridge_id="bridge-test",
            node_id="node-test",
            node_name="测试节点",
            listen_port=listen_port,
            upstream_port=upstream_port,
        )
        try:
            server_thread = threading.Thread(target=upstream_server, daemon=True)
            server_thread.start()
            proxy.start()
            with socket.create_connection(("127.0.0.1", listen_port), timeout=3) as client:
                client.sendall(b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n")
                response_head = read_http_head(client)
                self.assertIn(b"200 Connection Established", response_head)
                client.sendall(b"ping")
                self.assertEqual(read_exact(client, 4), b"pong")
            server_thread.join(timeout=2)
            time.sleep(0.1)
            self.assertEqual(logs[0]["target"], "example.com:443")
            self.assertEqual(logs[0]["proxy_protocol"], "http")
            self.assertEqual(logs[0]["command"], "connect")
            self.assertEqual(logs[0]["status"], "closed")
        finally:
            proxy.stop()
            app.append_request_log = old_append
            app.update_request_log = old_update
            app.TRAFFIC_TABLE.clear()
            app.TRAFFIC_TABLE.update(old_traffic_table)

    def test_http_proxy_rewrites_absolute_form_request(self):
        upstream_port = free_port()
        listen_port = free_port()
        logs: list[dict] = []

        def append_log(entry: dict) -> None:
            logs.append(dict(entry))

        def update_log(log_id: str, updates: dict) -> None:
            for entry in logs:
                if entry["id"] == log_id:
                    entry.update(updates)
                    break

        def read_http_head(sock: socket.socket) -> bytes:
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = sock.recv(1024)
                if not chunk:
                    break
                data += chunk
            return data

        def upstream_server() -> None:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
                server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                server.bind(("127.0.0.1", upstream_port))
                server.listen(1)
                conn, _ = server.accept()
                with conn:
                    self.assertEqual(read_exact(conn, 3), b"\x05\x01\x00")
                    conn.sendall(b"\x05\x00")
                    head = read_exact(conn, 4)
                    self.assertEqual(head[:3], b"\x05\x01\x00")
                    self.assertEqual(head[3], 3)
                    size = read_exact(conn, 1)[0]
                    self.assertEqual(read_exact(conn, size), b"example.com")
                    self.assertEqual(int.from_bytes(read_exact(conn, 2), "big"), 80)
                    conn.sendall(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
                    request_head = read_http_head(conn)
                    self.assertTrue(request_head.startswith(b"GET /path?q=1 HTTP/1.1\r\n"))
                    self.assertIn(b"Host: example.com\r\n", request_head)
                    self.assertNotIn(b"Proxy-Connection:", request_head)
                    conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK")

        old_append = app.append_request_log
        old_update = app.update_request_log
        old_traffic_table = dict(app.TRAFFIC_TABLE)
        app.append_request_log = append_log
        app.update_request_log = update_log
        app.TRAFFIC_TABLE.clear()
        app.init_bridge_traffic("bridge-test")
        proxy = Socks5LogProxy(
            bridge_id="bridge-test",
            node_id="node-test",
            node_name="测试节点",
            listen_port=listen_port,
            upstream_port=upstream_port,
        )
        try:
            server_thread = threading.Thread(target=upstream_server, daemon=True)
            server_thread.start()
            proxy.start()
            with socket.create_connection(("127.0.0.1", listen_port), timeout=3) as client:
                client.sendall(
                    b"GET http://example.com/path?q=1 HTTP/1.1\r\n"
                    b"Host: example.com\r\n"
                    b"Proxy-Connection: keep-alive\r\n"
                    b"\r\n"
                )
                response = read_http_head(client)
                self.assertIn(b"HTTP/1.1 200 OK", response)
            server_thread.join(timeout=2)
            time.sleep(0.1)
            self.assertEqual(logs[0]["target"], "example.com:80")
            self.assertEqual(logs[0]["proxy_protocol"], "http")
            self.assertEqual(logs[0]["command"], "get")
            self.assertEqual(logs[0]["status"], "closed")
            self.assertGreater(logs[0]["upload_bytes"], 0)
            self.assertGreater(logs[0]["download_bytes"], 0)
        finally:
            proxy.stop()
            app.append_request_log = old_append
            app.update_request_log = old_update
            app.TRAFFIC_TABLE.clear()
            app.TRAFFIC_TABLE.update(old_traffic_table)


if __name__ == "__main__":
    unittest.main()
