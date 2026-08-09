from __future__ import annotations

import hashlib
import io
import json
import os
import tempfile
import unittest
import urllib.parse
import zipfile
from email.message import Message
from pathlib import Path
from unittest import mock

import app


class TrackingBody(io.BytesIO):
    def __init__(self, value: bytes) -> None:
        super().__init__(value)
        self.read_calls = 0

    def read(self, size: int = -1) -> bytes:
        self.read_calls += 1
        return super().read(size)


def make_handler(
    body: bytes,
    *,
    headers: dict[str, str] | None = None,
    authenticated: bool = True,
    trusted: bool = True,
) -> app.AppHandler:
    handler = object.__new__(app.AppHandler)
    handler.command = "POST"
    handler.path = app.CORE_UPLOAD_PATH
    handler.requestline = f"POST {app.CORE_UPLOAD_PATH} HTTP/1.1"
    handler.request_version = "HTTP/1.1"
    handler.headers = Message()
    defaults = {
        "Host": "127.0.0.1:8787" if trusted else "evil.example",
        "Content-Type": "application/octet-stream",
        "Content-Length": str(len(body)),
        app.CORE_UPLOAD_KIND_HEADER: "sing-box",
        app.CORE_UPLOAD_NAME_HEADER: urllib.parse.quote(
            "sing-box-1.13.15-linux-amd64-glibc.tar.gz",
            safe="",
        ),
    }
    defaults.update(headers or {})
    for name, value in defaults.items():
        handler.headers[name] = value
    if authenticated:
        handler.headers[app.BACKEND_INSTANCE_HEADER] = app.management_api_token()
    handler.rfile = TrackingBody(body)
    handler.wfile = io.BytesIO()
    return handler


def response(handler: app.AppHandler) -> tuple[int, dict[str, object]]:
    raw_headers, body = handler.wfile.getvalue().split(b"\r\n\r\n", 1)
    status = int(raw_headers.split(b"\r\n", 1)[0].split(b" ", 2)[1])
    return status, json.loads(body)


class CoreUploadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_work_root = app.WORK_ROOT
        self.temp_dir = tempfile.TemporaryDirectory()
        app.configure_work_root(Path(self.temp_dir.name) / "work")
        app.invalidate_fingerprint_chromium_cache()

    def tearDown(self) -> None:
        app.configure_work_root(self.old_work_root)
        app.invalidate_fingerprint_chromium_cache()
        self.temp_dir.cleanup()

    def test_uploaded_filename_rejects_paths_controls_and_oversized_names(self) -> None:
        invalid = (
            "",
            ".",
            "..",
            "../core.zip",
            "..%2Fcore.zip",
            "folder%5Ccore.zip",
            "core%00.zip",
            urllib.parse.quote("x" * 181, safe=""),
        )
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                app._safe_uploaded_filename(value)

        filename = "sing-box-1.13.15-linux-amd64-glibc.tar.gz"
        self.assertEqual(
            app._safe_uploaded_filename(urllib.parse.quote(filename, safe="")),
            filename,
        )

    def test_package_names_must_keep_official_platform_and_architecture_markers(self) -> None:
        with (
            mock.patch.object(app, "host_os_token", return_value="linux"),
            mock.patch.object(app, "host_arch_token", return_value="x86_64"),
            mock.patch.object(app, "remote_browser_feature_enabled", return_value=False),
        ):
            app._validate_uploaded_package_name(
                "sing-box",
                "sing-box-1.13.15-linux-amd64-glibc.tar.gz",
            )
            app._validate_uploaded_package_name("xray", "Xray-linux-64.zip")
            with self.assertRaisesRegex(ValueError, "保留官方文件名"):
                app._validate_uploaded_package_name("sing-box", "sing-box.zip")
            with self.assertRaisesRegex(ValueError, "linux/x86_64"):
                app._validate_uploaded_package_name("xray", "Xray-windows-64.zip")

        with (
            mock.patch.object(app, "host_os_token", return_value="linux"),
            mock.patch.object(app, "host_arch_token", return_value="arm64"),
            mock.patch.object(app, "remote_browser_feature_enabled", return_value=True),
        ):
            app._validate_uploaded_package_name(
                "fingerprint-chromium",
                "fingerprint-chromium_linux_x64.zip",
            )
            with self.assertRaisesRegex(ValueError, "linux/x86_64"):
                app._validate_uploaded_package_name(
                    "fingerprint-chromium",
                    "fingerprint-chromium_linux_arm64.zip",
                )

    def test_upload_streams_to_private_staging_file_and_returns_digest(self) -> None:
        body = b"not-secret-package-content"
        handler = make_handler(body)
        captured: dict[str, object] = {}

        def fake_install(
            kind: str,
            package_path: Path,
            filename: str,
            *,
            size: int,
            sha256: str,
        ) -> dict[str, object]:
            captured.update(
                {
                    "kind": kind,
                    "filename": filename,
                    "size": size,
                    "sha256": sha256,
                    "bytes": package_path.read_bytes(),
                    "mode": package_path.stat().st_mode & 0o777,
                }
            )
            return {"ok": True, "kind": kind, "version": "sing-box 1.13.15", "sha256": sha256}

        with (
            mock.patch.object(app, "_validate_uploaded_package_name"),
            mock.patch.object(app, "_assert_kernel_upload_target_idle"),
            mock.patch.object(app, "install_uploaded_kernel", side_effect=fake_install),
            mock.patch.object(app, "log_event"),
        ):
            handler.do_POST()

        status, payload = response(handler)
        expected_sha256 = hashlib.sha256(body).hexdigest()
        self.assertEqual(status, 200)
        self.assertEqual(payload["sha256"], expected_sha256)
        self.assertEqual(captured["bytes"], body)
        self.assertEqual(captured["size"], len(body))
        self.assertEqual(captured["sha256"], expected_sha256)
        if os.name != "nt":
            self.assertEqual(captured["mode"], 0o600)
        self.assertEqual(list(app.RUNTIME_ROOT.glob("proxybridge-upload-*")), [])
        self.assertNotIn(body, handler.wfile.getvalue())

    def test_sha_mismatch_and_short_body_cleanup_staging_without_installing(self) -> None:
        cases = (
            ({app.CORE_UPLOAD_SHA256_HEADER: "0" * 64}, b"package", len(b"package"), "SHA-256"),
            ({"Content-Length": "8"}, b"short", 8, "不完整"),
        )
        for extra_headers, body, _declared_size, expected_error in cases:
            with self.subTest(expected_error=expected_error):
                handler = make_handler(body, headers=extra_headers)
                with (
                    mock.patch.object(app, "_validate_uploaded_package_name"),
                    mock.patch.object(app, "_assert_kernel_upload_target_idle"),
                    mock.patch.object(app, "install_uploaded_kernel") as install,
                    mock.patch.object(app, "log_event"),
                ):
                    handler.do_POST()

                status, payload = response(handler)
                self.assertEqual(status, 400)
                self.assertIn(expected_error, str(payload["error"]))
                install.assert_not_called()
                self.assertEqual(list(app.RUNTIME_ROOT.glob("proxybridge-upload-*")), [])

    def test_header_errors_and_size_limit_are_rejected_before_reading_body(self) -> None:
        cases = (
            ({"Content-Type": "application/json"}, 415),
            ({"Content-Encoding": "gzip"}, 415),
            ({"Transfer-Encoding": "chunked"}, 400),
            ({"Content-Length": "invalid"}, 400),
        )
        for extra_headers, expected_status in cases:
            with self.subTest(headers=extra_headers):
                handler = make_handler(b"package", headers=extra_headers)
                with mock.patch.object(app, "log_event"):
                    handler.do_POST()
                status, _payload = response(handler)
                self.assertEqual(status, expected_status)
                self.assertEqual(handler.rfile.read_calls, 0)

        missing_length = make_handler(b"package")
        del missing_length.headers["Content-Length"]
        with mock.patch.object(app, "log_event"):
            missing_length.do_POST()
        self.assertEqual(response(missing_length)[0], 411)
        self.assertEqual(missing_length.rfile.read_calls, 0)

        duplicate_length = make_handler(b"package")
        duplicate_length.headers["Content-Length"] = "7"
        with mock.patch.object(app, "log_event"):
            duplicate_length.do_POST()
        self.assertEqual(response(duplicate_length)[0], 400)
        self.assertEqual(duplicate_length.rfile.read_calls, 0)

        oversized = make_handler(b"package", headers={"Content-Length": str(app.CORE_UPLOAD_LIMIT + 1)})
        with mock.patch.object(app, "log_event"):
            oversized.do_POST()
        self.assertEqual(response(oversized)[0], 413)
        self.assertEqual(oversized.rfile.read_calls, 0)

    def test_authentication_and_origin_checks_happen_before_body_or_upload_headers(self) -> None:
        for authenticated, trusted in ((False, True), (True, False)):
            with self.subTest(authenticated=authenticated, trusted=trusted):
                handler = make_handler(
                    b"package",
                    authenticated=authenticated,
                    trusted=trusted,
                    headers={"Content-Length": str(app.CORE_UPLOAD_LIMIT + 1)},
                )
                handler.do_POST()
                status, _payload = response(handler)
                self.assertEqual(status, 403)
                self.assertEqual(handler.rfile.read_calls, 0)

    def test_parallel_upload_is_rejected_before_reading_and_stream_timeout_is_explicit(self) -> None:
        handler = make_handler(b"package")
        app.CORE_UPLOAD_LOCK.acquire()
        try:
            with (
                mock.patch.object(app, "_validate_uploaded_package_name"),
                mock.patch.object(app, "_assert_kernel_upload_target_idle"),
                mock.patch.object(app, "log_event"),
            ):
                handler.do_POST()
        finally:
            app.CORE_UPLOAD_LOCK.release()
        self.assertEqual(response(handler)[0], 409)
        self.assertEqual(handler.rfile.read_calls, 0)

        class TimedOutBody:
            def read(self, _size: int) -> bytes:
                raise TimeoutError("slow client")

        with self.assertRaisesRegex(RuntimeError, "上传读取超时"):
            app._stream_core_upload(TimedOutBody(), io.BytesIO(), 1)

    def test_active_target_checks_block_core_and_fingerprint_replacement(self) -> None:
        with mock.patch.object(
            app,
            "load_state",
            return_value={
                "bridges": [{"id": "bridge-1", "core": "sing-box", "status": "running"}],
                "browser_instances": [],
            },
        ):
            with self.assertRaisesRegex(RuntimeError, "停止正在使用 sing-box"):
                app._assert_kernel_upload_target_idle("sing-box")

        with mock.patch.object(
            app,
            "load_state",
            return_value={
                "bridges": [],
                "browser_instances": [{"id": "browser-1", "status": "starting"}],
            },
        ):
            with self.assertRaisesRegex(RuntimeError, "停止所有浏览器"):
                app._assert_kernel_upload_target_idle("fingerprint-chromium")

        running_test = mock.Mock()
        running_test.poll.return_value = None
        with (
            mock.patch.object(
                app,
                "load_state",
                return_value={"bridges": [], "browser_instances": []},
            ),
            mock.patch.dict(app.TEST_PROCESS_TABLE, {"test-1": running_test}, clear=True),
        ):
            with self.assertRaisesRegex(RuntimeError, "等待所有节点测速结束"):
                app._assert_kernel_upload_target_idle("xray")

        empty_state = {"bridges": [], "browser_instances": []}
        with (
            mock.patch.object(app, "load_state", return_value=empty_state),
            mock.patch.object(app, "remote_browser_feature_enabled", return_value=True),
            mock.patch.object(app, "remote_browser_sessions", return_value=(None, "broker offline")),
        ):
            with self.assertRaisesRegex(RuntimeError, "恢复 Browser Broker"):
                app._assert_kernel_upload_target_idle("fingerprint-chromium")

        with (
            mock.patch.object(app, "load_state", return_value=empty_state),
            mock.patch.object(app, "remote_browser_feature_enabled", return_value=True),
            mock.patch.object(
                app,
                "remote_browser_sessions",
                return_value=({"remote-1": {"status": "running"}}, ""),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "停止所有浏览器"):
                app._assert_kernel_upload_target_idle("fingerprint-chromium")

    def test_sing_box_and_xray_archives_use_atomic_shared_installer(self) -> None:
        app.ensure_dirs()
        sing_target = app.proxy_core_bin_dir() / ("sing-box.exe" if os.name == "nt" else "sing-box")
        sing_target.write_bytes(b"old-sing-box")
        sing_archive = Path(self.temp_dir.name) / "sing-box.zip"
        with zipfile.ZipFile(sing_archive, "w") as archive:
            archive.writestr(f"release/{app._core_binary_candidates('sing-box')[0]}", b"new-sing-box")

        with mock.patch.object(
            app,
            "_validate_uploaded_core_binary",
            return_value="sing-box version 1.13.15",
        ):
            installed = app._install_core_from_archive(
                "sing-box",
                sing_archive,
                "sing-box-1.13.15-linux-amd64-glibc.zip",
                "",
                validate_binary=True,
            )
        self.assertEqual(installed, str(sing_target))
        self.assertEqual(sing_target.read_bytes(), b"new-sing-box")
        self.assertIn(
            "sing-box version 1.13.15",
            (app.proxy_core_root() / "sing-box.version").read_text(encoding="utf-8"),
        )

        xray_archive = Path(self.temp_dir.name) / "Xray.zip"
        with zipfile.ZipFile(xray_archive, "w") as archive:
            archive.writestr(f"release/{app._core_binary_candidates('xray')[0]}", b"new-xray")
        with mock.patch.object(
            app,
            "_validate_uploaded_core_binary",
            return_value="Xray 25.1.1",
        ):
            installed = app._install_core_from_archive(
                "xray",
                xray_archive,
                "Xray-linux-64.zip",
                "",
                validate_binary=True,
            )
        self.assertTrue(Path(installed).is_file())
        xray_binary = app.proxy_core_bin_dir() / ("xray.exe" if os.name == "nt" else "xray-core")
        self.assertEqual(xray_binary.read_bytes(), b"new-xray")

    def test_failed_binary_validation_preserves_existing_core(self) -> None:
        app.ensure_dirs()
        target = app.proxy_core_bin_dir() / ("sing-box.exe" if os.name == "nt" else "sing-box")
        target.write_bytes(b"old-core")
        archive_path = Path(self.temp_dir.name) / "sing-box.zip"
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr(app._core_binary_candidates("sing-box")[0], b"wrong-platform")

        with mock.patch.object(
            app,
            "_validate_uploaded_core_binary",
            side_effect=RuntimeError("wrong platform"),
        ):
            with self.assertRaisesRegex(RuntimeError, "wrong platform"):
                app._install_core_from_archive(
                    "sing-box",
                    archive_path,
                    "sing-box.zip",
                    "",
                    validate_binary=True,
                )
        self.assertEqual(target.read_bytes(), b"old-core")

    def test_remote_fingerprint_package_requires_wrapper_before_replacement(self) -> None:
        destination = app.fingerprint_chromium_root()
        existing = destination / "chromium" / "chrome"
        existing.parent.mkdir(parents=True)
        existing.write_bytes(b"old-chrome")
        if os.name != "nt":
            existing.chmod(0o755)

        archive_path = Path(self.temp_dir.name) / "fingerprint-chromium_linux_x64.zip"
        info = zipfile.ZipInfo("bundle/chrome")
        info.external_attr = 0o755 << 16
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr(info, b"new-chrome")

        with self.assertRaisesRegex(RuntimeError, "chrome-wrapper"):
            app._install_fingerprint_from_archive(
                archive_path,
                destination,
                require_wrapper=True,
            )
        self.assertEqual(existing.read_bytes(), b"old-chrome")

    def test_archive_member_and_expanded_size_limits_are_checked_before_extraction(self) -> None:
        archive_path = Path(self.temp_dir.name) / "limited.zip"
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr("first", b"1234")
            archive.writestr("second", b"5678")
        destination = Path(self.temp_dir.name) / "extract"
        destination.mkdir()

        with mock.patch.object(app, "ARCHIVE_MEMBER_LIMIT", 1):
            with self.assertRaisesRegex(RuntimeError, "文件数量"):
                app._safe_extract_zip(archive_path, destination)
        self.assertEqual(list(destination.iterdir()), [])

        with mock.patch.object(app, "ARCHIVE_EXTRACTED_SIZE_LIMIT", 7):
            with self.assertRaisesRegex(RuntimeError, "体积"):
                app._safe_extract_zip(archive_path, destination)
        self.assertEqual(list(destination.iterdir()), [])

        corrupt_archive = Path(self.temp_dir.name) / "sing-box.tar.gz"
        corrupt_archive.write_bytes(b"not a compressed archive")
        with self.assertRaisesRegex(RuntimeError, "压缩包损坏"):
            app._extract_archive(corrupt_archive, destination)


if __name__ == "__main__":
    unittest.main()
