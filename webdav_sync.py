"""WebDAV sync for ProxyBridge subscriptions, nodes, and browser profiles.

v1: manual push/pull of state + stopped browser profiles
v1.1: hash-skip incremental profiles, auto-upload on browser stop, startup check
v2: LWW conflict metadata (device_id + updated_at), optional OpenSSL AES encryption
"""

from __future__ import annotations

import base64
import functools
import gzip
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import shutil
import ssl
import subprocess
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any, Callable


SYNC_SCHEMA_VERSION = 1
MANIFEST_NAME = "manifest.json"
STATE_SYNC_NAME = "state.sync.json"
PROFILES_DIR_NAME = "profiles"
DEFAULT_REMOTE_ROOT = "ProxyBridge"
PROFILE_SYNC_MARKER = ".pb-sync-sha256"
SAFE_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
SAFE_FILENAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,255}\Z")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
MAX_PROFILE_ARCHIVE_BYTES = 1024 * 1024 * 1024
MAX_PROFILE_EXPANDED_BYTES = 2 * 1024 * 1024 * 1024
MAX_PROFILE_MEMBER_COUNT = 200_000
MAX_STATE_BYTES = 64 * 1024 * 1024
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_SYNC_ITEMS = 100_000
MAX_HTTP_ERROR_BYTES = 64 * 1024
PBENC1_MAGIC = b"PBENC1"
PBENC2_MAGIC = b"PBENC2"
PBENC2_SALT_BYTES = 16
PBENC2_TAG_BYTES = 32
PBENC2_KDF_ITERATIONS = 200_000

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


class _LimitedBytesIO(io.BytesIO):
    def __init__(self, limit: int) -> None:
        super().__init__()
        self.limit = limit

    def write(self, data: bytes) -> int:
        if self.tell() + len(data) > self.limit:
            raise RuntimeError("Profile 归档超过允许大小")
        return super().write(data)


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
        # Default on: closing a browser pushes profile when sync is enabled.
        "auto_upload_on_browser_stop": True,
        "check_on_startup": False,
        # When True, HTTPS WebDAV skips certificate verification (expired/self-signed).
        "insecure_ssl": False,
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
    try:
        base["remote_root"] = _normalize_remote_root(base.get("remote_root"))
    except ValueError:
        base["remote_root"] = DEFAULT_REMOTE_ROOT
    base["enabled"] = bool(base.get("enabled"))
    base["auto_upload_on_browser_stop"] = bool(base.get("auto_upload_on_browser_stop"))
    base["check_on_startup"] = bool(base.get("check_on_startup"))
    base["insecure_ssl"] = bool(base.get("insecure_ssl"))
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
        "insecure_ssl": cfg["insecure_ssl"],
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


def _normalize_remote_root(value: Any) -> str:
    raw = str(value or DEFAULT_REMOTE_ROOT).strip().strip("/")
    if not raw:
        return DEFAULT_REMOTE_ROOT
    parts = raw.replace("\\", "/").split("/")
    cleaned: list[str] = []
    for raw_part in parts:
        part = raw_part.strip()
        if not part:
            continue
        if part in {".", ".."} or any(ord(char) < 32 for char in part):
            raise ValueError("远端目录不能包含路径穿越或控制字符")
        cleaned.append(part)
    if not cleaned:
        return DEFAULT_REMOTE_ROOT
    return "/".join(cleaned)


def _validate_sync_id(value: Any, field: str = "ID") -> str:
    text = str(value or "")
    if not SAFE_ID_PATTERN.fullmatch(text):
        raise RuntimeError(f"远端 {field} 无效")
    return text


def _validate_sha256(value: Any, field: str = "SHA-256") -> str:
    text = str(value or "").strip().lower()
    if not SHA256_PATTERN.fullmatch(text):
        raise RuntimeError(f"远端 {field} 无效")
    return text


def _validate_remote_filename(value: Any, field: str, suffix: str) -> str:
    text = str(value or "")
    if not SAFE_FILENAME_PATTERN.fullmatch(text) or not text.endswith(suffix):
        raise RuntimeError(f"远端 {field} 文件名无效")
    return text


def _canonicalize_tarinfo(info: tarfile.TarInfo) -> tarfile.TarInfo:
    """Remove host-specific metadata so unchanged profiles hash identically."""
    info.mtime = 0
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.pax_headers = {}
    return info


def _should_exclude_profile_member(name: str, is_dir: bool) -> bool:
    base = Path(name).name
    if base == PROFILE_SYNC_MARKER or base.startswith(f"{PROFILE_SYNC_MARKER}."):
        return True
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
    buffer = _LimitedBytesIO(MAX_PROFILE_ARCHIVE_BYTES)
    with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w") as archive:
            for root, dirs, files in os.walk(profile_path):
                rel_root = Path(root).relative_to(profile_path)
                # Prune excluded directories in-place and make traversal stable.
                dirs[:] = sorted(
                    d
                    for d in dirs
                    if not _should_exclude_profile_member(
                        str(rel_root / d) if str(rel_root) != "." else d, True
                    )
                )
                for filename in sorted(files):
                    rel = rel_root / filename if str(rel_root) != "." else Path(filename)
                    rel_name = rel.as_posix()
                    if _should_exclude_profile_member(rel_name, False):
                        continue
                    full = Path(root) / filename
                    if full.is_symlink():
                        continue
                    try:
                        archive.add(
                            full,
                            arcname=rel_name,
                            recursive=False,
                            filter=_canonicalize_tarinfo,
                        )
                    except OSError as exc:
                        raise RuntimeError(f"读取 Profile 文件失败: {full}: {exc}") from exc
    payload = buffer.getvalue()
    if len(payload) > MAX_PROFILE_ARCHIVE_BYTES:
        raise RuntimeError("Profile 归档超过允许大小")
    return payload


