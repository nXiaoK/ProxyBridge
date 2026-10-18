from __future__ import annotations

import base64
import hashlib
import json
import socket
import struct
import tempfile
import threading
import unittest
from pathlib import Path

import browser_fingerprint as bf


class FingerprintConfigTests(unittest.TestCase):
    def test_legacy_and_recommended_defaults_differ_only_in_recommended_fields(self) -> None:
        legacy = bf.legacy_fingerprint_config()
        recommended = bf.recommended_fingerprint_config()
        self.assertEqual(legacy["brand"], "")
        self.assertEqual(legacy["timezone_mode"], "region")
        self.assertEqual(legacy["geolocation"], "ask")
        self.assertEqual(recommended["brand"], "Chrome")
        self.assertEqual(recommended["timezone_mode"], "exit_ip")
        self.assertEqual(recommended["language_mode"], "exit_ip")
        self.assertEqual(recommended["geolocation"], "block")
        changed = {key for key in legacy if legacy[key] != recommended[key]}
        self.assertEqual(changed, {"brand", "timezone_mode", "language_mode", "geolocation"})

    def test_strict_patch_validation_merges_and_rejects_invalid_values(self) -> None:
        merged = bf.validate_fingerprint_patch(
            {"platform": "Windows", "brand": "edge", "disable_spoofing": ["gpu", "canvas", "gpu"], "window_size": "1920×1080"},
            base=bf.recommended_fingerprint_config(),
        )
        self.assertEqual(merged["platform"], "windows")
        self.assertEqual(merged["brand"], "Edge")
        self.assertEqual(merged["disable_spoofing"], ["canvas", "gpu"])
        self.assertEqual(merged["window_size"], "1920x1080")
        self.assertEqual(merged["timezone_mode"], "exit_ip")

        invalid = (
            {"platform": "android"},
            {"brand": "Firefox"},
            {"hardware_concurrency": 3},
            {"hardware_concurrency": True},
            {"disable_spoofing": ["webgl"]},
            {"timezone": "Not A Zone"},
            {"language": "english"},
            {"accept_language": "en-US;q=\"0.9\""},
            {"window_size": "100x100"},
            {"timezone_mode": "custom"},
            {"unknown": 1},
        )
        for patch in invalid:
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                bf.validate_fingerprint_patch(patch)

    def test_brand_version_is_cleared_without_brand(self) -> None:
        merged = bf.validate_fingerprint_patch({"brand": "", "brand_version": "150.0.1.2"})
        self.assertEqual(merged["brand_version"], "")

    def test_lenient_normalization_falls_back_per_field(self) -> None:
        normalized = bf.normalize_fingerprint_config(
            {"platform": "beos", "brand": "Chrome", "timezone_mode": "custom", "timezone": ""},
            bf.legacy_fingerprint_config(),
        )
        self.assertEqual(normalized["platform"], "auto")
        self.assertEqual(normalized["brand"], "Chrome")
        self.assertEqual(normalized["timezone_mode"], "region")

    def test_instance_option_normalizers(self) -> None:
        self.assertEqual(bf.normalize_group("  电商   店铺 "), "电商 店铺")
        self.assertEqual(
            bf.normalize_start_urls("https://a.test/\n\nhttps://a.test/\nhttp://b.test/x?y=1", strict=True),
            ["https://a.test/", "http://b.test/x?y=1"],
        )
        with self.assertRaises(ValueError):
            bf.normalize_start_urls(["javascript:alert(1)"], strict=True)
        with self.assertRaises(ValueError):
            bf.normalize_start_urls(["https://a.test/ x"], strict=True)
        self.assertEqual(bf.normalize_start_urls(["file:///etc/passwd", "https://ok.test/"], strict=False), ["https://ok.test/"])
        self.assertEqual(bf.normalize_extension_ids(["ABCDEF0123456789", "bad"], strict=False), ["abcdef0123456789"])
        with self.assertRaises(ValueError):
            bf.normalize_extension_ids(["bad"], strict=True)
        self.assertEqual(bf.normalize_kernel_id("150.0.7871.186", strict=True), "150.0.7871.186")
        with self.assertRaises(ValueError):
            bf.normalize_kernel_id("../escape", strict=True)


