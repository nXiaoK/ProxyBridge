import base64
import json
import os
import socket
import tempfile
import threading
import time
import unittest
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


if __name__ == "__main__":
    unittest.main()