def unpack_profile_dir(archive_bytes: bytes, dest: Path) -> None:
    if not isinstance(archive_bytes, (bytes, bytearray)):
        raise TypeError("Profile 归档必须是 bytes")
    if len(archive_bytes) > MAX_PROFILE_ARCHIVE_BYTES:
        raise RuntimeError("Profile 归档超过允许大小")
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_symlink():
        raise RuntimeError(f"Profile 目标不能是符号链接: {dest}")

    temporary: Path | None = Path(
        tempfile.mkdtemp(prefix=f".{dest.name}.", dir=str(dest.parent))
    )
    backup: Path | None = None
    try:
        with tarfile.open(fileobj=io.BytesIO(bytes(archive_bytes)), mode="r:gz") as archive:
            members = archive.getmembers()
            if len(members) > MAX_PROFILE_MEMBER_COUNT:
                raise RuntimeError("Profile 归档文件数量过多")
            seen: set[str] = set()
            regular_files: set[str] = set()
            expanded_size = 0
            for member in members:
                name = str(member.name or "").replace("\\", "/")
                path = PurePosixPath(name)
                if (
                    not name
                    or "\x00" in name
                    or path.is_absolute()
                    or ":" in path.parts[0]
                    or ".." in path.parts
                ):
                    raise RuntimeError(f"不安全的 Profile 归档路径: {name}")
                normalized = "/".join(path.parts)
                if normalized in seen:
                    raise RuntimeError(f"Profile 归档包含重复路径: {name}")
                seen.add(normalized)
                if member.issym() or member.islnk() or member.isdev():
                    raise RuntimeError(f"Profile 归档包含不允许的特殊文件: {name}")
                if member.isdir():
                    continue
                if not member.isfile() or member.size < 0:
                    raise RuntimeError(f"Profile 归档包含不支持的成员: {name}")
                regular_files.add(normalized)
                expanded_size += int(member.size)
                if expanded_size > MAX_PROFILE_EXPANDED_BYTES:
                    raise RuntimeError("Profile 解压后超过允许大小")
            for name in regular_files:
                for parent in PurePosixPath(name).parents:
                    if str(parent) in regular_files:
                        raise RuntimeError(f"Profile 归档路径冲突: {name}")

            for member in members:
                path = PurePosixPath(str(member.name or "").replace("\\", "/"))
                target = temporary.joinpath(*path.parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    raise RuntimeError(f"无法读取 Profile 归档成员: {member.name}")
                with source, target.open("xb") as output:
                    remaining = int(member.size)
                    while remaining:
                        chunk = source.read(min(1024 * 1024, remaining))
                        if not chunk:
                            raise RuntimeError(f"Profile 归档成员长度不足: {member.name}")
                        output.write(chunk)
                        remaining -= len(chunk)
                try:
                    os.chmod(target, member.mode & 0o0777)
                except OSError:
                    pass

        if dest.exists():
            backup = dest.with_name(f".{dest.name}.old-{secrets.token_hex(6)}")
            os.replace(dest, backup)
        os.replace(temporary, dest)
        temporary = None
        if backup is not None:
            shutil.rmtree(backup, ignore_errors=True)
            backup = None
    except Exception:
        if backup is not None and not dest.exists():
            os.replace(backup, dest)
        raise
    finally:
        if temporary is not None and temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)
        if backup is not None and backup.exists() and dest.exists():
            shutil.rmtree(backup, ignore_errors=True)


def openssl_available() -> bool:
    return shutil.which("openssl") is not None


def encrypt_payload(data: bytes, password: str) -> bytes:
    if not password:
        raise ValueError("启用同步加密时必须设置加密密码")
    if not openssl_available():
        raise RuntimeError("启用了同步加密，但系统未找到 openssl")
    salt = secrets.token_bytes(PBENC2_SALT_BYTES)
    env = os.environ.copy()
    env["PB_SYNC_PASS"] = password
    proc = subprocess.run(
        [
            "openssl",
            "enc",
            "-aes-256-cbc",
            "-pbkdf2",
            "-iter",
            str(PBENC2_KDF_ITERATIONS),
            "-md",
            "sha256",
            "-S",
            salt.hex(),
            "-pass",
            "env:PB_SYNC_PASS",
        ],
        input=data,
        capture_output=True,
        env=env,
        check=False,
    )
    if proc.returncode != 0:
        err = (proc.stderr or b"").decode("utf-8", "ignore")
        raise RuntimeError(f"加密失败: {err or proc.returncode}")
    mac_key = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        b"ProxyBridge PBENC2 MAC\0" + salt,
        PBENC2_KDF_ITERATIONS,
        dklen=32,
    )
    authenticated = PBENC2_MAGIC + salt + proc.stdout
    tag = hmac.new(mac_key, authenticated, hashlib.sha256).digest()
    return authenticated + tag


def decrypt_payload(data: bytes, password: str) -> bytes:
    if data.startswith(PBENC2_MAGIC):
        if not password:
            raise RuntimeError("远端数据已加密，请填写同步加密密码")
        if not openssl_available():
            raise RuntimeError("远端数据已加密，但系统未找到 openssl")
        minimum = len(PBENC2_MAGIC) + PBENC2_SALT_BYTES + PBENC2_TAG_BYTES + 16
        if len(data) < minimum:
            raise RuntimeError("PBENC2 数据不完整")
        salt_start = len(PBENC2_MAGIC)
        salt = data[salt_start : salt_start + PBENC2_SALT_BYTES]
        cipher_end = len(data) - PBENC2_TAG_BYTES
        authenticated = data[:cipher_end]
        supplied_tag = data[cipher_end:]
        mac_key = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            b"ProxyBridge PBENC2 MAC\0" + salt,
            PBENC2_KDF_ITERATIONS,
            dklen=32,
        )
        expected_tag = hmac.new(mac_key, authenticated, hashlib.sha256).digest()
        if not hmac.compare_digest(expected_tag, supplied_tag):
            raise RuntimeError("解密失败（密码不正确或远端数据已被篡改）")
        magicless = data[len(PBENC2_MAGIC) + PBENC2_SALT_BYTES : cipher_end]
        env = os.environ.copy()
        env["PB_SYNC_PASS"] = password
        proc = subprocess.run(
            [
                "openssl",
                "enc",
                "-d",
                "-aes-256-cbc",
                "-pbkdf2",
                "-iter",
                str(PBENC2_KDF_ITERATIONS),
                "-md",
                "sha256",
                "-S",
                salt.hex(),
                "-pass",
                "env:PB_SYNC_PASS",
            ],
            input=magicless,
            capture_output=True,
            env=env,
            check=False,
        )
        if proc.returncode != 0:
            err = (proc.stderr or b"").decode("utf-8", "ignore")
            raise RuntimeError(f"解密失败: {err or proc.returncode}")
        return proc.stdout
    if not data.startswith(PBENC1_MAGIC):
        return data
    if not password:
        raise RuntimeError("远端数据已加密，请填写同步加密密码")
    if not openssl_available():
        raise RuntimeError("远端数据已加密，但系统未找到 openssl")
    cipher = data[len(PBENC1_MAGIC) :]
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
    # Keep free-text label/tag for cross-machine notes (e.g. 访问 xx 的浏览器).
    label = str(record.get("label") or record.get("tag") or "").strip()
    if len(label) > 120:
        label = label[:120].rstrip()
    record["label"] = label
    record.pop("tag", None)
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


def _validated_profile_hashes(value: Any, field: str = "profile_hashes") -> dict[str, str]:
    if not isinstance(value, dict):
        raise RuntimeError(f"远端 {field} 必须是对象")
    if len(value) > MAX_SYNC_ITEMS:
        raise RuntimeError(f"远端 {field} 数量过多")
    result: dict[str, str] = {}
    for raw_id, raw_hash in value.items():
        instance_id = _validate_sync_id(raw_id, f"{field} 实例 ID")
        if instance_id in result:
            raise RuntimeError(f"远端 {field} 包含重复实例 ID")
        result[instance_id] = _validate_sha256(raw_hash, f"{field}.{instance_id}")
    return result


def _validated_entity_ids(value: Any, field: str) -> tuple[list[dict[str, Any]], set[str]]:
    if not isinstance(value, list):
        raise RuntimeError(f"远端 {field} 必须是数组")
    if len(value) > MAX_SYNC_ITEMS:
        raise RuntimeError(f"远端 {field} 数量过多")
    ids: set[str] = set()
    records: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise RuntimeError(f"远端 {field}[{index}] 必须是对象")
        item_id = _validate_sync_id(item.get("id"), f"{field}[{index}].id")
        if item_id in ids:
            raise RuntimeError(f"远端 {field} 包含重复 ID: {item_id}")
        ids.add(item_id)
        records.append(item)
    return records, ids


