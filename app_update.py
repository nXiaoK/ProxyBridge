"""ProxyBridge 软件更新：检查 GitHub 最新 Release，并在应用内安装新版本。

更新分两种方式，由 Release 中签名的 update-manifest.json 决定：
- 载荷热更新：外壳修订号与清单一致时，只下载 ProxyBridge-payload-<版本>.zip，解压到
  数据目录的 app-updates/payloads/<版本>/，再请求外壳重启后端（见 app_payload.py）；
- 外壳整包替换：外壳本身有变化时，macOS 原子交换 .app，Windows 由新 exe 等待旧进程
  退出后替换自身，然后重新启动。

源码运行、Docker、Linux 以及清单未签名的版本只提示新版本并给出下载地址，不自动安装。
本模块只依赖标准库；网络下载、解压与日志由 app.py 注入，避免循环导入。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import plistlib
import secrets
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping

import app_payload


UPDATE_REPOSITORY = "nXiaoK/ProxyBridge"
LATEST_RELEASE_API = f"https://api.github.com/repos/{UPDATE_REPOSITORY}/releases/latest"
RELEASES_PAGE_URL = f"https://github.com/{UPDATE_REPOSITORY}/releases/latest"
MANIFEST_ASSET_NAME = "update-manifest.json"
SIGNATURE_ASSET_NAME = "update-manifest.json.sig"
MANIFEST_SCHEMA = 1
MANIFEST_SIZE_LIMIT = 256 * 1024
SIGNATURE_SIZE_LIMIT = 1024
# 受信任的更新清单签名公钥（Ed25519，32 字节十六进制）。对应私钥只保存在 GitHub 仓库 Secret
# PROXY_BRIDGE_UPDATE_SIGNING_KEY 中，由发布流水线签名。轮换密钥时先在这里追加新公钥并发布
# 一个版本，再更换流水线私钥，否则已安装的客户端无法验证新清单，只能手动下载升级。
TRUSTED_SIGNING_KEYS = ("8c0e30d729e1f4a27acf453a0290af5c39f4f48278dabb9bf9ef57a5b0897d93",)

SHELL_KIND_MACOS = "macos-app"
SHELL_KIND_WINDOWS = "windows-exe"
SHELL_KINDS = (SHELL_KIND_MACOS, SHELL_KIND_WINDOWS)
# PROXY_BRIDGE_SHELL_KIND：支持应用内更新的外壳写入自身类型（macos-app / windows-exe）；
# 未设置表示源码、Docker 或 Linux 运行，只提示新版本。
ENV_SHELL_KIND = "PROXY_BRIDGE_SHELL_KIND"
# PROXY_BRIDGE_SHELL_PATH：外壳自身位置（macOS 为 .app 目录，Windows 为 ProxyBridge.exe），整包替换时使用。
ENV_SHELL_PATH = "PROXY_BRIDGE_SHELL_PATH"
# PROXY_BRIDGE_PARENT_PID：外壳进程 PID，整包替换时等待它退出后再重新启动新版本。
ENV_PARENT_PID = "PROXY_BRIDGE_PARENT_PID"

# 自动检查：后端启动 90 秒后首次检查，之后每 12 小时一次；只提示，不自动安装。
AUTO_CHECK_INITIAL_DELAY = 90.0
AUTO_CHECK_INTERVAL = 12 * 3600.0
AUTO_CHECK_POLL = 300.0
# 外壳轮询状态的间隔为 4~5 秒；请求在此时间内未被处理就不再展示。
SHELL_REQUEST_TTL = 120.0
DOWNLOADS_DIRNAME = "downloads"
CHECK_CACHE_FILENAME = "check.json"
LAST_RUN_FILENAME = "last-run.json"
SHELL_UPDATE_RESULT_FILENAME = "shell-update.json"
MACOS_STAGING_PREFIX = ".ProxyBridge-update-"
WINDOWS_APPLY_UPDATE_FLAG = "--apply-update"
WINDOWS_WAIT_PID_FLAG = "--wait-pid"


class UpdateBusyError(RuntimeError):
    """已有检查或安装任务在进行。"""


# --- Ed25519（RFC 8032）-------------------------------------------------------
# 标准库没有 Ed25519，而 macOS App 使用的独立 Python 运行时不能携带编译扩展，因此内置纯
# Python 实现。只用于校验公开的发布签名（及流水线签名），不涉及常数时间要求的本机私钥。

_P = 2**255 - 19
_Q = 2**252 + 27742317777372353535851937790883648493


def _inv(value: int) -> int:
    return pow(value, _P - 2, _P)


_D = -121665 * _inv(121666) % _P
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)
_IDENTITY = (0, 1, 1, 0)


def _point_add(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    x1, y1, z1, t1 = a
    x2, y2, z2, t2 = b
    sum_a = (y1 - x1) * (y2 - x2) % _P
    sum_b = (y1 + x1) * (y2 + x2) % _P
    sum_c = 2 * t1 * t2 * _D % _P
    sum_d = 2 * z1 * z2 % _P
    e, f, g, h = sum_b - sum_a, sum_d - sum_c, sum_d + sum_c, sum_b + sum_a
    return (e * f % _P, g * h % _P, f * g % _P, e * h % _P)


def _point_mul(scalar: int, point: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    result = _IDENTITY
    while scalar > 0:
        if scalar & 1:
            result = _point_add(result, point)
        point = _point_add(point, point)
        scalar >>= 1
    return result


def _point_equal(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> bool:
    return (a[0] * b[2] - b[0] * a[2]) % _P == 0 and (a[1] * b[2] - b[1] * a[2]) % _P == 0


def _recover_x(y: int, sign: int) -> int | None:
    if y >= _P:
        return None
    x2 = (y * y - 1) * _inv(_D * y * y + 1) % _P
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P != 0:
        x = x * _SQRT_M1 % _P
    if (x * x - x2) % _P != 0:
        return None
    if (x & 1) != sign:
        x = _P - x
    return x


_BASE_Y = 4 * _inv(5) % _P
_BASE_X = _recover_x(_BASE_Y, 0) or 0
_BASE = (_BASE_X, _BASE_Y, 1, _BASE_X * _BASE_Y % _P)


def _compress(point: tuple[int, int, int, int]) -> bytes:
    z_inv = _inv(point[2])
    x = point[0] * z_inv % _P
    y = point[1] * z_inv % _P
    return int.to_bytes(y | ((x & 1) << 255), 32, "little")


def _decompress(data: bytes) -> tuple[int, int, int, int] | None:
    if len(data) != 32:
        return None
    y = int.from_bytes(data, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _recover_x(y, sign)
    return None if x is None else (x, y, 1, x * y % _P)


def _hash_scalar(data: bytes) -> int:
    return int.from_bytes(hashlib.sha512(data).digest(), "little") % _Q


def _expand_seed(seed: bytes) -> tuple[int, bytes]:
    if len(seed) != 32:
        raise ValueError("Ed25519 私钥必须是 32 字节")
    digest = hashlib.sha512(seed).digest()
    scalar = int.from_bytes(digest[:32], "little")
    scalar &= (1 << 254) - 8
    scalar |= 1 << 254
    return scalar, digest[32:]


def ed25519_public_key(seed: bytes) -> bytes:
    scalar, _prefix = _expand_seed(seed)
    return _compress(_point_mul(scalar, _BASE))


def ed25519_sign(seed: bytes, message: bytes) -> bytes:
    scalar, prefix = _expand_seed(seed)
    public = _compress(_point_mul(scalar, _BASE))
    nonce = _hash_scalar(prefix + message)
    encoded_r = _compress(_point_mul(nonce, _BASE))
    s = (nonce + _hash_scalar(encoded_r + public + message) * scalar) % _Q
    return encoded_r + int.to_bytes(s, 32, "little")


def ed25519_verify(public: bytes, message: bytes, signature: bytes) -> bool:
    if len(public) != 32 or len(signature) != 64:
        return False
    point_a = _decompress(public)
    point_r = _decompress(signature[:32])
    if point_a is None or point_r is None:
        return False
    s = int.from_bytes(signature[32:], "little")
    if s >= _Q:
        return False
    h = _hash_scalar(signature[:32] + public + message)
    return _point_equal(_point_mul(s, _BASE), _point_add(point_r, _point_mul(h, point_a)))


def parse_signature(data: bytes) -> bytes:
    text = data.decode("ascii", errors="replace").strip().lower()
    if len(text) != 128 or any(char not in "0123456789abcdef" for char in text):
        raise ValueError("更新清单签名格式无效")
    return bytes.fromhex(text)


def verify_manifest_signature(
    data: bytes,
    signature: bytes,
    trusted_keys: tuple[str, ...] | None = None,
) -> bool:
    keys = TRUSTED_SIGNING_KEYS if trusted_keys is None else trusted_keys
    return any(ed25519_verify(bytes.fromhex(key), data, signature) for key in keys)


# --- 清单 ---------------------------------------------------------------------


def _hex_digest(value: Any, label: str) -> str:
    text = str(value or "").strip().lower()
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise ValueError(f"{label} 必须是 64 位十六进制 SHA-256")
    return text


def _safe_asset_name(value: Any) -> str:
    name = str(value or "")
    if not name or name in {".", ".."} or "/" in name or "\\" in name or Path(name).name != name:
        raise ValueError("清单中的资源名称不安全")
    return name


def _manifest_asset(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"清单缺少 {label}")
    size = value.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        raise ValueError(f"{label} 大小无效")
    return {
        "name": _safe_asset_name(value.get("name")),
        "size": size,
        "sha256": _hex_digest(value.get("sha256"), f"{label} 摘要"),
    }


def payload_asset_name(version: str) -> str:
    return f"ProxyBridge-payload-{version}.zip"


def parse_manifest(data: bytes) -> dict[str, Any]:
    """校验并规范化 update-manifest.json；调用前必须先验证签名。"""
    try:
        raw = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise ValueError("更新清单不是有效的 JSON") from error
    if not isinstance(raw, dict) or raw.get("schema") != MANIFEST_SCHEMA:
        raise ValueError("更新清单格式版本不受支持")
    version = str(raw.get("version") or "")
    if app_payload.parse_version(version) is None:
        raise ValueError("更新清单版本号无效")
    payload = _manifest_asset(raw.get("payload"), "载荷")
    if payload["name"] != payload_asset_name(version):
        raise ValueError("更新清单中的载荷文件名与版本不一致")
    shells: dict[str, Any] = {}
    raw_shells = raw.get("shells")
    if not isinstance(raw_shells, dict):
        raise ValueError("更新清单缺少外壳信息")
    for kind in SHELL_KINDS:
        entry = raw_shells.get(kind)
        if entry is None:
            continue
        if not isinstance(entry, dict):
            raise ValueError(f"更新清单中的 {kind} 无效")
        shells[kind] = {
            "revision": _hex_digest(entry.get("revision"), f"{kind} 外壳修订号"),
            "asset": _manifest_asset(entry.get("asset"), f"{kind} 安装包"),
        }
    return {
        "schema": MANIFEST_SCHEMA,
        "version": version,
        "tag": str(raw.get("tag") or f"v{version}"),
        "commit": str(raw.get("commit") or "")[:64],
        "payload": payload,
        "shells": shells,
    }


def build_manifest(
    *,
    version: str,
    tag: str,
    commit: str,
    payload: Mapping[str, Any],
    shells: Mapping[str, Mapping[str, Any]],
) -> bytes:
    """发布流水线使用：生成规范化（排序键、UTF-8）的清单字节，签名覆盖这些原始字节。"""
    document = {
        "schema": MANIFEST_SCHEMA,
        "version": version,
        "tag": tag,
        "commit": commit,
        "payload": dict(payload),
        "shells": {kind: {"revision": entry["revision"], "asset": dict(entry["asset"])} for kind, entry in shells.items()},
    }
    data = (json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    parse_manifest(data)
    return data


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# --- 更新管理 -----------------------------------------------------------------


class UpdateManager:
    def __init__(
        self,
        *,
        current_version: str,
        work_root: Callable[[], Path],
        fetch_json: Callable[[str], Any],
        download_asset: Callable[[dict[str, Any], Path], None],
        extract_zip: Callable[[Path, Path], None],
        log: Callable[..., None],
        runtime_mode: Callable[[], str],
        on_change: Callable[[], None] | None = None,
        environ: Mapping[str, str] | None = None,
        trusted_keys: tuple[str, ...] | None = None,
        platform: str | None = None,
    ) -> None:
        self.current_version = current_version
        self._work_root = work_root
        self._fetch_json = fetch_json
        self._download_asset = download_asset
        self._extract_zip = extract_zip
        self._log = log
        self._runtime_mode = runtime_mode
        self._on_change = on_change
        self._environ = os.environ if environ is None else environ
        self._trusted_keys = trusted_keys
        self._platform = platform or sys.platform
        self._lock = threading.RLock()
        self._busy = ""
        self._error = ""
        self._message = ""
        self._release: dict[str, Any] | None = None
        self._checked_at = 0.0
        self._shell_request: dict[str, Any] | None = None
        self._scheduler: threading.Thread | None = None
        self._cache_loaded = False
        self._bundled_version_cache: tuple[Path, str] | None = None

    # 运行环境 ------------------------------------------------------------------

    def _env(self, name: str) -> str:
        return str(self._environ.get(name) or "").strip()

    @property
    def work_root(self) -> Path:
        return Path(self._work_root())

    @property
    def updates_root(self) -> Path:
        return app_payload.updates_root(self.work_root)

    def shell_root(self) -> Path | None:
        value = self._env(app_payload.ENV_SHELL_ROOT)
        return Path(value) if value else None

    def shell_revision(self) -> str:
        value = self._env(app_payload.ENV_SHELL_REVISION).lower()
        return value if len(value) == 64 else ""

    def shell_kind(self) -> str:
        kind = self._env(ENV_SHELL_KIND)
        if kind not in SHELL_KINDS or not self.shell_revision():
            return ""
        return kind

    def running_payload_version(self) -> str:
        return self._env(app_payload.ENV_PAYLOAD_VERSION)

    def bundled_version(self) -> str:
        # 外壳内置版本在进程生命周期内不变；状态接口调用频繁，缓存避免重复读文件。
        root = self.shell_root()
        if root is None:
            return ""
        cached = self._bundled_version_cache
        if cached is None or cached[0] != root:
            cached = (root, app_payload.read_bundled_version(root))
            self._bundled_version_cache = cached
        return cached[1]

    def _changed(self) -> None:
        if self._on_change is not None:
            try:
                self._on_change()
            except Exception:
                pass

    # 状态 ----------------------------------------------------------------------

    def _manual_reason(self) -> str:
        mode = self._runtime_mode()
        if mode == "docker":
            return "Docker 部署请拉取最新代码后重新构建镜像"
        if self._env(ENV_SHELL_KIND) in SHELL_KINDS:
            return "当前外壳缺少修订号，请下载最新安装包手动升级"
        if mode in {"tray", "tray-backend"}:
            return "当前运行方式不支持应用内更新，请下载新版本或使用 git pull 更新源码"
        return "源码运行请使用 git pull 更新；桌面版请下载新版本安装包"

    def _shell_replace_problem(self, kind: str) -> str:
        shell_path = self._env(ENV_SHELL_PATH)
        if not shell_path:
            return "外壳未提供自身路径，请手动下载新版本"
        path = Path(shell_path)
        if kind == SHELL_KIND_MACOS:
            if self._platform != "darwin":
                return "当前系统不是 macOS"
            if path.suffix != ".app" or not path.is_dir():
                return "无法定位 ProxyBridge App，请手动下载新版本"
            if "/AppTranslocation/" in str(path):
                return "App 正在 macOS 隔离位置运行，请先把 App 移到「应用程序」文件夹并重新打开，再更新"
        elif kind == SHELL_KIND_WINDOWS:
            if not self._platform.startswith("win"):
                return "当前系统不是 Windows"
            if not path.is_file():
                return "无法定位 ProxyBridge.exe，请手动下载新版本"
        if not os.access(path.parent, os.W_OK):
            return f"没有写入 {path.parent} 的权限，请手动下载新版本替换"
        return ""

    def _plan(self, release: dict[str, Any] | None) -> dict[str, Any]:
        if not release:
            return {"available": False, "mode": "", "reason": ""}
        latest = str(release.get("version") or "")
        try:
            newer = app_payload.compare_versions(latest, self.current_version) > 0
        except ValueError:
            newer = False
        if not newer:
            return {"available": False, "mode": "", "reason": ""}
        manifest = release.get("manifest")
        kind = self.shell_kind()
        mode, reason = "manual", ""
        if not kind:
            reason = self._manual_reason()
        elif not isinstance(manifest, dict):
            reason = str(release.get("manifest_error") or "该版本没有可验证的更新清单，请手动下载")
        elif kind not in manifest["shells"]:
            reason = "该版本没有当前平台的安装包，请手动下载"
        elif manifest["shells"][kind]["revision"] == self.shell_revision():
            mode = "payload"
        else:
            reason = self._shell_replace_problem(kind)
            mode = "manual" if reason else "shell"
        failure = next(
            (item for item in reversed(app_payload.load_failures(self.work_root)) if item.get("version") == latest),
            None,
        )
        return {
            "available": True,
            "mode": mode,
            "reason": reason,
            "previous_failure": failure,
        }

    def _active_shell_request(self) -> dict[str, Any] | None:
        request = self._shell_request
        if request and time.time() - float(request.get("at") or 0) > SHELL_REQUEST_TTL:
            self._shell_request = None
            return None
        return request

    def shell_request(self) -> dict[str, Any] | None:
        with self._lock:
            request = self._active_shell_request()
            return {"id": request["id"], "action": request["action"]} if request else None

    def snapshot(self, *, auto_check: bool = True) -> dict[str, Any]:
        self._load_cache()
        with self._lock:
            release = dict(self._release) if self._release else None
            plan = self._plan(release)
            latest = None
            if release:
                latest = {
                    key: release.get(key)
                    for key in ("version", "tag", "url", "name", "published_at", "notes")
                }
            payload_version = self.running_payload_version()
            request = self._active_shell_request()
            return {
                "current_version": self.current_version,
                "bundled_version": self.bundled_version(),
                "payload_version": payload_version,
                "shell_kind": self.shell_kind(),
                "in_app": bool(self.shell_kind()),
                "auto_check": auto_check,
                "checked_at": int(self._checked_at),
                "latest": latest,
                "busy": self._busy,
                "error": self._error,
                "message": self._message,
                "restarting": bool(request),
                "can_rollback": bool(payload_version),
                "releases_url": str((latest or {}).get("url") or RELEASES_PAGE_URL),
                **plan,
            }

    # 缓存 ----------------------------------------------------------------------

    def _load_cache(self) -> None:
        with self._lock:
            if self._cache_loaded:
                return
            self._cache_loaded = True
            data = app_payload.read_json(self.updates_root / CHECK_CACHE_FILENAME)
        if not isinstance(data, dict) or not isinstance(data.get("release"), dict):
            return
        release = data["release"]
        if app_payload.parse_version(release.get("version")) is None:
            return
        manifest = release.get("manifest")
        if manifest is not None:
            try:
                # 缓存只用于展示；安装前总会重新下载并验证签名。
                release["manifest"] = parse_manifest(json.dumps(manifest).encode("utf-8"))
            except ValueError:
                release["manifest"] = None
        with self._lock:
            if self._release is None:
                self._release = release
                self._checked_at = float(data.get("checked_at") or 0)

    def _save_cache(self) -> None:
        with self._lock:
            data = {"checked_at": int(self._checked_at), "release": self._release}
        try:
            app_payload.write_json_atomic(self.updates_root / CHECK_CACHE_FILENAME, data)
        except OSError as error:
            self._log(f"保存更新检查结果失败：{error}", level="warn", category="更新")

    # 检查 ----------------------------------------------------------------------

    def _begin(self, busy: str) -> None:
        with self._lock:
            if self._busy:
                raise UpdateBusyError("正在检查或安装更新，请稍候")
            self._busy = busy
            self._error = ""
        self._changed()

    def _set_busy(self, busy: str) -> None:
        with self._lock:
            self._busy = busy
        self._changed()

    def _fetch_small_asset(self, asset: dict[str, Any], limit: int) -> bytes:
        size = asset.get("size")
        if isinstance(size, int) and size > limit:
            raise ValueError(f"{asset.get('name')} 超过 {limit} 字节限制")
        downloads = self.updates_root / DOWNLOADS_DIRNAME
        target = downloads / f".{secrets.token_hex(6)}.{_safe_asset_name(asset.get('name'))}"
        try:
            self._download_asset(asset, target)
            data = target.read_bytes()
        finally:
            target.unlink(missing_ok=True)
        if len(data) > limit:
            raise ValueError(f"{asset.get('name')} 超过 {limit} 字节限制")
        return data

    def _inspect_release(self, release: Any) -> dict[str, Any]:
        if not isinstance(release, dict):
            raise ValueError("GitHub Release 响应格式无效")
        tag = str(release.get("tag_name") or "")
        version = tag[1:] if tag.startswith("v") else tag
        if app_payload.parse_version(version) is None:
            raise ValueError(f"最新 Release 标签不是有效版本号：{tag or '空'}")
        url = str(release.get("html_url") or "")
        if not url.startswith(f"https://github.com/{UPDATE_REPOSITORY}/"):
            url = RELEASES_PAGE_URL
        assets: dict[str, dict[str, Any]] = {}
        for asset in release.get("assets") or []:
            if isinstance(asset, dict) and asset.get("name"):
                assets[str(asset["name"])] = asset
        info: dict[str, Any] = {
            "version": version,
            "tag": tag,
            "url": url,
            "name": str(release.get("name") or tag)[:200],
            "published_at": str(release.get("published_at") or "")[:40],
            "notes": str(release.get("body") or "")[:4000],
            "manifest": None,
            "manifest_error": "",
        }
        manifest_asset = assets.get(MANIFEST_ASSET_NAME)
        signature_asset = assets.get(SIGNATURE_ASSET_NAME)
        if manifest_asset is None:
            info["manifest_error"] = "该版本没有更新清单，请手动下载"
            return info
        if signature_asset is None:
            info["manifest_error"] = "该版本的更新清单未签名，请手动下载"
            return info
        data = self._fetch_small_asset(manifest_asset, MANIFEST_SIZE_LIMIT)
        signature = parse_signature(self._fetch_small_asset(signature_asset, SIGNATURE_SIZE_LIMIT))
        if not verify_manifest_signature(data, signature, self._trusted_keys):
            info["manifest_error"] = "更新清单签名校验失败，已拒绝自动安装"
            self._log(f"版本 {version} 的更新清单签名校验失败", level="warn", category="更新")
            return info
        manifest = parse_manifest(data)
        if manifest["version"] != version:
            raise ValueError("更新清单版本与 Release 标签不一致")
        # 清单引用的每个文件都必须存在于同一 Release，且大小一致。
        referenced = [manifest["payload"], *(entry["asset"] for entry in manifest["shells"].values())]
        for item in referenced:
            asset = assets.get(item["name"])
            if asset is None or asset.get("size") != item["size"]:
                raise ValueError(f"Release 缺少清单中的文件或大小不一致：{item['name']}")
        info["manifest"] = manifest
        info["_assets"] = {item["name"]: assets[item["name"]] for item in referenced}
        return info

    def _store_release(self, info: dict[str, Any]) -> None:
        public = {key: value for key, value in info.items() if not key.startswith("_")}
        with self._lock:
            self._release = public
            self._checked_at = time.time()
            self._cache_loaded = True
        self._save_cache()

    def check(self) -> dict[str, Any]:
        self._begin("checking")
        try:
            info = self._inspect_release(self._fetch_json(LATEST_RELEASE_API))
            self._store_release(info)
            plan = self._plan(info)
            if plan["available"]:
                self._log(f"发现新版本 {info['version']}（当前 {self.current_version}）", category="更新")
        except Exception as error:
            with self._lock:
                self._error = f"检查更新失败：{error}"
            raise
        finally:
            self._set_busy("")
        return self.snapshot()

    # 安装 ----------------------------------------------------------------------

    def start_apply(self, expected_version: str) -> dict[str, Any]:
        expected = str(expected_version or "").strip()
        if app_payload.parse_version(expected) is None:
            raise ValueError("请先检查更新，再选择要安装的版本")
        self._begin("checking")
        with self._lock:
            self._message = ""
        thread = threading.Thread(target=self._apply_worker, args=(expected,), name="app-update-apply", daemon=True)
        try:
            thread.start()
        except BaseException:
            self._set_busy("")
            raise
        return self.snapshot()

    def _apply_worker(self, expected_version: str) -> None:
        try:
            # 安装前重新获取并验证 Release 与签名清单，不信任本地缓存。
            info = self._inspect_release(self._fetch_json(LATEST_RELEASE_API))
            self._store_release(info)
            if info["version"] != expected_version:
                raise RuntimeError(f"最新版本已变为 {info['version']}，请确认后重新更新")
            plan = self._plan(info)
            if not plan["available"]:
                raise RuntimeError("当前已是最新版本")
            if plan["mode"] == "payload":
                self._install_payload(info)
            elif plan["mode"] == "shell":
                self._install_shell(info)
            else:
                raise RuntimeError(plan["reason"] or "该版本需要手动下载安装")
        except Exception as error:
            with self._lock:
                self._error = f"更新失败：{error}"
            self._log(f"安装更新失败：{error}", level="error", category="更新")
        finally:
            self._set_busy("")

    def _download_verified(self, info: dict[str, Any], item: dict[str, Any]) -> Path:
        asset = info["_assets"][item["name"]]
        downloads = self.updates_root / DOWNLOADS_DIRNAME
        downloads.mkdir(parents=True, exist_ok=True)
        target = downloads / item["name"]
        self._set_busy("downloading")
        self._download_asset(asset, target)
        actual_size = target.stat().st_size
        actual = file_sha256(target)
        if actual_size != item["size"] or not hmac.compare_digest(actual, item["sha256"]):
            target.unlink(missing_ok=True)
            raise RuntimeError(f"{item['name']} 与签名清单中的 SHA-256 不一致")
        return target

    def _install_payload(self, info: dict[str, Any]) -> None:
        manifest = info["manifest"]
        version = manifest["version"]
        work = self.work_root
        archive = self._download_verified(info, manifest["payload"])
        self._set_busy("installing")
        payloads = app_payload.payloads_root(work)
        payloads.mkdir(parents=True, exist_ok=True)
        staging = payloads / f".{version}.{secrets.token_hex(4)}.staging"
        try:
            self._extract_zip(archive, staging)
            problem = app_payload.validate_payload_dir(staging, version)
            if problem:
                raise RuntimeError(problem)
            for required in ("app_payload.py", "app_update.py"):
                if not (staging / required).is_file():
                    raise RuntimeError(f"载荷缺少 {required}")
            final = app_payload.payload_dir(work, version)
            if final.exists():
                shutil.rmtree(final)
            os.replace(staging, final)
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            archive.unlink(missing_ok=True)
        app_payload.activate_payload(work, version, self.shell_revision())
        self._log(f"新版本 {version} 已安装，正在重启后端", category="更新")
        with self._lock:
            self._message = f"新版本 {version} 已安装，正在重启后端"
        self._request_shell("restart-backend")

    def _install_shell(self, info: dict[str, Any]) -> None:
        kind = self.shell_kind()
        manifest = info["manifest"]
        entry = manifest["shells"][kind]
        archive = self._download_verified(info, entry["asset"])
        self._set_busy("installing")
        try:
            if kind == SHELL_KIND_MACOS:
                self._replace_macos_app(archive, manifest["version"], entry["revision"])
            else:
                self._launch_windows_replacement(archive, manifest["version"])
        except BaseException:
            archive.unlink(missing_ok=True)
            raise
        self._log(f"新版本 {manifest['version']} 已下载，正在重新启动 ProxyBridge", category="更新")
        with self._lock:
            self._message = f"新版本 {manifest['version']} 已下载，ProxyBridge 将自动重新启动"
        self._request_shell("quit")

    def _replace_macos_app(self, archive: Path, version: str, revision: str) -> None:
        app_path = Path(self._env(ENV_SHELL_PATH))
        staging = app_path.parent / f"{MACOS_STAGING_PREFIX}{secrets.token_hex(4)}"
        staging.mkdir()
        try:
            # ditto 保留可执行权限、扩展属性与签名；Python zipfile 会丢失这些信息。
            _run_checked(["/usr/bin/ditto", "-x", "-k", str(archive), str(staging)], "解压新版本 App")
            archive.unlink(missing_ok=True)
            candidates = [child for child in staging.iterdir() if child.suffix == ".app" and child.is_dir()]
            if len(candidates) != 1:
                raise RuntimeError("安装包中没有唯一的 .app")
            new_app = candidates[0]
            self._validate_macos_app(new_app, app_path, version, revision)
            _swap_directories(new_app, app_path)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        archive.unlink(missing_ok=True)
        # 交换后 staging 中是旧版本；等待外壳退出后打开新版本并删除旧版本。
        shell_pid = _positive_int(self._env(ENV_PARENT_PID))
        script = (
            'pid="$1"; app="$2"; old="$3"; n=0; '
            'if [ "$pid" -gt 0 ]; then while kill -0 "$pid" 2>/dev/null && [ "$n" -lt 600 ]; '
            "do n=$((n+1)); sleep 0.2; done; fi; "
            '/usr/bin/open "$app"; rm -rf "$old"'
        )
        subprocess.Popen(
            ["/bin/sh", "-c", script, "proxybridge-update", str(shell_pid), str(app_path), str(staging)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )

    def _validate_macos_app(self, new_app: Path, current_app: Path, version: str, revision: str) -> None:
        def bundle_id(app: Path) -> str:
            with (app / "Contents" / "Info.plist").open("rb") as handle:
                return str(plistlib.load(handle).get("CFBundleIdentifier") or "")

        expected_id = bundle_id(current_app)
        if not expected_id or bundle_id(new_app) != expected_id:
            raise RuntimeError("新版本 App 的 Bundle ID 与当前 App 不一致")
        bridge = new_app / "Contents" / "Resources" / "bridge"
        if app_payload.read_bundled_version(bridge) != version:
            raise RuntimeError("新版本 App 内的版本号与清单不一致")
        if app_payload.read_shell_revision(bridge) != revision:
            raise RuntimeError("新版本 App 的外壳修订号与清单不一致")
        _run_checked(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(new_app)], "校验新版本 App 签名")

    def _launch_windows_replacement(self, archive: Path, version: str) -> None:
        with archive.open("rb") as handle:
            if handle.read(2) != b"MZ":
                raise RuntimeError("下载的 Windows 安装包不是可执行文件")
        target = Path(self._env(ENV_SHELL_PATH))
        helper = archive.with_name(f"ProxyBridge-update-{version}.exe")
        os.replace(archive, helper)
        command = [str(helper), WINDOWS_APPLY_UPDATE_FLAG, str(target)]
        for pid in (_positive_int(self._env(ENV_PARENT_PID)), os.getpid()):
            if pid > 0:
                command += [WINDOWS_WAIT_PID_FLAG, str(pid)]
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP：托盘退出时不连带结束替换进程。
        creationflags = 0x00000008 | 0x00000200
        subprocess.Popen(
            command,
            cwd=str(target.parent),
            env=app_payload.independent_process_environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=creationflags,
        )

    def _request_shell(self, action: str) -> None:
        with self._lock:
            self._shell_request = {"id": secrets.token_hex(8), "action": action, "at": time.time()}
        self._changed()

    # 回退 / 启动维护 -----------------------------------------------------------

    def rollback(self) -> dict[str, Any]:
        version = self.running_payload_version()
        if not version:
            raise RuntimeError("当前运行的已是外壳内置版本")
        with self._lock:
            if self._busy:
                raise UpdateBusyError("正在检查或安装更新，请稍候")
        app_payload.record_failure(self.work_root, version, "用户手动回退到内置版本", kind="rollback")
        app_payload.clear_active(self.work_root)
        bundled = self.bundled_version() or "内置版本"
        self._log(f"已从 {version} 回退到 {bundled}，正在重启后端", category="更新")
        with self._lock:
            self._message = f"正在回退到 {bundled}"
        self._request_shell("restart-backend")
        return self.snapshot()

    def confirm_running_payload(self) -> None:
        version = self.running_payload_version()
        if version and app_payload.confirm_payload(self.work_root, version):
            self._log(f"后端载荷 {version} 启动成功", category="更新")

    def maintain(self) -> None:
        """启动时清理过期载荷与下载残留，并在版本变化后记录一次更新结果。"""
        work = self.work_root
        active = app_payload.load_active(work)
        keep = {str(active.get("version"))} if active else set()
        running = self.running_payload_version()
        if running:
            keep.add(running)
        payloads = app_payload.payloads_root(work)
        if payloads.is_dir():
            for child in payloads.iterdir():
                if child.name in keep:
                    continue
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    child.unlink(missing_ok=True)
        downloads = self.updates_root / DOWNLOADS_DIRNAME
        if downloads.is_dir():
            for child in downloads.iterdir():
                try:
                    if child.is_dir():
                        shutil.rmtree(child)
                    else:
                        child.unlink()
                except OSError:
                    # Windows 上替换进程可能仍在退出，下次启动再清理。
                    pass
        self._cleanup_macos_staging()
        result = app_payload.read_json(self.updates_root / SHELL_UPDATE_RESULT_FILENAME)
        if isinstance(result, dict):
            (self.updates_root / SHELL_UPDATE_RESULT_FILENAME).unlink(missing_ok=True)
            if result.get("ok") is False:
                message = f"自动替换 ProxyBridge.exe 失败：{result.get('message') or '未知错误'}"
                self._log(message, level="error", category="更新")
                with self._lock:
                    self._error = message
        last = app_payload.read_json(self.updates_root / LAST_RUN_FILENAME)
        previous = str(last.get("version") or "") if isinstance(last, dict) else ""
        if previous and previous != self.current_version:
            try:
                upgraded = app_payload.compare_versions(self.current_version, previous) > 0
            except ValueError:
                upgraded = False
            message = f"已{'更新' if upgraded else '切换'}到 {self.current_version}（之前 {previous}）"
            self._log(message, category="更新")
            with self._lock:
                self._message = message
        try:
            app_payload.write_json_atomic(
                self.updates_root / LAST_RUN_FILENAME,
                {"version": self.current_version, "at": int(time.time())},
            )
        except OSError:
            pass

    def _cleanup_macos_staging(self) -> None:
        if self.shell_kind() != SHELL_KIND_MACOS:
            return
        app_path = Path(self._env(ENV_SHELL_PATH))
        try:
            siblings = list(app_path.parent.iterdir())
        except OSError:
            return
        for child in siblings:
            if child.name.startswith(MACOS_STAGING_PREFIX) and child.is_dir():
                try:
                    if time.time() - child.stat().st_mtime > 3600:
                        shutil.rmtree(child, ignore_errors=True)
                except OSError:
                    pass

    def start_scheduler(self, auto_check_enabled: Callable[[], bool], stop_event: threading.Event) -> None:
        if self._scheduler is not None:
            return

        def worker() -> None:
            if stop_event.wait(AUTO_CHECK_INITIAL_DELAY):
                return
            while not stop_event.is_set():
                self._load_cache()
                due = time.time() - self._checked_at >= AUTO_CHECK_INTERVAL
                try:
                    if due and auto_check_enabled():
                        self.check()
                except UpdateBusyError:
                    pass
                except Exception as error:  # noqa: BLE001 - 自动检查失败只记录日志
                    self._log(f"自动检查更新失败：{error}", level="warn", category="更新")
                if stop_event.wait(AUTO_CHECK_POLL):
                    return

        self._scheduler = threading.Thread(target=worker, name="app-update-scheduler", daemon=True)
        self._scheduler.start()


def _positive_int(value: Any) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 else 0


def _run_checked(command: list[str], label: str) -> None:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=180)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError(f"{label}失败：{error}") from error
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        raise RuntimeError(f"{label}失败：{detail[-1] if detail else f'退出码 {result.returncode}'}")


def _swap_directories(first: Path, second: Path) -> None:
    """macOS 上原子交换两个目录（renameatx_np + RENAME_SWAP），失败时退化为两次重命名。"""
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        renameatx_np = libc.renameatx_np
        renameatx_np.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        renameatx_np.restype = ctypes.c_int
        at_fdcwd, rename_swap = -2, 0x00000002
        if renameatx_np(at_fdcwd, os.fsencode(first), at_fdcwd, os.fsencode(second), rename_swap) == 0:
            return
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    except (AttributeError, OSError) as error:
        if isinstance(error, OSError) and error.errno not in {None, 45, 78}:  # ENOTSUP / ENOSYS 才退化
            raise RuntimeError(f"替换 App 失败：{error}") from error
    backup = first.with_name(f"{first.name}.previous")
    os.replace(second, backup)
    try:
        os.replace(first, second)
    except OSError:
        os.replace(backup, second)
        raise
    os.replace(backup, first)
