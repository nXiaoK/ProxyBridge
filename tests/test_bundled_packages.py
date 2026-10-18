from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app


def asset(name: str) -> dict[str, object]:
    return {"name": name, "browser_download_url": f"https://example.invalid/{name}"}


class BundledPackageTests(unittest.TestCase):
    """完整版（离线包）：首次启动从 bundled/ 安装 sing-box、Xray 与指纹内核。"""

    def setUp(self) -> None:
        self.old_work_root = app.WORK_ROOT
        self.old_runtime_root = app.RUNTIME_ROOT
        self.old_state_path = app.STATE_PATH
        self.temp_dir = tempfile.TemporaryDirectory()
        app.configure_work_root(Path(self.temp_dir.name) / "work")
        app.ensure_dirs()
        self.bundle = Path(self.temp_dir.name) / "bundled"
        self.env = mock.patch.dict(os.environ, {"PROXY_BRIDGE_BUNDLED_DIR": str(self.bundle)})
        self.env.start()
        self.host = mock.patch.multiple(
            app,
            host_os_token=mock.Mock(return_value="windows"),
            host_arch_token=mock.Mock(return_value="x86_64"),
        )
        self.host.start()

    def tearDown(self) -> None:
        self.host.stop()
        self.env.stop()
        app.configure_work_root(self.old_work_root)
        app.RUNTIME_ROOT = self.old_runtime_root
        app.STATE_PATH = self.old_state_path
        self.temp_dir.cleanup()

    def put(self, kind: str, name: str) -> Path:
        path = self.bundle / kind / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"package")
        return path

    def test_find_bundled_package_only_accepts_matching_official_names(self) -> None:
        self.assertIsNone(app.find_bundled_package("sing-box"))
        self.put("sing-box", "sing-box-1.14.3-darwin-arm64.tar.gz")
        self.put("sing-box", "renamed.zip")
        self.put("sing-box", ".hidden-sing-box-1.14.3-windows-amd64.zip")
        self.assertIsNone(app.find_bundled_package("sing-box"))
        expected = self.put("sing-box", "sing-box-1.14.3-windows-amd64.zip")
        self.assertEqual(app.find_bundled_package("sing-box"), expected)
        browser = self.put("fingerprint-chromium", "ungoogled-chromium_150.0.7871.186-1.1_windows_x64.zip")
        self.assertEqual(app.find_bundled_package("fingerprint-chromium"), browser)
        with self.assertRaises(ValueError):
            app.find_bundled_package("other")

    def test_default_roots_include_resources_and_frozen_executable_dir(self) -> None:
        with mock.patch.dict(os.environ, {"PROXY_BRIDGE_BUNDLED_DIR": ""}):
            self.assertEqual(app.bundled_package_roots(), [app.APP_ROOT / "bundled"])
            exe = Path(self.temp_dir.name) / "portable" / "ProxyBridge.exe"
            with mock.patch.object(app.sys, "frozen", True, create=True), mock.patch.object(app.sys, "executable", str(exe)):
                self.assertEqual(app.bundled_package_roots()[-1], exe.resolve().parent / "bundled")

    def test_ensure_core_installed_prefers_bundled_package_before_github(self) -> None:
        package = self.put("xray", "Xray-windows-64.zip")
        with (
            mock.patch.object(app, "core_paths", return_value={"sing-box": None, "xray": None}),
            mock.patch.object(app, "_install_core_from_archive", return_value="C:/cores/xray.exe") as install,
            mock.patch.object(app, "_github_json") as github,
        ):
            self.assertEqual(app.ensure_core_installed("xray"), "C:/cores/xray.exe")
        install.assert_called_once_with("xray", package, package.name, "", validate_binary=True)
        github.assert_not_called()

    def test_broken_bundled_core_falls_back_to_online_download(self) -> None:
        self.put("sing-box", "sing-box-1.14.3-windows-amd64.zip")
        release = {"tag_name": "v1.14.3", "assets": [asset("sing-box-1.14.3-windows-amd64.zip")]}
        with (
            mock.patch.object(app, "core_paths", return_value={"sing-box": None, "xray": None}),
            mock.patch.object(app, "_install_core_from_archive", side_effect=RuntimeError("损坏")),
            mock.patch.object(app, "_github_json", return_value=release),
            mock.patch.object(app, "_install_core_from_asset", return_value="C:/cores/sing-box.exe") as online,
        ):
            self.assertEqual(app.ensure_core_installed("sing-box"), "C:/cores/sing-box.exe")
        online.assert_called_once()

    def test_fingerprint_kernel_is_seeded_only_when_none_installed(self) -> None:
        package = self.put("fingerprint-chromium", "ungoogled-chromium_150.0.7871.186-1.1_windows_x64.zip")
        with (
            mock.patch.object(app, "fingerprint_chromium_path", return_value=None),
            mock.patch.object(app, "_install_fingerprint_package", return_value={"ok": True, "kernel_id": "150.0.7871.186"}) as install,
        ):
            result = app.install_bundled_fingerprint_kernel()
        self.assertEqual(result["kernel_id"], "150.0.7871.186")
        args, kwargs = install.call_args
        self.assertEqual(args[0], package)
        self.assertEqual(kwargs["kernel_id"], "150.0.7871.186")
        self.assertEqual(kwargs["source"], "bundled")

        with (
            mock.patch.object(app, "fingerprint_chromium_path", return_value="C:/kernel/chrome.exe"),
            mock.patch.object(app, "_install_fingerprint_package") as install,
        ):
            self.assertIsNone(app.install_bundled_fingerprint_kernel())
        install.assert_not_called()

        with mock.patch.dict(os.environ, {"PROXY_BRIDGE_DISABLE_BROWSER": "1"}), mock.patch.object(app, "_install_fingerprint_package") as install:
            self.assertIsNone(app.install_bundled_fingerprint_kernel())
        install.assert_not_called()


class CoreAssetPreferenceTests(unittest.TestCase):
    def test_standard_builds_win_over_legacy_compat_builds(self) -> None:
        sing_box = {
            "assets": [
                asset("sing-box-1.14.3-windows-amd64-legacy-windows-7.zip"),
                asset("sing-box-1.14.3-windows-amd64.zip"),
                asset("sing-box-1.14.3-darwin-amd64-legacy-macos-10.13.tar.gz"),
                asset("sing-box-1.14.3-darwin-amd64.tar.gz"),
            ]
        }
        self.assertEqual(app._pick_sing_box_asset(sing_box, "x86_64", "windows")["name"], "sing-box-1.14.3-windows-amd64.zip")
        self.assertEqual(app._pick_sing_box_asset(sing_box, "x86_64", "darwin")["name"], "sing-box-1.14.3-darwin-amd64.tar.gz")
        xray = {"assets": [asset("Xray-win7-64.zip"), asset("Xray-windows-64.zip")]}
        self.assertEqual(app._pick_xray_asset(xray, "x86_64", "windows")["name"], "Xray-windows-64.zip")
        # 只有兼容构建时仍可使用（例如用户本地导入 Windows 7 版本）。
        self.assertEqual(
            app._pick_xray_asset({"assets": [asset("Xray-win7-64.zip")]}, "x86_64", "windows")["name"],
            "Xray-win7-64.zip",
        )


if __name__ == "__main__":
    unittest.main()
