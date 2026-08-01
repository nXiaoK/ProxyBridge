from __future__ import annotations

import errno
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import app


class FakeProcess:
    def __init__(self, pid: int) -> None:
        self.pid = pid

    def poll(self) -> None:
        return None


class FakeProxy:
    def __init__(self, **_kwargs: object) -> None:
        self.stopped = False

    def start(self) -> None:
        return None

    def stop(self) -> None:
        self.stopped = True

    def is_alive(self) -> bool:
        return not self.stopped


class LifecycleRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self._old_work_root = app.WORK_ROOT
        self._old_runtime_root = app.RUNTIME_ROOT
        self._old_state_path = app.STATE_PATH
        self._old_process_table = dict(app.PROCESS_TABLE)
        self._old_bridge_node_table = dict(app.BRIDGE_NODE_TABLE)
        self._old_browser_table = {key: list(value) for key, value in app.BROWSER_TABLE.items()}
        self._old_browser_process_table = dict(app.BROWSER_PROCESS_TABLE)
        self._old_log_proxy_table = dict(app.LOG_PROXY_TABLE)
        self._old_runtime_log_pump_table = dict(app.RUNTIME_LOG_PUMP_TABLE)
        self._old_traffic_table = {key: dict(value) for key, value in app.TRAFFIC_TABLE.items()}
        self._old_test_process_table = dict(app.TEST_PROCESS_TABLE)
        self._old_fingerprint_cache = dict(app.FINGERPRINT_CHROMIUM_CACHE)
        self._was_shutting_down = app.SHUTTING_DOWN.is_set()
        shutdown_signal_claimed = getattr(app, "SHUTDOWN_SIGNAL_CLAIMED", None)
        self._was_shutdown_signal_claimed = bool(
            shutdown_signal_claimed is not None and shutdown_signal_claimed.is_set()
        )

        root = Path(self._temp_dir.name)
        app.WORK_ROOT = root / "work"
        app.RUNTIME_ROOT = app.WORK_ROOT / "runtime"
        app.STATE_PATH = app.WORK_ROOT / "state.json"
        app.PROCESS_TABLE.clear()
        app.BRIDGE_NODE_TABLE.clear()
        app.BROWSER_TABLE.clear()
        app.BROWSER_PROCESS_TABLE.clear()
        app.LOG_PROXY_TABLE.clear()
        app.RUNTIME_LOG_PUMP_TABLE.clear()
        app.TRAFFIC_TABLE.clear()
        app.TEST_PROCESS_TABLE.clear()
        app.SHUTTING_DOWN.clear()
        if shutdown_signal_claimed is not None:
            shutdown_signal_claimed.clear()
        app.invalidate_fingerprint_chromium_cache()
        app.ensure_dirs()

    def tearDown(self) -> None:
        app.WORK_ROOT = self._old_work_root
        app.RUNTIME_ROOT = self._old_runtime_root
        app.STATE_PATH = self._old_state_path
        app.PROCESS_TABLE.clear()
        app.PROCESS_TABLE.update(self._old_process_table)
        app.BRIDGE_NODE_TABLE.clear()
        app.BRIDGE_NODE_TABLE.update(self._old_bridge_node_table)
        app.BROWSER_TABLE.clear()
        app.BROWSER_TABLE.update(self._old_browser_table)
        app.BROWSER_PROCESS_TABLE.clear()
        app.BROWSER_PROCESS_TABLE.update(self._old_browser_process_table)
        app.LOG_PROXY_TABLE.clear()
        app.LOG_PROXY_TABLE.update(self._old_log_proxy_table)
        app.RUNTIME_LOG_PUMP_TABLE.clear()
        app.RUNTIME_LOG_PUMP_TABLE.update(self._old_runtime_log_pump_table)
        app.TRAFFIC_TABLE.clear()
        app.TRAFFIC_TABLE.update(self._old_traffic_table)
        app.TEST_PROCESS_TABLE.clear()
        app.TEST_PROCESS_TABLE.update(self._old_test_process_table)
        app.FINGERPRINT_CHROMIUM_CACHE.clear()
        app.FINGERPRINT_CHROMIUM_CACHE.update(self._old_fingerprint_cache)
        app.SHUTTING_DOWN.clear()
        if self._was_shutting_down:
            app.SHUTTING_DOWN.set()
        shutdown_signal_claimed = getattr(app, "SHUTDOWN_SIGNAL_CLAIMED", None)
        if shutdown_signal_claimed is not None:
            shutdown_signal_claimed.clear()
            if self._was_shutdown_signal_claimed:
                shutdown_signal_claimed.set()
        self._temp_dir.cleanup()

    def _node(self, node_id: str) -> dict[str, object]:
        return {
            "id": node_id,
            "name": "Lifecycle Node",
            "protocol": "vless",
            "server": "example.com",
            "port": 443,
            "region": "HK",
            "source": "test",
            "raw": "",
            "settings": {},
            "tags": [],
            "transport": "tcp",
            "security": "tls",
            "preferred_core": "sing-box",
        }

    def _write_state(
        self,
        node_id: str,
        *,
        bridges: list[dict[str, object]] | None = None,
        browser_instances: list[dict[str, object]] | None = None,
    ) -> None:
        app.save_state(
            {
                "subscriptions": [],
                "nodes": [self._node(node_id)],
                "bridges": bridges or [],
                "browser_instances": browser_instances or [],
                "tests": {},
                "request_logs": [],
            }
        )

    def _bridge(self, bridge_id: str, node_id: str, pid: int, port: int) -> dict[str, object]:
        return {
            "id": bridge_id,
            "node_id": node_id,
            "node_name": "Lifecycle Node",
            "protocol": "vless",
            "region": "HK",
            "core": "sing-box",
            "port": port,
            "core_port": port + 1000,
            "socks_url": f"socks5://127.0.0.1:{port}",
            "http_url": f"http://127.0.0.1:{port}",
            "status": "running",
            "enabled": True,
            "pid": pid,
            "started_at": 10,
            "upload_bytes": 0,
            "download_bytes": 0,
            "total_bytes": 0,
        }

    def _browser_instance(self, node_id: str, instance_id: str) -> dict[str, object]:
        return {
            "id": instance_id,
            "node_id": node_id,
            "name": "浏览器 1",
            "status": "stopped",
            "profile_path": str(app.browser_instance_profile_path(node_id, instance_id)),
            "fingerprint_seed": app.browser_instance_seed(node_id, instance_id),
            "created_at": 10,
            "last_started_at": None,
            "last_stopped_at": None,
            "pid": None,
            "bridge_id": None,
            "proxy": "",
            "launch_count": 0,
            "legacy": False,
        }

    def _activate_bridge(self, bridge: dict[str, object], upload: int = 0, download: int = 0) -> None:
        bridge_id = str(bridge["id"])
        app.PROCESS_TABLE[bridge_id] = FakeProcess(int(bridge["pid"]))
        app.BRIDGE_NODE_TABLE[bridge_id] = str(bridge["node_id"])
        app.LOG_PROXY_TABLE[bridge_id] = FakeProxy()
        app.init_bridge_traffic(bridge_id, upload, download)

    def _assert_public_stop_waits_for_shutdown(self, action: str) -> None:
        node_id = f"node-shutdown-{action}-single-flight"
        bridge_id = f"bridge-shutdown-{action}-single-flight"
        bridge_pid = 4600
        bridge = self._bridge(bridge_id, node_id, bridge_pid, 24400)
        browser_instance_id = f"browser-shutdown-{action}-single-flight"
        browser_instances: list[dict[str, object]] = []
        if action == "browser":
            browser_instance = self._browser_instance(node_id, browser_instance_id)
            browser_instance.update({"status": "running", "pid": 4601})
            browser_instances.append(browser_instance)
        self._write_state(node_id, bridges=[bridge], browser_instances=browser_instances)

        if action == "browser":
            app.init_bridge_traffic(bridge_id, 77, 88)
            browser_process = FakeProcess(4601)
            app.BROWSER_PROCESS_TABLE[browser_instance_id] = browser_process
            app.BROWSER_TABLE[node_id] = [browser_process]
        else:
            self._activate_bridge(bridge, 77, 88)

        terminate_barrier = threading.Barrier(2)
        first_terminate_entered = threading.Event()
        duplicate_terminate_entered = threading.Event()
        public_started = threading.Event()
        terminate_lock = threading.Lock()
        terminate_pids: list[int] = []
        shutdown_errors: list[BaseException] = []
        public_errors: list[BaseException] = []

        def controlled_terminate(process: FakeProcess) -> None:
            with terminate_lock:
                terminate_pids.append(process.pid)
                call_number = len(terminate_pids)
            if call_number == 1:
                first_terminate_entered.set()
            else:
                duplicate_terminate_entered.set()
            try:
                terminate_barrier.wait(timeout=0.5)
            except threading.BrokenBarrierError:
                pass

        def shutdown_worker() -> None:
            try:
                app.shutdown()
            except BaseException as error:
                shutdown_errors.append(error)

        def public_worker() -> None:
            public_started.set()
            try:
                if action == "bridge":
                    app.stop_bridge(bridge_id)
                elif action == "all":
                    app.stop_all_bridges()
                else:
                    app.stop_browser({"instance_id": browser_instance_id})
            except BaseException as error:
                public_errors.append(error)

        with mock.patch.object(app, "terminate_process", side_effect=controlled_terminate):
            shutdown_thread = threading.Thread(target=shutdown_worker)
            shutdown_thread.start()
            self.assertTrue(first_terminate_entered.wait(timeout=2))
            public_thread = threading.Thread(target=public_worker)
            public_thread.start()
            self.assertTrue(public_started.wait(timeout=2))
            shutdown_thread.join(timeout=2)
            public_thread.join(timeout=2)

        self.assertFalse(shutdown_thread.is_alive())
        self.assertFalse(public_thread.is_alive())
        self.assertEqual(shutdown_errors, [])
        self.assertEqual(public_errors, [])
        self.assertFalse(duplicate_terminate_entered.is_set())
        self.assertEqual(len(terminate_pids), 1)
        saved_bridge = app.load_state()["bridges"][0]
        self.assertEqual(
            (saved_bridge["upload_bytes"], saved_bridge["download_bytes"], saved_bridge["total_bytes"]),
            (77, 88, 165),
        )

    def _assert_sync_payload_waits_for_shutdown(self, payload_name: str) -> None:
        node_id = f"node-shutdown-{payload_name}-gate"
        bridge_id = f"bridge-shutdown-{payload_name}-gate"
        bridge = self._bridge(bridge_id, node_id, 4610, 24410)
        self._write_state(node_id, bridges=[bridge])
        self._activate_bridge(bridge, 12, 34)
        terminate_entered = threading.Event()
        release_terminate = threading.Event()
        sync_entered = threading.Event()
        payload_started = threading.Event()
        shutdown_errors: list[BaseException] = []
        payload_errors: list[BaseException] = []
        original_sync = app.sync_process_status

        def controlled_terminate(_process: FakeProcess) -> None:
            terminate_entered.set()
            if not release_terminate.wait(timeout=2):
                raise AssertionError("shutdown 终止未获准继续")

        def observed_sync(state: dict[str, object]) -> bool:
            sync_entered.set()
            return original_sync(state)

        def shutdown_worker() -> None:
            try:
                app.shutdown()
            except BaseException as error:
                shutdown_errors.append(error)

        def payload_worker() -> None:
            payload_started.set()
            try:
                if payload_name == "status":
                    app.status_payload()
                else:
                    app.core_log_payload("sing-box")
            except BaseException as error:
                payload_errors.append(error)

        with (
            mock.patch.object(app, "terminate_process", side_effect=controlled_terminate),
            mock.patch.object(app, "sync_process_status", side_effect=observed_sync),
            mock.patch.object(app, "wait_for_port", return_value=True),
        ):
            shutdown_thread = threading.Thread(target=shutdown_worker)
            shutdown_thread.start()
            self.assertTrue(terminate_entered.wait(timeout=2))
            payload_thread = threading.Thread(target=payload_worker)
            payload_thread.start()
            self.assertTrue(payload_started.wait(timeout=2))
            sync_entered_before_release = sync_entered.wait(timeout=0.2)
            release_terminate.set()
            shutdown_thread.join(timeout=2)
            payload_thread.join(timeout=2)

        self.assertFalse(shutdown_thread.is_alive())
        self.assertFalse(payload_thread.is_alive())
        self.assertEqual(shutdown_errors, [])
        self.assertEqual(payload_errors, [])
        self.assertFalse(sync_entered_before_release)

    def test_start_bridge_returns_existing_active_bridge(self) -> None:
        node_id = "node-bridge-single-flight"
        self._write_state(node_id)
        ports = iter([24180, 25180, 24181, 25181])

        class FakePopen:
            calls: list[tuple[list[str], dict[str, object]]] = []

            def __init__(self, command: list[str], **kwargs: object) -> None:
                self.pid = 4100 + len(self.calls)
                self.calls.append((command, kwargs))

            def poll(self) -> None:
                return None

        with (
            mock.patch.object(app, "generate_config", return_value={"test": True}),
            mock.patch.object(app, "core_command", return_value=["fake-core"]),
            mock.patch.object(app, "next_free_port", side_effect=lambda _preferred=None: next(ports)),
            mock.patch.object(app, "wait_for_port", return_value=True),
            mock.patch.object(app.subprocess, "Popen", FakePopen),
            mock.patch.object(app, "Socks5LogProxy", FakeProxy),
        ):
            first = app.start_bridge({"node_id": node_id, "core": "auto"})
            second = app.start_bridge({"node_id": node_id, "core": "auto"})

        self.assertEqual(second["id"], first["id"])
        self.assertEqual(len(FakePopen.calls), 1)
        active = [
            bridge
            for bridge in app.load_state()["bridges"]
            if bridge.get("node_id") == node_id and bridge.get("status") in app.ACTIVE_BRIDGE_STATUSES
        ]
        self.assertEqual([bridge["id"] for bridge in active], [first["id"]])

    def test_start_bridge_validates_core_before_reusing_active_bridge(self) -> None:
        node_id = "node-bridge-invalid-core"
        bridge = self._bridge("bridge-invalid-core", node_id, 4150, 24190)
        self._write_state(node_id, bridges=[bridge])
        self._activate_bridge(bridge)

        with mock.patch.object(app, "wait_for_port", return_value=True):
            with self.assertRaisesRegex(ValueError, "核心只能"):
                app.start_bridge({"node_id": node_id, "core": "invalid-core"})

        self.assertEqual(list(app.PROCESS_TABLE), [bridge["id"]])

    def test_start_bridge_rejects_conflicting_active_parameters(self) -> None:
        node_id = "node-bridge-parameter-conflict"
        bridge = self._bridge("bridge-parameter-conflict", node_id, 4160, 24191)
        self._write_state(node_id, bridges=[bridge])
        self._activate_bridge(bridge)

        with mock.patch.object(app, "wait_for_port", return_value=True):
            for payload in (
                {"node_id": node_id, "core": "xray"},
                {"node_id": node_id, "core": "auto", "port": 24192},
            ):
                with self.subTest(payload=payload):
                    with self.assertRaisesRegex(ValueError, "冲突"):
                        app.start_bridge(payload)

            reused = app.start_bridge({"node_id": node_id, "core": "sing-box", "port": 24191})

        self.assertEqual(reused["id"], bridge["id"])
        self.assertEqual(list(app.PROCESS_TABLE), [bridge["id"]])

    def test_start_bridge_rolls_back_runtime_when_final_save_fails(self) -> None:
        node_id = "node-bridge-save-rollback"
        self._write_state(node_id)
        ports = iter([24420, 25420])
        persistence_error = RuntimeError("Bridge 最终保存失败")
        terminated_pids: list[int] = []

        class FakePopen:
            calls: list["FakePopen"] = []

            def __init__(self, _command: list[str], **_kwargs: object) -> None:
                self.pid = 4700
                self.calls.append(self)

            def poll(self) -> None:
                return None

        class TrackingProxy:
            instances: list["TrackingProxy"] = []

            def __init__(self, **kwargs: object) -> None:
                self.node_id = str(kwargs["node_id"])
                self.stopped = False
                self.instances.append(self)

            def start(self) -> None:
                return None

            def stop(self) -> None:
                self.stopped = True

            def is_alive(self) -> bool:
                return not self.stopped

        with (
            mock.patch.object(app, "generate_config", return_value={"test": True}),
            mock.patch.object(app, "core_command", return_value=["fake-core"]),
            mock.patch.object(app, "next_free_port", side_effect=lambda _preferred=None: next(ports)),
            mock.patch.object(app, "wait_for_port", return_value=True),
            mock.patch.object(app.subprocess, "Popen", FakePopen),
            mock.patch.object(app, "Socks5LogProxy", TrackingProxy),
            mock.patch.object(app, "terminate_process", side_effect=lambda process: terminated_pids.append(process.pid)),
            mock.patch.object(app, "save_state", side_effect=persistence_error),
        ):
            with self.assertRaises(RuntimeError) as caught:
                app.start_bridge({"node_id": node_id, "core": "auto"})

        self.assertIs(caught.exception, persistence_error)
        self.assertEqual(terminated_pids, [4700])
        self.assertTrue(TrackingProxy.instances[0].stopped)
        self.assertEqual(app.PROCESS_TABLE, {})
        self.assertEqual(app.BRIDGE_NODE_TABLE, {})
        self.assertEqual(app.LOG_PROXY_TABLE, {})
        self.assertEqual(app.TRAFFIC_TABLE, {})

    def test_start_bridge_keeps_failed_cleanup_registered_and_blocks_duplicate_start(self) -> None:
        node_id = "node-bridge-save-terminate-failure"
        self._write_state(node_id)
        ports = iter([24421, 25421, 24422, 25422])
        persistence_error = RuntimeError("Bridge 保存失败且终止失败")
        terminate_attempts: list[int] = []

        class FakePopen:
            calls: list["FakePopen"] = []

            def __init__(self, _command: list[str], **_kwargs: object) -> None:
                self.pid = 4710 + len(self.calls)
                self.calls.append(self)

            def poll(self) -> None:
                return None

        class TrackingProxy:
            instances: list["TrackingProxy"] = []

            def __init__(self, **kwargs: object) -> None:
                self.node_id = str(kwargs["node_id"])
                self.stop_calls = 0
                self.instances.append(self)

            def start(self) -> None:
                return None

            def stop(self) -> None:
                self.stop_calls += 1

            def is_alive(self) -> bool:
                return self.stop_calls == 0

        def failing_terminate(process: FakePopen) -> None:
            terminate_attempts.append(process.pid)
            raise RuntimeError("Bridge 回滚终止失败")

        with (
            mock.patch.object(app, "generate_config", return_value={"test": True}),
            mock.patch.object(app, "core_command", return_value=["fake-core"]),
            mock.patch.object(app, "next_free_port", side_effect=lambda _preferred=None: next(ports)),
            mock.patch.object(app, "wait_for_port", return_value=True),
            mock.patch.object(app.subprocess, "Popen", FakePopen),
            mock.patch.object(app, "Socks5LogProxy", TrackingProxy),
        ):
            with (
                mock.patch.object(app, "terminate_process", side_effect=failing_terminate),
                mock.patch.object(app, "save_state", side_effect=persistence_error),
            ):
                with self.assertRaises(RuntimeError) as caught:
                    app.start_bridge({"node_id": node_id, "core": "auto"})

            process = FakePopen.calls[0]
            proxy = TrackingProxy.instances[0]
            self.assertIs(caught.exception, persistence_error)
            self.assertEqual(terminate_attempts, [process.pid])
            self.assertEqual(proxy.stop_calls, 1)
            self.assertIn(process, app.PROCESS_TABLE.values())
            self.assertNotIn(proxy, app.LOG_PROXY_TABLE.values())
            self.assertEqual(set(app.BRIDGE_NODE_TABLE), set(app.PROCESS_TABLE))
            self.assertEqual(list(app.BRIDGE_NODE_TABLE.values()), [node_id])
            self.assertTrue(app.TRAFFIC_TABLE)

            with self.assertRaisesRegex(RuntimeError, "未清理|残留"):
                app.start_bridge({"node_id": node_id, "core": "auto"})
            self.assertEqual(len(FakePopen.calls), 1)

        retried_pids: list[int] = []
        with mock.patch.object(app, "terminate_process", side_effect=lambda process: retried_pids.append(process.pid)):
            app.shutdown()

        self.assertEqual(retried_pids, [process.pid])
        self.assertEqual(app.PROCESS_TABLE, {})
        self.assertEqual(app.BRIDGE_NODE_TABLE, {})
        self.assertEqual(app.LOG_PROXY_TABLE, {})
        self.assertEqual(app.TRAFFIC_TABLE, {})

    def test_process_only_bridge_residual_blocks_duplicate_start(self) -> None:
        node_id = "node-process-only-residual"
        self._write_state(node_id)
        ports = iter([24510, 25510, 24511, 25511])
        terminate_attempts: list[int] = []

        class FakePopen:
            calls: list["FakePopen"] = []

            def __init__(self, _command: list[str], **_kwargs: object) -> None:
                self.pid = 4820 + len(self.calls)
                self.calls.append(self)

            def poll(self) -> None:
                return None

        def fail_first_termination(process: FakePopen) -> None:
            terminate_attempts.append(process.pid)
            if len(terminate_attempts) == 1:
                raise RuntimeError("core cleanup failed")

        with (
            mock.patch.object(app, "generate_config", return_value={"test": True}),
            mock.patch.object(app, "core_command", return_value=["fake-core"]),
            mock.patch.object(app, "next_free_port", side_effect=lambda _preferred=None: next(ports)),
            mock.patch.object(app, "wait_for_port", return_value=False),
            mock.patch.object(app.subprocess, "Popen", FakePopen),
            mock.patch.object(app, "terminate_process", side_effect=fail_first_termination),
        ):
            with self.assertRaisesRegex(RuntimeError, "内部端口启动超时"):
                app.start_bridge({"node_id": node_id, "core": "auto"})
            with self.assertRaisesRegex(RuntimeError, "未清理|残留"):
                app.start_bridge({"node_id": node_id, "core": "auto"})

        self.assertEqual(len(FakePopen.calls), 1)
        self.assertEqual(len(app.PROCESS_TABLE), 1)

        with mock.patch.object(app, "terminate_process", return_value=None):
            app.shutdown()
        self.assertEqual(app.PROCESS_TABLE, {})
        self.assertEqual(app.BRIDGE_NODE_TABLE, {})

    def test_running_browser_instance_cannot_be_launched_twice(self) -> None:
        node_id = "node-browser-single-flight"
        bridge_id = "bridge-browser-single-flight"
        instance_id = "browser-single-flight"
        bridge = self._bridge(bridge_id, node_id, 4200, 24280)
        instance = self._browser_instance(node_id, instance_id)
        self._write_state(node_id, bridges=[bridge], browser_instances=[instance])
        self._activate_bridge(bridge)
        start_barrier = threading.Barrier(3)
        popen_barrier = threading.Barrier(2)
        popen_entered = threading.Event()
        results_lock = threading.Lock()
        results: list[dict[str, object]] = []
        errors: list[BaseException] = []

        class FakePopen:
            calls: list[tuple[list[str], dict[str, object]]] = []

            def __init__(self, command: list[str], **kwargs: object) -> None:
                self.pid = 4300 + len(self.calls)
                self.calls.append((command, kwargs))
                popen_entered.set()
                try:
                    popen_barrier.wait(timeout=0.5)
                except threading.BrokenBarrierError:
                    pass

            def poll(self) -> None:
                return None

        def worker() -> None:
            try:
                start_barrier.wait()
                result = app.launch_browser({"node_id": node_id, "instance_id": instance_id})
                with results_lock:
                    results.append(result)
            except BaseException as error:
                with results_lock:
                    errors.append(error)

        with (
            mock.patch.object(app, "fingerprint_chromium_path", return_value="/fake/Chromium"),
            mock.patch.object(app, "wait_for_port", return_value=True),
            mock.patch.object(app, "configure_browser_profile_defaults"),
            mock.patch.object(app, "ensure_google_search_extension", return_value=Path("/fake/google-search")),
            mock.patch.object(app.subprocess, "Popen", FakePopen),
        ):
            threads = [threading.Thread(target=worker) for _ in range(2)]
            for thread in threads:
                thread.start()
            start_barrier.wait()
            self.assertTrue(popen_entered.wait(timeout=2))
            for thread in threads:
                thread.join(timeout=2)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(len(FakePopen.calls), 1)
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], ValueError)
        self.assertRegex(str(errors[0]), "已在运行")
        saved_instance = app.find_browser_instance(app.load_state(), instance_id)
        self.assertEqual(saved_instance["pid"], results[0]["pid"])
        self.assertEqual(saved_instance["launch_count"], 1)

    def test_launch_browser_rejects_shutdown_before_popen(self) -> None:
        node_id = "node-browser-shutdown-reject"
        bridge = self._bridge("bridge-browser-shutdown-reject", node_id, 4350, 24290)
        instance_id = "browser-shutdown-reject"
        instance = self._browser_instance(node_id, instance_id)
        self._write_state(node_id, bridges=[bridge], browser_instances=[instance])
        self._activate_bridge(bridge)
        app.SHUTTING_DOWN.set()

        class FakePopen:
            calls = 0

            def __init__(self, _command: list[str], **_kwargs: object) -> None:
                self.pid = 4351
                FakePopen.calls += 1

            def poll(self) -> None:
                return None

        with (
            mock.patch.object(app, "fingerprint_chromium_path", return_value="/fake/Chromium"),
            mock.patch.object(app, "wait_for_port", return_value=True),
            mock.patch.object(app.subprocess, "Popen", FakePopen),
        ):
            with self.assertRaisesRegex(RuntimeError, "正在退出"):
                app.launch_browser({"node_id": node_id, "instance_id": instance_id})

        self.assertEqual(FakePopen.calls, 0)
        self.assertNotIn(instance_id, app.BROWSER_PROCESS_TABLE)

    def test_launch_browser_rolls_back_process_when_state_save_fails(self) -> None:
        node_id = "node-browser-save-rollback"
        bridge = self._bridge("bridge-browser-save-rollback", node_id, 4360, 24291)
        instance_id = "browser-save-rollback"
        instance = self._browser_instance(node_id, instance_id)
        self._write_state(node_id, bridges=[bridge], browser_instances=[instance])
        self._activate_bridge(bridge)
        persistence_error = RuntimeError("最终浏览器状态保存失败")

        class FakePopen:
            created: list["FakePopen"] = []

            def __init__(self, _command: list[str], **_kwargs: object) -> None:
                self.pid = 4361
                self.terminated = False
                self.created.append(self)

            def poll(self) -> None:
                return None

        def successful_cleanup(process: FakePopen) -> None:
            process.terminated = True

        with (
            mock.patch.object(app, "fingerprint_chromium_path", return_value="/fake/Chromium"),
            mock.patch.object(app, "wait_for_port", return_value=True),
            mock.patch.object(app.subprocess, "Popen", FakePopen),
            mock.patch.object(app, "terminate_process", side_effect=successful_cleanup),
            mock.patch.object(app, "save_state", side_effect=persistence_error),
        ):
            with self.assertRaises(RuntimeError) as caught:
                app.launch_browser({"node_id": node_id, "instance_id": instance_id})

        self.assertIs(caught.exception, persistence_error)
        self.assertEqual(len(FakePopen.created), 1)
        self.assertTrue(FakePopen.created[0].terminated)
        self.assertNotIn(instance_id, app.BROWSER_PROCESS_TABLE)
        self.assertNotIn(FakePopen.created[0], app.BROWSER_TABLE.get(node_id, []))
        saved_instance = app.find_browser_instance(app.load_state(), instance_id)
        self.assertEqual(saved_instance["status"], "stopped")
        self.assertIsNone(saved_instance["pid"])
        self.assertEqual(saved_instance["launch_count"], 0)

    def test_launch_browser_keeps_process_registered_when_save_and_terminate_fail(self) -> None:
        node_id = "node-browser-save-terminate-failure"
        bridge = self._bridge("bridge-browser-save-terminate-failure", node_id, 4362, 24292)
        instance_id = "browser-save-terminate-failure"
        instance = self._browser_instance(node_id, instance_id)
        self._write_state(node_id, bridges=[bridge], browser_instances=[instance])
        self._activate_bridge(bridge)
        persistence_error = RuntimeError("浏览器最终保存失败")
        terminate_attempts: list[int] = []

        class FakePopen:
            created: list["FakePopen"] = []

            def __init__(self, _command: list[str], **_kwargs: object) -> None:
                self.pid = 4363
                self.created.append(self)

            def poll(self) -> None:
                return None

        def failing_terminate(process: FakePopen) -> None:
            terminate_attempts.append(process.pid)
            raise RuntimeError("浏览器回滚终止失败")

        with (
            mock.patch.object(app, "fingerprint_chromium_path", return_value="/fake/Chromium"),
            mock.patch.object(app, "wait_for_port", return_value=True),
            mock.patch.object(app.subprocess, "Popen", FakePopen),
            mock.patch.object(app, "terminate_process", side_effect=failing_terminate),
            mock.patch.object(app, "save_state", side_effect=persistence_error),
        ):
            with self.assertRaises(RuntimeError) as caught:
                app.launch_browser({"node_id": node_id, "instance_id": instance_id})

        process = FakePopen.created[0]
        self.assertIs(caught.exception, persistence_error)
        self.assertEqual(terminate_attempts, [process.pid])
        self.assertIs(app.BROWSER_PROCESS_TABLE.get(instance_id), process)
        self.assertIn(process, app.BROWSER_TABLE[node_id])

        retried_pids: list[int] = []
        with mock.patch.object(app, "terminate_process", side_effect=lambda item: retried_pids.append(item.pid)):
            app.shutdown()

        self.assertIn(process.pid, retried_pids)
        self.assertNotIn(instance_id, app.BROWSER_PROCESS_TABLE)
        self.assertNotIn(process, app.BROWSER_TABLE.get(node_id, []))

    def test_terminate_process_waits_for_exit_after_force_kill(self) -> None:
        events: list[object] = []

        class ForceKilledProcess:
            def __init__(self) -> None:
                self.pid = 4821
                self.wait_calls = 0

            def poll(self) -> None:
                return None

            def wait(self, *, timeout: int) -> int:
                self.wait_calls += 1
                events.append(("wait", timeout))
                if self.wait_calls == 1:
                    raise app.subprocess.TimeoutExpired(str(self.pid), timeout)
                return 0

        process = ForceKilledProcess()

        def kill_process_group(_pid: int, sig: int) -> None:
            events.append(sig)

        with mock.patch.object(app.os, "killpg", side_effect=kill_process_group):
            app.terminate_process(process)

        self.assertEqual(
            events,
            [app.signal.SIGTERM, ("wait", 3), app.signal.SIGKILL, ("wait", 3)],
        )

    def test_runtime_log_pump_reports_failure_and_unregisters_itself(self) -> None:
        bridge_id = "bridge-log-pump-failure"

        class FailingStream:
            def __init__(self) -> None:
                self.closed = False

            def read(self, _size: int) -> bytes:
                raise OSError("broken core log pipe")

            def close(self) -> None:
                self.closed = True

        stream = FailingStream()
        log_path = app.RUNTIME_ROOT / f"{bridge_id}.log"
        thread = threading.Thread(
            target=app.pump_runtime_log,
            args=(stream, log_path),
            kwargs={"bridge_id": bridge_id},
        )
        app.RUNTIME_LOG_PUMP_TABLE[bridge_id] = thread

        with mock.patch.object(app, "log_event") as logger:
            thread.start()
            thread.join(timeout=2)

        self.assertFalse(thread.is_alive())
        self.assertTrue(stream.closed)
        self.assertNotIn(bridge_id, app.RUNTIME_LOG_PUMP_TABLE)
        logger.assert_called_once()
        self.assertIn("broken core log pipe", logger.call_args.args[0])

    def test_stop_process_keeps_live_process_but_removes_dead_log_pump_on_failure(self) -> None:
        bridge_id = "bridge-stop-dead-log-pump"
        node_id = "node-stop-dead-log-pump"
        process = FakeProcess(4825)
        log_pump = mock.Mock()
        log_pump.is_alive.return_value = False
        app.PROCESS_TABLE[bridge_id] = process
        app.BRIDGE_NODE_TABLE[bridge_id] = node_id
        app.RUNTIME_LOG_PUMP_TABLE[bridge_id] = log_pump

        with mock.patch.object(app, "terminate_process", side_effect=RuntimeError("terminate failed")):
            with self.assertRaisesRegex(RuntimeError, "terminate failed"):
                app.stop_process(bridge_id)

        self.assertIs(app.PROCESS_TABLE.get(bridge_id), process)
        self.assertEqual(app.BRIDGE_NODE_TABLE.get(bridge_id), node_id)
        self.assertNotIn(bridge_id, app.RUNTIME_LOG_PUMP_TABLE)

    def test_stop_process_finishes_cleanup_when_process_exits_during_termination_error(self) -> None:
        bridge_id = "bridge-stop-exit-race"
        node_id = "node-stop-exit-race"

        class ExitedProcess(FakeProcess):
            def poll(self) -> int:
                return 0

        process = ExitedProcess(4827)
        app.PROCESS_TABLE[bridge_id] = process
        app.BRIDGE_NODE_TABLE[bridge_id] = node_id

        with mock.patch.object(app, "terminate_process", side_effect=OSError("wait raced with exit")):
            result = app.stop_process(bridge_id)

        self.assertEqual(result, {"upload_bytes": 0, "download_bytes": 0, "total_bytes": 0})
        self.assertNotIn(bridge_id, app.PROCESS_TABLE)
        self.assertNotIn(bridge_id, app.BRIDGE_NODE_TABLE)

    def test_bridge_log_thread_start_failure_leaves_no_runtime_entries(self) -> None:
        node_id = "node-log-thread-start-failure"
        self._write_state(node_id)
        terminated: list[int] = []

        class FakeStream:
            def close(self) -> None:
                return None

        class FakePopen:
            def __init__(self, _command: list[str], **_kwargs: object) -> None:
                self.pid = 4826
                self.stdout = FakeStream()

            def poll(self) -> None:
                return None

        ports = iter([24536, 25536])
        with (
            mock.patch.object(app, "generate_config", return_value={"test": True}),
            mock.patch.object(app, "core_command", return_value=["fake-core"]),
            mock.patch.object(app, "next_free_port", side_effect=lambda _preferred=None: next(ports)),
            mock.patch.object(app.subprocess, "Popen", FakePopen),
            mock.patch.object(threading.Thread, "start", side_effect=RuntimeError("thread start failed")),
            mock.patch.object(app, "terminate_process", side_effect=lambda process: terminated.append(process.pid)),
        ):
            with self.assertRaisesRegex(RuntimeError, "thread start failed"):
                app.start_bridge({"node_id": node_id, "core": "auto"})

        self.assertEqual(terminated, [4826])
        self.assertEqual(app.PROCESS_TABLE, {})
        self.assertEqual(app.RUNTIME_LOG_PUMP_TABLE, {})
        self.assertEqual(app.BRIDGE_NODE_TABLE, {})

    def test_stop_browser_instance_keeps_handle_when_force_kill_wait_fails(self) -> None:
        node_id = "node-browser-force-kill-retry"
        instance_id = "browser-force-kill-retry"

        class StubbornProcess:
            def __init__(self) -> None:
                self.pid = 4822
                self.wait_timeouts: list[int] = []

            def poll(self) -> None:
                return None

            def wait(self, *, timeout: int) -> int:
                self.wait_timeouts.append(timeout)
                raise app.subprocess.TimeoutExpired(str(self.pid), timeout)

        process = StubbornProcess()
        app.BROWSER_PROCESS_TABLE[instance_id] = process
        app.BROWSER_TABLE[node_id] = [process]

        with mock.patch.object(app.os, "killpg"):
            with self.assertRaises(app.subprocess.TimeoutExpired):
                app.stop_browser_instance_process(instance_id)

        self.assertEqual(process.wait_timeouts, [3, 3])
        self.assertIs(app.BROWSER_PROCESS_TABLE.get(instance_id), process)
        self.assertEqual(app.BROWSER_TABLE.get(node_id), [process])

    def test_stop_browser_instance_keeps_handle_until_termination_succeeds(self) -> None:
        node_id = "node-browser-stop-retry"
        instance_id = "browser-stop-retry"
        process = FakeProcess(4801)
        app.BROWSER_PROCESS_TABLE[instance_id] = process
        app.BROWSER_TABLE[node_id] = [process]

        with mock.patch.object(app, "terminate_process", side_effect=RuntimeError("terminate failed")):
            with self.assertRaisesRegex(RuntimeError, "terminate failed"):
                app.stop_browser_instance_process(instance_id)

        self.assertIs(app.BROWSER_PROCESS_TABLE.get(instance_id), process)
        self.assertEqual(app.BROWSER_TABLE.get(node_id), [process])

        retried: list[int] = []
        with mock.patch.object(app, "terminate_process", side_effect=lambda item: retried.append(item.pid)):
            self.assertEqual(app.stop_browser_instance_process(instance_id), 1)
        self.assertEqual(retried, [4801])
        self.assertNotIn(instance_id, app.BROWSER_PROCESS_TABLE)
        self.assertNotIn(node_id, app.BROWSER_TABLE)

    def test_stop_untracked_browsers_removes_only_successful_processes(self) -> None:
        node_id = "node-browser-batch-retry"
        failed = FakeProcess(4811)
        successful = FakeProcess(4812)
        app.BROWSER_TABLE[node_id] = [failed, successful]
        attempted: list[int] = []

        def terminate(item: FakeProcess) -> None:
            attempted.append(item.pid)
            if item is failed:
                raise RuntimeError("batch terminate failed")

        with mock.patch.object(app, "terminate_process", side_effect=terminate):
            with self.assertRaisesRegex(RuntimeError, "batch terminate failed"):
                app.stop_untracked_browser_processes_for_node(node_id)

        self.assertEqual(attempted, [4811, 4812])
        self.assertEqual(app.BROWSER_TABLE[node_id], [failed])

    def test_start_bridge_and_shutdown_share_lifecycle_barrier(self) -> None:
        node_id = "node-bridge-shutdown-barrier"
        self._write_state(node_id)
        port_entered = threading.Event()
        release_port = threading.Event()
        shutdown_started = threading.Event()
        shutdown_done = threading.Event()
        ports = iter([24300, 25300])
        port_lock = threading.Lock()
        port_calls = 0
        start_results: list[dict[str, object]] = []
        start_errors: list[BaseException] = []
        shutdown_errors: list[BaseException] = []
        terminated_pids: list[int] = []

        class FakePopen:
            calls = 0

            def __init__(self, _command: list[str], **_kwargs: object) -> None:
                self.pid = 4370
                FakePopen.calls += 1

            def poll(self) -> None:
                return None

        def controlled_next_port(_preferred: int | None = None) -> int:
            nonlocal port_calls
            with port_lock:
                port_calls += 1
                first_call = port_calls == 1
            if first_call:
                port_entered.set()
                if not release_port.wait(timeout=2):
                    raise AssertionError("Bridge 端口分配未获准继续")
            return next(ports)

        def start_worker() -> None:
            try:
                start_results.append(app.start_bridge({"node_id": node_id, "core": "auto"}))
            except BaseException as error:
                start_errors.append(error)

        def shutdown_worker() -> None:
            shutdown_started.set()
            try:
                app.shutdown()
            except BaseException as error:
                shutdown_errors.append(error)
            finally:
                shutdown_done.set()

        with (
            mock.patch.object(app, "generate_config", return_value={"test": True}),
            mock.patch.object(app, "core_command", return_value=["fake-core"]),
            mock.patch.object(app, "next_free_port", side_effect=controlled_next_port),
            mock.patch.object(app, "wait_for_port", return_value=True),
            mock.patch.object(app.subprocess, "Popen", FakePopen),
            mock.patch.object(app, "Socks5LogProxy", FakeProxy),
            mock.patch.object(app, "terminate_process", side_effect=lambda process: terminated_pids.append(process.pid)),
        ):
            start_thread = threading.Thread(target=start_worker)
            start_thread.start()
            self.assertTrue(port_entered.wait(timeout=2))
            lifecycle_barrier_was_free = app.SHUTDOWN_LOCK.acquire(blocking=False)
            if lifecycle_barrier_was_free:
                app.SHUTDOWN_LOCK.release()
            shutdown_thread = threading.Thread(target=shutdown_worker)
            shutdown_thread.start()
            self.assertTrue(shutdown_started.wait(timeout=2))
            try:
                if lifecycle_barrier_was_free:
                    self.assertTrue(shutdown_done.wait(timeout=2))
            finally:
                release_port.set()
            start_thread.join(timeout=2)
            shutdown_thread.join(timeout=2)

        self.assertFalse(start_thread.is_alive())
        self.assertFalse(shutdown_thread.is_alive())
        self.assertFalse(lifecycle_barrier_was_free)
        self.assertEqual(start_errors, [])
        self.assertEqual(shutdown_errors, [])
        self.assertEqual(len(start_results), 1)
        self.assertEqual(FakePopen.calls, 1)
        self.assertEqual(terminated_pids, [4370])
        self.assertEqual(app.PROCESS_TABLE, {})
        self.assertEqual(app.LOG_PROXY_TABLE, {})

    def test_core_resolution_does_not_hold_shutdown_lock(self) -> None:
        node_id = "node-core-download-outside-shutdown"
        self._write_state(node_id)
        core_entered = threading.Event()
        release_core = threading.Event()
        ports = iter([24310, 25310])
        results: list[dict[str, object]] = []
        errors: list[BaseException] = []

        class FakePopen:
            pid = 4371

            def __init__(self, _command: list[str], **_kwargs: object) -> None:
                return None

            def poll(self) -> None:
                return None

        def blocking_core_command(_core: str, _path: Path) -> list[str]:
            core_entered.set()
            if not release_core.wait(timeout=2):
                raise AssertionError("core resolution was not released")
            return ["fake-core"]

        def worker() -> None:
            try:
                results.append(app.start_bridge({"node_id": node_id, "core": "auto"}))
            except BaseException as error:
                errors.append(error)

        with (
            mock.patch.object(app, "generate_config", return_value={"test": True}),
            mock.patch.object(app, "core_command", side_effect=blocking_core_command),
            mock.patch.object(app, "next_free_port", side_effect=lambda _preferred=None: next(ports)),
            mock.patch.object(app, "wait_for_port", return_value=True),
            mock.patch.object(app.subprocess, "Popen", FakePopen),
            mock.patch.object(app, "Socks5LogProxy", FakeProxy),
        ):
            thread = threading.Thread(target=worker)
            thread.start()
            self.assertTrue(core_entered.wait(timeout=2))
            lifecycle_lock_free = app.SHUTDOWN_LOCK.acquire(blocking=False)
            if lifecycle_lock_free:
                app.SHUTDOWN_LOCK.release()
            release_core.set()
            thread.join(timeout=2)

        self.assertTrue(lifecycle_lock_free)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 1)
        with mock.patch.object(app, "terminate_process", return_value=None):
            app.shutdown()

    def test_browser_waiting_for_bridge_start_does_not_hold_shutdown_lock(self) -> None:
        node_id = "node-browser-bridge-lock-order"
        instance_id = "browser-bridge-lock-order"
        instance = self._browser_instance(node_id, instance_id)
        self._write_state(node_id, browser_instances=[instance])
        core_entered = threading.Event()
        release_core = threading.Event()
        browser_calls_start_bridge = threading.Event()
        start_errors: list[BaseException] = []
        browser_errors: list[BaseException] = []
        bridge = self._bridge("bridge-lock-order", node_id, 4372, 24311)
        original_start_bridge = app.start_bridge

        def blocking_prepare(_plan: dict[str, object]) -> list[str]:
            core_entered.set()
            if not release_core.wait(timeout=2):
                raise AssertionError("bridge core preparation was not released")
            return ["fake-core"]

        def complete_plan(plan: dict[str, object], command: list[str]):
            return plan, command

        def publish_bridge(_plan: dict[str, object], _command: list[str]) -> dict[str, object]:
            with app.STATE_LOCK:
                state = app.load_state()
                state["bridges"].append(dict(bridge))
                app.save_state(state)
            return dict(bridge)

        def observed_start_bridge(payload: dict[str, object]) -> dict[str, object]:
            if threading.current_thread().name == "browser-lock-order":
                browser_calls_start_bridge.set()
            return original_start_bridge(payload)

        def start_worker() -> None:
            try:
                app.start_bridge({"node_id": node_id, "core": "auto"})
            except BaseException as error:
                start_errors.append(error)

        def browser_worker() -> None:
            try:
                app.launch_browser({"node_id": node_id, "instance_id": instance_id})
            except BaseException as error:
                browser_errors.append(error)

        with (
            mock.patch.object(app, "start_bridge", side_effect=observed_start_bridge),
            mock.patch.object(app, "_prepare_core_command", side_effect=blocking_prepare),
            mock.patch.object(app, "_complete_bridge_plan", side_effect=complete_plan),
            mock.patch.object(app, "_run_prepared_bridge", side_effect=publish_bridge),
            mock.patch.object(app, "_run_browser_launch", return_value={"ok": True}),
            mock.patch.object(app, "fingerprint_chromium_path", return_value="/fake/Chromium"),
            mock.patch.object(app, "synchronize_process_state"),
        ):
            start_thread = threading.Thread(target=start_worker, name="bridge-lock-order")
            start_thread.start()
            self.assertTrue(core_entered.wait(timeout=2))
            browser_thread = threading.Thread(target=browser_worker, name="browser-lock-order")
            browser_thread.start()
            self.assertTrue(browser_calls_start_bridge.wait(timeout=2))
            lifecycle_lock_free = app.SHUTDOWN_LOCK.acquire(blocking=False)
            if lifecycle_lock_free:
                app.SHUTDOWN_LOCK.release()
            release_core.set()
            start_thread.join(timeout=2)
            browser_thread.join(timeout=2)

        self.assertTrue(lifecycle_lock_free)
        self.assertFalse(start_thread.is_alive())
        self.assertFalse(browser_thread.is_alive())
        self.assertEqual(start_errors, [])
        self.assertEqual(browser_errors, [])

    def test_launch_browser_and_shutdown_share_lifecycle_barrier(self) -> None:
        node_id = "node-browser-shutdown-barrier"
        bridge = self._bridge("bridge-browser-shutdown-barrier", node_id, 4380, 24301)
        instance_id = "browser-shutdown-barrier"
        instance = self._browser_instance(node_id, instance_id)
        self._write_state(node_id, bridges=[bridge], browser_instances=[instance])
        self._activate_bridge(bridge)
        profile_entered = threading.Event()
        release_profile = threading.Event()
        shutdown_started = threading.Event()
        shutdown_done = threading.Event()
        launch_results: list[dict[str, object]] = []
        launch_errors: list[BaseException] = []
        shutdown_errors: list[BaseException] = []
        terminated_pids: list[int] = []

        class FakePopen:
            calls = 0

            def __init__(self, _command: list[str], **_kwargs: object) -> None:
                self.pid = 4381
                FakePopen.calls += 1

            def poll(self) -> None:
                return None

        def controlled_profile(_profile_path: Path) -> None:
            profile_entered.set()
            if not release_profile.wait(timeout=2):
                raise AssertionError("浏览器 Profile 配置未获准继续")

        def launch_worker() -> None:
            try:
                launch_results.append(app.launch_browser({"node_id": node_id, "instance_id": instance_id}))
            except BaseException as error:
                launch_errors.append(error)

        def shutdown_worker() -> None:
            shutdown_started.set()
            try:
                app.shutdown()
            except BaseException as error:
                shutdown_errors.append(error)
            finally:
                shutdown_done.set()

        with (
            mock.patch.object(app, "fingerprint_chromium_path", return_value="/fake/Chromium"),
            mock.patch.object(app, "wait_for_port", return_value=True),
            mock.patch.object(app, "configure_browser_profile_defaults", side_effect=controlled_profile),
            mock.patch.object(app.subprocess, "Popen", FakePopen),
            mock.patch.object(app, "terminate_process", side_effect=lambda process: terminated_pids.append(process.pid)),
        ):
            launch_thread = threading.Thread(target=launch_worker)
            launch_thread.start()
            self.assertTrue(profile_entered.wait(timeout=2))
            lifecycle_barrier_was_free = app.SHUTDOWN_LOCK.acquire(blocking=False)
            if lifecycle_barrier_was_free:
                app.SHUTDOWN_LOCK.release()
            shutdown_thread = threading.Thread(target=shutdown_worker)
            shutdown_thread.start()
            self.assertTrue(shutdown_started.wait(timeout=2))
            try:
                if lifecycle_barrier_was_free:
                    self.assertTrue(shutdown_done.wait(timeout=2))
            finally:
                release_profile.set()
            launch_thread.join(timeout=2)
            shutdown_thread.join(timeout=2)

        self.assertFalse(launch_thread.is_alive())
        self.assertFalse(shutdown_thread.is_alive())
        self.assertFalse(lifecycle_barrier_was_free)
        self.assertEqual(launch_errors, [])
        self.assertEqual(shutdown_errors, [])
        self.assertEqual(len(launch_results), 1)
        self.assertEqual(FakePopen.calls, 1)
        self.assertCountEqual(terminated_pids, [4380, 4381])
        self.assertEqual(app.PROCESS_TABLE, {})
        self.assertEqual(app.LOG_PROXY_TABLE, {})
        self.assertEqual(app.BROWSER_PROCESS_TABLE, {})
        self.assertEqual(app.BROWSER_TABLE, {})

    def test_fingerprint_install_is_single_flight(self) -> None:
        start_barrier = threading.Barrier(3)
        install_barrier = threading.Barrier(2)
        installed = threading.Event()
        counter_lock = threading.Lock()
        results_lock = threading.Lock()
        counters = {"active": 0, "max_active": 0}
        results: list[dict[str, object]] = []
        errors: list[BaseException] = []
        installed_binary = app.fingerprint_chromium_root() / "Chromium.app" / "Contents" / "MacOS" / "Chromium"

        release_payload = [
            {
                "tag_name": "v-test",
                "draft": False,
                "prerelease": False,
                "assets": [
                    {
                        "name": "fingerprint-chromium_macos.dmg",
                        "browser_download_url": "https://example.invalid/fingerprint.dmg",
                    }
                ],
            }
        ]

        def fake_open_https(request: object, *, timeout: int) -> io.BytesIO:
            del timeout
            url = str(getattr(request, "full_url", request))
            if url.startswith(app.FP_CHROMIUM_RELEASES_API):
                return io.BytesIO(json.dumps(release_payload).encode("utf-8"))
            return io.BytesIO(b"fake-dmg")

        def fake_fingerprint_path(force_refresh: bool = False) -> str | None:
            del force_refresh
            return str(installed_binary) if installed.is_set() else None

        def fake_install(_dmg_path: Path, dest_root: Path) -> Path:
            with counter_lock:
                counters["active"] += 1
                counters["max_active"] = max(counters["max_active"], counters["active"])
            try:
                try:
                    install_barrier.wait(timeout=0.5)
                except threading.BrokenBarrierError:
                    pass
                installed.set()
                return dest_root / "Chromium.app"
            finally:
                with counter_lock:
                    counters["active"] -= 1

        def worker() -> None:
            try:
                start_barrier.wait()
                result = app.download_fingerprint_chromium({})
                with results_lock:
                    results.append(result)
            except BaseException as error:
                with results_lock:
                    errors.append(error)

        with (
            mock.patch.object(app, "_open_https_url", side_effect=fake_open_https),
            mock.patch.object(app, "fingerprint_chromium_path", side_effect=fake_fingerprint_path),
            mock.patch.object(app, "_install_from_dmg", side_effect=fake_install),
            mock.patch.object(app, "_verify_macos_binary_arch", return_value=None),
        ):
            threads = [threading.Thread(target=worker) for _ in range(2)]
            for thread in threads:
                thread.start()
            start_barrier.wait()
            for thread in threads:
                thread.join(timeout=2)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(counters["max_active"], 1)
        self.assertEqual(sorted(result["already_installed"] for result in results), [False, True])

    def test_shutdown_serializes_stop_bridge(self) -> None:
        self._assert_public_stop_waits_for_shutdown("bridge")

    def test_shutdown_serializes_stop_all_bridges(self) -> None:
        self._assert_public_stop_waits_for_shutdown("all")

    def test_shutdown_serializes_stop_browser(self) -> None:
        self._assert_public_stop_waits_for_shutdown("browser")

    def test_status_payload_waits_for_shutdown(self) -> None:
        self._assert_sync_payload_waits_for_shutdown("status")

    def test_core_log_payload_waits_for_shutdown(self) -> None:
        self._assert_sync_payload_waits_for_shutdown("core-log")

    def test_stop_bridge_drains_proxy_handler_without_holding_state_lock(self) -> None:
        node_id = "node-public-stop-drain"
        bridge_id = "bridge-public-stop-drain"
        log_id = "request-public-stop-drain"
        bridge = self._bridge(bridge_id, node_id, 4829, 24519)
        self._write_state(node_id, bridges=[bridge])
        app.PROCESS_TABLE[bridge_id] = FakeProcess(4829)
        app.BRIDGE_NODE_TABLE[bridge_id] = node_id
        app.init_bridge_traffic(bridge_id)

        entered = threading.Event()
        drain_entered = threading.Event()
        release = threading.Event()
        handler_done = threading.Event()
        stop_done = threading.Event()
        results: list[dict[str, object]] = []
        errors: list[BaseException] = []

        class FakeClient:
            def shutdown(self, _how: int) -> None:
                drain_entered.set()

            def close(self) -> None:
                return None

        class FakeServer:
            def __init__(self) -> None:
                self.accepted = False
                self.closed = threading.Event()

            def accept(self) -> tuple[FakeClient, tuple[str, int]]:
                if not self.accepted:
                    self.accepted = True
                    return FakeClient(), ("127.0.0.1", 50001)
                self.closed.wait(timeout=5)
                raise OSError("closed")

            def close(self) -> None:
                self.closed.set()

        proxy = app.Socks5LogProxy(
            bridge_id=bridge_id,
            node_id=node_id,
            node_name="Lifecycle Node",
            listen_port=24519,
            upstream_port=25519,
        )
        proxy._server = FakeServer()

        def handler(_client: FakeClient, _address: tuple[str, int]) -> None:
            entered.set()
            try:
                if not release.wait(timeout=5):
                    raise AssertionError("公共停止未进入代理排空")
                app.append_request_log(
                    {
                        "id": log_id,
                        "bridge_id": bridge_id,
                        "status": "open",
                        "upload_bytes": 0,
                        "download_bytes": 0,
                    }
                )
                app.update_request_log(
                    log_id,
                    {"status": "completed", "download_bytes": 23},
                )
                app.add_bridge_traffic(bridge_id, "download_bytes", 23)
            finally:
                handler_done.set()

        with mock.patch.object(proxy, "_handle_client", side_effect=handler):
            proxy._thread = threading.Thread(target=proxy._serve, daemon=True)
            proxy._thread.start()
            self.assertTrue(entered.wait(timeout=1))
            app.LOG_PROXY_TABLE[bridge_id] = proxy

            def run_stop() -> None:
                try:
                    with mock.patch.object(app, "terminate_process", return_value=None):
                        results.append(app.stop_bridge(bridge_id))
                except BaseException as error:
                    errors.append(error)
                finally:
                    stop_done.set()

            worker = threading.Thread(target=run_stop)
            worker.start()
            try:
                self.assertTrue(drain_entered.wait(timeout=1))
                self.assertFalse(stop_done.is_set())
                release.set()
                worker.join(timeout=5)
                self.assertFalse(worker.is_alive())
                self.assertTrue(handler_done.wait(timeout=1))
                self.assertEqual(errors, [])
            finally:
                release.set()
                worker.join(timeout=5)
                handler_done.wait(timeout=1)
                if app.LOG_PROXY_TABLE.get(bridge_id) is proxy:
                    proxy.stop()

        self.assertEqual(results, [{"ok": True, "killed_browsers": 0}])
        saved = app.load_state()
        saved_bridge = saved["bridges"][0]
        self.assertEqual(saved_bridge["status"], "stopped")
        self.assertFalse(saved_bridge["enabled"])
        self.assertEqual(saved_bridge["download_bytes"], 23)
        self.assertEqual(saved_bridge["total_bytes"], 23)
        self.assertEqual(
            saved["request_logs"],
            [
                {
                    "id": log_id,
                    "bridge_id": bridge_id,
                    "status": "completed",
                    "upload_bytes": 0,
                    "download_bytes": 23,
                }
            ],
        )
        self.assertNotIn(bridge_id, app.PROCESS_TABLE)
        self.assertNotIn(bridge_id, app.LOG_PROXY_TABLE)
        self.assertNotIn(bridge_id, app.BRIDGE_NODE_TABLE)
        self.assertEqual(app.TRAFFIC_TABLE, {})

    def test_stop_bridge_queues_stopped_browser_profile_for_auto_upload(self) -> None:
        node_id = "node-stop-browser-sync"
        bridge_id = "bridge-stop-browser-sync"
        instance_id = "browser-stop-browser-sync"
        bridge = self._bridge(bridge_id, node_id, 4830, 24520)
        instance = self._browser_instance(node_id, instance_id)
        instance.update(
            {
                "status": "running",
                "pid": 5830,
                "bridge_id": bridge_id,
                "proxy": "socks5://127.0.0.1:24520",
            }
        )
        self._write_state(node_id, bridges=[bridge], browser_instances=[instance])
        self._activate_bridge(bridge)
        browser_process = FakeProcess(5830)
        app.BROWSER_PROCESS_TABLE[instance_id] = browser_process
        app.BROWSER_TABLE[node_id] = [browser_process]
        sync_engine = mock.Mock()
        queued_snapshots: list[tuple[str, object, object]] = []

        def observe_auto_upload(queued_instance_id: str) -> None:
            saved = next(
                item
                for item in app.load_state()["browser_instances"]
                if item["id"] == queued_instance_id
            )
            queued_snapshots.append(
                (str(saved["status"]), saved.get("pid"), saved.get("last_stopped_at"))
            )

        sync_engine.maybe_auto_upload_instance.side_effect = observe_auto_upload

        with (
            mock.patch.object(app, "terminate_process", return_value=None),
            mock.patch.object(app, "get_sync_engine", return_value=sync_engine),
        ):
            result = app.stop_bridge(bridge_id)

        self.assertEqual(result, {"ok": True, "killed_browsers": 1})
        saved_instance = app.load_state()["browser_instances"][0]
        self.assertEqual(saved_instance["status"], "stopped")
        self.assertIsNone(saved_instance["pid"])
        self.assertIsNotNone(saved_instance["last_stopped_at"])
        self.assertEqual(
            queued_snapshots,
            [("stopped", None, saved_instance["last_stopped_at"])],
        )
        sync_engine.maybe_auto_upload_instance.assert_called_once_with(instance_id)

    def test_stop_bridge_queues_browser_upload_when_process_handle_is_already_gone(self) -> None:
        node_id = "node-stop-browser-stale-state"
        bridge_id = "bridge-stop-browser-stale-state"
        instance_id = "browser-stop-browser-stale-state"
        bridge = self._bridge(bridge_id, node_id, 4831, 24521)
        instance = self._browser_instance(node_id, instance_id)
        instance.update({"status": "running", "pid": 5831, "bridge_id": bridge_id})
        self._write_state(node_id, bridges=[bridge], browser_instances=[instance])
        self._activate_bridge(bridge)
        sync_engine = mock.Mock()

        with (
            mock.patch.object(app, "terminate_process", return_value=None),
            mock.patch.object(app, "get_sync_engine", return_value=sync_engine),
        ):
            result = app.stop_bridge(bridge_id)

        self.assertEqual(result, {"ok": True, "killed_browsers": 0})
        saved_instance = app.load_state()["browser_instances"][0]
        self.assertEqual(saved_instance["status"], "stopped")
        self.assertIsNone(saved_instance["pid"])
        sync_engine.maybe_auto_upload_instance.assert_called_once_with(instance_id)

    def test_stop_all_bridges_queues_each_stopped_browser_profile_once(self) -> None:
        node_id = "node-stop-all-browser-sync"
        bridge_id = "bridge-stop-all-browser-sync"
        bridge = self._bridge(bridge_id, node_id, 4832, 24522)
        instance_ids = ["browser-stop-all-sync-a", "browser-stop-all-sync-b"]
        instances = []
        browser_processes = []
        for offset, instance_id in enumerate(instance_ids):
            instance = self._browser_instance(node_id, instance_id)
            instance.update(
                {
                    "status": "running",
                    "pid": 5832 + offset,
                    "bridge_id": bridge_id,
                    "proxy": "socks5://127.0.0.1:24522",
                }
            )
            instances.append(instance)
            process = FakeProcess(5832 + offset)
            browser_processes.append(process)
            app.BROWSER_PROCESS_TABLE[instance_id] = process
        self._write_state(node_id, bridges=[bridge], browser_instances=instances)
        self._activate_bridge(bridge)
        app.BROWSER_TABLE[node_id] = browser_processes
        sync_engine = mock.Mock()

        with (
            mock.patch.object(app, "terminate_process", return_value=None),
            mock.patch.object(app, "get_sync_engine", return_value=sync_engine),
        ):
            result = app.stop_all_bridges()

        self.assertEqual(result["killed_browsers"], 2)
        saved_instances = app.load_state()["browser_instances"]
        self.assertTrue(all(item["status"] == "stopped" for item in saved_instances))
        self.assertEqual(sync_engine.maybe_auto_upload_instance.call_count, 2)
        sync_engine.maybe_auto_upload_instance.assert_has_calls(
            [mock.call(instance_id) for instance_id in instance_ids],
            any_order=True,
        )

    def test_stop_bridge_retains_live_traffic_until_state_save_succeeds(self) -> None:
        node_id = "node-stop-save-retry"
        bridge_id = "bridge-stop-save-retry"
        bridge = self._bridge(bridge_id, node_id, 4831, 24521)
        self._write_state(node_id, bridges=[bridge])
        self._activate_bridge(bridge, 40, 60)
        state_tmp_path = app.STATE_PATH.with_suffix(".tmp")
        original_path_open = Path.open

        def disk_full_on_state_tmp(path: Path, *args: object, **kwargs: object):
            mode = str(args[0]) if args else str(kwargs.get("mode", "r"))
            if path == state_tmp_path and "w" in mode:
                raise OSError(errno.ENOSPC, "No space left on device")
            return original_path_open(path, *args, **kwargs)

        with (
            mock.patch.object(app, "terminate_process", return_value=None),
            mock.patch.object(app, "stop_browser_processes_for_node", return_value=0),
            mock.patch.object(Path, "open", new=disk_full_on_state_tmp),
        ):
            with self.assertRaises(OSError) as caught:
                app.stop_bridge(bridge_id)

        self.assertEqual(caught.exception.errno, errno.ENOSPC)
        self.assertEqual(
            app.bridge_traffic_snapshot(bridge_id),
            {"upload_bytes": 40, "download_bytes": 60, "total_bytes": 100},
        )
        self.assertEqual(app.load_state()["bridges"][0]["status"], "running")

        with (
            mock.patch.object(app, "terminate_process", return_value=None),
            mock.patch.object(app, "stop_browser_processes_for_node", return_value=0),
        ):
            app.stop_bridge(bridge_id)

        saved = app.load_state()["bridges"][0]
        self.assertEqual(saved["status"], "stopped")
        self.assertEqual(
            (saved["upload_bytes"], saved["download_bytes"], saved["total_bytes"]),
            (40, 60, 100),
        )
        self.assertNotIn(bridge_id, app.TRAFFIC_TABLE)

    def test_stop_all_retains_successful_traffic_when_another_stop_fails(self) -> None:
        node_id = "node-stop-all-partial"
        bridge_ids = ["bridge-stop-all-a", "bridge-stop-all-b"]
        bridges = [
            self._bridge(bridge_ids[0], node_id, 4832, 24522),
            self._bridge(bridge_ids[1], node_id, 4833, 24523),
        ]
        self._write_state(node_id, bridges=bridges)
        self._activate_bridge(bridges[0], 40, 60)
        self._activate_bridge(bridges[1], 30, 20)
        runtime_order = list(set(app.PROCESS_TABLE) | set(app.LOG_PROXY_TABLE))
        successful_id, failed_id = runtime_order
        failed_process = app.PROCESS_TABLE[failed_id]
        attempts: list[int] = []

        def partially_failing_terminate(process: FakeProcess) -> None:
            attempts.append(process.pid)
            if process is failed_process:
                raise RuntimeError("partial Bridge termination failure")

        with mock.patch.object(app, "terminate_process", side_effect=partially_failing_terminate):
            with self.assertRaisesRegex(RuntimeError, "partial Bridge termination failure"):
                app.stop_all_bridges()

        self.assertNotIn(successful_id, app.TRAFFIC_TABLE)
        self.assertIn(failed_id, app.TRAFFIC_TABLE)
        first_state = {item["id"]: item for item in app.load_state()["bridges"]}
        self.assertEqual(first_state[successful_id]["status"], "stopped")
        self.assertEqual(first_state[successful_id]["total_bytes"], 100 if successful_id == bridge_ids[0] else 50)
        self.assertEqual(first_state[failed_id]["status"], "running")

        with mock.patch.object(app, "terminate_process", return_value=None):
            app.shutdown()

        final_state = {item["id"]: item for item in app.load_state()["bridges"]}
        expected_totals = {bridge_ids[0]: 100, bridge_ids[1]: 50}
        for bridge_id in bridge_ids:
            self.assertEqual(final_state[bridge_id]["status"], "stopped")
            self.assertEqual(final_state[bridge_id]["total_bytes"], expected_totals[bridge_id])
        self.assertEqual(app.TRAFFIC_TABLE, {})

    def test_stop_all_retries_traffic_only_entry_after_state_save_failure(self) -> None:
        node_id = "node-stop-all-save-retry"
        bridge_id = "bridge-stop-all-save-retry"
        bridge = self._bridge(bridge_id, node_id, 4842, 24532)
        self._write_state(node_id, bridges=[bridge])
        self._activate_bridge(bridge, 40, 60)
        state_tmp_path = app.STATE_PATH.with_suffix(".tmp")
        original_path_open = Path.open

        def disk_full_on_state_tmp(path: Path, *args: object, **kwargs: object):
            mode = str(args[0]) if args else str(kwargs.get("mode", "r"))
            if path == state_tmp_path and "w" in mode:
                raise OSError(errno.ENOSPC, "No space left on device")
            return original_path_open(path, *args, **kwargs)

        with (
            mock.patch.object(app, "terminate_process", return_value=None),
            mock.patch.object(Path, "open", new=disk_full_on_state_tmp),
        ):
            with self.assertRaisesRegex(RuntimeError, "No space left on device") as caught:
                app.stop_all_bridges()

        self.assertIsInstance(caught.exception.__cause__, OSError)
        self.assertEqual(app.load_state()["bridges"][0]["status"], "running")
        self.assertNotIn(bridge_id, app.PROCESS_TABLE)
        self.assertNotIn(bridge_id, app.LOG_PROXY_TABLE)
        self.assertEqual(app.bridge_traffic_snapshot(bridge_id)["total_bytes"], 100)

        with mock.patch.object(app, "terminate_process", return_value=None):
            app.stop_all_bridges()
        saved = app.load_state()["bridges"][0]
        self.assertEqual(saved["status"], "stopped")
        self.assertEqual(saved["total_bytes"], 100)
        self.assertNotIn(bridge_id, app.TRAFFIC_TABLE)

    def test_status_drains_stale_proxy_before_taking_state_lock(self) -> None:
        node_id = "node-status-drain"
        bridge_id = "bridge-status-drain"
        log_id = "request-status-drain"
        bridge = self._bridge(bridge_id, node_id, 4834, 24524)
        self._write_state(node_id, bridges=[bridge])

        class ExitedProcess(FakeProcess):
            def poll(self) -> int:
                return 1

        handler_acquired_state = threading.Event()

        class StateLockDrainProxy:
            def is_alive(self) -> bool:
                return True

            def stop(self) -> None:
                def finish_handler() -> None:
                    app.append_request_log(
                        {
                            "id": log_id,
                            "bridge_id": bridge_id,
                            "status": "completed",
                            "upload_bytes": 0,
                            "download_bytes": 7,
                        }
                    )
                    app.add_bridge_traffic(bridge_id, "download_bytes", 7)
                    handler_acquired_state.set()

                handler = threading.Thread(target=finish_handler)
                handler.start()
                if not handler_acquired_state.wait(timeout=0.3):
                    raise RuntimeError("proxy handler blocked on STATE_LOCK")
                handler.join(timeout=1)

        app.PROCESS_TABLE[bridge_id] = ExitedProcess(4834)
        app.BRIDGE_NODE_TABLE[bridge_id] = node_id
        app.LOG_PROXY_TABLE[bridge_id] = StateLockDrainProxy()
        app.init_bridge_traffic(bridge_id, 40, 60)

        with mock.patch.object(app, "terminate_process", return_value=None):
            status = app.status_payload()

        saved_bridge = {item["id"]: item for item in status["bridges"]}[bridge_id]
        self.assertTrue(handler_acquired_state.is_set())
        self.assertEqual(saved_bridge["status"], "stopped")
        self.assertEqual(saved_bridge["total_bytes"], 107)
        self.assertEqual(status["request_logs"][0]["id"], log_id)
        self.assertNotIn(bridge_id, app.LOG_PROXY_TABLE)
        self.assertNotIn(bridge_id, app.TRAFFIC_TABLE)

    def test_status_retains_stale_traffic_when_state_save_fails(self) -> None:
        node_id = "node-status-save-retry"
        bridge_id = "bridge-status-save-retry"
        bridge = self._bridge(bridge_id, node_id, 4835, 24525)
        self._write_state(node_id, bridges=[bridge])

        class ExitedProcess(FakeProcess):
            def poll(self) -> int:
                return 1

        app.PROCESS_TABLE[bridge_id] = ExitedProcess(4835)
        app.BRIDGE_NODE_TABLE[bridge_id] = node_id
        app.LOG_PROXY_TABLE[bridge_id] = FakeProxy()
        app.init_bridge_traffic(bridge_id, 40, 60)
        state_tmp_path = app.STATE_PATH.with_suffix(".tmp")
        original_path_open = Path.open

        def disk_full_on_state_tmp(path: Path, *args: object, **kwargs: object):
            mode = str(args[0]) if args else str(kwargs.get("mode", "r"))
            if path == state_tmp_path and "w" in mode:
                raise OSError(errno.ENOSPC, "No space left on device")
            return original_path_open(path, *args, **kwargs)

        with (
            mock.patch.object(app, "terminate_process", return_value=None),
            mock.patch.object(Path, "open", new=disk_full_on_state_tmp),
        ):
            with self.assertRaisesRegex(RuntimeError, "No space left on device") as caught:
                app.status_payload()

        self.assertIsInstance(caught.exception.__cause__, OSError)
        self.assertEqual(getattr(caught.exception.__cause__, "errno", None), errno.ENOSPC)
        self.assertEqual(app.bridge_traffic_snapshot(bridge_id)["total_bytes"], 100)
        with mock.patch.object(app, "terminate_process", return_value=None):
            status = app.status_payload()
        saved_bridge = {item["id"]: item for item in status["bridges"]}[bridge_id]
        self.assertEqual(saved_bridge["status"], "stopped")
        self.assertEqual(saved_bridge["total_bytes"], 100)
        self.assertNotIn(bridge_id, app.TRAFFIC_TABLE)

    def test_status_commits_successful_stale_cleanup_before_reporting_failure(self) -> None:
        node_id = "node-status-partial-cleanup"
        successful_id = "bridge-status-partial-success"
        failed_id = "bridge-status-partial-failure"
        bridges = [
            self._bridge(successful_id, node_id, 4838, 24528),
            self._bridge(failed_id, node_id, 4839, 24529),
        ]
        self._write_state(node_id, bridges=bridges)

        class ExitedProcess(FakeProcess):
            def poll(self) -> int:
                return 1

        class FailingProxy(FakeProxy):
            def stop(self) -> None:
                raise RuntimeError("stale proxy drain failed")

        app.PROCESS_TABLE.update(
            {
                successful_id: ExitedProcess(4838),
                failed_id: ExitedProcess(4839),
            }
        )
        app.BRIDGE_NODE_TABLE.update({successful_id: node_id, failed_id: node_id})
        app.LOG_PROXY_TABLE.update({successful_id: FakeProxy(), failed_id: FailingProxy()})
        app.init_bridge_traffic(successful_id, 40, 60)
        app.init_bridge_traffic(failed_id, 30, 20)

        with mock.patch.object(app, "terminate_process", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "stale proxy drain failed"):
                app.status_payload()

        first_state = {item["id"]: item for item in app.load_state()["bridges"]}
        self.assertEqual(first_state[successful_id]["status"], "stopped")
        self.assertEqual(first_state[successful_id]["total_bytes"], 100)
        self.assertNotIn(successful_id, app.TRAFFIC_TABLE)
        self.assertEqual(first_state[failed_id]["status"], "running")
        self.assertEqual(app.bridge_traffic_snapshot(failed_id)["total_bytes"], 50)

        app.LOG_PROXY_TABLE[failed_id] = FakeProxy()
        with mock.patch.object(app, "terminate_process", return_value=None):
            status = app.status_payload()
        final_state = {item["id"]: item for item in status["bridges"]}
        self.assertEqual(final_state[failed_id]["status"], "stopped")
        self.assertEqual(final_state[failed_id]["total_bytes"], 50)
        self.assertEqual(app.TRAFFIC_TABLE, {})

    def test_status_does_not_publish_stopped_before_stale_runtime_is_drained(self) -> None:
        node_id = "node-status-exit-after-snapshot"
        bridge_id = "bridge-status-exit-after-snapshot"
        bridge = self._bridge(bridge_id, node_id, 4840, 24530)
        self._write_state(node_id, bridges=[bridge])

        class JustExitedProcess(FakeProcess):
            def poll(self) -> int:
                return 1

        process = JustExitedProcess(4840)
        proxy = FakeProxy()
        app.PROCESS_TABLE[bridge_id] = process
        app.BRIDGE_NODE_TABLE[bridge_id] = node_id
        app.LOG_PROXY_TABLE[bridge_id] = proxy
        app.init_bridge_traffic(bridge_id, 40, 60)

        with mock.patch.object(app, "bridge_runtime_is_active", return_value=True):
            first_status = app.status_payload()

        first_bridge = {item["id"]: item for item in first_status["bridges"]}[bridge_id]
        self.assertEqual(first_bridge["status"], "running")
        self.assertIs(app.PROCESS_TABLE.get(bridge_id), process)
        self.assertIs(app.LOG_PROXY_TABLE.get(bridge_id), proxy)
        self.assertEqual(app.bridge_traffic_snapshot(bridge_id)["total_bytes"], 100)

        with mock.patch.object(app, "terminate_process", return_value=None):
            second_status = app.status_payload()
        second_bridge = {item["id"]: item for item in second_status["bridges"]}[bridge_id]
        self.assertEqual(second_bridge["status"], "stopped")
        self.assertEqual(second_bridge["total_bytes"], 100)
        self.assertNotIn(bridge_id, app.PROCESS_TABLE)
        self.assertNotIn(bridge_id, app.LOG_PROXY_TABLE)
        self.assertNotIn(bridge_id, app.TRAFFIC_TABLE)

    def test_status_does_not_wait_for_bound_proxy_port_under_state_lock(self) -> None:
        node_id = "node-status-no-port-wait"
        bridge_id = "bridge-status-no-port-wait"
        bridge = self._bridge(bridge_id, node_id, 4841, 24531)
        self._write_state(node_id, bridges=[bridge])
        self._activate_bridge(bridge, 40, 60)

        with mock.patch.object(
            app,
            "wait_for_port",
            side_effect=AssertionError("status waited for a socket while STATE_LOCK was held"),
        ):
            status = app.status_payload()

        saved_bridge = {item["id"]: item for item in status["bridges"]}[bridge_id]
        self.assertEqual(saved_bridge["status"], "running")
        self.assertEqual(saved_bridge["total_bytes"], 100)

    def test_shutdown_owns_registered_node_test_while_probe_is_blocked(self) -> None:
        node_id = "node-test-shutdown-owner"
        self._write_state(node_id)
        probe_entered = threading.Event()
        release_probe = threading.Event()
        terminate_entered = threading.Event()
        terminate_calls: list[int] = []
        test_errors: list[BaseException] = []
        shutdown_errors: list[BaseException] = []

        class TestProcess(FakeProcess):
            pass

        process = TestProcess(4836)

        def blocking_probe(_port: int) -> dict[str, object]:
            probe_entered.set()
            if not release_probe.wait(timeout=3):
                raise AssertionError("node probe was never released")
            return {
                "ok": True,
                "latency_ms": 1,
                "target": "example.test:80/",
                "status_code": 204,
                "message": "通信正常",
            }

        def terminate(item: FakeProcess) -> None:
            terminate_calls.append(item.pid)
            terminate_entered.set()

        def run_test() -> None:
            try:
                app.test_node({"node_id": node_id, "core": "auto"})
            except BaseException as error:
                test_errors.append(error)

        def run_shutdown() -> None:
            try:
                app.shutdown()
            except BaseException as error:
                shutdown_errors.append(error)

        with (
            mock.patch.object(app, "generate_config", return_value={"test": True}),
            mock.patch.object(app, "core_command", return_value=["fake-core"]),
            mock.patch.object(app, "next_free_port", return_value=24526),
            mock.patch.object(app, "wait_for_port", return_value=True),
            mock.patch.object(app.subprocess, "Popen", return_value=process),
            mock.patch.object(app, "socks5_http_probe", side_effect=blocking_probe),
            mock.patch.object(app, "terminate_process", side_effect=terminate),
        ):
            test_thread = threading.Thread(target=run_test)
            test_thread.start()
            self.assertTrue(probe_entered.wait(timeout=1))
            test_process_table = getattr(app, "TEST_PROCESS_TABLE", {})
            self.assertEqual(list(test_process_table.values()), [process])

            shutdown_thread = threading.Thread(target=run_shutdown)
            shutdown_thread.start()
            self.assertTrue(terminate_entered.wait(timeout=1))
            shutdown_thread.join(timeout=1)
            self.assertFalse(shutdown_thread.is_alive())
            release_probe.set()
            test_thread.join(timeout=2)

        self.assertFalse(test_thread.is_alive())
        self.assertEqual(test_errors, [])
        self.assertEqual(shutdown_errors, [])
        self.assertEqual(terminate_calls, [4836])
        self.assertEqual(getattr(app, "TEST_PROCESS_TABLE", {}), {})
        self.assertNotIn(node_id, app.load_state()["tests"])

    def test_node_core_resolution_holds_no_lifecycle_or_state_lock(self) -> None:
        node_id = "node-test-core-outside-locks"
        self._write_state(node_id)
        core_entered = threading.Event()
        release_core = threading.Event()
        errors: list[BaseException] = []
        results: list[dict[str, object]] = []
        process = FakeProcess(4844)

        def blocking_core_command(_core: str, _path: Path) -> list[str]:
            core_entered.set()
            if not release_core.wait(timeout=2):
                raise AssertionError("node test core resolution was not released")
            return ["fake-core", "run", "-c", "placeholder"]

        def worker() -> None:
            try:
                results.append(app.test_node({"node_id": node_id, "core": "auto"}))
            except BaseException as error:
                errors.append(error)

        with (
            mock.patch.object(app, "core_command", side_effect=blocking_core_command),
            mock.patch.object(app, "generate_config", return_value={"test": True}),
            mock.patch.object(app, "next_free_port", return_value=24534),
            mock.patch.object(app, "wait_for_port", return_value=True),
            mock.patch.object(app.subprocess, "Popen", return_value=process),
            mock.patch.object(
                app,
                "socks5_http_probe",
                return_value={
                    "ok": True,
                    "latency_ms": 1,
                    "target": "example.test:80/",
                    "status_code": 204,
                    "message": "通信正常",
                },
            ),
            mock.patch.object(app, "terminate_process", return_value=None),
        ):
            thread = threading.Thread(target=worker)
            thread.start()
            self.assertTrue(core_entered.wait(timeout=2))
            lifecycle_lock_free = app.SHUTDOWN_LOCK.acquire(blocking=False)
            if lifecycle_lock_free:
                app.SHUTDOWN_LOCK.release()
            state_lock_free = app.STATE_LOCK.acquire(blocking=False)
            if state_lock_free:
                app.STATE_LOCK.release()
            release_core.set()
            thread.join(timeout=2)

        self.assertTrue(lifecycle_lock_free)
        self.assertTrue(state_lock_free)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["ok"])

    def test_node_result_save_is_serialized_with_shutdown_start(self) -> None:
        node_id = "node-test-save-shutdown-race"
        self._write_state(node_id)
        process = FakeProcess(4843)
        final_state_acquire_entered = threading.Event()
        release_final_state_acquire = threading.Event()
        shutdown_done = threading.Event()
        test_errors: list[BaseException] = []
        shutdown_errors: list[BaseException] = []
        saved_after_shutdown = False
        original_state_lock = app.STATE_LOCK
        original_save_state = app.save_state

        class GateLock:
            def __init__(self, delegate: threading.RLock) -> None:
                self.delegate = delegate
                self.target_acquisitions = 0

            def __enter__(self) -> "GateLock":
                if threading.current_thread().name == "node-test-save-worker":
                    self.target_acquisitions += 1
                    if self.target_acquisitions == 2:
                        final_state_acquire_entered.set()
                        if not release_final_state_acquire.wait(timeout=2):
                            raise AssertionError("final node-test state acquisition was never released")
                self.delegate.acquire()
                return self

            def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
                self.delegate.release()

        def observed_save_state(state: dict[str, object]) -> None:
            nonlocal saved_after_shutdown
            tests = state.get("tests")
            if isinstance(tests, dict) and node_id in tests and app.SHUTTING_DOWN.is_set():
                saved_after_shutdown = True
            original_save_state(state)

        def run_test() -> None:
            try:
                app.test_node({"node_id": node_id, "core": "auto"})
            except BaseException as error:
                test_errors.append(error)

        def run_shutdown() -> None:
            try:
                app.shutdown()
            except BaseException as error:
                shutdown_errors.append(error)
            finally:
                shutdown_done.set()

        gate_lock = GateLock(original_state_lock)
        app.STATE_LOCK = gate_lock
        try:
            with (
                mock.patch.object(app, "generate_config", return_value={"test": True}),
                mock.patch.object(app, "core_command", return_value=["fake-core"]),
                mock.patch.object(app, "next_free_port", return_value=24533),
                mock.patch.object(app, "wait_for_port", return_value=True),
                mock.patch.object(app.subprocess, "Popen", return_value=process),
                mock.patch.object(
                    app,
                    "socks5_http_probe",
                    return_value={
                        "ok": True,
                        "latency_ms": 1,
                        "target": "example.test:80/",
                        "status_code": 204,
                        "message": "通信正常",
                    },
                ),
                mock.patch.object(app, "terminate_process", return_value=None),
                mock.patch.object(app, "save_state", side_effect=observed_save_state),
            ):
                test_thread = threading.Thread(target=run_test, name="node-test-save-worker")
                test_thread.start()
                self.assertTrue(final_state_acquire_entered.wait(timeout=1))
                shutdown_thread = threading.Thread(target=run_shutdown, name="node-test-save-shutdown")
                shutdown_thread.start()
                shutdown_completed_before_release = shutdown_done.wait(timeout=0.2)
                release_final_state_acquire.set()
                test_thread.join(timeout=2)
                shutdown_thread.join(timeout=2)
        finally:
            release_final_state_acquire.set()
            app.STATE_LOCK = original_state_lock

        self.assertFalse(test_thread.is_alive())
        self.assertFalse(shutdown_thread.is_alive())
        self.assertEqual(test_errors, [])
        self.assertEqual(shutdown_errors, [])
        self.assertFalse(saved_after_shutdown)
        self.assertFalse(shutdown_completed_before_release)

    def test_shutdown_retains_node_test_process_when_termination_fails(self) -> None:
        test_process_table = getattr(app, "TEST_PROCESS_TABLE", None)
        self.assertIsNotNone(test_process_table)
        assert test_process_table is not None
        process = FakeProcess(4837)
        test_process_table["test-retry"] = process
        attempts = 0

        def flaky_terminate(item: FakeProcess) -> None:
            nonlocal attempts
            self.assertIs(item, process)
            attempts += 1
            if attempts == 1:
                raise RuntimeError("node test terminate failed")

        with mock.patch.object(app, "terminate_process", side_effect=flaky_terminate):
            with self.assertRaisesRegex(RuntimeError, "node test terminate failed"):
                app.shutdown()
            self.assertIs(test_process_table.get("test-retry"), process)
            app.shutdown()

        self.assertEqual(attempts, 2)
        self.assertEqual(test_process_table, {})

    def test_node_rejects_new_process_after_shutdown_begins(self) -> None:
        node_id = "node-test-after-shutdown"
        self._write_state(node_id)
        app.shutdown()
        with mock.patch.object(app.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(RuntimeError, "正在退出"):
                app.test_node({"node_id": node_id, "core": "auto"})
        popen.assert_not_called()

    def test_shutdown_waits_for_proxy_clients_before_persisting_traffic(self) -> None:
        node_id = "node-final-traffic"
        bridge_id = "bridge-final-traffic"
        bridge = self._bridge(bridge_id, node_id, 4830, 24520)
        self._write_state(node_id, bridges=[bridge])
        app.PROCESS_TABLE[bridge_id] = FakeProcess(4830)
        app.BRIDGE_NODE_TABLE[bridge_id] = node_id
        app.init_bridge_traffic(bridge_id)

        entered = threading.Event()
        drain_entered = threading.Event()
        release = threading.Event()
        stop_done = threading.Event()
        errors: list[BaseException] = []

        class FakeClient:
            def shutdown(self, _how: int) -> None:
                drain_entered.set()

            def close(self) -> None:
                return None

        class FakeServer:
            def __init__(self) -> None:
                self.accepted = False
                self.closed = threading.Event()

            def accept(self) -> tuple[FakeClient, tuple[str, int]]:
                if not self.accepted:
                    self.accepted = True
                    return FakeClient(), ("127.0.0.1", 50000)
                self.closed.wait(timeout=2)
                raise OSError("closed")

            def close(self) -> None:
                self.closed.set()

        proxy = app.Socks5LogProxy(
            bridge_id=bridge_id,
            node_id=node_id,
            node_name="Lifecycle Node",
            listen_port=24520,
            upstream_port=25520,
        )
        proxy._server = FakeServer()

        def handler(_client: FakeClient, _address: tuple[str, int]) -> None:
            entered.set()
            release.wait(timeout=5)
            app.add_bridge_traffic(bridge_id, "download_bytes", 19)

        with mock.patch.object(proxy, "_handle_client", side_effect=handler):
            proxy._thread = threading.Thread(target=proxy._serve, daemon=True)
            proxy._thread.start()
            self.assertTrue(entered.wait(timeout=1))
            app.LOG_PROXY_TABLE[bridge_id] = proxy

            def run_shutdown() -> None:
                try:
                    with mock.patch.object(app, "terminate_process", return_value=None):
                        app.shutdown()
                except BaseException as error:
                    errors.append(error)
                finally:
                    stop_done.set()

            worker = threading.Thread(target=run_shutdown)
            worker.start()
            try:
                self.assertTrue(drain_entered.wait(timeout=1))
                self.assertFalse(stop_done.is_set())
            finally:
                release.set()
            worker.join(timeout=2)

        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        saved = app.load_state()["bridges"][0]
        self.assertEqual(saved["download_bytes"], 19)
        self.assertEqual(saved["total_bytes"], 19)
        self.assertEqual(app.TRAFFIC_TABLE, {})

    def test_shutdown_persists_successful_traffic_and_retries_failed_bridge(self) -> None:
        node_id = "node-shutdown-bridge-retry"
        successful_id = "bridge-shutdown-success"
        failed_id = "bridge-shutdown-retry"
        bridges = [
            self._bridge(successful_id, node_id, 4541, 24310),
            self._bridge(failed_id, node_id, 4542, 24311),
        ]
        self._write_state(node_id, bridges=bridges)
        self._activate_bridge(bridges[0], 11, 22)
        self._activate_bridge(bridges[1], 33, 44)
        browser_process = FakeProcess(4543)
        app.BROWSER_PROCESS_TABLE["browser-shutdown-success"] = browser_process
        app.BROWSER_TABLE[node_id] = [browser_process]
        attempts: dict[int, int] = {}
        completed_pids: list[int] = []
        attempts_lock = threading.Lock()

        def flaky_terminate(process: FakeProcess) -> None:
            with attempts_lock:
                attempts[process.pid] = attempts.get(process.pid, 0) + 1
                attempt = attempts[process.pid]
                completed_pids.append(process.pid)
            if process.pid == 4542 and attempt == 1:
                raise RuntimeError("Bridge 终止失败")

        with mock.patch.object(app, "terminate_process", side_effect=flaky_terminate):
            with self.assertRaises(RuntimeError) as caught:
                app.shutdown()

            first_state = {bridge["id"]: bridge for bridge in app.load_state()["bridges"]}
            self.assertCountEqual(completed_pids, [4541, 4542, 4543])
            self.assertEqual(
                (
                    first_state[successful_id]["upload_bytes"],
                    first_state[successful_id]["download_bytes"],
                    first_state[successful_id]["total_bytes"],
                ),
                (11, 22, 33),
            )
            self.assertEqual(first_state[failed_id]["total_bytes"], 0)
            self.assertNotIn(successful_id, app.PROCESS_TABLE)
            self.assertNotIn(successful_id, app.LOG_PROXY_TABLE)
            self.assertNotIn(successful_id, app.BRIDGE_NODE_TABLE)
            self.assertNotIn(successful_id, app.TRAFFIC_TABLE)
            self.assertIn(failed_id, app.PROCESS_TABLE)
            self.assertNotIn(failed_id, app.LOG_PROXY_TABLE)
            self.assertEqual(app.BRIDGE_NODE_TABLE.get(failed_id), node_id)
            self.assertEqual(app.TRAFFIC_TABLE[failed_id]["total_bytes"], 77)
            self.assertEqual(app.BROWSER_PROCESS_TABLE, {})
            self.assertRegex(str(caught.exception), "关闭.*失败")

            status = app.status_payload()
            status_bridges = {bridge["id"]: bridge for bridge in status["bridges"]}
            self.assertEqual(status_bridges[successful_id]["status"], "stopped")
            self.assertIsNone(status_bridges[successful_id]["pid"])
            self.assertTrue(status_bridges[successful_id]["enabled"])
            self.assertEqual(
                (
                    status_bridges[successful_id]["upload_bytes"],
                    status_bridges[successful_id]["download_bytes"],
                    status_bridges[successful_id]["total_bytes"],
                ),
                (11, 22, 33),
            )

            app.shutdown()

        final_state = {bridge["id"]: bridge for bridge in app.load_state()["bridges"]}
        self.assertEqual(
            (
                final_state[failed_id]["upload_bytes"],
                final_state[failed_id]["download_bytes"],
                final_state[failed_id]["total_bytes"],
            ),
            (33, 44, 77),
        )
        self.assertEqual(attempts[4542], 2)
        self.assertEqual(app.PROCESS_TABLE, {})
        self.assertEqual(app.BRIDGE_NODE_TABLE, {})
        self.assertEqual(app.LOG_PROXY_TABLE, {})
        self.assertEqual(app.TRAFFIC_TABLE, {})

    def test_shutdown_persists_bridge_traffic_and_retries_failed_browser(self) -> None:
        node_id = "node-shutdown-browser-retry"
        bridge_id = "bridge-shutdown-browser-retry"
        bridge = self._bridge(bridge_id, node_id, 4550, 24312)
        self._write_state(node_id, bridges=[bridge])
        self._activate_bridge(bridge, 55, 66)
        successful_browser = FakeProcess(4551)
        failed_browser = FakeProcess(4552)
        app.BROWSER_PROCESS_TABLE.update(
            {"browser-shutdown-ok": successful_browser, "browser-shutdown-retry": failed_browser}
        )
        app.BROWSER_TABLE[node_id] = [successful_browser, failed_browser]
        attempts: dict[int, int] = {}
        attempts_lock = threading.Lock()

        def flaky_terminate(process: FakeProcess) -> None:
            with attempts_lock:
                attempts[process.pid] = attempts.get(process.pid, 0) + 1
                attempt = attempts[process.pid]
            if process.pid == failed_browser.pid and attempt == 1:
                raise RuntimeError("浏览器终止失败")

        with mock.patch.object(app, "terminate_process", side_effect=flaky_terminate):
            with self.assertRaises(RuntimeError) as caught:
                app.shutdown()

            first_bridge = app.load_state()["bridges"][0]
            self.assertEqual(
                (first_bridge["upload_bytes"], first_bridge["download_bytes"], first_bridge["total_bytes"]),
                (55, 66, 121),
            )
            self.assertNotIn("browser-shutdown-ok", app.BROWSER_PROCESS_TABLE)
            self.assertIs(app.BROWSER_PROCESS_TABLE["browser-shutdown-retry"], failed_browser)
            self.assertEqual(app.BROWSER_TABLE[node_id], [failed_browser])
            self.assertEqual(app.PROCESS_TABLE, {})
            self.assertEqual(app.LOG_PROXY_TABLE, {})
            self.assertEqual(app.TRAFFIC_TABLE, {})
            self.assertRegex(str(caught.exception), "关闭.*失败")

            app.shutdown()

        self.assertEqual(attempts[failed_browser.pid], 2)
        self.assertEqual(app.BROWSER_PROCESS_TABLE, {})
        self.assertEqual(app.BROWSER_TABLE, {})

    def test_shutdown_stops_independent_processes_concurrently(self) -> None:
        node_id = "node-concurrent-shutdown"
        bridge_ids = ["bridge-shutdown-a", "bridge-shutdown-b"]
        bridges = [
            self._bridge(bridge_ids[0], node_id, 4401, 24380),
            self._bridge(bridge_ids[1], node_id, 4402, 24381),
        ]
        self._write_state(node_id, bridges=bridges)
        app.PROCESS_TABLE.update(
            {
                bridge_ids[0]: FakeProcess(4401),
                bridge_ids[1]: FakeProcess(4402),
            }
        )
        app.LOG_PROXY_TABLE.update({bridge_id: FakeProxy() for bridge_id in bridge_ids})
        app.init_bridge_traffic(bridge_ids[0], 11, 22)
        app.init_bridge_traffic(bridge_ids[1], 33, 44)

        browser_processes = [FakeProcess(4501), FakeProcess(4502)]
        app.BROWSER_TABLE[node_id] = list(browser_processes)
        app.BROWSER_PROCESS_TABLE.update(
            {"browser-shutdown-a": browser_processes[0], "browser-shutdown-b": browser_processes[1]}
        )

        termination_barrier = threading.Barrier(4)
        counter_lock = threading.Lock()
        counters = {"active": 0, "max_active": 0}
        terminated_pids: list[int] = []
        process_lock_observations: list[bool] = []
        barrier_broken = threading.Event()

        def fake_terminate(process: FakeProcess) -> None:
            process_lock_available = app.PROCESS_LOCK.acquire(blocking=False)
            process_lock_observations.append(process_lock_available)
            if process_lock_available:
                app.PROCESS_LOCK.release()
            with counter_lock:
                counters["active"] += 1
                counters["max_active"] = max(counters["max_active"], counters["active"])
                terminated_pids.append(process.pid)
            try:
                try:
                    termination_barrier.wait(timeout=0.5)
                except threading.BrokenBarrierError:
                    barrier_broken.set()
            finally:
                with counter_lock:
                    counters["active"] -= 1

        with mock.patch.object(app, "terminate_process", side_effect=fake_terminate):
            app.shutdown()
            first_call_pids = list(terminated_pids)
            first_state = app.load_state()
            app.shutdown()
            second_state = app.load_state()

        self.assertFalse(barrier_broken.is_set())
        self.assertEqual(counters["max_active"], 4)
        self.assertTrue(all(process_lock_observations))
        self.assertCountEqual(first_call_pids, [4401, 4402, 4501, 4502])
        self.assertEqual(terminated_pids, first_call_pids)
        self.assertEqual(app.PROCESS_TABLE, {})
        self.assertEqual(app.BROWSER_PROCESS_TABLE, {})
        self.assertEqual(app.BROWSER_TABLE, {})

        traffic = {
            bridge["id"]: (bridge["upload_bytes"], bridge["download_bytes"], bridge["total_bytes"])
            for bridge in first_state["bridges"]
        }
        self.assertEqual(traffic[bridge_ids[0]], (11, 22, 33))
        self.assertEqual(traffic[bridge_ids[1]], (33, 44, 77))
        self.assertEqual(second_state["bridges"], first_state["bridges"])

    def test_shutdown_signal_is_single_flight_during_recursive_delivery(self) -> None:
        shutdown_calls = 0

        def recursive_shutdown() -> None:
            nonlocal shutdown_calls
            shutdown_calls += 1
            if shutdown_calls == 1:
                app.handle_shutdown_signal(app.signal.SIGINT, None)

        with mock.patch.object(app, "shutdown", side_effect=recursive_shutdown):
            with self.assertRaises(SystemExit) as caught:
                app.handle_shutdown_signal(app.signal.SIGTERM, None)

        self.assertEqual(caught.exception.code, 0)
        self.assertEqual(shutdown_calls, 1)
        claimed = getattr(app, "SHUTDOWN_SIGNAL_CLAIMED", threading.Event())
        self.assertTrue(claimed.is_set())

    def test_shutdown_signal_claim_persists_when_first_shutdown_raises(self) -> None:
        failure = RuntimeError("shutdown persistence failed")
        with mock.patch.object(app, "shutdown", side_effect=failure) as shutdown_mock:
            with self.assertRaises(RuntimeError) as caught:
                app.handle_shutdown_signal(app.signal.SIGTERM, None)
            self.assertIs(caught.exception, failure)
            try:
                app.handle_shutdown_signal(app.signal.SIGINT, None)
            except RuntimeError as error:
                self.fail(f"second signal restarted shutdown: {error}")

        self.assertEqual(shutdown_mock.call_count, 1)


if __name__ == "__main__":
    unittest.main()
