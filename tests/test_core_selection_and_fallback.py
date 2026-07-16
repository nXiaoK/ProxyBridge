from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app


class CoreSelectionAndFallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_work_root = app.WORK_ROOT
        self.old_runtime_root = app.RUNTIME_ROOT
        self.old_state_path = app.STATE_PATH
        self.temp_dir = tempfile.TemporaryDirectory()
        app.configure_work_root(Path(self.temp_dir.name) / "work")
        app.ensure_dirs()

    def tearDown(self) -> None:
        app.configure_work_root(self.old_work_root)
        app.RUNTIME_ROOT = self.old_runtime_root
        app.STATE_PATH = self.old_state_path
        self.temp_dir.cleanup()

    @staticmethod
    def node(**updates: object) -> dict[str, object]:
        node: dict[str, object] = {
            "id": "node-core",
            "name": "Core Node",
            "protocol": "vless",
            "server": "example.com",
            "port": 443,
            "region": "HK",
            "source": "test",
            "raw": "",
            "settings": {"uuid": "00000000-0000-0000-0000-000000000000", "sni": "example.com"},
            "tags": [],
            "transport": "ws",
            "security": "tls",
            "preferred_core": "sing-box",
            "fallback_enabled": False,
        }
        node.update(updates)
        return node

    def save_node(self, node: dict[str, object]) -> None:
        state = app.empty_state()
        state["nodes"] = [node]
        app.save_state(state)

    def test_core_compatibility_and_xray_config(self) -> None:
        dual = self.node()
        self.assertEqual(app.compatible_cores(dual), ["sing-box", "xray"])
        xray = app.xray_config(dual, 23080)
        outbound = xray["outbounds"][0]
        self.assertEqual(outbound["protocol"], "vless")
        self.assertEqual(outbound["streamSettings"]["network"], "ws")

        xhttp = self.node(transport="xhttp", preferred_core="xray")
        self.assertEqual(app.compatible_cores(xhttp), ["xray"])
        quic = self.node(protocol="hysteria2", transport="", security="tls")
        self.assertEqual(app.compatible_cores(quic), ["sing-box"])

    def test_node_core_settings_are_validated_and_persisted(self) -> None:
        self.save_node(self.node())
        result = app.update_node_core_settings(
            {"node_id": "node-core", "preferred_core": "xray", "fallback_enabled": True}
        )
        self.assertEqual(result["node"]["preferred_core"], "xray")
        self.assertTrue(result["node"]["fallback_enabled"])
        saved = app.load_state()["nodes"][0]
        self.assertEqual(saved["preferred_core"], "xray")

        self.save_node(self.node(protocol="hysteria2", transport="", security="tls"))
        with self.assertRaisesRegex(ValueError, "不兼容"):
            app.update_node_core_settings(
                {"node_id": "node-core", "preferred_core": "xray", "fallback_enabled": False}
            )

    def test_subscription_replacement_preserves_core_preference(self) -> None:
        state = app.empty_state()
        previous = self.node(preferred_core="xray", fallback_enabled=True)
        state["nodes"] = [previous]
        replacement = self.node(preferred_core="sing-box", fallback_enabled=False)

        app._apply_subscription_replacement(
            state,
            url="",
            subscription={"node_ids": ["node-core"]},
            node_dicts=[replacement],
        )

        self.assertEqual(state["nodes"][0]["preferred_core"], "xray")
        self.assertTrue(state["nodes"][0]["fallback_enabled"])

    def test_start_bridge_falls_back_after_primary_core_failure(self) -> None:
        self.save_node(self.node(fallback_enabled=True))
        attempts: list[str] = []

        def fake_prepare(plan: dict[str, object]) -> list[str]:
            return [str(plan["core"])]

        def fake_complete(plan: dict[str, object], command: list[str]):
            return dict(plan), command

        def fake_run(plan: dict[str, object], _command: list[str]) -> dict[str, object]:
            core = str(plan["core"])
            attempts.append(core)
            if core == "sing-box":
                raise app.CoreStartError("primary failed")
            return {"id": "bridge-xray", "core": core, "fallback_from": plan.get("fallback_from")}

        with (
            mock.patch.object(app, "_prepare_core_command", side_effect=fake_prepare),
            mock.patch.object(app, "_complete_bridge_plan", side_effect=fake_complete),
            mock.patch.object(app, "_run_prepared_bridge", side_effect=fake_run),
            mock.patch.object(app, "synchronize_process_state", return_value=None),
        ):
            result = app.start_bridge({"node_id": "node-core", "core": "auto"})

        self.assertEqual(attempts, ["sing-box", "xray"])
        self.assertEqual(result["core"], "xray")
        self.assertEqual(result["fallback_from"], "sing-box")


if __name__ == "__main__":
    unittest.main()
