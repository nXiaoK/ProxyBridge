import json
import tempfile
import unittest
from pathlib import Path

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

    def tearDown(self):
        app.WORK_ROOT = self.old_work_root
        app.RUNTIME_ROOT = self.old_runtime_root
        app.STATE_PATH = self.old_state_path
        self.temp_dir.cleanup()

    def test_refresh_replaces_nodes_removed_from_same_url(self):
        app.import_subscription(
            {"name": "same", "url": "https://example.test/sub", "raw": "vless://a@example.com:443#A"}
        )
        app.import_subscription(
            {"name": "same", "url": "https://example.test/sub", "raw": "vless://b@example.net:443#B"}
        )

        self.assertEqual([node["name"] for node in app.load_state()["nodes"]], ["B"])

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
