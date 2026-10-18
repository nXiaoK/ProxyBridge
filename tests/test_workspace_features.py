from __future__ import annotations

import io
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from unittest import mock

import app


def handler_for(path: str, *, authenticated: bool = True) -> app.AppHandler:
    handler = object.__new__(app.AppHandler)
    handler.command = "GET"
    handler.path = path
    handler.requestline = f"GET {path} HTTP/1.1"
    handler.request_version = "HTTP/1.1"
    handler.headers = Message()
    handler.headers["Host"] = "127.0.0.1:8787"
    if authenticated:
        handler.headers[app.BACKEND_INSTANCE_HEADER] = app.management_api_token()
    handler.rfile = io.BytesIO()
    handler.wfile = io.BytesIO()
    return handler


class WorkspaceFeatureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.previous_root = app.WORK_ROOT
        self.temp = tempfile.TemporaryDirectory()
        app.configure_work_root(Path(self.temp.name))
        app.ensure_dirs()
        app.SHUTTING_DOWN.clear()
        self.raw = "\n".join([
            "vless://11111111-1111-1111-1111-111111111111@hk.example.com:443#HK-fast",
            "vless://22222222-2222-2222-2222-222222222222@sg.example.com:443#SG-slow",
            "vless://33333333-3333-3333-3333-333333333333@ph.example.com:443#PH-fast",
        ])

    def tearDown(self) -> None:
        app.configure_work_root(self.previous_root)
        app.SHUTTING_DOWN.clear()
        self.temp.cleanup()

    def imported(self, **values: object) -> dict:
        metadata = values.pop("_fetch_metadata", None)
        with mock.patch.object(app, "mark_sync_local_change"):
            return app.import_subscription({"name": "Example", "url": "https://example.com/sub", "raw": self.raw, **values}, fetch_metadata=metadata)

    def test_regions_include_flags_chinese_contiguous_and_no_substring_code(self) -> None:
        for name, expected in [("PH-01", "菲律宾"), ("🇮🇩01", "印度尼西亚"), ("香港01", "香港"),
                               ("India Mumbai", "印度"), ("BR12", "巴西"), ("status", "未分类")]:
            with self.subTest(name=name):
                self.assertEqual(app.node_region(name), expected)
        self.assertGreaterEqual(len(app.REGION_CATALOG), 40)
        self.assertEqual(app.region_locale("菲律宾")[0], "Asia/Manila")

    def test_userinfo_ignores_invalid_and_unknown_values(self) -> None:
        self.assertEqual(app.parse_subscription_userinfo("upload=12; download=34; total=100; expire=200; secret=1"),
                         {"upload": 12, "download": 34, "total": 100, "expire": 200})
        self.assertEqual(app.parse_subscription_userinfo("upload=-1; total=unknown; download=0"), {"download": 0})
        self.assertEqual(app.parse_subscription_userinfo(None), {})

    def test_fetch_reads_usage_header_for_both_200_and_304(self) -> None:
        for status in (200, 304):
            with self.subTest(status=status):
                headers = Message()
                headers["Content-Type"] = "text/plain; charset=utf-8"
                headers["subscription-userinfo"] = "upload=12; total=1000; expire=2000000000"
                response = mock.Mock(status=status, headers=headers)
                response.read1.side_effect = [self.raw.encode(), b""]
                connection, timer = mock.Mock(), mock.Mock()
                with mock.patch.object(app, "_public_subscription_addresses", return_value=(app.urllib.parse.urlsplit("https://example.test/sub"), [])), \
                        mock.patch.object(app, "_open_subscription_response", return_value=(connection, response, timer)):
                    fetched = app.fetch_subscription_response("https://example.test/sub")
                self.assertEqual(fetched["usage"], {"upload": 12, "total": 1000, "expire": 2000000000})
                self.assertEqual(fetched["not_modified"], status == 304)
                connection.close.assert_called_once()
                timer.cancel.assert_called_once()

    def test_preview_is_read_only_with_include_and_exclude(self) -> None:
        app.save_state(app.empty_state())
        original = app.STATE_PATH.read_bytes()
        preview = app.preview_subscription({"raw": self.raw, "filter_include": "fast", "filter_exclude": "ph"})
        self.assertEqual((preview["total"], preview["matched"], preview["excluded"]), (3, 1, 2))
        self.assertEqual(preview["nodes"][0]["region_code"], "HK")
        self.assertEqual(app.STATE_PATH.read_bytes(), original)
        self.assertNotIn("raw", preview["nodes"][0])
        self.assertNotIn("settings", preview["nodes"][0])

    def test_client_cannot_supply_server_owned_subscription_usage(self) -> None:
        with mock.patch.object(app, "mark_sync_local_change"):
            result = app.import_subscription({"name": "Client", "raw": self.raw,
                                               "_fetch_metadata": {"etag": "fake", "usage": {"total": 999}}})
        self.assertEqual(result["subscription"]["usage"], {})
        self.assertEqual(result["subscription"]["etag"], "")

    def test_regex_validation_precedes_mutation(self) -> None:
        self.imported()
        original = app.STATE_PATH.read_bytes()
        for pattern in ["[", "(a+)+$", "(a|aa)+$", r"(a)\1"]:
            with self.subTest(pattern=pattern), self.assertRaises(ValueError):
                self.imported(filter_include=pattern, filter_regex=True)
        self.assertEqual(app.STATE_PATH.read_bytes(), original)
        preview = app.preview_subscription({"raw": self.raw, "filter_include": "^(HK|PH)-", "filter_regex": True})
        self.assertEqual(preview["matched"], 2)
        preview = app.preview_subscription({"raw": self.raw, "filter_include": "^[A-Z]{1,3}-fast$", "filter_regex": True})
        self.assertEqual(preview["matched"], 2)

    def test_filter_rules_and_usage_survive_refresh_and_304(self) -> None:
        imported = self.imported(filter_include="fast", _fetch_metadata={"usage": {"download": 10, "total": 100}})
        subscription_id = imported["subscription"]["id"]
        self.assertEqual(imported["subscription"]["node_count"], 2)
        fetched = {"text": self.raw, "etag": "version2", "not_modified": False}
        with mock.patch.object(app, "fetch_subscription_response", return_value=fetched), mock.patch.object(app, "mark_sync_local_change"):
            refreshed = app.refresh_subscription(subscription_id)
        self.assertEqual(refreshed["subscription"]["node_count"], 2)
        self.assertEqual(refreshed["subscription"]["usage"]["download"], 10)
        with mock.patch.object(app, "fetch_subscription_response", return_value={"not_modified": True, "usage": {"download": 30}}):
            refreshed = app.refresh_subscription(subscription_id)
        self.assertEqual(refreshed["subscription"]["usage"], {"download": 30, "total": 100})

    def test_empty_filter_result_is_intentional_and_invalid_payload_preserves_old_nodes(self) -> None:
        self.imported()
        original = app.STATE_PATH.read_bytes()
        with self.assertRaises(ValueError):
            self.imported(raw="not a subscription")
        self.assertEqual(app.STATE_PATH.read_bytes(), original)
        result = self.imported(filter_include="no-matches")
        self.assertEqual(result["subscription"]["node_count"], 0)
        self.assertEqual(app.load_state()["nodes"], [])

    def test_node_personalization_survives_subscription_refresh_and_blocks_client_fields(self) -> None:
        result = self.imported()
        node_id = result["nodes"][0]["id"]
        with mock.patch.object(app, "mark_sync_local_change"):
            result = app.update_node_details({"node_id": node_id, "region": "PH", "favorite": True,
                                              "remark": "Work", "settings": {"password": "injected"}, "id": "injected"})
        self.assertEqual(result["node"]["region_code"], "PH")
        self.imported()
        node = app.public_node(app.find_node(app.load_state(), node_id))
        self.assertEqual((node["region"], node["favorite"], node["remark"]), ("菲律宾", True, "Work"))
        self.assertEqual(node["id"], node_id)
        with self.assertRaises(ValueError):
            app.update_node_details({"node_id": node_id, "favorite": "false"})
        with mock.patch.object(app, "mark_sync_local_change"):
            node = app.update_node_details({"node_id": node_id, "region": "auto"})["node"]
        self.assertEqual(node["region_code"], "HK")

    def test_changing_rules_invalidates_conditional_cache(self) -> None:
        result = self.imported(_fetch_metadata={"etag": "cached", "last_modified": "yesterday"})
        with mock.patch.object(app, "mark_sync_local_change"):
            updated = app.update_subscription_details({"id": result["subscription"]["id"], "filter_exclude": "slow"})
        self.assertTrue(updated["refresh_required"])
        self.assertEqual(updated["subscription"]["etag"], "")
        self.assertEqual(updated["subscription"]["last_modified"], "")

    def test_history_is_bounded_without_changing_aggregate_success_rate(self) -> None:
        for index in range(23):
            app.persist_test_result({"node_id": "node-a", "ok": index % 2 == 0, "tested_at": index})
        state = app.load_state()
        self.assertEqual(len(state["test_history"]["node-a"]), 20)
        self.assertEqual(state["test_history"]["node-a"][0]["tested_at"], 22)
        self.assertEqual(state["tests"]["node-a"]["attempt_count"], 23)

    def test_new_lan_share_generates_token_but_preserves_explicit_and_existing_none(self) -> None:
        with mock.patch.object(app, "lan_ipv4_addresses", return_value=[]):
            result = app.update_network_settings({"allow_lan": True})
            self.assertEqual(result["network"]["security"]["auth_mode"], "token")
            self.assertGreaterEqual(len(result["generated_token"]), 16)
            self.assertNotIn("token", result["network"]["security"])
            retained = app.update_network_settings({"allow_lan": True, "lan_security": {"max_connections": 64}})
            self.assertEqual(retained["network"]["security"]["auth_mode"], "token")
            app.update_network_settings({"allow_lan": False})
            app.update_network_settings({"allow_lan": True, "lan_security": {"auth_mode": "none"}})
            result = app.update_network_settings({"allow_lan": True})
            self.assertEqual(result["network"]["security"]["auth_mode"], "none")
            self.assertNotIn("generated_token", result)
        with self.assertRaises(ValueError):
            app.update_network_settings({"allow_lan": True, "lan_security": None})

    def test_event_stream_uses_instance_header_and_not_query_credentials(self) -> None:
        denied = handler_for("/api/events?token=not-authentication", authenticated=False)
        denied.do_GET()
        self.assertIn(b" 403 ", denied.wfile.getvalue().split(b"\r\n")[0])
        handler = handler_for("/api/events")
        with mock.patch.object(app, "status_payload", return_value={"nodes": []}), \
                mock.patch.object(app.time, "monotonic", side_effect=[0, 0, 100]), \
                mock.patch.object(app.STATUS_EVENT_CONDITION, "wait_for", return_value=True):
            handler.do_GET()
        response = handler.wfile.getvalue()
        self.assertIn(b"text/event-stream", response)
        self.assertIn(b'event: status\ndata: {"nodes":[]}\n\n', response)
        self.assertIn(b"Cache-Control: no-store", response)

    def test_native_module_uses_javascript_mime_even_with_platform_override(self) -> None:
        handler = handler_for("/static/main.mjs", authenticated=False)
        with mock.patch.object(app.mimetypes, "guess_type", return_value=("text/plain", None)):
            handler.do_GET()
        response = handler.wfile.getvalue()
        self.assertIn(b"Content-Type: application/javascript; charset=utf-8", response)
        self.assertIn(b'import "./pages/nodes.js";', response)
        self.assertNotIn(app.BACKEND_INSTANCE_HEADER.encode() + b":", response)

    def test_probe_result_does_not_apply_after_bridge_restart(self) -> None:
        state = app.empty_state()
        state["bridges"] = [{"id": "b", "node_id": "n", "status": "running", "core_port": 22000,
                             "started_at": 1, "pid": 10}]
        app.save_state(state)

        def restart(*args: object, **kwargs: object) -> dict:
            current = app.load_state()
            current["bridges"][0]["started_at"] = 2
            app.save_state(current)
            return {"ok": False, "message": "old failure"}

        with mock.patch.object(app, "socks5_http_probe", side_effect=restart):
            self.assertEqual(app.check_running_bridges(), 0)
        self.assertEqual(app.load_state()["bridges"][0]["health"]["status"], "unknown")

    def test_probe_marks_failure_and_browser_binding_prevents_switch(self) -> None:
        state = app.empty_state()
        state["settings"]["health"]["auto_switch"] = True
        state["nodes"] = [{"id": "n", "name": "HK", "region": "香港", "protocol": "vless"}]
        state["bridges"] = [{"id": "b", "node_id": "n", "status": "running", "core_port": 22000}]
        state["browser_instances"] = [{"id": "browser", "node_id": "n", "status": "stopped"}]
        app.save_state(state)
        with mock.patch.object(app, "socks5_http_probe", return_value={"ok": False, "message": "offline"}), \
                mock.patch.object(app, "start_bridge") as start:
            self.assertEqual(app.check_running_bridges(), 1)
        self.assertEqual(app.load_state()["bridges"][0]["health"]["status"], "error")
        start.assert_not_called()

    def test_unexpected_runtime_exit_is_reported_but_manual_stop_is_quiet(self) -> None:
        for manual in (False, True):
            with self.subTest(manual=manual):
                state = app.empty_state()
                state["bridges"] = [{"id": "b", "node_id": "n", "node_name": "Example", "status": "running", "enabled": True}]
                app.save_state(state)
                with mock.patch.object(app, "log_event") as log:
                    app.persist_stopped_bridges({"b": app.traffic_defaults()}, disable=manual)
                bridge = app.load_state()["bridges"][0]
                self.assertEqual(bridge["status"], "stopped")
                if manual:
                    log.assert_not_called()
                    self.assertFalse(bridge["enabled"])
                else:
                    self.assertTrue(bridge["enabled"])
                    self.assertEqual(bridge["health"]["status"], "error")
                    self.assertEqual(log.call_args.kwargs["level"], "error")

    def test_exit_check_records_mismatch_and_ignores_stopped_browser(self) -> None:
        state = app.empty_state()
        state["nodes"] = [{"id": "n", "name": "HK", "region": "香港", "protocol": "vless"}]
        state["bridges"] = [{"id": "b", "node_id": "n", "status": "running", "port": 21080}]
        state["browser_instances"] = [{"id": "browser", "node_id": "n", "bridge_id": "b", "status": "running",
                                       "last_started_at": 1, "launch_count": 1}]
        app.save_state(state)
        with mock.patch.object(app, "lookup_browser_exit", return_value={"ip": "1.1.1.1", "region_code": "US"}):
            app.check_browser_exit("browser", (1, 1, "b"))
        check = app.load_state()["browser_instances"][0]["exit_check"]
        self.assertTrue(check["mismatch"])
        state = app.load_state()
        state["browser_instances"][0]["status"] = "stopped"
        app.save_state(state)
        with mock.patch.object(app, "lookup_browser_exit") as lookup:
            app.check_browser_exit("browser", (1, 1, "b"))
        lookup.assert_not_called()

    def test_auto_switch_preserves_port_and_attempts_rollback_after_start_failure(self) -> None:
        for fail in (False, True):
            with self.subTest(fail=fail):
                state = app.empty_state()
                state["settings"]["health"]["auto_switch"] = True
                state["nodes"] = [{"id": node_id, "name": node_id, "region": "香港", "protocol": "vless"}
                                  for node_id in ("original", "replacement")]
                bridge = {"id": "b", "node_id": "original", "status": "running", "port": 21080,
                          "core_port": 22080, "pid": 10, "started_at": 1, "health": {"status": "error"}}
                state["bridges"] = [bridge]
                state["tests"] = {"replacement": {"ok": True, "latency_ms": 80, "tested_at": app.now_ts()}}
                app.save_state(state)
                behavior = [RuntimeError("unavailable"), {"node_name": "original"}] if fail else [{"node_name": "replacement"}]
                with mock.patch.object(app, "ensure_core_installed"), mock.patch.object(app, "stop_bridge") as stop, \
                        mock.patch.object(app, "start_bridge", side_effect=behavior) as start:
                    app.maybe_start_health_replacement(bridge)
                stop.assert_called_once_with("b")
                self.assertEqual(start.call_args_list[0].args[0], {"node_id": "replacement", "core": "auto", "port": 21080,
                                                                 "_require_exact_port": True, "_health_switch_from": "original"})
                self.assertEqual(start.call_count, 2 if fail else 1)
                if fail:
                    self.assertEqual(start.call_args_list[1].args[0]["node_id"], "original")

    def test_core_update_checks_idle_state_before_downloading(self) -> None:
        with mock.patch.object(app, "_assert_kernel_upload_target_idle", side_effect=RuntimeError("active")), \
                mock.patch.object(app, "_github_json") as fetch:
            with self.assertRaisesRegex(RuntimeError, "active"):
                app.update_proxy_core({"core": "sing-box"})
        fetch.assert_not_called()

    def test_auto_switch_revalidates_browser_binding_before_starting(self) -> None:
        state = app.empty_state()
        state["settings"]["health"]["auto_switch"] = True
        state["nodes"] = [{"id": node_id, "name": node_id, "region": "香港", "protocol": "vless"}
                          for node_id in ("source", "target")]
        state["tests"] = {"target": {"ok": True, "tested_at": app.now_ts(), "latency_ms": 50}}
        self.assertTrue(app.health_switch_allowed(state, "source", "target"))
        state["browser_instances"] = [{"id": "late-browser", "node_id": "target", "status": "stopped"}]
        app.save_state(state)
        with self.assertRaisesRegex(ValueError, "切换条件已变化"):
            app._prepare_bridge_start({"node_id": "target", "_health_switch_from": "source"})
        self.assertEqual(app.load_state()["bridges"], [])

    def test_auto_switch_revalidates_region_and_test_freshness(self) -> None:
        state = app.empty_state()
        state["settings"]["health"]["auto_switch"] = True
        state["nodes"] = [{"id": "source", "region": "香港"}, {"id": "target", "region": "美国"}]
        state["tests"] = {"target": {"ok": True, "tested_at": app.now_ts()}}
        self.assertFalse(app.health_switch_allowed(state, "source", "target"))
        state["nodes"][1]["region"] = "香港"
        state["tests"]["target"]["tested_at"] = 1
        self.assertFalse(app.health_switch_allowed(state, "source", "target"))


if __name__ == "__main__":
    unittest.main()