class LaunchSwitchTests(unittest.TestCase):
    def test_legacy_config_keeps_previous_switches(self) -> None:
        locale = {"timezone": "Asia/Tokyo", "language": "ja-JP", "accept_language": "ja-JP,ja;q=0.9"}
        switches = bf.fingerprint_switches(bf.legacy_fingerprint_config(), seed=42, host_platform="macos", locale=locale)
        self.assertEqual(
            switches,
            [
                "--fingerprint=42",
                "--fingerprint-platform=macos",
                "--timezone=Asia/Tokyo",
                "--lang=ja-JP",
                "--accept-lang=ja-JP,ja;q=0.9",
            ],
        )

    def test_full_config_emits_every_supported_switch(self) -> None:
        config = bf.validate_fingerprint_patch(
            {
                "platform": "windows",
                "platform_version": "10.0.0",
                "brand": "Chrome",
                "brand_version": "150.0.7871.186",
                "hardware_concurrency": 8,
                "disable_spoofing": ["audio", "font"],
                "window_size": "1600x900",
            }
        )
        locale = {"timezone": "UTC", "language": "en-US", "accept_language": "en-US,en;q=0.9"}
        switches = bf.fingerprint_switches(config, seed=7, host_platform="linux", locale=locale)
        for expected in (
            "--fingerprint-platform=windows",
            "--fingerprint-platform-version=10.0.0",
            "--fingerprint-brand=Chrome",
            "--fingerprint-brand-version=150.0.7871.186",
            "--fingerprint-hardware-concurrency=8",
            "--disable-spoofing=font,audio",
            "--window-size=1600,900",
        ):
            self.assertIn(expected, switches)

    def test_locale_resolution_sources(self) -> None:
        region = ("America/Los_Angeles", "en-US", "en-US,en;q=0.9")
        exit_locale = ("America/New_York", "en-US", "en-US,en;q=0.9")
        recommended = bf.recommended_fingerprint_config()
        resolved = bf.resolve_locale(recommended, region, exit_locale)
        self.assertEqual(resolved["timezone"], "America/New_York")
        self.assertEqual(resolved["timezone_source"], "exit_ip")
        fallback = bf.resolve_locale(recommended, region, None)
        self.assertEqual(fallback["timezone"], "America/Los_Angeles")
        self.assertEqual(fallback["timezone_source"], "region_fallback")
        custom = bf.validate_fingerprint_patch(
            {"timezone_mode": "custom", "timezone": "Europe/Paris", "language_mode": "custom", "language": "fr-FR"}
        )
        resolved_custom = bf.resolve_locale(custom, region, exit_locale)
        self.assertEqual(resolved_custom["timezone"], "Europe/Paris")
        self.assertEqual(resolved_custom["accept_language"], "fr-FR,fr;q=0.9,en;q=0.8")
        self.assertEqual(bf.default_accept_language("en-GB"), "en-GB,en;q=0.9")

    def test_geolocation_preference_block_and_ask(self) -> None:
        prefs: dict[str, object] = {"profile": {"default_content_setting_values": {"notifications": 2}}}
        bf.apply_geolocation_preference(prefs, "block")
        self.assertEqual(prefs["profile"]["default_content_setting_values"], {"notifications": 2, "geolocation": 2})
        bf.apply_geolocation_preference(prefs, "ask")
        self.assertEqual(prefs["profile"]["default_content_setting_values"], {"notifications": 2})

    def test_version_extraction_from_package_names(self) -> None:
        self.assertEqual(bf.fingerprint_version_from_name("ungoogled-chromium_150.0.7871.186-1_linux.tar.xz"), "150.0.7871.186")
        self.assertEqual(bf.fingerprint_version_from_name("v148.0.7778.215"), "148.0.7778.215")
        self.assertEqual(bf.fingerprint_version_from_name("fingerprint-chromium_macos.dmg"), "")
        self.assertGreater(bf.version_sort_key("150.0.1.2"), bf.version_sort_key("148.9.9.9"))


