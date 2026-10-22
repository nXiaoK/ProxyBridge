#!/usr/bin/env python3
"""软件更新：载荷选择与回退、签名清单、安装流程、发布工具与托盘替换辅助。"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import app_payload  # noqa: E402
import app_update  # noqa: E402
import build_update_release  # noqa: E402

REVISION_A = "a" * 64
REVISION_B = "b" * 64
TEST_SEED = bytes.fromhex("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb")
TEST_PUBLIC = "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c"


def write_payload(root: Path, version: str, *, app_source: str = "") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "app.py").write_text(app_source or "print('payload')\n", encoding="utf-8")
    (root / "proxybridge_version.py").write_text("VERSION = 'x'\n", encoding="utf-8")
    (root / "app_payload.py").write_text("", encoding="utf-8")
    (root / "app_update.py").write_text("", encoding="utf-8")
    (root / "VERSION").write_text(f"{version}\n", encoding="utf-8")
    (root / "static").mkdir(exist_ok=True)
    (root / "static" / "index.html").write_text("<!doctype html>", encoding="utf-8")
    return root


def write_shell(root: Path, version: str, revision: str = REVISION_A) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "VERSION").write_text(f"{version}\n", encoding="utf-8")
    (root / app_payload.SHELL_REVISION_FILENAME).write_text(f"{revision}\n", encoding="utf-8")
    return root


class VersionCompareTests(unittest.TestCase):
    def test_semver_precedence_matches_release_numbering(self) -> None:
        compare = app_payload.compare_versions
        self.assertEqual(compare("0.3.0-build.15", "0.3.0-build.9"), 1)
        self.assertEqual(compare("0.3.0-build.9", "0.3.0-build.15"), -1)
        self.assertEqual(compare("0.4.0-build.1", "0.3.0-build.99"), 1)
        self.assertEqual(compare("0.3.0", "0.3.0-build.99"), 1)
        self.assertEqual(compare("0.3.0-build.2", "0.3.0-build.2+meta"), 0)
        self.assertEqual(compare("1.0.0-alpha", "1.0.0-1"), 1)
        self.assertEqual(compare("1.0.0-alpha.1", "1.0.0-alpha"), 1)
        self.assertEqual(compare("0.3.0-build.1", "0.0.0-dev"), 1)
        with self.assertRaises(ValueError):
            compare("latest", "0.3.0")


class PayloadSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.work = base / "work"
        self.shell = write_shell(base / "shell", "0.3.0-build.10")
        self.version = "0.3.0-build.12"
        write_payload(app_payload.payload_dir(self.work, self.version), self.version)

    def select(self, **env: str) -> app_payload.PayloadSelection | None:
        return app_payload.select_payload(self.work, self.shell, environ=dict(env))

    def test_pending_payload_gets_one_attempt_then_falls_back(self) -> None:
        app_payload.activate_payload(self.work, self.version, REVISION_A)

        first = self.select()
        self.assertIsNotNone(first)
        self.assertEqual(first.version, self.version)
        self.assertEqual(app_payload.load_active(self.work)["attempts"], 1)

        # 未确认就再次启动：视为启动失败，回退并记录。
        self.assertIsNone(self.select())
        self.assertIsNone(app_payload.load_active(self.work))
        failures = app_payload.load_failures(self.work)
        self.assertEqual([item["version"] for item in failures], [self.version])
        self.assertTrue(app_payload.payload_failed_since(self.work, time.time() - 5))

    def test_confirmed_payload_is_reused(self) -> None:
        app_payload.activate_payload(self.work, self.version, REVISION_A)
        self.assertIsNotNone(self.select())
        self.assertTrue(app_payload.confirm_payload(self.work, self.version))
        for _ in range(3):
            self.assertIsNotNone(self.select())
        self.assertEqual(app_payload.load_active(self.work)["state"], "confirmed")

    def test_payload_not_newer_than_shell_is_retired(self) -> None:
        write_shell(self.shell, "0.3.0-build.12")
        app_payload.activate_payload(self.work, self.version, REVISION_A)
        self.assertIsNone(self.select())
        self.assertIsNone(app_payload.load_active(self.work))

    def test_shell_revision_mismatch_disabled_flag_and_broken_payload(self) -> None:
        app_payload.activate_payload(self.work, self.version, REVISION_B)
        self.assertIsNone(self.select())
        self.assertIsNotNone(app_payload.load_active(self.work), "修订号不一致时保留指针")

        app_payload.activate_payload(self.work, self.version, REVISION_A)
        self.assertIsNone(self.select(PROXY_BRIDGE_PAYLOAD_DISABLED="1"))

        (app_payload.payload_dir(self.work, self.version) / "static" / "index.html").unlink()
        self.assertIsNone(self.select())
        self.assertIsNone(app_payload.load_active(self.work))
        self.assertIn("static/index.html", app_payload.load_failures(self.work)[-1]["reason"])

    def test_export_environment_describes_selection(self) -> None:
        env: dict[str, str] = {"PROXY_BRIDGE_PAYLOAD_VERSION": "stale"}
        app_payload.export_shell_environment(self.shell, None, environ=env)
        self.assertEqual(env["PROXY_BRIDGE_SHELL_REVISION"], REVISION_A)
        self.assertEqual(Path(env["PROXY_BRIDGE_SHELL_ROOT"]), self.shell.resolve())
        self.assertNotIn("PROXY_BRIDGE_PAYLOAD_VERSION", env)
        selection = app_payload.PayloadSelection(app_payload.payload_dir(self.work, self.version), self.version)
        app_payload.export_shell_environment(self.shell, selection, environ=env)
        self.assertEqual(env["PROXY_BRIDGE_PAYLOAD_VERSION"], self.version)

    def test_frozen_finder_imports_payload_modules_and_can_be_abandoned(self) -> None:
        root = app_payload.payload_dir(self.work, self.version)
        (root / "pb_update_probe.py").write_text("SOURCE = 'payload'\n", encoding="utf-8")
        app_payload.activate_payload(self.work, self.version, REVISION_A)
        with mock.patch.dict(sys.modules), mock.patch.dict(os.environ), \
                mock.patch.object(sys, "meta_path", list(sys.meta_path)), \
                mock.patch.object(sys, "path", list(sys.path)):
            sys.modules["pb_update_probe"] = mock.Mock(SOURCE="stale")
            selection = app_payload.prepare_frozen_backend(self.work, self.shell)
            self.assertIsNotNone(selection)
            self.assertEqual(os.environ["PROXY_BRIDGE_PAYLOAD_VERSION"], self.version)
            import pb_update_probe  # type: ignore[import-not-found]

            self.assertEqual(pb_update_probe.SOURCE, "payload")
            self.assertEqual(Path(pb_update_probe.__file__).parent, root)

            app_payload.abandon_frozen_payload(self.work, selection, "导入失败")
            self.assertNotIn("pb_update_probe", sys.modules)
            self.assertNotIn("PROXY_BRIDGE_PAYLOAD_VERSION", os.environ)
            self.assertFalse(any(isinstance(item, app_payload._PayloadModuleFinder) for item in sys.meta_path))
        self.assertIsNone(app_payload.load_active(self.work))

    def test_independent_process_environment_resets_pyinstaller_state(self) -> None:
        env = app_payload.independent_process_environment(
            {"_PYI_ARCHIVE_FILE": "x", "_MEIPASS2": "y", "PATH": "/bin"}
        )
        self.assertEqual(env, {"PATH": "/bin", "PYINSTALLER_RESET_ENVIRONMENT": "1"})


@unittest.skipIf(os.name == "nt", "macOS 启动器使用 os.execv 回退，仅在 POSIX 上验证")
class LauncherProcessTests(unittest.TestCase):
    """真实子进程：app_payload.py 作为 macOS App 的后端入口。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.work = base / "work"
        self.shell = write_shell(base / "bridge", "0.3.0-build.10")
        shutil.copy2(ROOT / "app_payload.py", self.shell / "app_payload.py")
        (self.shell / "app.py").write_text("print('bundled', flush=True)\n", encoding="utf-8")
        self.version = "0.3.0-build.12"

    def run_launcher(self) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, "PROXY_BRIDGE_WORKDIR": str(self.work)}
        env.pop("PROXY_BRIDGE_PAYLOAD_DISABLED", None)
        return subprocess.run(
            [sys.executable, "-I", str(self.shell / "app_payload.py")],
            capture_output=True, text=True, env=env, timeout=60, check=False,
        )

    def test_runs_confirmed_payload_with_its_own_modules(self) -> None:
        root = write_payload(
            app_payload.payload_dir(self.work, self.version),
            self.version,
            app_source=textwrap.dedent("""
                import os, sys
                import app_payload
                print('payload', os.environ['PROXY_BRIDGE_PAYLOAD_VERSION'], sys.path[0], flush=True)
            """),
        )
        app_payload.activate_payload(self.work, self.version, REVISION_A)
        result = self.run_launcher()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"payload {self.version} {root}", result.stdout)

    def test_failed_payload_falls_back_to_bundled_backend(self) -> None:
        write_payload(
            app_payload.payload_dir(self.work, self.version),
            self.version,
            app_source="raise RuntimeError('broken release')\n",
        )
        app_payload.activate_payload(self.work, self.version, REVISION_A)
        result = self.run_launcher()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("bundled", result.stdout)
        self.assertIn("broken release", result.stderr)
        self.assertIsNone(app_payload.load_active(self.work))
        self.assertIn("broken release", app_payload.load_failures(self.work)[-1]["reason"])


