from __future__ import annotations

import io
import json
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from unittest import mock

import app


def make_handler(path: str, body: dict[str, object]) -> app.AppHandler:
    encoded = json.dumps(body).encode("utf-8")
    handler = object.__new__(app.AppHandler)
    handler.command = "POST"
    handler.path = path
    handler.requestline = f"POST {path} HTTP/1.1"
    handler.request_version = "HTTP/1.1"
    handler.headers = Message()
    handler.headers["Host"] = "127.0.0.1:8787"
    handler.headers["Content-Type"] = "application/json"
    handler.headers["Content-Length"] = str(len(encoded))
    handler.headers[app.BACKEND_INSTANCE_HEADER] = app.management_api_token()
    handler.rfile = io.BytesIO(encoded)
    handler.wfile = io.BytesIO()
    return handler


def response_json(handler: app.AppHandler) -> tuple[int, dict[str, object]]:
    raw_headers, body = handler.wfile.getvalue().split(b"\r\n\r\n", 1)
    status = int(raw_headers.split(b"\r\n", 1)[0].split(b" ", 2)[1])
    return status, json.loads(body)


class DiagnosticTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_work_root = app.WORK_ROOT
        self.old_runtime_root = app.RUNTIME_ROOT
        self.old_state_path = app.STATE_PATH
        self.temp_dir = tempfile.TemporaryDirectory()
        app.configure_work_root(Path(self.temp_dir.name) / "work")
        app.ensure_dirs()
        app.save_state(app.empty_state())

    def tearDown(self) -> None:
        app.configure_work_root(self.old_work_root)
        app.RUNTIME_ROOT = self.old_runtime_root
        app.STATE_PATH = self.old_state_path
        self.temp_dir.cleanup()

    @staticmethod
    def ok(message: str = "ok") -> dict[str, object]:
        return {"status": "ok", "message": message, "details": {}}

    def test_default_run_never_calls_external_checks(self) -> None:
        with (
            mock.patch.object(app, "_diagnose_cores", return_value=self.ok()),
            mock.patch.object(app, "_diagnose_ports", return_value=self.ok()),
            mock.patch.object(app, "_diagnose_storage", return_value=self.ok()),
            mock.patch.object(app, "_diagnose_github") as github,
            mock.patch.object(app, "_diagnose_webdav") as webdav,
            mock.patch.object(app, "_diagnostic_proxy_ip_lookup") as outbound,
            mock.patch.object(app, "_selected_diagnostic_bridge") as select_bridge,
        ):
            result = app.run_diagnostics({"network": False})

        github.assert_not_called()
        webdav.assert_not_called()
        outbound.assert_not_called()
        select_bridge.assert_not_called()
        statuses = {item["id"]: item["status"] for item in result["results"]}
        self.assertEqual(statuses["cores"], "ok")
        self.assertEqual(statuses["github"], "skipped")
        self.assertEqual(statuses["webdav"], "skipped")
        self.assertEqual(statuses["outbound"], "skipped")
        self.assertEqual(statuses["dns"], "skipped")

    def test_network_failures_are_isolated_and_secrets_are_redacted(self) -> None:
        state = app.empty_state()
        state["bridges"] = [
            {
                "id": "bridge-one",
                "node_name": "One",
                "status": "running",
                "port": 21080,
                "core": "sing-box",
            }
        ]
        app.save_state(state)
        secret = "Authorization: Bearer should-never-leak"
        proxy_result = {
            "ip": "203.0.113.7",
            "bridge_id": "bridge-one",
            "node_name": "One",
            "core": "sing-box",
            "remote_dns": True,
        }
        with (
            mock.patch.object(app, "_diagnose_cores", return_value=self.ok()),
            mock.patch.object(app, "_diagnose_ports", return_value=self.ok()),
            mock.patch.object(app, "_diagnose_storage", return_value=self.ok()),
            mock.patch.object(app, "_diagnose_github", side_effect=RuntimeError(secret)),
            mock.patch.object(app, "_diagnose_webdav", return_value=self.ok("webdav")),
            mock.patch.object(app, "_diagnostic_proxy_ip_lookup", return_value=proxy_result) as lookup,
        ):
            result = app.run_diagnostics({"network": True, "bridge_id": "bridge-one"})

        lookup.assert_called_once()
        items = {item["id"]: item for item in result["results"]}
        self.assertEqual(items["github"]["status"], "error")
        self.assertEqual(items["webdav"]["status"], "ok")
        self.assertEqual(items["outbound"]["status"], "ok")
        self.assertEqual(items["dns"]["status"], "warn")
        self.assertNotIn(secret, json.dumps(result, ensure_ascii=False))

    def test_webdav_result_never_contains_credentials(self) -> None:
        state = app.empty_state()
        state.setdefault("settings", {})["webdav"] = {
            "enabled": True,
            "url": "https://dav.example.test/root",
            "username": "private-user",
            "password": "private-password",
        }
        app.save_state(state)
        client = mock.Mock()
        client.test_connection.return_value = {"ok": True, "status": 207}

        with mock.patch.object(app.webdav_sync, "WebDAVClient", return_value=client):
            result = app._diagnose_webdav()

        serialized = json.dumps(result, ensure_ascii=False)
        self.assertEqual(result["status"], "ok")
        self.assertNotIn("private-user", serialized)
        self.assertNotIn("private-password", serialized)
        self.assertEqual(result["details"]["host"], "dav.example.test")

    def test_insecure_webdav_is_not_contacted(self) -> None:
        state = app.empty_state()
        state.setdefault("settings", {})["webdav"] = {
            "enabled": True,
            "url": "https://dav.example.test/root",
            "username": "private-user",
            "password": "private-password",
            "insecure_ssl": True,
        }
        app.save_state(state)

        with mock.patch.object(app.webdav_sync, "WebDAVClient") as client:
            result = app._diagnose_webdav()

        client.assert_not_called()
        self.assertEqual(result["status"], "warn")

    def test_invalid_payload_and_bridge_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "必须是对象"):
            app.run_diagnostics([])  # type: ignore[arg-type]
        with self.assertRaisesRegex(ValueError, "未运行或不存在"):
            app.run_diagnostics({"network": True, "bridge_id": "missing"})

    def test_post_endpoint_returns_aggregated_result(self) -> None:
        expected = {
            "generated_at": 1,
            "network_requested": False,
            "selected_bridge_id": "",
            "results": [],
            "summary": {"ok": 0, "warn": 0, "error": 0, "skipped": 0},
        }
        handler = make_handler("/api/diagnostics/run", {"network": False})
        with (
            mock.patch.object(app, "run_diagnostics", return_value=expected) as run,
            mock.patch.object(app, "log_event") as log_event,
        ):
            handler.do_POST()

        status, payload = response_json(handler)
        self.assertEqual(status, 200)
        self.assertEqual(payload, expected)
        run.assert_called_once_with({"network": False})
        log_event.assert_called_once()


if __name__ == "__main__":
    unittest.main()