class CookieFormatTests(unittest.TestCase):
    def test_json_import_handles_host_only_domain_and_same_site(self) -> None:
        cookies = bf.parse_cookie_text(
            json.dumps(
                [
                    {"domain": "example.com", "hostOnly": True, "name": "sid", "value": "1", "path": "/", "secure": True, "httpOnly": True, "sameSite": "lax", "expirationDate": 1999999999},
                    {"domain": ".example.com", "name": "pref", "value": "x", "sameSite": "no_restriction", "secure": False, "session": True},
                ]
            )
        )
        self.assertEqual(cookies[0]["url"], "https://example.com/")
        self.assertNotIn("domain", cookies[0])
        self.assertEqual(cookies[0]["sameSite"], "Lax")
        self.assertEqual(cookies[0]["expires"], 1999999999)
        self.assertEqual(cookies[1]["domain"], ".example.com")
        # 非 secure 的 SameSite=None 会被 Chromium 拒绝，因此降级为默认策略。
        self.assertNotIn("sameSite", cookies[1])
        self.assertNotIn("expires", cookies[1])

    def test_netscape_import_and_round_trip(self) -> None:
        text = "# Netscape HTTP Cookie File\n#HttpOnly_.example.com\tTRUE\t/\tTRUE\t1999999999\tsid\tabc\nexample.org\tFALSE\t/app\tFALSE\t0\tk\tv\tw\n"
        cookies = bf.parse_cookie_text(text)
        self.assertEqual(cookies[0]["domain"], ".example.com")
        self.assertTrue(cookies[0]["httpOnly"])
        self.assertEqual(cookies[1]["url"], "http://example.org/app")
        self.assertEqual(cookies[1]["value"], "v\tw")
        exported = [
            {"name": "sid", "value": "abc", "domain": ".example.com", "path": "/", "expires": 1999999999, "httpOnly": True, "secure": True, "session": False, "sameSite": "Lax"},
            {"name": "k", "value": "v", "domain": "example.org", "path": "/", "expires": -1, "httpOnly": False, "secure": False, "session": True},
        ]
        as_json = json.loads(bf.format_cookies_json(exported))
        self.assertEqual(as_json[0]["sameSite"], "lax")
        self.assertFalse(as_json[0]["hostOnly"])
        self.assertTrue(as_json[1]["hostOnly"])
        self.assertNotIn("expirationDate", as_json[1])
        self.assertEqual(len(bf.parse_cookie_text(json.dumps(as_json))), 2)
        netscape = bf.format_cookies_netscape(exported)
        self.assertIn("#HttpOnly_.example.com\tTRUE\t/\tTRUE\t1999999999\tsid\tabc", netscape)
        self.assertEqual(len(bf.parse_cookie_text(netscape)), 2)

    def test_invalid_cookie_payloads_raise(self) -> None:
        for content in ("", "{}", "[1]", '[{"name": "", "domain": "a"}]', "a\tb\n"):
            with self.subTest(content=content), self.assertRaises(ValueError):
                bf.parse_cookie_text(content)


class ExtensionPackageTests(unittest.TestCase):
    def test_crx_offsets(self) -> None:
        self.assertEqual(bf.crx_zip_offset(b"PK\x03\x04" + b"\x00" * 12), 0)
        self.assertEqual(bf.crx_zip_offset(b"Cr24" + struct.pack("<II", 3, 100) + b"\x00" * 4), 112)
        self.assertEqual(bf.crx_zip_offset(b"Cr24" + struct.pack("<III", 2, 10, 20) + b"\x00" * 4), 46)
        with self.assertRaises(ValueError):
            bf.crx_zip_offset(b"Cr24" + struct.pack("<II", 9, 0) + b"\x00" * 4)

    def test_manifest_resolves_localized_name(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "manifest.json").write_text(json.dumps({"manifest_version": 3, "name": "__MSG_appName__", "version": "1.2", "default_locale": "en"}), encoding="utf-8")
            (root / "_locales" / "en").mkdir(parents=True)
            (root / "_locales" / "en" / "messages.json").write_text(json.dumps({"appName": {"message": "Demo"}}), encoding="utf-8")
            self.assertEqual(bf.read_extension_manifest(root)["name"], "Demo")
            (root / "manifest.json").write_text(json.dumps({"manifest_version": 1, "name": "x", "version": "1"}), encoding="utf-8")
            with self.assertRaises(ValueError):
                bf.read_extension_manifest(root)


