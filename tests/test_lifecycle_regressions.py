from __future__ import annotations

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
        self._old_traffic_table = {key: dict(value) for key, value in app.TRAFFIC_TABLE.items()}
        self._old_fingerprint_cache = dict(app.FINGERPRINT_CHROMIUM_CACHE)
        self._was_shutting_down = app.SHUTTING_DOWN.is_set()

        root = Path(self._temp_dir.name)
        app.WORK_ROOT = root / "work"
        app.RUNTIME_ROOT = app.WORK_ROOT / "runtime"
        app.STATE_PATH = app.WORK_ROOT / "state.json"
        app.PROCESS_TABLE.clear()
        app.BRIDGE_NODE_TABLE.clear()
        app.BROWSER_TABLE.clear()
        app.BROWSER_PROCESS_TABLE.clear()
        app.LOG_PROXY_TABLE.clear()
        app.TRAFFIC_TABLE.clear()
        app.SHUTTING_DOWN.clear()
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
        app.TRAFFIC_TABLE.clear()
        app.TRAFFIC_TABLE.update(self._old_traffic_table)
        app.FINGERPRINT_CHROMIUM_CACHE.clear()
        app.FINGERPRINT_CHROMIUM_CACHE.update(self._old_fingerprint_cache)
        app.SHUTTING_DOWN.clear()
        if self._was_shutting_down:
            app.SHUTTING_DOWN.set()
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

        def fake_urlopen(request: object, timeout: int) -> io.BytesIO:
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
            mock.patch.object(app.urllib.request, "urlopen", side_effect=fake_urlopen),
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


if __name__ == "__main__":
    unittest.main()
