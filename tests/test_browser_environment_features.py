from __future__ import annotations

import hashlib
import json
import os
import struct
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import app
import browser_broker
import webdav_sync


class FakePopen:
    calls: list[list[str]] = []

    def __init__(self, command: list[str], **_kwargs: object) -> None:
        self.pid = 5150 + len(FakePopen.calls)
        self.command = command
        FakePopen.calls.append(command)

    def poll(self) -> None:
        return None

    def wait(self, timeout: float | None = None) -> int:
        return 0


class FakeSyncEngine:
    def mark_local_change(self) -> None:
        return None

    def maybe_auto_push(self, *, reason: str = "") -> None:
        return None


class BrowserEnvironmentFeatureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_work_root = app.WORK_ROOT
        self.old_sync_engine = app.SYNC_ENGINE
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        app.configure_work_root(self.root / "work")
        app.SYNC_ENGINE = None
        self.env = mock.patch.dict(
            os.environ,
            {
                "PROXY_BRIDGE_DOCKER": "",
                "PROXY_BRIDGE_DISABLE_BROWSER": "",
                "PROXY_BRIDGE_BROWSER_BROKER_URL": "",
                "PROXY_BRIDGE_FP_CHROMIUM": "",
            },
            clear=False,
        )
        self.env.start()
        self.sync_patch = mock.patch.object(app, "get_sync_engine", return_value=FakeSyncEngine())
        self.sync_patch.start()
        with app.PROCESS_LOCK:
            self.old_browser_table = {key: list(value) for key, value in app.BROWSER_TABLE.items()}
            self.old_browser_process_table = dict(app.BROWSER_PROCESS_TABLE)
            self.old_launch_commands = dict(app.BROWSER_LAUNCH_COMMANDS)
            app.BROWSER_TABLE.clear()
            app.BROWSER_PROCESS_TABLE.clear()
            app.BROWSER_LAUNCH_COMMANDS.clear()
        with app.EXIT_LOCALE_CACHE_LOCK:
            app.EXIT_LOCALE_CACHE.clear()
        app.invalidate_fingerprint_chromium_cache()
        FakePopen.calls = []
        app.ensure_dirs()

    def tearDown(self) -> None:
        with app.PROCESS_LOCK:
            app.BROWSER_TABLE.clear()
            app.BROWSER_TABLE.update(self.old_browser_table)
            app.BROWSER_PROCESS_TABLE.clear()
            app.BROWSER_PROCESS_TABLE.update(self.old_browser_process_table)
            app.BROWSER_LAUNCH_COMMANDS.clear()
            app.BROWSER_LAUNCH_COMMANDS.update(self.old_launch_commands)
        with app.EXIT_LOCALE_CACHE_LOCK:
            app.EXIT_LOCALE_CACHE.clear()
        self.sync_patch.stop()
        self.env.stop()
        app.configure_work_root(self.old_work_root)
        app.SYNC_ENGINE = self.old_sync_engine
        app.invalidate_fingerprint_chromium_cache()
        self.temp.cleanup()

    # ---------- 辅助 ----------
    def install_stub_kernel(self, version: str = "150.0.7871.186") -> str:
        archive = self.root / f"fingerprint-chromium_{version}_linux_x64.zip"
        info = zipfile.ZipInfo("bundle/chrome")
        info.external_attr = 0o755 << 16
        with zipfile.ZipFile(archive, "w") as handle:
            handle.writestr(info, b"#!/bin/sh\nexit 0\n")
        with mock.patch.object(app, "host_os_token", return_value="linux"):
            result = app._install_fingerprint_package(archive, archive.name, f"v{version}")
        return str(result["path"])

    def save_state_with(self, *, nodes: list[dict[str, object]], instances: list[dict[str, object]] | None = None) -> None:
        state = app.empty_state()
        state["nodes"] = nodes
        state["browser_instances"] = instances or []
        app.save_state(state)

    def launch(self, instance_id: str, lookup: object) -> list[str]:
        bridge = {"id": "bridge-1", "port": 25080, "status": "running"}
        with (
            mock.patch.object(app.subprocess, "Popen", FakePopen),
            mock.patch.object(app, "synchronize_process_state"),
            mock.patch.object(app, "active_bridge_for_node", return_value=bridge),
            mock.patch.object(app, "lookup_browser_exit", side_effect=lookup) as lookup_mock,
        ):
            app.launch_browser({"instance_id": instance_id})
            self.lookup_calls = lookup_mock.call_count
        return FakePopen.calls[-1]

    # ---------- 指纹与启动参数 ----------
    def test_new_records_use_recommended_config_and_legacy_records_keep_previous_flags(self) -> None:
        self.install_stub_kernel()
        state = app.empty_state()
        state["nodes"] = [{"id": "node-us", "name": "US East", "region": "美国", "protocol": "vless"}]
        new_record = app.create_browser_instance_record(state, "node-us")
        legacy_record = {"id": "legacy-record", "node_id": "node-us", "name": "旧记录", "fingerprint_seed": 1234, "status": "stopped"}
        state["browser_instances"] = [new_record, legacy_record]
        app.save_state(state)
        saved = {item["id"]: item for item in app.load_state()["browser_instances"]}
        self.assertEqual(saved["legacy-record"]["fingerprint"], app.browser_fingerprint.legacy_fingerprint_config())
        self.assertEqual(saved[new_record["id"]]["fingerprint"]["brand"], "Chrome")

        exit_info = {"ip": "203.0.113.5", "region_code": "US", "timezone": "America/New_York"}
        command = self.launch(new_record["id"], lambda _port: exit_info)
        self.assertEqual(self.lookup_calls, 1)
        self.assertIn("--fingerprint-brand=Chrome", command)
        self.assertIn("--timezone=America/New_York", command)
        self.assertIn("--lang=en-US", command)
        self.assertNotIn("--remote-debugging-port=0", command)
        record = next(item for item in app.load_state()["browser_instances"] if item["id"] == new_record["id"])
        self.assertEqual(record["runtime_kernel"], "150.0.7871.186")
        self.assertEqual(record["runtime_locale"]["timezone_source"], "exit_ip")
        prefs = json.loads((Path(record["profile_path"]) / "Default" / "Preferences").read_text(encoding="utf-8"))
        self.assertEqual(prefs["profile"]["default_content_setting_values"]["geolocation"], 2)

        legacy_command = self.launch("legacy-record", lambda _port: exit_info)
        self.assertEqual(self.lookup_calls, 0)
        self.assertFalse(any(arg.startswith("--fingerprint-brand") for arg in legacy_command))
        self.assertIn("--timezone=America/Los_Angeles", legacy_command)
        self.assertIn("--fingerprint=1234", legacy_command)

    def test_exit_lookup_failure_falls_back_to_region_and_cdp_flag_is_added(self) -> None:
        self.install_stub_kernel()
        state = app.empty_state()
        state["nodes"] = [{"id": "node-jp", "name": "Tokyo", "region": "日本", "protocol": "vless"}]
        record = app.create_browser_instance_record(state, "node-jp", {"cdp_enabled": True, "start_urls": ["https://start.test/"]})
        state["browser_instances"] = [record]
        app.save_state(state)

        def fail(_port: int) -> dict[str, str]:
            raise RuntimeError("offline")

        command = self.launch(record["id"], fail)
        self.assertIn("--timezone=Asia/Tokyo", command)
        self.assertIn("--remote-debugging-port=0", command)
        self.assertEqual(command[-1], "https://start.test/")
        saved = app.load_state()["browser_instances"][0]
        self.assertEqual(saved["runtime_locale"]["timezone_source"], "region_fallback")
        self.assertEqual(app.BROWSER_LAUNCH_COMMANDS[record["id"]][-1], "--new-window")

    def test_pinned_kernel_must_exist_and_is_used_for_launch(self) -> None:
        older = self.install_stub_kernel("148.0.7778.215")
        self.install_stub_kernel("150.0.7871.186")
        state = app.empty_state()
        state["nodes"] = [{"id": "node-1", "name": "Node", "region": "香港", "protocol": "vless"}]
        pinned = app.create_browser_instance_record(state, "node-1", {"kernel_version": "148.0.7778.215"})
        missing = app.create_browser_instance_record(state, "node-1", {"kernel_version": "999.0.0.1"})
        state["browser_instances"] = [pinned, missing]
        app.save_state(state)
        command = self.launch(pinned["id"], lambda _port: {"ip": "1.1.1.1", "region_code": "HK"})
        self.assertEqual(command[0], older)
        with self.assertRaisesRegex(RuntimeError, "999.0.0.1"):
            self.launch(missing["id"], lambda _port: {"ip": "1.1.1.1", "region_code": "HK"})
        self.assertEqual(len(app.load_state()["browser_instances"]), 2)

    # ---------- 记录编辑 ----------
    def test_create_batch_update_and_regenerate(self) -> None:
        self.save_state_with(nodes=[{"id": "n1", "name": "Node 1", "region": "香港"}])
        created = app.create_browser_instances(
            {"node_id": "n1", "count": 3, "group": "电商", "fingerprint": {"platform": "windows"}}
        )
        self.assertEqual(created["created"], 3)
        ids = [item["id"] for item in created["instances"]]
        records = {item["id"]: item for item in app.load_state()["browser_instances"]}
        self.assertEqual(sorted(records[item]["name"] for item in ids), ["浏览器 1", "浏览器 2", "浏览器 3"])
        self.assertTrue(all(records[item]["group"] == "电商" for item in ids))
        self.assertTrue(all(records[item]["fingerprint"]["platform"] == "windows" for item in ids))
        self.assertTrue(all(records[item]["fingerprint"]["brand"] == "Chrome" for item in ids))
        self.assertEqual(len({records[item]["fingerprint_seed"] for item in ids}), 3)

        result = app.update_browser_instance({"instance_ids": ids[:2], "group": "B", "fingerprint": {"brand": "Edge"}})
        self.assertEqual(result["updated"], 2)
        records = {item["id"]: item for item in app.load_state()["browser_instances"]}
        self.assertEqual(records[ids[0]]["fingerprint"]["brand"], "Edge")
        self.assertEqual(records[ids[0]]["fingerprint"]["platform"], "windows")
        self.assertEqual(records[ids[2]]["group"], "电商")

        single = app.update_browser_instance({"instance_id": ids[2], "label": "店铺 A"})
        self.assertEqual(single["instance"]["label"], "店铺 A")
        for payload in (
            {"instance_ids": ids, "name": "same"},
            {"instance_id": ids[0], "kernel_version": "not-installed"},
            {"instance_id": ids[0], "fingerprint": {"platform": "android"}},
            {"instance_id": ids[0], "extensions": ["0123456789abcdef"]},
            {"instance_id": ids[0], "start_urls": ["ftp://x.test/"]},
            {"instance_id": ids[0], "cdp_enabled": "yes"},
        ):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                app.update_browser_instance(payload)

        before = records[ids[0]]["fingerprint_seed"]
        app.regenerate_browser_fingerprints({"instance_ids": [ids[0]]})
        after = next(item for item in app.load_state()["browser_instances"] if item["id"] == ids[0])
        self.assertNotEqual(after["fingerprint_seed"], before)
        state = app.load_state()
        state["browser_instances"][1]["status"] = "running"
        app.save_state(state)
        with mock.patch.object(app, "sync_browser_instance_status", return_value=False):
            with self.assertRaisesRegex(ValueError, "停止"):
                app.regenerate_browser_fingerprints({"instance_ids": [state["browser_instances"][1]["id"]]})

    def test_rebind_moves_profile_keeps_seed_and_prunes_preserved_node(self) -> None:
        state = app.empty_state()
        state["nodes"] = [
            {"id": "old-node", "name": "Old", webdav_sync.LOCAL_BROWSER_NODE_MARKER: True},
            {"id": "new-node", "name": "New"},
        ]
        record = app.create_browser_instance_record(state, "old-node")
        state["browser_instances"] = [record]
        app.save_state(state)
        source = app.browser_instance_profile_path("old-node", record["id"])
        (source / "Default").mkdir(parents=True)
        (source / "Default" / "Cookies").write_text("cookie", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "已从订阅中移除"):
            app.rebind_browser_instances({"instance_ids": [record["id"]], "node_id": "old-node"})
        result = app.rebind_browser_instances({"instance_ids": [record["id"]], "node_id": "new-node"})
        self.assertEqual(result["removed_preserved_node_ids"], ["old-node"])
        destination = app.browser_instance_profile_path("new-node", record["id"])
        self.assertEqual((destination / "Default" / "Cookies").read_text(encoding="utf-8"), "cookie")
        self.assertFalse(source.exists())
        saved_state = app.load_state()
        saved = saved_state["browser_instances"][0]
        self.assertEqual(saved["node_id"], "new-node")
        self.assertEqual(saved["fingerprint_seed"], record["fingerprint_seed"])
        self.assertEqual([node["id"] for node in saved_state["nodes"]], ["new-node"])

        saved_state["browser_instances"][0]["status"] = "running"
        saved_state["nodes"].append({"id": "third", "name": "Third"})
        app.save_state(saved_state)
        with mock.patch.object(app, "sync_browser_instance_status", return_value=False):
            with self.assertRaisesRegex(ValueError, "停止"):
                app.rebind_browser_instances({"instance_ids": [record["id"]], "node_id": "third"})

    # ---------- 订阅变化 ----------
    def test_subscription_delete_preserves_records_by_default_and_purges_when_requested(self) -> None:
        for purge in (False, True):
            with self.subTest(purge=purge):
                imported = app.import_subscription({"name": f"sub-{purge}", "raw": f"vless://{purge}@example.com:443#A"})
                node_id = imported["nodes"][0]["id"]
                state = app.load_state()
                record = app.create_browser_instance_record(state, node_id)
                state["browser_instances"].append(record)
                app.save_state(state)
                profile = app.browser_instance_profile_path(node_id, record["id"])
                profile.mkdir(parents=True)
                (profile / "marker").write_text("x", encoding="utf-8")

                result = app.delete_subscription(imported["subscription"]["id"], delete_browser_profiles=purge)
                saved = app.load_state()
                kept = [item for item in saved["browser_instances"] if item["id"] == record["id"]]
                node = next((item for item in saved["nodes"] if item["id"] == node_id), None)
                if purge:
                    self.assertEqual(kept, [])
                    self.assertIsNone(node)
                    self.assertFalse(profile.exists())
                    self.assertIn(record["id"], result["removed_instances"])
                else:
                    self.assertEqual(len(kept), 1)
                    self.assertTrue(node[webdav_sync.LOCAL_BROWSER_NODE_MARKER])
                    self.assertTrue((profile / "marker").exists())
                    self.assertEqual(result["preserved_instances"], [record["id"]])
                    public = next(item for item in app.status_payload()["nodes"] if item["id"] == node_id)
                    self.assertTrue(public["browser_preserved"])
                    # 删除最后一条引用后，保留节点随之清理。
                    app.delete_browser_instance({"instance_id": record["id"]})
                    self.assertFalse(any(item["id"] == node_id for item in app.load_state()["nodes"]))

    # ---------- 多版本内核 ----------
    def test_multi_version_kernel_listing_default_and_delete(self) -> None:
        legacy = app.fingerprint_chromium_root() / "chromium" / "chrome"
        legacy.parent.mkdir(parents=True)
        legacy.write_text("#!/bin/sh\n", encoding="utf-8")
        legacy.chmod(0o755)
        app.invalidate_fingerprint_chromium_cache()
        self.assertEqual([item["id"] for item in app.list_fingerprint_kernels()], ["legacy"])

        self.install_stub_kernel("148.0.7778.215")
        newest = self.install_stub_kernel("150.0.7871.186")
        kernels = app.list_fingerprint_kernels(force_refresh=True)
        self.assertEqual([item["id"] for item in kernels], ["150.0.7871.186", "148.0.7778.215", "legacy"])
        self.assertTrue(kernels[0]["default"])
        self.assertEqual(app.fingerprint_chromium_path(force_refresh=True), newest)

        app.set_default_fingerprint_kernel({"id": "148.0.7778.215"})
        self.assertTrue(app.fingerprint_chromium_path(force_refresh=True).endswith("148.0.7778.215/chromium/chrome"))
        with self.assertRaises(ValueError):
            app.set_default_fingerprint_kernel({"id": "1.2.3.4"})

        state = app.empty_state()
        state["nodes"] = [{"id": "n", "name": "N"}]
        state["browser_instances"] = [app.create_browser_instance_record(state, "n", {"kernel_version": "148.0.7778.215"})]
        app.save_state(state)
        with self.assertRaisesRegex(ValueError, "固定使用"):
            app.delete_fingerprint_kernel({"id": "148.0.7778.215"})
        app.delete_fingerprint_kernel({"id": "150.0.7871.186"})
        self.assertEqual([item["id"] for item in app.list_fingerprint_kernels(force_refresh=True)], ["148.0.7778.215", "legacy"])
        # 新版本安装到独立目录，不需要先停止浏览器；替换已安装版本仍需要。
        state["browser_instances"][0]["status"] = "running"
        app.save_state(state)
        app._assert_kernel_upload_target_idle("fingerprint-chromium", fingerprint_kernel_id="151.0.0.1")
        with self.assertRaisesRegex(RuntimeError, "停止所有浏览器"):
            app._assert_kernel_upload_target_idle("fingerprint-chromium", fingerprint_kernel_id="148.0.7778.215")

    def test_kernel_without_version_in_name_is_renamed_to_detected_version(self) -> None:
        archive = self.root / "fingerprint-chromium_linux_x64.zip"
        info = zipfile.ZipInfo("bundle/chrome")
        info.external_attr = 0o755 << 16
        with zipfile.ZipFile(archive, "w") as handle:
            handle.writestr(info, b"#!/bin/sh\n")
            handle.writestr("bundle/150.0.7871.186/resources.pak", b"x")
        with mock.patch.object(app, "host_os_token", return_value="linux"):
            result = app._install_fingerprint_package(archive, archive.name, "本地上传", kernel_id="upload-abcdef012345", source="upload")
        self.assertEqual(result["kernel_id"], "150.0.7871.186")
        self.assertFalse((app.fingerprint_kernel_versions_root() / "upload-abcdef012345").exists())
        self.assertEqual([item["id"] for item in app.list_fingerprint_kernels(force_refresh=True)], ["150.0.7871.186"])

    def test_kernel_id_derivation(self) -> None:
        self.assertEqual(app.fingerprint_kernel_id_for("fingerprint-chromium_150.0.7871.186_linux.zip"), "150.0.7871.186")
        self.assertEqual(app.fingerprint_kernel_id_for("fingerprint-chromium_macos.dmg", "v148.0.7778.215"), "148.0.7778.215")
        self.assertEqual(app.fingerprint_kernel_id_for("pkg.zip", "nightly build"), "nightly-build")
        self.assertEqual(app.fingerprint_kernel_id_for("pkg.zip", "", "ab" * 32), "upload-abababababab")

    # ---------- 扩展 ----------
    def build_extension(self, name: str, *, crx: bool = False, nested: bool = False) -> tuple[Path, str]:
        buffer = self.root / f"{name}.zip"
        prefix = "inner/" if nested else ""
        with zipfile.ZipFile(buffer, "w") as handle:
            handle.writestr(f"{prefix}manifest.json", json.dumps({"manifest_version": 3, "name": name, "version": "1.0"}))
            handle.writestr(f"{prefix}background.js", "console.log(1)")
        data = buffer.read_bytes()
        if crx:
            data = b"Cr24" + struct.pack("<II", 3, 4) + b"\x00" * 4 + data
            buffer = self.root / f"{name}.crx"
        package = self.root / f"{name}.package"
        package.write_bytes(data)
        return package, hashlib.sha256(data).hexdigest()

    def test_extension_library_install_profile_sync_and_delete(self) -> None:
        zip_package, zip_sha = self.build_extension("Demo")
        crx_package, crx_sha = self.build_extension("Nested", crx=True, nested=True)
        first = app.install_browser_extension_package(zip_package, "demo.zip", zip_sha)
        second = app.install_browser_extension_package(crx_package, "nested.crx", crx_sha)
        again = app.install_browser_extension_package(zip_package, "demo.zip", zip_sha)
        self.assertTrue(again["already_installed"])
        library = {item["id"]: item for item in app.browser_extension_library()}
        self.assertEqual(sorted(item["name"] for item in library.values()), ["Demo", "Nested"])
        with self.assertRaises(ValueError):
            broken = self.root / "broken.package"
            broken.write_bytes(b"not a zip")
            app.install_browser_extension_package(broken, "broken.zip", "cd" * 32)

        profile = self.root / "profile"
        first_id, second_id = first["extension"]["id"], second["extension"]["id"]
        self.assertEqual(app.sync_profile_extensions(profile, [first_id, second_id]), [first_id, second_id])
        self.assertTrue((profile / app.PROFILE_EXTENSIONS_DIR / second_id / "manifest.json").is_file())
        self.assertEqual(app.sync_profile_extensions(profile, [second_id]), [second_id])
        self.assertFalse((profile / app.PROFILE_EXTENSIONS_DIR / first_id).exists())

        state = app.empty_state()
        state["nodes"] = [{"id": "n", "name": "N"}]
        state["browser_instances"] = [app.create_browser_instance_record(state, "n", {"extensions": [first_id, second_id]})]
        app.save_state(state)
        app.delete_browser_extension({"id": first_id})
        self.assertEqual(app.load_state()["browser_instances"][0]["extensions"], [second_id])
        # 库中已删除但 Profile 内已有副本时继续加载副本。
        app.delete_browser_extension({"id": second_id})
        self.assertEqual(app.sync_profile_extensions(profile, [second_id]), [second_id])

    # ---------- Cookie 与打开网址 ----------
    def running_record(self, **overrides: object) -> dict[str, object]:
        state = app.empty_state()
        state["nodes"] = [{"id": "n", "name": "N"}]
        record = app.create_browser_instance_record(state, "n", {"cdp_enabled": True})
        record.update({"status": "running", "pid": 77, "cdp_port": 45678, "cdp_ws_path": "/devtools/browser/abcdef12-3456", **overrides})
        state["browser_instances"] = [record]
        app.save_state(state)
        with app.PROCESS_LOCK:
            app.BROWSER_PROCESS_TABLE[record["id"]] = FakePopen(["chrome"])
        return record

    def test_cookie_export_and_import_require_cdp(self) -> None:
        record = self.running_record()
        cookies = [{"name": "sid", "value": "1", "domain": ".a.test", "path": "/", "expires": -1, "session": True}]
        with mock.patch.object(app.browser_fingerprint, "cdp_call", return_value={"cookies": cookies}) as call:
            exported = app.export_browser_cookies({"instance_id": record["id"], "format": "json"})
        call.assert_called_once_with(45678, "/devtools/browser/abcdef12-3456", "Storage.getCookies")
        self.assertEqual(exported["count"], 1)
        self.assertEqual(json.loads(exported["content"])[0]["name"], "sid")
        self.assertTrue(exported["filename"].endswith(".json"))

        session = mock.MagicMock()
        session.__enter__.return_value = session
        with (
            mock.patch.object(app.browser_fingerprint, "CdpSession", return_value=session),
            mock.patch.object(app.browser_fingerprint, "set_cookies", return_value=(1, [])) as set_cookies,
        ):
            imported = app.import_browser_cookies({"instance_id": record["id"], "content": "a.test\tFALSE\t/\tFALSE\t0\tk\tv\n"})
        self.assertEqual(imported["imported"], 1)
        self.assertEqual(set_cookies.call_args.args[1][0]["url"], "http://a.test/")

        state = app.load_state()
        state["browser_instances"][0]["cdp_enabled"] = False
        app.save_state(state)
        with self.assertRaisesRegex(ValueError, "CDP"):
            app.export_browser_cookies({"instance_id": record["id"]})

    def test_open_url_uses_singleton_command_without_cdp(self) -> None:
        record = self.running_record(cdp_enabled=False, cdp_port=None, cdp_ws_path="")
        with app.PROCESS_LOCK:
            app.BROWSER_LAUNCH_COMMANDS[record["id"]] = ["/opt/chrome", "--user-data-dir=/p", "--proxy-server=socks5://127.0.0.1:1"]
        with mock.patch.object(app.subprocess, "Popen", FakePopen):
            result = app.open_browser_url({"instance_id": record["id"], "site": "browserscan"})
        self.assertEqual(result["method"], "singleton")
        self.assertEqual(FakePopen.calls[-1][-1], app.FINGERPRINT_CHECK_SITES["browserscan"])
        self.assertIn("--proxy-server=socks5://127.0.0.1:1", FakePopen.calls[-1])
        with self.assertRaises(ValueError):
            app.open_browser_url({"instance_id": record["id"], "url": "javascript:alert(1)"})

    def test_sync_sanitize_drops_runtime_fields(self) -> None:
        record = {
            "id": "a",
            "node_id": "n",
            "status": "running",
            "cdp_port": 45678,
            "cdp_ws_path": "/devtools/browser/abcdef12-3456",
            "runtime_kernel": "150",
            "runtime_locale": {"timezone": "UTC"},
            "fingerprint": {"brand": "Chrome"},
            "group": "G",
        }
        sanitized = webdav_sync.sanitize_browser_instance_for_sync(record)
        for field in ("cdp_port", "cdp_ws_path", "runtime_kernel", "runtime_locale"):
            self.assertNotIn(field, sanitized)
        self.assertEqual(sanitized["fingerprint"], {"brand": "Chrome"})
        self.assertEqual(sanitized["group"], "G")
        self.assertTrue(webdav_sync._should_exclude_profile_member("DevToolsActivePort", False))


    def test_pull_marks_browser_only_nodes_as_preserved(self) -> None:
        local: dict[str, object] = {"subscriptions": [], "nodes": [], "browser_instances": []}
        remote = {
            "subscriptions": [{"id": "sub", "name": "s", "node_ids": ["live"]}],
            "nodes": [{"id": "live", "name": "live"}, {"id": "orphan", "name": "orphan"}],
            "browser_instances": [
                {"id": "b1", "node_id": "live", "name": "a", "status": "stopped"},
                {"id": "b2", "node_id": "orphan", "name": "b", "status": "stopped"},
            ],
        }
        webdav_sync.apply_remote_state(local, remote, profile_path_builder=lambda n, i, legacy=False: Path(f"/tmp/{n}/{i}"))
        markers = {node["id"]: node.get(webdav_sync.LOCAL_BROWSER_NODE_MARKER) is True for node in local["nodes"]}
        self.assertEqual(markers, {"live": False, "orphan": True})


