from __future__ import annotations

import base64
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import app


class LanSecurityAndSmartPoolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_work_root = app.WORK_ROOT
        self.old_runtime_root = app.RUNTIME_ROOT
        self.old_state_path = app.STATE_PATH
        self.temp_dir = tempfile.TemporaryDirectory()
        app.configure_work_root(Path(self.temp_dir.name) / "work")
        app.ensure_dirs()
        with app.PROXY_CLIENT_LOCK:
            app.ACTIVE_PROXY_CLIENTS.clear()

    def tearDown(self) -> None:
        with app.PROXY_CLIENT_LOCK:
            clients = list(app.ACTIVE_PROXY_CLIENTS.values())
            app.ACTIVE_PROXY_CLIENTS.clear()
        for item in clients:
            try:
                item["socket"].close()
            except (KeyError, OSError):
                pass
        app.configure_work_root(self.old_work_root)
        app.RUNTIME_ROOT = self.old_runtime_root
        app.STATE_PATH = self.old_state_path
        self.temp_dir.cleanup()

    @staticmethod
    def proxy(security: dict) -> app.Socks5LogProxy:
        return app.Socks5LogProxy(
            bridge_id="bridge-secure",
            node_id="node-secure",
            node_name="Secure Node",
            listen_port=1,
            upstream_port=2,
            security=security,
        )

    @staticmethod
    def bridge(bridge_id: str, node_id: str, health: dict | None = None) -> dict:
        return {
            "id": bridge_id,
            "node_id": node_id,
            "node_name": bridge_id,
            "status": "running",
            "port": 21080,
            "socks_url": f"socks5://127.0.0.1:21080",
            "http_url": f"http://127.0.0.1:21080",
            "pool_health": health or {},
        }

    def test_lan_security_validation_and_public_payload(self) -> None:
        security = app.validate_lan_security(
            {
                "auth_mode": "basic",
                "username": "alice",
                "password": "secret",
                "allowed_cidrs": ["192.168.1.25", "10.0.0.0/24"],
                "max_connections": 12,
            }
        )
        public = app.public_lan_security(security)

        self.assertEqual(security["allowed_cidrs"], ["192.168.1.25/32", "10.0.0.0/24"])
        self.assertTrue(app.proxy_client_allowed("192.168.1.25", security["allowed_cidrs"]))
        self.assertFalse(app.proxy_client_allowed("192.168.1.26", security["allowed_cidrs"]))
        self.assertNotIn("password", public)
        self.assertNotIn("token", public)
        self.assertTrue(public["password_configured"])

        token_security = app.validate_lan_security(
            {"auth_mode": "token", "token": "0123456789abcdef"}
        )
        token_public = app.public_lan_security(token_security)
        self.assertTrue(token_public["token_configured"])
        self.assertNotIn("token", token_public)
        with self.assertRaisesRegex(ValueError, "Token不能为空"):
            app.validate_lan_security({"auth_mode": "token"})
        retained = app.validate_lan_security(
            {"auth_mode": "token", "token": ""},
            token_security,
        )
        self.assertEqual(retained["token"], "0123456789abcdef")

    def test_socks5_rfc1929_and_http_authentication(self) -> None:
        security = {
            "auth_mode": "basic",
            "username": "alice",
            "password": "secret",
            "allowed_cidrs": ["192.168.0.0/16"],
            "max_connections": 5,
        }
        proxy = self.proxy(security)
        server, client = socket.socketpair()
        try:
            client.sendall(b"\x01\x05alice\x06secret")
            self.assertTrue(proxy._authenticate_socks5(server))
            self.assertEqual(app.read_exact(client, 2), b"\x01\x00")

            token = base64.b64encode(b"alice:secret").decode("ascii")
            request = proxy._parse_http_request(
                f"CONNECT example.com:443 HTTP/1.1\r\nProxy-Authorization: Basic {token}\r\n\r\n".encode("ascii")
            )
            self.assertTrue(proxy._http_authenticated(request))
            request["header_map"]["proxy-authorization"] = "Basic bad"
            self.assertFalse(proxy._http_authenticated(request))
        finally:
            server.close()
            client.close()

        token_proxy = self.proxy({**security, "auth_mode": "token", "token": "0123456789abcdef"})
        bearer = token_proxy._parse_http_request(
            b"CONNECT example.com:443 HTTP/1.1\r\nProxy-Authorization: Bearer 0123456789abcdef\r\n\r\n"
        )
        self.assertTrue(token_proxy._http_authenticated(bearer))

    def test_active_client_payload_and_kick(self) -> None:
        proxy = self.proxy(app.normalize_lan_security({}))
        server, client = socket.socketpair()
        try:
            client_id = app.register_active_proxy_client(
                proxy=proxy,
                client=server,
                address=("192.168.1.20", 50000),
                is_lan=True,
                max_connections=1,
            )
            self.assertIsNotNone(client_id)
            rejected = app.register_active_proxy_client(
                proxy=proxy,
                client=mock.Mock(),
                address=("192.168.1.21", 50001),
                is_lan=True,
                max_connections=1,
            )
            self.assertIsNone(rejected)
            payload = app.active_proxy_clients_payload()
            self.assertEqual(payload["lan_count"], 1)
            self.assertNotIn("socket", payload["clients"][0])

            app.kick_proxy_client(str(client_id))
            client.settimeout(1)
            self.assertEqual(client.recv(1), b"")
            self.assertEqual(app.active_proxy_clients_payload()["count"], 0)
        finally:
            server.close()
            client.close()

    def test_health_strategy_circuit_recovery_and_exclusion(self) -> None:
        healthy = self.bridge("healthy", "node-healthy")
        slower = self.bridge("slower", "node-slower")
        members = [
            app.public_pool_member(healthy, {"latency_ms": 35, "success_rate": 1.0}),
            app.public_pool_member(slower, {"latency_ms": 180, "success_rate": 0.75}),
        ]
        self.assertEqual(app.pick_pool_member(members, "health")["id"], "healthy")
        self.assertEqual(app.pick_pool_member(members, "latency")["id"], "healthy")

        health: dict = {}
        for index in range(3):
            health = app.update_pool_health_record(health, ok=False, error="timeout", current_time=100 + index)
        bridge = self.bridge("circuit", "node-circuit", health)
        opened = app.pool_member_availability(bridge, current_time=102)
        self.assertEqual(opened["circuit_state"], "open")
        self.assertFalse(opened["available"])

        half_open = app.pool_member_availability(bridge, current_time=health["circuit_open_until"])
        self.assertEqual(half_open["circuit_state"], "half_open")
        leased = app.normalize_pool_health(health)
        leased["half_open_probe_until"] = health["circuit_open_until"] + 30
        leased_bridge = self.bridge("leased", "node-leased", leased)
        availability = app.pool_member_availability(
            leased_bridge,
            current_time=health["circuit_open_until"],
        )
        self.assertFalse(availability["available"])
        self.assertTrue(availability["probe_in_progress"])
        recovered = app.update_pool_health_record(
            health,
            ok=True,
            latency_ms=50,
            current_time=health["circuit_open_until"],
        )
        self.assertEqual(recovered["consecutive_failures"], 0)
        self.assertEqual(recovered["circuit_open_until"], 0)
        self.assertEqual(recovered["half_open_probe_until"], 0)

        state = app.empty_state()
        state["bridges"] = [healthy]
        app.save_state(state)
        with mock.patch.object(app, "now_ts", return_value=500):
            result = app.set_pool_member_exclusion({"id": "healthy", "duration_seconds": 300})
        self.assertEqual(result["member"]["circuit_state"], "excluded")
        self.assertEqual(app.load_state()["bridges"][0]["pool_health"]["manual_excluded_until"], 800)


if __name__ == "__main__":
    unittest.main()