def validate_sync_state(remote_state: Any) -> dict[str, Any]:
    if not isinstance(remote_state, dict):
        raise RuntimeError("远端 state.sync.json 格式无效")
    if remote_state.get("schema_version") != SYNC_SCHEMA_VERSION:
        raise RuntimeError("远端 state.sync.json schema_version 不受支持")
    _validate_sync_id(remote_state.get("device_id"), "state.device_id")
    updated_at = remote_state.get("updated_at")
    if isinstance(updated_at, bool) or not isinstance(updated_at, int) or updated_at <= 0:
        raise RuntimeError("远端 state.updated_at 无效")

    subscriptions, subscription_ids = _validated_entity_ids(
        remote_state.get("subscriptions"), "subscriptions"
    )
    nodes, node_ids = _validated_entity_ids(remote_state.get("nodes"), "nodes")
    browsers, browser_ids = _validated_entity_ids(
        remote_state.get("browser_instances"), "browser_instances"
    )
    profile_hashes = _validated_profile_hashes(remote_state.get("profile_hashes"))
    unknown_profiles = set(profile_hashes) - browser_ids
    if unknown_profiles:
        raise RuntimeError(f"远端 Profile 引用了未知浏览器实例: {sorted(unknown_profiles)[0]}")

    for index, browser in enumerate(browsers):
        node_id = _validate_sync_id(browser.get("node_id"), f"browser_instances[{index}].node_id")
        if node_id not in node_ids:
            raise RuntimeError(f"远端浏览器实例引用了未知节点: {node_id}")
    for index, subscription in enumerate(subscriptions):
        referenced = subscription.get("node_ids")
        if referenced is None:
            continue
        if not isinstance(referenced, list):
            raise RuntimeError(f"远端 subscriptions[{index}].node_ids 必须是数组")
        for raw_node_id in referenced:
            node_id = _validate_sync_id(raw_node_id, f"subscriptions[{index}].node_ids")
            if node_id not in node_ids:
                raise RuntimeError(f"远端订阅引用了未知节点: {node_id}")

    return {
        "subscriptions": subscriptions,
        "subscription_ids": subscription_ids,
        "nodes": nodes,
        "node_ids": node_ids,
        "browsers": browsers,
        "browser_ids": browser_ids,
        "profile_hashes": profile_hashes,
    }


def validate_manifest(manifest: Any) -> dict[str, Any]:
    if not isinstance(manifest, dict):
        raise RuntimeError("远端 manifest.json 格式无效")
    if manifest.get("schema_version") != SYNC_SCHEMA_VERSION:
        raise RuntimeError("远端 manifest.json schema_version 不受支持")
    _validate_sync_id(manifest.get("device_id"), "manifest.device_id")
    updated_at = manifest.get("updated_at")
    if isinstance(updated_at, bool) or not isinstance(updated_at, int) or updated_at <= 0:
        raise RuntimeError("远端 manifest.updated_at 无效")
    state_sha256 = _validate_sha256(manifest.get("state_sha256"), "manifest.state_sha256")
    profile_hashes = _validated_profile_hashes(manifest.get("profile_hashes"))
    for field in (
        "subscription_count",
        "node_count",
        "browser_instance_count",
        "profile_count",
    ):
        value = manifest.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > MAX_SYNC_ITEMS:
            raise RuntimeError(f"远端 manifest.{field} 无效")
    if manifest.get("profile_count") != len(profile_hashes):
        raise RuntimeError("远端 manifest.profile_count 与 profile_hashes 不一致")
    state_file = _validate_remote_filename(
        manifest.get("state_file") or STATE_SYNC_NAME,
        "manifest.state_file",
        ".json",
    )
    raw_profile_files = manifest.get("profile_files")
    if raw_profile_files is None:
        profile_files = {
            instance_id: f"{instance_id}.tar.gz" for instance_id in profile_hashes
        }
    else:
        if not isinstance(raw_profile_files, dict):
            raise RuntimeError("远端 manifest.profile_files 必须是对象")
        if set(raw_profile_files) != set(profile_hashes):
            raise RuntimeError("远端 manifest.profile_files 与 profile_hashes 不一致")
        profile_files = {
            _validate_sync_id(instance_id, "manifest.profile_files 实例 ID"): _validate_remote_filename(
                filename,
                f"manifest.profile_files.{instance_id}",
                ".tar.gz",
            )
            for instance_id, filename in raw_profile_files.items()
        }
    return {
        "state_sha256": state_sha256,
        "state_file": state_file,
        "profile_hashes": profile_hashes,
        "profile_files": profile_files,
    }


def validate_remote_snapshot(
    manifest: dict[str, Any], remote_state: dict[str, Any], raw_state: bytes
) -> dict[str, Any]:
    manifest_meta = validate_manifest(manifest)
    state_meta = validate_sync_state(remote_state)
    actual_state_hash = sha256_bytes(raw_state)
    expected_state_hash = _validate_sha256(manifest.get("state_sha256"), "manifest.state_sha256")
    if actual_state_hash != expected_state_hash:
        raise RuntimeError("远端 state.sync.json 哈希与 manifest 不一致")
    if str(manifest.get("device_id")) != str(remote_state.get("device_id")):
        raise RuntimeError("远端 state 与 manifest 的 device_id 不一致")
    if int(manifest.get("updated_at")) != int(remote_state.get("updated_at")):
        raise RuntimeError("远端 state 与 manifest 的 updated_at 不一致")
    if manifest_meta["profile_hashes"] != state_meta["profile_hashes"]:
        raise RuntimeError("远端 state 与 manifest 的 profile_hashes 不一致")
    expected_counts = {
        "subscription_count": len(state_meta["subscriptions"]),
        "node_count": len(state_meta["nodes"]),
        "browser_instance_count": len(state_meta["browsers"]),
        "profile_count": len(state_meta["profile_hashes"]),
    }
    for field, expected in expected_counts.items():
        if manifest.get(field) != expected:
            raise RuntimeError(f"远端 manifest.{field} 与 state 不一致")
    return manifest_meta


