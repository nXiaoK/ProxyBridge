from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import app


class FakeGateway:
    def __init__(
        self,
        *,
        listen_port: int = app.SMART_GATEWAY_DEFAULT_PORT,
        listen_host: str = app.LOCAL_PROXY_LISTEN_HOST,
        max_clients: int = app.SMART_GATEWAY_MAX_CLIENTS,
        strategy: str = "health",
        region: str = "",
        query: str = "",
        security: dict | None = None,
    ) -> None:
        self.listen_port = listen_port
        self.listen_host = listen_host
        self.strategy = strategy
        self.region = region
        self.query = query
        self.capacity_limit = max_clients
        self.max_clients = max_clients
        self.security = app.normalize_lan_security(security)
        self.alive = False
        self.stop_calls = 0
        self.start_calls = 0
        self._stop = threading.Event()

    def start(self) -> None:
        self.start_calls += 1
        self.alive = True

    def _start_after_rebind(self) -> None:
        self.start()

    def stop(self) -> None:
        self.stop_calls += 1
        self.alive = False

    def is_alive(self) -> bool:
        return self.alive

    def settings_snapshot(self) -> dict:
        return {
            "strategy": self.strategy,
            "region": self.region,
            "query": self.query,
            "listen_host": self.listen_host,
            "listen_port": self.listen_port,
        }

    def update_gateway_settings(self, *, strategy: str, region: str, query: str) -> None:
        self.strategy = strategy
        self.region = region
        self.query = query

    def security_snapshot(self) -> dict:
        return dict(self.security)

    def update_security(self, security: dict, *, disconnect_lan: bool = False) -> int:
        self.security = app.normalize_lan_security(security)
        return 0

    def rebind(self, listen_host: str) -> None:
        self.listen_host = listen_host


class SmartGatewayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_work_root = app.WORK_ROOT
        self.old_runtime_root = app.RUNTIME_ROOT
        self.old_state_path = app.STATE_PATH
        self.old_gateway_table = dict(app.SMART_GATEWAY_TABLE)
        self.temp_dir = tempfile.TemporaryDirectory()
        app.configure_work_root(Path(self.temp_dir.name) / "work")
        app.ensure_dirs()
        app.SHUTTING_DOWN.clear()
        with app.SMART_GATEWAY_LOCK:
            app.SMART_GATEWAY_TABLE.clear()

    def tearDown(self) -> None:
        with app.SMART_GATEWAY_LOCK:
            app.SMART_GATEWAY_TABLE.clear()
            app.SMART_GATEWAY_TABLE.update(self.old_gateway_table)
        app.configure_work_root(self.old_work_root)
        app.RUNTIME_ROOT = self.old_runtime_root
        app.STATE_PATH = self.old_state_path
        app.SHUTTING_DOWN.clear()
        self.temp_dir.cleanup()

    @staticmethod
    def gateway(strategy: str = "health") -> app.SmartGatewayProxy:
        return app.SmartGatewayProxy(listen_port=21000, strategy=strategy)

    def test_gateway_rejects_empty_pool(self) -> None:
        proxy = self.gateway()
        with (
            mock.patch.object(proxy, "_gateway_candidates", return_value=[]),
            self.assertRaisesRegex(RuntimeError, "没有可用代理"),
        ):
            proxy._connect_upstream_for_socks_request(b"request")

    def test_gateway_uses_selected_strategy_and_falls_back_before_tunnel(self) -> None:
        proxy = self.gateway("latency")
        raw_first = {"id": "first", "core_port": 23001}
        raw_second = {"id": "second", "core_port": 23002}
        public_first = {"id": "first", "latency_ms": 10, "health_score": 90}
        public_second = {"id": "second", "latency_ms": 20, "health_score": 80}
        connected = mock.Mock(name="connected-socket")

        with (
            mock.patch.object(
                proxy,
                "_gateway_candidates",
                return_value=[(raw_first, public_first), (raw_second, public_second)],
            ),
            mock.patch.object(
                proxy,
                "_connect_to_gateway_member",
                side_effect=[OSError("first down"), (connected, b"reply")],
            ) as connect,
            mock.patch.object(app, "pick_pool_member", wraps=app.pick_pool_member) as pick,
        ):
            result = proxy._connect_upstream_for_socks_request(b"request")

        self.assertEqual(result, (connected, b"reply"))
        self.assertEqual([call.args[0]["id"] for call in connect.call_args_list], ["first", "second"])
        self.assertEqual([call.args[1] for call in pick.call_args_list], ["latency", "latency"])

    def test_gateway_records_core_connect_failure(self) -> None:
        proxy = self.gateway()
        with (
            mock.patch.object(app.socket, "create_connection", side_effect=OSError("core offline")),
            mock.patch.object(proxy, "_record_gateway_health") as record,
            self.assertRaisesRegex(OSError, "core offline"),
        ):
            proxy._connect_to_gateway_member({"id": "offline", "core_port": 23001}, b"request")

        record.assert_called_once()
        self.assertEqual(record.call_args.kwargs["ok"], False)
        self.assertIn("core offline", str(record.call_args.kwargs["error"]))

    def test_gateway_port_conflicts_with_bridge_or_external_listener(self) -> None:
        state = app.empty_state()
        state["bridges"] = [{"id": "bridge-a", "status": "running", "port": 21000}]
        self.assertEqual(
            app._smart_gateway_port_conflict(state, 21000, app.LOCAL_PROXY_LISTEN_HOST),
            "端口 21000 已被本地代理占用",
        )

        with mock.patch.object(app, "is_port_free", return_value=False):
            conflict = app._smart_gateway_port_conflict(
                app.empty_state(),
                21001,
                app.LOCAL_PROXY_LISTEN_HOST,
            )
        self.assertEqual(conflict, "端口 21001 已被其他进程占用")

    def test_start_and_stop_persist_gateway_lifecycle(self) -> None:
        created: list[FakeGateway] = []

        def factory(**kwargs) -> FakeGateway:
            proxy = FakeGateway(**kwargs)
            created.append(proxy)
            return proxy

        with (
            mock.patch.object(app, "docker_mode", return_value=False),
            mock.patch.object(app, "synchronize_process_state"),
            mock.patch.object(app, "_smart_gateway_port_conflict", return_value=None),
            mock.patch.object(app, "SmartGatewayProxy", side_effect=factory),
            mock.patch.object(app, "mark_sync_local_change"),
        ):
            started = app.start_smart_gateway(
                {"port": 21005, "strategy": "round_robin", "max_connections": 12}
            )
            stopped = app.stop_smart_gateway()

        self.assertTrue(started["running"])
        self.assertEqual(started["strategy"], "round_robin")
        self.assertFalse(stopped["running"])
        self.assertEqual(created[0].stop_calls, 1)
        self.assertFalse(app.load_state()["settings"]["smart_gateway"]["enabled"])
        self.assertNotIn(app.SMART_GATEWAY_ID, app.SMART_GATEWAY_TABLE)

    def test_running_setting_save_failure_restores_persisted_runtime(self) -> None:
        state = app.empty_state()
        state["settings"]["smart_gateway"] = app.normalize_smart_gateway_settings(
            {
                "enabled": True,
                "port": 21000,
                "strategy": "health",
                "region": "old-region",
                "max_connections": 8,
            }
        )
        app.save_state(state)
        proxy = FakeGateway(
            listen_port=21000,
            strategy="health",
            region="old-region",
            max_clients=8,
        )
        proxy.start()
        app.SMART_GATEWAY_TABLE[app.SMART_GATEWAY_ID] = proxy

        with (
            mock.patch.object(app, "_smart_gateway_port_conflict", return_value=None),
            mock.patch.object(app, "_persist_smart_gateway_settings", side_effect=OSError("disk full")),
            self.assertRaisesRegex(OSError, "disk full"),
        ):
            app.update_smart_gateway_settings(
                {
                    "port": 21001,
                    "strategy": "latency",
                    "region": "new-region",
                    "max_connections": 16,
                }
            )

        self.assertTrue(proxy.is_alive())
        self.assertEqual(proxy.listen_port, 21000)
        self.assertEqual(proxy.strategy, "health")
        self.assertEqual(proxy.region, "old-region")
        self.assertEqual(proxy.capacity_limit, 8)

    def test_lan_gateway_status_uses_connectable_addresses(self) -> None:
        state = app.empty_state()
        state["settings"]["allow_lan"] = True
        state["settings"]["smart_gateway"] = app.normalize_smart_gateway_settings(
            {"enabled": True, "port": 21000}
        )

        payload = app.smart_gateway_status_payload(
            state,
            lan_addresses=["192.168.1.20", "10.0.0.8"],
        )

        self.assertEqual(payload["socks_url"], "socks5://192.168.1.20:21000")
        self.assertEqual(
            payload["http_urls"],
            ["http://192.168.1.20:21000", "http://10.0.0.8:21000"],
        )
        self.assertNotIn("0.0.0.0", payload["socks_url"])


if __name__ == "__main__":
    unittest.main()
