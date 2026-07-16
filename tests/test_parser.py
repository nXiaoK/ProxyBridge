import base64
import hashlib
import io
import json
import os
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import zipfile
from pathlib import Path
from unittest import mock

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

        self.assertEqual(
            app._pick_sing_box_asset(release, "arm64", "darwin")["browser_download_url"],
            "https://example/sing-arm",
        )
        self.assertEqual(
            app._pick_sing_box_asset(release, "x86_64", "darwin")["browser_download_url"],
            "https://example/sing-amd",
        )
        self.assertEqual(
            app._pick_xray_asset(release, "arm64", "darwin")["browser_download_url"],
            "https://example/xray-arm",
        )
        self.assertEqual(
            app._pick_xray_asset(release, "x86_64", "darwin")["browser_download_url"],
            "https://example/xray-amd",
        )

    def test_release_asset_pickers_select_linux_and_windows_archives(self):
        release = {
            "assets": [
                {"name": "sing-box_1.0.0_linux_amd64.tar.gz", "browser_download_url": "https://example/sing-linux-amd"},
                {"name": "sing-box_1.0.0_linux_arm64.tar.gz", "browser_download_url": "https://example/sing-linux-arm"},
                {"name": "sing-box_1.0.0_windows_amd64.zip", "browser_download_url": "https://example/sing-win-amd"},
                {"name": "sing-box_1.0.0_darwin_arm64.tar.gz", "browser_download_url": "https://example/sing-mac"},
                {"name": "Xray-linux-64.zip", "browser_download_url": "https://example/xray-linux-amd"},
                {"name": "Xray-linux-arm64-v8a.zip", "browser_download_url": "https://example/xray-linux-arm"},
                {"name": "Xray-windows-64.zip", "browser_download_url": "https://example/xray-win-amd"},
                {"name": "Xray-windows-arm64-v8a.zip", "browser_download_url": "https://example/xray-win-arm"},
                {"name": "Xray-macos-arm64-v8a.zip", "browser_download_url": "https://example/xray-mac"},
            ]
        }

        self.assertEqual(
            app._pick_sing_box_asset(release, "x86_64", "linux")["browser_download_url"],
            "https://example/sing-linux-amd",
        )
        self.assertEqual(
            app._pick_sing_box_asset(release, "arm64", "linux")["browser_download_url"],
            "https://example/sing-linux-arm",
        )
        self.assertEqual(
            app._pick_sing_box_asset(release, "x86_64", "windows")["browser_download_url"],
            "https://example/sing-win-amd",
        )
        self.assertEqual(
            app._pick_xray_asset(release, "x86_64", "linux")["browser_download_url"],
            "https://example/xray-linux-amd",
        )
        self.assertEqual(
            app._pick_xray_asset(release, "arm64", "linux")["browser_download_url"],
            "https://example/xray-linux-arm",
        )
        self.assertEqual(
            app._pick_xray_asset(release, "x86_64", "windows")["browser_download_url"],
            "https://example/xray-win-amd",
        )
        self.assertEqual(
            app._pick_xray_asset(release, "arm64", "windows")["browser_download_url"],
            "https://example/xray-win-arm",
        )

    def test_fingerprint_asset_picker_is_os_aware(self):
        releases = [
            {
                "tag_name": "v1",
                "assets": [
                    {"name": "fingerprint-chromium_macos.dmg", "browser_download_url": "https://example/mac.dmg"},
                    {"name": "fingerprint-chromium_windows_x64.zip", "browser_download_url": "https://example/win.zip"},
                    {"name": "fingerprint-chromium_linux_x64.zip", "browser_download_url": "https://example/linux.zip"},
                ],
            }
        ]
        self.assertEqual(app._pick_fingerprint_asset(releases, "darwin")["url"], "https://example/mac.dmg")
        self.assertEqual(app._pick_fingerprint_asset(releases, "windows")["url"], "https://example/win.zip")
        self.assertEqual(app._pick_fingerprint_asset(releases, "linux")["url"], "https://example/linux.zip")

    def test_fingerprint_asset_picker_preserves_integrity_metadata(self):
        releases = [
            {
                "tag_name": "v1",
                "assets": [
                    {
                        "name": "fingerprint-chromium_linux_x64.zip",
                        "browser_download_url": "https://example/linux.zip",
                        "size": 123,
                        "digest": f"sha256:{'a' * 64}",
                    }
                ],
            }
        ]

        asset = app._pick_fingerprint_asset(releases, "linux")
        self.assertEqual(asset["size"], 123)
        self.assertEqual(asset["digest"], f"sha256:{'a' * 64}")
        self.assertEqual(asset["browser_download_url"], "https://example/linux.zip")

    def test_download_asset_retries_atomically_and_validates_digest(self):
        payload = b"verified payload"
        digest = hashlib.sha256(payload).hexdigest()

        class FakeResponse(io.BytesIO):
            def __init__(self) -> None:
                super().__init__(payload)
                self.headers = {"Content-Length": str(len(payload))}

            def geturl(self) -> str:
                return "https://cdn.example/asset.zip"

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                self.close()

        asset = {
            "name": "asset.zip",
            "browser_download_url": "https://example/asset.zip",
            "size": len(payload),
            "digest": f"sha256:{digest}",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "asset.zip"
            target.write_bytes(b"previous")
            with mock.patch.object(
                app,
                "_open_https_url",
                side_effect=[urllib.error.URLError("temporary"), FakeResponse()],
            ) as opener:
                app._download_asset(asset, target, retry_count=2, retry_backoff=0)

            self.assertEqual(target.read_bytes(), payload)
            self.assertEqual(list(target.parent.glob("*.part")), [])
            self.assertEqual(opener.call_count, 2)

    def test_download_asset_rejects_bad_digest_without_replacing_target(self):
        payload = b"tampered"

        class FakeResponse(io.BytesIO):
            def __init__(self) -> None:
                super().__init__(payload)
                self.headers = {"Content-Length": str(len(payload))}

            def geturl(self) -> str:
                return "https://example/asset.zip"

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                self.close()

        asset = {
            "name": "asset.zip",
            "browser_download_url": "https://example/asset.zip",
            "digest": f"sha256:{'0' * 64}",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "asset.zip"
            target.write_bytes(b"previous")
            with mock.patch.object(app, "_open_https_url", return_value=FakeResponse()):
                with self.assertRaisesRegex(RuntimeError, "SHA-256"):
                    app._download_asset(asset, target, retry_count=1, retry_backoff=0)

            self.assertEqual(target.read_bytes(), b"previous")
            self.assertEqual(list(target.parent.glob("*.part")), [])

    def test_download_asset_rejects_unsafe_name_and_http_url(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(RuntimeError, "名称不安全"):
                app._safe_asset_name({"name": "../asset.zip"})
            with self.assertRaisesRegex(RuntimeError, "HTTPS"):
                app._download_asset(
                    {"name": "asset.zip", "browser_download_url": "http://example/asset.zip"},
                    Path(temp_dir) / "asset.zip",
                    retry_count=1,
                )

    def test_https_redirect_handler_rejects_protocol_downgrade(self):
        handler = app._HTTPSOnlyRedirectHandler()
        request = app.urllib.request.Request("https://github.com/asset")

        with self.assertRaisesRegex(RuntimeError, "HTTPS"):
            handler.redirect_request(
                request,
                None,
                302,
                "Found",
                {},
                "http://objects.example/asset",
            )

    def test_download_asset_enforces_limit_without_length_metadata(self):
        payload = b"12345"

        class FakeResponse(io.BytesIO):
            headers = {}

            def geturl(self) -> str:
                return "https://example/asset.zip"

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                self.close()

        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "asset.zip"
            target.write_bytes(b"previous")
            with (
                mock.patch.object(app, "DOWNLOAD_RESPONSE_LIMIT", 4),
                mock.patch.object(app, "_open_https_url", return_value=FakeResponse(payload)),
            ):
                with self.assertRaisesRegex(RuntimeError, "超过 4 字节限制"):
                    app._download_asset(
                        {"name": "asset.zip", "browser_download_url": "https://example/asset.zip"},
                        target,
                        retry_count=1,
                    )

            self.assertEqual(target.read_bytes(), b"previous")
            self.assertEqual(list(target.parent.glob("*.part")), [])

    def test_fingerprint_picker_sorts_releases_and_accepts_darwin_name(self):
        releases = [
            {
                "tag_name": "old",
                "published_at": "2025-01-01T00:00:00Z",
                "assets": [
                    {
                        "name": "fingerprint-chromium_macos.dmg",
                        "browser_download_url": "https://example/old.dmg",
                    }
                ],
            },
            {
                "tag_name": "new",
                "published_at": "2026-01-01T00:00:00Z",
                "assets": [
                    {
                        "name": "fingerprint-chromium_darwin.dmg",
                        "browser_download_url": "https://example/new.dmg",
                    }
                ],
            },
        ]

        asset = app._pick_fingerprint_asset(releases, "darwin")
        self.assertEqual(asset["tag"], "new")
        self.assertEqual(asset["url"], "https://example/new.dmg")

    def test_install_path_replacement_restores_previous_target_on_failure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "Chromium.app"
            staged = root / ".Chromium.app.install"
            target.mkdir()
            staged.mkdir()
            (target / "old").write_text("old", encoding="utf-8")
            (staged / "new").write_text("new", encoding="utf-8")
            original_replace = Path.replace

            def fail_staged_replace(path: Path, destination: Path):
                if path == staged:
                    raise OSError("copy interrupted")
                return original_replace(path, destination)

            with mock.patch.object(Path, "replace", autospec=True, side_effect=fail_staged_replace):
                with self.assertRaisesRegex(OSError, "copy interrupted"):
                    app._replace_installed_path(staged, target)

            self.assertEqual((target / "old").read_text(encoding="utf-8"), "old")
            self.assertFalse((target / "new").exists())

    def test_install_executable_preserves_previous_binary_on_copy_failure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "downloaded-core"
            target = root / "bin" / "core"
            source.write_bytes(b"new-core")
            target.parent.mkdir()
            target.write_bytes(b"old-core")

            def partial_copy(_source: Path, destination: Path) -> None:
                destination.write_bytes(b"partial")
                raise OSError("copy interrupted")

            with mock.patch.object(app.shutil, "copy2", side_effect=partial_copy):
                with self.assertRaisesRegex(OSError, "copy interrupted"):
                    app._install_executable(source, target)

            self.assertEqual(target.read_bytes(), b"old-core")
            self.assertEqual(list(target.parent.glob("*.install-*")), [])

    def test_install_transaction_rejects_duplicate_targets(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "core"
            first = root / ".core.first"
            second = root / ".core.second"
            target.write_bytes(b"old")
            first.write_bytes(b"first")
            second.write_bytes(b"second")

            with self.assertRaisesRegex(ValueError, "目标不能重复"):
                app._replace_install_files([(first, target), (second, target)])

            self.assertEqual(target.read_bytes(), b"old")
            self.assertEqual(first.read_bytes(), b"first")
            self.assertEqual(second.read_bytes(), b"second")
            self.assertEqual(list(root.glob("*.backup-*")), [])

    def test_core_install_rolls_back_binary_when_metadata_commit_fails(self):
        old_work_root = app.WORK_ROOT
        old_download_asset = app._download_asset
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                app.WORK_ROOT = Path(temp_dir)
                app.ensure_dirs()
                target = app.proxy_core_bin_dir() / ("sing-box.exe" if os.name == "nt" else "sing-box")
                version_path = app.proxy_core_root() / "sing-box.version"
                target.write_bytes(b"old-core")
                version_path.write_text("v-old\nold.zip\n", encoding="utf-8")

                def fake_download(_asset: dict, archive_path: Path) -> None:
                    with zipfile.ZipFile(archive_path, "w") as archive:
                        archive.writestr(target.name, b"new-core")

                original_replace = Path.replace

                def fail_metadata_commit(path: Path, destination: Path):
                    if path.name.startswith(".sing-box.version.install-") and destination == version_path:
                        raise OSError("metadata commit failed")
                    return original_replace(path, destination)

                app._download_asset = fake_download
                with mock.patch.object(Path, "replace", autospec=True, side_effect=fail_metadata_commit):
                    with self.assertRaisesRegex(OSError, "metadata commit failed"):
                        app._install_core_from_asset(
                            "sing-box",
                            {
                                "name": "sing-box-test.zip",
                                "browser_download_url": "https://example/sing-box.zip",
                            },
                            "v-new",
                        )

                self.assertEqual(target.read_bytes(), b"old-core")
                self.assertEqual(version_path.read_text(encoding="utf-8"), "v-old\nold.zip\n")
                self.assertEqual(list(app.proxy_core_root().rglob("*.install-*")), [])
                self.assertEqual(list(app.proxy_core_root().rglob("*.backup-*")), [])
        finally:
            app.WORK_ROOT = old_work_root
            app._download_asset = old_download_asset

    def test_github_json_retries_and_caps_response(self):
        payload = json.dumps({"ok": True}).encode("utf-8")

        class FakeResponse(io.BytesIO):
            def __init__(self, data: bytes, declared_size=None) -> None:
                super().__init__(data)
                self.headers = {}
                if declared_size is not None:
                    self.headers["Content-Length"] = str(declared_size)

            def geturl(self) -> str:
                return "https://api.github.com/example"

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                self.close()

        with mock.patch.object(
            app,
            "_open_https_url",
            side_effect=[urllib.error.URLError("temporary"), FakeResponse(payload)],
        ) as opener:
            result = app._github_json(
                "https://api.github.com/example",
                retry_count=2,
                retry_backoff=0,
            )
        self.assertEqual(result, {"ok": True})
        self.assertEqual(opener.call_count, 2)

        with (
            mock.patch.object(app, "GITHUB_JSON_RESPONSE_LIMIT", 4),
            mock.patch.object(app, "_open_https_url", return_value=FakeResponse(b"{}", declared_size=5)),
        ):
            with self.assertRaisesRegex(RuntimeError, "响应过大"):
                app._github_json("https://api.github.com/example", retry_count=1)

        with mock.patch.object(
            app,
            "_open_https_url",
            return_value=FakeResponse(payload, declared_size=len(payload) + 1),
        ):
            with self.assertRaisesRegex(RuntimeError, "响应长度不匹配"):
                app._github_json("https://api.github.com/example", retry_count=1)

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
        old_synchronize_process_state = app.synchronize_process_state

        class FakePopen:
            calls: list[tuple[list[str], dict[str, object]]] = []

            def __init__(self, command: list[str], **kwargs: object) -> None:
                self.pid = 4242
                self.command = command
                FakePopen.calls.append((command, kwargs))

            def poll(self) -> None:
                return None

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
                app.synchronize_process_state = lambda: None
                fake_bridge = {
                    "id": "bridge-browser",
                    "node_id": node_id,
                    "node_name": "Browser Node",
                    "port": 25080,
                    "socks_url": "socks5://127.0.0.1:25080",
                    "status": "running",
                }

                def fake_start_bridge(_payload):
                    with app.STATE_LOCK:
                        state = app.load_state()
                        if not any(item.get("id") == fake_bridge["id"] for item in state["bridges"]):
                            state["bridges"].append(dict(fake_bridge))
                            app.save_state(state)
                    return dict(fake_bridge)

                app.start_bridge = fake_start_bridge

                result = app.launch_browser({"node_id": node_id})
                self.assertEqual(result["proxy"], "socks5://127.0.0.1:25080")
                self.assertEqual(result["pid"], 4242)
                self.assertIn("instance_id", result)
                instance_id = result["instance_id"]
                self.assertEqual(len(FakePopen.calls), 1)
                command = FakePopen.calls[0][0]
                self.assertEqual(command[0], str(fp_kernel))
                self.assertIn("--proxy-server=socks5://127.0.0.1:25080", command)
                # 校验方案 B 的指纹伪造标志已下发。
                self.assertTrue(any(arg.startswith("--fingerprint=") for arg in command))
                self.assertIn(f"--fingerprint-platform={app.fingerprint_platform_token()}", command)
                self.assertIn("--webrtc-ip-handling-policy=disable_non_proxied_udp", command)
                self.assertTrue(any(arg.startswith("--timezone=") for arg in command))
                self.assertTrue(any(arg.startswith("--lang=") for arg in command))
                self.assertEqual(result["fingerprint_seed"], app.fingerprint_seed(f"{node_id}:{instance_id}"))
                self.assertNotEqual(result["fingerprint_seed"], app.fingerprint_seed(node_id))
                profile_arg = next(arg for arg in command if arg.startswith("--user-data-dir="))
                profile_path = Path(profile_arg.split("=", 1)[1])
                self.assertTrue(profile_path.is_dir())
                self.assertEqual(profile_path.parent, app.browser_profiles_root() / app.stable_id(node_id))
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

                second = app.launch_browser({"node_id": node_id})
                self.assertNotEqual(second["instance_id"], instance_id)
                self.assertNotEqual(second["fingerprint_seed"], result["fingerprint_seed"])
                self.assertNotEqual(second["profile_path"], result["profile_path"])
                state = app.load_state()
                instances = [item for item in state["browser_instances"] if item["node_id"] == node_id]
                self.assertEqual(len(instances), 2)
                self.assertTrue(all(item["status"] == "running" for item in instances))
                self.assertEqual([item["name"] for item in instances], ["浏览器 1", "浏览器 2"])
        finally:
            app.WORK_ROOT = old_work_root
            app.RUNTIME_ROOT = old_runtime_root
            app.STATE_PATH = old_state_path
            app.subprocess.Popen = old_popen
            app.start_bridge = old_start_bridge
            app.synchronize_process_state = old_synchronize_process_state
            if old_chrome is None:
                os.environ.pop("PROXY_BRIDGE_CHROME", None)
            else:
                os.environ["PROXY_BRIDGE_CHROME"] = old_chrome
            if old_fp_kernel is None:
                os.environ.pop("PROXY_BRIDGE_FP_CHROMIUM", None)
            else:
                os.environ["PROXY_BRIDGE_FP_CHROMIUM"] = old_fp_kernel

    def test_load_state_adds_legacy_browser_instance_for_existing_profile(self):
        old_work_root = app.WORK_ROOT
        old_runtime_root = app.RUNTIME_ROOT
        old_state_path = app.STATE_PATH
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                app.WORK_ROOT = Path(temp_dir)
                app.RUNTIME_ROOT = Path(temp_dir) / "runtime"
                app.STATE_PATH = Path(temp_dir) / "state.json"
                app.ensure_dirs()
                node_id = "node-legacy-browser"
                legacy_profile = app.browser_profiles_root() / app.stable_id(node_id)
                (legacy_profile / "Default").mkdir(parents=True)
                app.STATE_PATH.write_text(
                    json.dumps(
                        {
                            "subscriptions": [],
                            "nodes": [
                                {
                                    "id": node_id,
                                    "name": "Legacy Browser Node",
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

                state = app.load_state()
                legacy = state["browser_instances"][0]
                self.assertTrue(legacy["legacy"])
                self.assertEqual(legacy["node_id"], node_id)
                self.assertEqual(legacy["profile_path"], str(legacy_profile))
                self.assertEqual(legacy["fingerprint_seed"], app.fingerprint_seed(node_id))
                self.assertEqual(legacy["status"], "stopped")
        finally:
            app.WORK_ROOT = old_work_root
            app.RUNTIME_ROOT = old_runtime_root
            app.STATE_PATH = old_state_path

    def test_stop_single_browser_instance_keeps_sibling_and_socks_bridge(self):
        old_work_root = app.WORK_ROOT
        old_runtime_root = app.RUNTIME_ROOT
        old_state_path = app.STATE_PATH
        old_browser_table = {key: list(value) for key, value in app.BROWSER_TABLE.items()}
        old_process_table = dict(app.PROCESS_TABLE)
        old_log_proxy_table = dict(app.LOG_PROXY_TABLE)
        old_browser_process_table = dict(getattr(app, "BROWSER_PROCESS_TABLE", {}))
        old_terminate_process = app.terminate_process
        old_wait_for_port = app.wait_for_port

        class FakeProcess:
            def __init__(self, pid: int) -> None:
                self.pid = pid

            def poll(self) -> None:
                return None

        killed_pids: list[int] = []

        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                app.WORK_ROOT = Path(temp_dir)
                app.RUNTIME_ROOT = Path(temp_dir) / "runtime"
                app.STATE_PATH = Path(temp_dir) / "state.json"
                app.ensure_dirs()
                node_id = "node-single-instance-stop"
                bridge_id = "bridge-single-instance-stop"
                first_id = "browser-first"
                second_id = "browser-second"
                app.STATE_PATH.write_text(
                    json.dumps(
                        {
                            "subscriptions": [],
                            "nodes": [],
                            "bridges": [
                                {
                                    "id": bridge_id,
                                    "node_id": node_id,
                                    "node_name": "Single Stop Node",
                                    "core": "sing-box",
                                    "status": "running",
                                    "enabled": True,
                                    "port": 25480,
                                    "pid": 5454,
                                    "started_at": 10,
                                }
                            ],
                            "browser_instances": [
                                {
                                    "id": first_id,
                                    "node_id": node_id,
                                    "name": "浏览器 1",
                                    "status": "running",
                                    "profile_path": str(app.browser_profiles_root() / app.stable_id(node_id) / first_id),
                                    "fingerprint_seed": app.fingerprint_seed(f"{node_id}:{first_id}"),
                                    "pid": 6463,
                                    "launch_count": 1,
                                    "legacy": False,
                                },
                                {
                                    "id": second_id,
                                    "node_id": node_id,
                                    "name": "浏览器 2",
                                    "status": "running",
                                    "profile_path": str(app.browser_profiles_root() / app.stable_id(node_id) / second_id),
                                    "fingerprint_seed": app.fingerprint_seed(f"{node_id}:{second_id}"),
                                    "pid": 6464,
                                    "launch_count": 1,
                                    "legacy": False,
                                },
                            ],
                            "tests": {},
                            "request_logs": [],
                        }
                    ),
                    encoding="utf-8",
                )
                app.PROCESS_TABLE.clear()
                app.PROCESS_TABLE[bridge_id] = FakeProcess(5454)
                app.LOG_PROXY_TABLE.clear()
                if hasattr(app, "BROWSER_PROCESS_TABLE"):
                    app.BROWSER_PROCESS_TABLE.clear()
                    app.BROWSER_PROCESS_TABLE[first_id] = FakeProcess(6463)
                    app.BROWSER_PROCESS_TABLE[second_id] = FakeProcess(6464)
                app.terminate_process = lambda process: killed_pids.append(process.pid)
                app.wait_for_port = lambda port, timeout=2.0: True

                result = app.stop_browser({"instance_id": first_id})
                state = app.load_state()
                instances = {item["id"]: item for item in state["browser_instances"]}
                bridge = state["bridges"][0]

                self.assertEqual(result["killed_browsers"], 1)
                self.assertEqual(killed_pids, [6463])
                self.assertEqual(instances[first_id]["status"], "stopped")
                self.assertIsNone(instances[first_id]["pid"])
                self.assertEqual(instances[second_id]["status"], "running")
                self.assertEqual(instances[second_id]["pid"], 6464)
                self.assertEqual(bridge["status"], "running")
                self.assertTrue(bridge["enabled"])
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
            if hasattr(app, "BROWSER_PROCESS_TABLE"):
                app.BROWSER_PROCESS_TABLE.clear()
                app.BROWSER_PROCESS_TABLE.update(old_browser_process_table)

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

    def test_read_tail_seeks_from_end_and_rotation_caps_backups(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "bridge.log"
            log_path.write_bytes(b"x" * 10000 + b"TAIL")
            app.runtime_log_backup_path(log_path, 1).write_bytes(b"older")

            self.assertEqual(app.read_tail(log_path, 4), "TAIL")
            self.assertTrue(app.rotate_runtime_log(log_path, max_bytes=5, backup_count=2))

            self.assertEqual(log_path.read_bytes(), b"")
            self.assertEqual(app.runtime_log_backup_path(log_path, 1).read_bytes(), b"xTAIL")
            self.assertEqual(app.runtime_log_backup_path(log_path, 2).read_bytes(), b"older")
            self.assertEqual(app.runtime_log_backup_path(log_path, 1).stat().st_mode & 0o777, 0o600)

    def test_runtime_log_pump_rotates_while_stream_is_writing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "bridge.log"
            app.pump_runtime_log(io.BytesIO(b"abcdefghijk"), log_path, max_bytes=5, backup_count=2)

            self.assertEqual(log_path.read_bytes(), b"k")
            self.assertEqual(app.runtime_log_backup_path(log_path, 1).read_bytes(), b"fghij")
            self.assertEqual(app.runtime_log_backup_path(log_path, 2).read_bytes(), b"abcde")
            self.assertLessEqual(log_path.stat().st_size, 5)

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

    def test_relay_preserves_response_after_client_half_closes(self):
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
        response = b"world"
        transfer_error: Exception | None = None
        try:
            client_peer.settimeout(2.0)
            upstream_peer.settimeout(2.0)
            client_peer.sendall(b"hello")
            client_peer.shutdown(socket.SHUT_WR)
            self.assertEqual(read_exact(upstream_peer, 5), b"hello")
            self.assertEqual(upstream_peer.recv(1), b"")
            try:
                upstream_peer.sendall(response)
                upstream_peer.shutdown(socket.SHUT_WR)
                received = read_exact(client_peer, len(response))
            except (OSError, RuntimeError) as exc:
                transfer_error = exc
                received = b""

            self.assertEqual(received, response, f"reverse transfer failed: {transfer_error!r}")
            self.assertEqual(client_peer.recv(1), b"")
            relay_thread.join(timeout=2.0)
            self.assertFalse(relay_thread.is_alive())
            self.assertEqual(result["counters"], {"upload": 5, "download": len(response)})
        finally:
            proxy._stop.set()
            with app.TRAFFIC_LOCK:
                app.TRAFFIC_TABLE.pop("bridge-half-close", None)
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



class PlatformSupportTests(unittest.TestCase):
    def test_cli_parser_accepts_headless_flags(self):
        args = app.parse_cli_args(["--port", "8799", "--host", "127.0.0.1", "--workdir", "/tmp/pb-work", "--no-core-auto-install"])
        self.assertEqual(args.port, 8799)
        self.assertEqual(args.host, "127.0.0.1")
        self.assertEqual(args.workdir, "/tmp/pb-work")
        self.assertTrue(args.no_core_auto_install)

    def test_configure_work_root_updates_paths(self):
        old_work = app.WORK_ROOT
        old_runtime = app.RUNTIME_ROOT
        old_state = app.STATE_PATH
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                target = Path(temp_dir) / "data"
                app.configure_work_root(target)
                self.assertEqual(app.WORK_ROOT, target.resolve())
                self.assertEqual(app.RUNTIME_ROOT, target.resolve() / "runtime")
                self.assertEqual(app.STATE_PATH, target.resolve() / "state.json")
        finally:
            app.WORK_ROOT = old_work
            app.RUNTIME_ROOT = old_runtime
            app.STATE_PATH = old_state

    def test_host_tokens_are_stable(self):
        self.assertIn(app.host_os_token(), {"darwin", "linux", "windows", "unknown"})
        self.assertIn(app.host_arch_token(), {"arm64", "x86_64"})
        self.assertIn(app.fingerprint_platform_token(), {"macos", "linux", "windows"})
