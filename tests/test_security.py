import io
import json
import tarfile
import tempfile
import unittest
from email.message import Message
from pathlib import Path

import app


def make_handler(
    method: str,
    path: str,
    headers: dict[str, str],
    body: bytes = b"",
) -> app.AppHandler:
    handler = object.__new__(app.AppHandler)
    handler.command = method
    handler.path = path
    handler.requestline = f"{method} {path} HTTP/1.1"
    handler.request_version = "HTTP/1.1"
    handler.headers = Message()
    for name, value in headers.items():
        handler.headers[name] = value
    handler.rfile = io.BytesIO(body)
    handler.wfile = io.BytesIO()
    return handler


def response_parts(handler: app.AppHandler) -> tuple[int, Message, bytes]:
    raw_headers, body = handler.wfile.getvalue().split(b"\r\n\r\n", 1)
    status_line, *header_lines = raw_headers.split(b"\r\n")
    status = int(status_line.split(b" ", 2)[1])
    headers = Message()
    for line in header_lines:
        name, value = line.decode("iso-8859-1").split(":", 1)
        headers[name] = value.strip()
    return status, headers, body


class SecurityTests(unittest.TestCase):
    def test_tar_rejects_symlink_pivot(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            archive_path = root / "pivot.tar"
            destination = root / "destination"
            outside = root / "outside"
            destination.mkdir()
            outside.mkdir()

            payload = b"escaped"
            with tarfile.open(archive_path, "w") as archive:
                pivot = tarfile.TarInfo("pivot")
                pivot.type = tarfile.SYMTYPE
                pivot.linkname = "../outside"
                archive.addfile(pivot)

                escaped = tarfile.TarInfo("pivot/escaped.txt")
                escaped.size = len(payload)
                archive.addfile(escaped, io.BytesIO(payload))

            with self.assertRaises(RuntimeError):
                app._safe_extract_tar(archive_path, destination)
            self.assertFalse((outside / "escaped.txt").exists())

    def test_tar_allows_regular_files_and_directories(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            archive_path = root / "regular.tar"
            destination = root / "destination"
            destination.mkdir()

            payload = b"safe"
            with tarfile.open(archive_path, "w") as archive:
                directory = tarfile.TarInfo("nested")
                directory.type = tarfile.DIRTYPE
                directory.mode = 0o755
                archive.addfile(directory)

                regular = tarfile.TarInfo("nested/file.txt")
                regular.size = len(payload)
                regular.mode = 0o644
                archive.addfile(regular, io.BytesIO(payload))

            app._safe_extract_tar(archive_path, destination)

            self.assertEqual((destination / "nested" / "file.txt").read_bytes(), payload)

    def test_tar_rejects_hard_links_and_special_files(self):
        member_types = (
            tarfile.LNKTYPE,
            tarfile.FIFOTYPE,
            tarfile.CHRTYPE,
            tarfile.BLKTYPE,
        )
        for member_type in member_types:
            with self.subTest(member_type=member_type), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                archive_path = root / "special.tar"
                destination = root / "destination"
                destination.mkdir()
                with tarfile.open(archive_path, "w") as archive:
                    target = tarfile.TarInfo("target")
                    target.size = 1
                    target.mode = 0o644
                    archive.addfile(target, io.BytesIO(b"x"))

                    special = tarfile.TarInfo("special")
                    special.type = member_type
                    special.mode = 0o644
                    special.linkname = "target"
                    special.devmajor = 1
                    special.devminor = 3
                    archive.addfile(special)

                with self.assertRaisesRegex(RuntimeError, "非法类型"):
                    app._safe_extract_tar(archive_path, destination)

    def test_loopback_host_parser_accepts_supported_hosts_only(self):
        for value in ("127.0.0.1", "127.0.0.1:8787", "localhost:8787", "[::1]:8787"):
            with self.subTest(value=value):
                self.assertTrue(app._is_loopback_host(value))
        for value in ("", "evil.example", "127.0.0.1.evil.example", "evil@127.0.0.1", "[::2]:8787"):
            with self.subTest(value=value):
                self.assertFalse(app._is_loopback_host(value))

    def test_handler_accepts_loopback_host_and_local_origins(self):
        for headers in (
            {"Host": "127.0.0.1:8787"},
            {"Host": "localhost:8787", "Origin": "http://localhost:8787"},
            {"Host": "[::1]:8787", "Origin": "https://[::1]:8787"},
        ):
            with self.subTest(headers=headers):
                handler = make_handler("GET", "/", headers)
                self.assertTrue(handler.is_trusted_request())

    def test_handler_rejects_remote_host_origin_and_cross_site_fetch(self):
        for headers in (
            {"Host": "evil.example"},
            {"Host": "127.0.0.1:8787", "Origin": "https://evil.example"},
            {"Host": "127.0.0.1:8787", "Sec-Fetch-Site": "cross-site"},
            {},
        ):
            with self.subTest(headers=headers):
                handler = make_handler("GET", "/", headers)
                self.assertFalse(handler.is_trusted_request())

    def test_read_json_rejects_non_json_and_oversized_body(self):
        body = b'{"ok": true}'
        valid = make_handler(
            "POST",
            "/api/unknown",
            {
                "Host": "127.0.0.1:8787",
                "Content-Type": "application/json; charset=utf-8",
                "Content-Length": str(len(body)),
            },
            body,
        )
        self.assertEqual(valid.read_json(), {"ok": True})

        oversized = make_handler(
            "POST",
            "/api/unknown",
            {
                "Host": "127.0.0.1:8787",
                "Content-Type": "application/json",
                "Content-Length": str(app.API_JSON_BODY_LIMIT + 1),
            },
            b"{}",
        )
        with self.assertRaises(ValueError):
            oversized.read_json()
        self.assertEqual(oversized.rfile.tell(), 0)

        non_json = make_handler(
            "POST",
            "/api/unknown",
            {
                "Host": "127.0.0.1:8787",
                "Content-Type": "text/plain",
                "Content-Length": "2",
            },
            b"{}",
        )
        non_json.do_POST()
        status, headers, response_body = response_parts(non_json)
        self.assertEqual(status, 415)
        self.assertEqual(headers.get_content_type(), "application/json")
        self.assertIn("error", json.loads(response_body))

    def test_untrusted_requests_are_rejected_before_dispatch(self):
        cases = (
            ("GET", "/api/system/logs", "", 403, "application/json"),
            ("GET", "/", "", 403, "text/html"),
            ("HEAD", "/api/status", "", 403, "application/json"),
            ("POST", "/api/unknown", "{}", 403, "application/json"),
        )
        for method, path, body_text, expected_status, expected_type in cases:
            with self.subTest(method=method, path=path):
                body = body_text.encode("utf-8")
                handler = make_handler(
                    method,
                    path,
                    {
                        "Host": "evil.example",
                        "Content-Type": "application/json",
                        "Content-Length": str(len(body)),
                    },
                    body,
                )
                getattr(handler, f"do_{method}")()

                status, headers, response_body = response_parts(handler)
                self.assertEqual(status, expected_status)
                self.assertEqual(headers.get_content_type(), expected_type)
                if method == "HEAD":
                    self.assertEqual(response_body, b"")
                elif path.startswith("/api/"):
                    self.assertIn("error", json.loads(response_body))

    def test_invalid_content_length_returns_json_error(self):
        for content_length in ("-1", "invalid"):
            with self.subTest(content_length=content_length):
                handler = make_handler(
                    "POST",
                    "/api/unknown",
                    {
                        "Host": "127.0.0.1:8787",
                        "Content-Type": "application/json",
                        "Content-Length": content_length,
                    },
                    b"{}",
                )
                handler.do_POST()

                status, headers, response_body = response_parts(handler)
                self.assertEqual(status, 400)
                self.assertEqual(headers.get_content_type(), "application/json")
                self.assertIn("error", json.loads(response_body))


if __name__ == "__main__":
    unittest.main()
