from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import app
import webdav_sync


class WebDAVSyncUnitTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.work = root / "work"
        self.profiles = self.work / "browser_profiles"
        self.state_path = self.work / "state.json"
        self.work.mkdir(parents=True)
        self.profiles.mkdir(parents=True)
        self.state = {
            "subscriptions": [
                {"id": "sub1", "name": "机场A", "url": "https://a.test/sub", "node_ids": ["n1"], "node_count": 1}
            ],
            "nodes": [
                {
                    "id": "n1",
                    "name": "香港",
                    "protocol": "vless",
                    "server": "1.2.3.4",
                    "port": 443,
                    "region": "香港",
                    "source": "机场A",
                    "settings": {"uuid": "u"},
                }
            ],
            "bridges": [],
            "browser_instances": [
                {
                    "id": "inst1",
                    "node_id": "n1",
                    "name": "浏览器 1",
                    "status": "stopped",
                    "legacy": False,
                    "launch_count": 2,
                }
            ],
            "tests": {},
            "request_logs": [],
            "settings": {
                "webdav": {
                    "url": "https://dav.test/base",
                    "username": "u",
                    "password": "p",
                    "remote_root": "ProxyBridge",
                    "enabled": True,
                    "device_id": "device-aaaa-bbbb",
                    "auto_upload_on_browser_stop": False,
                    "check_on_startup": False,
                    "encrypt": False,
                    "encrypt_password": "",
                }
            },
        }
        self.state_path.write_text(json.dumps(self.state), encoding="utf-8")
        self.lock = threading.RLock()
        self.store = webdav_sync.MemoryWebDAV()

        profile = self.profiles / "nodehash" / "inst1"
        (profile / "Default").mkdir(parents=True)
        (profile / "Default" / "Cookies").write_bytes(b"cookie-data")
        (profile / "Default" / "Local Storage").mkdir()
        (profile / "Default" / "Local Storage" / "x").write_text("ls", encoding="utf-8")
        (profile / "GPUCache").mkdir()
        (profile / "GPUCache" / "big").write_bytes(b"x" * 1000)

        def profile_path(node_id: str, instance_id: str, legacy: bool = False) -> Path:
            if legacy:
                return self.profiles / "nodehash"
            return self.profiles / "nodehash" / instance_id

        self.engine = webdav_sync.SyncEngine(
            work_root=self.work,
            state_path=self.state_path,
            browser_profiles_root=self.profiles,
            load_state=self._load,
            save_state=self._save,
            state_lock=self.lock,
            browser_instance_profile_path=profile_path,
            browser_runtime_is_active=lambda _iid: False,
            log=lambda *a, **k: None,
            client_factory=self.store.client,
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def _load(self) -> dict:
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def _save(self, state: dict) -> None:
        self.state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

    def test_pack_excludes_cache(self):
        profile = self.profiles / "nodehash" / "inst1"
        blob = webdav_sync.pack_profile_dir(profile)
        dest = self.work / "unpacked"
        webdav_sync.unpack_profile_dir(blob, dest)
        self.assertTrue((dest / "Default" / "Cookies").exists())
        self.assertFalse((dest / "GPUCache").exists())

    def test_push_and_pull_roundtrip(self):
        push = self.engine.push()
        self.assertTrue(push["ok"])
        self.assertIn("inst1", push["uploaded_profiles"])

        # Mutate local state and wipe profile, then pull back.
        # Local edits invalidate applied sha so pull is not treated as already-latest.
        with self.lock:
            state = self._load()
            state["subscriptions"] = []
            state["nodes"] = []
            state["browser_instances"] = []
            self._save(state)
        self.engine.mark_local_change()
        import shutil

        shutil.rmtree(self.profiles / "nodehash" / "inst1", ignore_errors=True)

        pull = self.engine.pull()
        self.assertTrue(pull["ok"])
        self.assertFalse(pull.get("skipped"))
        state = self._load()
        self.assertEqual(len(state["subscriptions"]), 1)
        self.assertEqual(len(state["nodes"]), 1)
        self.assertEqual(len(state["browser_instances"]), 1)
        self.assertEqual(state["browser_instances"][0]["status"], "stopped")
        self.assertTrue((self.profiles / "nodehash" / "inst1" / "Default" / "Cookies").exists())

    def test_hash_skip_on_second_push(self):
        first = self.engine.push()
        self.assertEqual(len(first["uploaded_profiles"]), 1)
        second = self.engine.push()
        self.assertEqual(second["uploaded_profiles"], [])
        self.assertIn("inst1", second["skipped_unchanged"])

    def test_pull_skips_when_already_latest(self):
        push = self.engine.push()
        self.assertTrue(push["ok"])
        # Immediately pull again: same state_sha256 → full skip.
        again = self.engine.pull()
        self.assertTrue(again["ok"])
        self.assertTrue(again.get("skipped"))
        self.assertEqual(again.get("reason"), "already_latest")
        self.assertEqual(again.get("downloaded_profiles"), [])

        check = self.engine.check_remote()
        self.assertTrue(check.get("already_latest"))
        self.assertFalse(check.get("remote_newer"))

    def test_pull_force_bypasses_latest_skip(self):
        self.engine.push()
        forced = self.engine.pull(force=True)
        self.assertTrue(forced["ok"])
        self.assertFalse(forced.get("skipped"))
        self.assertIn("inst1", forced.get("skipped_unchanged") or forced.get("downloaded_profiles") or [])

    def test_remote_is_already_latest_helper(self):
        cfg = {
            "device_id": "dev-a",
            "last_pull_at": 100,
            "last_remote_updated_at": 200,
            "last_applied_state_sha256": "abc",
        }
        self.assertTrue(
            webdav_sync.remote_is_already_latest(cfg, {"state_sha256": "abc", "updated_at": 999, "device_id": "other"})
        )
        self.assertFalse(
            webdav_sync.remote_is_already_latest(cfg, {"state_sha256": "def", "updated_at": 50, "device_id": "other"})
        )
        # No sha: timestamp fallback
        cfg2 = {"device_id": "dev-a", "last_pull_at": 100, "last_remote_updated_at": 200, "last_applied_state_sha256": ""}
        self.assertTrue(webdav_sync.remote_is_already_latest(cfg2, {"updated_at": 90, "device_id": "other"}))
        self.assertTrue(
            webdav_sync.remote_is_already_latest(cfg2, {"updated_at": 150, "device_id": "dev-a"})
        )
        self.assertFalse(
            webdav_sync.remote_is_already_latest(cfg2, {"updated_at": 250, "device_id": "other"})
        )

    def test_public_settings_masks_secrets(self):
        pub = webdav_sync.public_webdav_settings(self._load()["settings"]["webdav"])
        self.assertTrue(pub["password_set"])
        self.assertNotIn("password", pub)
        self.assertNotIn("encrypt_password", pub)

    def test_apply_remote_state_lww(self):
        local = {
            "subscriptions": [{"id": "sub1", "name": "old"}],
            "nodes": [{"id": "n1", "name": "old"}],
            "browser_instances": [],
        }
        remote = {
            "subscriptions": [{"id": "sub1", "name": "new"}, {"id": "sub2", "name": "B"}],
            "nodes": [{"id": "n1", "name": "new-node"}],
            "browser_instances": [
                {"id": "inst9", "node_id": "n1", "name": "B1", "status": "running", "legacy": False}
            ],
        }
        webdav_sync.apply_remote_state(
            local,
            remote,
            profile_path_builder=lambda n, i, legacy=False: Path(f"/tmp/{n}/{i}"),
        )
        self.assertEqual(local["subscriptions"][0]["name"], "new")
        self.assertEqual(len(local["subscriptions"]), 2)
        self.assertEqual(local["browser_instances"][0]["status"], "stopped")
        self.assertIn("profile_path", local["browser_instances"][0])

    def test_encrypt_roundtrip_if_openssl(self):
        if not webdav_sync.openssl_available():
            self.skipTest("openssl not available")
        raw = b"hello-sync-payload"
        enc = webdav_sync.encrypt_payload(raw, "secret")
        self.assertTrue(enc.startswith(b"PBENC1"))
        self.assertEqual(webdav_sync.decrypt_payload(enc, "secret"), raw)


class WebDAVAppAPITests(unittest.TestCase):
    def setUp(self):
        self.old_work = app.WORK_ROOT
        self.old_runtime = app.RUNTIME_ROOT
        self.old_state = app.STATE_PATH
        self.old_engine = app.SYNC_ENGINE
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        app.WORK_ROOT = root / "work"
        app.RUNTIME_ROOT = app.WORK_ROOT / "runtime"
        app.STATE_PATH = app.WORK_ROOT / "state.json"
        app.SYNC_ENGINE = None
        app.ensure_dirs()
        app.save_state(app.empty_state())

    def tearDown(self):
        app.WORK_ROOT = self.old_work
        app.RUNTIME_ROOT = self.old_runtime
        app.STATE_PATH = self.old_state
        app.SYNC_ENGINE = self.old_engine
        self.temp.cleanup()

    def test_sync_status_and_settings(self):
        status = app.sync_status_payload()
        self.assertIn("settings", status)
        self.assertIn("device_id", status["settings"])

        updated = app.get_sync_engine().update_settings(
            {
                "url": "https://example.com/dav",
                "username": "alice",
                "password": "secret",
                "enabled": True,
                "remote_root": "PB",
            }
        )
        self.assertEqual(updated["url"], "https://example.com/dav")
        self.assertTrue(updated["password_set"])
        self.assertTrue(updated["enabled"])

        # Empty password leaves previous
        again = app.get_sync_engine().update_settings({"password": ""})
        self.assertTrue(again["password_set"])


if __name__ == "__main__":
    unittest.main()