def _serialized_sync_operation(method: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(method)
    def wrapped(self: "SyncEngine", *args: Any, **kwargs: Any) -> Any:
        with self._operation_lock:
            return method(self, *args, **kwargs)

    return wrapped


class WebDAVClient:
    """Minimal WebDAV client (Basic auth) using urllib."""

    def __init__(
        self,
        base_url: str,
        username: str = "",
        password: str = "",
        timeout: float = 60.0,
        *,
        insecure_ssl: bool = False,
    ) -> None:
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
        self.insecure_ssl = bool(insecure_ssl)
        if self.insecure_ssl:
            # Allow expired / self-signed certificates (user opt-in).
            ctx = ssl._create_unverified_context()
            self._opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))
        else:
            self._opener = urllib.request.build_opener(urllib.request.HTTPSHandler())

    def _auth_header(self) -> str | None:
        if not self.username and not self.password:
            return None
        token = base64.b64encode(f"{self.username}:{self.password}".encode("utf-8")).decode("ascii")
        return f"Basic {token}"

    def url_for(self, *parts: str) -> str:
        segments = [self.base]
        for part in parts:
            for raw_segment in str(part or "").replace("\\", "/").split("/"):
                segment = raw_segment.strip()
                if not segment:
                    continue
                if segment in {".", ".."} or any(ord(char) < 32 for char in segment):
                    raise ValueError("WebDAV 路径包含不安全的片段")
                segments.append(urllib.parse.quote(segment, safe="-._~"))
        return "/".join(segments)

    @staticmethod
    def _read_limited(response: Any, max_body_bytes: int | None) -> bytes:
        if max_body_bytes is None:
            return response.read()
        content_length = response.headers.get("Content-Length") if response.headers else None
        if content_length:
            try:
                if int(content_length) > max_body_bytes:
                    raise RuntimeError("WebDAV 响应超过允许大小")
            except ValueError:
                pass
        body = response.read(max_body_bytes + 1)
        if len(body) > max_body_bytes:
            raise RuntimeError("WebDAV 响应超过允许大小")
        return body

    def request(
        self,
        method: str,
        url: str,
        *,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
        allow_statuses: set[int] | None = None,
        max_body_bytes: int | None = None,
        read_body: bool = True,
    ) -> tuple[int, dict[str, str], bytes]:
        req_headers = dict(headers or {})
        auth = self._auth_header()
        if auth:
            req_headers["Authorization"] = auth
        request = urllib.request.Request(url, data=data, method=method, headers=req_headers)
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                status = getattr(response, "status", None) or response.getcode()
                resp_headers = {k.lower(): v for k, v in response.headers.items()}
                body = self._read_limited(response, max_body_bytes) if read_body else b""
                if allow_statuses is not None and int(status) not in allow_statuses:
                    message = body.decode("utf-8", "ignore")[:300] if body else "unexpected status"
                    raise RuntimeError(f"WebDAV {method} {url} 失败 ({status}): {message}")
                return int(status), resp_headers, body
        except urllib.error.HTTPError as exc:
            body = (
                self._read_limited(exc, min(max_body_bytes or MAX_HTTP_ERROR_BYTES, MAX_HTTP_ERROR_BYTES))
                if read_body and hasattr(exc, "read")
                else b""
            )
            status = int(exc.code)
            if allow_statuses and status in allow_statuses:
                resp_headers = {k.lower(): v for k, v in (exc.headers or {}).items()}
                return status, resp_headers, body
            message = body.decode("utf-8", "ignore")[:300] if body else str(exc)
            raise RuntimeError(f"WebDAV {method} {url} 失败 ({status}): {message}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"WebDAV 连接失败: {exc}") from exc

    def ensure_collection(self, url: str) -> None:
        collection_url = url if url.endswith("/") else url + "/"
        status, _, _ = self.request(
            "MKCOL", collection_url, allow_statuses={200, 201, 204, 405}, read_body=False
        )
        if status != 405:
            return
        propfind = (
            b'<?xml version="1.0" encoding="utf-8"?>'
            b'<d:propfind xmlns:d="DAV:"><d:prop><d:resourcetype/></d:prop></d:propfind>'
        )
        check_status, _, _ = self.request(
            "PROPFIND",
            collection_url,
            data=propfind,
            headers={"Depth": "0", "Content-Type": "application/xml; charset=utf-8"},
            allow_statuses={200, 207, 404, 405},
            max_body_bytes=MAX_MANIFEST_BYTES,
        )
        if check_status not in {200, 207}:
            raise RuntimeError(f"远端目录不可用: {check_status}")

    def put_bytes(self, url: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        self.request(
            "PUT",
            url,
            data=data,
            headers={"Content-Type": content_type, "Content-Length": str(len(data))},
            allow_statuses={200, 201, 204},
        )

    def move(self, source_url: str, destination_url: str, *, overwrite: bool = True) -> None:
        self.request(
            "MOVE",
            source_url,
            headers={
                "Destination": destination_url,
                "Overwrite": "T" if overwrite else "F",
            },
            allow_statuses={201, 204},
            read_body=False,
        )

    def copy(self, source_url: str, destination_url: str, *, overwrite: bool = True) -> None:
        self.request(
            "COPY",
            source_url,
            headers={
                "Destination": destination_url,
                "Overwrite": "T" if overwrite else "F",
            },
            allow_statuses={201, 204},
            read_body=False,
        )

    def delete(self, url: str) -> bool:
        """Delete a remote resource. Returns True if deleted or already absent."""
        status, _, _ = self.request(
            "DELETE",
            url,
            allow_statuses={200, 204, 404},
        )
        return status in {200, 204, 404}

    def get_bytes(self, url: str, *, max_bytes: int | None = None) -> bytes:
        _status, _headers, body = self.request(
            "GET", url, allow_statuses={200}, max_body_bytes=max_bytes
        )
        return body

    def exists(self, url: str) -> bool:
        """Return whether a remote resource exists.

        Many WebDAV providers (坚果云/部分 Nextcloud 配置) 对 HEAD 实现不完整：
        文件存在时仍可能返回 404/405/501。因此 HEAD 非 200 时必须再试 GET，
        避免误报「远端还没有同步数据」。
        """
        status, _, _ = self.request(
            "HEAD",
            url,
            allow_statuses={200, 204, 404, 405, 501},
            read_body=False,
        )
        if status in {200, 204}:
            return True
        status, _, _ = self.request(
            "GET", url, allow_statuses={200, 204, 404}, read_body=False
        )
        return status in {200, 204}

    def test_connection(self) -> dict[str, Any]:
        # PROPFIND depth 0 on base
        propfind = (
            b'<?xml version="1.0" encoding="utf-8"?>'
            b'<d:propfind xmlns:d="DAV:"><d:prop><d:resourcetype/></d:prop></d:propfind>'
        )
        status, headers, _body = self.request(
            "PROPFIND",
            self.base if self.base.endswith("/") else self.base + "/",
            data=propfind,
            headers={"Depth": "0", "Content-Type": "application/xml; charset=utf-8"},
            allow_statuses={200, 207, 401, 403, 404, 405},
            max_body_bytes=MAX_MANIFEST_BYTES,
        )
        if status in {401, 403}:
            raise RuntimeError("WebDAV 认证失败，请检查用户名和密码")
        if status not in {200, 207}:
            raise RuntimeError(f"目标不是可用的 WebDAV collection ({status})")
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
        self._operation_lock = threading.RLock()
        self._bg_lock = threading.Lock()

    def _default_client(self, cfg: dict[str, Any]) -> WebDAVClient:
        return WebDAVClient(
            cfg["url"],
            cfg.get("username") or "",
            cfg.get("password") or "",
            insecure_ssl=bool(cfg.get("insecure_ssl")),
        )

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

    @_serialized_sync_operation
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
                cfg["remote_root"] = _normalize_remote_root(payload.get("remote_root"))
            for key in (
                "enabled",
                "auto_upload_on_browser_stop",
                "check_on_startup",
                "insecure_ssl",
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
            self._validate_encryption_cfg(cfg)
            state.setdefault("settings", {})["webdav"] = cfg
            self.save_state(state)
            return public_webdav_settings(cfg)

    def _require_cfg(self, cfg: dict[str, Any]) -> None:
        if not cfg.get("url"):
            raise ValueError("请先配置 WebDAV URL")
        self._validate_encryption_cfg(cfg)

    def _validate_encryption_cfg(self, cfg: dict[str, Any]) -> None:
        if not cfg.get("encrypt"):
            return
        if not str(cfg.get("encrypt_password") or ""):
            raise ValueError("启用同步加密时必须设置加密密码")
        if not openssl_available():
            raise RuntimeError("启用了同步加密，但系统未找到 openssl")

    def _remote_urls(self, client: WebDAVClient, cfg: dict[str, Any]) -> dict[str, str]:
        root = client.url_for(cfg["remote_root"])
        return {
            "root": root,
            "manifest": client.url_for(cfg["remote_root"], MANIFEST_NAME),
            "state": client.url_for(cfg["remote_root"], STATE_SYNC_NAME),
            "profiles": client.url_for(cfg["remote_root"], PROFILES_DIR_NAME),
        }

    def _ensure_remote_layout(self, client: WebDAVClient, urls: dict[str, str]) -> None:
        base = str(client.base).rstrip("/")
        root = urls["root"].rstrip("/")
        if root.startswith(base + "/"):
            current = base
            for segment in root[len(base) + 1 :].split("/"):
                current = f"{current}/{segment}"
                client.ensure_collection(current)
        else:
            client.ensure_collection(root)
        client.ensure_collection(urls["profiles"])

    def _encode_blob(self, data: bytes, cfg: dict[str, Any]) -> bytes:
        if cfg.get("encrypt"):
            return encrypt_payload(data, str(cfg.get("encrypt_password") or ""))
        return data

    def _decode_blob(self, data: bytes, cfg: dict[str, Any]) -> bytes:
        if data.startswith(PBENC1_MAGIC) or data.startswith(PBENC2_MAGIC):
            return decrypt_payload(data, str(cfg.get("encrypt_password") or ""))
        if cfg.get("encrypt"):
            raise RuntimeError("当前已启用加密，但远端数据是未加密格式")
        return data

    @_serialized_sync_operation
    def test(self) -> dict[str, Any]:
        with self.state_lock:
            state = self.load_state()
            cfg = self.get_settings(state)
        self._require_cfg(cfg)
        client = self.client_factory(cfg)
        result = client.test_connection()
        urls = self._remote_urls(client, cfg)
        self._ensure_remote_layout(client, urls)
        result["layout_ok"] = True
        with self.state_lock:
            state = self.load_state()
            cfg = self.get_settings(state)
            cfg["last_error"] = ""
            cfg["last_status"] = "连接测试成功"
            state["settings"]["webdav"] = cfg
            self.save_state(state)
        self.log("WebDAV 连接测试成功", category="同步")
        return {"ok": True, **result, "settings": public_webdav_settings(cfg)}

    def _safe_profile_path(self, node_id: str, instance_id: str, legacy: bool = False) -> Path:
        _validate_sync_id(instance_id, "浏览器实例 ID")
        candidate = Path(self.browser_instance_profile_path(node_id, instance_id, legacy))
        root = Path(self.browser_profiles_root).resolve(strict=False)
        resolved = candidate.resolve(strict=False)
        if resolved == root or root not in resolved.parents:
            raise RuntimeError(f"Profile 路径越出同步目录: {candidate}")
        return resolved

    def _profile_path_for(self, instance: dict[str, Any]) -> Path:
        node_id = str(instance.get("node_id") or "")
        instance_id = str(instance.get("id") or "")
        legacy = bool(instance.get("legacy"))
        return self._safe_profile_path(node_id, instance_id, legacy)

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

    def _record_sync_failure(self, message: str, status: str) -> dict[str, Any]:
        with self.state_lock:
            state = self.load_state()
            cfg = self.get_settings(state)
            cfg["last_error"] = message
            cfg["last_status"] = status
            state["settings"]["webdav"] = cfg
            self.save_state(state)
            return cfg

    @_serialized_sync_operation
    def push(self, *, instance_ids: list[str] | None = None) -> dict[str, Any]:
        """Upload local subscriptions/nodes/browsers and reconcile remote Profile deletions.

        ``instance_ids`` limits which profiles are re-packed/uploaded (auto single-instance
        upload). State + remote orphan deletion always reflect the full local truth.
        """
        with self.state_lock:
            state = self.load_state()
            cfg = self.get_settings(state)
            self._require_cfg(cfg)
            if not cfg.get("enabled") and not cfg.get("url"):
                raise ValueError("请先启用并配置 WebDAV")

            local_instance_ids = {
                _validate_sync_id(item.get("id"), "本地浏览器实例 ID")
                for item in state.get("browser_instances", [])
                if isinstance(item, dict) and item.get("id")
            }
            instances = self._collect_pushable_profiles(state)
            if instance_ids is not None:
                wanted = {_validate_sync_id(item, "浏览器实例 ID") for item in instance_ids}
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

        snapshot_id = f"{int(time.time())}-{cfg['device_id']}-{secrets.token_hex(4)}"
        remote_hashes: dict[str, str] = {}
        remote_profile_files: dict[str, str] = {}
        if client.exists(urls["manifest"]):
            raw_manifest = self._decode_blob(
                client.get_bytes(urls["manifest"], max_bytes=MAX_MANIFEST_BYTES + 1024), cfg
            )
            if len(raw_manifest) > MAX_MANIFEST_BYTES:
                raise RuntimeError("远端 manifest.json 超过允许大小")
            remote_manifest = json.loads(raw_manifest.decode("utf-8"))
            remote_manifest_meta = validate_manifest(remote_manifest)
            remote_hashes = remote_manifest_meta["profile_hashes"]
            remote_profile_files = remote_manifest_meta["profile_files"]

        # Rebuild from local only so deletions propagate; seed with still-local remote hashes.
        profile_hashes: dict[str, str] = {
            iid: digest for iid, digest in remote_hashes.items() if iid in local_instance_ids
        }
        profile_files: dict[str, str] = {
            iid: remote_profile_files.get(iid, f"{iid}.tar.gz")
            for iid in profile_hashes
        }
        uploaded_profile_files: dict[str, str] = {}
        uploaded: list[str] = []
        skipped_unchanged: list[str] = []
        deleted_remote: list[str] = []
        errors: list[str] = []

        for instance in instances:
            instance_id = _validate_sync_id(instance.get("id"), "浏览器实例 ID")
            try:
                archive = pack_profile_dir(self._profile_path_for(instance))
                digest = sha256_bytes(archive)
                existing_filename = remote_profile_files.get(instance_id, f"{instance_id}.tar.gz")
                existing_url = client.url_for(
                    cfg["remote_root"], PROFILES_DIR_NAME, existing_filename
                )
                if remote_hashes.get(instance_id) == digest and client.exists(existing_url):
                    skipped_unchanged.append(instance_id)
                    profile_hashes[instance_id] = digest
                    profile_files[instance_id] = existing_filename
                    continue
                profile_filename = f"{instance_id}-{digest[:16]}-{snapshot_id}.tar.gz"
                profile_url = client.url_for(
                    cfg["remote_root"], PROFILES_DIR_NAME, profile_filename
                )
                blob = self._encode_blob(archive, cfg)
                client.put_bytes(
                    profile_url,
                    blob,
                    content_type="application/gzip",
                )
                profile_hashes[instance_id] = digest
                profile_files[instance_id] = profile_filename
                uploaded_profile_files[instance_id] = profile_filename
                uploaded.append(instance_id)
            except Exception as exc:
                errors.append(f"{instance_id}: {exc}")

        if errors:
            message = "; ".join(errors)
            cfg = self._record_sync_failure(message, "上传失败，未发布新的远端状态")
            self.log(message, level="warn", category="同步")
            return {
                "ok": False,
                "uploaded_profiles": uploaded,
                "skipped_unchanged": skipped_unchanged,
                "skipped_running": skipped_running,
                "deleted_remote_profiles": [],
                "errors": errors,
                "manifest": None,
                "settings": public_webdav_settings(cfg),
            }

        with self.state_lock:
            state = self.load_state()
            cfg = self.get_settings(state)
            local_ids_now = {
                str(item.get("id") or "")
                for item in state.get("browser_instances", [])
                if isinstance(item, dict) and item.get("id")
            }
            profile_hashes = {k: v for k, v in profile_hashes.items() if k in local_ids_now}
            profile_files = {k: v for k, v in profile_files.items() if k in profile_hashes}
            sync_state = build_sync_state(state, device_id=str(cfg["device_id"]), profile_hashes=profile_hashes)
            validate_sync_state(sync_state)
            sync_bytes = json.dumps(sync_state, ensure_ascii=False, indent=2).encode("utf-8")
            if len(sync_bytes) > MAX_STATE_BYTES:
                raise RuntimeError("同步状态超过允许大小")
            state_hash = sha256_bytes(sync_bytes)
            state_filename = f"state-{state_hash[:16]}-{snapshot_id}.sync.json"
            manifest = {
                "schema_version": SYNC_SCHEMA_VERSION,
                "device_id": cfg["device_id"],
                "updated_at": sync_state["updated_at"],
                "state_sha256": state_hash,
                "state_file": state_filename,
                "profile_hashes": profile_hashes,
                "profile_files": profile_files,
                "subscription_count": len(sync_state.get("subscriptions") or []),
                "node_count": len(sync_state.get("nodes") or []),
                "browser_instance_count": len(sync_state.get("browser_instances") or []),
                "profile_count": len(profile_hashes),
            }
            validate_manifest(manifest)
            manifest_bytes = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
            if len(manifest_bytes) > MAX_MANIFEST_BYTES:
                raise RuntimeError("同步 manifest 超过允许大小")

        encoded_state = self._encode_blob(sync_bytes, cfg)
        client.put_bytes(
            client.url_for(cfg["remote_root"], state_filename),
            encoded_state,
            content_type="application/json",
        )
        encoded_manifest = self._encode_blob(manifest_bytes, cfg)
        temporary_manifest_url = client.url_for(
            cfg["remote_root"], f"{MANIFEST_NAME}.tmp-{snapshot_id}"
        )
        client.put_bytes(
            temporary_manifest_url,
            encoded_manifest,
            content_type="application/json",
        )
        client.move(temporary_manifest_url, urls["manifest"], overwrite=True)

        # Compatibility aliases for older clients. New clients only follow the
        # versioned filenames in manifest, so alias failures do not corrupt the snapshot.
        try:
            client.put_bytes(urls["state"], encoded_state, content_type="application/json")
        except Exception as exc:
            errors.append(f"更新兼容 state.sync.json 失败: {exc}")
        for instance_id, profile_filename in uploaded_profile_files.items():
            try:
                client.copy(
                    client.url_for(
                        cfg["remote_root"], PROFILES_DIR_NAME, profile_filename
                    ),
                    client.url_for(
                        cfg["remote_root"], PROFILES_DIR_NAME, f"{instance_id}.tar.gz"
                    ),
                    overwrite=True,
                )
            except Exception as exc:
                errors.append(f"更新兼容 Profile {instance_id} 失败: {exc}")

        # Garbage collection happens only after the new manifest is published.
        for rid in list(remote_hashes.keys()):
            if rid in local_instance_ids:
                continue
            try:
                filenames = {
                    remote_profile_files.get(rid, f"{rid}.tar.gz"),
                    f"{rid}.tar.gz",
                }
                for filename in filenames:
                    client.delete(client.url_for(cfg["remote_root"], PROFILES_DIR_NAME, filename))
                deleted_remote.append(rid)
            except Exception as exc:
                errors.append(f"删除远端 Profile {rid}: {exc}")

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
                f"删除远端 {len(deleted_remote)}，跳过运行中 {len(skipped_running)}"
            )
            state["settings"]["webdav"] = cfg
            self.save_state(state)

        self.log(cfg["last_status"], category="同步")
        return {
            "ok": not errors,
            "uploaded_profiles": uploaded,
            "skipped_unchanged": skipped_unchanged,
            "skipped_running": skipped_running,
            "deleted_remote_profiles": deleted_remote,
            "errors": errors,
            "manifest": manifest,
            "settings": public_webdav_settings(cfg),
        }

    @_serialized_sync_operation
    def pull(self, *, force: bool = False) -> dict[str, Any]:
        with self.state_lock:
            state = self.load_state()
            cfg = self.get_settings(state)
            self._require_cfg(cfg)
            local_last_change = cfg.get("last_local_change_at")
            local_device = cfg.get("device_id")
            local_state_fingerprint = sha256_bytes(
                json.dumps(
                    {
                        "subscriptions": state.get("subscriptions") or [],
                        "nodes": state.get("nodes") or [],
                        "browser_instances": state.get("browser_instances") or [],
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            )

        client = self.client_factory(cfg)
        urls = self._remote_urls(client, cfg)
        if not client.exists(urls["manifest"]):
            raise RuntimeError(
                "远端还没有同步数据（找不到 manifest.json）。"
                f"请确认：1) 源电脑已成功「上传到远端」；"
                f"2) 两台电脑的 WebDAV URL / 远端目录一致；"
                f"当前检查路径：{urls['manifest']}"
            )

        try:
            raw_manifest = self._decode_blob(
                client.get_bytes(urls["manifest"], max_bytes=MAX_MANIFEST_BYTES + 1024), cfg
            )
        except Exception as exc:
            raise RuntimeError(
                f"读取远端 manifest 失败：{exc}；路径：{urls['manifest']}"
            ) from exc
        if len(raw_manifest) > MAX_MANIFEST_BYTES:
            raise RuntimeError("远端 manifest.json 超过允许大小")
        manifest = json.loads(raw_manifest.decode("utf-8"))
        manifest_meta = validate_manifest(manifest)

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

        raw_state = self._decode_blob(
            client.get_bytes(
                client.url_for(cfg["remote_root"], manifest_meta["state_file"]),
                max_bytes=MAX_STATE_BYTES + 1024 * 1024,
            ),
            cfg,
        )
        if len(raw_state) > MAX_STATE_BYTES:
            raise RuntimeError("远端 state.sync.json 超过允许大小")
        remote_state = json.loads(raw_state.decode("utf-8"))
        snapshot_meta = validate_remote_snapshot(manifest, remote_state, raw_state)
        profile_hashes = snapshot_meta["profile_hashes"]
        profile_files = snapshot_meta["profile_files"]

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

        remote_instances = {
            _validate_sync_id(item.get("id"), "远端浏览器实例 ID"): item
            for item in remote_state.get("browser_instances") or []
            if isinstance(item, dict)
        }
        for instance_id, local in local_instances.items():
            if local.get("status") in {"running", "starting"} or self.browser_runtime_is_active(instance_id):
                skipped_running.append(instance_id)

        for instance_id, expected_hash in profile_hashes.items():
            instance_id = _validate_sync_id(instance_id, "Profile 实例 ID")
            expected_hash = _validate_sha256(expected_hash, f"Profile {instance_id} SHA-256")
            local = local_instances.get(instance_id)
            if local and (
                local.get("status") in {"running", "starting"} or self.browser_runtime_is_active(instance_id)
            ):
                if instance_id not in skipped_running:
                    skipped_running.append(instance_id)
                continue
            try:
                meta = remote_instances[instance_id]
                dest = self._safe_profile_path(
                    str(meta.get("node_id") or ""),
                    instance_id,
                    bool(meta.get("legacy")),
                )
                if dest.exists():
                    try:
                        # Cheap skip: if a marker hash file matches.
                        marker = dest / PROFILE_SYNC_MARKER
                        if (
                            not force
                            and marker.exists()
                            and marker.read_text(encoding="utf-8").strip() == expected_hash
                        ):
                            skipped_unchanged.append(instance_id)
                            continue
                    except OSError:
                        pass
                url = client.url_for(
                    cfg["remote_root"], PROFILES_DIR_NAME, profile_files[instance_id]
                )
                if not client.exists(url):
                    errors.append(f"{instance_id}: 远端缺少 Profile 包")
                    continue
                blob = self._decode_blob(
                    client.get_bytes(url, max_bytes=MAX_PROFILE_ARCHIVE_BYTES + 1024 * 1024), cfg
                )
                if len(blob) > MAX_PROFILE_ARCHIVE_BYTES:
                    raise RuntimeError("远端 Profile 包超过允许大小")
                actual_hash = sha256_bytes(blob)
                if actual_hash != expected_hash:
                    raise RuntimeError(
                        f"Profile 哈希不匹配（期望 {expected_hash[:12]}，实际 {actual_hash[:12]}）"
                    )
                unpack_profile_dir(blob, dest)
                marker = dest / PROFILE_SYNC_MARKER
                marker_tmp = dest / f"{PROFILE_SYNC_MARKER}.tmp"
                marker_tmp.write_text(expected_hash, encoding="utf-8")
                marker_tmp.replace(marker)
                downloaded.append(instance_id)
            except Exception as exc:
                errors.append(f"{instance_id}: {exc}")

        if skipped_running:
            errors.extend(
                f"{instance_id}: 浏览器正在运行，未应用远端快照"
                for instance_id in skipped_running
            )
        if errors:
            message = "; ".join(errors)
            cfg = self._record_sync_failure(message, "拉取未完成，未应用远端状态")
            self.log(message, level="warn", category="同步")
            return {
                "ok": False,
                "skipped": False,
                "downloaded_profiles": downloaded,
                "skipped_running": skipped_running,
                "skipped_unchanged": skipped_unchanged,
                "purged_local_profiles": [],
                "errors": errors,
                "conflict_warning": conflict_warning,
                "manifest": manifest,
                "applied": {"subscriptions": 0, "nodes": 0, "browser_instances": 0},
                "settings": public_webdav_settings(cfg),
            }

        purged_profiles: list[str] = []
        with self.state_lock:
            state = self.load_state()
            cfg = self.get_settings(state)
            current_fingerprint = sha256_bytes(
                json.dumps(
                    {
                        "subscriptions": state.get("subscriptions") or [],
                        "nodes": state.get("nodes") or [],
                        "browser_instances": state.get("browser_instances") or [],
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            if current_fingerprint != local_state_fingerprint:
                raise RuntimeError("拉取期间本地状态发生变化，已取消应用远端快照")
            active_now = [
                str(item.get("id") or "")
                for item in state.get("browser_instances") or []
                if isinstance(item, dict)
                and item.get("id")
                and (
                    item.get("status") in {"running", "starting"}
                    or self.browser_runtime_is_active(str(item.get("id") or ""))
                )
            ]
            if active_now:
                raise RuntimeError(f"浏览器正在运行，已取消应用远端快照: {active_now[0]}")
            applied = apply_remote_state(
                state,
                remote_state,
                profile_path_builder=lambda node_id, instance_id, legacy=False: self._safe_profile_path(
                    node_id, instance_id, legacy
                ),
            )
            # Drop local browser profiles for records deleted on the remote side.
            for record in applied.get("removed_browsers") or []:
                if not isinstance(record, dict):
                    continue
                iid = str(record.get("id") or "")
                if iid and (
                    record.get("status") in {"running", "starting"} or self.browser_runtime_is_active(iid)
                ):
                    skipped_running.append(iid)
                    continue
                try:
                    path = self._safe_profile_path(
                        str(record.get("node_id") or ""),
                        iid,
                        bool(record.get("legacy")),
                    )
                    if path.exists():
                        shutil.rmtree(path, ignore_errors=False)
                        purged_profiles.append(iid)
                    # Legacy node-level profile dir when empty of instances.
                    node_root = path.parent if not record.get("legacy") else path
                    if node_root.exists() and node_root.is_dir() and not any(node_root.iterdir()):
                        shutil.rmtree(node_root, ignore_errors=False)
                except Exception as exc:
                    errors.append(f"清理本地 Profile {iid}: {exc}")

            if errors:
                raise RuntimeError("; ".join(errors))

            now = int(time.time())
            cfg["last_pull_at"] = now
            cfg["last_remote_updated_at"] = remote_updated or None
            cfg["last_applied_state_sha256"] = _validate_sha256(
                manifest.get("state_sha256"), "manifest.state_sha256"
            )
            cfg["last_error"] = ""
            removed_subs = len(applied.get("removed_subscription_ids") or [])
            cfg["last_status"] = (
                f"已拉取：订阅 {applied['subscriptions']}，节点 {applied['nodes']}，"
                f"浏览器记录 {applied['browser_instances']}，Profile {len(downloaded)}，"
                f"删除本地 Profile {len(purged_profiles)}"
                + (f"，移除订阅 {removed_subs}" if removed_subs else "")
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
            "purged_local_profiles": purged_profiles,
            "errors": errors,
            "conflict_warning": conflict_warning,
            "manifest": manifest,
            "applied": applied,
            "settings": public_webdav_settings(cfg),
        }

    @_serialized_sync_operation
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
                "manifest_url": urls["manifest"],
                "settings": public_webdav_settings(cfg),
            }
        raw = self._decode_blob(
            client.get_bytes(urls["manifest"], max_bytes=MAX_MANIFEST_BYTES + 1024), cfg
        )
        if len(raw) > MAX_MANIFEST_BYTES:
            raise RuntimeError("远端 manifest.json 超过允许大小")
        manifest = json.loads(raw.decode("utf-8"))
        validate_manifest(manifest)
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
        """Fire-and-forget upload after a browser stops (API stop or process exit).

        Requires WebDAV enabled + URL + auto_upload_on_browser_stop (default True).
        Covers both explicit「停止」and user closing the Chromium window (process exit
        detected on next status poll).
        """
        if not instance_id:
            return
        if not self._bg_lock.acquire(blocking=False):
            return

        def worker() -> None:
            try:
                with self.state_lock:
                    state = self.load_state()
                    cfg = self.get_settings(state)
                    if (
                        not cfg.get("enabled")
                        or not cfg.get("url")
                        or not cfg.get("auto_upload_on_browser_stop")
                    ):
                        return
                self.log(f"浏览器已关闭，自动上传 Profile {instance_id[:12]}…", category="同步")
                self.push(instance_ids=[instance_id])
            except Exception as exc:
                self.log(f"自动上传浏览器 Profile 失败: {exc}", level="warn", category="同步")
            finally:
                self._bg_lock.release()

        threading.Thread(target=worker, name="webdav-auto-upload", daemon=True).start()

    def maybe_auto_push(self, *, reason: str = "") -> None:
        """Fire-and-forget full push when WebDAV is enabled (e.g. after subscription delete)."""
        if not self._bg_lock.acquire(blocking=False):
            # Another sync is running; still mark dirty via last_local_change already done.
            self.log("自动同步跳过：已有同步任务进行中", level="warn", category="同步")
            return

        def worker() -> None:
            try:
                with self.state_lock:
                    state = self.load_state()
                    cfg = self.get_settings(state)
                    if not cfg.get("enabled") or not cfg.get("url"):
                        return
                label = f"（{reason}）" if reason else ""
                self.log(f"自动上传到 WebDAV{label}", category="同步")
                self.push()
            except Exception as exc:
                self.log(f"自动上传失败: {exc}", level="warn", category="同步")
            finally:
                self._bg_lock.release()

        threading.Thread(target=worker, name="webdav-auto-push", daemon=True).start()

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
) -> dict[str, Any]:
    """Apply remote subscriptions/nodes/browser_instances as LWW source of truth.

    Local-only subscriptions/nodes/browser records are **removed** so deletions
    on the source machine propagate after pull.
    """
    remote_subs = [item for item in remote_state.get("subscriptions") or [] if isinstance(item, dict)]
    remote_nodes = [item for item in remote_state.get("nodes") or [] if isinstance(item, dict)]
    remote_browsers = [item for item in remote_state.get("browser_instances") or [] if isinstance(item, dict)]

    old_browser_by_id = {
        str(item.get("id") or ""): dict(item)
        for item in local_state.get("browser_instances") or []
        if isinstance(item, dict) and item.get("id")
    }
    old_browser_ids = set(old_browser_by_id.keys())
    old_node_ids = {
        str(item.get("id") or "")
        for item in local_state.get("nodes") or []
        if isinstance(item, dict) and item.get("id")
    }
    old_sub_ids = {
        str(item.get("id") or "")
        for item in local_state.get("subscriptions") or []
        if isinstance(item, dict) and item.get("id")
    }

    # Full replace from remote (deletions included).
    local_state["subscriptions"] = [dict(item) for item in remote_subs if item.get("id")]
    local_state["nodes"] = [dict(item) for item in remote_nodes if item.get("id")]

    new_browsers: list[dict[str, Any]] = []
    new_browser_ids: set[str] = set()
    for item in remote_browsers:
        iid = _validate_sync_id(item.get("id"), "远端浏览器实例 ID")
        record = sanitize_browser_instance_for_sync(item)
        node_id = str(record.get("node_id") or "")
        _validate_sync_id(node_id, "远端浏览器实例 node_id")
        legacy = bool(record.get("legacy"))
        record["profile_path"] = str(profile_path_builder(node_id, iid, legacy))
        local = old_browser_by_id.get(iid)
        if local:
            try:
                record["launch_count"] = max(int(local.get("launch_count") or 0), int(record.get("launch_count") or 0))
            except (TypeError, ValueError):
                pass
        new_browsers.append(record)
        new_browser_ids.add(iid)
    local_state["browser_instances"] = new_browsers

    new_node_ids = {
        str(item.get("id") or "")
        for item in local_state.get("nodes") or []
        if isinstance(item, dict) and item.get("id")
    }
    new_sub_ids = {
        str(item.get("id") or "")
        for item in local_state.get("subscriptions") or []
        if isinstance(item, dict) and item.get("id")
    }

    return {
        "subscriptions": len(remote_subs),
        "nodes": len(remote_nodes),
        "browser_instances": len(remote_browsers),
        "removed_subscription_ids": sorted(old_sub_ids - new_sub_ids),
        "removed_node_ids": sorted(old_node_ids - new_node_ids),
        "removed_browser_ids": sorted(old_browser_ids - new_browser_ids),
        "removed_browsers": [old_browser_by_id[i] for i in sorted(old_browser_ids - new_browser_ids)],
    }


class MemoryWebDAV:
    """In-memory WebDAV stand-in for unit tests."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.collections: set[str] = set()
        self.lock = threading.RLock()

    def client(self, cfg: dict[str, Any]) -> "MemoryWebDAVClient":
        return MemoryWebDAVClient(self, cfg.get("url") or "https://dav.test/base")


class MemoryWebDAVClient:
    def __init__(self, store: MemoryWebDAV, base: str) -> None:
        self.store = store
        self.base = base.rstrip("/")

    def url_for(self, *parts: str) -> str:
        segs = [self.base]
        for part in parts:
            for raw_segment in str(part or "").replace("\\", "/").split("/"):
                segment = raw_segment.strip()
                if not segment:
                    continue
                if segment in {".", ".."} or any(ord(char) < 32 for char in segment):
                    raise ValueError("WebDAV 路径包含不安全的片段")
                segs.append(urllib.parse.quote(segment, safe="-._~"))
        return "/".join(segs)

    def ensure_collection(self, url: str) -> None:
        with self.store.lock:
            self.store.collections.add(url.rstrip("/"))

    def put_bytes(self, url: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        with self.store.lock:
            self.store.files[url] = data

    def move(self, source_url: str, destination_url: str, *, overwrite: bool = True) -> None:
        with self.store.lock:
            if source_url not in self.store.files:
                raise RuntimeError(f"WebDAV MOVE missing {source_url}")
            if not overwrite and destination_url in self.store.files:
                raise RuntimeError(f"WebDAV MOVE destination exists {destination_url}")
            self.store.files[destination_url] = self.store.files.pop(source_url)

    def copy(self, source_url: str, destination_url: str, *, overwrite: bool = True) -> None:
        with self.store.lock:
            if source_url not in self.store.files:
                raise RuntimeError(f"WebDAV COPY missing {source_url}")
            if not overwrite and destination_url in self.store.files:
                raise RuntimeError(f"WebDAV COPY destination exists {destination_url}")
            self.store.files[destination_url] = self.store.files[source_url]

    def delete(self, url: str) -> bool:
        with self.store.lock:
            self.store.files.pop(url, None)
        return True

    def get_bytes(self, url: str, *, max_bytes: int | None = None) -> bytes:
        with self.store.lock:
            if url not in self.store.files:
                raise RuntimeError(f"WebDAV GET missing {url}")
            data = self.store.files[url]
        if max_bytes is not None and len(data) > max_bytes:
            raise RuntimeError("WebDAV 响应超过允许大小")
        return data

    def exists(self, url: str) -> bool:
        with self.store.lock:
            return url in self.store.files

    def test_connection(self) -> dict[str, Any]:
        return {"ok": True, "status": 207, "content_type": "application/xml"}
