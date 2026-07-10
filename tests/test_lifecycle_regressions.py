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

    def test_running_browser_instance_cannot_be_launched_twice(self) -> None:
        node_id = "node-browser-single-flight"
        bridge_id = "bridge-browser-single-flight"
        bridge = self._bridge(bridge_id, node_id, 4200, 24280)
        self._write_state(node_id, bridges=[bridge])
        app.PROCESS_TABLE[bridge_id] = FakeProcess(4200)
        app.LOG_PROXY_TABLE[bridge_id] = FakeProxy()
        app.init_bridge_traffic(bridge_id)

        class FakePopen:
            calls: list[tuple[list[str], dict[str, object]]] = []

            def __init__(self, command: list[str], **kwargs: object) -> None:
                self.pid = 4300 + len(self.calls)
                self.calls.append((command, kwargs))

            def poll(self) -> None:
                return None

        with (
            mock.patch.object(app, "fingerprint_chromium_path", return_value="/fake/Chromium"),
            mock.patch.object(app, "wait_for_port", return_value=True),
            mock.patch.object(app.subprocess, "Popen", FakePopen),
        ):
            first = app.launch_browser({"node_id": node_id})
            with self.assertRaisesRegex(ValueError, "已在运行"):
                app.launch_browser({"node_id": node_id, "instance_id": first["instance_id"]})

        self.assertEqual(len(FakePopen.calls), 1)
        instance = app.find_browser_instance(app.load_state(), first["instance_id"])
        self.assertEqual(instance["pid"], first["pid"])
        self.assertEqual(instance["launch_count"], 1)

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
