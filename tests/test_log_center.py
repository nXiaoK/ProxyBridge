from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

import app


class LogCenterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_work_root = app.WORK_ROOT
        self.old_runtime_root = app.RUNTIME_ROOT
        self.old_state_path = app.STATE_PATH
        self.old_log_settings = app.runtime_log_settings_snapshot()
        self.temp_dir = tempfile.TemporaryDirectory()
        app.configure_work_root(Path(self.temp_dir.name) / "work")
        app.ensure_dirs()

    def tearDown(self) -> None:
        app.configure_work_root(self.old_work_root)
        app.RUNTIME_ROOT = self.old_runtime_root
        app.STATE_PATH = self.old_state_path
        app.cache_log_settings(self.old_log_settings)
        self.temp_dir.cleanup()

    def test_request_log_pagination_filters_and_total(self) -> None:
        state = app.empty_state()
        state["request_logs"] = [
            {
                "id": f"log-{index}",
                "node_id": "node-a" if index % 2 == 0 else "node-b",
                "node_name": "Alpha" if index % 2 == 0 else "Beta",
                "status": "failed" if index % 3 == 0 else "closed",
                "target": f"example-{index}.com:443",
            }
            for index in range(12)
        ]
        app.save_state(state)

        payload = app.query_logs(
            {
                "kind": ["request"],
                "page": ["2"],
                "limit": ["2"],
                "node": ["node-a"],
                "status": ["closed"],
            }
        )

        self.assertEqual(payload["total"], 12)
        self.assertEqual(payload["filtered_total"], 4)
        self.assertEqual(payload["page_count"], 2)
        self.assertEqual(payload["page"], 2)
        self.assertEqual(len(payload["items"]), 2)

    def test_csv_export_neutralizes_formulas(self) -> None:
        state = app.empty_state()
        state["request_logs"] = [{"id": "one", "node_name": "=cmd()", "status": "failed"}]
        app.save_state(state)

        data, content_type, filename = app.export_logs({"kind": ["request"], "format": ["csv"]})

        text = data.decode("utf-8-sig")
        self.assertIn("'=cmd()", text)
        self.assertEqual(content_type, "text/csv; charset=utf-8")
        self.assertTrue(filename.endswith(".csv"))

    def test_daily_rotation_and_retention_cleanup(self) -> None:
        log_path = app.RUNTIME_ROOT / "bridge.log"
        log_path.write_bytes(b"yesterday")
        now = time.time()
        old = now - 2 * 24 * 60 * 60
        os.utime(log_path, (old, old))

        self.assertTrue(app.rotate_runtime_log_daily(log_path, current_time=now))
        archives = list(app.RUNTIME_ROOT.glob("bridge.log.*"))
        self.assertEqual(len(archives), 1)
        self.assertFalse(log_path.exists())

        result = app.cleanup_runtime_logs(
            {"retention_days": 1, "max_file_bytes": 1024 * 1024, "disk_quota_bytes": 8 * 1024 * 1024},
            current_time=now,
        )
        self.assertEqual(result["deleted_files"], 1)
        self.assertFalse(archives[0].exists())

    def test_quota_cleanup_preserves_active_log(self) -> None:
        active = app.RUNTIME_ROOT / "active.log"
        old = app.RUNTIME_ROOT / "old.log.2026-01-01"
        with active.open("wb") as handle:
            handle.truncate(5 * 1024 * 1024)
        with old.open("wb") as handle:
            handle.truncate(5 * 1024 * 1024)
        with app.PROCESS_LOCK:
            app.PROCESS_TABLE["active"] = object()
        try:
            result = app.cleanup_runtime_logs(
                {
                    "retention_days": 365,
                    "max_file_bytes": 1024 * 1024,
                    "disk_quota_bytes": 8 * 1024 * 1024,
                }
            )
        finally:
            with app.PROCESS_LOCK:
                app.PROCESS_TABLE.pop("active", None)

        self.assertTrue(active.exists())
        self.assertFalse(old.exists())
        self.assertEqual(result["used_bytes"], 5 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