class Ed25519Tests(unittest.TestCase):
    def test_rfc8032_vectors(self) -> None:
        seed = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
        self.assertEqual(
            app_update.ed25519_public_key(seed).hex(),
            "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a",
        )
        self.assertEqual(
            app_update.ed25519_sign(seed, b"").hex(),
            "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b",
        )
        signature = bytes.fromhex(
            "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da"
            "085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"
        )
        self.assertEqual(app_update.ed25519_public_key(TEST_SEED).hex(), TEST_PUBLIC)
        self.assertTrue(app_update.ed25519_verify(bytes.fromhex(TEST_PUBLIC), b"\x72", signature))
        self.assertFalse(app_update.ed25519_verify(bytes.fromhex(TEST_PUBLIC), b"\x73", signature))
        tampered = bytearray(signature)
        tampered[10] ^= 1
        self.assertFalse(app_update.ed25519_verify(bytes.fromhex(TEST_PUBLIC), b"\x72", bytes(tampered)))

    def test_embedded_public_key_is_valid(self) -> None:
        for key in app_update.TRUSTED_SIGNING_KEYS:
            self.assertIsNotNone(app_update._decompress(bytes.fromhex(key)))


class FakeRelease:
    """本地目录模拟 GitHub Release：fetch_json 返回 Release JSON，download_asset 从目录复制。"""

    def __init__(self, root: Path, version: str, *, shell_revision: str = REVISION_A, sign: bool = True) -> None:
        self.root = root
        self.version = version
        root.mkdir(parents=True, exist_ok=True)
        payload = root / app_update.payload_asset_name(version)
        with zipfile.ZipFile(payload, "w") as bundle:
            staging = write_payload(root / "staging", version)
            for path in sorted(staging.rglob("*")):
                if path.is_file():
                    bundle.write(path, path.relative_to(staging).as_posix())
        shutil.rmtree(root / "staging")
        exe = root / f"ProxyBridge-Windows-x64-{version}.exe"
        exe.write_bytes(b"MZ" + b"\0" * 64)
        mac = root / f"ProxyBridge-macOS-arm64-{version}.zip"
        mac.write_bytes(b"PK\x05\x06" + b"\0" * 18)
        entry = build_update_release._asset_entry
        data = app_update.build_manifest(
            version=version,
            tag=f"v{version}",
            commit="deadbeef",
            payload=entry(payload),
            shells={
                app_update.SHELL_KIND_MACOS: {"revision": shell_revision, "asset": entry(mac)},
                app_update.SHELL_KIND_WINDOWS: {"revision": shell_revision, "asset": entry(exe)},
            },
        )
        (root / app_update.MANIFEST_ASSET_NAME).write_bytes(data)
        if sign:
            (root / app_update.SIGNATURE_ASSET_NAME).write_text(
                app_update.ed25519_sign(TEST_SEED, data).hex() + "\n", encoding="ascii"
            )
        self.fetches = 0

    def release_json(self) -> dict[str, object]:
        assets = [
            {"name": path.name, "size": path.stat().st_size,
             "browser_download_url": f"https://example.invalid/{path.name}"}
            for path in sorted(self.root.iterdir()) if path.is_file()
        ]
        return {
            "tag_name": f"v{self.version}",
            "name": f"ProxyBridge {self.version}",
            "html_url": f"https://github.com/{app_update.UPDATE_REPOSITORY}/releases/tag/v{self.version}",
            "published_at": "2026-10-22T00:00:00Z",
            "body": "notes",
            "assets": assets,
        }

    def fetch_json(self, url: str) -> dict[str, object]:
        assert url == app_update.LATEST_RELEASE_API
        self.fetches += 1
        return self.release_json()

    def download_asset(self, asset: dict[str, object], target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.root / str(asset["name"]), target)


