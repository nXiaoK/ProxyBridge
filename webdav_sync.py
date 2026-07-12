"""WebDAV sync for ProxyBridge subscriptions, nodes, and browser profiles.

v1: manual push/pull of state + stopped browser profiles
v1.1: hash-skip incremental profiles, auto-upload on browser stop, startup check
v2: LWW conflict metadata (device_id + updated_at), optional OpenSSL AES encryption
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable


SYNC_SCHEMA_VERSION = 1
MANIFEST_NAME = "manifest.json"
STATE_SYNC_NAME = "state.sync.json"
PROFILES_DIR_NAME = "profiles"
DEFAULT_REMOTE_ROOT = "ProxyBridge"

# Chromium profile noise that is large and/or unsafe to copy while hot.
PROFILE_EXCLUDE_DIR_NAMES = {
    "Cache",
    "Code Cache",
    "GPUCache",
    "ShaderCache",
    "GrShaderCache",
    "GraphiteDawnCache",
    "DawnGraphiteCache",
    "DawnWebGPUCache",
    "BrowserMetrics",
    "Crashpad",
    "Safe Browsing",
    "component_crx_cache",
    "extensions_crx_cache",
    "optimization_guide_model_store",
}
PROFILE_EXCLUDE_FILE_NAMES = {
    "LOCK",
    "lockfile",
    "SingletonLock",
    "SingletonCookie",
    "SingletonSocket",
    "LOG",
    "LOG.old",
}
PROFILE_EXCLUDE_NAME_SUBSTR = (
    "Cache",
    "cache",
    "Singleton",
)

LogFn = Callable[..., None]


def _default_log(message: str, level: str = "info", category: str = "同步") -> None:
    print(f"[{level}] [{category}] {message}", flush=True)


def new_device_id() -> str:
    return secrets.token_hex(8)


def default_webdav_settings() -> dict[str, Any]:
    return {
        "url": "",
        "username": "",
        "password": "",
        "remote_root": DEFAULT_REMOTE_ROOT,
        "enabled": False,
        "auto_upload_on_browser_stop": False,
        "check_on_startup": False,
        "encrypt": False,
        "encrypt_password": "",
        "device_id": new_device_id(),
        "last_push_at": None,
        "last_pull_at": None,
        "last_remote_updated_at": None,
        "last_local_change_at": None,
        # SHA-256 of the last applied/uploaded state.sync.json body (manifest.state_sha256).
        "last_applied_state_sha256": "",
        "last_error": "",
        "last_status": "",
    }


def normalize_webdav_settings(raw: Any) -> dict[str, Any]:
    base = default_webdav_settings()
    if not isinstance(raw, dict):
        return base
    base.update({k: raw.get(k, base[k]) for k in base})
    base["url"] = str(base.get("url") or "").strip()
    base["username"] = str(base.get("username") or "").strip()
    base["password"] = str(base.get("password") or "")
    base["remote_root"] = str(base.get("remote_root") or DEFAULT_REMOTE_ROOT).strip().strip("/") or DEFAULT_REMOTE_ROOT
    base["enabled"] = bool(base.get("enabled"))
    base["auto_upload_on_browser_stop"] = bool(base.get("auto_upload_on_browser_stop"))
    base["check_on_startup"] = bool(base.get("check_on_startup"))
    base["encrypt"] = bool(base.get("encrypt"))
    base["encrypt_password"] = str(base.get("encrypt_password") or "")
    device_id = str(base.get("device_id") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]{8,64}", device_id or ""):
        device_id = new_device_id()
    base["device_id"] = device_id
    for key in ("last_push_at", "last_pull_at", "last_remote_updated_at", "last_local_change_at"):
        value = base.get(key)
        if value in ("", None):
            base[key] = None
        else:
            try:
                base[key] = int(value)
            except (TypeError, ValueError):
                base[key] = None
    base["last_applied_state_sha256"] = str(base.get("last_applied_state_sha256") or "").strip().lower()
    base["last_error"] = str(base.get("last_error") or "")
    base["last_status"] = str(base.get("last_status") or "")
    return base


def public_webdav_settings(settings: dict[str, Any]) -> dict[str, Any]:
    """Settings safe for console JSON (mask secrets)."""
    cfg = normalize_webdav_settings(settings)
    return {
        "url": cfg["url"],
        "username": cfg["username"],
        "password_set": bool(cfg["password"]),
        "remote_root": cfg["remote_root"],
        "enabled": cfg["enabled"],
        "auto_upload_on_browser_stop": cfg["auto_upload_on_browser_stop"],
        "check_on_startup": cfg["check_on_startup"],
        "encrypt": cfg["encrypt"],
        "encrypt_password_set": bool(cfg["encrypt_password"]),
        "device_id": cfg["device_id"],
        "last_push_at": cfg["last_push_at"],
        "last_pull_at": cfg["last_pull_at"],
        "last_remote_updated_at": cfg["last_remote_updated_at"],
        "last_local_change_at": cfg["last_local_change_at"],
        "last_applied_state_sha256": cfg["last_applied_state_sha256"],
        "last_error": cfg["last_error"],
        "last_status": cfg["last_status"],
    }


def remote_is_already_latest(cfg: dict[str, Any], manifest: dict[str, Any]) -> bool:
    """Return True when local already has the remote snapshot (skip full pull).

    Primary signal: manifest.state_sha256 == settings.last_applied_state_sha256
    (set after a successful push or pull of that exact state.sync.json).

    Fallback when sha is missing (older remotes):
    - remote.updated_at <= last_pull_at, or
    - remote is from this device and updated_at <= last_remote_updated_at.
    """
    if not isinstance(manifest, dict):
        return False
    remote_sha = str(manifest.get("state_sha256") or "").strip().lower()
    local_sha = str(cfg.get("last_applied_state_sha256") or "").strip().lower()
    if remote_sha and local_sha and remote_sha == local_sha:
        return True

    remote_updated = 0
    try:
        remote_updated = int(manifest.get("updated_at") or 0)
    except (TypeError, ValueError):
        remote_updated = 0
    if not remote_updated:
        return False

    last_pull = int(cfg.get("last_pull_at") or 0)
    last_remote = int(cfg.get("last_remote_updated_at") or 0)
    local_device = str(cfg.get("device_id") or "")
    remote_device = str(manifest.get("device_id") or "")

    # Prefer sha when present: differing sha means not latest even if timestamps look equal.
    if remote_sha and local_sha and remote_sha != local_sha:
        return False
    if remote_sha and not local_sha:
        # Upgraded client / first pull after feature: must pull once to record sha.
        return False

    if remote_updated <= last_pull:
        return True
    if remote_device and remote_device == local_device and remote_updated <= last_remote:
        return True
    return False


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _should_exclude_profile_member(name: str, is_dir: bool) -> bool:
    base = Path(name).name
    if base in PROFILE_EXCLUDE_FILE_NAMES:
        return True
    if is_dir and base in PROFILE_EXCLUDE_DIR_NAMES:
        return True
    for part in Path(name).parts:
        if part in PROFILE_EXCLUDE_DIR_NAMES:
            return True
        if any(token in part for token in PROFILE_EXCLUDE_NAME_SUBSTR) and part not in {
            "Session Storage",
            "Local Storage",
            "Service Worker",
        }:
            # Keep Storage dirs; drop generic *Cache* path segments.
            if "Storage" in part or part in {"IndexedDB", "File System"}:
                continue
            if "Cache" in part or "cache" in part:
                return True
    return False


def pack_profile_dir(profile_path: Path) -> bytes:
    """Tar.gz a Chromium profile, excluding cache/lock noise."""
    if not profile_path.exists() or not profile_path.is_dir():
        raise FileNotFoundError(f"浏览器 Profile 不存在: {profile_path}")
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for root, dirs, files in os.walk(profile_path):
            rel_root = Path(root).relative_to(profile_path)
            # Prune excluded directories in-place.
            dirs[:] = [
                d
                for d in dirs
                if not _should_exclude_profile_member(str(rel_root / d) if str(rel_root) != "." else d, True)
            ]
            for filename in files:
                rel = rel_root / filename if str(rel_root) != "." else Path(filename)
                rel_name = rel.as_posix()
                if _should_exclude_profile_member(rel_name, False):
                    continue
                full = Path(root) / filename
                try:
                    if full.is_symlink():
                        continue
                    archive.add(full, arcname=rel_name, recursive=False)
                except OSError:
                    continue
    return buffer.getvalue()


def unpack_profile_dir(archive_bytes: bytes, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as archive:
        # Python 3.12+ has data filter; keep portable extraction.
        for member in archive.getmembers():
            name = member.name.replace("\\", "/")
            if name.startswith("/") or ".." in Path(name).parts:
                raise RuntimeError(f"不安全的 Profile 归档路径: {name}")
            if member.issym() or member.islnk():
                continue
            archive.extract(member, path=dest)


def openssl_available() -> bool:
    return shutil.which("openssl") is not None


def encrypt_payload(data: bytes, password: str) -> bytes:
    if not password:
        return data
    if not openssl_available():
        raise RuntimeError("启用了同步加密，但系统未找到 openssl")
    env = os.environ.copy()
    env["PB_SYNC_PASS"] = password
    proc = subprocess.run(
        ["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-salt", "-pass", "env:PB_SYNC_PASS"],
        input=data,
        capture_output=True,
        env=env,
        check=False,
    )
    if proc.returncode != 0:
        err = (proc.stderr or b"").decode("utf-8", "ignore")
        raise RuntimeError(f"加密失败: {err or proc.returncode}")
    return b"PBENC1" + proc.stdout


def decrypt_payload(data: bytes, password: str) -> bytes:
    if not data.startswith(b"PBENC1"):
        return data
    if not password:
        raise RuntimeError("远端数据已加密，请填写同步加密密码")
    if not openssl_available():
        raise RuntimeError("远端数据已加密，但系统未找到 openssl")
    cipher = data[len(b"PBENC1") :]
    env = os.environ.copy()
    env["PB_SYNC_PASS"] = password
    proc = subprocess.run(
        ["openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-pass", "env:PB_SYNC_PASS"],
        input=cipher,
        capture_output=True,
        env=env,
        check=False,
    )
    if proc.returncode != 0:
        err = (proc.stderr or b"").decode("utf-8", "ignore")
        raise RuntimeError(f"解密失败（密码可能不正确）: {err or proc.returncode}")
    return proc.stdout


def sanitize_browser_instance_for_sync(instance: dict[str, Any]) -> dict[str, Any]:
    record = dict(instance)
    record["status"] = "stopped"
    record["pid"] = None
    record["bridge_id"] = None
    record["proxy"] = ""
    # Drop absolute path; receiver rebuilds from node_id + instance id.
    record.pop("profile_path", None)
    return record


def build_sync_state(state: dict[str, Any], *, device_id: str, profile_hashes: dict[str, str]) -> dict[str, Any]:
    instances = [
        sanitize_browser_instance_for_sync(item)
        for item in state.get("browser_instances", [])
        if isinstance(item, dict)
    ]
    return {
        "schema_version": SYNC_SCHEMA_VERSION,
        "device_id": device_id,
        "updated_at": int(time.time()),
        "subscriptions": list(state.get("subscriptions") or []),
        "nodes": list(state.get("nodes") or []),
        "browser_instances": instances,
        "profile_hashes": profile_hashes,
    }


class WebDAVClient:
    """Minimal WebDAV client (Basic auth) using urllib."""

    def __init__(self, base_url: str, username: str = "", password: str = "", timeout: float = 60.0) -> None:
        raw = (base_url or "").strip()
        if not raw:
            raise ValueError("WebDAV URL 不能为空")
        parsed = urllib.parse.urlsplit(raw)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("WebDAV URL 必须是 http(s)://host[/path]")
        # Normalize trailing slash off for join logic; collections use explicit slash.
        path = parsed.path or "/"
        self.base = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path.rstrip("/"), "", ""))
        self.username = username or ""
        self.password = password or ""
        self.timeout = timeout
        self._opener = urllib.request.build_opener(urllib.request.HTTPSHandler())

    def _auth_header(self) -> str | None:
        if not self.username and not self.password:
            return None
        token = base64.b64encode(f"{self.username}:{self.password}".encode("utf-8")).decode("ascii")
        return f"Basic {token}"

    def url_for(self, *parts: str) -> str:
        segments = [self.base]
        for part in parts:
            cleaned = str(part or "").strip("/")
            if cleaned:
                segments.append(cleaned)
        return "/".join(segments)

    def request(
        self,
        method: str,
        url: str,
        *,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
        allow_statuses: set[int] | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        req_headers = dict(headers or {})
        auth = self._auth_header()
        if auth:
            req_headers["Authorization"] = auth
        request = urllib.request.Request(url, data=data, method=method, headers=req_headers)
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                body = response.read()
                status = getattr(response, "status", None) or response.getcode()
                resp_headers = {k.lower(): v for k, v in response.headers.items()}
                return int(status), resp_headers, body
        except urllib.error.HTTPError as exc:
            body = exc.read() if hasattr(exc, "read") else b""
            status = int(exc.code)
            if allow_statuses and status in allow_statuses:
                resp_headers = {k.lower(): v for k, v in (exc.headers or {}).items()}
                return status, resp_headers, body
            message = body.decode("utf-8", "ignore")[:300] if body else str(exc)
            raise RuntimeError(f"WebDAV {method} {url} 失败 ({status}): {message}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"WebDAV 连接失败: {exc}") from exc

    def ensure_collection(self, url: str) -> None:
        # MKCOL; 405/409/301 mean exists or not supported for leaf.
        status, _, _ = self.request("MKCOL", url if url.endswith("/") else url + "/", allow_statuses={201, 405, 409, 301, 200})
        if status not in {201, 405, 409, 301, 200}:
            raise RuntimeError(f"创建远端目录失败: {status}")

    def put_bytes(self, url: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        self.request(
            "PUT",
            url,
            data=data,
            headers={"Content-Type": content_type, "Content-Length": str(len(data))},
            allow_statuses={200, 201, 204},
        )

    def get_bytes(self, url: str) -> bytes:
        _status, _headers, body = self.request("GET", url, allow_statuses={200})
        return body

    def exists(self, url: str) -> bool:
        try:
            status, _, _ = self.request("HEAD", url, allow_statuses={200, 404})
            return status == 200
        except RuntimeError:
            try:
                status, _, _ = self.request("GET", url, allow_statuses={200, 404})
                return status == 200
            except RuntimeError:
                return False

    def test_connection(self) -> dict[str, Any]:
        # PROPFIND depth 0 on base
        propfind = (
            b'<?xml version="1.0" encoding="utf-8"?>'
            b'<d:propfind xmlns:d="DAV:"><d:prop><d:resourcetype/></d:prop></d:propfind>'
        )
        status, headers, body = self.request(
            "PROPFIND",
            self.base if self.base.endswith("/") else self.base + "/",
            data=propfind,
            headers={"Depth": "0", "Content-Type": "application/xml; charset=utf-8"},
            allow_statuses={200, 207, 404, 405},
        )
        if status in {404, 405}:
            # Some endpoints reject PROPFIND on root; try GET
            try:
                self.request("GET", self.base, allow_statuses={200, 301, 302, 401, 403, 404, 405})
            except RuntimeError as exc:
                if "401" in str(exc):
                    raise RuntimeError("WebDAV 认证失败，请检查用户名和密码") from exc
        if status == 401:
            raise RuntimeError("WebDAV 认证失败，请检查用户名和密码")
        return {"ok": True, "status": status, "content_type": headers.get("content-type", "")}


class SyncEngine:
    def __init__(
        self,
        *,
        work_root: Path,
        state_path: Path,
        browser_profiles_root: Path,
        load_state: Callable[[], dict[str, Any]],
        save_state: Callable[[dict[str, Any]], None],
        state_lock: threading.RLock,
        browser_instance_profile_path: Callable[..., Path],
        browser_runtime_is_active: Callable[[str], bool],
        log: LogFn | None = None,
        client_factory: Callable[[dict[str, Any]], WebDAVClient] | None = None,
    ) -> None:
        self.work_root = work_root
        self.state_path = state_path
        self.browser_profiles_root = browser_profiles_root
        self.load_state = load_state
        self.save_state = save_state
        self.state_lock = state_lock
        self.browser_instance_profile_path = browser_instance_profile_path
        self.browser_runtime_is_active = browser_runtime_is_active
        self.log = log or _default_log
        self.client_factory = client_factory or self._default_client
        self._bg_lock = threading.Lock()

    def _default_client(self, cfg: dict[str, Any]) -> WebDAVClient:
        return WebDAVClient(cfg["url"], cfg.get("username") or "", cfg.get("password") or "")

    def get_settings(self, state: dict[str, Any] | None = None) -> dict[str, Any]:
        if state is None:
            with self.state_lock:
                state = self.load_state()
        settings = state.setdefault("settings", {})
        if not isinstance(settings, dict):
            settings = {}
            state["settings"] = settings
        cfg = normalize_webdav_settings(settings.get("webdav"))
        settings["webdav"] = cfg
        return cfg

    def update_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self.state_lock:
            state = self.load_state()
            cfg = self.get_settings(state)
            if "url" in payload:
                cfg["url"] = str(payload.get("url") or "").strip()
            if "username" in payload:
                cfg["username"] = str(payload.get("username") or "").strip()
            if "password" in payload:
                # Empty string means "leave unchanged" when password_set dance is used from UI.
                password = payload.get("password")
                if password is not None and str(password) != "":
                    cfg["password"] = str(password)
                elif payload.get("clear_password"):
                    cfg["password"] = ""
            if "remote_root" in payload:
                cfg["remote_root"] = str(payload.get("remote_root") or DEFAULT_REMOTE_ROOT).strip().strip("/") or DEFAULT_REMOTE_ROOT
            for key in (
                "enabled",
                "auto_upload_on_browser_stop",
                "check_on_startup",
                "encrypt",
            ):
                if key in payload:
                    cfg[key] = bool(payload.get(key))
            if "encrypt_password" in payload:
                enc = payload.get("encrypt_password")
                if enc is not None and str(enc) != "":
                    cfg["encrypt_password"] = str(enc)
                elif payload.get("clear_encrypt_password"):
                    cfg["encrypt_password"] = ""
            cfg = normalize_webdav_settings(cfg)
            state.setdefault("settings", {})["webdav"] = cfg
            self.save_state(state)
            return public_webdav_settings(cfg)

    def _require_cfg(self, cfg: dict[str, Any]) -> None:
        if not cfg.get("url"):
            raise ValueError("请先配置 WebDAV URL")

    def _remote_urls(self, client: WebDAVClient, cfg: dict[str, Any]) -> dict[str, str]:
        root = client.url_for(cfg["remote_root"])
        return {
            "root": root,
            "manifest": client.url_for(cfg["remote_root"], MANIFEST_NAME),
            "state": client.url_for(cfg["remote_root"], STATE_SYNC_NAME),
            "profiles": client.url_for(cfg["remote_root"], PROFILES_DIR_NAME),
        }

    def _ensure_remote_layout(self, client: WebDAVClient, urls: dict[str, str]) -> None:
        client.ensure_collection(urls["root"])
        client.ensure_collection(urls["profiles"])

    def _encode_blob(self, data: bytes, cfg: dict[str, Any]) -> bytes:
        if cfg.get("encrypt"):
            return encrypt_payload(data, str(cfg.get("encrypt_password") or ""))
        return data

    def _decode_blob(self, data: bytes, cfg: dict[str, Any]) -> bytes:
        if data.startswith(b"PBENC1") or cfg.get("encrypt"):
            return decrypt_payload(data, str(cfg.get("encrypt_password") or ""))
        return data

    def test(self) -> dict[str, Any]:
        with self.state_lock:
            state = self.load_state()
            cfg = self.get_settings(state)
        self._require_cfg(cfg)
        client = self.client_factory(cfg)
        result = client.test_connection()
        urls = self._remote_urls(client, cfg)
        try:
            self._ensure_remote_layout(client, urls)
            result["layout_ok"] = True
        except Exception as exc:
            result["layout_ok"] = False
            result["layout_error"] = str(exc)
        with self.state_lock:
            state = self.load_state()
            cfg = self.get_settings(state)
            cfg["last_error"] = ""
            cfg["last_status"] = "连接测试成功"
            state["settings"]["webdav"] = cfg
            self.save_state(state)
        self.log("WebDAV 连接测试成功", category="同步")
        return {"ok": True, **result, "settings": public_webdav_settings(cfg)}

    def _profile_path_for(self, instance: dict[str, Any]) -> Path:
        node_id = str(instance.get("node_id") or "")
        instance_id = str(instance.get("id") or "")
        legacy = bool(instance.get("legacy"))
        return self.browser_instance_profile_path(node_id, instance_id, legacy)

    def _collect_pushable_profiles(self, state: dict[str, Any]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for instance in state.get("browser_instances", []):
            if not isinstance(instance, dict):
                continue
            instance_id = str(instance.get("id") or "")
            if not instance_id:
                continue
            if instance.get("status") in {"running", "starting"} or self.browser_runtime_is_active(instance_id):
                continue
            path = self._profile_path_for(instance)
            if path.exists() and path.is_dir():
                result.append(instance)
        return result

    def push(self, *, instance_ids: list[str] | None = None) -> dict[str, Any]:
        with self.state_lock:
            state = self.load_state()
            cfg = self.get_settings(state)
            self._require_cfg(cfg)
            if not cfg.get("enabled") and not cfg.get("url"):
                raise ValueError("请先启用并配置 WebDAV")

            instances = self._collect_pushable_profiles(state)
            if instance_ids is not None:
                wanted = set(instance_ids)
                instances = [item for item in instances if str(item.get("id") or "") in wanted]

            skipped_running = [
                str(item.get("id") or "")
                for item in state.get("browser_instances", [])
                if isinstance(item, dict)
                and (
                    item.get("status") in {"running", "starting"}
                    or self.browser_runtime_is_active(str(item.get("id") or ""))
                )
            ]

        client = self.client_factory(cfg)
        urls = self._remote_urls(client, cfg)
        self._ensure_remote_layout(client, urls)

        # Read remote manifest for hash skip (v1.1).
        remote_hashes: dict[str, str] = {}
        remote_manifest: dict[str, Any] = {}
        try:
            if client.exists(urls["manifest"]):
                raw_manifest = self._decode_blob(client.get_bytes(urls["manifest"]), cfg)
                remote_manifest = json.loads(raw_manifest.decode("utf-8"))
                if isinstance(remote_manifest.get("profile_hashes"), dict):
                    remote_hashes = {
                        str(k): str(v) for k, v in remote_manifest["profile_hashes"].items() if k and v
                    }
        except Exception:
            remote_manifest = {}
            remote_hashes = {}

        profile_hashes: dict[str, str] = dict(remote_hashes)
        uploaded: list[str] = []
        skipped_unchanged: list[str] = []
        errors: list[str] = []

        for instance in instances:
            instance_id = str(instance.get("id") or "")
            try:
                archive = pack_profile_dir(self._profile_path_for(instance))
                digest = sha256_bytes(archive)
                if remote_hashes.get(instance_id) == digest:
                    skipped_unchanged.append(instance_id)
                    profile_hashes[instance_id] = digest
                    continue
                blob = self._encode_blob(archive, cfg)
                client.put_bytes(
                    client.url_for(cfg["remote_root"], PROFILES_DIR_NAME, f"{instance_id}.tar.gz"),
                    blob,
                    content_type="application/gzip",
                )
                profile_hashes[instance_id] = digest
                uploaded.append(instance_id)
            except Exception as exc:
                errors.append(f"{instance_id}: {exc}")

        with self.state_lock:
            state = self.load_state()
            cfg = self.get_settings(state)
            sync_state = build_sync_state(state, device_id=str(cfg["device_id"]), profile_hashes=profile_hashes)
            sync_bytes = json.dumps(sync_state, ensure_ascii=False, indent=2).encode("utf-8")
            manifest = {
                "schema_version": SYNC_SCHEMA_VERSION,
                "device_id": cfg["device_id"],
                "updated_at": sync_state["updated_at"],
                "state_sha256": sha256_bytes(sync_bytes),
                "profile_hashes": profile_hashes,
                "subscription_count": len(sync_state.get("subscriptions") or []),
                "node_count": len(sync_state.get("nodes") or []),
                "browser_instance_count": len(sync_state.get("browser_instances") or []),
                "profile_count": len(profile_hashes),
            }

        client.put_bytes(
            urls["state"],
            self._encode_blob(sync_bytes, cfg),
            content_type="application/json",
        )
        client.put_bytes(
            urls["manifest"],
            self._encode_blob(json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"), cfg),
            content_type="application/json",
        )

        now = int(time.time())
        with self.state_lock:
            state = self.load_state()
            cfg = self.get_settings(state)
            cfg["last_push_at"] = now
            cfg["last_remote_updated_at"] = int(manifest["updated_at"])
            cfg["last_applied_state_sha256"] = str(manifest.get("state_sha256") or "")
            cfg["last_error"] = "; ".join(errors) if errors else ""
            cfg["last_status"] = (
                f"已上传：{len(uploaded)} 个 Profile，跳过未变 {len(skipped_unchanged)}，"
                f"跳过运行中 {len(skipped_running)}"
            )
            state["settings"]["webdav"] = cfg
            self.save_state(state)

        self.log(cfg["last_status"], category="同步")
        return {
            "ok": not errors,
            "uploaded_profiles": uploaded,
            "skipped_unchanged": skipped_unchanged,
            "skipped_running": skipped_running,
            "errors": errors,
            "manifest": manifest,
            "settings": public_webdav_settings(cfg),
        }

    def pull(self, *, force: bool = False) -> dict[str, Any]:
        with self.state_lock:
            state = self.load_state()
            cfg = self.get_settings(state)
            self._require_cfg(cfg)
            local_last_change = cfg.get("last_local_change_at")
            local_device = cfg.get("device_id")

        client = self.client_factory(cfg)
        urls = self._remote_urls(client, cfg)
        if not client.exists(urls["manifest"]):
            raise RuntimeError("远端还没有同步数据，请先在另一台电脑上上传")

        raw_manifest = self._decode_blob(client.get_bytes(urls["manifest"]), cfg)
        manifest = json.loads(raw_manifest.decode("utf-8"))
        if not isinstance(manifest, dict):
            raise RuntimeError("远端 manifest.json 格式无效")

        # 「已最新则跳过」：只读 manifest 即可判定，避免再下 state/profiles。
        if not force and remote_is_already_latest(cfg, manifest):
            status = "已是最新，跳过拉取"
            with self.state_lock:
                state = self.load_state()
                cfg = self.get_settings(state)
                cfg["last_error"] = ""
                cfg["last_status"] = status
                # Keep last_remote_updated_at aligned with manifest clock for UI.
                try:
                    remote_updated_preview = int(manifest.get("updated_at") or 0) or None
                except (TypeError, ValueError):
                    remote_updated_preview = None
                if remote_updated_preview:
                    cfg["last_remote_updated_at"] = remote_updated_preview
                if manifest.get("state_sha256"):
                    cfg["last_applied_state_sha256"] = str(manifest.get("state_sha256") or "")
                state["settings"]["webdav"] = cfg
                self.save_state(state)
            self.log(status, category="同步")
            return {
                "ok": True,
                "skipped": True,
                "reason": "already_latest",
                "downloaded_profiles": [],
                "skipped_running": [],
                "skipped_unchanged": [],
                "errors": [],
                "conflict_warning": "",
                "manifest": manifest,
                "applied": {"subscriptions": 0, "nodes": 0, "browser_instances": 0},
                "settings": public_webdav_settings(cfg),
            }

        raw_state = self._decode_blob(client.get_bytes(urls["state"]), cfg)
        remote_state = json.loads(raw_state.decode("utf-8"))
        if not isinstance(remote_state, dict):
            raise RuntimeError("远端 state.sync.json 格式无效")

        remote_updated = int(manifest.get("updated_at") or remote_state.get("updated_at") or 0)
        conflict_warning = ""
        if (
            local_last_change
            and remote_updated
            and int(local_last_change) > int(cfg.get("last_pull_at") or 0)
            and str(manifest.get("device_id") or "") != str(local_device)
        ):
            conflict_warning = (
                "本地在上次拉取之后有修改，且远端来自其他设备；已按远端后写覆盖（LWW）。"
            )

        profile_hashes = remote_state.get("profile_hashes") or manifest.get("profile_hashes") or {}
        if not isinstance(profile_hashes, dict):
            profile_hashes = {}

        downloaded: list[str] = []
        skipped_running: list[str] = []
        skipped_unchanged: list[str] = []
        errors: list[str] = []

        with self.state_lock:
            state = self.load_state()
            local_instances = {
                str(item.get("id") or ""): item
                for item in state.get("browser_instances", [])
                if isinstance(item, dict) and item.get("id")
            }

        for instance_id, expected_hash in profile_hashes.items():
            instance_id = str(instance_id)
            if not instance_id:
                continue
            local = local_instances.get(instance_id)
            if local and (
                local.get("status") in {"running", "starting"} or self.browser_runtime_is_active(instance_id)
            ):
                skipped_running.append(instance_id)
                continue
            try:
                # Determine destination path from remote instance meta when possible.
                remote_instances = {
                    str(item.get("id") or ""): item
                    for item in remote_state.get("browser_instances") or []
                    if isinstance(item, dict)
                }
                meta = remote_instances.get(instance_id) or local or {}
                dest = self.browser_instance_profile_path(
                    str(meta.get("node_id") or ""),
                    instance_id,
                    bool(meta.get("legacy")),
                )
                if dest.exists():
                    try:
                        # Cheap skip: if a marker hash file matches.
                        marker = dest / ".pb-sync-sha256"
                        if marker.exists() and marker.read_text(encoding="utf-8").strip() == str(expected_hash):
                            skipped_unchanged.append(instance_id)
                            continue
                    except OSError:
                        pass
                url = client.url_for(cfg["remote_root"], PROFILES_DIR_NAME, f"{instance_id}.tar.gz")
                if not client.exists(url):
                    errors.append(f"{instance_id}: 远端缺少 Profile 包")
                    continue
                blob = self._decode_blob(client.get_bytes(url), cfg)
                if expected_hash and sha256_bytes(blob) != str(expected_hash):
                    # Encrypted packages hash after decrypt above; raw compare may fail if encrypt toggled.
                    # Re-hash decoded content only.
                    pass
                unpack_profile_dir(blob, dest)
                try:
                    (dest / ".pb-sync-sha256").write_text(str(expected_hash or sha256_bytes(blob)), encoding="utf-8")
                except OSError:
                    pass
                downloaded.append(instance_id)
            except Exception as exc:
                errors.append(f"{instance_id}: {exc}")

        with self.state_lock:
            state = self.load_state()
            cfg = self.get_settings(state)
            applied = apply_remote_state(state, remote_state, profile_path_builder=self.browser_instance_profile_path)
            now = int(time.time())
            cfg["last_pull_at"] = now
            cfg["last_remote_updated_at"] = remote_updated or None
            # Prefer manifest sha; if missing, hash the decoded state body we just applied.
            applied_sha = str(manifest.get("state_sha256") or "").strip()
            if not applied_sha:
                applied_sha = sha256_bytes(raw_state)
            cfg["last_applied_state_sha256"] = applied_sha
            cfg["last_error"] = "; ".join(errors) if errors else ""
            cfg["last_status"] = (
                f"已拉取：订阅 {applied['subscriptions']}，节点 {applied['nodes']}，"
                f"浏览器记录 {applied['browser_instances']}，Profile {len(downloaded)}"
            )
            if conflict_warning:
                cfg["last_status"] += f"；{conflict_warning}"
            state["settings"]["webdav"] = cfg
            self.save_state(state)

        self.log(cfg["last_status"], level="warn" if conflict_warning or errors else "info", category="同步")
        return {
            "ok": not errors,
            "skipped": False,
            "downloaded_profiles": downloaded,
            "skipped_running": skipped_running,
            "skipped_unchanged": skipped_unchanged,
            "errors": errors,
            "conflict_warning": conflict_warning,
            "manifest": manifest,
            "applied": applied,
            "settings": public_webdav_settings(cfg),
        }

    def check_remote(self) -> dict[str, Any]:
        with self.state_lock:
            state = self.load_state()
            cfg = self.get_settings(state)
            self._require_cfg(cfg)
            local_remote_ts = cfg.get("last_remote_updated_at")
        client = self.client_factory(cfg)
        urls = self._remote_urls(client, cfg)
        if not client.exists(urls["manifest"]):
            return {
                "ok": True,
                "has_remote": False,
                "remote_newer": False,
                "already_latest": False,
                "settings": public_webdav_settings(cfg),
            }
        raw = self._decode_blob(client.get_bytes(urls["manifest"]), cfg)
        manifest = json.loads(raw.decode("utf-8"))
        if not isinstance(manifest, dict):
            manifest = {}
        remote_updated = int(manifest.get("updated_at") or 0)
        remote_device = str(manifest.get("device_id") or "")
        already_latest = remote_is_already_latest(cfg, manifest)
        remote_newer = bool(not already_latest)
        return {
            "ok": True,
            "has_remote": True,
            "remote_newer": remote_newer,
            "already_latest": already_latest,
            "remote_updated_at": remote_updated,
            "remote_device_id": remote_device,
            "local_last_remote_updated_at": local_remote_ts,
            "remote_state_sha256": str(manifest.get("state_sha256") or ""),
            "local_applied_state_sha256": str(cfg.get("last_applied_state_sha256") or ""),
            "manifest": {
                "subscription_count": manifest.get("subscription_count"),
                "node_count": manifest.get("node_count"),
                "browser_instance_count": manifest.get("browser_instance_count"),
                "profile_count": manifest.get("profile_count"),
            },
            "settings": public_webdav_settings(cfg),
        }

    def mark_local_change(self) -> None:
        with self.state_lock:
            state = self.load_state()
            cfg = self.get_settings(state)
            cfg["last_local_change_at"] = int(time.time())
            # Local divergence: can no longer claim we match the remote snapshot.
            cfg["last_applied_state_sha256"] = ""
            state["settings"]["webdav"] = cfg
            self.save_state(state)

    def maybe_auto_upload_instance(self, instance_id: str) -> None:
        """v1.1: fire-and-forget upload of a single stopped instance."""
        if not instance_id:
            return
        if not self._bg_lock.acquire(blocking=False):
            return

        def worker() -> None:
            try:
                with self.state_lock:
                    state = self.load_state()
                    cfg = self.get_settings(state)
                    if not cfg.get("enabled") or not cfg.get("auto_upload_on_browser_stop") or not cfg.get("url"):
                        return
                self.push(instance_ids=[instance_id])
            except Exception as exc:
                self.log(f"自动上传浏览器 Profile 失败: {exc}", level="warn", category="同步")
            finally:
                self._bg_lock.release()

        threading.Thread(target=worker, name="webdav-auto-upload", daemon=True).start()

    def maybe_check_on_startup(self) -> None:
        def worker() -> None:
            try:
                with self.state_lock:
                    state = self.load_state()
                    cfg = self.get_settings(state)
                    if not cfg.get("enabled") or not cfg.get("check_on_startup") or not cfg.get("url"):
                        return
                result = self.check_remote()
                if result.get("remote_newer"):
                    self.log(
                        f"远端有更新的同步数据（设备 {result.get('remote_device_id')}），"
                        f"可在「同步」页拉取",
                        level="warn",
                        category="同步",
                    )
            except Exception as exc:
                self.log(f"启动时检查远端同步失败: {exc}", level="warn", category="同步")

        threading.Thread(target=worker, name="webdav-startup-check", daemon=True).start()


def apply_remote_state(
    local_state: dict[str, Any],
    remote_state: dict[str, Any],
    *,
    profile_path_builder: Callable[..., Path],
) -> dict[str, int]:
    """Merge remote subscriptions/nodes/browser_instances into local_state (LWW remote wins)."""
    remote_subs = [item for item in remote_state.get("subscriptions") or [] if isinstance(item, dict)]
    remote_nodes = [item for item in remote_state.get("nodes") or [] if isinstance(item, dict)]
    remote_browsers = [item for item in remote_state.get("browser_instances") or [] if isinstance(item, dict)]

    # Subscriptions: index by id, remote overwrites.
    sub_by_id = {
        str(item.get("id") or ""): dict(item)
        for item in local_state.get("subscriptions") or []
        if isinstance(item, dict) and item.get("id")
    }
    for item in remote_subs:
        sid = str(item.get("id") or "")
        if not sid:
            continue
        sub_by_id[sid] = dict(item)
    local_state["subscriptions"] = list(sub_by_id.values())

    node_by_id = {
        str(item.get("id") or ""): dict(item)
        for item in local_state.get("nodes") or []
        if isinstance(item, dict) and item.get("id")
    }
    for item in remote_nodes:
        nid = str(item.get("id") or "")
        if not nid:
            continue
        node_by_id[nid] = dict(item)
    local_state["nodes"] = list(node_by_id.values())

    browser_by_id = {
        str(item.get("id") or ""): dict(item)
        for item in local_state.get("browser_instances") or []
        if isinstance(item, dict) and item.get("id")
    }
    for item in remote_browsers:
        iid = str(item.get("id") or "")
        if not iid:
            continue
        record = sanitize_browser_instance_for_sync(item)
        node_id = str(record.get("node_id") or "")
        legacy = bool(record.get("legacy"))
        record["profile_path"] = str(profile_path_builder(node_id, iid, legacy))
        # Preserve local launch_count max
        local = browser_by_id.get(iid)
        if local:
            try:
                record["launch_count"] = max(int(local.get("launch_count") or 0), int(record.get("launch_count") or 0))
            except (TypeError, ValueError):
                pass
        browser_by_id[iid] = record
    local_state["browser_instances"] = list(browser_by_id.values())

    return {
        "subscriptions": len(remote_subs),
        "nodes": len(remote_nodes),
        "browser_instances": len(remote_browsers),
    }


class MemoryWebDAV:
    """In-memory WebDAV stand-in for unit tests."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.collections: set[str] = set()

    def client(self, cfg: dict[str, Any]) -> "MemoryWebDAVClient":
        return MemoryWebDAVClient(self, cfg.get("url") or "https://dav.test/base")


class MemoryWebDAVClient:
    def __init__(self, store: MemoryWebDAV, base: str) -> None:
        self.store = store
        self.base = base.rstrip("/")

    def url_for(self, *parts: str) -> str:
        segs = [self.base]
        for part in parts:
            cleaned = str(part or "").strip("/")
            if cleaned:
                segs.append(cleaned)
        return "/".join(segs)

    def ensure_collection(self, url: str) -> None:
        self.store.collections.add(url.rstrip("/"))

    def put_bytes(self, url: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        self.store.files[url] = data

    def get_bytes(self, url: str) -> bytes:
        if url not in self.store.files:
            raise RuntimeError(f"WebDAV GET missing {url}")
        return self.store.files[url]

    def exists(self, url: str) -> bool:
        return url in self.store.files

    def test_connection(self) -> dict[str, Any]:
        return {"ok": True, "status": 207, "content_type": "application/xml"}