class FakeDevToolsServer:
    """最小 WebSocket 服务端：校验握手，按方法名返回预设结果并先推送一条事件。"""

    def __init__(self, responses: dict[str, object]) -> None:
        self.responses = responses
        self.calls: list[dict[str, object]] = []
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(4)
        self.port = self.server.getsockname()[1]
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self) -> None:
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            with conn:
                request = b""
                while b"\r\n\r\n" not in request:
                    request += conn.recv(4096)
                key = next(
                    line.split(":", 1)[1].strip()
                    for line in request.decode().split("\r\n")
                    if line.lower().startswith("sec-websocket-key")
                )
                accept = base64.b64encode(hashlib.sha1((key + bf._WEBSOCKET_GUID).encode()).digest()).decode()
                conn.sendall(f"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n".encode())
                while True:
                    try:
                        _fin, opcode, payload = bf._read_frame(conn)
                    except bf.CdpError:
                        break
                    if opcode == 0x8:
                        break
                    message = json.loads(payload)
                    self.calls.append(message)
                    response = self.responses.get(message["method"], {})
                    conn.sendall(self.frame({"method": "Target.targetCreated", "params": {}}))
                    if isinstance(response, Exception):
                        body = {"id": message["id"], "error": {"message": str(response)}}
                    elif callable(response):
                        body = response(message)
                    else:
                        body = {"id": message["id"], "result": response}
                    conn.sendall(self.frame(body))

    @staticmethod
    def frame(body: dict[str, object]) -> bytes:
        payload = json.dumps(body).encode()
        header = bytearray([0x81])
        if len(payload) < 126:
            header.append(len(payload))
        else:
            header.append(126)
            header.extend(struct.pack(">H", len(payload)))
        return bytes(header) + payload

    def close(self) -> None:
        self.server.close()


class CdpClientTests(unittest.TestCase):
    def test_session_ignores_events_and_returns_matching_result(self) -> None:
        server = FakeDevToolsServer({"Storage.getCookies": {"cookies": [{"name": "a"}]}})
        try:
            result = bf.cdp_call(server.port, "/devtools/browser/abcdef12-3456", "Storage.getCookies", timeout=5)
        finally:
            server.close()
        self.assertEqual(result, {"cookies": [{"name": "a"}]})
        self.assertEqual(server.calls[0]["method"], "Storage.getCookies")

    def test_set_cookies_retries_individually_after_batch_rejection(self) -> None:
        def handler(message: dict[str, object]) -> dict[str, object]:
            cookies = message["params"]["cookies"]
            if len(cookies) > 1 or cookies[0]["name"] == "bad":
                return {"id": message["id"], "error": {"message": "Invalid cookie fields"}}
            return {"id": message["id"], "result": {}}

        server = FakeDevToolsServer({"Storage.setCookies": handler})
        try:
            with bf.CdpSession(server.port, "/devtools/browser/abcdef12-3456", timeout=5) as session:
                imported, failures = bf.set_cookies(
                    session,
                    [{"name": "good", "domain": ".a.test"}, {"name": "bad", "domain": ".a.test"}],
                )
        finally:
            server.close()
        self.assertEqual(imported, 1)
        self.assertEqual(len(failures), 1)
        self.assertIn("bad", failures[0])

    def test_rejects_non_devtools_paths_and_reads_active_port_file(self) -> None:
        with self.assertRaises(bf.CdpError):
            bf.CdpSession(9222, "/json/version")
        with tempfile.TemporaryDirectory() as temp:
            profile = Path(temp)
            self.assertIsNone(bf.read_devtools_active_port(profile))
            (profile / bf.DEVTOOLS_ACTIVE_PORT_FILE).write_text("45678\n/devtools/browser/abcdef12-3456\n", encoding="utf-8")
            self.assertEqual(bf.read_devtools_active_port(profile), (45678, "/devtools/browser/abcdef12-3456"))
            (profile / bf.DEVTOOLS_ACTIVE_PORT_FILE).write_text("80\n/devtools/browser/abcdef12-3456\n", encoding="utf-8")
            self.assertIsNone(bf.read_devtools_active_port(profile))


if __name__ == "__main__":
    unittest.main()