def extract_zip(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as bundle:
        bundle.extractall(destination)


class UpdateManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.work = self.base / "work"
        self.shell = write_shell(self.base / "shell", "0.3.0-build.10")
        self.logs: list[str] = []
        self.changes = 0

    def manager(self, release: FakeRelease, *, env: dict[str, str] | None = None,
                current: str = "0.3.0-build.10", platform: str = "linux") -> app_update.UpdateManager:
        def on_change() -> None:
            self.changes += 1

        return app_update.UpdateManager(
            current_version=current,
            work_root=lambda: self.work,
            fetch_json=release.fetch_json,
            download_asset=release.download_asset,
            extract_zip=extract_zip,
            log=lambda message, **_kwargs: self.logs.append(message),
            runtime_mode=lambda: "headless",
            on_change=on_change,
            environ=env if env is not None else {},
            trusted_keys=(TEST_PUBLIC,),
            platform=platform,
        )

    def shell_env(self, revision: str = REVISION_A, **extra: str) -> dict[str, str]:
        return {
            "PROXY_BRIDGE_SHELL_KIND": app_update.SHELL_KIND_WINDOWS,
            "PROXY_BRIDGE_SHELL_ROOT": str(self.shell),
            "PROXY_BRIDGE_SHELL_REVISION": revision,
            **extra,
        }

    def wait_idle(self, manager: app_update.UpdateManager) -> dict[str, object]:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            snapshot = manager.snapshot()
            if not snapshot["busy"]:
                return snapshot
            time.sleep(0.02)
        self.fail("更新任务没有结束")

    def test_source_runs_only_get_a_notification(self) -> None:
        release = FakeRelease(self.base / "release", "0.3.0-build.12")
        snapshot = self.manager(release).check()
        self.assertTrue(snapshot["available"])
        self.assertEqual(snapshot["mode"], "manual")
        self.assertIn("git pull", snapshot["reason"])
        self.assertEqual(snapshot["latest"]["version"], "0.3.0-build.12")
        self.assertFalse(snapshot["in_app"])

    def test_same_shell_revision_hot_updates_payload(self) -> None:
        release = FakeRelease(self.base / "release", "0.3.0-build.12")
        manager = self.manager(release, env=self.shell_env())
        self.assertEqual(manager.check()["mode"], "payload")

        manager.start_apply("0.3.0-build.12")
        snapshot = self.wait_idle(manager)
        self.assertEqual(snapshot["error"], "")
        self.assertTrue(snapshot["restarting"])
        self.assertEqual(manager.shell_request()["action"], "restart-backend")
        root = app_payload.payload_dir(self.work, "0.3.0-build.12")
        self.assertEqual(app_payload.validate_payload_dir(root, "0.3.0-build.12"), "")
        active = app_payload.load_active(self.work)
        self.assertEqual((active["version"], active["state"], active["shell_revision"]),
                         ("0.3.0-build.12", "pending", REVISION_A))
        self.assertEqual(release.fetches, 2, "安装前必须重新获取并验证 Release")
        # 新载荷可以被外壳选中。
        self.assertEqual(app_payload.select_payload(self.work, self.shell, environ={}).root, root)

    def test_shell_revision_change_requires_full_replacement(self) -> None:
        release = FakeRelease(self.base / "release", "0.3.0-build.12", shell_revision=REVISION_B)
        exe = self.base / "portable" / "ProxyBridge.exe"
        exe.parent.mkdir()
        exe.write_bytes(b"MZ")
        env = self.shell_env(PROXY_BRIDGE_SHELL_PATH=str(exe))
        self.assertEqual(self.manager(release, env=env, platform="win32").check()["mode"], "shell")
        snapshot = self.manager(release, env=env, platform="linux").check()
        self.assertEqual(snapshot["mode"], "manual")
        self.assertIn("不是 Windows", snapshot["reason"])

    def test_unsigned_or_forged_manifest_is_never_installed(self) -> None:
        unsigned = FakeRelease(self.base / "unsigned", "0.3.0-build.12", sign=False)
        snapshot = self.manager(unsigned, env=self.shell_env()).check()
        self.assertEqual(snapshot["mode"], "manual")
        self.assertIn("未签名", snapshot["reason"])

        forged = FakeRelease(self.base / "forged", "0.3.0-build.12")
        (forged.root / app_update.SIGNATURE_ASSET_NAME).write_text(
            app_update.ed25519_sign(b"\x01" * 32, (forged.root / app_update.MANIFEST_ASSET_NAME).read_bytes()).hex(),
            encoding="ascii",
        )
        manager = self.manager(forged, env=self.shell_env())
        snapshot = manager.check()
        self.assertEqual(snapshot["mode"], "manual")
        self.assertIn("签名校验失败", snapshot["reason"])
        manager.start_apply("0.3.0-build.12")
        self.assertIn("签名校验失败", self.wait_idle(manager)["error"])
        self.assertIsNone(app_payload.load_active(self.work))

    def test_payload_digest_mismatch_aborts_install(self) -> None:
        release = FakeRelease(self.base / "release", "0.3.0-build.12")
        payload = release.root / app_update.payload_asset_name("0.3.0-build.12")
        data = bytearray(payload.read_bytes())
        data[-1] ^= 1
        payload.write_bytes(bytes(data))
        manager = self.manager(release, env=self.shell_env())
        manager.start_apply("0.3.0-build.12")
        snapshot = self.wait_idle(manager)
        self.assertIn("SHA-256", snapshot["error"])
        self.assertIsNone(app_payload.load_active(self.work))
        self.assertIsNone(manager.shell_request())

    def test_apply_rejects_changed_latest_version_and_up_to_date(self) -> None:
        release = FakeRelease(self.base / "release", "0.3.0-build.12")
        manager = self.manager(release, env=self.shell_env())
        manager.start_apply("0.3.0-build.11")
        self.assertIn("0.3.0-build.12", self.wait_idle(manager)["error"])

        current = self.manager(release, env=self.shell_env(), current="0.3.0-build.12")
        self.assertFalse(current.check()["available"])
        with self.assertRaises(ValueError):
            current.start_apply("")

    def test_rollback_disables_running_payload(self) -> None:
        release = FakeRelease(self.base / "release", "0.3.0-build.12")
        app_payload.activate_payload(self.work, "0.3.0-build.12", REVISION_A)
        manager = self.manager(
            release,
            env=self.shell_env(PROXY_BRIDGE_PAYLOAD_VERSION="0.3.0-build.12"),
            current="0.3.0-build.12",
        )
        self.assertTrue(manager.snapshot()["can_rollback"])
        manager.rollback()
        self.assertIsNone(app_payload.load_active(self.work))
        self.assertEqual(app_payload.load_failures(self.work)[-1]["kind"], "rollback")
        self.assertEqual(manager.shell_request()["action"], "restart-backend")
        with self.assertRaises(RuntimeError):
            self.manager(release, env=self.shell_env()).rollback()

    def test_maintain_prunes_stale_payloads_and_reports_version_change(self) -> None:
        release = FakeRelease(self.base / "release", "0.3.0-build.12")
        write_payload(app_payload.payload_dir(self.work, "0.3.0-build.11"), "0.3.0-build.11")
        write_payload(app_payload.payload_dir(self.work, "0.3.0-build.12"), "0.3.0-build.12")
        app_payload.activate_payload(self.work, "0.3.0-build.12", REVISION_A)
        downloads = app_payload.updates_root(self.work) / app_update.DOWNLOADS_DIRNAME
        downloads.mkdir(parents=True)
        (downloads / "leftover.zip").write_bytes(b"x")
        app_payload.write_json_atomic(
            app_payload.updates_root(self.work) / app_update.LAST_RUN_FILENAME, {"version": "0.3.0-build.10"}
        )
        manager = self.manager(release, env=self.shell_env(PROXY_BRIDGE_PAYLOAD_VERSION="0.3.0-build.12"),
                               current="0.3.0-build.12")
        manager.confirm_running_payload()
        manager.maintain()
        self.assertEqual(app_payload.load_active(self.work)["state"], "confirmed")
        self.assertFalse(app_payload.payload_dir(self.work, "0.3.0-build.11").exists())
        self.assertTrue(app_payload.payload_dir(self.work, "0.3.0-build.12").exists())
        self.assertEqual(list(downloads.iterdir()), [])
        self.assertIn("已更新到 0.3.0-build.12", manager.snapshot()["message"])

    def test_check_result_is_cached_for_display(self) -> None:
        release = FakeRelease(self.base / "release", "0.3.0-build.12")
        self.manager(release, env=self.shell_env()).check()
        cached = self.manager(release, env=self.shell_env()).snapshot()
        self.assertEqual(cached["latest"]["version"], "0.3.0-build.12")
        self.assertEqual(cached["mode"], "payload")
        self.assertEqual(release.fetches, 1)

    def test_busy_manager_rejects_concurrent_operations(self) -> None:
        release = FakeRelease(self.base / "release", "0.3.0-build.12")
        gate = threading.Event()
        original = release.fetch_json

        def slow_fetch(url: str) -> dict[str, object]:
            gate.wait(5)
            return original(url)

        release.fetch_json = slow_fetch  # type: ignore[method-assign]
        manager = self.manager(release, env=self.shell_env())
        manager.start_apply("0.3.0-build.12")
        with self.assertRaises(app_update.UpdateBusyError):
            manager.check()
        gate.set()
        self.wait_idle(manager)

    @unittest.skipUnless(sys.platform == "darwin", "renameatx_np 仅在 macOS 可用")
    def test_macos_directory_swap_is_atomic(self) -> None:
        first = self.base / "new.app"
        second = self.base / "current.app"
        first.mkdir()
        second.mkdir()
        (first / "marker").write_text("new", encoding="utf-8")
        (second / "marker").write_text("old", encoding="utf-8")
        app_update._swap_directories(first, second)
        self.assertEqual((second / "marker").read_text(encoding="utf-8"), "new")
        self.assertEqual((first / "marker").read_text(encoding="utf-8"), "old")


class ManifestTests(unittest.TestCase):
    def manifest(self, **overrides: object) -> bytes:
        document: dict[str, object] = {
            "schema": 1,
            "version": "0.3.0-build.12",
            "tag": "v0.3.0-build.12",
            "commit": "abc",
            "payload": {"name": "ProxyBridge-payload-0.3.0-build.12.zip", "size": 10, "sha256": "0" * 64},
            "shells": {},
        }
        document.update(overrides)
        return json.dumps(document).encode("utf-8")

    def test_rejects_malformed_manifests(self) -> None:
        self.assertEqual(app_update.parse_manifest(self.manifest())["version"], "0.3.0-build.12")
        bad = [
            self.manifest(schema=2),
            self.manifest(version="next"),
            self.manifest(payload={"name": "other.zip", "size": 10, "sha256": "0" * 64}),
            self.manifest(payload={"name": "ProxyBridge-payload-0.3.0-build.12.zip", "size": 0, "sha256": "0" * 64}),
            self.manifest(shells={"windows-exe": {"revision": "x", "asset": {}}}),
            self.manifest(shells={"windows-exe": {"revision": "a" * 64,
                                                  "asset": {"name": "../evil.exe", "size": 1, "sha256": "0" * 64}}}),
            b"not json",
        ]
        for data in bad:
            with self.subTest(data=data[:60]), self.assertRaises(ValueError):
                app_update.parse_manifest(data)
        with self.assertRaises(ValueError):
            app_update.parse_signature(b"zz")


class ReleaseToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def test_shell_revision_ignores_line_endings_but_tracks_content(self) -> None:
        copy = self.base / "repo"
        for kind, files in build_update_release.SHELL_SOURCES.items():
            for relative in files:
                (copy / relative).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / relative, copy / relative)
        original = {kind: build_update_release.shell_revision(kind, copy) for kind in build_update_release.SHELL_SOURCES}
        tray = copy / "desktop" / "tray_host.py"
        tray.write_bytes(tray.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
        self.assertEqual(build_update_release.shell_revision("windows-exe", copy), original["windows-exe"])
        tray.write_bytes(tray.read_bytes() + b"# changed\r\n")
        self.assertNotEqual(build_update_release.shell_revision("windows-exe", copy), original["windows-exe"])
        self.assertEqual(build_update_release.shell_revision("macos-app", copy), original["macos-app"])

    def test_payload_archive_is_complete_and_reproducible(self) -> None:
        vendor = self.base / "vendor"
        (vendor / "yaml").mkdir(parents=True)
        (vendor / "yaml" / "__init__.py").write_text("", encoding="utf-8")
        (vendor / "yaml" / "__pycache__").mkdir()
        (vendor / "yaml" / "__pycache__" / "x.pyc").write_bytes(b"x")
        first = build_update_release.build_payload("0.3.0-build.12", self.base / "a", python=sys.executable,
                                                   vendor_from=str(vendor))
        second = build_update_release.build_payload("0.3.0-build.12", self.base / "b", python=sys.executable,
                                                    vendor_from=str(vendor))
        self.assertEqual(first.name, "ProxyBridge-payload-0.3.0-build.12.zip")
        self.assertEqual(first.read_bytes(), second.read_bytes())
        with zipfile.ZipFile(first) as bundle:
            names = set(bundle.namelist())
            self.assertEqual(bundle.read("VERSION").decode().strip(), "0.3.0-build.12")
        for module in build_update_release.PAYLOAD_MODULES:
            self.assertIn(module, names)
        self.assertIn("static/index.html", names)
        self.assertIn("vendor/yaml/__init__.py", names)
        self.assertFalse(any("__pycache__" in name for name in names))

    def write_assets(self, version: str) -> Path:
        assets = self.base / "assets"
        assets.mkdir()
        for name in (
            app_update.payload_asset_name(version),
            f"ProxyBridge-macOS-arm64-{version}.zip",
            f"ProxyBridge-Windows-x64-{version}.exe",
        ):
            (assets / name).write_bytes(name.encode("utf-8"))
        return assets

    def manifest_args(self, assets: Path) -> dict[str, object]:
        return {"version": "0.3.0-build.12", "tag": "v0.3.0-build.12", "commit": "abc", "assets_dir": assets,
                "macos_revision": REVISION_A, "windows_revision": REVISION_B}

    def test_manifest_signing_requires_a_trusted_key(self) -> None:
        assets = self.write_assets("0.3.0-build.12")
        with mock.patch.dict(os.environ, {build_update_release.SIGNING_KEY_ENV: ""}):
            manifest, signature = build_update_release.write_manifest(**self.manifest_args(assets))
            self.assertIsNone(signature)
            with self.assertRaises(SystemExit):
                build_update_release.write_manifest(**self.manifest_args(assets), require_signature=True)
        with mock.patch.dict(os.environ, {build_update_release.SIGNING_KEY_ENV: TEST_SEED.hex()}):
            with self.assertRaises(SystemExit):
                build_update_release.write_manifest(**self.manifest_args(assets))
            with mock.patch.object(app_update, "TRUSTED_SIGNING_KEYS", (TEST_PUBLIC,)):
                manifest, signature = build_update_release.write_manifest(**self.manifest_args(assets))
                data = manifest.read_bytes()
                self.assertTrue(app_update.verify_manifest_signature(
                    data, app_update.parse_signature(signature.read_bytes())))
        parsed = app_update.parse_manifest(data)
        self.assertEqual(parsed["shells"]["macos-app"]["revision"], REVISION_A)
        self.assertEqual(parsed["shells"]["windows-exe"]["asset"]["name"], "ProxyBridge-Windows-x64-0.3.0-build.12.exe")

    def test_every_backend_module_ships_in_payload_and_packages(self) -> None:
        sibling_imports = set()
        for line in (ROOT / "app.py").read_text(encoding="utf-8").splitlines():
            for prefix in ("import ", "from "):
                if line.startswith(prefix):
                    name = line[len(prefix):].split()[0].split(".")[0]
                    if (ROOT / f"{name}.py").is_file():
                        sibling_imports.add(f"{name}.py")
        sibling_imports.update({"app.py", "app_payload.py"})
        self.assertTrue(sibling_imports <= set(build_update_release.PAYLOAD_MODULES), sibling_imports)
        macos = (ROOT / "scripts" / "build_menubar_app.sh").read_text(encoding="utf-8")
        windows = (ROOT / "scripts" / "build_windows_exe.ps1").read_text(encoding="utf-8")
        linux = (ROOT / "scripts" / "build_linux_bin.sh").read_text(encoding="utf-8")
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        for module in build_update_release.PAYLOAD_MODULES:
            with self.subTest(module=module):
                self.assertIn(f'"$ROOT_DIR/{module}"', macos)
                self.assertIn(module, windows)
                self.assertIn(f'"{module}:."', linux)
                self.assertIn(module, dockerfile)
        self.assertIn('"$BRIDGE_DIR/SHELL_REVISION"', macos)
        self.assertIn('"$ShellRevisionPath;."', windows)


class TrayHostUpdateTests(unittest.TestCase):
    def setUp(self) -> None:
        from desktop import tray_host

        self.tray_host = tray_host
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def tray(self):
        config = self.tray_host.BackendConfig(
            python_executable=sys.executable,
            app_script=ROOT / "app.py",
            work_root=self.base / "work",
            port=18787,
        )
        with mock.patch.object(self.tray_host.autostart, "is_autostart_enabled", return_value=False):
            return self.tray_host.TrayApp(config)

    def test_shell_requests_are_dispatched_once(self) -> None:
        tray = self.tray()
        with mock.patch.object(tray, "restart_backend") as restart, mock.patch.object(tray, "quit") as quit_app:
            tray.handle_shell_request({"id": "r1", "action": "restart-backend"})
            tray.handle_shell_request({"id": "r1", "action": "restart-backend"})
            tray.handle_shell_request({"id": "q1", "action": "quit"})
            tray.handle_shell_request({"action": "quit"})
            tray.handle_shell_request("bogus")
        restart.assert_called_once_with(open_console=False, keep_token=True)
        quit_app.assert_called_once_with()

    def test_failed_payload_start_retries_with_bundled_backend(self) -> None:
        tray = self.tray()
        with mock.patch.object(tray.backend, "start", side_effect=RuntimeError("payload broke")), \
                mock.patch.object(tray.backend, "restart", return_value={}) as restart, \
                mock.patch.object(self.tray_host.app_payload, "payload_failed_since", return_value=True):
            tray._start_backend_with_payload_fallback()
        restart.assert_called_once_with(keep_token=True)
        with mock.patch.object(tray.backend, "start", side_effect=RuntimeError("other")), \
                mock.patch.object(self.tray_host.app_payload, "payload_failed_since", return_value=False):
            with self.assertRaises(RuntimeError):
                tray._start_backend_with_payload_fallback()

    def test_restart_can_keep_instance_token(self) -> None:
        tray = self.tray()
        token = tray.backend.instance_token
        with mock.patch.object(tray.backend, "stop"), mock.patch.object(tray.backend, "start"), \
                mock.patch.object(tray.backend, "wait_until_ready", return_value={}), \
                mock.patch.object(self.tray_host, "is_port_free", return_value=True):
            tray.backend.restart(keep_token=True)
            self.assertEqual(tray.backend.instance_token, token)
            tray.backend.restart()
            self.assertNotEqual(tray.backend.instance_token, token)

    def test_replace_executable_retries_until_target_is_released(self) -> None:
        source = self.base / "new.exe"
        target = self.base / "ProxyBridge.exe"
        source.write_bytes(b"MZnew")
        target.write_bytes(b"MZold")
        real_replace = os.replace
        calls = {"count": 0}

        def flaky_replace(src: object, dst: object) -> None:
            calls["count"] += 1
            if calls["count"] < 3:
                raise PermissionError("file in use")
            real_replace(src, dst)

        with mock.patch.object(self.tray_host.os, "replace", side_effect=flaky_replace), \
                mock.patch.object(self.tray_host.time, "sleep"):
            self.assertEqual(self.tray_host.replace_executable(source, target, timeout=30), "")
        self.assertEqual(target.read_bytes(), b"MZnew")
        self.assertEqual(calls["count"], 3)
        self.assertIn("相同", self.tray_host.replace_executable(target, target))

    def test_apply_update_replaces_target_and_relaunches_cleanly(self) -> None:
        target = self.base / "portable" / "ProxyBridge.exe"
        work = self.base / "work"
        env = {
            "PROXY_BRIDGE_WORKDIR": str(work),
            "PROXY_BRIDGE_INSTANCE_TOKEN": "secret",
            "PROXY_BRIDGE_PAYLOAD_DISABLED": "1",
            "_PYI_ARCHIVE_FILE": "old",
        }
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(self.tray_host, "wait_for_process_exit", return_value=True) as wait, \
                mock.patch.object(self.tray_host, "replace_executable", return_value="") as replace, \
                mock.patch.object(self.tray_host.subprocess, "Popen") as popen:
            code = self.tray_host.main([
                self.tray_host.APPLY_UPDATE_FLAG, str(target),
                self.tray_host.WAIT_PID_FLAG, "111", self.tray_host.WAIT_PID_FLAG, "222",
            ])
        self.assertEqual(code, 0)
        self.assertEqual([call.args[0] for call in wait.call_args_list], [111, 222])
        replace.assert_called_once()
        self.assertEqual(replace.call_args.args[1], target)
        launched_env = popen.call_args.kwargs["env"]
        self.assertEqual(popen.call_args.args[0], [str(target)])
        self.assertEqual(launched_env["PROXY_BRIDGE_WORKDIR"], str(work))
        self.assertNotIn("PROXY_BRIDGE_INSTANCE_TOKEN", launched_env)
        self.assertNotIn("PROXY_BRIDGE_PAYLOAD_DISABLED", launched_env)
        self.assertNotIn("_PYI_ARCHIVE_FILE", launched_env)
        self.assertEqual(launched_env["PYINSTALLER_RESET_ENVIRONMENT"], "1")
        result = json.loads((app_payload.updates_root(work) / "shell-update.json").read_text(encoding="utf-8"))
        self.assertTrue(result["ok"])


class AppIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        import app

        self.app = app
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.old = (app.WORK_ROOT, app.RUNTIME_ROOT, app.STATE_PATH)
        app.configure_work_root(Path(self.temp.name) / "work")
        app.ensure_dirs()
        self.addCleanup(self.restore)

    def restore(self) -> None:
        self.app.WORK_ROOT, self.app.RUNTIME_ROOT, self.app.STATE_PATH = self.old

    def test_update_settings_default_on_and_validate_input(self) -> None:
        self.assertEqual(self.app.normalize_update_settings(None), {"auto_check": True})
        self.assertEqual(self.app.normalize_update_settings({"auto_check": False}), {"auto_check": False})
        with self.assertRaises(ValueError):
            self.app.update_settings({"auto_check": "no"})
        result = self.app.update_settings({"auto_check": False})
        self.assertEqual(result["settings"], {"auto_check": False})
        self.assertFalse(result["update"]["auto_check"])
        self.assertFalse(self.app.update_auto_check_enabled())

    def test_shell_resource_root_follows_launcher_environment(self) -> None:
        with mock.patch.dict(os.environ, {"PROXY_BRIDGE_SHELL_ROOT": ""}):
            self.assertEqual(self.app.shell_resource_root(), self.app.APP_ROOT)
        with mock.patch.dict(os.environ, {"PROXY_BRIDGE_SHELL_ROOT": str(self.temp.name)}):
            self.assertEqual(self.app.shell_resource_root(), Path(self.temp.name))
            with mock.patch.dict(os.environ, {"PROXY_BRIDGE_BUNDLED_DIR": ""}):
                self.assertEqual(self.app.bundled_package_roots()[0], Path(self.temp.name) / "bundled")

    def test_update_status_payload_exposes_update_state(self) -> None:
        payload = self.app.update_status_payload()
        for key in ("current_version", "available", "mode", "busy", "in_app", "releases_url"):
            self.assertIn(key, payload)
        self.assertEqual(payload["current_version"], self.app.APP_VERSION)


if __name__ == "__main__":
    unittest.main()
