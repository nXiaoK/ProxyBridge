from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import app


class SpeedAndSubscriptionFeatureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_work_root = app.WORK_ROOT
        self.old_runtime_root = app.RUNTIME_ROOT
        self.old_state_path = app.STATE_PATH
        self.temp_dir = tempfile.TemporaryDirectory()
        app.configure_work_root(Path(self.temp_dir.name) / "work")
        app.ensure_dirs()
        app.SHUTTING_DOWN.clear()
        with app.SPEED_TEST_TASK_LOCK:
            app.SPEED_TEST_TASKS.clear()
            app.SPEED_TEST_CANCEL_EVENTS.clear()
        with app.SUBSCRIPTION_REFRESH_TASK_LOCK:
            app.SUBSCRIPTION_REFRESH_TASKS.clear()
            app.SUBSCRIPTION_REFRESH_CANCEL_EVENTS.clear()

    def tearDown(self) -> None:
        with app.SPEED_TEST_TASK_LOCK:
            for event in app.SPEED_TEST_CANCEL_EVENTS.values():
                event.set()
            app.SPEED_TEST_TASKS.clear()
            app.SPEED_TEST_CANCEL_EVENTS.clear()
        with app.SUBSCRIPTION_REFRESH_TASK_LOCK:
            for event in app.SUBSCRIPTION_REFRESH_CANCEL_EVENTS.values():
                event.set()
            app.SUBSCRIPTION_REFRESH_TASKS.clear()
            app.SUBSCRIPTION_REFRESH_CANCEL_EVENTS.clear()
        app.configure_work_root(self.old_work_root)
        app.RUNTIME_ROOT = self.old_runtime_root
        app.STATE_PATH = self.old_state_path
        app.SHUTTING_DOWN.clear()
        self.temp_dir.cleanup()

    def wait_for_task(self, task_id: str, statuses: set[str], timeout: float = 2.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            task = app.speed_test_task_payload(task_id)
            if task["status"] in statuses:
                return task
            time.sleep(0.01)
        self.fail(f"task {task_id} did not reach {statuses}")

    def wait_for_subscription_task(
        self,
        task_id: str,
        statuses: set[str],
        timeout: float = 2.0,
    ) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            task = app.subscription_refresh_task_payload(task_id)
            if task["status"] in statuses:
                return task
            time.sleep(0.01)
        self.fail(f"subscription task {task_id} did not reach {statuses}")

    def save_nodes(self, count: int) -> list[str]:
        state = app.empty_state()
        node_ids = []
        for index in range(count):
            node_id = f"node-{index}"
            node_ids.append(node_id)
            state["nodes"].append(
                {
                    "id": node_id,
                    "name": f"Node {index}",
                    "protocol": "vless",
                    "preferred_core": "sing-box",
                    "settings": {},
                }
            )
        app.save_state(state)
        return node_ids

    def test_speed_settings_and_public_result_age(self) -> None:
        settings = app.update_speed_test_settings(
            {
                "targets": ["https://example.com:8443/check?q=1"],
                "rotation": "random",
                "stale_after_seconds": 120,
                "batch_concurrency": 5,
                "retry_failures": 2,
            }
        )
        state = app.load_state()
        state["tests"] = {
            "node-a": {
                "ok": True,
                "latency_ms": 25,
                "tested_at": 100,
                "attempt_count": 4,
                "success_count": 3,
            }
        }
        app.save_state(state)

        public = app.public_test_results(app.load_state(), current_time=221)["node-a"]

        self.assertEqual(settings["targets"], ["https://example.com:8443/check?q=1"])
        self.assertEqual(settings["batch_concurrency"], 5)
        self.assertEqual(public["age_seconds"], 121)
        self.assertTrue(public["stale"])
        self.assertEqual(public["success_rate"], 0.75)

    def test_conditional_subscription_refresh_keeps_id_and_nodes_on_304(self) -> None:
        original = app.import_subscription(
            {"name": "same", "url": "https://example.test/sub", "raw": "vless://a@example.com:443#A"}
        )
        subscription_id = original["subscription"]["id"]
        app.update_subscription_schedule({"id": subscription_id, "refresh_interval_minutes": 30})
        state = app.load_state()
        state["subscriptions"][0]["etag"] = '"etag-v1"'
        state["subscriptions"][0]["last_modified"] = "Wed, 01 Jan 2025 00:00:00 GMT"
        app.save_state(state)

        with mock.patch.object(
            app,
            "fetch_subscription_response",
            return_value={
                "not_modified": True,
                "etag": '"etag-v1"',
                "last_modified": "Wed, 01 Jan 2025 00:00:00 GMT",
            },
        ) as fetch:
            result = app.refresh_subscription(subscription_id)

        saved = app.load_state()
        fetch.assert_called_once_with(
            "https://example.test/sub",
            etag='"etag-v1"',
            last_modified="Wed, 01 Jan 2025 00:00:00 GMT",
        )
        self.assertTrue(result["not_modified"])
        self.assertEqual(saved["subscriptions"][0]["id"], subscription_id)
        self.assertEqual(saved["subscriptions"][0]["refresh_interval_minutes"], 30)
        self.assertEqual([node["name"] for node in saved["nodes"]], ["A"])
        self.assertEqual(saved["subscriptions"][0]["refresh_history"][-1]["status"], "not_modified")

    def test_failed_refresh_preserves_nodes_and_applies_exponential_backoff(self) -> None:
        original = app.import_subscription(
            {"name": "same", "url": "https://example.test/sub", "raw": "vless://a@example.com:443#A"}
        )
        subscription_id = original["subscription"]["id"]
        app.update_subscription_schedule({"id": subscription_id, "refresh_interval_minutes": 5})

        with mock.patch.object(app, "fetch_subscription_response", side_effect=TimeoutError("offline")):
            for _ in range(2):
                with self.assertRaisesRegex(TimeoutError, "offline"):
                    app.refresh_subscription(subscription_id)

        saved = app.load_state()
        subscription = saved["subscriptions"][0]
        self.assertEqual(subscription["refresh_failures"], 2)
        self.assertGreaterEqual(subscription["next_refresh_at"] - subscription["last_attempt"], 10 * 60)
        self.assertEqual([node["name"] for node in saved["nodes"]], ["A"])
        self.assertEqual(
            [entry["status"] for entry in subscription["refresh_history"]],
            ["failed", "failed"],
        )

    def test_refresh_reports_changed_nodes_without_changing_subscription_id(self) -> None:
        original = app.import_subscription(
            {"name": "same", "url": "https://example.test/sub", "raw": "vless://a@example.com:443#A"}
        )
        subscription_id = original["subscription"]["id"]
        result = app.import_subscription(
            {"name": "same", "url": "https://example.test/sub", "raw": "vless://a@example.net:8443#A"}
        )

        self.assertEqual(result["subscription"]["id"], subscription_id)
        self.assertEqual(result["changes"]["changed"], 1)
        self.assertEqual(result["changes"]["added"], 0)
        self.assertEqual(result["changes"]["removed"], 0)

    def test_batch_speed_test_is_bounded_and_retries_failures(self) -> None:
        node_ids = self.save_nodes(8)
        active = 0
        maximum = 0
        attempts: dict[str, int] = {}
        lock = threading.Lock()

        def fake_test(payload: dict, *, cancel_event=None, persist_result=True) -> dict:
            nonlocal active, maximum
            node_id = payload["node_id"]
            with lock:
                active += 1
                maximum = max(maximum, active)
                attempts[node_id] = attempts.get(node_id, 0) + 1
                attempt = attempts[node_id]
            time.sleep(0.02)
            with lock:
                active -= 1
            return {"ok": attempt > 1, "node_id": node_id, "message": "ok" if attempt > 1 else "fail"}

        with (
            mock.patch.object(app, "test_node", side_effect=fake_test) as test,
            mock.patch.object(app, "persist_test_result", side_effect=lambda result: result) as persist,
        ):
            task = app.start_speed_test_task({"node_ids": node_ids, "concurrency": 3, "retry_failures": 1})
            finished = self.wait_for_task(task["id"], {"completed"})

        self.assertLessEqual(maximum, 3)
        self.assertGreaterEqual(maximum, 2)
        self.assertEqual(finished["completed"], len(node_ids))
        self.assertTrue(all(item["ok"] and item["attempts"] == 2 for item in finished["items"]))
        self.assertEqual(persist.call_count, len(node_ids))
        self.assertTrue(all(call.kwargs.get("persist_result") is False for call in test.call_args_list))

    def test_batch_speed_test_cancel_stops_scheduling_new_items(self) -> None:
        node_ids = self.save_nodes(8)
        entered = threading.Event()

        def fake_test(payload: dict, *, cancel_event=None, persist_result=True) -> dict:
            entered.set()
            assert cancel_event is not None
            cancel_event.wait(timeout=1)
            return {"ok": False, "node_id": payload["node_id"], "message": "cancelled", "cancelled": True}

        with mock.patch.object(app, "test_node", side_effect=fake_test):
            task = app.start_speed_test_task({"node_ids": node_ids, "concurrency": 3})
            self.assertTrue(entered.wait(timeout=1))
            app.cancel_speed_test_task(task["id"])
            finished = self.wait_for_task(task["id"], {"cancelled"})

        self.assertEqual(finished["completed"], len(node_ids))
        self.assertTrue(any(item.get("cancelled") for item in finished["items"]))

    def test_batch_speed_test_rejects_excessive_node_count(self) -> None:
        with mock.patch.object(app, "SPEED_TEST_BATCH_NODE_LIMIT", 2):
            node_ids = self.save_nodes(3)
            with self.assertRaisesRegex(ValueError, "最多测试 2 个节点"):
                app.start_speed_test_task({"node_ids": node_ids})

    def test_subscription_refresh_history_keeps_latest_entries(self) -> None:
        imported = app.import_subscription(
            {"name": "history", "url": "https://example.test/history", "raw": "vless://a@example.com:443#A"}
        )
        subscription_id = imported["subscription"]["id"]

        for index in range(app.SUBSCRIPTION_REFRESH_HISTORY_LIMIT + 5):
            app._append_subscription_refresh_history(
                subscription_id,
                status="updated",
                duration_ms=index,
                changes={"added": index},
            )

        history = app.load_state()["subscriptions"][0]["refresh_history"]
        self.assertEqual(len(history), app.SUBSCRIPTION_REFRESH_HISTORY_LIMIT)
        self.assertEqual(history[0]["duration_ms"], 5)
        self.assertEqual(history[-1]["duration_ms"], app.SUBSCRIPTION_REFRESH_HISTORY_LIMIT + 4)

    def test_batch_subscription_refresh_reports_each_result(self) -> None:
        first = app.import_subscription(
            {"name": "first", "url": "https://example.test/first", "raw": "vless://a@example.com:443#A"}
        )
        second = app.import_subscription(
            {"name": "second", "url": "https://example.test/second", "raw": "vless://b@example.com:443#B"}
        )
        subscription_ids = [first["subscription"]["id"], second["subscription"]["id"]]

        with mock.patch.object(
            app,
            "refresh_subscription",
            side_effect=[
                {"not_modified": True, "changes": {"added": 0, "removed": 0, "changed": 0}},
                TimeoutError("offline"),
            ],
        ):
            task = app.start_subscription_refresh_task({"subscription_ids": subscription_ids})
            finished = self.wait_for_subscription_task(task["id"], {"completed"})

        self.assertEqual(finished["completed"], 2)
        self.assertEqual([item["status"] for item in finished["items"]], ["not_modified", "failed"])
        self.assertIn("offline", finished["items"][1]["error"])

    def test_batch_subscription_refresh_cancel_marks_remaining_items(self) -> None:
        subscription_ids = []
        for index in range(3):
            imported = app.import_subscription(
                {
                    "name": f"sub-{index}",
                    "url": f"https://example.test/{index}",
                    "raw": f"vless://node-{index}@example.com:443#Node-{index}",
                }
            )
            subscription_ids.append(imported["subscription"]["id"])
        entered = threading.Event()
        release = threading.Event()

        def fake_refresh(_subscription_id: str) -> dict:
            entered.set()
            release.wait(timeout=1)
            return {"changes": {"added": 0, "removed": 0, "changed": 0}}

        with mock.patch.object(app, "refresh_subscription", side_effect=fake_refresh):
            task = app.start_subscription_refresh_task({"subscription_ids": subscription_ids})
            self.assertTrue(entered.wait(timeout=1))
            cancelling = app.cancel_subscription_refresh_task(task["id"])
            self.assertEqual(cancelling["status"], "cancelling")
            release.set()
            finished = self.wait_for_subscription_task(task["id"], {"cancelled"})

        self.assertEqual(finished["completed"], 3)
        self.assertEqual(finished["items"][0]["status"], "updated")
        self.assertEqual([item["status"] for item in finished["items"][1:]], ["cancelled", "cancelled"])
        saved = {item["id"]: item for item in app.load_state()["subscriptions"]}
        self.assertEqual(
            [saved[subscription_id]["refresh_history"][-1]["status"] for subscription_id in subscription_ids[1:]],
            ["cancelled", "cancelled"],
        )

    def test_batch_subscription_refresh_cancel_keeps_inflight_task_cancelled(self) -> None:
        imported = app.import_subscription(
            {
                "name": "inflight",
                "url": "https://example.test/inflight",
                "raw": "vless://node@example.com:443#Inflight",
            }
        )
        entered = threading.Event()
        release = threading.Event()

        def fake_refresh(_subscription_id: str) -> dict:
            entered.set()
            release.wait(timeout=1)
            return {"changes": {"added": 0, "removed": 0, "changed": 0}}

        with mock.patch.object(app, "refresh_subscription", side_effect=fake_refresh):
            task = app.start_subscription_refresh_task({"subscription_ids": [imported["subscription"]["id"]]})
            self.assertTrue(entered.wait(timeout=1))
            app.cancel_subscription_refresh_task(task["id"])
            release.set()
            finished = self.wait_for_subscription_task(task["id"], {"cancelled"})

        self.assertEqual(finished["items"][0]["status"], "updated")
        self.assertEqual(finished["completed"], 1)

    def test_shutdown_marks_pending_subscription_refresh_history(self) -> None:
        subscription_ids = []
        for index in range(2):
            imported = app.import_subscription(
                {
                    "name": f"shutdown-{index}",
                    "url": f"https://example.test/shutdown-{index}",
                    "raw": f"vless://node-{index}@example.com:443#Shutdown-{index}",
                }
            )
            subscription_ids.append(imported["subscription"]["id"])
        entered = threading.Event()
        release = threading.Event()

        def fake_refresh(_subscription_id: str) -> dict:
            entered.set()
            release.wait(timeout=1)
            return {"changes": {"added": 0, "removed": 0, "changed": 0}}

        old_process_table = dict(app.PROCESS_TABLE)
        old_bridge_node_table = dict(app.BRIDGE_NODE_TABLE)
        old_browser_table = {key: list(value) for key, value in app.BROWSER_TABLE.items()}
        old_browser_process_table = dict(app.BROWSER_PROCESS_TABLE)
        old_log_proxy_table = dict(app.LOG_PROXY_TABLE)
        old_runtime_log_pump_table = dict(app.RUNTIME_LOG_PUMP_TABLE)
        old_traffic_table = {key: dict(value) for key, value in app.TRAFFIC_TABLE.items()}
        old_test_process_table = dict(app.TEST_PROCESS_TABLE)
        app.PROCESS_TABLE.clear()
        app.BRIDGE_NODE_TABLE.clear()
        app.BROWSER_TABLE.clear()
        app.BROWSER_PROCESS_TABLE.clear()
        app.LOG_PROXY_TABLE.clear()
        app.RUNTIME_LOG_PUMP_TABLE.clear()
        app.TRAFFIC_TABLE.clear()
        app.TEST_PROCESS_TABLE.clear()
        with mock.patch.object(app, "refresh_subscription", side_effect=fake_refresh):
            try:
                task = app.start_subscription_refresh_task({"subscription_ids": subscription_ids})
                self.assertTrue(entered.wait(timeout=1))
                app.shutdown()
                release.set()
                deadline = time.monotonic() + 2
                finished = None
                while time.monotonic() < deadline:
                    candidate = app.subscription_refresh_task_payload(task["id"])
                    if candidate["status"] == "cancelled" and all(item is not None for item in candidate["items"]):
                        finished = candidate
                        break
                    time.sleep(0.01)
                self.assertIsNotNone(finished)
                assert finished is not None
            finally:
                app.PROCESS_TABLE.clear()
                app.PROCESS_TABLE.update(old_process_table)
                app.BRIDGE_NODE_TABLE.clear()
                app.BRIDGE_NODE_TABLE.update(old_bridge_node_table)
                app.BROWSER_TABLE.clear()
                app.BROWSER_TABLE.update(old_browser_table)
                app.BROWSER_PROCESS_TABLE.clear()
                app.BROWSER_PROCESS_TABLE.update(old_browser_process_table)
                app.LOG_PROXY_TABLE.clear()
                app.LOG_PROXY_TABLE.update(old_log_proxy_table)
                app.RUNTIME_LOG_PUMP_TABLE.clear()
                app.RUNTIME_LOG_PUMP_TABLE.update(old_runtime_log_pump_table)
                app.TRAFFIC_TABLE.clear()
                app.TRAFFIC_TABLE.update(old_traffic_table)
                app.TEST_PROCESS_TABLE.clear()
                app.TEST_PROCESS_TABLE.update(old_test_process_table)

        self.assertEqual([item["status"] for item in finished["items"]], ["cancelled", "cancelled"])
        saved = {item["id"]: item for item in app.load_state()["subscriptions"]}
        self.assertEqual(
            [saved[subscription_id]["refresh_history"][-1]["status"] for subscription_id in subscription_ids],
            ["cancelled", "cancelled"],
        )


if __name__ == "__main__":
    unittest.main()