class BrokerFingerprintPayloadTests(unittest.TestCase):
    @staticmethod
    def payload(**overrides: object) -> dict[str, object]:
        return {
            "instance_id": "0123456789abcdef",
            "operation_id": "1" * 32,
            "profile_rel": "browser_profiles/node-record/0123456789abcdef",
            "fingerprint_rel": "fingerprint-chromium/versions/150.0.7871.186/chromium",
            "username": "proxybridge",
            "password": "correct-horse-battery-staple",
            "proxy_port": 21080,
            "fingerprint_seed": 99,
            "language": "en-US",
            "accept_language": "en-US,en;q=0.9",
            "timezone": "America/New_York",
            **overrides,
        }

    def test_defaults_match_previous_worker_switches(self) -> None:
        values = browser_broker._validate_start_payload(self.payload())
        self.assertEqual(values["platform"], "linux")
        self.assertEqual(
            browser_broker._worker_fingerprint_switches(values),
            [
                "--fingerprint=99",
                "--fingerprint-platform=linux",
                "--timezone=America/New_York",
                "--lang=en-US",
                "--accept-lang=en-US,en;q=0.9",
            ],
        )

    def test_extended_fingerprint_fields_are_validated_and_rendered(self) -> None:
        values = browser_broker._validate_start_payload(
            self.payload(
                platform="windows",
                brand="Chrome",
                brand_version="150.0.7871.186",
                hardware_concurrency=8,
                disable_spoofing=["gpu", "audio"],
                window_size="1600x900",
                extension_ids=["abcdef0123456789"],
                start_urls=["https://www.browserscan.net/?a=1"],
            )
        )
        switches = browser_broker._worker_fingerprint_switches(values)
        self.assertIn("--fingerprint-platform=windows", switches)
        self.assertIn("--fingerprint-brand=Chrome", switches)
        self.assertIn("--disable-spoofing=audio,gpu", switches)
        self.assertIn("--window-size=1600,900", switches)
        self.assertIn("--load-extension=/config/browser-profile/ProxyBridgeExtensions/abcdef0123456789", switches)
        self.assertEqual(values["start_urls"], ["https://www.browserscan.net/?a=1"])
        for overrides in (
            {"platform": "android"},
            {"brand": "Firefox"},
            {"hardware_concurrency": 3},
            {"disable_spoofing": ["webgl"]},
            {"window_size": "1x1"},
            {"extension_ids": ["../../etc"]},
            {"start_urls": ["https://a.test/ --remote-debugging-port=9222"]},
            {"start_urls": ["file:///etc/passwd"]},
        ):
            with self.subTest(overrides=overrides), self.assertRaises(browser_broker.BrokerError):
                browser_broker._validate_start_payload(self.payload(**overrides))


if __name__ == "__main__":
    unittest.main()
