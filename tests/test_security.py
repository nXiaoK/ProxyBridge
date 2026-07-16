import io
import json
import os
import tarfile
import tempfile
import unittest
import urllib.parse
from email.message import Message
from pathlib import Path
from unittest import mock

import app


def make_handler(
    method: str,
    path: str,
    headers: dict[str, str],
    body: bytes = b"",
    *,
    authenticated: bool = True,
) -> app.AppHandler:
    handler = object.__new__(app.AppHandler)
    handler.command = method
    handler.path = path
    handler.requestline = f"{method} {path} HTTP/1.1"
    handler.request_version = "HTTP/1.1"
    handler.headers = Message()
    for name, value in headers.items():
        handler.headers[name] = value
    if authenticated and app.BACKEND_INSTANCE_HEADER not in handler.headers:
        handler.headers[app.BACKEND_INSTANCE_HEADER] = app.management_api_token()
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
    def test_console_access_url_places_token_only_in_fragment(self):
        token = "01234567-89AB-CDEF-0123-456789ABCDEF"
        access_url = app.console_access_url("http://127.0.0.1:8787/", token)
        parsed = urllib.parse.urlsplit(access_url)

        self.assertEqual(parsed.path, "/")
        self.assertEqual(parsed.query, "")
        self.assertEqual(urllib.parse.parse_qs(parsed.fragment), {"token": [token]})

    def test_json_response_echoes_valid_backend_instance_token(self):
        token = "01234567-89AB-CDEF-0123-456789ABCDEF"
        with mock.patch.dict(
            os.environ,
            {"PROXY_BRIDGE_INSTANCE_TOKEN": token},
        ):
            handler = make_handler(
                "GET",
                "/api/status",
                {"Host": "127.0.0.1:8787", app.BACKEND_INSTANCE_HEADER: token},
            )
            app.json_response(handler, {"ok": True})

        _status, headers, _body = response_parts(handler)
        self.assertEqual(
            headers["X-ProxyBridge-Instance"],
            token,
        )

    def test_json_response_never_discloses_token_to_unauthenticated_requests(self):
        invalid_tokens = (
            "",
            "contains space",
            "bad\r\nInjected: yes",
            "x" * 129,
            "令牌",
        )
        for token in invalid_tokens:
            with self.subTest(token=token):
                with mock.patch.dict(
                    os.environ,
                    {"PROXY_BRIDGE_INSTANCE_TOKEN": token},
                ):
                    handler = make_handler(
                        "GET",
                        "/api/status",
                        {"Host": "127.0.0.1:8787", app.BACKEND_INSTANCE_HEADER: token},
                        authenticated=False,
                    )
                    app.json_response(handler, {"ok": True})

                _status, headers, _body = response_parts(handler)
                self.assertIsNone(headers["X-ProxyBridge-Instance"])

    def test_management_api_rejects_missing_wrong_and_duplicate_tokens(self):
        cases = []
        missing = make_handler(
            "GET",
            "/api/status",
            {"Host": "127.0.0.1:8787"},
            authenticated=False,
        )
        cases.append(missing)
        wrong = make_handler(
            "GET",
            "/api/status",
            {"Host": "127.0.0.1:8787", app.BACKEND_INSTANCE_HEADER: "wrong-token"},
            authenticated=False,
        )
        cases.append(wrong)
        duplicate = make_handler(
            "GET",
            "/api/status",
            {"Host": "127.0.0.1:8787", app.BACKEND_INSTANCE_HEADER: app.management_api_token()},
            authenticated=False,
        )
        duplicate.headers[app.BACKEND_INSTANCE_HEADER] = app.management_api_token()
        cases.append(duplicate)

        for handler in cases:
            with self.subTest(values=handler.headers.get_all(app.BACKEND_INSTANCE_HEADER, [])):
                handler.do_GET()
                status, headers, body = response_parts(handler)
                self.assertEqual(status, 403)
                self.assertIsNone(headers[app.BACKEND_INSTANCE_HEADER])
                self.assertIn("凭证无效", json.loads(body)["error"])

    def test_health_head_and_static_assets_do_not_require_or_disclose_token(self):
        health = make_handler(
            "HEAD",
            "/api/status",
            {"Host": "127.0.0.1:8787"},
            authenticated=False,
        )
        health.do_HEAD()
        status, headers, body = response_parts(health)
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")
        self.assertIsNone(headers[app.BACKEND_INSTANCE_HEADER])

        static = make_handler("GET", "/", {"Host": "127.0.0.1:8787"}, authenticated=False)
        static.do_GET()
        status, headers, body = response_parts(static)
        self.assertEqual(status, 200)
        self.assertEqual(headers.get_content_type(), "text/html")
        self.assertNotEqual(body, b"")

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
        for value in (
            "",
            "evil.example",
            "127.0.0.1.evil.example",
            "evil@127.0.0.1",
            "[::2]:8787",
            "[::1].evil",
            "[::1]evil:80",
        ):
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

    def test_handler_accepts_only_configured_docker_lan_ip(self):
        with mock.patch.dict(
            os.environ,
            {
                "PROXY_BRIDGE_DOCKER": "1",
                "PROXY_BRIDGE_CONSOLE_HOST": "192.168.1.20",
            },
            clear=False,
        ):
            for headers in (
                {"Host": "192.168.1.20:8787"},
                {
                    "Host": "192.168.1.20:8787",
                    "Origin": "http://192.168.1.20:8787",
                },
            ):
                with self.subTest(headers=headers):
                    handler = make_handler("GET", "/", headers)
                    self.assertTrue(handler.is_trusted_request())

            for headers in (
                {"Host": "192.168.1.21:8787"},
                {"Host": "8.8.8.8:8787"},
                {"Host": "nas.local:8787"},
                {
                    "Host": "192.168.1.20:8787",
                    "Origin": "http://192.168.1.21:8787",
                },
                {
                    "Host": "192.168.1.20:8787",
                    "Origin": "http://127.0.0.1:8787",
                },
                {
                    "Host": "192.168.1.20:8787",
                    "Sec-Fetch-Site": "cross-site",
                },
            ):
                with self.subTest(headers=headers):
                    handler = make_handler("GET", "/", headers)
                    self.assertFalse(handler.is_trusted_request())

    def test_handler_ignores_console_host_outside_docker_or_private_ipv4(self):
        for environment in (
            {"PROXY_BRIDGE_DOCKER": "0", "PROXY_BRIDGE_CONSOLE_HOST": "192.168.1.20"},
            {"PROXY_BRIDGE_DOCKER": "1", "PROXY_BRIDGE_CONSOLE_HOST": "0.0.0.0"},
            {"PROXY_BRIDGE_DOCKER": "1", "PROXY_BRIDGE_CONSOLE_HOST": "8.8.8.8"},
            {"PROXY_BRIDGE_DOCKER": "1", "PROXY_BRIDGE_CONSOLE_HOST": "nas.local"},
        ):
            with self.subTest(environment=environment), mock.patch.dict(
                os.environ, environment, clear=False
            ):
                handler = make_handler("GET", "/", {"Host": "192.168.1.20:8787"})
                self.assertFalse(handler.is_trusted_request())

    def test_handler_rejects_remote_host_origin_and_cross_site_fetch(self):
        for headers in (
            {"Host": "evil.example"},
            {"Host": "127.0.0.1:8787", "Origin": "https://evil.example"},
            {"Host": "127.0.0.1:8787", "Origin": "http://[::1].evil"},
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

    def test_runtime_config_endpoint_never_returns_proxy_credentials(self):
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(app, "RUNTIME_ROOT", Path(temp_dir)):
            (Path(temp_dir) / "bridge-secret.json").write_text(
                json.dumps({"password": "top-secret", "uuid": "private-id"}),
                encoding="utf-8",
            )
            handler = make_handler(
                "GET",
                "/api/config/bridge-secret",
                {"Host": "127.0.0.1:8787"},
            )

            handler.do_GET()

        status, _headers, body = response_parts(handler)
        self.assertEqual(status, 404)
        self.assertNotIn(b"top-secret", body)
        self.assertNotIn(b"private-id", body)

    def test_api_head_error_declares_unsent_json_entity_length(self):
        handler = make_handler("HEAD", "/api/status", {"Host": "evil.example"})

        handler.do_HEAD()

        status, headers, response_body = response_parts(handler)
        expected_body = json.dumps({"error": "不可信请求来源"}, ensure_ascii=False).encode("utf-8")
        self.assertEqual(status, 403)
        self.assertEqual(headers.get_content_type(), "application/json")
        self.assertEqual(int(headers["Content-Length"]), len(expected_body))
        self.assertEqual(response_body, b"")

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
