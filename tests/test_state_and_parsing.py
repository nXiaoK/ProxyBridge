import io
import json
import os
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from unittest import mock

import app


class StateAndParsingTests(unittest.TestCase):
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
        with app.REQUEST_LOG_PENDING_LOCK:
            app.REQUEST_LOG_PENDING_UPDATES.clear()

    def tearDown(self):
        app.WORK_ROOT = self.old_work_root
        app.RUNTIME_ROOT = self.old_runtime_root
        app.STATE_PATH = self.old_state_path
        with app.REQUEST_LOG_PENDING_LOCK:
            app.REQUEST_LOG_PENDING_UPDATES.clear()
        self.temp_dir.cleanup()

    def test_refresh_replaces_nodes_removed_from_same_url(self):
        app.import_subscription(
            {"name": "same", "url": "https://example.test/sub", "raw": "vless://a@example.com:443#A"}
        )
        app.import_subscription(
            {"name": "same", "url": "https://example.test/sub", "raw": "vless://b@example.net:443#B"}
        )

        self.assertEqual([node["name"] for node in app.load_state()["nodes"]], ["B"])

    def test_refresh_stops_removed_node_runtime_and_removes_profile(self):
        original = app.import_subscription(
            {"name": "same", "url": "https://example.test/sub", "raw": "vless://a@example.com:443#A"}
        )
        node_id = original["nodes"][0]["id"]
        instance_id = "browser-refresh-cleanup"
        bridge_id = "bridge-refresh-cleanup"
        profile_root = app.browser_node_profile_root(node_id)
        profile_root.mkdir(parents=True, exist_ok=True)
        (profile_root / "marker").write_text("profile", encoding="utf-8")
        state = app.load_state()
        state["browser_instances"] = [
            {
                "id": instance_id,
                "node_id": node_id,
                "status": "running",
                "profile_path": str(profile_root),
                "legacy": True,
            }
        ]
        state["bridges"] = [
            {
                "id": bridge_id,
                "node_id": node_id,
                "status": "running",
                "enabled": True,
                "port": 21080,
                "upload_bytes": 3,
                "download_bytes": 4,
            }
        ]
        app.save_state(state)

        class FakeProcess:
            pid = 9876

            def poll(self):
                return None

        process = FakeProcess()
        old_process_table = dict(app.PROCESS_TABLE)
        old_bridge_node_table = dict(app.BRIDGE_NODE_TABLE)
        old_browser_table = {key: list(value) for key, value in app.BROWSER_TABLE.items()}
        old_browser_process_table = dict(app.BROWSER_PROCESS_TABLE)
        old_traffic_table = dict(app.TRAFFIC_TABLE)
        killed: list[int] = []
        try:
            with app.PROCESS_LOCK:
                app.PROCESS_TABLE[bridge_id] = process
                app.BRIDGE_NODE_TABLE[bridge_id] = node_id
                app.BROWSER_PROCESS_TABLE[instance_id] = process
                app.BROWSER_TABLE[node_id] = [process]
            app.init_bridge_traffic(bridge_id, 3, 4)
            with mock.patch.object(app, "terminate_process", side_effect=lambda item: killed.append(item.pid)):
                app.import_subscription(
                    {"name": "same", "url": "https://example.test/sub", "raw": "vless://b@example.net:443#B"}
                )
        finally:
            with app.PROCESS_LOCK:
                app.PROCESS_TABLE.clear()
                app.PROCESS_TABLE.update(old_process_table)
                app.BRIDGE_NODE_TABLE.clear()
                app.BRIDGE_NODE_TABLE.update(old_bridge_node_table)
                app.BROWSER_TABLE.clear()
                app.BROWSER_TABLE.update(old_browser_table)
                app.BROWSER_PROCESS_TABLE.clear()
                app.BROWSER_PROCESS_TABLE.update(old_browser_process_table)
            with app.TRAFFIC_LOCK:
                app.TRAFFIC_TABLE.clear()
                app.TRAFFIC_TABLE.update(old_traffic_table)

        saved = app.load_state()
        self.assertIn(process.pid, killed)
        self.assertEqual([node["name"] for node in saved["nodes"]], ["B"])
        self.assertEqual(saved["browser_instances"], [])
        self.assertEqual(saved["bridges"][0]["status"], "stopped")
        self.assertFalse(saved["bridges"][0]["enabled"])
        self.assertFalse(profile_root.exists())
        self.assertNotIn(bridge_id, app.TRAFFIC_TABLE)

    def test_refresh_rejects_zero_nodes_without_replacing_existing_state(self):
        original = app.import_subscription(
            {"name": "same", "url": "https://example.test/sub", "raw": "vless://a@example.com:443#A"}
        )

        with self.assertRaisesRegex(ValueError, "没有可用节点"):
            app.import_subscription(
                {"name": "same", "url": "https://example.test/sub", "raw": "temporarily unavailable"}
            )

        state = app.load_state()
        self.assertEqual([item["id"] for item in state["subscriptions"]], [original["subscription"]["id"]])
        self.assertEqual([node["name"] for node in state["nodes"]], ["A"])

    def test_clash_refresh_reports_missing_yaml_parser_without_state_loss(self):
        original = app.import_subscription(
            {"name": "same", "url": "https://example.test/sub", "raw": "vless://a@example.com:443#A"}
        )

        with mock.patch.object(app, "import_yaml", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "PyYAML"):
                app.import_subscription(
                    {
                        "name": "same",
                        "url": "https://example.test/sub",
                        "raw": "proxies:\n  - name: B\n    type: vless\n    server: example.net\n    port: 443\n",
                    }
                )

        state = app.load_state()
        self.assertEqual([item["id"] for item in state["subscriptions"]], [original["subscription"]["id"]])
        self.assertEqual([node["name"] for node in state["nodes"]], ["A"])

    def test_delete_preserves_node_referenced_by_another_subscription(self):
        shared_link = "vless://shared@example.com:443#Shared"
        first = app.import_subscription(
            {"name": "one", "url": "https://one.test/sub", "raw": shared_link}
        )
        app.import_subscription({"name": "two", "url": "https://two.test/sub", "raw": shared_link})

        app.delete_subscription(first["subscription"]["id"])

        state = app.load_state()
        self.assertEqual(len(state["subscriptions"]), 1)
        self.assertEqual(len(state["nodes"]), 1)

    def test_manual_imports_receive_distinct_ids_with_same_second(self):
        old_now = app.now_ts
        try:
            app.now_ts = lambda: 42
            first = app.import_subscription({"name": "manual", "raw": "vless://a@example.com:443#A"})
            second = app.import_subscription({"name": "manual", "raw": "vless://b@example.com:443#B"})
            self.assertNotEqual(first["subscription"]["id"], second["subscription"]["id"])
        finally:
            app.now_ts = old_now

    def test_load_state_normalizes_valid_json_with_invalid_shapes(self):
        payloads = ([], None, {"bridges": None}, {"nodes": [None, "bad"], "tests": []})
        for payload in payloads:
            with self.subTest(payload=payload):
                app.STATE_PATH.write_text(json.dumps(payload), encoding="utf-8")

                state = app.load_state()

                self.assertIsInstance(state, dict)
                self.assertIsInstance(state["bridges"], list)
                self.assertIsInstance(state["nodes"], list)
                self.assertTrue(all(isinstance(node, dict) for node in state["nodes"]))
                self.assertIsInstance(state["tests"], dict)

    def test_load_state_rejects_corrupt_primary_without_backup(self):
        app.STATE_PATH.write_text("{not-json", encoding="utf-8")

        with self.assertRaisesRegex(RuntimeError, "拒绝回落为空状态"):
            app.load_state()

        self.assertEqual(app.STATE_PATH.read_text(encoding="utf-8"), "{not-json")

    def test_load_state_recovers_valid_backup_and_status_preserves_data(self):
        state = app.empty_state()
        state["nodes"] = [app.asdict(app.parse_link("vless://a@example.com:443#A"))]
        app.save_state(state)
        state["settings"] = {"marker": "newer"}
        app.save_state(state)
        backup_path = app.state_backup_path()
        self.assertTrue(backup_path.exists())

        app.STATE_PATH.write_text("{not-json", encoding="utf-8")
        payload = app.status_payload()

        self.assertEqual([node["name"] for node in payload["nodes"]], ["A"])
        recovered = json.loads(app.STATE_PATH.read_text(encoding="utf-8"))
        self.assertEqual([node["name"] for node in recovered["nodes"]], ["A"])
        self.assertEqual(app.STATE_PATH.stat().st_mode & 0o777, 0o600)
        self.assertEqual(backup_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(app.WORK_ROOT.stat().st_mode & 0o777, 0o700)

    def test_fetch_subscription_rejects_non_http_and_oversized_response(self):
        with self.assertRaisesRegex(ValueError, "http\(s\)"):
            app.fetch_subscription("file:///tmp/subscription.txt")

        class FakeResponse(io.BytesIO):
            def __init__(self, payload: bytes) -> None:
                super().__init__(payload)
                self.headers = Message()

            def geturl(self) -> str:
                return "https://example.test/sub"

        response = FakeResponse(b"123456789")
        with (
            mock.patch.object(app, "SUBSCRIPTION_RESPONSE_LIMIT", 8),
            mock.patch.object(app.urllib.request, "urlopen", return_value=response),
        ):
            with self.assertRaisesRegex(ValueError, "超过 8 字节"):
                app.fetch_subscription("https://example.test/sub")

    def test_connected_request_log_update_is_coalesced_until_final_state(self):
        original_save = app.save_state
        save_calls = 0

        def counting_save(state: dict) -> None:
            nonlocal save_calls
            save_calls += 1
            original_save(state)

        with mock.patch.object(app, "save_state", side_effect=counting_save):
            app.append_request_log({"id": "log-1", "status": "connecting"})
            self.assertEqual(save_calls, 1)
            app.update_request_log("log-1", {"status": "connected"})
            self.assertEqual(save_calls, 1)
            self.assertEqual(app.load_state()["request_logs"][0]["status"], "connected")
            app.update_request_log("log-1", {"status": "closed", "download_bytes": 7})

        self.assertEqual(save_calls, 2)
        self.assertEqual(app.load_state()["request_logs"][0]["status"], "closed")
        with app.REQUEST_LOG_PENDING_LOCK:
            self.assertEqual(app.REQUEST_LOG_PENDING_UPDATES, {})

    def test_late_connected_update_cannot_resurrect_terminal_request_log(self):
        app.append_request_log({"id": "log-late", "status": "connecting"})
        app.update_request_log("log-late", {"status": "closed", "download_bytes": 9})

        app.update_request_log("log-late", {"status": "connected"})

        entry = app.load_state()["request_logs"][0]
        self.assertEqual(entry["status"], "closed")
        self.assertEqual(entry["download_bytes"], 9)
        with app.REQUEST_LOG_PENDING_LOCK:
            self.assertNotIn("log-late", app.REQUEST_LOG_PENDING_UPDATES)

    def test_stale_pending_update_does_not_override_persisted_terminal_state(self):
        app.append_request_log({"id": "log-stale", "status": "closed"})
        with app.REQUEST_LOG_PENDING_LOCK:
            app.REQUEST_LOG_PENDING_UPDATES["log-stale"] = {"status": "connected"}

        entry = app.load_state()["request_logs"][0]

        self.assertEqual(entry["status"], "closed")

    def test_vless_grpc_service_name_reaches_sing_box_config(self):
        node = app.parse_link("vless://id@example.com:443?type=grpc&security=tls&serviceName=orders#grpc")

        config = app.sing_box_config(app.asdict(node), 21080)

        self.assertEqual(config["outbounds"][0]["transport"]["service_name"], "orders")

    def test_clash_grpc_service_name_reaches_sing_box_config(self):
        node = app.clash_proxy_to_node(
            {
                "name": "grpc",
                "type": "vless",
                "server": "example.com",
                "port": 443,
                "uuid": "id",
                "network": "grpc",
                "tls": True,
                "grpc-opts": {"grpc-service-name": "orders"},
            },
            "demo",
        )

        config = app.sing_box_config(app.asdict(node), 21080)

        self.assertEqual(config["outbounds"][0]["transport"]["service_name"], "orders")


if __name__ == "__main__":
    unittest.main()
