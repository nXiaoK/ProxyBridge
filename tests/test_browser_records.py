from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app


class BrowserRecordTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_work_root = app.WORK_ROOT
        self.old_sync_engine = app.SYNC_ENGINE
        self.temp = tempfile.TemporaryDirectory()
        app.configure_work_root(Path(self.temp.name) / "work")
        app.SYNC_ENGINE = None
        with app.PROCESS_LOCK:
            self.old_browser_table = {key: list(value) for key, value in app.BROWSER_TABLE.items()}
            self.old_browser_process_table = dict(app.BROWSER_PROCESS_TABLE)
            app.BROWSER_TABLE.clear()
            app.BROWSER_PROCESS_TABLE.clear()

    def tearDown(self) -> None:
        with app.PROCESS_LOCK:
            app.BROWSER_TABLE.clear()
            app.BROWSER_TABLE.update(self.old_browser_table)
            app.BROWSER_PROCESS_TABLE.clear()
            app.BROWSER_PROCESS_TABLE.update(self.old_browser_process_table)
        app.configure_work_root(self.old_work_root)
        app.SYNC_ENGINE = self.old_sync_engine
        self.temp.cleanup()

    @staticmethod
    def local_browser_env():
        return mock.patch.dict(
            os.environ,
            {
                "PROXY_BRIDGE_DOCKER": "",
                "PROXY_BRIDGE_DISABLE_BROWSER": "",
                "PROXY_BRIDGE_BROWSER_BROKER_URL": "",
            },
            clear=False,
        )

    @staticmethod
    def docker_browser_env():
        return mock.patch.dict(
            os.environ,
            {
                "PROXY_BRIDGE_DOCKER": "1",
                "PROXY_BRIDGE_DISABLE_BROWSER": "",
                "PROXY_BRIDGE_BROWSER_BROKER_URL": "http://browser-broker:8765",
                "PROXY_BRIDGE_BROWSER_BROKER_TOKEN_FILE": "/run/proxybridge-browser/token",
            },
            clear=False,
        )

    @staticmethod
    def state_with_instance(*, remote: bool = False) -> tuple[dict[str, object], dict[str, object]]:
        state = app.empty_state()
        state["nodes"] = [{"id": "node-1", "name": "Test node"}]
        instance = app.create_browser_instance_record(state, "node-1")
        instance["remote_worker"] = remote
        state["browser_instances"] = [instance]
        return state, instance

    def test_local_delete_removes_profile_and_record_but_preserves_node_and_full_pushes(self) -> None:
        with self.local_browser_env():
            app.ensure_dirs()
            state, instance = self.state_with_instance()
            profile = app.browser_instance_profile_path("node-1", str(instance["id"]))
            profile.mkdir(parents=True)
            (profile / "Cookies").write_text("cookie-data", encoding="utf-8")
            app.save_state(state)
            engine = mock.Mock()

            with (
                mock.patch.object(app, "mark_sync_local_change") as mark_change,
                mock.patch.object(app, "get_sync_engine", return_value=engine),
                mock.patch.object(app, "maybe_auto_upload_browser_instances") as single_upload,
            ):
                result = app.delete_browser_instance({"instance_id": instance["id"]})

            saved = app.load_state()

        self.assertTrue(result["profile_deleted"])
        self.assertFalse(profile.exists())
        self.assertEqual(saved["browser_instances"], [])
        self.assertEqual([node["id"] for node in saved["nodes"]], ["node-1"])
        single_upload.assert_not_called()
        mark_change.assert_called_once_with()
        engine.maybe_auto_push.assert_called_once_with(reason="删除浏览器记录")

    def test_docker_delete_stops_worker_before_removing_profile(self) -> None:
        with self.docker_browser_env():
            app.ensure_dirs()
            state, instance = self.state_with_instance(remote=True)
            instance["status"] = "running"
            profile = app.browser_instance_profile_path("node-1", str(instance["id"]))
            profile.mkdir(parents=True)
            (profile / "marker").write_text("profile", encoding="utf-8")
            app.save_state(state)
            engine = mock.Mock()
            observations: list[tuple[bool, bool, bool]] = []

            def stop_worker(payload: dict[str, object], *, auto_upload: bool = True) -> dict[str, object]:
                state_free = app.STATE_LOCK.acquire(blocking=False)
                if state_free:
                    app.STATE_LOCK.release()
                shutdown_free = app.SHUTDOWN_LOCK.acquire(blocking=False)
                if shutdown_free:
                    app.SHUTDOWN_LOCK.release()
                observations.append((profile.exists(), state_free, shutdown_free))
                self.assertFalse(auto_upload)
                self.assertEqual(payload, {"instance_id": instance["id"]})
                return {"ok": True, "killed_browsers": 1}

            with (
                mock.patch.object(app, "_stop_remote_browser", side_effect=stop_worker),
                mock.patch.object(app, "mark_sync_local_change"),
                mock.patch.object(app, "get_sync_engine", return_value=engine),
                mock.patch.object(app, "maybe_auto_upload_browser_instances") as single_upload,
            ):
                result = app.delete_browser_instance({"instance_id": instance["id"]})

            saved = app.load_state()

        self.assertEqual(observations, [(True, True, True)])
        self.assertEqual(result["killed_browsers"], 1)
        self.assertFalse(profile.exists())
        self.assertEqual(saved["browser_instances"], [])
        single_upload.assert_not_called()
        engine.maybe_auto_push.assert_called_once_with(reason="删除浏览器记录")

    def test_legacy_delete_preserves_other_instance_directory(self) -> None:
        sibling_id = "b" * 16
        node_id = "node-legacy"
        legacy_id = app.legacy_browser_instance_id(node_id)
        with self.local_browser_env():
            app.ensure_dirs()
            node_root = app.browser_node_profile_root(node_id)
            (node_root / "Default").mkdir(parents=True)
            (node_root / "Default" / "Cookies").write_text("legacy", encoding="utf-8")
            (node_root / "Local State").write_text("legacy-state", encoding="utf-8")
            sibling_profile = node_root / sibling_id
            (sibling_profile / "Default").mkdir(parents=True)
            sibling_marker = sibling_profile / "Default" / "Cookies"
            sibling_marker.write_text("sibling", encoding="utf-8")
            state = app.empty_state()
            state["nodes"] = [{"id": node_id, "name": "Legacy node"}]
            state["browser_instances"] = [
                {
                    "id": legacy_id,
                    "node_id": node_id,
                    "name": "默认浏览器",
                    "legacy": True,
                    "status": "stopped",
                    "profile_path": str(node_root),
                },
                {
                    "id": sibling_id,
                    "node_id": node_id,
                    "name": "浏览器 2",
                    "legacy": False,
                    "status": "stopped",
                    "profile_path": str(sibling_profile),
                },
            ]
            app.save_state(state)
            engine = mock.Mock()
            with (
                mock.patch.object(app, "mark_sync_local_change"),
                mock.patch.object(app, "get_sync_engine", return_value=engine),
            ):
                app.delete_browser_instance({"instance_id": legacy_id})
            saved = app.load_state()

        self.assertFalse((node_root / "Default").exists())
        self.assertFalse((node_root / "Local State").exists())
        self.assertEqual(sibling_marker.read_text(encoding="utf-8"), "sibling")
        self.assertEqual([item["id"] for item in saved["browser_instances"]], [sibling_id])

    def test_malicious_profile_record_cannot_delete_outside_data_root(self) -> None:
        outside = Path(self.temp.name) / "outside.txt"
        outside.write_text("keep", encoding="utf-8")
        malicious_id = "../../outside.txt"
        with self.local_browser_env():
            app.ensure_dirs()
            state = app.empty_state()
            state["nodes"] = [{"id": "node-1", "name": "Test node"}]
            state["browser_instances"] = [
                {
                    "id": malicious_id,
                    "node_id": "node-1",
                    "name": "malicious",
                    "status": "stopped",
                    "profile_path": str(outside),
                    "legacy": False,
                }
            ]
            app.save_state(state)
            with (
                mock.patch.object(app, "_stop_browser") as stop_browser,
                self.assertRaisesRegex(ValueError, "实例 ID 无效"),
            ):
                app.delete_browser_instance({"instance_id": malicious_id})
            saved = app.load_state()

        self.assertEqual(outside.read_text(encoding="utf-8"), "keep")
        self.assertEqual([item["id"] for item in saved["browser_instances"]], [malicious_id])
        stop_browser.assert_not_called()

    def test_public_records_sort_by_last_use_without_mutating_state_order(self) -> None:
        state = app.empty_state()
        state["browser_instances"] = [
            {"id": "created", "created_at": 500},
            {"id": "started", "created_at": 100, "last_started_at": 900},
            {"id": "stopped", "created_at": 200, "last_stopped_at": 800},
            {"id": "explicit", "created_at": 50, "last_used_at": 1000},
        ]

        public = app.public_browser_instances(state)

        self.assertEqual(
            [item["id"] for item in public],
            ["explicit", "started", "stopped", "created"],
        )
        self.assertEqual(
            [item["id"] for item in state["browser_instances"]],
            ["created", "started", "stopped", "explicit"],
        )


if __name__ == "__main__":
    unittest.main()
