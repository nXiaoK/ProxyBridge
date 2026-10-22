from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app


class FakeProcess:
    def __init__(self, pid: int) -> None:
        self.pid = pid

    def poll(self) -> None:
        return None


PROBE_OK = {"ok": True, "latency_ms": 120, "target": "https://example.test/", "status_code": 204, "message": "通信正常"}


class NodeExitIpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.previous_root = app.WORK_ROOT
        self.temp = tempfile.TemporaryDirectory()
        app.configure_work_root(Path(self.temp.name))
        app.ensure_dirs()
        app.SHUTTING_DOWN.clear()
        state = app.empty_state()
        state["nodes"] = [{
            "id": "n", "name": "HK 01", "protocol": "vless", "server": "relay.example.com", "port": 443,
            "region": "香港", "settings": {}, "transport": "tcp", "security": "tls", "preferred_core": "sing-box",
        }]
        app.save_state(state)

    def tearDown(self) -> None:
        app.configure_work_root(self.previous_root)
        app.SHUTTING_DOWN.clear()
        self.temp.cleanup()

    def run_test_node(self, probe: dict, lookup: mock.Mock) -> dict:
        with (
            mock.patch.object(app, "generate_config", return_value={"test": True}),
            mock.patch.object(app, "core_command", return_value=["fake-core"]),
            mock.patch.object(app, "next_free_port", return_value=24610),
            mock.patch.object(app, "wait_for_port", return_value=True),
            mock.patch.object(app.subprocess, "Popen", return_value=FakeProcess(4610)),
            mock.patch.object(app, "terminate_process", return_value=None),
            mock.patch.object(app, "socks5_http_probe", return_value=dict(probe)),
            mock.patch.object(app, "socks5_exit_lookup", lookup),
        ):
            return app.test_node({"node_id": "n", "core": "auto"})

    def test_parse_cloudflare_trace_and_ipwho_responses(self) -> None:
        trace = b"fl=1\nh=www.cloudflare.com\nip=203.0.113.9\nts=1\nloc=jp\ncolo=NRT\n"
        self.assertEqual(app.parse_exit_lookup("trace", trace), {"ip": "203.0.113.9", "region_code": "JP"})
        unknown = b"ip=2001:db8::1\nloc=XX\n"
        self.assertEqual(app.parse_exit_lookup("trace", unknown), {"ip": "2001:db8::1"})
        body = json.dumps({"success": True, "ip": "198.51.100.7", "country_code": "US"}).encode()
        self.assertEqual(app.parse_exit_lookup("json", body), {"ip": "198.51.100.7", "region_code": "US"})
        with self.assertRaises(RuntimeError):
            app.parse_exit_lookup("json", json.dumps({"success": False}).encode())
        with self.assertRaises(ValueError):
            app.parse_exit_lookup("trace", b"ip=not-an-ip\nloc=US\n")

    def test_exit_lookup_falls_back_to_next_service(self) -> None:
        body = json.dumps({"ip": "198.51.100.7", "country_code": "SG"}).encode()
        failures = [RuntimeError("HTTP 403"), RuntimeError("timed out")]
        with mock.patch.object(app, "socks5_https_get", side_effect=[*failures, body]) as get:
            self.assertEqual(app.socks5_exit_lookup(24610), {"ip": "198.51.100.7", "region_code": "SG"})
        self.assertEqual([call.args[1] for call in get.call_args_list], ["1.1.1.1", "www.cloudflare.com", "ipwho.is"])
        with mock.patch.object(app, "socks5_https_get", side_effect=RuntimeError("timed out")):
            with self.assertRaisesRegex(RuntimeError, "1.1.1.1.*www.cloudflare.com.*ipwho.is"):
                app.socks5_exit_lookup(24610)

    def test_successful_test_records_exit_through_the_test_port(self) -> None:
        lookup = mock.Mock(return_value={"ip": "203.0.113.9", "region_code": "JP"})
        result = self.run_test_node(PROBE_OK, lookup)
        self.assertEqual(lookup.call_args.args[0], 24610)
        self.assertEqual(result["exit"]["ip"], "203.0.113.9")
        stored = app.load_state()["tests"]["n"]
        self.assertEqual(stored["exit"]["region_code"], "JP")
        self.assertGreater(stored["exit"]["checked_at"], 0)
        public = app.public_test_results(app.load_state(), stored["exit"]["checked_at"] + 90)["n"]
        self.assertEqual(public["exit"]["age_seconds"], 90)

    def test_failed_lookup_or_probe_keeps_previous_exit_without_failing_test(self) -> None:
        self.run_test_node(PROBE_OK, mock.Mock(return_value={"ip": "203.0.113.9", "region_code": "JP"}))

        result = self.run_test_node(PROBE_OK, mock.Mock(side_effect=RuntimeError("www.cloudflare.com: timed out")))
        self.assertTrue(result["ok"])
        self.assertEqual(result["exit"]["ip"], "203.0.113.9")
        self.assertIn("timed out", result["exit_error"])

        skipped = mock.Mock()
        result = self.run_test_node({**PROBE_OK, "ok": False, "message": "HTTP 502"}, skipped)
        skipped.assert_not_called()
        self.assertFalse(result["ok"])
        self.assertEqual(result["exit"]["ip"], "203.0.113.9")
        history = app.load_state()["test_history"]["n"]
        self.assertNotIn("exit", history[0])
        self.assertEqual(history[2]["exit"]["ip"], "203.0.113.9")

    def test_browser_exit_check_refreshes_existing_test_record_only(self) -> None:
        state = app.load_state()
        state["bridges"] = [{"id": "b", "node_id": "n", "status": "running", "port": 21080}]
        state["browser_instances"] = [{"id": "browser", "node_id": "n", "bridge_id": "b", "status": "running",
                                       "last_started_at": 1, "launch_count": 1}]
        app.save_state(state)
        app.EXIT_LOCALE_CACHE.pop("n", None)
        with mock.patch.object(app, "lookup_browser_exit", return_value={"ip": "198.51.100.7", "region_code": "HK"}):
            app.check_browser_exit("browser", (1, 1, "b"))
        self.assertNotIn("n", app.load_state()["tests"])

        state = app.load_state()
        state["tests"]["n"] = {"ok": True, "latency_ms": 100, "tested_at": 1, "exit": {"ip": "203.0.113.9", "checked_at": 1}}
        app.save_state(state)
        app.EXIT_LOCALE_CACHE.pop("n", None)
        with mock.patch.object(app, "lookup_browser_exit", return_value={"ip": "198.51.100.7", "region_code": "HK"}):
            app.check_browser_exit("browser", (1, 1, "b"))
        exit_info = app.load_state()["tests"]["n"]["exit"]
        self.assertEqual(exit_info["ip"], "198.51.100.7")
        self.assertEqual(exit_info["region_code"], "HK")
        app.EXIT_LOCALE_CACHE.pop("n", None)


if __name__ == "__main__":
    unittest.main()
