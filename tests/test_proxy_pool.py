from __future__ import annotations

import io
import json
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from email.message import Message
from pathlib import Path
from unittest import mock

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
    if app.BACKEND_INSTANCE_HEADER not in handler.headers:
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


class ProxyPoolTests(unittest.TestCase):
    def setUp(self):
        self.old_work_root = app.WORK_ROOT
        self.old_runtime_root = app.RUNTIME_ROOT
        self.old_state_path = app.STATE_PATH
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        app.WORK_ROOT = root / "work"
        app.RUNTIME_ROOT = app.WORK_ROOT / "runtime"
        app.STATE_PATH = app.WORK_ROOT / "state.json"
        app.ensure_dirs()
        with app.SYSTEM_LOG_LOCK:
            app.SYSTEM_LOGS.clear()
        with app.PROCESS_LOCK:
            app.PROCESS_TABLE.clear()
            app.LOG_PROXY_TABLE.clear()
            app.BRIDGE_NODE_TABLE.clear()
        with app.POOL_RR_LOCK:
            app.POOL_RR_COUNTER = 0
        app.SHUTTING_DOWN.clear()

    def tearDown(self):
        app.WORK_ROOT = self.old_work_root
        app.RUNTIME_ROOT = self.old_runtime_root
        app.STATE_PATH = self.old_state_path
        with app.PROCESS_LOCK:
            app.PROCESS_TABLE.clear()
            app.LOG_PROXY_TABLE.clear()
            app.BRIDGE_NODE_TABLE.clear()
        with app.SYSTEM_LOG_LOCK:
            app.SYSTEM_LOGS.clear()
        self.temp_dir.cleanup()

    def _bridge(
        self,
        *,
        bridge_id: str = "b1",
        name: str = "香港 A",
        region: str = "香港",
        port: int = 21080,
        status: str = "running",
        enabled: bool = True,
        protocol: str = "vless",
    ) -> dict:
        return {
            "id": bridge_id,
            "node_id": f"node-{bridge_id}",
            "node_name": name,
            "region": region,
            "protocol": protocol,
            "port": port,
            "socks_url": f"socks5://127.0.0.1:{port}",
            "http_url": f"http://127.0.0.1:{port}",
            "supported_proxy_protocols": ["socks5", "http", "https-connect"],
            "status": status,
            "enabled": enabled,
        }

    def _save_bridges(self, bridges: list[dict], token: str | None = None) -> None:
        state = app.empty_state()
        state["bridges"] = bridges
        if token is not None:
            state["settings"] = {"pool_token": token}
        app.save_state(state)

    def _mark_runtime_active(self, bridge_id: str) -> None:
        process = mock.Mock()
        process.poll.return_value = None
        process.pid = 12345
        proxy = mock.Mock()
        proxy.is_alive.return_value = True
        with app.PROCESS_LOCK:
            app.PROCESS_TABLE[bridge_id] = process
            app.LOG_PROXY_TABLE[bridge_id] = proxy

    def test_is_pool_member_list_rules(self):
        self.assertTrue(app.is_pool_member(self._bridge(status="running")))
        self.assertTrue(app.is_pool_member(self._bridge(status="starting")))
        self.assertFalse(app.is_pool_member(self._bridge(status="failed", enabled=True)))
        self.assertFalse(app.is_pool_member(self._bridge(status="stopped")))
        self.assertFalse(app.is_pool_member(self._bridge(status="running", port=0)))
        self.assertFalse(app.is_pool_member(self._bridge(status="running", port=-1)))
        # enabled alone never grants membership
        self.assertFalse(
            app.is_pool_member(self._bridge(status="failed", enabled=True, port=21080))
        )

    def test_is_pick_ready_requires_running_and_runtime(self):
        starting = self._bridge(status="starting")
        self.assertFalse(app.is_pick_ready_member(starting))

        running = self._bridge(status="running", bridge_id="ready-1")
        self.assertFalse(app.is_pick_ready_member(running))
        self._mark_runtime_active("ready-1")
        self.assertTrue(app.is_pick_ready_member(running))

    def test_pool_members_filter_and_sort(self):
        bridges = [
            self._bridge(bridge_id="b2", name="日本 B", region="日本", port=21082),
            self._bridge(bridge_id="b1", name="香港 A", region="香港", port=21080),
            self._bridge(bridge_id="b3", name="香港 C", region="香港", port=21081, status="starting"),
            self._bridge(bridge_id="b4", name="香港 D", region="香港", port=21083, status="failed"),
        ]
        state = {"bridges": bridges}
        members = app.pool_members(state)
        # Stable sort by node_name, then port, then id (Unicode order: 日本 < 香港).
        self.assertEqual([m["id"] for m in members], ["b2", "b1", "b3"])

        region_members = app.pool_members(state, region="香港")
        self.assertEqual([m["id"] for m in region_members], ["b1", "b3"])

        q_members = app.pool_members(state, q="日本")
        self.assertEqual([m["id"] for m in q_members], ["b2"])

    def test_protocol_output_helpers(self):
        member = app.public_pool_member(self._bridge())
        self.assertEqual(app.parse_protocol_param(None), None)
        self.assertEqual(app.parse_protocol_param("HTTP"), "http")
        with self.assertRaises(ValueError):
            app.parse_protocol_param("vless")

        self.assertEqual(app.select_proxy_url(member, None), member["socks_url"])
        self.assertEqual(app.select_proxy_url(member, "http"), member["http_url"])

        text = app.render_proxies_txt([member], None)
        self.assertEqual(
            text,
            f"{member['socks_url']}\n{member['http_url']}\n",
        )
        self.assertEqual(
            app.render_proxies_txt([member], "socks5"),
            f"{member['socks_url']}\n",
        )

    def test_ensure_pool_token_generates_and_keeps_valid(self):
        state = app.empty_state()
        token, changed = app.ensure_pool_token(state)
        self.assertTrue(changed)
        self.assertGreaterEqual(len(token), app.POOL_TOKEN_MIN_LEN)
        self.assertIsNotNone(app.POOL_TOKEN_PATTERN.fullmatch(token))

        again, changed_again = app.ensure_pool_token(state)
        self.assertFalse(changed_again)
        self.assertEqual(again, token)

        state["settings"]["pool_token"] = "short"
        reset, changed_reset = app.ensure_pool_token(state)
        self.assertTrue(changed_reset)
        self.assertNotEqual(reset, "short")

    def test_pick_round_robin_cycles(self):
        members = [
            self._bridge(bridge_id="a", name="A", port=1),
            self._bridge(bridge_id="b", name="B", port=2),
            self._bridge(bridge_id="c", name="C", port=3),
        ]
        seen = [app.pick_round_robin(members)["id"] for _ in range(6)]
        self.assertEqual(seen, ["a", "b", "c", "a", "b", "c"])

    def test_redact_pool_token_in_requestline_and_path(self):
        token = "AbCdEfGhIjKlMnOpQrStUvWx"
        requestline = f"GET /pool/{token}/list.json HTTP/1.1"
        bare = f"/pool/{token}/proxies.txt"
        self.assertNotIn(token, app._redact_pool_token_in_log_arg(requestline))
        self.assertIn("/pool/***/list.json", app._redact_pool_token_in_log_arg(requestline))
        self.assertEqual(app._redact_pool_token_in_log_arg(bare), "/pool/***/proxies.txt")
        # Console admin path must not be mangled as if it were a consumer token path.
        admin = "/api/pool/token/rotate"
        self.assertEqual(app._redact_pool_token_in_log_arg(admin), admin)

    def test_log_message_does_not_emit_raw_token(self):
        token = "AbCdEfGhIjKlMnOpQrStUvWx"
        handler = make_handler("GET", f"/pool/{token}/list.json", {"Host": "127.0.0.1:8787"})
        buf = io.StringIO()
        with redirect_stdout(buf):
            handler.log_message('%s - - "%s" %s %s', "127.0.0.1", f"GET /pool/{token}/list.json HTTP/1.1", "200", "-")
        output = buf.getvalue()
        self.assertNotIn(token, output)
        self.assertIn("/pool/***/list.json", output)

    def test_handler_list_json_ok_without_origin(self):
        token = "valid-pool-token-001"
        self._save_bridges([self._bridge()], token=token)
        handler = make_handler(
            "GET",
            f"/pool/{token}/list.json",
            {"Host": "127.0.0.1:8787"},
        )
        with mock.patch.object(app, "synchronize_process_state", return_value=None):
            handler.do_GET()
        status, headers, body = response_parts(handler)
        self.assertEqual(status, 200)
        payload = json.loads(body.decode("utf-8"))
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["proxies"][0]["socks_url"], "socks5://127.0.0.1:21080")
        self.assertEqual(headers.get_content_type(), "application/json")

    def test_pool_response_write_does_not_hold_lifecycle_or_state_lock(self):
        token = "valid-pool-token-001"
        self._save_bridges([self._bridge()], token=token)
        handler = make_handler(
            "GET",
            f"/pool/{token}/list.json",
            {"Host": "127.0.0.1:8787"},
        )
        write_started = threading.Event()
        allow_write = threading.Event()

        class BlockingWriter:
            def write(self, data: bytes) -> int:
                write_started.set()
                if not allow_write.wait(timeout=2):
                    raise RuntimeError("test writer timed out")
                return len(data)

            def flush(self) -> None:
                return None

        handler.wfile = BlockingWriter()
        errors: list[BaseException] = []

        def serve() -> None:
            try:
                handler.do_GET()
            except BaseException as error:
                errors.append(error)

        with mock.patch.object(app, "synchronize_process_state", return_value=None):
            thread = threading.Thread(target=serve)
            thread.start()
            self.assertTrue(write_started.wait(timeout=2))
            state_available = app.STATE_LOCK.acquire(blocking=False)
            if state_available:
                app.STATE_LOCK.release()
            shutdown_available = app.SHUTDOWN_LOCK.acquire(blocking=False)
            if shutdown_available:
                app.SHUTDOWN_LOCK.release()
            allow_write.set()
            thread.join(timeout=2)

        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(state_available)
        self.assertTrue(shutdown_available)

    def test_handler_wrong_token_is_quiet_403(self):
        token = "valid-pool-token-001"
        self._save_bridges([self._bridge()], token=token)
        with app.SYSTEM_LOG_LOCK:
            app.SYSTEM_LOGS.clear()
        before = len(app.system_log_entries())
        handler = make_handler(
            "GET",
            "/pool/wrong-token-value-xx/list.json",
            {"Host": "127.0.0.1:8787"},
        )
        with mock.patch.object(app, "synchronize_process_state", return_value=None):
            handler.do_GET()
        status, _headers, body = response_parts(handler)
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body.decode("utf-8"))["error"], "无效的代理池凭证")
        self.assertEqual(len(app.system_log_entries()), before)

    def test_handler_non_loopback_host_forbidden(self):
        token = "valid-pool-token-001"
        self._save_bridges([self._bridge()], token=token)
        handler = make_handler(
            "GET",
            f"/pool/{token}/list.json",
            {"Host": "example.com"},
        )
        handler.do_GET()
        status, _headers, body = response_parts(handler)
        self.assertEqual(status, 403)
        self.assertIn("无效", json.loads(body.decode("utf-8"))["error"])

    def test_handler_malformed_paths(self):
        cases = [
            ("/pool/", 404),
            ("/pool", 404),
            ("/pool/only-token", 404),
            ("/pool/valid-pool-token-001/", 404),
            ("/pool/valid-pool-token-001/unknown", 404),
            ("/pool//list.json", 403),
        ]
        for path, expected in cases:
            with self.subTest(path=path):
                handler = make_handler("GET", path, {"Host": "127.0.0.1:8787"})
                with mock.patch.object(app, "synchronize_process_state", return_value=None):
                    handler.do_GET()
                status, _headers, _body = response_parts(handler)
                self.assertEqual(status, expected)

    def test_handler_proxies_txt_and_empty_get(self):
        token = "valid-pool-token-001"
        self._save_bridges([], token=token)
        list_handler = make_handler(
            "GET",
            f"/pool/{token}/proxies.txt",
            {"Host": "127.0.0.1:8787"},
        )
        with mock.patch.object(app, "synchronize_process_state", return_value=None):
            list_handler.do_GET()
        status, headers, body = response_parts(list_handler)
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")
        self.assertIn("text/plain", headers.get_content_type())

        get_handler = make_handler(
            "GET",
            f"/pool/{token}/get?strategy=random",
            {"Host": "127.0.0.1:8787"},
        )
        with mock.patch.object(app, "synchronize_process_state", return_value=None):
            get_handler.do_GET()
        status, _headers, body = response_parts(get_handler)
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body.decode("utf-8"))["error"], "代理池为空")

    def test_handler_get_ready_only_and_include_starting(self):
        token = "valid-pool-token-001"
        self._save_bridges(
            [self._bridge(bridge_id="start-1", status="starting", port=21090)],
            token=token,
        )
        # starting is not pick-ready by default
        handler = make_handler(
            "GET",
            f"/pool/{token}/get",
            {"Host": "127.0.0.1:8787"},
        )
        with mock.patch.object(app, "synchronize_process_state", return_value=None):
            handler.do_GET()
        status, _headers, _body = response_parts(handler)
        self.assertEqual(status, 404)

        include_handler = make_handler(
            "GET",
            f"/pool/{token}/get?include_starting=1&protocol=http",
            {"Host": "127.0.0.1:8787"},
        )
        with mock.patch.object(app, "synchronize_process_state", return_value=None):
            include_handler.do_GET()
        status, _headers, body = response_parts(include_handler)
        self.assertEqual(status, 200)
        payload = json.loads(body.decode("utf-8"))
        self.assertEqual(payload["protocol"], "http")
        self.assertEqual(payload["picked_url"], "http://127.0.0.1:21090")

    def test_handler_get_pick_ready_running(self):
        token = "valid-pool-token-001"
        self._save_bridges([self._bridge(bridge_id="run-1", status="running")], token=token)
        self._mark_runtime_active("run-1")
        handler = make_handler(
            "GET",
            f"/pool/{token}/get?strategy=random&format=text&protocol=socks5",
            {"Host": "127.0.0.1:8787"},
        )
        with mock.patch.object(app, "synchronize_process_state", return_value=None):
            handler.do_GET()
        status, _headers, body = response_parts(handler)
        self.assertEqual(status, 200)
        self.assertEqual(body.decode("utf-8"), "socks5://127.0.0.1:21080\n")

    def test_handler_head_pool_ok(self):
        token = "valid-pool-token-001"
        self._save_bridges([self._bridge()], token=token)
        handler = make_handler(
            "HEAD",
            f"/pool/{token}/list.json",
            {"Host": "127.0.0.1:8787"},
        )
        with mock.patch.object(app, "synchronize_process_state", return_value=None):
            handler.do_HEAD()
        status, headers, body = response_parts(handler)
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")
        self.assertGreater(int(headers["Content-Length"]), 0)

    def test_api_pool_requires_trusted(self):
        handler = make_handler(
            "GET",
            "/api/pool",
            {"Host": "127.0.0.1:8787", "Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"},
        )
        handler.do_GET()
        status, _headers, _body = response_parts(handler)
        self.assertEqual(status, 403)

    def test_api_pool_and_rotate(self):
        token = "valid-pool-token-001"
        self._save_bridges([self._bridge()], token=token)
        handler = make_handler("GET", "/api/pool", {"Host": "127.0.0.1:8787"})
        with mock.patch.object(app, "synchronize_process_state", return_value=None):
            handler.do_GET()
        status, _headers, body = response_parts(handler)
        self.assertEqual(status, 200)
        payload = json.loads(body.decode("utf-8"))
        self.assertEqual(payload["member_count"], 1)
        self.assertIn("members", payload)
        self.assertEqual(len(payload["members"]), 1)
        self.assertIn(token, payload["links"]["list_json"])

        rotate = make_handler(
            "POST",
            "/api/pool/token/rotate",
            {
                "Host": "127.0.0.1:8787",
                "Content-Type": "application/json",
                "Content-Length": "2",
            },
            body=b"{}",
        )
        with mock.patch.object(app, "synchronize_process_state", return_value=None):
            rotate.do_POST()
        status, _headers, body = response_parts(rotate)
        self.assertEqual(status, 200)
        rotated = json.loads(body.decode("utf-8"))
        self.assertTrue(rotated["ok"])
        new_token = rotated["base_url"].rsplit("/", 1)[-1]
        self.assertNotEqual(new_token, token)

        old_list = make_handler(
            "GET",
            f"/pool/{token}/list.json",
            {"Host": "127.0.0.1:8787"},
        )
        with mock.patch.object(app, "synchronize_process_state", return_value=None):
            old_list.do_GET()
        self.assertEqual(response_parts(old_list)[0], 403)

        new_list = make_handler(
            "GET",
            f"/pool/{new_token}/list.json",
            {"Host": "127.0.0.1:8787"},
        )
        with mock.patch.object(app, "synchronize_process_state", return_value=None):
            new_list.do_GET()
        self.assertEqual(response_parts(new_list)[0], 200)

    def test_status_payload_includes_pool(self):
        token = "valid-pool-token-001"
        self._save_bridges([self._bridge()], token=token)
        with mock.patch.object(app, "synchronize_process_state", return_value=None):
            with mock.patch.object(app, "core_paths", return_value={}):
                with mock.patch.object(app, "chrome_path", return_value=None):
                    with mock.patch.object(app, "fingerprint_chromium_path", return_value=None):
                        payload = app.status_payload()
        self.assertIn("pool", payload)
        self.assertEqual(payload["pool"]["member_count"], 1)
        self.assertIn(token, payload["pool"]["links"]["list_json"])

    def test_status_endpoint_serializes_first_fingerprint_app_discovery(self):
        app_bundle = app.WORK_ROOT / "Fingerprint Chromium.app"
        executable = app_bundle / "Contents" / "MacOS" / "Chromium"
        executable.parent.mkdir(parents=True)
        executable.write_bytes(b"binary")
        executable.chmod(0o755)
        with (
            mock.patch.object(app, "synchronize_process_state", return_value=None),
            mock.patch.object(app, "core_paths", return_value={}),
            mock.patch.object(app, "chrome_path", return_value=None),
            mock.patch.object(
                app,
                "_resolve_fingerprint_chromium_path",
                side_effect=lambda: app._resolve_fingerprint_executable_in_root(app_bundle),
            ),
        ):
            app.invalidate_fingerprint_chromium_cache()
            for _request_number in range(2):
                handler = make_handler(
                    "GET",
                    "/api/status",
                    {"Host": "127.0.0.1:8787"},
                )
                handler.do_GET()
                status, _headers, body = response_parts(handler)
                payload = json.loads(body)
                self.assertEqual(status, 200)
                self.assertEqual(payload["fingerprint_chromium"], str(executable))
                self.assertIsInstance(payload["fingerprint_chromium"], str)

    def test_invalid_protocol_returns_400(self):
        token = "valid-pool-token-001"
        self._save_bridges([self._bridge()], token=token)
        handler = make_handler(
            "GET",
            f"/pool/{token}/get?protocol=vless",
            {"Host": "127.0.0.1:8787"},
        )
        with mock.patch.object(app, "synchronize_process_state", return_value=None):
            handler.do_GET()
        status, _headers, body = response_parts(handler)
        self.assertEqual(status, 400)
        self.assertIn("protocol", json.loads(body.decode("utf-8"))["error"])


if __name__ == "__main__":
    unittest.main()
