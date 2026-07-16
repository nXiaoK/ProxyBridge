from __future__ import annotations

import io
import json
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from unittest import mock

import app


LOOPBACK_HOST = "127.0.0.1"
LAN_LISTEN_HOST = "0.0.0.0"


def make_handler(method: str, path: str, headers: dict[str, str], body: bytes = b"") -> app.AppHandler:
    handler = object.__new__(app.AppHandler)
    handler.command = method
    handler.path = path
    handler.requestline = f"{method} {path} HTTP/1.1"
    handler.request_version = "HTTP/1.1"
    handler.headers = Message()
    for name, value in headers.items():
        handler.headers[name] = value
    if app.BACKEND_INSTANCE_HEADER not in handler.headers:
        handler.headers[app.BACKEND_INSTANCE_HEADER] = app.management_api_token()
    handler.rfile = io.BytesIO(body)
    handler.wfile = io.BytesIO()
    return handler


def response_status(handler: app.AppHandler) -> int:
    raw_headers, _body = handler.wfile.getvalue().split(b"\r\n\r\n", 1)
    status_line = raw_headers.split(b"\r\n", 1)[0]
    return int(status_line.split(b" ", 2)[1])


def result_settings(result: dict[str, object]) -> dict[str, object]:
    for key in ("network", "settings"):
        nested = result.get(key)
        if isinstance(nested, dict):
            return nested
    return result


class FakeRunningProcess:
    def __init__(self, pid: int) -> None:
        self.pid = pid

    def poll(self) -> None:
        return None


class FakeRunningProxy:
    def __init__(self, bridge_id: str, listen_host: str, *, fail_on: str | None = None) -> None:
        self.bridge_id = bridge_id
        self.node_id = f"node-{bridge_id}"
        self.node_name = bridge_id
        self.listen_port = 21080
        self.upstream_port = 22080
        self.listen_host = listen_host
        self.fail_on = fail_on
        self.rebind_calls: list[str] = []

    def is_alive(self) -> bool:
        return True

    def rebind(self, listen_host: str) -> None:
        self.rebind_calls.append(listen_host)
        if listen_host == self.fail_on:
            raise OSError(f"cannot bind {listen_host}")
        self.listen_host = listen_host


class LanSettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_work_root = app.WORK_ROOT
        self.old_runtime_root = app.RUNTIME_ROOT
        self.old_state_path = app.STATE_PATH
        self.old_process_table = dict(app.PROCESS_TABLE)
        self.old_bridge_node_table = dict(app.BRIDGE_NODE_TABLE)
        self.old_log_proxy_table = dict(app.LOG_PROXY_TABLE)
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        app.WORK_ROOT = root / "work"
        app.RUNTIME_ROOT = app.WORK_ROOT / "runtime"
        app.STATE_PATH = app.WORK_ROOT / "state.json"
        app.ensure_dirs()
        self.lan_addresses_patch = mock.patch.object(
            app,
            "lan_ipv4_addresses",
            return_value=["192.168.1.10"],
        )
        self.lan_addresses_patch.start()
        with app.PROCESS_LOCK:
            app.PROCESS_TABLE.clear()
            app.BRIDGE_NODE_TABLE.clear()
            app.LOG_PROXY_TABLE.clear()

    def tearDown(self) -> None:
        self.lan_addresses_patch.stop()
        with app.PROCESS_LOCK:
            app.PROCESS_TABLE.clear()
            app.PROCESS_TABLE.update(self.old_process_table)
            app.BRIDGE_NODE_TABLE.clear()
            app.BRIDGE_NODE_TABLE.update(self.old_bridge_node_table)
            app.LOG_PROXY_TABLE.clear()
            app.LOG_PROXY_TABLE.update(self.old_log_proxy_table)
        app.WORK_ROOT = self.old_work_root
        app.RUNTIME_ROOT = self.old_runtime_root
        app.STATE_PATH = self.old_state_path
        self.temp_dir.cleanup()

    def save_network_state(self, allow_lan: bool, bridge_ids: tuple[str, ...] = ()) -> None:
        state = app.empty_state()
        state["settings"] = {
            "allow_lan": allow_lan,
            "pool_token": "keep-this-pool-token",
            "webdav": {"url": "https://dav.example.test"},
            "unrelated": {"keep": True},
        }
        state["bridges"] = [
            {
                "id": bridge_id,
                "node_id": f"node-{bridge_id}",
                "node_name": bridge_id,
                "status": "running",
                "enabled": True,
                "port": 21080 + index,
            }
            for index, bridge_id in enumerate(bridge_ids)
        ]
        app.save_state(state)

    def install_running_proxies(
        self,
        listen_host: str,
        *,
        fail_second_on: str | None = None,
    ) -> tuple[FakeRunningProxy, FakeRunningProxy]:
        first = FakeRunningProxy("bridge-a", listen_host)
        second = FakeRunningProxy("bridge-b", listen_host, fail_on=fail_second_on)
        with app.PROCESS_LOCK:
            app.LOG_PROXY_TABLE[first.bridge_id] = first
            app.LOG_PROXY_TABLE[second.bridge_id] = second
            app.PROCESS_TABLE[first.bridge_id] = FakeRunningProcess(4101)
            app.PROCESS_TABLE[second.bridge_id] = FakeRunningProcess(4102)
            app.BRIDGE_NODE_TABLE[first.bridge_id] = first.node_id
            app.BRIDGE_NODE_TABLE[second.bridge_id] = second.node_id
        return first, second

    def test_allow_lan_defaults_false_and_normalizes_fail_closed(self) -> None:
        self.assertIs(app.empty_state()["settings"]["allow_lan"], False)

        cases = (
            ({}, False),
            ({"settings": None}, False),
            ({"settings": {}}, False),
            ({"settings": {"allow_lan": False}}, False),
            ({"settings": {"allow_lan": True}}, True),
            ({"settings": {"allow_lan": "true"}}, False),
            ({"settings": {"allow_lan": 1}}, False),
        )
        for raw_state, expected in cases:
            with self.subTest(raw_state=raw_state):
                normalized = app._normalize_loaded_state(raw_state)
                self.assertIs(normalized["settings"]["allow_lan"], expected)

    def test_network_settings_payload_reports_persisted_listener_mode(self) -> None:
        self.save_network_state(False)
        local = app.network_settings_payload()
        self.assertIs(local["allow_lan"], False)
        self.assertEqual(local["listen_host"], LOOPBACK_HOST)

        state = app.load_state()
        state["settings"]["allow_lan"] = True
        app.save_state(state)
        shared = app.network_settings_payload()
        self.assertIs(shared["allow_lan"], True)
        self.assertEqual(shared["listen_host"], LAN_LISTEN_HOST)

    def test_network_settings_payload_publishes_connectable_lan_urls(self) -> None:
        self.save_network_state(True, ("bridge-a",))

        payload = app.network_settings_payload()

        self.assertEqual(payload["primary_lan_address"], "192.168.1.10")
        self.assertEqual(payload["proxy_urls"][0]["socks_url"], "socks5://192.168.1.10:21080")
        self.assertEqual(payload["proxy_urls"][0]["http_url"], "http://192.168.1.10:21080")

    def test_lan_address_discovery_prefers_physical_interface_and_ignores_tun_benchmark_ip(self) -> None:
        ifconfig_output = """\
en5: flags=8863<UP,BROADCAST,RUNNING> mtu 1500
\tinet 192.168.10.184 netmask 0xffffff00 broadcast 192.168.10.255
utun6: flags=8051<UP,POINTOPOINT,RUNNING> mtu 4000
\tinet 198.18.0.1 --> 198.18.0.1 netmask 0xfffe0000
"""
        hostname_infos = [
            (app.socket.AF_INET, app.socket.SOCK_STREAM, 6, "", ("10.211.55.2", 0)),
            (app.socket.AF_INET, app.socket.SOCK_STREAM, 6, "", ("198.18.0.1", 0)),
        ]
        with (
            mock.patch.object(app.subprocess, "run", return_value=mock.Mock(stdout=ifconfig_output)),
            mock.patch.object(app.socket, "socket", side_effect=OSError("no route probe")),
            mock.patch.object(app.socket, "getaddrinfo", return_value=hostname_infos),
        ):
            addresses = app._discover_lan_ipv4_addresses()

        self.assertEqual(addresses, ["192.168.10.184", "10.211.55.2"])

    def test_port_availability_check_does_not_enable_address_reuse(self) -> None:
        context_socket = mock.MagicMock()
        bound_socket = context_socket.__enter__.return_value
        with mock.patch.object(app.socket, "socket", return_value=context_socket):
            self.assertTrue(app.is_port_free(21080, LAN_LISTEN_HOST))

        bound_socket.bind.assert_called_once_with((LAN_LISTEN_HOST, 21080))
        bound_socket.setsockopt.assert_not_called()

    def test_update_network_settings_persists_value_and_preserves_other_settings(self) -> None:
        self.save_network_state(False)

        update_result = app.update_network_settings({"allow_lan": True})
        result = result_settings(update_result)

        self.assertIs(result["allow_lan"], True)
        self.assertEqual(update_result["rebound_count"], 0)
        self.assertIs(update_result["connections_interrupted"], False)
        saved = app.load_state()["settings"]
        self.assertIs(saved["allow_lan"], True)
        self.assertEqual(saved["pool_token"], "keep-this-pool-token")
        self.assertEqual(saved["webdav"], {"url": "https://dav.example.test"})
        self.assertEqual(saved["unrelated"], {"keep": True})

    def test_update_network_settings_rejects_non_boolean_values(self) -> None:
        for payload in ({}, {"allow_lan": 1}, {"allow_lan": "true"}, {"allow_lan": None}):
            with self.subTest(payload=payload), self.assertRaisesRegex(ValueError, "布尔值"):
                app.update_network_settings(payload)

    def test_socks5_log_proxy_constructor_uses_explicit_listen_host(self) -> None:
        proxy = app.Socks5LogProxy(
            bridge_id="bridge-test",
            node_id="node-test",
            node_name="test node",
            listen_host=LAN_LISTEN_HOST,
            listen_port=21080,
            upstream_port=22080,
        )

        self.assertEqual(proxy.listen_host, LAN_LISTEN_HOST)
        fake_socket = mock.Mock()
        with (
            mock.patch.object(app.socket, "socket", return_value=fake_socket),
            mock.patch.object(app.threading, "Thread") as thread_class,
        ):
            proxy.start()

        fake_socket.bind.assert_called_once_with((LAN_LISTEN_HOST, 21080))
        thread_class.return_value.start.assert_called_once_with()

    def test_update_network_settings_rebinds_running_proxies_in_both_directions(self) -> None:
        for original_allow_lan, requested_allow_lan, old_host, new_host in (
            (False, True, LOOPBACK_HOST, LAN_LISTEN_HOST),
            (True, False, LAN_LISTEN_HOST, LOOPBACK_HOST),
        ):
            with self.subTest(requested_allow_lan=requested_allow_lan):
                with app.PROCESS_LOCK:
                    app.LOG_PROXY_TABLE.clear()
                self.save_network_state(original_allow_lan, ("bridge-a", "bridge-b"))
                first, second = self.install_running_proxies(old_host)

                update_result = app.update_network_settings({"allow_lan": requested_allow_lan})
                result = result_settings(update_result)

                self.assertIs(result["allow_lan"], requested_allow_lan)
                self.assertEqual(update_result["rebound_count"], 2)
                self.assertIs(update_result["connections_interrupted"], True)
                self.assertEqual(first.rebind_calls, [new_host])
                self.assertEqual(second.rebind_calls, [new_host])
                self.assertEqual(first.listen_host, new_host)
                self.assertEqual(second.listen_host, new_host)
                self.assertIs(app.load_state()["settings"]["allow_lan"], requested_allow_lan)

    def test_save_failure_rolls_running_proxies_back_to_persisted_mode(self) -> None:
        self.save_network_state(False, ("bridge-a", "bridge-b"))
        first, second = self.install_running_proxies(LOOPBACK_HOST)

        with (
            mock.patch.object(app, "save_state", side_effect=OSError("disk full")),
            self.assertRaisesRegex(OSError, "disk full"),
        ):
            app.update_network_settings({"allow_lan": True})

        self.assertEqual(first.rebind_calls, [LAN_LISTEN_HOST, LOOPBACK_HOST])
        self.assertEqual(second.rebind_calls, [LAN_LISTEN_HOST, LOOPBACK_HOST])
        self.assertEqual(first.listen_host, LOOPBACK_HOST)
        self.assertEqual(second.listen_host, LOOPBACK_HOST)
        self.assertIs(app.load_state()["settings"]["allow_lan"], False)

    def test_post_commit_save_error_reconciles_runtime_to_committed_setting(self) -> None:
        original_save = app.save_state
        for original_allow_lan, requested_allow_lan, old_host, committed_host in (
            (False, True, LOOPBACK_HOST, LAN_LISTEN_HOST),
            (True, False, LAN_LISTEN_HOST, LOOPBACK_HOST),
        ):
            with self.subTest(requested_allow_lan=requested_allow_lan):
                with app.PROCESS_LOCK:
                    app.PROCESS_TABLE.clear()
                    app.BRIDGE_NODE_TABLE.clear()
                    app.LOG_PROXY_TABLE.clear()
                self.save_network_state(original_allow_lan, ("bridge-a", "bridge-b"))
                first, second = self.install_running_proxies(old_host)

                def commit_then_fail(state: dict) -> None:
                    original_save(state)
                    raise OSError("post-commit fsync failure")

                with (
                    mock.patch.object(app, "save_state", side_effect=commit_then_fail),
                    self.assertRaisesRegex(OSError, "post-commit"),
                ):
                    app.update_network_settings({"allow_lan": requested_allow_lan})

                self.assertEqual(first.listen_host, committed_host)
                self.assertEqual(second.listen_host, committed_host)
                self.assertEqual(first.rebind_calls, [committed_host])
                self.assertEqual(second.rebind_calls, [committed_host])
                self.assertIs(app.load_state()["settings"]["allow_lan"], requested_allow_lan)

    def test_failed_rebind_rolls_back_proxies_and_persisted_setting_in_both_directions(self) -> None:
        for original_allow_lan, requested_allow_lan, old_host, new_host in (
            (False, True, LOOPBACK_HOST, LAN_LISTEN_HOST),
            (True, False, LAN_LISTEN_HOST, LOOPBACK_HOST),
        ):
            with self.subTest(requested_allow_lan=requested_allow_lan):
                with app.PROCESS_LOCK:
                    app.LOG_PROXY_TABLE.clear()
                self.save_network_state(original_allow_lan, ("bridge-a", "bridge-b"))
                first, second = self.install_running_proxies(old_host, fail_second_on=new_host)

                with self.assertRaisesRegex(RuntimeError, "bind|监听|重绑"):
                    app.update_network_settings({"allow_lan": requested_allow_lan})

                self.assertEqual(first.rebind_calls, [new_host, old_host])
                self.assertEqual(second.rebind_calls, [new_host])
                self.assertEqual(first.listen_host, old_host)
                self.assertEqual(second.listen_host, old_host)
                self.assertIs(app.load_state()["settings"]["allow_lan"], original_allow_lan)

    def test_proxy_stop_closes_tracked_upstream_sockets(self) -> None:
        proxy = app.Socks5LogProxy(
            bridge_id="bridge-stop",
            node_id="node-stop",
            node_name="stop",
            listen_port=21080,
            upstream_port=22080,
        )
        upstream = mock.MagicMock()
        proxy._upstream_sockets.add(upstream)

        proxy.stop()

        upstream.shutdown.assert_called_once_with(app.socket.SHUT_RDWR)
        upstream.close.assert_called_once_with()
        self.assertEqual(proxy._upstream_sockets, set())

    def test_rebind_restores_old_listener_when_stop_raises(self) -> None:
        proxy = app.Socks5LogProxy(
            bridge_id="bridge-rebind",
            node_id="node-rebind",
            node_name="rebind",
            listen_port=21080,
            upstream_port=22080,
        )
        with (
            mock.patch.object(proxy, "stop", side_effect=RuntimeError("drain timeout")),
            mock.patch.object(proxy, "_start_after_rebind") as restart,
            self.assertRaisesRegex(RuntimeError, "drain timeout"),
        ):
            proxy.rebind(LAN_LISTEN_HOST)

        self.assertEqual(proxy.listen_host, LOOPBACK_HOST)
        restart.assert_called_once_with()

    def test_proxy_client_acl_allows_only_loopback_and_rfc1918(self) -> None:
        for address in ("127.0.0.1", "10.1.2.3", "172.16.4.5", "192.168.1.20"):
            with self.subTest(address=address):
                self.assertTrue(app.proxy_client_allowed(address))
        for address in ("8.8.8.8", "100.64.0.1", "169.254.1.2", "198.18.0.1", "invalid"):
            with self.subTest(address=address):
                self.assertFalse(app.proxy_client_allowed(address))

    def test_management_api_still_rejects_lan_host_when_proxy_sharing_is_enabled(self) -> None:
        self.save_network_state(True)
        body = json.dumps({"allow_lan": False}).encode("utf-8")
        cases = (
            ("GET", b""),
            ("POST", body),
        )
        for method, request_body in cases:
            with self.subTest(method=method):
                handler = make_handler(
                    method,
                    "/api/settings/network",
                    {
                        "Host": "192.168.1.20:8787",
                        "Content-Type": "application/json",
                        "Content-Length": str(len(request_body)),
                    },
                    request_body,
                )

                getattr(handler, f"do_{method}")()

                self.assertEqual(response_status(handler), 403)

    def test_local_network_settings_api_dispatches_boolean_payload(self) -> None:
        body = json.dumps({"allow_lan": True}).encode("utf-8")
        handler = make_handler(
            "POST",
            "/api/settings/network",
            {
                "Host": "127.0.0.1:8787",
                "Content-Type": "application/json",
                "Content-Length": str(len(body)),
            },
            body,
        )
        result = {
            "ok": True,
            "network": {"allow_lan": True},
            "rebound_count": 0,
            "connections_interrupted": False,
        }

        with mock.patch.object(app, "update_network_settings", return_value=result) as update:
            handler.do_POST()

        self.assertEqual(response_status(handler), 200)
        update.assert_called_once_with({"allow_lan": True})


if __name__ == "__main__":
    unittest.main()
