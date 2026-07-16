from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app
import webdav_sync


class DockerModeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_work = app.WORK_ROOT
        self.old_engine = app.SYNC_ENGINE
        self.temp = tempfile.TemporaryDirectory()
        app.configure_work_root(Path(self.temp.name) / "work")
        app.SYNC_ENGINE = None

    def tearDown(self) -> None:
        app.configure_work_root(self.old_work)
        app.SYNC_ENGINE = self.old_engine
        self.temp.cleanup()

    def docker_env(self, **extra: str):
        values = {
            "PROXY_BRIDGE_DOCKER": "1",
            "PROXY_BRIDGE_PROXY_PORT_START": "21080",
            "PROXY_BRIDGE_PROXY_PORT_END": "21179",
            **extra,
        }
        return mock.patch.dict(os.environ, values, clear=False)

    def test_compose_binds_console_and_proxy_ports_to_loopback(self) -> None:
        compose_path = Path(__file__).resolve().parents[1] / "compose.yaml"
        compose_text = compose_path.read_text(encoding="utf-8")

        self.assertIn('PROXY_BRIDGE_CONSOLE_HOST: "${PROXY_BRIDGE_CONSOLE_HOST:-127.0.0.1}"', compose_text)
        self.assertIn('- "${PROXY_BRIDGE_CONSOLE_HOST:-127.0.0.1}:8787:8787"', compose_text)
        self.assertIn('- "${PROXY_BRIDGE_PROXY_HOST:-127.0.0.1}:21080-21179:21080-21179"', compose_text)
        self.assertNotIn('\n      - "8787:8787"', compose_text)
        self.assertNotIn('\n      - "21080-21179:21080-21179"', compose_text)

    def test_docker_mode_disables_browser_and_skips_browser_directories(self) -> None:
        with self.docker_env():
            self.assertTrue(app.docker_mode())
            self.assertFalse(app.browser_feature_enabled())
            with self.assertRaisesRegex(RuntimeError, "不提供浏览器功能"):
                app.launch_browser({"node_id": "n1"})
            app.ensure_dirs()

        self.assertTrue(app.WORK_ROOT.exists())
        self.assertTrue(app.proxy_core_bin_dir().exists())
        self.assertFalse(app.browser_profiles_root().exists())
        self.assertFalse(app.browser_extensions_root().exists())

    def test_docker_mode_uses_published_proxy_range(self) -> None:
        with self.docker_env(
            PROXY_BRIDGE_PROXY_PORT_START="21080",
            PROXY_BRIDGE_PROXY_PORT_END="21082",
        ):
            with mock.patch.object(app, "is_port_free", side_effect=lambda port, *_args: port == 21082):
                self.assertEqual(app.next_free_public_proxy_port(None, app.LAN_PROXY_LISTEN_HOST), 21082)
            with mock.patch.object(app, "is_port_free", return_value=False):
                with self.assertRaisesRegex(RuntimeError, "Docker 已发布范围"):
                    app.next_free_public_proxy_port(None, app.LAN_PROXY_LISTEN_HOST)
        with self.docker_env(
            PROXY_BRIDGE_PROXY_PORT_START="65000",
            PROXY_BRIDGE_PROXY_PORT_END="65100",
        ):
            with self.assertRaisesRegex(RuntimeError, "端口范围无效"):
                app.docker_proxy_port_range()

    def test_docker_network_is_runtime_managed(self) -> None:
        with self.docker_env():
            state = app.empty_state()
            network = app.network_settings_payload(state, lan_addresses=["192.168.1.10"])
            self.assertEqual(app.proxy_listen_host(state), app.LAN_PROXY_LISTEN_HOST)
            self.assertTrue(network["allow_lan"])
            self.assertTrue(network["docker_managed"])
            self.assertFalse(network["configurable"])
            self.assertEqual(network["lan_addresses"], [])
            with self.assertRaisesRegex(ValueError, "Docker 模式"):
                app.update_network_settings({"allow_lan": False})

    def test_docker_sync_status_reports_subscription_scope(self) -> None:
        with self.docker_env():
            app.ensure_dirs()
            app.save_state(app.empty_state())
            status = app.sync_status_payload()
        self.assertEqual(status["settings"]["sync_scope"], "subscriptions")
        self.assertFalse(status["settings"]["browser_sync_enabled"])

    def test_docker_status_hides_browser_state_and_kernel_probes(self) -> None:
        with self.docker_env():
            app.ensure_dirs()
            state = app.empty_state()
            state["nodes"] = [
                {
                    "id": "n1",
                    "name": "browser-only node",
                    webdav_sync.LOCAL_BROWSER_NODE_MARKER: True,
                }
            ]
            state["browser_instances"] = [
                {"id": "hidden-browser", "node_id": "n1", "status": "stopped"}
            ]
            app.save_state(state)
            with (
                mock.patch.object(app, "chrome_path", side_effect=AssertionError("must not probe chrome")),
                mock.patch.object(
                    app,
                    "fingerprint_chromium_path",
                    side_effect=AssertionError("must not probe fingerprint chromium"),
                ),
            ):
                payload = app.status_payload()

        self.assertFalse(payload["features"]["browser"])
        self.assertTrue(payload["runtime"]["docker"])
        self.assertEqual(payload["runtime"]["proxy_port_range"], [21080, 21179])
        self.assertEqual(payload["browser_instances"], [])
        self.assertEqual(payload["active_browsers"], [])
        self.assertEqual(payload["nodes"], [])

    def test_docker_subscription_delete_preserves_hidden_browser_node(self) -> None:
        class FakeSyncEngine:
            def mark_local_change(self) -> None:
                return None

            def maybe_auto_push(self, *, reason: str = "") -> None:
                return None

        with self.docker_env():
            app.ensure_dirs()
            state = app.empty_state()
            state["subscriptions"] = [
                {"id": "sub1", "name": "sub", "node_ids": ["n1"], "node_count": 1}
            ]
            state["nodes"] = [{"id": "n1", "name": "node"}]
            state["browser_instances"] = [
                {"id": "browser1", "node_id": "n1", "status": "stopped"}
            ]
            app.save_state(state)
            with mock.patch.object(app, "get_sync_engine", return_value=FakeSyncEngine()):
                app.delete_subscription("sub1")
            saved = app.load_state()

        self.assertEqual(saved["subscriptions"], [])
        self.assertEqual([item["id"] for item in saved["browser_instances"]], ["browser1"])
        self.assertEqual([item["id"] for item in saved["nodes"]], ["n1"])
        self.assertTrue(saved["nodes"][0][webdav_sync.LOCAL_BROWSER_NODE_MARKER])


if __name__ == "__main__":
    unittest.main()
