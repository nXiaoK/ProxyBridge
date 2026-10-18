from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
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

        self.profile_path = profile_path
        self.engine = self._new_engine()

    def _new_engine(self, *, sync_browser_data: bool = True) -> webdav_sync.SyncEngine:
        return webdav_sync.SyncEngine(
            work_root=self.work,
            state_path=self.state_path,
            browser_profiles_root=self.profiles,
            load_state=self._load,
            save_state=self._save,
            state_lock=self.lock,
            browser_instance_profile_path=self.profile_path,
            browser_runtime_is_active=lambda _iid: False,
            log=lambda *a, **k: None,
            client_factory=self.store.client,
            sync_browser_data=sync_browser_data,
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def _load(self) -> dict:
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def _save(self, state: dict) -> None:
        self.state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

    def _remote_manifest(self) -> tuple[webdav_sync.MemoryWebDAVClient, dict]:
        client = self.store.client(self._load()["settings"]["webdav"])
        manifest_url = client.url_for("ProxyBridge", "manifest.json")
        return client, json.loads(self.store.files[manifest_url].decode("utf-8"))

    def _remote_state_url(self, client, manifest: dict) -> str:
        return client.url_for("ProxyBridge", manifest.get("state_file") or "state.sync.json")

    def _remote_profile_url(self, client, manifest: dict, instance_id: str) -> str:
        filename = (manifest.get("profile_files") or {}).get(instance_id) or f"{instance_id}.tar.gz"
        return client.url_for("ProxyBridge", "profiles", filename)

    def test_pack_excludes_cache(self):
        profile = self.profiles / "nodehash" / "inst1"
        blob = webdav_sync.pack_profile_dir(profile)
        dest = self.work / "unpacked"
        webdav_sync.unpack_profile_dir(blob, dest)
        self.assertTrue((dest / "Default" / "Cookies").exists())
        self.assertFalse((dest / "GPUCache").exists())

    def test_pack_is_deterministic_and_excludes_sync_marker(self):
        profile = self.profiles / "nodehash" / "inst1"
        first = webdav_sync.pack_profile_dir(profile)
        (profile / webdav_sync.PROFILE_SYNC_MARKER).write_text("f" * 64, encoding="utf-8")
        time.sleep(1.05)
        second = webdav_sync.pack_profile_dir(profile)
        self.assertEqual(first, second)

    def test_unpack_rejects_unsafe_members_without_replacing_destination(self):
        dest = self.work / "existing-profile"
        dest.mkdir()
        (dest / "keep.txt").write_text("keep", encoding="utf-8")

        for name, member_type in (("../escape", tarfile.REGTYPE), ("fifo", tarfile.FIFOTYPE)):
            with self.subTest(name=name):
                buffer = io.BytesIO()
                with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
                    member = tarfile.TarInfo(name)
                    member.type = member_type
                    if member_type == tarfile.REGTYPE:
                        payload = b"bad"
                        member.size = len(payload)
                        archive.addfile(member, io.BytesIO(payload))
                    else:
                        archive.addfile(member)
                with self.assertRaises(RuntimeError):
                    webdav_sync.unpack_profile_dir(buffer.getvalue(), dest)
                self.assertEqual((dest / "keep.txt").read_text(encoding="utf-8"), "keep")

    def test_push_and_pull_roundtrip(self):
        push = self.engine.push()
        self.assertTrue(push["ok"])
        self.assertIn("inst1", push["uploaded_profiles"])

        # Mutate local state and wipe profile, then pull back.
        # 本地变更版本使旧快照失效，手动拉取不能被判定为已最新。
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

    def test_node_metadata_and_subscription_filters_roundtrip_through_webdav(self):
        with self.lock:
            state = self._load()
            state["subscriptions"][0].update(
                {
                    "filter_include": "media,work",
                    "filter_exclude": "ads",
                    "filter_regex": True,
                }
            )
            state["nodes"][0].update(
                {
                    "region_code": "HK",
                    "region_override": "HK",
                    "favorite": True,
                    "remark": "daily",
                }
            )
            self._save(state)

        pushed = self.engine.push()
        self.assertTrue(pushed["ok"])
        client, manifest = self._remote_manifest()
        remote_state = json.loads(
            self.store.files[self._remote_state_url(client, manifest)].decode("utf-8")
        )
        self.assertEqual(
            {
                key: remote_state["nodes"][0][key]
                for key in ("region", "region_code", "region_override", "favorite", "remark")
            },
            {
                "region": "香港",
                "region_code": "HK",
                "region_override": "HK",
                "favorite": True,
                "remark": "daily",
            },
        )
        self.assertEqual(
            {
                key: remote_state["subscriptions"][0][key]
                for key in ("filter_include", "filter_exclude", "filter_regex")
            },
            {"filter_include": "media,work", "filter_exclude": "ads", "filter_regex": True},
        )

        with self.lock:
            state = self._load()
            state["subscriptions"][0].update(
                {"filter_include": "", "filter_exclude": "", "filter_regex": False}
            )
            state["nodes"][0].update(
                {
                    "region": "日本",
                    "region_code": "JP",
                    "region_override": "JP",
                    "favorite": False,
                    "remark": "local",
                }
            )
            self._save(state)

        pulled = self.engine.pull(force=True)
        self.assertTrue(pulled["ok"])
        state = self._load()
        self.assertEqual(
            {
                key: state["nodes"][0][key]
                for key in ("region", "region_code", "region_override", "favorite", "remark")
            },
            {
                "region": "香港",
                "region_code": "HK",
                "region_override": "HK",
                "favorite": True,
                "remark": "daily",
            },
        )
        self.assertEqual(
            {
                key: state["subscriptions"][0][key]
                for key in ("filter_include", "filter_exclude", "filter_regex")
            },
            {"filter_include": "media,work", "filter_exclude": "ads", "filter_regex": True},
        )

    def test_preview_pull_returns_hash_and_safe_remote_summary_without_writing(self):
        with self.lock:
            state = self._load()
            state["browser_instances"][0].update(
                {
                    "pid": 987,
                    "bridge_id": "local-bridge",
                    "proxy": "socks5://127.0.0.1:1080",
                    "exit_check": {"ip": "192.0.2.1", "region_code": "US"},
                }
            )
            self._save(state)
        self.assertTrue(self.engine.push()["ok"])
        client, manifest = self._remote_manifest()
        manifest_url = client.url_for("ProxyBridge", "manifest.json")
        state_url = self._remote_state_url(client, manifest)
        remote_state = json.loads(self.store.files[state_url].decode("utf-8"))
        remote_state["browser_instances"].append(
            {"id": "remote-only", "node_id": "n1", "name": "Remote only", "legacy": False}
        )
        remote_state["profile_hashes"]["remote-only"] = "f" * 64
        remote_state["updated_at"] += 1
        state_bytes = json.dumps(remote_state, ensure_ascii=False, indent=2).encode("utf-8")
        state_hash = webdav_sync.sha256_bytes(state_bytes)
        state_file = f"state-{state_hash[:16]}-preview.sync.json"
        manifest["state_sha256"] = state_hash
        manifest["state_file"] = state_file
        manifest["updated_at"] = remote_state["updated_at"]
        manifest["browser_instance_count"] = len(remote_state["browser_instances"])
        manifest["profile_hashes"] = dict(remote_state["profile_hashes"])
        manifest["profile_files"]["remote-only"] = "remote-only.tar.gz"
        manifest["profile_count"] = len(remote_state["profile_hashes"])
        self.store.files[client.url_for("ProxyBridge", state_file)] = state_bytes
        self.store.files[manifest_url] = json.dumps(
            manifest, ensure_ascii=False, indent=2
        ).encode("utf-8")
        local_only_profile = self.profiles / "nodehash" / "local-only" / "Default"
        local_only_profile.mkdir(parents=True)
        with self.lock:
            state = self._load()
            state["browser_instances"].append(
                {
                    "id": "local-only",
                    "node_id": "n1",
                    "name": "Local only",
                    "status": "stopped",
                    "legacy": False,
                }
            )
            self._save(state)
        before_state_file = self.state_path.read_bytes()

        preview = self.engine.preview_pull()

        self.assertTrue(preview["ok"])
        self.assertEqual(preview["state_sha256"], manifest["state_sha256"])
        self.assertEqual(preview["summary"]["subscriptions"]["remote_count"], 1)
        self.assertEqual(preview["summary"]["nodes"]["remote_count"], 1)
        self.assertEqual(preview["summary"]["browser_instances"]["remote_count"], 2)
        self.assertEqual(preview["summary"]["browser_instances"]["updated_count"], 0)
        self.assertEqual(preview["summary"]["profiles"]["local_count"], 2)
        self.assertEqual(preview["summary"]["profiles"]["remote_count"], 2)
        self.assertEqual(preview["summary"]["profiles"]["added_count"], 1)
        self.assertEqual(preview["summary"]["profiles"]["removed_count"], 1)
        self.assertEqual(preview["summary"]["profiles"]["updated_count"], 1)
        self.assertEqual(preview["remote"]["nodes"][0]["region"], "香港")
        self.assertNotIn("exit_check", preview["remote"]["browser_instances"][0])
        self.assertNotIn("settings", preview["remote"]["nodes"][0])
        self.assertNotIn("url", preview["remote"]["subscriptions"][0])
        self.assertEqual(self.state_path.read_bytes(), before_state_file)

    def test_convenience_pull_preserves_unuploaded_local_browser_record(self):
        self.engine.push()
        local_record = {
            "id": "local-new-browser",
            "node_id": "n1",
            "name": "浏览器 2",
            "status": "stopped",
            "legacy": False,
        }
        with self.lock:
            state = self._load()
            state["browser_instances"].append(dict(local_record))
            self._save(state)
        self.engine.mark_local_change()

        result = self.engine.pull(preserve_local_changes=True)

        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "local_changes_pending")
        saved_ids = {item["id"] for item in self._load()["browser_instances"]}
        self.assertEqual(saved_ids, {"inst1", "local-new-browser"})

        manual = self.engine.pull()
        self.assertTrue(manual["ok"])
        self.assertNotIn(
            "local-new-browser",
            {item["id"] for item in self._load()["browser_instances"]},
        )

        with self.lock:
            state = self._load()
            state["browser_instances"].append(dict(local_record))
            self._save(state)
        self.engine.mark_local_change()
        forced = self.engine.pull(force=True, preserve_local_changes=True)
        self.assertTrue(forced["ok"])
        self.assertNotIn(
            "local-new-browser",
            {item["id"] for item in self._load()["browser_instances"]},
        )

    def test_push_rejects_disabled_sync_without_touching_remote(self):
        state = self._load()
        state["settings"]["webdav"]["enabled"] = False
        self._save(state)
        client_factory = mock.Mock(side_effect=AssertionError("remote client must not be created"))
        self.engine.client_factory = client_factory

        with self.assertRaisesRegex(ValueError, "请先启用并配置 WebDAV"):
            self.engine.push()

        client_factory.assert_not_called()

    def test_pull_rejects_tampered_profile_hash(self):
        self.engine.push()
        client, manifest = self._remote_manifest()
        profile_url = self._remote_profile_url(client, manifest, "inst1")
        profile = self.profiles / "nodehash" / "inst1"
        cookie = profile / "Default" / "Cookies"
        cookie.write_bytes(b"tampered-remote")
        self.store.files[profile_url] = webdav_sync.pack_profile_dir(profile)
        cookie.write_bytes(b"cookie-data")
        self.engine.mark_local_change()
        before = self._load()["settings"]["webdav"]

        result = self.engine.pull(force=True)
        self.assertFalse(result["ok"])
        self.assertTrue(any("哈希不匹配" in error for error in result["errors"]))
        self.assertEqual(cookie.read_bytes(), b"cookie-data")
        after = self._load()["settings"]["webdav"]
        self.assertEqual(after["last_applied_state_sha256"], before["last_applied_state_sha256"])
        self.assertEqual(after["last_applied_local_change_version"], before["last_applied_local_change_version"])
        self.assertFalse(webdav_sync.remote_is_already_latest(after, manifest))

    def test_failed_pull_does_not_mark_snapshot_applied(self):
        self.engine.push()
        client, manifest = self._remote_manifest()
        profile_url = self._remote_profile_url(client, manifest, "inst1")
        self.store.files.pop(profile_url)
        self.engine.mark_local_change()
        before = self._load()["settings"]["webdav"]

        failed = self.engine.pull(force=True)
        self.assertFalse(failed["ok"])
        after = self._load()["settings"]["webdav"]
        self.assertEqual(after["last_applied_state_sha256"], before["last_applied_state_sha256"])
        self.assertEqual(after["last_applied_local_change_version"], before["last_applied_local_change_version"])
        self.assertFalse(webdav_sync.remote_is_already_latest(after, manifest))
        retried = self.engine.pull()
        self.assertFalse(retried["ok"])
        self.assertFalse(retried.get("skipped", False))

    def test_pull_rejects_unsafe_remote_instance_id(self):
        self.engine.push()
        client, manifest = self._remote_manifest()
        state_url = self._remote_state_url(client, manifest)
        manifest_url = client.url_for("ProxyBridge", "manifest.json")
        remote_state = json.loads(self.store.files[state_url].decode("utf-8"))
        remote_state["browser_instances"].append(
            {"id": "../escape", "node_id": "n1", "name": "bad", "legacy": False}
        )
        remote_state["profile_hashes"]["../escape"] = "0" * 64
        state_bytes = json.dumps(remote_state, ensure_ascii=False, indent=2).encode("utf-8")
        manifest["state_sha256"] = webdav_sync.sha256_bytes(state_bytes)
        manifest["profile_hashes"] = dict(remote_state["profile_hashes"])
        manifest["browser_instance_count"] = len(remote_state["browser_instances"])
        manifest["profile_count"] = len(remote_state["profile_hashes"])
        self.store.files[state_url] = state_bytes
        self.store.files[manifest_url] = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
        self.engine.mark_local_change()

        with self.assertRaisesRegex(RuntimeError, "ID 无效"):
            self.engine.pull(force=True)

    def test_pull_rejects_profile_destination_outside_root(self):
        self.engine.push()
        outside = self.work / "outside-profile"
        self.engine.browser_instance_profile_path = lambda *_args, **_kwargs: outside
        self.engine.mark_local_change()

        result = self.engine.pull(force=True)
        self.assertFalse(result["ok"])
        self.assertTrue(any("越出同步目录" in error for error in result["errors"]))
        self.assertFalse(outside.exists())

    def test_pull_rejects_state_hash_mismatch(self):
        self.engine.push()
        preview = self.engine.preview_pull()
        client, manifest = self._remote_manifest()
        state_url = self._remote_state_url(client, manifest)
        remote_state = json.loads(self.store.files[state_url].decode("utf-8"))
        remote_state["subscriptions"][0]["name"] = "tampered"
        self.store.files[state_url] = json.dumps(remote_state, ensure_ascii=False, indent=2).encode("utf-8")
        self.engine.mark_local_change()
        before = self._load()["settings"]["webdav"]
        before_state_file = self.state_path.read_bytes()
        cookie = self.profiles / "nodehash" / "inst1" / "Default" / "Cookies"
        before_cookie = cookie.read_bytes()

        with mock.patch.object(webdav_sync, "unpack_profile_dir") as unpack:
            with self.assertRaisesRegex(RuntimeError, "重新预览"):
                self.engine.pull(expected_state_sha256=preview["state_sha256"])
        unpack.assert_not_called()
        self.assertEqual(self._load()["subscriptions"][0]["name"], "机场A")
        self.assertEqual(self.state_path.read_bytes(), before_state_file)
        self.assertEqual(cookie.read_bytes(), before_cookie)
        after = self._load()["settings"]["webdav"]
        self.assertEqual(after["last_applied_state_sha256"], before["last_applied_state_sha256"])
        self.assertEqual(after["last_applied_local_change_version"], before["last_applied_local_change_version"])
        self.assertFalse(webdav_sync.remote_is_already_latest(after, manifest))

    def test_pull_rejects_snapshot_changed_after_preview_before_writing_profiles(self):
        self.assertTrue(self.engine.push()["ok"])
        preview = self.engine.preview_pull()
        client, manifest = self._remote_manifest()
        manifest_url = client.url_for("ProxyBridge", "manifest.json")
        original_state_url = self._remote_state_url(client, manifest)
        remote_state = json.loads(self.store.files[original_state_url].decode("utf-8"))
        remote_state["nodes"][0]["remark"] = "changed after preview"
        remote_state["updated_at"] += 1
        state_bytes = json.dumps(remote_state, ensure_ascii=False, indent=2).encode("utf-8")
        changed_hash = webdav_sync.sha256_bytes(state_bytes)
        changed_state_file = f"state-{changed_hash[:16]}-after-preview.sync.json"
        manifest["state_sha256"] = changed_hash
        manifest["state_file"] = changed_state_file
        manifest["updated_at"] = remote_state["updated_at"]
        self.store.files[client.url_for("ProxyBridge", changed_state_file)] = state_bytes
        self.store.files[manifest_url] = json.dumps(
            manifest, ensure_ascii=False, indent=2
        ).encode("utf-8")
        before_state_file = self.state_path.read_bytes()
        cookie = self.profiles / "nodehash" / "inst1" / "Default" / "Cookies"
        before_cookie = cookie.read_bytes()

        with mock.patch.object(webdav_sync, "unpack_profile_dir") as unpack:
            with self.assertRaisesRegex(RuntimeError, "重新预览"):
                self.engine.pull(force=True, expected_state_sha256=preview["state_sha256"])

        unpack.assert_not_called()
        self.assertEqual(self.state_path.read_bytes(), before_state_file)
        self.assertEqual(cookie.read_bytes(), before_cookie)

    def test_pull_rejects_unsupported_schema(self):
        self.engine.push()
        client, manifest = self._remote_manifest()
        state_url = self._remote_state_url(client, manifest)
        manifest_url = client.url_for("ProxyBridge", "manifest.json")
        remote_state = json.loads(self.store.files[state_url].decode("utf-8"))
        remote_state["schema_version"] = 99
        state_bytes = json.dumps(remote_state, ensure_ascii=False, indent=2).encode("utf-8")
        manifest["state_sha256"] = webdav_sync.sha256_bytes(state_bytes)
        self.store.files[state_url] = state_bytes
        self.store.files[manifest_url] = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
        self.engine.mark_local_change()

        with self.assertRaisesRegex(RuntimeError, "schema_version"):
            self.engine.pull(force=True)

    def test_push_operations_are_serialized(self):
        original_pack = webdav_sync.pack_profile_dir
        counter_lock = threading.Lock()
        start = threading.Barrier(3)
        active = 0
        max_active = 0
        results: list[dict] = []
        failures: list[BaseException] = []

        def observed_pack(path: Path) -> bytes:
            nonlocal active, max_active
            with counter_lock:
                active += 1
                max_active = max(max_active, active)
            try:
                time.sleep(0.05)
                return original_pack(path)
            finally:
                with counter_lock:
                    active -= 1

        def worker() -> None:
            try:
                start.wait()
                results.append(self.engine.push())
            except BaseException as exc:
                failures.append(exc)

        with mock.patch.object(webdav_sync, "pack_profile_dir", side_effect=observed_pack):
            threads = [threading.Thread(target=worker) for _ in range(2)]
            for thread in threads:
                thread.start()
            start.wait()
            for thread in threads:
                thread.join(timeout=5)

        self.assertEqual(failures, [])
        self.assertEqual(len(results), 2)
        self.assertTrue(all(result["ok"] for result in results))
        self.assertEqual(max_active, 1)

    def test_auto_push_queued_while_worker_is_busy(self):
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        calls: list[list[str] | None] = []

        def push(*, instance_ids=None):
            calls.append(instance_ids)
            if len(calls) == 1:
                started.set()
                self.assertTrue(release.wait(timeout=2))
            if len(calls) == 2:
                finished.set()
            return {"ok": True}

        with mock.patch.object(self.engine, "push", side_effect=push):
            self.engine.maybe_auto_push(reason="first")
            self.assertTrue(started.wait(timeout=1))
            self.engine.maybe_auto_push(reason="second")
            release.set()
            self.assertTrue(finished.wait(timeout=2))

        self.assertEqual(calls, [None, None])

    def test_auto_profile_uploads_merge_while_worker_is_busy(self):
        state = self._load()
        state["settings"]["webdav"]["auto_upload_on_browser_stop"] = True
        self._save(state)
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        calls: list[list[str] | None] = []

        def push(*, instance_ids=None):
            calls.append(instance_ids)
            if len(calls) == 1:
                started.set()
                self.assertTrue(release.wait(timeout=2))
            if len(calls) == 2:
                finished.set()
            return {"ok": True}

        with mock.patch.object(self.engine, "push", side_effect=push):
            self.engine.maybe_auto_upload_instance("inst1")
            self.assertTrue(started.wait(timeout=1))
            self.engine.maybe_auto_upload_instance("inst2")
            self.engine.maybe_auto_upload_instance("inst3")
            release.set()
            self.assertTrue(finished.wait(timeout=2))

        self.assertEqual(calls, [["inst1"], ["inst2", "inst3"]])

    def test_full_auto_push_supersedes_queued_profile_uploads(self):
        state = self._load()
        state["settings"]["webdav"]["auto_upload_on_browser_stop"] = True
        self._save(state)
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        calls: list[list[str] | None] = []

        def push(*, instance_ids=None):
            calls.append(instance_ids)
            if len(calls) == 1:
                started.set()
                self.assertTrue(release.wait(timeout=2))
            if len(calls) == 2:
                finished.set()
            return {"ok": True}

        with mock.patch.object(self.engine, "push", side_effect=push):
            self.engine.maybe_auto_upload_instance("inst1")
            self.assertTrue(started.wait(timeout=1))
            self.engine.maybe_auto_upload_instance("inst2")
            self.engine.maybe_auto_push(reason="state changed")
            release.set()
            self.assertTrue(finished.wait(timeout=2))

        self.assertEqual(calls, [["inst1"], None])

    def test_push_fails_closed_when_manifest_cannot_be_read(self):
        self.engine.push()
        client, manifest = self._remote_manifest()
        manifest_url = client.url_for("ProxyBridge", "manifest.json")
        state_url = self._remote_state_url(client, manifest)
        original_manifest = self.store.files[manifest_url]
        original_state = self.store.files[state_url]
        put_calls: list[str] = []

        class FailingManifestClient(webdav_sync.MemoryWebDAVClient):
            def get_bytes(self, url: str, *, max_bytes=None) -> bytes:
                if url == manifest_url:
                    raise RuntimeError("temporary read failure")
                return super().get_bytes(url, max_bytes=max_bytes)

            def put_bytes(self, url: str, data: bytes, content_type="application/octet-stream") -> None:
                put_calls.append(url)
                super().put_bytes(url, data, content_type)

        self.engine.client_factory = lambda cfg: FailingManifestClient(self.store, cfg["url"])
        with self.assertRaisesRegex(RuntimeError, "temporary read failure"):
            self.engine.push(instance_ids=["inst1"])

        self.assertEqual(put_calls, [])
        self.assertEqual(self.store.files[manifest_url], original_manifest)
        self.assertEqual(self.store.files[state_url], original_state)

    def test_failed_manifest_move_keeps_previous_snapshot_readable(self):
        self.engine.push()
        client, _manifest = self._remote_manifest()
        manifest_url = client.url_for("ProxyBridge", "manifest.json")
        original_manifest = self.store.files[manifest_url]
        with self.lock:
            state = self._load()
            state["subscriptions"][0]["name"] = "new-local-name"
            self._save(state)
        self.engine.mark_local_change()

        class FailingMoveClient(webdav_sync.MemoryWebDAVClient):
            def move(self, source_url: str, destination_url: str, *, overwrite=True) -> None:
                if destination_url == manifest_url:
                    raise RuntimeError("MOVE interrupted")
                super().move(source_url, destination_url, overwrite=overwrite)

        self.engine.client_factory = lambda cfg: FailingMoveClient(self.store, cfg["url"])
        with self.assertRaisesRegex(RuntimeError, "MOVE interrupted"):
            self.engine.push()

        self.assertEqual(self.store.files[manifest_url], original_manifest)
        pulled = self.engine.pull(force=True)
        self.assertTrue(pulled["ok"])
        self.assertEqual(self._load()["subscriptions"][0]["name"], "机场A")

    def test_hash_skip_on_second_push(self):
        first = self.engine.push()
        self.assertEqual(len(first["uploaded_profiles"]), 1)
        second = self.engine.push()
        self.assertEqual(second["uploaded_profiles"], [])
        self.assertIn("inst1", second["skipped_unchanged"])

    def test_push_creates_nested_remote_collections_in_order(self):
        self.engine.update_settings({"remote_root": "nested/root"})
        result = self.engine.push()
        self.assertTrue(result["ok"])
        base = "https://dav.test/base"
        self.assertIn(f"{base}/nested", self.store.collections)
        self.assertIn(f"{base}/nested/root", self.store.collections)
        self.assertIn(f"{base}/nested/root/profiles", self.store.collections)

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

    def test_push_deletes_remote_profile_for_removed_instance(self):
        self.engine.push()
        store_client, manifest = self._remote_manifest()
        versioned_url = self._remote_profile_url(store_client, manifest, "inst1")
        purl = store_client.url_for("ProxyBridge", "profiles", "inst1.tar.gz")
        self.assertIn(purl, self.store.files)
        self.assertIn(versioned_url, self.store.files)

        with self.lock:
            state = self._load()
            state["browser_instances"] = []
            state["subscriptions"] = []
            state["nodes"] = []
            self._save(state)
        self.engine.mark_local_change()
        result = self.engine.push()
        self.assertTrue(result["ok"])
        self.assertIn("inst1", result.get("deleted_remote_profiles") or [])
        self.assertNotIn(purl, self.store.files)
        self.assertNotIn(versioned_url, self.store.files)
        # Manifest should not list removed profile.
        man = json.loads(self.store.files[store_client.url_for("ProxyBridge", "manifest.json")].decode("utf-8"))
        self.assertNotIn("inst1", man.get("profile_hashes") or {})

    def test_subscription_only_push_preserves_remote_browser_snapshot(self):
        first = self.engine.push()
        self.assertTrue(first["ok"])
        client, before_manifest = self._remote_manifest()
        before_profile_url = self._remote_profile_url(client, before_manifest, "inst1")
        before_profile = self.store.files[before_profile_url]

        with self.lock:
            state = self._load()
            state["subscriptions"] = [
                {
                    "id": "sub2",
                    "name": "机场B",
                    "url": "https://b.test/sub",
                    "node_ids": ["n2"],
                    "node_count": 1,
                }
            ]
            state["nodes"] = [
                {
                    "id": "n2",
                    "name": "新加坡",
                    "protocol": "trojan",
                    "server": "2.3.4.5",
                    "port": 443,
                    "region": "新加坡",
                    "source": "机场B",
                    "settings": {"password": "p"},
                }
            ]
            state["browser_instances"] = []
            self._save(state)

        engine = self._new_engine(sync_browser_data=False)
        result = engine.push()
        self.assertTrue(result["ok"])
        self.assertEqual(result["uploaded_profiles"], [])
        self.assertEqual(result["deleted_remote_profiles"], [])
        self.assertEqual(result["settings"]["sync_scope"], "subscriptions")

        client, manifest = self._remote_manifest()
        remote_state = json.loads(self.store.files[self._remote_state_url(client, manifest)].decode("utf-8"))
        self.assertEqual([item["id"] for item in remote_state["subscriptions"]], ["sub2"])
        self.assertEqual({item["id"] for item in remote_state["nodes"]}, {"n1", "n2"})
        self.assertEqual(remote_state["preserved_browser_node_ids"], ["n1"])
        self.assertEqual([item["id"] for item in remote_state["browser_instances"]], ["inst1"])
        self.assertEqual(manifest["profile_hashes"], before_manifest["profile_hashes"])
        self.assertEqual(manifest["profile_files"], before_manifest["profile_files"])
        self.assertEqual(self.store.files[before_profile_url], before_profile)

    def test_subscription_only_pull_preserves_local_browser_data(self):
        self.engine.push()
        with self.lock:
            state = self._load()
            state["subscriptions"] = [
                {"id": "sub2", "name": "机场B", "url": "https://b.test/sub", "node_ids": ["n2"], "node_count": 1}
            ]
            state["nodes"] = [
                {
                    "id": "n2",
                    "name": "新加坡",
                    "protocol": "trojan",
                    "server": "2.3.4.5",
                    "port": 443,
                    "region": "新加坡",
                    "source": "机场B",
                    "settings": {"password": "p"},
                }
            ]
            state["browser_instances"] = []
            self._save(state)
        subscription_engine = self._new_engine(sync_browser_data=False)
        subscription_engine.push()

        local_profile = self.profiles / "nodehash" / "local-browser"
        local_profile.mkdir(parents=True)
        (local_profile / "keep.txt").write_text("keep", encoding="utf-8")
        with self.lock:
            state = self._load()
            state["subscriptions"] = [{"id": "local-sub", "name": "本地旧订阅", "node_ids": ["local-node"]}]
            state["nodes"] = [{"id": "local-node", "name": "本地旧节点"}]
            state["browser_instances"] = [
                {
                    "id": "local-browser",
                    "node_id": "local-node",
                    "name": "本地浏览器",
                    "status": "stopped",
                    "legacy": False,
                }
            ]
            self._save(state)
        subscription_engine.mark_local_change()

        result = subscription_engine.pull(force=True)
        self.assertTrue(result["ok"])
        self.assertEqual(result["downloaded_profiles"], [])
        self.assertEqual(result["purged_local_profiles"], [])
        state = self._load()
        self.assertEqual([item["id"] for item in state["subscriptions"]], ["sub2"])
        self.assertEqual({item["id"] for item in state["nodes"]}, {"n2", "local-node"})
        preserved_local_node = next(item for item in state["nodes"] if item["id"] == "local-node")
        self.assertTrue(preserved_local_node[webdav_sync.LOCAL_BROWSER_NODE_MARKER])
        self.assertEqual([item["id"] for item in state["browser_instances"]], ["local-browser"])
        self.assertEqual((local_profile / "keep.txt").read_text(encoding="utf-8"), "keep")

        subscription_engine.push()
        client, manifest = self._remote_manifest()
        remote_state = json.loads(self.store.files[self._remote_state_url(client, manifest)].decode("utf-8"))
        self.assertNotIn("local-node", {item["id"] for item in remote_state["nodes"]})

    def test_remote_worker_compat_layout_syncs_only_chromium_profile(self):
        root = self.profiles / "nodehash" / "inst1"
        chromium_profile = root / "chromium-profile"
        chromium_profile.mkdir()
        (root / "Default").replace(chromium_profile / "Default")
        (root / "GPUCache").replace(chromium_profile / "GPUCache")
        (root / "selkies-tls.key").write_text("must-not-sync", encoding="utf-8")
        with self.lock:
            state = self._load()
            state["browser_instances"][0]["profile_layout"] = "remote-worker-v1"
            self._save(state)

        self.engine.push()
        client, manifest = self._remote_manifest()
        archive = self.store.files[self._remote_profile_url(client, manifest, "inst1")]
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as package:
            names = set(package.getnames())
        self.assertIn("Default/Cookies", names)
        self.assertNotIn("selkies-tls.key", names)
        self.assertFalse(any(name.startswith("chromium-profile/") for name in names))

        shutil.rmtree(chromium_profile)
        result = self.engine.pull(force=True)
        self.assertTrue(result["ok"])
        self.assertEqual((chromium_profile / "Default" / "Cookies").read_bytes(), b"cookie-data")
        self.assertEqual((root / "selkies-tls.key").read_text(encoding="utf-8"), "must-not-sync")

    def test_subscription_only_push_rejects_tampered_remote_state(self):
        self.engine.push()
        client, manifest = self._remote_manifest()
        manifest_url = client.url_for("ProxyBridge", "manifest.json")
        state_url = self._remote_state_url(client, manifest)
        original_manifest = self.store.files[manifest_url]
        remote_state = json.loads(self.store.files[state_url].decode("utf-8"))
        remote_state["subscriptions"][0]["name"] = "tampered"
        self.store.files[state_url] = json.dumps(remote_state, ensure_ascii=False, indent=2).encode("utf-8")

        with self.assertRaisesRegex(RuntimeError, "哈希与 manifest 不一致"):
            self._new_engine(sync_browser_data=False).push()
        self.assertEqual(self.store.files[manifest_url], original_manifest)

    def test_subscription_only_push_fails_when_preserved_profile_is_missing(self):
        self.engine.push()
        client, manifest = self._remote_manifest()
        manifest_url = client.url_for("ProxyBridge", "manifest.json")
        original_manifest = self.store.files[manifest_url]
        self.store.files.pop(self._remote_profile_url(client, manifest, "inst1"))

        with self.assertRaisesRegex(RuntimeError, "缺少需保留的 Profile"):
            self._new_engine(sync_browser_data=False).push()
        self.assertEqual(self.store.files[manifest_url], original_manifest)

    def test_encrypted_subscription_only_push_preserves_profile(self):
        if not webdav_sync.openssl_available():
            self.skipTest("openssl not available")
        self.engine.update_settings({"encrypt": True, "encrypt_password": "secret"})
        self.engine.push()
        client = self.store.client(self._load()["settings"]["webdav"])
        manifest_url = client.url_for("ProxyBridge", "manifest.json")
        before_manifest = json.loads(
            webdav_sync.decrypt_payload(self.store.files[manifest_url], "secret").decode("utf-8")
        )
        profile_url = self._remote_profile_url(client, before_manifest, "inst1")
        before_profile = self.store.files[profile_url]
        self.assertTrue(before_profile.startswith(webdav_sync.PBENC2_MAGIC))

        with self.lock:
            state = self._load()
            state["subscriptions"] = []
            state["nodes"] = []
            state["browser_instances"] = []
            self._save(state)
        result = self._new_engine(sync_browser_data=False).push()
        self.assertTrue(result["ok"])
        self.assertTrue(self.store.files[manifest_url].startswith(webdav_sync.PBENC2_MAGIC))
        self.assertEqual(self.store.files[profile_url], before_profile)

    def test_apply_remote_state_removes_local_only_subscription(self):
        local = {
            "subscriptions": [{"id": "sub1", "name": "keep"}, {"id": "sub-gone", "name": "delete-me"}],
            "nodes": [{"id": "n1", "name": "A"}, {"id": "n-gone", "name": "B"}],
            "browser_instances": [
                {"id": "inst-keep", "node_id": "n1", "name": "B1", "status": "stopped", "legacy": False},
                {"id": "inst-gone", "node_id": "n-gone", "name": "B2", "status": "stopped", "legacy": False},
            ],
        }
        remote = {
            "subscriptions": [{"id": "sub1", "name": "keep"}],
            "nodes": [{"id": "n1", "name": "A"}],
            "browser_instances": [
                {"id": "inst-keep", "node_id": "n1", "name": "B1", "status": "stopped", "legacy": False},
            ],
        }
        applied = webdav_sync.apply_remote_state(
            local,
            remote,
            profile_path_builder=lambda n, i, legacy=False: Path(f"/tmp/{n}/{i}"),
        )
        self.assertEqual([s["id"] for s in local["subscriptions"]], ["sub1"])
        self.assertEqual([n["id"] for n in local["nodes"]], ["n1"])
        self.assertEqual([b["id"] for b in local["browser_instances"]], ["inst-keep"])
        self.assertIn("sub-gone", applied["removed_subscription_ids"])
        self.assertIn("inst-gone", applied["removed_browser_ids"])

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
        # 本地版本领先于已应用版本时，哈希相同和旧时间戳都不能触发跳过。
        cfg.update(local_change_version=2, last_applied_local_change_version=1)
        for manifest in ({"state_sha256": "abc", "updated_at": 90}, {"updated_at": 90}):
            with self.subTest(manifest=manifest):
                self.assertFalse(webdav_sync.remote_is_already_latest(cfg, manifest))
        cfg["last_applied_local_change_version"] = 2
        self.assertTrue(webdav_sync.remote_is_already_latest(cfg, {"state_sha256": "abc"}))
        # No sha: timestamp fallback
        cfg2 = {"device_id": "dev-a", "last_pull_at": 100, "last_remote_updated_at": 200, "last_applied_state_sha256": ""}
        self.assertTrue(webdav_sync.remote_is_already_latest(cfg2, {"updated_at": 90, "device_id": "other"}))
        self.assertTrue(
            webdav_sync.remote_is_already_latest(cfg2, {"updated_at": 150, "device_id": "dev-a"})
        )
        self.assertFalse(
            webdav_sync.remote_is_already_latest(cfg2, {"updated_at": 250, "device_id": "other"})
        )

    def test_exists_retries_get_when_head_404(self):
        """Some WebDAV servers lie on HEAD; file may still be fetchable via GET."""

        class FlakyHeadClient(webdav_sync.WebDAVClient):
            def __init__(self):
                self.files: dict[str, bytes] = {}

            def request(
                self,
                method,
                url,
                *,
                data=None,
                headers=None,
                allow_statuses=None,
                max_body_bytes=None,
                read_body=True,
            ):
                if method == "HEAD":
                    return 404, {}, b""
                if method == "GET":
                    if url in self.files:
                        return 200, {}, self.files[url] if read_body else b""
                    return 404, {}, b""

        client = FlakyHeadClient()
        url = "https://dav.test/base/ProxyBridge/manifest.json"
        self.assertFalse(client.exists(url))
        client.files[url] = b'{"ok":true}'
        self.assertTrue(client.exists(url))

    def test_exists_propagates_authentication_errors(self):
        class AuthFailureClient(webdav_sync.WebDAVClient):
            def __init__(self):
                pass

            def request(self, *args, **kwargs):
                raise RuntimeError("WebDAV HEAD failed (401)")

        with self.assertRaisesRegex(RuntimeError, "401"):
            AuthFailureClient().exists("https://dav.test/manifest.json")

    def test_public_settings_masks_secrets(self):
        pub = webdav_sync.public_webdav_settings(self._load()["settings"]["webdav"])
        self.assertTrue(pub["password_set"])
        self.assertNotIn("password", pub)
        self.assertNotIn("encrypt_password", pub)
        self.assertIn("insecure_ssl", pub)

    def test_url_userinfo_is_migrated_and_removed_from_public_settings(self):
        raw = {
            "url": "https://alice%40home:p%3Ass@[fd00::20]:8443/dav?folder=one",
            "username": "",
            "password": "",
            "last_error": "failed https://alice%40home:p%3Ass@[fd00::20]:8443/dav",
        }

        cfg = webdav_sync.normalize_webdav_settings(raw)
        public = webdav_sync.public_webdav_settings(cfg)

        self.assertEqual(cfg["url"], "https://[fd00::20]:8443/dav?folder=one")
        self.assertEqual(cfg["username"], "alice@home")
        self.assertEqual(cfg["password"], "p:ss")
        self.assertEqual(public["url"], cfg["url"])
        self.assertNotIn("alice", public["last_error"])
        self.assertNotIn("p%3Ass", public["last_error"])

    def test_nested_url_userinfo_is_redacted_from_settings_and_errors(self):
        nested = "https://dav.test/base?next=https://alice:top-secret@evil.test/path"
        cfg = webdav_sync.normalize_webdav_settings(
            {"url": nested, "last_error": f"redirect failed: {nested}"}
        )
        public = webdav_sync.public_webdav_settings(cfg)

        self.assertNotIn("alice", cfg["url"])
        self.assertNotIn("top-secret", cfg["url"])
        self.assertNotIn("alice", public["last_error"])
        self.assertNotIn("top-secret", public["last_error"])
        self.assertIn("next=https://evil.test/path", public["url"])

    def test_explicit_webdav_credentials_override_url_userinfo(self):
        cfg = webdav_sync.normalize_webdav_settings(
            {
                "url": "https://url-user:url-pass@dav.test/base",
                "username": "settings-user",
                "password": "settings-pass",
            }
        )

        self.assertEqual(cfg["url"], "https://dav.test/base")
        self.assertEqual(cfg["username"], "settings-user")
        self.assertEqual(cfg["password"], "settings-pass")

    def test_update_settings_can_clear_credentials_from_userinfo_url(self):
        result = self.engine.update_settings(
            {
                "url": "https://url-user:url-pass@dav.test/base",
                "username": "",
                "password": "",
                "clear_password": True,
            }
        )
        stored = self._load()["settings"]["webdav"]

        self.assertEqual(result["url"], "https://dav.test/base")
        self.assertEqual(result["username"], "")
        self.assertFalse(result["password_set"])
        self.assertEqual(stored["url"], "https://dav.test/base")
        self.assertEqual(stored["username"], "")
        self.assertEqual(stored["password"], "")

    def test_webdav_client_strips_url_userinfo_and_validates_ports(self):
        client = webdav_sync.WebDAVClient(
            "https://alice%40home:p%3Ass@[fd00::20]:8443/dav"
        )
        self.assertEqual(client.base, "https://[fd00::20]:8443/dav")
        self.assertEqual(client.username, "alice@home")
        self.assertEqual(client.password, "p:ss")

        for url in ("https://dav.test:bad/base", "https://dav.test:70000/base"):
            with self.subTest(url=url), self.assertRaisesRegex(ValueError, "端口"):
                webdav_sync.WebDAVClient(url)

    def test_webdav_request_errors_redact_url_credentials(self):
        client = webdav_sync.WebDAVClient("https://dav.test/base")
        secret_url = "https://alice:top,secret;value@dav.test/private"
        client._opener = mock.Mock()
        client._opener.open.side_effect = webdav_sync.urllib.error.URLError(
            f"cannot reach {secret_url}"
        )

        with self.assertRaises(RuntimeError) as caught:
            client.request("GET", secret_url)

        message = str(caught.exception)
        self.assertNotIn("alice", message)
        self.assertNotIn("top,secret;value", message)
        self.assertIn("https://dav.test/private", message)

    def test_sync_failure_state_redacts_url_credentials(self):
        secret_url = "https://alice:top-secret@dav.test/private"

        cfg = self.engine._record_sync_failure(f"failed {secret_url}", "上传失败")

        self.assertNotIn("alice", cfg["last_error"])
        self.assertNotIn("top-secret", cfg["last_error"])
        self.assertNotIn("alice", self._load()["settings"]["webdav"]["last_error"])

    def test_insecure_ssl_client_builds(self):
        client = webdav_sync.WebDAVClient(
            "https://example.invalid/dav",
            username="u",
            password="p",
            insecure_ssl=True,
        )
        self.assertTrue(client.insecure_ssl)
        self.assertIsNotNone(client._opener)

    def test_webdav_redirects_stay_on_same_origin(self):
        handler = webdav_sync._WebDAVRedirectHandler()
        request = webdav_sync.urllib.request.Request(
            "https://nas.local:8443/dav/a",
            headers={"Authorization": "Basic secret"},
        )

        redirected = handler.redirect_request(
            request,
            None,
            302,
            "Found",
            {},
            "https://nas.local:8443/dav/b",
        )
        self.assertEqual(redirected.full_url, "https://nas.local:8443/dav/b")

        for target in ("https://other.local:8443/dav/b", "http://nas.local:8443/dav/b"):
            with self.subTest(target=target), self.assertRaisesRegex(
                webdav_sync.urllib.error.HTTPError,
                "跨源|降级",
            ):
                handler.redirect_request(request, None, 302, "Found", {}, target)

    def test_webdav_keeps_private_nas_urls_supported(self):
        for url in ("https://192.168.1.20/dav", "http://[fd00::20]/dav"):
            with self.subTest(url=url):
                client = webdav_sync.WebDAVClient(url)
                self.assertEqual(client.base, url)

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

    def test_apply_remote_state_subscription_only_preserves_browsers(self):
        local = {
            "subscriptions": [{"id": "old-sub", "name": "old"}],
            "nodes": [{"id": "old-node", "name": "old"}],
            "browser_instances": [
                {"id": "local-browser", "node_id": "old-node", "name": "local", "status": "stopped"}
            ],
        }
        remote = {
            "subscriptions": [{"id": "sub2", "name": "new", "node_ids": ["n2"]}],
            "nodes": [{"id": "n2", "name": "new"}, {"id": "preserved", "name": "browser-only"}],
            "browser_instances": [
                {"id": "remote-browser", "node_id": "preserved", "name": "remote", "status": "stopped"}
            ],
        }
        applied = webdav_sync.apply_remote_state(
            local,
            remote,
            profile_path_builder=lambda n, i, legacy=False: Path(f"/tmp/{n}/{i}"),
            include_browser_data=False,
            excluded_node_ids={"preserved"},
        )
        self.assertEqual([item["id"] for item in local["subscriptions"]], ["sub2"])
        self.assertEqual({item["id"] for item in local["nodes"]}, {"n2", "old-node"})
        preserved = next(item for item in local["nodes"] if item["id"] == "old-node")
        self.assertTrue(preserved[webdav_sync.LOCAL_BROWSER_NODE_MARKER])
        self.assertEqual([item["id"] for item in local["browser_instances"]], ["local-browser"])
        self.assertEqual(applied["browser_instances"], 0)
        self.assertEqual(applied["removed_browsers"], [])

    def test_encrypt_roundtrip_if_openssl(self):
        if not webdav_sync.openssl_available():
            self.skipTest("openssl not available")
        raw = b"hello-sync-payload"
        enc = webdav_sync.encrypt_payload(raw, "secret")
        self.assertTrue(enc.startswith(b"PBENC2"))
        self.assertEqual(webdav_sync.decrypt_payload(enc, "secret"), raw)
        tampered = bytearray(enc)
        tampered[-webdav_sync.PBENC2_TAG_BYTES - 1] ^= 1
        with self.assertRaisesRegex(RuntimeError, "篡改"):
            webdav_sync.decrypt_payload(bytes(tampered), "secret")

    def test_encrypt_requires_password(self):
        with self.assertRaises(ValueError):
            webdav_sync.encrypt_payload(b"plain", "")
        with self.assertRaises(ValueError):
            self.engine.update_settings({"encrypt": True})
        self.assertFalse(self._load()["settings"]["webdav"].get("encrypt"))

    def test_decrypt_reads_legacy_pbenc1(self):
        if not webdav_sync.openssl_available():
            self.skipTest("openssl not available")
        raw = b"legacy-sync-payload"
        env = os.environ.copy()
        env["PB_SYNC_PASS"] = "secret"
        proc = subprocess.run(
            ["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-salt", "-pass", "env:PB_SYNC_PASS"],
            input=raw,
            capture_output=True,
            env=env,
            check=True,
        )
        self.assertEqual(webdav_sync.decrypt_payload(b"PBENC1" + proc.stdout, "secret"), raw)

    def test_encrypted_push_pull_uses_pbenc2(self):
        if not webdav_sync.openssl_available():
            self.skipTest("openssl not available")
        self.engine.update_settings({"encrypt": True, "encrypt_password": "secret"})
        pushed = self.engine.push()
        self.assertTrue(pushed["ok"])
        client = self.store.client(self._load()["settings"]["webdav"])
        for relative in (
            ("ProxyBridge", "manifest.json"),
            ("ProxyBridge", "state.sync.json"),
            ("ProxyBridge", "profiles", "inst1.tar.gz"),
        ):
            self.assertTrue(self.store.files[client.url_for(*relative)].startswith(b"PBENC2"))

        profile = self.profiles / "nodehash" / "inst1"
        import shutil

        shutil.rmtree(profile)
        self.engine.mark_local_change()
        pulled = self.engine.pull(force=True)
        self.assertTrue(pulled["ok"])
        self.assertEqual((profile / "Default" / "Cookies").read_bytes(), b"cookie-data")


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
