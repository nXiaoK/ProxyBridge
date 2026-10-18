#!/usr/bin/env python3
from __future__ import annotations

import atexit
import base64
import csv
import collections
import concurrent.futures
import errno
import hashlib
import hmac
import http.client
import ipaddress
import io
import json
import mimetypes
import os
import platform
import random
import re
import secrets
import shutil
import signal
import socket
import ssl
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import browser_fingerprint
import webdav_sync
from proxybridge_version import VERSION as APP_VERSION
from remote_browser import RemoteBrowserBrokerClient, RemoteBrowserError


def _detect_app_root() -> Path:
    # PyInstaller onefile/onedir exposes bundled files under sys._MEIPASS.
    meipass = getattr(sys, "_MEIPASS", None)
    if getattr(sys, "frozen", False) and meipass:
        return Path(str(meipass))
    return Path(__file__).resolve().parent


APP_ROOT = _detect_app_root()
VENDOR_ROOT = APP_ROOT / "vendor"
if VENDOR_ROOT.is_dir() and str(VENDOR_ROOT) not in sys.path:
    sys.path.insert(0, str(VENDOR_ROOT))
STATIC_ROOT = APP_ROOT / "static"
SYNC_ENGINE: webdav_sync.SyncEngine | None = None
REMOTE_BROWSER_CLIENT: RemoteBrowserBrokerClient | None = None
REMOTE_BROWSER_CLIENT_KEY: tuple[str, str] | None = None
REMOTE_BROWSER_LOCK = threading.RLock()
REMOTE_BROWSER_SESSION_CONDITION = threading.Condition(REMOTE_BROWSER_LOCK)
REMOTE_BROWSER_SESSION_CACHE: dict[str, Any] = {
    "checked_at": 0.0,
    "sessions": None,
    "error": "",
    "epoch": 0,
    "fetching": False,
}


def host_os_token() -> str:
    """Return a stable OS token used for release asset selection."""
    system = platform.system().lower()
    if system == "darwin":
        return "darwin"
    if system == "windows":
        return "windows"
    if system == "linux":
        return "linux"
    return system or "unknown"


def host_arch_token() -> str:
    """Return arm64 or x86_64 for host architecture checks and downloads."""
    machine = platform.machine().lower()
    if machine in {"arm64", "aarch64"}:
        return "arm64"
    return "x86_64"


def fingerprint_platform_token() -> str:
    os_token = host_os_token()
    if os_token == "darwin":
        return "macos"
    if os_token == "windows":
        return "windows"
    return "linux"


def default_work_root() -> Path:
    env = os.environ.get("PROXY_BRIDGE_WORKDIR")
    if env:
        return Path(env).expanduser()
    # Source/dev default stays repo-local so existing macOS workflows keep working.
    # Production hosts can point PROXY_BRIDGE_WORKDIR at a user data directory.
    return APP_ROOT / "work"


def configure_work_root(path: Path | str) -> None:
    """Rebind runtime data directories. Used by CLI flags and tests."""
    global WORK_ROOT, RUNTIME_ROOT, STATE_PATH
    WORK_ROOT = Path(path).expanduser().resolve()
    RUNTIME_ROOT = WORK_ROOT / "runtime"
    STATE_PATH = WORK_ROOT / "state.json"


WORK_ROOT = default_work_root()
RUNTIME_ROOT = WORK_ROOT / "runtime"
STATE_PATH = WORK_ROOT / "state.json"
DEFAULT_SERVER_PORT = 8787
DEFAULT_SOCKS_PORT = 21080
DEFAULT_DOCKER_PROXY_PORT_END = 21179
DEFAULT_BROWSER_START_URL = "https://ip.skk.moe/"
LOCAL_PROXY_LISTEN_HOST = "127.0.0.1"
LAN_PROXY_LISTEN_HOST = "0.0.0.0"
PRIVATE_IPV4_NETWORKS = tuple(
    ipaddress.IPv4Network(value) for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)
REMOTE_BROWSER_PUBLIC_IPV4_NETWORKS = PRIVATE_IPV4_NETWORKS + (
    ipaddress.IPv4Network("100.64.0.0/10"),
)
REQUEST_LOG_LIMIT = 500
SYSTEM_LOG_LIMIT = 1000
HTTP_PROXY_HEADER_LIMIT = 65536
PROXY_MAX_CLIENTS = 128
PROXY_MAX_CLIENTS_LIMIT = 1024
PROXY_HANDSHAKE_TIMEOUT = 2.0
PROXY_RELAY_IDLE_TIMEOUT = 300.0
DEFAULT_LAN_ALLOWED_CIDRS = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
LAN_ALLOWED_CIDR_LIMIT = 32
LAN_SECRET_MAX_BYTES = 255
API_JSON_BODY_LIMIT = 1_048_576
BACKEND_INSTANCE_HEADER = "X-ProxyBridge-Instance"
BACKEND_INSTANCE_TOKEN_MAX_LENGTH = 128
BACKEND_INSTANCE_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9._~-]+")
GENERATED_BACKEND_INSTANCE_TOKEN = secrets.token_urlsafe(32)
REMOTE_BROWSER_PASSWORD_MIN_LENGTH = 10
REMOTE_BROWSER_PASSWORD_MAX_LENGTH = 128
REMOTE_BROWSER_USERNAME_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,32}")
BROWSER_PROFILE_PART_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,128}")
REMOTE_BROWSER_SESSION_CACHE_TTL = 2.0
REMOTE_BROWSER_SESSIONS_UNSET = object()
REMOTE_BROWSER_LIVE_SESSION_STATUSES = {"running", "starting", "failed"}
ACTIVE_BRIDGE_STATUSES = {"running", "starting"}
ACTIVE_BROWSER_STATUSES = {"running", "starting", "stopping"}
CORE_LOG_VISIBLE_STATUSES = ACTIVE_BRIDGE_STATUSES | {"failed"}
CORE_LOG_TAIL_LIMIT = 12000
CORE_LOG_ENTRY_LIMIT = 30
CORE_LOG_MAX_BYTES = 8 * 1024 * 1024
CORE_LOG_BACKUP_COUNT = 3
LOG_RETENTION_DAYS_MIN = 1
LOG_RETENTION_DAYS_MAX = 365
LOG_FILE_MAX_BYTES_MIN = 1 * 1024 * 1024
LOG_FILE_MAX_BYTES_MAX = 128 * 1024 * 1024
LOG_DISK_QUOTA_BYTES_MIN = 8 * 1024 * 1024
LOG_DISK_QUOTA_BYTES_MAX = 2 * 1024 * 1024 * 1024
LOG_PAGE_SIZE_DEFAULT = 50
LOG_PAGE_SIZE_MAX = 100
SUBSCRIPTION_RESPONSE_LIMIT = 8 * 1024 * 1024
SUBSCRIPTION_REDIRECT_LIMIT = 5
SUBSCRIPTION_FETCH_TIMEOUT = 120.0
SUBSCRIPTION_IO_TIMEOUT = 20.0
SUBSCRIPTION_REFRESH_MINUTES_MIN = 5
SUBSCRIPTION_REFRESH_MINUTES_MAX = 7 * 24 * 60
SUBSCRIPTION_BACKOFF_MAX_SECONDS = 24 * 60 * 60
SUBSCRIPTION_REFRESH_HISTORY_LIMIT = 20
SUBSCRIPTION_BATCH_LIMIT = 50
SUBSCRIPTION_REFRESH_TASK_LIMIT = 20
DEFAULT_SPEED_TEST_TARGETS = (
    "http://cp.cloudflare.com/generate_204",
    "http://connectivitycheck.gstatic.com/generate_204",
    "http://www.msftconnecttest.com/connecttest.txt",
)
DIAGNOSTIC_GITHUB_HOST = "api.github.com"
DIAGNOSTIC_GITHUB_PATH = "/repos/SagerNet/sing-box/releases/latest"
DIAGNOSTIC_OUTBOUND_IP_HOST = "api.ipify.org"
DIAGNOSTIC_OUTBOUND_IP_PATH = "/?format=json"
DIAGNOSTIC_NETWORK_TIMEOUT = 8.0
DIAGNOSTIC_RESPONSE_LIMIT = 64 * 1024
DIAGNOSTIC_MAX_CONCURRENT_RUNS = 2
SPEED_TEST_TARGET_LIMIT = 8
SPEED_TEST_STALE_MIN_SECONDS = 60
SPEED_TEST_STALE_MAX_SECONDS = 30 * 24 * 60 * 60
SPEED_TEST_BATCH_MIN_CONCURRENCY = 3
SPEED_TEST_BATCH_MAX_CONCURRENCY = 5
SPEED_TEST_BATCH_NODE_LIMIT = 500
SPEED_TEST_BATCH_TASK_LIMIT = 20
POOL_CIRCUIT_FAILURE_THRESHOLD = 3
POOL_CIRCUIT_BASE_SECONDS = 60
POOL_CIRCUIT_MAX_SECONDS = 60 * 60
POOL_HALF_OPEN_PROBE_SECONDS = 30
POOL_MANUAL_EXCLUDE_MAX_SECONDS = 7 * 24 * 60 * 60
SMART_GATEWAY_ID = "smart-gateway"
SMART_GATEWAY_DEFAULT_PORT = 21000
SMART_GATEWAY_STRATEGIES = frozenset({"random", "round_robin", "health", "latency"})
SMART_GATEWAY_MAX_CLIENTS = 128
DOWNLOAD_RETRY_COUNT = 3
DOWNLOAD_RETRY_BACKOFF = 0.25
DOWNLOAD_RESPONSE_LIMIT = 2 * 1024 * 1024 * 1024
# 本地内核上传与在线下载共用 2 GiB 上限；上传必须提供准确 Content-Length，
# 并以 1 MiB 分块写入工作目录（Docker 中为 /data）下的私有暂存目录，避免把大文件读入内存。
CORE_UPLOAD_LIMIT = DOWNLOAD_RESPONSE_LIMIT
CORE_UPLOAD_CHUNK_SIZE = 1024 * 1024
# 每个上传分块最多等待 120 秒；只允许一个上传并行，避免多个 2 GiB 暂存文件耗尽数据卷。
CORE_UPLOAD_IO_TIMEOUT = 120.0
CORE_UPLOAD_KINDS = frozenset({"sing-box", "xray", "fingerprint-chromium"})
CORE_UPLOAD_PATH = "/api/cores/upload"
CORE_UPLOAD_KIND_HEADER = "X-ProxyBridge-Upload-Kind"
CORE_UPLOAD_NAME_HEADER = "X-ProxyBridge-Upload-Name"
CORE_UPLOAD_SHA256_HEADER = "X-ProxyBridge-Upload-Sha256"
# 压缩包解压后的总文件大小和成员数也必须有界，防止压缩炸弹耗尽数据卷。
ARCHIVE_EXTRACTED_SIZE_LIMIT = 4 * 1024 * 1024 * 1024
ARCHIVE_MEMBER_LIMIT = 100_000
GITHUB_JSON_RESPONSE_LIMIT = 8 * 1024 * 1024
POOL_TOKEN_MIN_LEN = 16
POOL_TOKEN_MAX_LEN = 128
# 字符集与 BACKEND_INSTANCE_TOKEN_PATTERN 对齐；长度策略刻意更严（min 16）。
POOL_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9._~-]+")
# Only consumer paths `/pool/{token}/…` (or requestline with a space before them).
# Do not match console routes like `/api/pool/token/rotate`.
_POOL_PATH_REDACT = re.compile(r"(^|[\s\"'])(/pool/)([^/\s]+)(/[^\s]*)?")
ALLOWED_PROXY_PROTOCOLS = frozenset({"socks5", "http"})
PROCESS_TABLE: dict[str, subprocess.Popen] = {}
BRIDGE_NODE_TABLE: dict[str, str] = {}
RUNTIME_LOG_PUMP_TABLE: dict[str, threading.Thread] = {}
# 兼容旧的节点级浏览器进程表;新的多开浏览器以 BROWSER_PROCESS_TABLE 按实例记录。
BROWSER_TABLE: dict[str, list[subprocess.Popen]] = {}
BROWSER_PROCESS_TABLE: dict[str, subprocess.Popen] = {}
# 本机浏览器最近一次启动命令（仅内存）：未开启 CDP 时「打开网址」复用完整参数交给
# Chromium 单实例转发，即使原进程恰好退出，新进程也仍带代理与指纹参数。
BROWSER_LAUNCH_COMMANDS: dict[str, list[str]] = {}
# macOS 上关闭 Chromium 最后一个窗口后进程仍驻留在 Dock。启动本机浏览器时生成一次性令牌，
# 由窗口助手扩展在窗口全部关闭后携带令牌通知控制台停止该浏览器：令牌 -> (实例 ID, 进程)。
BROWSER_WINDOW_TOKENS: dict[str, tuple[str, subprocess.Popen]] = {}
LOG_PROXY_TABLE: dict[str, "Socks5LogProxy"] = {}
TRAFFIC_TABLE: dict[str, dict[str, int]] = {}
REQUEST_LOG_PENDING_UPDATES: dict[str, dict[str, Any]] = {}
REQUEST_LOG_TERMINAL_STATUSES = frozenset({"closed", "failed"})
TEST_PROCESS_TABLE: dict[str, subprocess.Popen] = {}
SPEED_TEST_TASKS: "collections.OrderedDict[str, dict[str, Any]]" = collections.OrderedDict()
SPEED_TEST_CANCEL_EVENTS: dict[str, threading.Event] = {}
SUBSCRIPTION_REFRESH_TASKS: "collections.OrderedDict[str, dict[str, Any]]" = collections.OrderedDict()
SUBSCRIPTION_REFRESH_CANCEL_EVENTS: dict[str, threading.Event] = {}
ACTIVE_PROXY_CLIENTS: dict[str, dict[str, Any]] = {}
SMART_GATEWAY_TABLE: dict[str, "SmartGatewayProxy"] = {}
STATE_LOCK = threading.RLock()
PROCESS_LOCK = threading.RLock()
TRAFFIC_LOCK = threading.RLock()
REQUEST_LOG_PENDING_LOCK = threading.RLock()
SHUTTING_DOWN = threading.Event()
SHUTDOWN_COMPLETE = threading.Event()
SHUTDOWN_RUN_LOCK = threading.Lock()
SHUTDOWN_SIGNAL_CLAIMED = threading.Event()
SHUTDOWN_SIGNAL_CLAIM_LOCK = threading.Lock()
PATH_CACHE_LOCK = threading.RLock()
LAN_ADDRESS_CACHE_LOCK = threading.RLock()
CORE_INSTALL_LOCK = threading.RLock()
CORE_UPLOAD_LOCK = threading.Lock()
BRIDGE_START_LOCK = threading.RLock()
BROWSER_LAUNCH_LOCK = threading.RLock()
REMOTE_BROWSER_LIFECYCLE_CONDITION = threading.Condition(threading.RLock())
REMOTE_BROWSER_GENERATIONS: dict[str, int] = {}
REMOTE_BROWSER_PENDING_STARTS: dict[str, int] = {}
REMOTE_BROWSER_PENDING_START_TOKENS: dict[str, str] = {}
REMOTE_BROWSER_OPERATIONS: set[tuple[str, int, str]] = set()
FINGERPRINT_INSTALL_LOCK = threading.RLock()
SHUTDOWN_LOCK = threading.RLock()
SUBSCRIPTION_LOCK = threading.RLock()
LOG_ROTATION_LOCK = threading.RLock()
POOL_RR_LOCK = threading.Lock()
SPEED_TEST_TASK_LOCK = threading.RLock()
SUBSCRIPTION_REFRESH_TASK_LOCK = threading.RLock()
PROXY_CLIENT_LOCK = threading.RLock()
SPEED_TEST_TARGET_LOCK = threading.Lock()
DIAGNOSTIC_RUN_SEMAPHORE = threading.BoundedSemaphore(DIAGNOSTIC_MAX_CONCURRENT_RUNS)
SUBSCRIPTION_SCHEDULER_LOCK = threading.Lock()
SUBSCRIPTION_SCHEDULER_WAKE = threading.Event()
SUBSCRIPTION_SCHEDULER_THREAD: threading.Thread | None = None
# 状态流复用现有实例认证；变更通知不携带订阅、密码等数据。
STATUS_EVENT_CONDITION = threading.Condition()
STATUS_EVENT_REVISION = 0
BRIDGE_HEALTH_THREAD: threading.Thread | None = None
BRIDGE_HEALTH_LOCK = threading.Lock()
# 浏览器出口自检最多三路，避免批量启动时同时占满网络连接。
BROWSER_EXIT_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=3, thread_name_prefix="browser-exit")
# 每 60 秒最多三路检查现有测速目标；探活本身不迁移连接，可选切换另行处理。
BRIDGE_HEALTH_INTERVAL = 60
SMART_GATEWAY_LOCK = threading.RLock()
POOL_RR_COUNTER = 0
SPEED_TEST_TARGET_COUNTER = 0
CORE_AUTO_INSTALL_STARTED = threading.Event()
FINGERPRINT_CHROMIUM_CACHE_TTL = 300.0
FINGERPRINT_CHROMIUM_CACHE: dict[str, Any] = {"key": None, "path": None, "checked_at": 0.0}
# 节点出口 IP 查询缓存：键为节点 ID，值含时区/国家与单调时钟时间。启动前按出口匹配时区
# 与语言时复用，避免批量启动时重复访问 ipwho.is；默认 10 分钟内有效。
EXIT_LOCALE_CACHE_TTL = 600.0
EXIT_LOCALE_CACHE: dict[str, dict[str, Any]] = {}
EXIT_LOCALE_CACHE_LOCK = threading.Lock()
# 扩展库上传/删除与 Profile 内扩展副本同步共用一把锁，防止并发复制半成品目录。
BROWSER_EXTENSION_LOCK = threading.RLock()
# 扩展包上传上限 64 MiB；解压后的文件数与体积仍受通用归档限制约束。
BROWSER_EXTENSION_UPLOAD_LIMIT = 64 * 1024 * 1024
BROWSER_EXTENSION_UPLOAD_PATH = "/api/browser/extensions/upload"
# 窗口助手扩展回调地址；只接受启动时下发的一次性令牌，不需要管理令牌，也不检查来源。
BROWSER_WINDOW_CLOSED_PATH = "/api/browser/window-closed"
# 一键检测可打开的指纹检测站点；前端只显示这些固定目标。
FINGERPRINT_CHECK_SITES = {
    "browserscan": "https://www.browserscan.net/",
    "pixelscan": "https://pixelscan.net/",
    "creepjs": "https://abrahamjuliot.github.io/creepjs/",
}
LAN_ADDRESS_CACHE_TTL = 30.0
LAN_ADDRESS_CACHE: dict[str, Any] = {"addresses": [], "checked_at": 0.0}
PROXY_REBIND_RETRY_TIMEOUT = 1.0

# 系统日志:App 自身运行事件的内存环形缓冲区,用于在控制台「系统日志」视图里
# 排查软件使用中的问题(启动、订阅导入、本地代理启停、测试、报错等)。
# 与「连接日志」(per-connection)、「核心日志」(sing-box/xray 输出)是三条独立的日志线。
SYSTEM_LOGS: "collections.deque[dict[str, Any]]" = collections.deque(maxlen=SYSTEM_LOG_LIMIT)
SYSTEM_LOG_LOCK = threading.RLock()
RUNTIME_LOG_SETTINGS_CACHE: dict[str, int] = {
    "retention_days": 14,
    "max_file_bytes": CORE_LOG_MAX_BYTES,
    "disk_quota_bytes": 128 * 1024 * 1024,
}


def _is_loopback_host(value: str) -> bool:
    match = re.fullmatch(
        r"(?:127\.0\.0\.1|localhost|\[::1\])(?::([0-9]+))?",
        value,
        re.IGNORECASE,
    )
    if match is None:
        return False
    port = match.group(1)
    if port is None:
        return True
    normalized_port = port.lstrip("0") or "0"
    return len(normalized_port) <= 5 and int(normalized_port) <= 65535


def _host_ipv4_literal(value: str) -> ipaddress.IPv4Address | None:
    match = re.fullmatch(r"([0-9]{1,3}(?:\.[0-9]{1,3}){3})(?::([0-9]+))?", value)
    if match is None:
        return None
    port = match.group(2)
    if port is not None:
        normalized_port = port.lstrip("0") or "0"
        if len(normalized_port) > 5 or int(normalized_port) > 65535:
            return None
    try:
        return ipaddress.IPv4Address(match.group(1))
    except ipaddress.AddressValueError:
        return None


def _configured_docker_console_ip() -> ipaddress.IPv4Address | None:
    if not docker_mode():
        return None
    raw = os.environ.get("PROXY_BRIDGE_CONSOLE_HOST", "")
    if not raw or raw != raw.strip():
        return None
    try:
        address = ipaddress.IPv4Address(raw)
    except ipaddress.AddressValueError:
        return None
    if address.is_loopback or any(address in network for network in PRIVATE_IPV4_NETWORKS):
        return address
    return None


def _is_trusted_console_host(value: str) -> bool:
    if _is_loopback_host(value):
        return True
    request_ip = _host_ipv4_literal(value)
    configured_ip = _configured_docker_console_ip()
    return request_ip is not None and configured_ip is not None and request_ip == configured_ip


def backend_instance_token() -> str | None:
    token = os.environ.get("PROXY_BRIDGE_INSTANCE_TOKEN", "")
    if not 1 <= len(token) <= BACKEND_INSTANCE_TOKEN_MAX_LENGTH:
        return None
    if BACKEND_INSTANCE_TOKEN_PATTERN.fullmatch(token) is None:
        return None
    return token


def management_api_token() -> str:
    return backend_instance_token() or GENERATED_BACKEND_INSTANCE_TOKEN


def management_tokens_equal(provided: str, expected: str | None = None) -> bool:
    token = expected or management_api_token()
    if (
        not 1 <= len(provided) <= BACKEND_INSTANCE_TOKEN_MAX_LENGTH
        or BACKEND_INSTANCE_TOKEN_PATTERN.fullmatch(provided) is None
        or not 1 <= len(token) <= BACKEND_INSTANCE_TOKEN_MAX_LENGTH
        or BACKEND_INSTANCE_TOKEN_PATTERN.fullmatch(token) is None
    ):
        return False
    return hmac.compare_digest(provided, token)


def console_access_url(base_url: str, token: str | None = None) -> str:
    access_token = token or management_api_token()
    fragment = urllib.parse.urlencode({"token": access_token})
    return f"{base_url.rstrip('/')}/#{fragment}"


def log_event(message: str, level: str = "info", category: str = "") -> None:
    """记录一条系统日志事件:写入内存环形缓冲区,同时打印到 stdout。

    stdout 会被原生 App 重定向到 menubar-backend.log,因此事件既能在控制台
    实时查看,也会持久化到磁盘日志文件。level 取 info/warn/error。
    """
    entry = {
        "ts": time.time(),
        "level": level if level in {"info", "warn", "error"} else "info",
        "category": category,
        "message": str(message),
    }
    with SYSTEM_LOG_LOCK:
        SYSTEM_LOGS.append(entry)
    notify_status_change()
    # 同步打印到标准输出,格式化为「时间 LEVEL [分类] 消息」便于在日志文件里检索。
    stamp = time.strftime("%H:%M:%S", time.localtime(entry["ts"]))
    tag = f"[{category}] " if category else ""
    print(f"{stamp} {entry['level'].upper():5} {tag}{message}", flush=True)


def system_log_entries() -> list[dict[str, Any]]:
    """返回系统日志快照(最新的在前),供前端展示。"""
    with SYSTEM_LOG_LOCK:
        return list(reversed(SYSTEM_LOGS))


def clear_system_logs() -> dict[str, Any]:
    """清空系统日志缓冲区。"""
    with SYSTEM_LOG_LOCK:
        SYSTEM_LOGS.clear()
    log_event("系统日志已清空", category="系统")
    return {"ok": True}


def notify_status_change() -> None:
    global STATUS_EVENT_REVISION
    with STATUS_EVENT_CONDITION:
        STATUS_EVENT_REVISION += 1
        STATUS_EVENT_CONDITION.notify_all()


@dataclass
class Node:
    id: str
    name: str
    protocol: str
    server: str
    port: int
    region: str
    source: str
    raw: str
    settings: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    transport: str = ""
    security: str = ""
    preferred_core: str = "sing-box"
    fallback_enabled: bool = False


class CoreStartError(RuntimeError):
    pass


class CoreUploadTooLarge(ValueError):
    pass


class CoreUploadUnsupportedMediaType(ValueError):
    pass


class CoreUploadLengthRequired(ValueError):
    pass


class CoreUploadBusy(RuntimeError):
    pass


def now_ts() -> int:
    return int(time.time())


def proxy_core_root() -> Path:
    return WORK_ROOT / "proxy-cores"


def proxy_core_bin_dir() -> Path:
    return proxy_core_root() / "bin"


def ensure_dirs() -> None:
    paths = [
        WORK_ROOT,
        RUNTIME_ROOT,
        proxy_core_root(),
        proxy_core_bin_dir(),
    ]
    if browser_feature_enabled():
        paths.extend((browser_profiles_root(), browser_extensions_root()))
    for path in paths:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            path.chmod(0o700)
        except OSError:
            # Windows and some mounted filesystems ignore or reject POSIX modes.
            pass


def browser_profiles_root() -> Path:
    return WORK_ROOT / "browser_profiles"


def browser_extensions_root() -> Path:
    return WORK_ROOT / "browser_extensions"


def clean_name(value: str | None, fallback: str) -> str:
    if not value:
        return fallback
    decoded = urllib.parse.unquote(value).strip()
    return re.sub(r"\s+", " ", decoded) or fallback


def first(values: dict[str, list[str]], *keys: str, default: str = "") -> str:
    for key in keys:
        for candidate in (key, key.lower(), key.upper()):
            if candidate in values and values[candidate]:
                return values[candidate][0]
    return default


def truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def docker_mode() -> bool:
    """Return whether this process is running with Docker-specific capabilities."""
    return truthy(os.environ.get("PROXY_BRIDGE_DOCKER"))


def remote_browser_feature_enabled() -> bool:
    """Return whether Docker remote-browser workers are configured.

    配置说明：PROXY_BRIDGE_BROWSER_BROKER_URL 只允许指向 Compose 内部网络的
    Broker；PROXY_BRIDGE_BROWSER_BROKER_TOKEN_FILE 是共享只读凭证文件，不能映射到
    Web 控制台或公开目录（未设置时使用容器内固定路径）。Broker URL 缺失时 Docker
    会保持旧版“禁用浏览器”的安全行为。
    """
    if not docker_mode() or truthy(os.environ.get("PROXY_BRIDGE_DISABLE_BROWSER")):
        return False
    return bool(str(os.environ.get("PROXY_BRIDGE_BROWSER_BROKER_URL") or "").strip())


def browser_feature_enabled() -> bool:
    """Return whether local or Docker-worker browser sessions are available."""
    if docker_mode():
        return remote_browser_feature_enabled()
    return not truthy(os.environ.get("PROXY_BRIDGE_DISABLE_BROWSER"))


def require_browser_feature() -> None:
    if not browser_feature_enabled():
        raise RuntimeError("当前运行模式不提供浏览器功能")


def remote_browser_broker_client() -> RemoteBrowserBrokerClient:
    global REMOTE_BROWSER_CLIENT, REMOTE_BROWSER_CLIENT_KEY
    if not remote_browser_feature_enabled():
        raise RuntimeError("Docker 远程浏览器功能未配置")
    url = str(os.environ.get("PROXY_BRIDGE_BROWSER_BROKER_URL") or "").strip()
    token_path = str(
        os.environ.get("PROXY_BRIDGE_BROWSER_BROKER_TOKEN_FILE")
        or "/run/proxybridge-browser/token"
    ).strip()
    key = (url, token_path)
    with REMOTE_BROWSER_LOCK:
        if REMOTE_BROWSER_CLIENT is None or REMOTE_BROWSER_CLIENT_KEY != key:
            REMOTE_BROWSER_CLIENT = RemoteBrowserBrokerClient(url, Path(token_path))
            REMOTE_BROWSER_CLIENT_KEY = key
        return REMOTE_BROWSER_CLIENT


def invalidate_remote_browser_session_cache() -> None:
    with REMOTE_BROWSER_SESSION_CONDITION:
        REMOTE_BROWSER_SESSION_CACHE.update(
            {
                "checked_at": 0.0,
                "sessions": None,
                "error": "",
                "epoch": safe_int(REMOTE_BROWSER_SESSION_CACHE.get("epoch"), 0) + 1,
            }
        )
        REMOTE_BROWSER_SESSION_CONDITION.notify_all()


def begin_remote_browser_start(instance_id: str) -> tuple[int, str]:
    with REMOTE_BROWSER_LIFECYCLE_CONDITION:
        generation = REMOTE_BROWSER_GENERATIONS.get(instance_id, 0) + 1
        operation_id = secrets.token_hex(16)
        REMOTE_BROWSER_GENERATIONS[instance_id] = generation
        REMOTE_BROWSER_PENDING_STARTS[instance_id] = generation
        REMOTE_BROWSER_PENDING_START_TOKENS[instance_id] = operation_id
        return generation, operation_id


def remote_browser_pending_start_tokens(
    instance_ids: list[str] | set[str] | None = None,
) -> dict[str, str]:
    with REMOTE_BROWSER_LIFECYCLE_CONDITION:
        selected = (
            set(str(item or "") for item in instance_ids)
            if instance_ids is not None
            else set(REMOTE_BROWSER_PENDING_START_TOKENS)
        )
        return {
            instance_id: token
            for instance_id, token in REMOTE_BROWSER_PENDING_START_TOKENS.items()
            if instance_id in selected and token
        }


def invalidate_remote_browser_generations(instance_ids: list[str] | set[str]) -> dict[str, int]:
    generations: dict[str, int] = {}
    with REMOTE_BROWSER_LIFECYCLE_CONDITION:
        for instance_id in dict.fromkeys(str(item or "") for item in instance_ids):
            if not instance_id:
                continue
            generation = REMOTE_BROWSER_GENERATIONS.get(instance_id, 0) + 1
            REMOTE_BROWSER_GENERATIONS[instance_id] = generation
            generations[instance_id] = generation
        REMOTE_BROWSER_LIFECYCLE_CONDITION.notify_all()
    return generations


def remote_browser_generation_current(instance_id: str, generation: int) -> bool:
    with REMOTE_BROWSER_LIFECYCLE_CONDITION:
        return REMOTE_BROWSER_GENERATIONS.get(instance_id, 0) == generation


def remote_browser_start_pending(instance_id: str) -> bool:
    with REMOTE_BROWSER_LIFECYCLE_CONDITION:
        generation = REMOTE_BROWSER_GENERATIONS.get(instance_id, 0)
        return REMOTE_BROWSER_PENDING_STARTS.get(instance_id) == generation


def any_remote_browser_start_pending() -> bool:
    with REMOTE_BROWSER_LIFECYCLE_CONDITION:
        return any(
            REMOTE_BROWSER_GENERATIONS.get(instance_id, 0) == generation
            for instance_id, generation in REMOTE_BROWSER_PENDING_STARTS.items()
        )


def finish_remote_browser_start(instance_id: str, generation: int) -> None:
    with REMOTE_BROWSER_LIFECYCLE_CONDITION:
        if REMOTE_BROWSER_PENDING_STARTS.get(instance_id) == generation:
            REMOTE_BROWSER_PENDING_STARTS.pop(instance_id, None)
            REMOTE_BROWSER_PENDING_START_TOKENS.pop(instance_id, None)
        REMOTE_BROWSER_LIFECYCLE_CONDITION.notify_all()


def prune_remote_browser_lifecycle(instance_id: str) -> None:
    with REMOTE_BROWSER_LIFECYCLE_CONDITION:
        active_operation = any(item[0] == instance_id for item in REMOTE_BROWSER_OPERATIONS)
        if instance_id not in REMOTE_BROWSER_PENDING_STARTS and not active_operation:
            REMOTE_BROWSER_GENERATIONS.pop(instance_id, None)
            REMOTE_BROWSER_PENDING_START_TOKENS.pop(instance_id, None)


def register_remote_browser_operation(instance_id: str, generation: int, operation: str) -> bool:
    token = (instance_id, generation, operation)
    with REMOTE_BROWSER_LIFECYCLE_CONDITION:
        if SHUTTING_DOWN.is_set() or REMOTE_BROWSER_GENERATIONS.get(instance_id, 0) != generation:
            return False
        REMOTE_BROWSER_OPERATIONS.add(token)
        return True


def finish_remote_browser_operation(instance_id: str, generation: int, operation: str) -> None:
    with REMOTE_BROWSER_LIFECYCLE_CONDITION:
        REMOTE_BROWSER_OPERATIONS.discard((instance_id, generation, operation))
        REMOTE_BROWSER_LIFECYCLE_CONDITION.notify_all()


def invalidate_all_remote_browser_operations() -> set[str]:
    with REMOTE_BROWSER_LIFECYCLE_CONDITION:
        instance_ids = (
            set(REMOTE_BROWSER_PENDING_STARTS)
            | {item[0] for item in REMOTE_BROWSER_OPERATIONS}
        )
        for instance_id in instance_ids:
            REMOTE_BROWSER_GENERATIONS[instance_id] = (
                REMOTE_BROWSER_GENERATIONS.get(instance_id, 0) + 1
            )
        REMOTE_BROWSER_LIFECYCLE_CONDITION.notify_all()
        return instance_ids


def wait_remote_browser_operations(timeout: float) -> bool:
    deadline = time.monotonic() + max(0.0, timeout)
    with REMOTE_BROWSER_LIFECYCLE_CONDITION:
        while REMOTE_BROWSER_OPERATIONS:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            REMOTE_BROWSER_LIFECYCLE_CONDITION.wait(timeout=remaining)
        return True


def remote_browser_sessions(
    *,
    force: bool = False,
    timeout: float | None = None,
) -> tuple[dict[str, dict[str, Any]] | None, str]:
    """Return (sessions_by_instance, error). None means the broker is unreachable."""
    if not remote_browser_feature_enabled():
        return {}, ""
    wait_deadline = (
        time.monotonic() + max(1.0, float(timeout)) + 0.5
        if timeout is not None
        else None
    )
    force_refresh = force
    while True:
        current = time.monotonic()
        if wait_deadline is not None and current >= wait_deadline:
            return None, "等待浏览器 Broker 会话快照超时"
        with REMOTE_BROWSER_SESSION_CONDITION:
            cached_at = float(REMOTE_BROWSER_SESSION_CACHE.get("checked_at") or 0.0)
            cached_sessions = REMOTE_BROWSER_SESSION_CACHE.get("sessions")
            cached_error = str(REMOTE_BROWSER_SESSION_CACHE.get("error") or "")
            if (
                not force_refresh
                and cached_at > 0.0
                and current - cached_at < REMOTE_BROWSER_SESSION_CACHE_TTL
            ):
                return cached_sessions, cached_error
            if REMOTE_BROWSER_SESSION_CACHE.get("fetching") is True:
                wait_timeout = 30.5
                if wait_deadline is not None:
                    wait_timeout = wait_deadline - current
                    if wait_timeout <= 0:
                        return None, "等待浏览器 Broker 会话快照超时"
                REMOTE_BROWSER_SESSION_CONDITION.wait(timeout=wait_timeout)
                force_refresh = False
                continue
            fetch_epoch = safe_int(REMOTE_BROWSER_SESSION_CACHE.get("epoch"), 0)
            REMOTE_BROWSER_SESSION_CACHE["fetching"] = True

        try:
            request_timeout = timeout
            if wait_deadline is not None:
                request_timeout = max(1.0, wait_deadline - time.monotonic())
            broker_client = remote_browser_broker_client()
            broker_sessions = (
                broker_client.sessions()
                if request_timeout is None
                else broker_client.sessions(timeout=request_timeout)
            )
            sessions = {
                str(item.get("instance_id") or ""): dict(item)
                for item in broker_sessions
                if item.get("instance_id")
            }
            error = ""
        except (RemoteBrowserError, RuntimeError, ValueError) as exc:
            sessions = None
            error = str(exc)
        except BaseException:
            with REMOTE_BROWSER_SESSION_CONDITION:
                REMOTE_BROWSER_SESSION_CACHE["fetching"] = False
                REMOTE_BROWSER_SESSION_CONDITION.notify_all()
            raise

        with REMOTE_BROWSER_SESSION_CONDITION:
            REMOTE_BROWSER_SESSION_CACHE["fetching"] = False
            if safe_int(REMOTE_BROWSER_SESSION_CACHE.get("epoch"), 0) == fetch_epoch:
                REMOTE_BROWSER_SESSION_CACHE.update(
                    {"checked_at": time.monotonic(), "sessions": sessions, "error": error}
                )
                REMOTE_BROWSER_SESSION_CONDITION.notify_all()
                return sessions, error
            # A start/stop invalidated this request while it was in flight. Do not let
            # that stale snapshot repopulate the cache; fetch the new lifecycle epoch.
            REMOTE_BROWSER_SESSION_CONDITION.notify_all()
            force_refresh = False


def remote_browser_broker_status() -> dict[str, Any]:
    if not remote_browser_feature_enabled():
        return {"configured": False, "available": False}
    try:
        status = remote_browser_broker_client().status()
        return {"configured": True, "available": True, **status}
    except (RemoteBrowserError, RuntimeError, ValueError) as exc:
        return {"configured": True, "available": False, "error": str(exc)}


def normalize_remote_browser_url(value: Any) -> str:
    """只接受 Broker 可生成的私有 HTTPS worker 链接。"""
    try:
        parsed = urllib.parse.urlsplit(str(value or "").strip())
        port = parsed.port
        address = ipaddress.IPv4Address(str(parsed.hostname or ""))
    except (ValueError, ipaddress.AddressValueError):
        return ""
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or port is None
        or not 1024 <= port <= 65535
        or not (
            address.is_loopback
            or any(address in network for network in REMOTE_BROWSER_PUBLIC_IPV4_NETWORKS)
        )
    ):
        return ""
    return f"https://{address}:{port}/"


def normalize_remote_browser_container_id(value: Any) -> str:
    container_id = str(value or "").strip().lower()
    return container_id if re.fullmatch(r"[a-f0-9]{12,64}", container_id) else ""


def runtime_mode() -> str:
    return "docker" if docker_mode() else str(os.environ.get("PROXY_BRIDGE_SHELL_MODE") or "headless")


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def clamp_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    return min(max(safe_int(value, default), minimum), maximum)


def normalize_speed_test_target(value: Any) -> str:
    raw = str(value or "").strip()
    try:
        parsed = urllib.parse.urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("测速目标 URL 格式无效") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("测速目标必须是 http(s) URL")
    if parsed.username is not None or parsed.password is not None or parsed.fragment:
        raise ValueError("测速目标不能包含凭据或 fragment")
    target_port = port or (443 if parsed.scheme.lower() == "https" else 80)
    if target_port <= 0 or target_port > 65535:
        raise ValueError("测速目标端口无效")
    hostname = parsed.hostname.encode("idna").decode("ascii")
    display_hostname = f"[{hostname}]" if ":" in hostname else hostname
    netloc = (
        display_hostname
        if target_port == (443 if parsed.scheme.lower() == "https" else 80)
        else f"{display_hostname}:{target_port}"
    )
    path = urllib.parse.quote(parsed.path or "/", safe="/%:@!$&'()*+,;=-._~")
    query = urllib.parse.quote(parsed.query, safe="%=&?/:;+,@!$'()*-._~")
    normalized = urllib.parse.urlunsplit((parsed.scheme.lower(), netloc, path, query, ""))
    if len(normalized) > 2048:
        raise ValueError("测速目标 URL 过长")
    return normalized


def normalize_speed_test_settings(value: Any) -> dict[str, Any]:
    raw = value if isinstance(value, dict) else {}
    targets: list[str] = []
    source_targets = raw.get("targets") if isinstance(raw.get("targets"), list) else DEFAULT_SPEED_TEST_TARGETS
    for candidate in source_targets[:SPEED_TEST_TARGET_LIMIT]:
        try:
            target = normalize_speed_test_target(candidate)
        except ValueError:
            continue
        if target not in targets:
            targets.append(target)
    if not targets:
        targets = list(DEFAULT_SPEED_TEST_TARGETS)
    rotation = str(raw.get("rotation") or "round_robin")
    if rotation not in {"round_robin", "random"}:
        rotation = "round_robin"
    return {
        "targets": targets,
        "rotation": rotation,
        "stale_after_seconds": clamp_int(
            raw.get("stale_after_seconds"),
            6 * 60 * 60,
            SPEED_TEST_STALE_MIN_SECONDS,
            SPEED_TEST_STALE_MAX_SECONDS,
        ),
        "batch_concurrency": clamp_int(
            raw.get("batch_concurrency"),
            SPEED_TEST_BATCH_MIN_CONCURRENCY,
            SPEED_TEST_BATCH_MIN_CONCURRENCY,
            SPEED_TEST_BATCH_MAX_CONCURRENCY,
        ),
        "retry_failures": clamp_int(raw.get("retry_failures"), 1, 0, 2),
    }


def normalize_log_settings(value: Any) -> dict[str, int]:
    raw = value if isinstance(value, dict) else {}
    return {
        "retention_days": clamp_int(raw.get("retention_days"), 14, LOG_RETENTION_DAYS_MIN, LOG_RETENTION_DAYS_MAX),
        "max_file_bytes": clamp_int(
            raw.get("max_file_bytes"),
            CORE_LOG_MAX_BYTES,
            LOG_FILE_MAX_BYTES_MIN,
            LOG_FILE_MAX_BYTES_MAX,
        ),
        "disk_quota_bytes": clamp_int(
            raw.get("disk_quota_bytes"),
            128 * 1024 * 1024,
            LOG_DISK_QUOTA_BYTES_MIN,
            LOG_DISK_QUOTA_BYTES_MAX,
        ),
    }


def normalize_subscription_settings(value: Any) -> dict[str, bool]:
    raw = value if isinstance(value, dict) else {}
    return {
        "allow_non_public_urls": raw.get("allow_non_public_urls") is True,
    }


def cache_log_settings(value: Any) -> dict[str, int]:
    normalized = normalize_log_settings(value)
    with LOG_ROTATION_LOCK:
        RUNTIME_LOG_SETTINGS_CACHE.clear()
        RUNTIME_LOG_SETTINGS_CACHE.update(normalized)
    return normalized


def runtime_log_settings_snapshot() -> dict[str, int]:
    with LOG_ROTATION_LOCK:
        return dict(RUNTIME_LOG_SETTINGS_CACHE)


def normalize_subscription_schedule(subscription: dict[str, Any]) -> None:
    # 过滤规则为空时导入全部节点；正则必须由用户显式启用。
    subscription.setdefault("filter_include", "")
    subscription.setdefault("filter_exclude", "")
    subscription["filter_regex"] = subscription.get("filter_regex") is True
    interval = safe_int(subscription.get("refresh_interval_minutes"), 0)
    if interval != 0:
        interval = min(max(interval, SUBSCRIPTION_REFRESH_MINUTES_MIN), SUBSCRIPTION_REFRESH_MINUTES_MAX)
    subscription["refresh_interval_minutes"] = interval
    subscription["refresh_failures"] = max(0, safe_int(subscription.get("refresh_failures"), 0))
    for field_name in ("last_attempt", "next_refresh_at"):
        subscription[field_name] = max(0, safe_int(subscription.get(field_name), 0))
    subscription["etag"] = str(subscription.get("etag") or "")[:512]
    subscription["last_modified"] = str(subscription.get("last_modified") or "")[:512]
    subscription["last_error"] = str(subscription.get("last_error") or "")[:1000]
    changes = subscription.get("last_changes")
    subscription["last_changes"] = changes if isinstance(changes, dict) else {}
    history = subscription.get("refresh_history")
    normalized_history: list[dict[str, Any]] = []
    if isinstance(history, list):
        for item in history[-SUBSCRIPTION_REFRESH_HISTORY_LIMIT:]:
            if not isinstance(item, dict):
                continue
            status = str(item.get("status") or "")
            if status not in {"updated", "not_modified", "failed", "cancelled"}:
                continue
            changes = item.get("changes")
            normalized_changes = {
                key: max(0, safe_int(changes.get(key), 0))
                for key in ("added", "removed", "changed")
            } if isinstance(changes, dict) else {"added": 0, "removed": 0, "changed": 0}
            normalized_history.append(
                {
                    "at": max(0, safe_int(item.get("at"), 0)),
                    "status": status,
                    "duration_ms": max(0, safe_int(item.get("duration_ms"), 0)),
                    "changes": normalized_changes,
                    "error": str(item.get("error") or "")[:1000],
                }
            )
    subscription["refresh_history"] = normalized_history[-SUBSCRIPTION_REFRESH_HISTORY_LIMIT:]


def normalize_smart_gateway_settings(value: Any) -> dict[str, Any]:
    raw = value if isinstance(value, dict) else {}
    strategy = str(raw.get("strategy") or "health").strip().lower()
    if strategy not in SMART_GATEWAY_STRATEGIES:
        strategy = "health"
    port = safe_int(raw.get("port"), SMART_GATEWAY_DEFAULT_PORT)
    if not 1 <= port <= 65535:
        port = SMART_GATEWAY_DEFAULT_PORT
    return {
        "enabled": raw.get("enabled") is True,
        "port": port,
        "strategy": strategy,
        "region": str(raw.get("region") or "")[:128],
        "query": str(raw.get("query") or "")[:128],
        "max_connections": clamp_int(
            raw.get("max_connections"),
            SMART_GATEWAY_MAX_CLIENTS,
            1,
            min(SMART_GATEWAY_MAX_CLIENTS, PROXY_MAX_CLIENTS_LIMIT),
        ),
        "last_error": str(raw.get("last_error") or "")[:1000],
    }


def normalize_lan_cidrs(value: Any, *, strict: bool = False) -> list[str]:
    if not isinstance(value, list):
        if strict:
            raise ValueError("IP/CIDR 白名单必须是数组")
        value = list(DEFAULT_LAN_ALLOWED_CIDRS)
    if len(value) > LAN_ALLOWED_CIDR_LIMIT:
        raise ValueError(f"IP/CIDR 白名单最多 {LAN_ALLOWED_CIDR_LIMIT} 项")
    normalized: list[str] = []
    for raw in value:
        try:
            network = ipaddress.ip_network(str(raw or "").strip(), strict=False)
        except ValueError as exc:
            if strict:
                raise ValueError(f"IP/CIDR 白名单格式无效: {raw}") from exc
            continue
        if not isinstance(network, ipaddress.IPv4Network):
            if strict:
                raise ValueError("当前代理监听仅支持 IPv4 白名单")
            continue
        text = str(network)
        if text not in normalized:
            normalized.append(text)
    if not normalized:
        if strict:
            raise ValueError("IP/CIDR 白名单不能为空")
        return list(DEFAULT_LAN_ALLOWED_CIDRS)
    return normalized


def _validate_lan_secret(value: Any, label: str, *, required: bool = False) -> str:
    secret = str(value or "")
    encoded = secret.encode("utf-8")
    if required and not encoded:
        raise ValueError(f"{label}不能为空")
    if len(encoded) > LAN_SECRET_MAX_BYTES or "\x00" in secret:
        raise ValueError(f"{label}不能超过 {LAN_SECRET_MAX_BYTES} 字节")
    return secret


def normalize_lan_security(value: Any) -> dict[str, Any]:
    raw = value if isinstance(value, dict) else {}
    auth_mode = str(raw.get("auth_mode") or "none").lower()
    if auth_mode not in {"none", "basic", "token"}:
        auth_mode = "none"
    return {
        "auth_mode": auth_mode,
        "username": str(raw.get("username") or "")[:128],
        "password": str(raw.get("password") or ""),
        "token": str(raw.get("token") or ""),
        "allowed_cidrs": normalize_lan_cidrs(raw.get("allowed_cidrs")),
        "max_connections": clamp_int(raw.get("max_connections"), PROXY_MAX_CLIENTS, 1, PROXY_MAX_CLIENTS_LIMIT),
    }


def validate_lan_security(value: Any, current: dict[str, Any] | None = None) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("lan_security 必须是对象")
    previous = normalize_lan_security(current)
    auth_mode = str(value.get("auth_mode", previous["auth_mode"]) or "none").lower()
    if auth_mode not in {"none", "basic", "token"}:
        raise ValueError("认证方式只能是 none、basic 或 token")
    username = _validate_lan_secret(value.get("username", previous["username"]), "用户名")
    password_value = value.get("password")
    password = (
        previous["password"]
        if password_value is None or password_value == ""
        else _validate_lan_secret(password_value, "密码")
    )
    token_value = value.get("token")
    token = (
        previous["token"]
        if token_value is None or token_value == ""
        else _validate_lan_secret(token_value, "Token")
    )
    if auth_mode == "basic":
        username = _validate_lan_secret(username, "用户名", required=True)
        password = _validate_lan_secret(password, "密码", required=True)
        if ":" in username:
            raise ValueError("用户名不能包含冒号")
    if auth_mode == "token":
        token = _validate_lan_secret(token, "Token", required=True)
        if len(token) < 16:
            raise ValueError("Token 至少需要 16 个字符")
    max_connections = safe_int(value.get("max_connections", previous["max_connections"]), -1)
    if not 1 <= max_connections <= PROXY_MAX_CLIENTS_LIMIT:
        raise ValueError(f"连接数上限必须在 1..{PROXY_MAX_CLIENTS_LIMIT} 之间")
    return {
        "auth_mode": auth_mode,
        "username": username,
        "password": password,
        "token": token,
        "allowed_cidrs": normalize_lan_cidrs(
            value.get("allowed_cidrs", previous["allowed_cidrs"]),
            strict=True,
        ),
        "max_connections": max_connections,
    }


def public_lan_security(value: Any) -> dict[str, Any]:
    settings = normalize_lan_security(value)
    return {
        "auth_mode": settings["auth_mode"],
        "username": settings["username"],
        "password_configured": bool(settings["password"]),
        "token_configured": bool(settings["token"]),
        "socks_username": "token" if settings["auth_mode"] == "token" else settings["username"],
        "allowed_cidrs": settings["allowed_cidrs"],
        "max_connections": settings["max_connections"],
    }


def normalize_pool_health(value: Any) -> dict[str, Any]:
    raw = value if isinstance(value, dict) else {}
    latency = safe_int(raw.get("latency_ewma_ms"), 0)
    return {
        "consecutive_failures": max(0, safe_int(raw.get("consecutive_failures"), 0)),
        "last_success_at": max(0, safe_int(raw.get("last_success_at"), 0)),
        "last_failure_at": max(0, safe_int(raw.get("last_failure_at"), 0)),
        "latency_ewma_ms": latency if latency > 0 else None,
        "circuit_open_until": max(0, safe_int(raw.get("circuit_open_until"), 0)),
        "half_open_probe_until": max(0, safe_int(raw.get("half_open_probe_until"), 0)),
        "manual_excluded_until": max(0, safe_int(raw.get("manual_excluded_until"), 0)),
        "last_error": str(raw.get("last_error") or "")[:500],
    }


def stable_id(*parts: str) -> str:
    h = hashlib.sha256()
    for part in parts:
        h.update(part.encode("utf-8", "ignore"))
        h.update(b"\0")
    return h.hexdigest()[:16]


def decode_base64_text(value: str) -> str | None:
    compact = "".join(value.strip().split())
    if not compact:
        return None
    for candidate in (compact, compact.replace("-", "+").replace("_", "/")):
        padded = candidate + "=" * (-len(candidate) % 4)
        try:
            decoded = base64.b64decode(padded, validate=False)
            text = decoded.decode("utf-8")
        except Exception:
            continue
        if "://" in text or "proxies:" in text or text.lstrip().startswith(("{", "[")):
            return text
    return None


def decode_base64_any(value: str) -> str | None:
    compact = "".join(value.strip().split())
    if not compact:
        return None
    for candidate in (compact, compact.replace("-", "+").replace("_", "/")):
        padded = candidate + "=" * (-len(candidate) % 4)
        try:
            decoded = base64.b64decode(padded, validate=False)
            return decoded.decode("utf-8")
        except Exception:
            continue
    return None


def parse_query(query: str) -> dict[str, str]:
    parsed = urllib.parse.parse_qs(query, keep_blank_values=True)
    return {key.lower(): values[-1] for key, values in parsed.items() if values}


REGION_CATALOG = {
    "HK": ("香港", "hong kong|hongkong|hkg", "Asia/Hong_Kong", "zh-HK"),
    "JP": ("日本", "japan|tokyo|osaka|jpn", "Asia/Tokyo", "ja-JP"),
    "SG": ("新加坡", "singapore|sin", "Asia/Singapore", "en-SG"),
    "TW": ("台湾", "taiwan|twn", "Asia/Taipei", "zh-TW"),
    "KR": ("韩国", "korea|seoul|kor", "Asia/Seoul", "ko-KR"),
    "US": ("美国", "united states|america|los angeles|san jose|usa", "America/Los_Angeles", "en-US"),
    "GB": ("英国", "united kingdom|london|uk", "Europe/London", "en-GB"),
    "DE": ("德国", "germany|frankfurt|deu", "Europe/Berlin", "de-DE"),
    "FR": ("法国", "france|paris|fra", "Europe/Paris", "fr-FR"),
    "CA": ("加拿大", "canada|toronto", "America/Toronto", "en-CA"),
    "AU": ("澳大利亚", "australia|sydney|aus|澳洲", "Australia/Sydney", "en-AU"),
    "NL": ("荷兰", "netherlands|amsterdam", "Europe/Amsterdam", "nl-NL"),
    "PH": ("菲律宾", "philippines|manila", "Asia/Manila", "en-PH"),
    "ID": ("印度尼西亚", "indonesia|jakarta|印尼", "Asia/Jakarta", "id-ID"),
    "IN": ("印度", "india|mumbai", "Asia/Kolkata", "en-IN"),
    "TR": ("土耳其", "turkey|turkiye|istanbul", "Europe/Istanbul", "tr-TR"),
    "RU": ("俄罗斯", "russia|moscow", "Europe/Moscow", "ru-RU"),
    "VN": ("越南", "vietnam|hanoi", "Asia/Ho_Chi_Minh", "vi-VN"),
    "TH": ("泰国", "thailand|bangkok", "Asia/Bangkok", "th-TH"),
    "MY": ("马来西亚", "malaysia|kuala lumpur", "Asia/Kuala_Lumpur", "ms-MY"),
    "BR": ("巴西", "brazil|sao paulo", "America/Sao_Paulo", "pt-BR"),
    "AR": ("阿根廷", "argentina|buenos aires", "America/Argentina/Buenos_Aires", "es-AR"),
    "MX": ("墨西哥", "mexico", "America/Mexico_City", "es-MX"),
    "CL": ("智利", "chile|santiago", "America/Santiago", "es-CL"),
    "ES": ("西班牙", "spain|madrid", "Europe/Madrid", "es-ES"),
    "IT": ("意大利", "italy|milan|rome", "Europe/Rome", "it-IT"),
    "CH": ("瑞士", "switzerland|zurich", "Europe/Zurich", "de-CH"),
    "SE": ("瑞典", "sweden|stockholm", "Europe/Stockholm", "sv-SE"),
    "NO": ("挪威", "norway|oslo", "Europe/Oslo", "nb-NO"),
    "FI": ("芬兰", "finland|helsinki", "Europe/Helsinki", "fi-FI"),
    "DK": ("丹麦", "denmark|copenhagen", "Europe/Copenhagen", "da-DK"),
    "PL": ("波兰", "poland|warsaw", "Europe/Warsaw", "pl-PL"),
    "CZ": ("捷克", "czech|prague", "Europe/Prague", "cs-CZ"),
    "AT": ("奥地利", "austria|vienna", "Europe/Vienna", "de-AT"),
    "BE": ("比利时", "belgium|brussels", "Europe/Brussels", "nl-BE"),
    "IE": ("爱尔兰", "ireland|dublin", "Europe/Dublin", "en-IE"),
    "PT": ("葡萄牙", "portugal|lisbon", "Europe/Lisbon", "pt-PT"),
    "NZ": ("新西兰", "new zealand|auckland", "Pacific/Auckland", "en-NZ"),
    "AE": ("阿联酋", "united arab emirates|dubai|uae", "Asia/Dubai", "ar-AE"),
    "IL": ("以色列", "israel|tel aviv", "Asia/Jerusalem", "he-IL"),
    "ZA": ("南非", "south africa|johannesburg", "Africa/Johannesburg", "en-ZA"),
    "UA": ("乌克兰", "ukraine|kyiv|kiev", "Europe/Kyiv", "uk-UA"),
    "HU": ("匈牙利", "hungary|budapest", "Europe/Budapest", "hu-HU"),
    "RO": ("罗马尼亚", "romania|bucharest", "Europe/Bucharest", "ro-RO"),
    "BG": ("保加利亚", "bulgaria|sofia", "Europe/Sofia", "bg-BG"),
    "GR": ("希腊", "greece|athens", "Europe/Athens", "el-GR"),
    "IS": ("冰岛", "iceland|reykjavik", "Atlantic/Reykjavik", "is-IS"),
    "LU": ("卢森堡", "luxembourg", "Europe/Luxembourg", "fr-LU"),
    "MO": ("澳门", "macau|macao", "Asia/Macau", "zh-MO"),
    "CN": ("中国", "china|中国大陆", "Asia/Shanghai", "zh-CN"),
    "PK": ("巴基斯坦", "pakistan", "Asia/Karachi", "ur-PK"),
    "BD": ("孟加拉国", "bangladesh|孟加拉", "Asia/Dhaka", "bn-BD"),
    "KZ": ("哈萨克斯坦", "kazakhstan", "Asia/Almaty", "kk-KZ"),
    "SA": ("沙特阿拉伯", "saudi arabia|沙特", "Asia/Riyadh", "ar-SA"),
    "EG": ("埃及", "egypt|cairo", "Africa/Cairo", "ar-EG"),
}


def node_region(name: str, server: str = "") -> str:
    haystack = f"{name} {server}".lower()
    # 先匹配完整名称，避免“印度尼西亚”被“印度”或“澳大利亚”被短代码误判。
    for code, (region, aliases, _timezone, _language) in REGION_CATALOG.items():
        for term in (region, *aliases.split("|")):
            if (not term.isascii() and term in haystack) or re.search(
                rf"(?<![a-z]){re.escape(term)}(?![a-z])", haystack
            ):
                return region
    for code, (region, *_rest) in REGION_CATALOG.items():
        flag = "".join(chr(0x1F1E6 + ord(letter) - ord("A")) for letter in code)
        if flag in haystack or re.search(rf"(?<![a-z]){code.lower()}(?![a-z])", haystack):
            return region
    return "未分类"


def node_region_code(region: Any) -> str:
    value = str(region or "")
    return next((code for code, item in REGION_CATALOG.items() if item[0] == value), "ZZ")


# 地区 -> (时区, 主语言, Accept-Language)。用于让浏览器指纹的时区/语言与该节点
# SOCKS5 代理的出口地区保持一致,避免"IP 在美国、浏览器时区在中国"这类不一致被风控识别。
REGION_LOCALE_MAP: dict[str, tuple[str, str, str]] = {
    "香港": ("Asia/Hong_Kong", "zh-HK", "zh-HK,zh;q=0.9,en;q=0.8"),
    "日本": ("Asia/Tokyo", "ja-JP", "ja-JP,ja;q=0.9,en;q=0.8"),
    "新加坡": ("Asia/Singapore", "en-SG", "en-SG,en;q=0.9,zh;q=0.8"),
    "台湾": ("Asia/Taipei", "zh-TW", "zh-TW,zh;q=0.9,en;q=0.8"),
    "韩国": ("Asia/Seoul", "ko-KR", "ko-KR,ko;q=0.9,en;q=0.8"),
    "美国": ("America/Los_Angeles", "en-US", "en-US,en;q=0.9"),
    "英国": ("Europe/London", "en-GB", "en-GB,en;q=0.9"),
    "德国": ("Europe/Berlin", "de-DE", "de-DE,de;q=0.9,en;q=0.8"),
    "法国": ("Europe/Paris", "fr-FR", "fr-FR,fr;q=0.9,en;q=0.8"),
    "加拿大": ("America/Toronto", "en-CA", "en-CA,en;q=0.9,fr;q=0.8"),
    "澳大利亚": ("Australia/Sydney", "en-AU", "en-AU,en;q=0.9"),
    "荷兰": ("Europe/Amsterdam", "nl-NL", "nl-NL,nl;q=0.9,en;q=0.8"),
}
# 无法识别地区时的兜底 locale。
DEFAULT_LOCALE = ("America/Los_Angeles", "en-US", "en-US,en;q=0.9")
for _region, _aliases, _timezone, _language in REGION_CATALOG.values():
    REGION_LOCALE_MAP.setdefault(_region, (_timezone, _language, f"{_language},en;q=0.8"))
GOOGLE_SEARCH_PROVIDER = {
    "enabled": True,
    "name": "Google",
    "keyword": "google.com",
    "search_url": "https://www.google.com/search?q={searchTerms}",
    "suggest_url": "https://www.google.com/complete/search?output=chrome&q={searchTerms}",
    "icon_url": "https://www.google.com/favicon.ico",
    "encoding": "UTF-8",
}
GOOGLE_SEARCH_EXTENSION_MANIFEST = {
    "manifest_version": 3,
    "name": "ProxyBridge Google Search",
    "version": "1.0.0",
    "chrome_settings_overrides": {
        "search_provider": {
            "name": "Google",
            "keyword": "google.com",
            "search_url": "https://www.google.com/search?q={searchTerms}",
            "suggest_url": "https://www.google.com/complete/search?output=chrome&q={searchTerms}",
            "favicon_url": "https://www.google.com/favicon.ico",
            "encoding": "UTF-8",
            "is_default": True,
        }
    },
}


def region_locale(region: str) -> tuple[str, str, str]:
    # 根据节点地区返回 (时区, 语言, Accept-Language),用于指纹内核的 --timezone/--lang/--accept-lang。
    return REGION_LOCALE_MAP.get(region or "", DEFAULT_LOCALE)


def fingerprint_seed(node_id: str) -> int:
    # 从节点 ID 确定性派生 32 位指纹种子:同一节点每次启动得到完全相同的指纹(可恢复环境),
    # 不同节点得到互不相同的指纹(避免被网站关联为同一设备)。取正数并避开 0(0 在部分构建中表示关闭)。
    digest = hashlib.sha256(f"fp-seed::{node_id}".encode("utf-8")).digest()
    seed = int.from_bytes(digest[:4], "big") & 0x7FFFFFFF
    return seed or 1


def _read_profile_preferences(profile_path: Path) -> tuple[Path, dict[str, Any]]:
    default_dir = profile_path / "Default"
    default_dir.mkdir(parents=True, exist_ok=True)
    prefs_path = default_dir / "Preferences"
    prefs: dict[str, Any] = {}
    if prefs_path.exists():
        try:
            with prefs_path.open("r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                prefs = loaded
        except Exception:
            prefs = {}
    return prefs_path, prefs


def _write_profile_preferences(prefs_path: Path, prefs: dict[str, Any]) -> None:
    tmp = prefs_path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(prefs, f, ensure_ascii=False, indent=2)
    tmp.replace(prefs_path)


def configure_browser_profile_defaults(profile_path: Path) -> None:
    prefs_path, prefs = _read_profile_preferences(profile_path)
    prefs["default_search_provider"] = dict(GOOGLE_SEARCH_PROVIDER)
    _write_profile_preferences(prefs_path, prefs)


def apply_browser_profile_preferences(profile_path: Path, config: dict[str, Any]) -> None:
    """写入记录级 Profile 偏好；目前是地理位置默认权限。"""
    prefs_path, prefs = _read_profile_preferences(profile_path)
    before = json.dumps(prefs, sort_keys=True)
    # 地理位置默认权限：block 时写入禁止，避免系统/Wi-Fi 定位绕过代理暴露真实位置。
    browser_fingerprint.apply_geolocation_preference(prefs, str(config.get("geolocation") or "ask"))
    if json.dumps(prefs, sort_keys=True) != before:
        _write_profile_preferences(prefs_path, prefs)


def ensure_google_search_extension() -> Path:
    extension_dir = browser_extensions_root() / "google_search"
    extension_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = extension_dir / "manifest.json"
    tmp = manifest_path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(GOOGLE_SEARCH_EXTENSION_MANIFEST, f, ensure_ascii=False, indent=2)
    tmp.replace(manifest_path)
    return extension_dir


# 扩展库：上传的扩展解压到 browser_extensions/library/<扩展 ID>/，ID 取安装包 SHA-256 前 16 位。
# 启动时把选中的扩展复制到 Profile 内 ProxyBridgeExtensions/<扩展 ID>/，随 Profile 一起同步，
# Docker worker 也能通过已挂载的 Profile 路径加载。
BROWSER_EXTENSION_METADATA_FILE = ".proxybridge-extension.json"
PROFILE_EXTENSIONS_DIR = "ProxyBridgeExtensions"


def browser_extension_library_root() -> Path:
    return browser_extensions_root() / "library"


def browser_extension_library() -> list[dict[str, Any]]:
    root = browser_extension_library_root()
    if not root.is_dir():
        return []
    items: list[dict[str, Any]] = []
    for entry in root.iterdir():
        if not entry.is_dir() or browser_fingerprint.EXTENSION_ID_PATTERN.fullmatch(entry.name) is None:
            continue
        if not (entry / "manifest.json").is_file():
            continue
        try:
            metadata = json.loads((entry / BROWSER_EXTENSION_METADATA_FILE).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        items.append(
            {
                "id": entry.name,
                "name": str(metadata.get("name") or entry.name)[:200],
                "version": str(metadata.get("version") or "")[:64],
                "filename": str(metadata.get("filename") or "")[:180],
                "installed_at": safe_int(metadata.get("installed_at"), 0) or None,
            }
        )
    return sorted(items, key=lambda item: (item["name"].lower(), item["id"]))


def install_browser_extension_package(package_path: Path, filename: str, sha256: str) -> dict[str, Any]:
    """校验并解压 .zip/.crx 扩展包到扩展库；同一安装包重复上传时直接返回已有条目。"""
    require_browser_feature()
    extension_id = sha256[:16]
    library_root = browser_extension_library_root()
    target = library_root / extension_id
    with BROWSER_EXTENSION_LOCK:
        if (target / "manifest.json").is_file():
            existing = next((item for item in browser_extension_library() if item["id"] == extension_id), None)
            return {"ok": True, "already_installed": True, "extension": existing}
        library_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        staging = library_root / f".install-{secrets.token_hex(6)}"
        staging.mkdir(mode=0o700)
        try:
            with package_path.open("rb") as handle:
                header = handle.read(16)
                offset = browser_fingerprint.crx_zip_offset(header)
                zip_path = staging / "package.zip"
                handle.seek(offset)
                with zip_path.open("wb") as output:
                    shutil.copyfileobj(handle, output, 1024 * 1024)
            extract_root = staging / "extract"
            extract_root.mkdir()
            try:
                _safe_extract_zip(zip_path, extract_root)
            except zipfile.BadZipFile as exc:
                raise ValueError("扩展包不是有效的 zip 或 crx 文件") from exc
            zip_path.unlink(missing_ok=True)
            extension_root = extract_root
            if not (extension_root / "manifest.json").is_file():
                children = [child for child in extract_root.iterdir() if not child.name.startswith("__MACOSX")]
                if len(children) == 1 and (children[0] / "manifest.json").is_file():
                    extension_root = children[0]
            # macOS 压缩包会附带 __MACOSX/；Chromium 拒绝加载含下划线开头目录的解压扩展。
            shutil.rmtree(extension_root / "__MACOSX", ignore_errors=True)
            manifest = browser_fingerprint.read_extension_manifest(extension_root)
            metadata = {
                "id": extension_id,
                "name": manifest["name"],
                "version": manifest["version"],
                "filename": filename,
                "sha256": sha256,
                "installed_at": now_ts(),
            }
            (extension_root / BROWSER_EXTENSION_METADATA_FILE).write_text(
                json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            extension_root.replace(target)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
    return {
        "ok": True,
        "already_installed": False,
        "extension": next((item for item in browser_extension_library() if item["id"] == extension_id), None),
    }


def delete_browser_extension(payload: dict[str, Any]) -> dict[str, Any]:
    """从扩展库删除扩展，并从所有浏览器记录的扩展列表中移除；Profile 内副本在下次启动时清理。"""
    require_browser_feature()
    extension_id = str(payload.get("id") or "").strip().lower()
    if browser_fingerprint.EXTENSION_ID_PATTERN.fullmatch(extension_id) is None:
        raise ValueError("扩展 ID 无效")
    with BROWSER_EXTENSION_LOCK:
        target = browser_extension_library_root() / extension_id
        if not target.is_dir():
            raise ValueError("扩展不存在")
        shutil.rmtree(target)
    changed = False
    with STATE_LOCK:
        state = load_state()
        for instance in state.get("browser_instances", []):
            if extension_id in (instance.get("extensions") or []):
                instance["extensions"] = [item for item in instance["extensions"] if item != extension_id]
                changed = True
        if changed:
            save_state(state)
    if changed:
        _push_browser_change("删除浏览器扩展")
    return {"ok": True, "deleted": extension_id, "extensions": browser_extension_library()}


def sync_profile_extensions(profile_path: Path, extension_ids: list[str]) -> list[str]:
    """把选中的扩展同步到 Profile 内并返回可加载的扩展 ID；库中缺失但 Profile 已有副本时继续使用副本。"""
    profile_root = profile_path / PROFILE_EXTENSIONS_DIR
    library_root = browser_extension_library_root()
    selected = [item for item in extension_ids if browser_fingerprint.EXTENSION_ID_PATTERN.fullmatch(item)]
    usable: list[str] = []
    with BROWSER_EXTENSION_LOCK:
        if profile_root.is_dir():
            for child in profile_root.iterdir():
                if child.name not in selected:
                    if child.is_dir() and not child.is_symlink():
                        shutil.rmtree(child, ignore_errors=True)
                    else:
                        child.unlink(missing_ok=True)
        for extension_id in selected:
            source = library_root / extension_id
            destination = profile_root / extension_id
            if (source / "manifest.json").is_file():
                if not (destination / BROWSER_EXTENSION_METADATA_FILE).is_file():
                    profile_root.mkdir(mode=0o700, parents=True, exist_ok=True)
                    staged = profile_root / f".{extension_id}.copy-{secrets.token_hex(4)}"
                    try:
                        shutil.copytree(source, staged, symlinks=False)
                        if destination.exists():
                            shutil.rmtree(destination, ignore_errors=True)
                        staged.replace(destination)
                    finally:
                        shutil.rmtree(staged, ignore_errors=True)
            if (destination / "manifest.json").is_file():
                usable.append(extension_id)
            else:
                log_event(f"扩展 {extension_id} 已不在扩展库中，启动时跳过", level="warn", category="浏览器")
    return usable


def browser_stop_on_last_window_enabled() -> bool:
    """macOS 默认开启「关闭最后一个窗口即停止浏览器」。

    环境变量 PROXY_BRIDGE_BROWSER_STOP_ON_LAST_WINDOW 设为 0 / false / off 时关闭；
    Windows 与 Linux 上 Chromium 关闭最后一个窗口本身就会退出，因此不加载窗口助手。
    """
    if host_os_token() != "darwin":
        return False
    value = str(os.environ.get("PROXY_BRIDGE_BROWSER_STOP_ON_LAST_WINDOW", "1")).strip().lower()
    return value not in {"0", "false", "no", "off"}


def browser_window_closed_endpoint() -> str | None:
    """返回浏览器可直连的控制台回调地址；控制台只监听局域网地址时浏览器流量会走代理，此时不启用。"""
    host = str(os.environ.get("PROXY_BRIDGE_HOST") or "127.0.0.1").strip().lower()
    port = safe_int(os.environ.get("PROXY_BRIDGE_PORT"), DEFAULT_SERVER_PORT)
    if not 1 <= port <= 65535:
        return None
    if host in {"127.0.0.1", "localhost", "0.0.0.0", ""}:
        return f"http://127.0.0.1:{port}{BROWSER_WINDOW_CLOSED_PATH}"
    if host in {"::1", "::"}:
        return f"http://[::1]:{port}{BROWSER_WINDOW_CLOSED_PATH}"
    return None


BROWSER_WINDOW_HELPER_SCRIPT = """// 由 ProxyBridge 在每次启动时生成，请勿修改。
// macOS 上关闭最后一个窗口后 Chromium 仍会驻留在 Dock；窗口全部关闭时通知 ProxyBridge 停止此浏览器。
const ENDPOINT = __ENDPOINT__;
const TOKEN = __TOKEN__;
const WINDOW_TYPES = ["normal", "popup", "panel", "app", "devtools"];
let pending = null;

async function notifyIfNoWindows() {
  pending = null;
  const windows = await chrome.windows.getAll({ windowTypes: WINDOW_TYPES });
  if (windows.length) return;
  try {
    await fetch(ENDPOINT, { method: "POST", headers: { "Content-Type": "text/plain" }, body: TOKEN, cache: "no-store", credentials: "omit" });
  } catch (_) {
    // 控制台未运行时忽略；浏览器保持原样。
  }
}

chrome.windows.onRemoved.addListener(() => {
  if (pending) clearTimeout(pending);
  // 稍等片刻再确认，避免拖出标签页等瞬时无窗口状态被误判。
  pending = setTimeout(notifyIfNoWindows, 1500);
});
"""


def prepare_browser_window_helper(instance_id: str, token: str, endpoint: str) -> Path:
    """写入该记录专用的窗口助手扩展（位于工作目录，不进入 Profile，也不参与同步）。"""
    root = browser_extensions_root() / "window_helper" / stable_id("window-helper", instance_id)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    hostname = str(urllib.parse.urlsplit(endpoint).hostname or "127.0.0.1")
    manifest = {
        "manifest_version": 3,
        "name": "ProxyBridge Window Helper",
        "description": "关闭最后一个窗口后通知 ProxyBridge 停止此浏览器。",
        "version": "1.0.0",
        "background": {"service_worker": "background.js"},
        # 只需访问本机控制台；不声明内容脚本与网页可访问资源，网页无法探测到该扩展。
        "host_permissions": [f"http://{'[::1]' if hostname == '::1' else hostname}/*"],
    }
    script = BROWSER_WINDOW_HELPER_SCRIPT.replace("__ENDPOINT__", json.dumps(endpoint)).replace(
        "__TOKEN__", json.dumps(token)
    )
    for name, content in (("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2)), ("background.js", script)):
        target = root / name
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(target)
    return root


def register_browser_window_token(token: str, instance_id: str, process: subprocess.Popen) -> None:
    """登记一次性令牌；调用方需持有 PROCESS_LOCK。同一记录的旧令牌与已退出进程的令牌一并清理。"""
    for existing, (owner, owner_process) in list(BROWSER_WINDOW_TOKENS.items()):
        if owner == instance_id or owner_process.poll() is not None:
            BROWSER_WINDOW_TOKENS.pop(existing, None)
    BROWSER_WINDOW_TOKENS[token] = (instance_id, process)


def handle_browser_last_window_closed(token: str) -> bool:
    """窗口助手回调：令牌有效且对应进程仍在运行时，在后台停止该浏览器。"""
    token = str(token or "").strip()
    if not token or len(token) > 128:
        return False
    with PROCESS_LOCK:
        match: tuple[str, str, subprocess.Popen] | None = None
        for candidate, (instance_id, process) in BROWSER_WINDOW_TOKENS.items():
            if hmac.compare_digest(candidate.encode("utf-8"), token.encode("utf-8")):
                match = (candidate, instance_id, process)
        if match is None:
            return False
        candidate, instance_id, process = match
        BROWSER_WINDOW_TOKENS.pop(candidate, None)
        if BROWSER_PROCESS_TABLE.get(instance_id) is not process or process.poll() is not None:
            return False
    threading.Thread(
        target=_stop_browser_after_last_window,
        args=(instance_id, process),
        name="browser-last-window",
        daemon=True,
    ).start()
    return True


def _stop_browser_after_last_window(instance_id: str, process: subprocess.Popen) -> None:
    if SHUTTING_DOWN.is_set():
        return
    with PROCESS_LOCK:
        if BROWSER_PROCESS_TABLE.get(instance_id) is not process or process.poll() is not None:
            return
    try:
        with STATE_LOCK:
            name = str(find_browser_instance(load_state(), instance_id).get("name") or "浏览器")
        # 与界面上的「停止」一致：结束浏览器进程组、保留本地代理，并按设置自动上传 Profile。
        stop_browser({"instance_id": instance_id})
        log_event(f"「{name}」的窗口已全部关闭，已自动停止浏览器", category="浏览器")
    except Exception as exc:
        log_event(f"窗口关闭后自动停止浏览器失败：{exc}", level="warn", category="浏览器")


def browser_node_profile_root(node_id: str) -> Path:
    return browser_profiles_root() / stable_id(node_id)


def legacy_browser_instance_id(node_id: str) -> str:
    return stable_id("browser-instance", "legacy", node_id)


def new_browser_instance_id(node_id: str) -> str:
    # Windows 的 time_ns() 精度只有毫秒级，连续新建会得到相同时间戳；加入随机数保证唯一。
    return stable_id("browser-instance", node_id, str(time.time_ns()), secrets.token_hex(8))


def browser_instance_seed(node_id: str, instance_id: str, legacy: bool = False) -> int:
    return fingerprint_seed(node_id if legacy else f"{node_id}:{instance_id}")


def browser_instance_profile_path(node_id: str, instance_id: str, legacy: bool = False) -> Path:
    if legacy:
        return browser_node_profile_root(node_id)
    return browser_node_profile_root(node_id) / instance_id


def is_legacy_browser_profile(profile_path: Path) -> bool:
    # 旧版本把 Default 目录直接放在节点目录下;新版本的 Default 位于实例子目录内。
    return (profile_path / "Default").exists() or (profile_path / "Local State").exists()


def browser_instances_for_node(state: dict[str, Any], node_id: str) -> list[dict[str, Any]]:
    return sorted(
        [
            item
            for item in state.get("browser_instances", [])
            if str(item.get("node_id") or "") == node_id
        ],
        key=browser_instance_sort_key,
        reverse=True,
    )


def browser_instance_last_used_at(instance: dict[str, Any]) -> int:
    """返回可迁移的最近使用时间：启动、停止、创建时间取最新值。"""
    return max(
        safe_int(instance.get("last_used_at"), 0),
        safe_int(instance.get("last_started_at"), 0),
        safe_int(instance.get("last_stopped_at"), 0),
        safe_int(instance.get("created_at"), 0),
    )


def browser_instance_sort_key(instance: dict[str, Any]) -> tuple[int, int, str]:
    return (
        browser_instance_last_used_at(instance),
        safe_int(instance.get("created_at"), 0),
        str(instance.get("id") or ""),
    )


def public_browser_instances(state: dict[str, Any]) -> list[dict[str, Any]]:
    """状态 API 按最近使用时间倒序返回记录，不改变持久化与同步顺序。"""
    records = [dict(item) for item in state.get("browser_instances", []) if isinstance(item, dict)]
    return sorted(records, key=browser_instance_sort_key, reverse=True)


def next_browser_instance_name(state: dict[str, Any], node_id: str) -> str:
    numbers: list[int] = []
    count = 0
    for item in browser_instances_for_node(state, node_id):
        if item.get("legacy"):
            continue
        count += 1
        match = re.fullmatch(r"浏览器\s+(\d+)", str(item.get("name") or "").strip())
        if match:
            numbers.append(safe_int(match.group(1), 0))
    return f"浏览器 {max(numbers, default=count) + 1}"


def normalize_browser_label(value: Any) -> str:
    """Free-text tag/note for a browser instance (e.g. 访问 xx 的浏览器)."""
    text = re.sub(r"\s+", " ", str(value or "").strip())
    if len(text) > 120:
        text = text[:120].rstrip()
    return text


def default_browser_instance_options() -> dict[str, Any]:
    """新建记录的可编辑字段默认值；旧记录缺少这些字段时由 normalize 补成升级前的等效值。"""
    return {
        # 自由分组名，用于浏览器页筛选与批量操作，最长 40 字。
        "group": "",
        # 指纹参数，字段含义见 browser_fingerprint.FINGERPRINT_FIELDS；macOS 上默认语言跟随系统、
        # 关闭 Canvas 与字体伪装，避免被检测站判定为 masking。
        "fingerprint": browser_fingerprint.recommended_fingerprint_config(fingerprint_platform_token()),
        # 固定内核版本 ID；空字符串表示跟随「内核管理」中的默认版本。
        "kernel_version": "",
        # 是否开放本机 CDP 调试端口（127.0.0.1 随机端口），用于 Cookie 导入导出与自动化。
        "cdp_enabled": False,
        # 扩展库中的扩展 ID 列表，启动时复制到 Profile 内并加载。
        "extensions": [],
        # 每次启动打开的网址；为空时沿用首次打开出口检测页、之后打开空白页的旧行为。
        "start_urls": [],
    }


def create_browser_instance_record(
    state: dict[str, Any],
    node_id: str,
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    existing_ids = {str(item.get("id") or "") for item in state.get("browser_instances", [])}
    instance_id = new_browser_instance_id(node_id)
    while instance_id in existing_ids:
        instance_id = new_browser_instance_id(node_id)
    profile_path = browser_instance_profile_path(node_id, instance_id)
    remote_worker = remote_browser_feature_enabled()
    created_at = now_ts()
    return {
        "id": instance_id,
        "node_id": node_id,
        "name": next_browser_instance_name(state, node_id),
        "label": "",
        "status": "stopped",
        "profile_path": str(profile_path),
        "fingerprint_seed": browser_instance_seed(node_id, instance_id),
        "created_at": created_at,
        "last_started_at": None,
        "last_stopped_at": None,
        "last_used_at": created_at,
        "pid": None,
        "bridge_id": None,
        "proxy": "",
        "launch_count": 0,
        "legacy": False,
        "remote_worker": remote_worker,
        "remote_url": "",
        "remote_container_id": "",
        "remote_host_port": None,
        "remote_username": "",
        # Chromium Profile 在桌面进程与 Docker worker 间保持同一布局；Selkies
        # TLS/认证运行文件由 worker 匿名卷承载，不写入这个同步目录。
        "profile_layout": "native-v1",
        **default_browser_instance_options(),
        **(options or {}),
    }


def find_browser_instance(state: dict[str, Any], instance_id: str) -> dict[str, Any]:
    for instance in state.get("browser_instances", []):
        if str(instance.get("id") or "") == instance_id:
            return instance
    raise ValueError("找不到浏览器记录")


def normalize_browser_instances(state: dict[str, Any]) -> None:
    instances = state.get("browser_instances")
    if not isinstance(instances, list):
        instances = []
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    next_numbers: dict[str, int] = {}
    for raw in instances:
        if not isinstance(raw, dict):
            continue
        node_id = str(raw.get("node_id") or "")
        if not node_id:
            continue
        legacy = bool(raw.get("legacy"))
        instance_id = str(raw.get("id") or "") or (
            legacy_browser_instance_id(node_id) if legacy else new_browser_instance_id(node_id)
        )
        if instance_id in seen:
            continue
        seen.add(instance_id)
        record = dict(raw)
        if not record.get("name"):
            if legacy:
                record["name"] = "默认浏览器"
            else:
                number = next_numbers.get(node_id, 1)
                record["name"] = f"浏览器 {number}"
                next_numbers[node_id] = number + 1
        status = str(record.get("status") or "stopped")
        if status not in ACTIVE_BROWSER_STATUSES | {"stopped", "failed"}:
            status = "stopped"
        remote_worker = record.get("remote_worker") is True
        remote_url = (
            normalize_remote_browser_url(record.get("remote_url"))
            if remote_worker and status in ACTIVE_BROWSER_STATUSES
            else ""
        )
        remote_host_port = safe_int(record.get("remote_host_port"), 0)
        if (
            not remote_worker
            or status not in ACTIVE_BROWSER_STATUSES
            or not 1024 <= remote_host_port <= 65535
        ):
            remote_host_port = 0
        remote_username = str(record.get("remote_username") or "")
        if REMOTE_BROWSER_USERNAME_PATTERN.fullmatch(remote_username) is None:
            remote_username = ""
        profile_layout = str(record.get("profile_layout") or "native-v1")
        if profile_layout not in {"native-v1", "remote-worker-v1"}:
            profile_layout = "native-v1"
        created_at = safe_int(record.get("created_at"), now_ts())
        last_started_at = safe_int(record.get("last_started_at"), 0) or None
        last_stopped_at = safe_int(record.get("last_stopped_at"), 0) or None
        last_used_at = max(
            safe_int(record.get("last_used_at"), 0),
            safe_int(last_started_at, 0),
            safe_int(last_stopped_at, 0),
            created_at,
        )
        # Prefer explicit label; accept legacy "tag" key if present.
        label = normalize_browser_label(record.get("label") if "label" in record else record.get("tag"))
        # 缺少 fingerprint 字段的是升级前创建的记录：按旧启动参数补齐，避免指纹悄然变化。
        fingerprint_fallback = (
            browser_fingerprint.legacy_fingerprint_config()
            if not isinstance(raw.get("fingerprint"), dict)
            else browser_fingerprint.recommended_fingerprint_config(fingerprint_platform_token())
        )
        active = status in ACTIVE_BROWSER_STATUSES
        cdp_port = safe_int(record.get("cdp_port"), 0)
        cdp_ws_path = str(record.get("cdp_ws_path") or "")
        if (
            not active
            or remote_worker
            or not 1024 <= cdp_port <= 65535
            or re.fullmatch(r"/devtools/browser/[A-Za-z0-9-]{8,64}", cdp_ws_path) is None
        ):
            cdp_port, cdp_ws_path = 0, ""
        record.update(
            {
                "group": browser_fingerprint.normalize_group(record.get("group")),
                "fingerprint": browser_fingerprint.normalize_fingerprint_config(
                    record.get("fingerprint"), fingerprint_fallback
                ),
                "kernel_version": browser_fingerprint.normalize_kernel_id(
                    record.get("kernel_version"), strict=False
                ),
                "cdp_enabled": record.get("cdp_enabled") is True,
                "extensions": browser_fingerprint.normalize_extension_ids(record.get("extensions"), strict=False),
                "start_urls": browser_fingerprint.normalize_start_urls(record.get("start_urls"), strict=False),
                # 以下为运行期信息：调试端口、实际内核、生效的时区语言，停止后清空。
                "cdp_port": cdp_port or None,
                "cdp_ws_path": cdp_ws_path,
                "runtime_kernel": str(record.get("runtime_kernel") or "") if active else "",
                "runtime_locale": record.get("runtime_locale") if active and isinstance(record.get("runtime_locale"), dict) else {},
            }
        )
        record.update(
            {
                "id": instance_id,
                "node_id": node_id,
                "label": label,
                "status": status,
                "profile_path": str(
                    record.get("profile_path") or browser_instance_profile_path(node_id, instance_id, legacy)
                ),
                "fingerprint_seed": safe_int(
                    record.get("fingerprint_seed"), browser_instance_seed(node_id, instance_id, legacy)
                ),
                "created_at": created_at,
                "last_started_at": last_started_at,
                "last_stopped_at": last_stopped_at,
                "last_used_at": last_used_at,
                "pid": safe_int(record.get("pid"), 0) or None,
                "bridge_id": record.get("bridge_id"),
                "proxy": str(record.get("proxy") or ""),
                "launch_count": safe_int(record.get("launch_count"), 0),
                "legacy": legacy,
                "remote_worker": remote_worker,
                "remote_url": remote_url,
                "remote_container_id": (
                    normalize_remote_browser_container_id(record.get("remote_container_id"))
                    if remote_worker and status in ACTIVE_BROWSER_STATUSES
                    else ""
                ),
                "remote_host_port": remote_host_port or None,
                "remote_username": remote_username,
                "profile_layout": profile_layout,
            }
        )
        record.pop("tag", None)
        normalized.append(record)
    state["browser_instances"] = normalized
    ensure_legacy_browser_instances(state)


def validate_browser_options_patch(
    payload: dict[str, Any],
    current: dict[str, Any],
    *,
    batch: bool = False,
) -> dict[str, Any]:
    """严格校验浏览器记录的可编辑字段，只返回本次提交涉及的字段。"""
    patch: dict[str, Any] = {}
    if "label" in payload or "tag" in payload:
        patch["label"] = normalize_browser_label(payload.get("label") if "label" in payload else payload.get("tag"))
    if "name" in payload:
        if batch:
            raise ValueError("批量编辑不能修改名称")
        name = browser_fingerprint.normalize_name(payload.get("name"))
        if not name:
            raise ValueError("名称不能为空")
        patch["name"] = name
    if "group" in payload:
        patch["group"] = browser_fingerprint.normalize_group(payload.get("group"))
    if "fingerprint" in payload:
        patch["fingerprint"] = browser_fingerprint.validate_fingerprint_patch(
            payload.get("fingerprint"),
            base=current.get("fingerprint") if isinstance(current.get("fingerprint"), dict) else None,
        )
    if "kernel_version" in payload:
        kernel_id = browser_fingerprint.normalize_kernel_id(payload.get("kernel_version"), strict=True)
        if kernel_id and kernel_id != current.get("kernel_version") and kernel_id not in fingerprint_kernel_ids():
            raise ValueError(f"指纹内核版本 {kernel_id} 未安装")
        patch["kernel_version"] = kernel_id
    if "cdp_enabled" in payload:
        if not isinstance(payload.get("cdp_enabled"), bool):
            raise ValueError("cdp_enabled 必须是布尔值")
        if payload["cdp_enabled"] and remote_browser_feature_enabled():
            raise ValueError("Docker 远程浏览器暂不支持 CDP 调试端口")
        patch["cdp_enabled"] = payload["cdp_enabled"]
    if "extensions" in payload:
        extension_ids = browser_fingerprint.normalize_extension_ids(payload.get("extensions"), strict=True)
        known = {item["id"] for item in browser_extension_library()} | set(current.get("extensions") or [])
        missing = [item for item in extension_ids if item not in known]
        if missing:
            raise ValueError(f"扩展 {missing[0]} 不在扩展库中")
        patch["extensions"] = extension_ids
    if "start_urls" in payload:
        patch["start_urls"] = browser_fingerprint.normalize_start_urls(payload.get("start_urls"), strict=True)
    return patch


def _requested_browser_instance_ids(payload: dict[str, Any]) -> list[str]:
    raw_ids = payload.get("instance_ids")
    if raw_ids is None:
        single = str(payload.get("instance_id") or payload.get("id") or "")
        raw_ids = [single] if single else []
    if not isinstance(raw_ids, list) or not raw_ids:
        raise ValueError("缺少浏览器实例 ID")
    if len(raw_ids) > 500:
        raise ValueError("单次最多操作 500 条浏览器记录")
    result: list[str] = []
    for item in raw_ids:
        instance_id = str(item or "")
        if not instance_id:
            raise ValueError("浏览器实例 ID 无效")
        if instance_id not in result:
            result.append(instance_id)
    return result


def _browser_instance_live(instance: dict[str, Any]) -> bool:
    if instance.get("status") in ACTIVE_BROWSER_STATUSES:
        return True
    with PROCESS_LOCK:
        process = BROWSER_PROCESS_TABLE.get(str(instance.get("id") or ""))
        return process is not None and process.poll() is None


def _push_browser_change(reason: str) -> None:
    mark_sync_local_change()
    # WebDAV 已启用时尽快上传，其他设备可及时看到配置变化。
    try:
        get_sync_engine().maybe_auto_push(reason=reason)
    except Exception:
        pass


def update_browser_instance(payload: dict[str, Any]) -> dict[str, Any]:
    """更新一条或多条浏览器记录的可编辑字段；运行中的记录在下次启动时生效。"""
    require_browser_feature()
    instance_ids = _requested_browser_instance_ids(payload)
    batch = "instance_ids" in payload
    editable = {"label", "tag", "name", "group", "fingerprint", "kernel_version", "cdp_enabled", "extensions", "start_urls"}
    if not editable & set(payload):
        raise ValueError("没有可更新的字段")
    saved: list[dict[str, Any]] = []
    restart_required = False
    with STATE_LOCK:
        state = load_state()
        instances = [find_browser_instance(state, instance_id) for instance_id in instance_ids]
        patches = [validate_browser_options_patch(payload, instance, batch=batch) for instance in instances]
        for instance, patch in zip(instances, patches):
            changed_runtime = any(key not in {"label", "name", "group"} for key in patch)
            if changed_runtime and instance.get("status") in ACTIVE_BROWSER_STATUSES:
                restart_required = True
            instance.update(patch)
            saved.append(dict(instance))
        save_state(state)
    _push_browser_change("更新浏览器配置")
    result: dict[str, Any] = {"ok": True, "updated": len(saved), "restart_required": restart_required}
    if batch:
        result["instances"] = saved
    else:
        result["instance"] = saved[0]
    return result


def create_browser_instances(payload: dict[str, Any]) -> dict[str, Any]:
    """为节点批量新建浏览器记录（不启动）。Profile 目录在首次启动时创建。"""
    require_browser_feature()
    node_id = str(payload.get("node_id") or "")
    if not node_id:
        raise ValueError("缺少节点 ID")
    count = safe_int(payload.get("count"), 1)
    if not 1 <= count <= 50:
        raise ValueError("单次可新建 1–50 条浏览器记录")
    with BROWSER_LAUNCH_LOCK:
        with STATE_LOCK:
            state = load_state()
            find_node(state, node_id)
            base = default_browser_instance_options()
            options_patch = validate_browser_options_patch(payload, base, batch=count > 1)
            created: list[dict[str, Any]] = []
            for _ in range(count):
                record = create_browser_instance_record(state, node_id, {**base, **options_patch})
                if count > 1 or "name" not in options_patch:
                    record["name"] = next_browser_instance_name(state, node_id)
                state["browser_instances"].append(record)
                created.append(dict(record))
            save_state(state)
    _push_browser_change("新建浏览器记录")
    return {"ok": True, "created": len(created), "instances": created}


def _remote_sessions_for_browser_edit() -> Any:
    if not remote_browser_feature_enabled():
        return REMOTE_BROWSER_SESSIONS_UNSET
    sessions, broker_error = remote_browser_sessions(force=True)
    if sessions is None:
        raise RuntimeError(f"无法确认远程浏览器是否已停止：{broker_error or 'Broker 不可用'}")
    return sessions


def regenerate_browser_fingerprints(payload: dict[str, Any]) -> dict[str, Any]:
    """为已停止的记录生成新的随机指纹种子；已登录网站会把它视为新设备。"""
    require_browser_feature()
    instance_ids = _requested_browser_instance_ids(payload)
    sessions = _remote_sessions_for_browser_edit()
    with BROWSER_LAUNCH_LOCK:
        with STATE_LOCK:
            state = load_state()
            if sync_browser_instance_status(state, remote_sessions_snapshot=sessions):
                save_state(state)
            instances = [find_browser_instance(state, instance_id) for instance_id in instance_ids]
            running = [item for item in instances if _browser_instance_live(item)]
            if running:
                raise ValueError(f"请先停止「{running[0].get('name') or '浏览器'}」再重新生成指纹")
            for instance in instances:
                old_seed = safe_int(instance.get("fingerprint_seed"), 0)
                seed = old_seed
                while seed == old_seed:
                    seed = secrets.randbelow(0x7FFFFFFE) + 1
                instance["fingerprint_seed"] = seed
            saved = [dict(item) for item in instances]
            save_state(state)
    _push_browser_change("重新生成浏览器指纹")
    return {"ok": True, "updated": len(saved), "instances": saved}


def prune_preserved_browser_nodes(state: dict[str, Any]) -> list[str]:
    """移除不再被任何浏览器记录引用的保留节点（订阅中已不存在，仅为浏览器保留）。"""
    referenced = {
        str(item.get("node_id") or "")
        for item in state.get("browser_instances", [])
        if isinstance(item, dict)
    }
    removed: list[str] = []
    retained: list[dict[str, Any]] = []
    for node in state.get("nodes", []):
        node_id = str(node.get("id") or "")
        if node.get(webdav_sync.LOCAL_BROWSER_NODE_MARKER) is True and node_id not in referenced:
            removed.append(node_id)
            continue
        retained.append(node)
    if removed:
        state["nodes"] = retained
        for node_id in removed:
            state.get("tests", {}).pop(node_id, None)
            state.get("test_history", {}).pop(node_id, None)
    return removed


def rebind_browser_instances(payload: dict[str, Any]) -> dict[str, Any]:
    """把已停止的浏览器记录换绑到另一个节点，并移动 Profile 目录；指纹种子保持不变。"""
    require_browser_feature()
    instance_ids = _requested_browser_instance_ids(payload)
    target_node_id = str(payload.get("node_id") or "")
    if not target_node_id:
        raise ValueError("缺少目标节点 ID")
    sessions = _remote_sessions_for_browser_edit()
    moved: list[tuple[Path, Path]] = []
    with BROWSER_LAUNCH_LOCK:
        with SHUTDOWN_LOCK:
            if SHUTTING_DOWN.is_set():
                raise RuntimeError("程序正在退出，不能换绑浏览器")
            with STATE_LOCK:
                state = load_state()
                if sync_browser_instance_status(state, remote_sessions_snapshot=sessions):
                    save_state(state)
                target = find_node(state, target_node_id)
                if target.get(webdav_sync.LOCAL_BROWSER_NODE_MARKER) is True:
                    raise ValueError("目标节点已从订阅中移除，请选择当前订阅中的节点")
                instances = [find_browser_instance(state, instance_id) for instance_id in instance_ids]
                for instance in instances:
                    if _browser_instance_live(instance):
                        raise ValueError(f"请先停止「{instance.get('name') or '浏览器'}」再换绑节点")
                    if instance.get("legacy"):
                        raise ValueError("早期的默认浏览器记录不支持换绑，请新建记录")
                try:
                    for instance in instances:
                        old_node_id = str(instance.get("node_id") or "")
                        if old_node_id == target_node_id:
                            continue
                        instance_id = str(instance["id"])
                        source = browser_instance_profile_path(old_node_id, instance_id)
                        destination = browser_instance_profile_path(target_node_id, instance_id)
                        if destination.exists():
                            raise RuntimeError(f"目标节点下已存在同名 Profile 目录：{destination}")
                        if source.exists():
                            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                            source.replace(destination)
                            moved.append((destination, source))
                            try:
                                source.parent.rmdir()
                            except OSError:
                                pass
                        instance.update(
                            {
                                "node_id": target_node_id,
                                "profile_path": str(destination),
                                "bridge_id": None,
                                "proxy": "",
                            }
                        )
                        instance.pop("exit_check", None)
                        instance.pop("error", None)
                    pruned = prune_preserved_browser_nodes(state)
                    save_state(state)
                except BaseException:
                    # 状态未提交时把已移动的目录放回原处，保证记录与 Profile 位置一致。
                    for destination, source in reversed(moved):
                        try:
                            source.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                            destination.replace(source)
                        except OSError:
                            log_event(f"回滚 Profile 移动失败：{destination}", level="warn", category="浏览器")
                    raise
                saved = [dict(item) for item in instances]
    _push_browser_change("换绑浏览器节点")
    return {
        "ok": True,
        "updated": len(saved),
        "node_id": target_node_id,
        "instances": saved,
        "removed_preserved_node_ids": pruned,
    }


def ensure_legacy_browser_instances(state: dict[str, Any]) -> None:
    instances = state.setdefault("browser_instances", [])
    existing_ids = {str(item.get("id") or "") for item in instances}
    for node in state.get("nodes", []):
        node_id = str(node.get("id") or "")
        if not node_id:
            continue
        profile_path = browser_node_profile_root(node_id)
        instance_id = legacy_browser_instance_id(node_id)
        if instance_id in existing_ids or not is_legacy_browser_profile(profile_path):
            continue
        created_at = now_ts()
        instances.append(
            {
                "id": instance_id,
                "node_id": node_id,
                "name": "默认浏览器",
                "status": "stopped",
                "profile_path": str(profile_path),
                "fingerprint_seed": browser_instance_seed(node_id, instance_id, legacy=True),
                "created_at": created_at,
                "last_started_at": None,
                "last_stopped_at": None,
                "last_used_at": created_at,
                "pid": None,
                "bridge_id": None,
                "proxy": "",
                "launch_count": 0,
                "legacy": True,
                **default_browser_instance_options(),
                # 早期默认浏览器沿用升级前的指纹参数。
                "fingerprint": browser_fingerprint.legacy_fingerprint_config(),
            }
        )
        existing_ids.add(instance_id)


def guess_transport(params: dict[str, str], fallback: str = "") -> str:
    value = params.get("type") or params.get("network") or fallback or ""
    value = value.lower()
    aliases = {"websocket": "ws", "h2": "http", "splithttp": "xhttp"}
    return aliases.get(value, value)


def guess_security(params: dict[str, str], tls_hint: Any = None) -> str:
    security = (params.get("security") or params.get("tls") or "").lower()
    if security in {"reality", "tls"}:
        return security
    if params.get("pbk") or params.get("publickey") or params.get("public-key"):
        return "reality"
    if truthy(tls_hint):
        return "tls"
    return security


def preferred_core(protocol: str, transport: str) -> str:
    if protocol == "vless" and transport in {"xhttp", "splithttp"}:
        return "xray"
    return "sing-box"


def compatible_cores(node: dict[str, Any]) -> list[str]:
    protocol = str(node.get("protocol") or "").lower()
    transport = guess_transport({}, str(node.get("transport") or ""))
    security = str(node.get("security") or "").lower()
    settings = node.get("settings") if isinstance(node.get("settings"), dict) else {}
    cores: list[str] = []
    sing_box_transports = {
        "vless": {"", "tcp", "ws", "grpc", "http", "h2"},
        "vmess": {"", "tcp", "ws", "grpc", "http", "h2"},
        "trojan": {"", "tcp", "ws", "grpc", "http", "h2"},
        "shadowsocks": {"", "tcp"},
        "hysteria2": {"", "tcp"},
        "tuic": {"", "tcp"},
        "socks": {"", "tcp"},
        "naive": {"", "tcp"},
        "anytls": {"", "tcp"},
    }
    if transport in sing_box_transports.get(protocol, set()):
        cores.append("sing-box")
    xray_transports = {"", "tcp", "ws", "grpc", "http", "h2", "xhttp"}
    xray_protocols = {"vless", "vmess", "trojan"}
    if (
        protocol in xray_protocols
        and transport in xray_transports
        and not (security == "reality" and protocol != "vless")
        and not (transport == "xhttp" and protocol != "vless")
    ):
        cores.append("xray")
    if (
        protocol == "shadowsocks"
        and transport in {"", "tcp"}
        and not str(settings.get("plugin") or "")
    ):
        cores.append("xray")
    return cores


def normalize_node_core_settings(node: dict[str, Any]) -> None:
    options = compatible_cores(node)
    default_core = preferred_core(
        str(node.get("protocol") or ""),
        str(node.get("transport") or ""),
    )
    selected = str(node.get("preferred_core") or default_core)
    if selected not in options:
        selected = default_core if default_core in options else (options[0] if options else default_core)
    node["preferred_core"] = selected
    node["fallback_enabled"] = node.get("fallback_enabled") is True and len(options) > 1


def core_attempt_order(node: dict[str, Any], requested_core: str = "auto") -> list[str]:
    options = compatible_cores(node)
    selected = str(node.get("preferred_core") or preferred_core(
        str(node.get("protocol") or ""),
        str(node.get("transport") or ""),
    )) if requested_core == "auto" else requested_core
    if selected not in options:
        raise ValueError(f"节点配置不兼容 {selected}")
    order = [selected]
    if requested_core == "auto" and node.get("fallback_enabled") is True:
        order.extend(core for core in options if core != selected)
    return order


def build_node(
    *,
    name: str,
    protocol: str,
    server: str,
    port: int,
    source: str,
    raw: str,
    settings: dict[str, Any],
    transport: str = "",
    security: str = "",
) -> Node | None:
    if not server or not port:
        return None
    protocol = protocol.lower()
    transport = guess_transport({k: str(v) for k, v in settings.items()}, transport)
    security = security or guess_security({k: str(v) for k, v in settings.items()})
    display_name = clean_name(name, f"{protocol.upper()} {server}:{port}")
    tags = sorted({value for value in (protocol, transport, security) if value})
    return Node(
        id=stable_id(protocol, server, str(port), raw or json.dumps(settings, sort_keys=True)),
        name=display_name,
        protocol=protocol,
        server=server,
        port=port,
        region=node_region(display_name, server),
        source=source,
        raw=raw,
        settings=settings,
        tags=tags,
        transport=transport,
        security=security,
        preferred_core=preferred_core(protocol, transport),
    )


def parse_vless(raw: str, source: str) -> Node | None:
    parsed = urllib.parse.urlsplit(raw)
    params = parse_query(parsed.query)
    uuid = urllib.parse.unquote(parsed.username or "")
    transport = guess_transport(params)
    security = guess_security(params)
    settings: dict[str, Any] = {
        "uuid": uuid,
        "encryption": params.get("encryption", "none"),
        "flow": params.get("flow", ""),
        "sni": params.get("sni") or params.get("servername") or params.get("peer"),
        "fingerprint": params.get("fp") or params.get("fingerprint"),
        "public_key": params.get("pbk") or params.get("publickey") or params.get("public-key"),
        "short_id": params.get("sid") or params.get("shortid") or params.get("short-id"),
        "spider_x": params.get("spx") or params.get("spiderx"),
        "path": params.get("path", ""),
        "host": params.get("host", ""),
        "mode": params.get("mode", ""),
        "service_name": params.get("servicename") or params.get("service_name") or params.get("service-name") or "",
        "allow_insecure": truthy(params.get("allowinsecure") or params.get("insecure")),
    }
    settings.update({f"query_{key}": value for key, value in params.items()})
    return build_node(
        name=parsed.fragment,
        protocol="vless",
        server=parsed.hostname or "",
        port=parsed.port or 0,
        source=source,
        raw=raw,
        settings=settings,
        transport=transport,
        security=security,
    )


def parse_vmess(raw: str, source: str) -> Node | None:
    payload = raw.split("://", 1)[1]
    decoded = decode_base64_text(payload)
    if not decoded:
        return None
    try:
        data = json.loads(decoded)
    except json.JSONDecodeError:
        return None
    transport = guess_transport({}, str(data.get("net", "")))
    security = "tls" if str(data.get("tls", "")).lower() == "tls" else ""
    settings: dict[str, Any] = {
        "uuid": data.get("id", ""),
        "alter_id": safe_int(data.get("aid"), 0),
        "security": data.get("scy") or data.get("security") or "auto",
        "sni": data.get("sni") or data.get("host"),
        "fingerprint": data.get("fp", ""),
        "path": data.get("path", ""),
        "host": data.get("host", ""),
        "tls": str(data.get("tls", "")).lower() == "tls",
        "raw_json": data,
    }
    return build_node(
        name=str(data.get("ps", "")),
        protocol="vmess",
        server=str(data.get("add", "")),
        port=safe_int(data.get("port")),
        source=source,
        raw=raw,
        settings=settings,
        transport=transport,
        security=security,
    )


def parse_host_port(value: str) -> tuple[str, int]:
    parsed = urllib.parse.urlsplit(f"dummy://{value}")
    return parsed.hostname or "", parsed.port or 0


def parse_ss(raw: str, source: str) -> Node | None:
    without_scheme = raw.split("://", 1)[1]
    main, _, fragment = without_scheme.partition("#")
    main, _, query = main.partition("?")
    method = ""
    password = ""
    host = ""
    port = 0

    if "@" in main:
        userinfo, hostport = main.rsplit("@", 1)
        decoded_userinfo = urllib.parse.unquote(userinfo)
        if ":" not in decoded_userinfo:
            decoded_userinfo = decode_base64_any(decoded_userinfo) or decoded_userinfo
        if ":" in decoded_userinfo:
            method, password = decoded_userinfo.split(":", 1)
        host, port = parse_host_port(hostport)
    else:
        decoded = decode_base64_any(main)
        if decoded and "@" in decoded:
            userinfo, hostport = decoded.rsplit("@", 1)
            if ":" in userinfo:
                method, password = userinfo.split(":", 1)
            host, port = parse_host_port(hostport)

    params = parse_query(query)
    settings = {
        "method": urllib.parse.unquote(method),
        "password": urllib.parse.unquote(password),
        "plugin": params.get("plugin", ""),
        "query": params,
    }
    return build_node(
        name=fragment,
        protocol="shadowsocks",
        server=host,
        port=port,
        source=source,
        raw=raw,
        settings=settings,
    )


def parse_userinfo_url(raw: str, source: str, protocol: str) -> Node | None:
    parsed = urllib.parse.urlsplit(raw)
    params = parse_query(parsed.query)
    username = urllib.parse.unquote(parsed.username or "")
    password = urllib.parse.unquote(parsed.password or "")
    transport = guess_transport(params)
    security = guess_security(params, tls_hint=params.get("sni") or params.get("peer"))
    settings: dict[str, Any] = {
        "username": username,
        "password": password,
        "uuid": username,
        "sni": params.get("sni") or params.get("servername") or params.get("peer"),
        "fingerprint": params.get("fp") or params.get("fingerprint"),
        "path": params.get("path", ""),
        "host": params.get("host", ""),
        "allow_insecure": truthy(params.get("allowinsecure") or params.get("insecure")),
        "congestion_control": params.get("congestion_control") or params.get("congestion-control"),
        "query": params,
        "scheme": parsed.scheme,
    }
    if protocol == "trojan":
        settings["password"] = username
        settings["username"] = ""
        security = security or "tls"
    if protocol in {"hysteria2", "anytls"}:
        settings["password"] = password or username
        settings["username"] = ""
        settings["uuid"] = ""
    if protocol in {"hysteria2", "tuic", "naive", "anytls"}:
        security = security or "tls"
    if protocol == "socks":
        security = ""
    return build_node(
        name=parsed.fragment,
        protocol=protocol,
        server=parsed.hostname or "",
        port=parsed.port or 0,
        source=source,
        raw=raw,
        settings=settings,
        transport=transport,
        security=security,
    )


def parse_link(raw: str, source: str = "手动导入") -> Node | None:
    raw = raw.strip().strip("'\"")
    if not raw or "://" not in raw:
        return None
    scheme = urllib.parse.urlsplit(raw).scheme.lower()
    try:
        if scheme == "vless":
            return parse_vless(raw, source)
        if scheme == "vmess":
            return parse_vmess(raw, source)
        if scheme == "ss":
            return parse_ss(raw, source)
        if scheme == "trojan":
            return parse_userinfo_url(raw, source, "trojan")
        if scheme in {"hy2", "hysteria2"}:
            return parse_userinfo_url(raw, source, "hysteria2")
        if scheme == "tuic":
            return parse_userinfo_url(raw, source, "tuic")
        if scheme in {"socks", "socks5"}:
            return parse_userinfo_url(raw, source, "socks")
        if scheme.startswith("naive"):
            return parse_userinfo_url(raw, source, "naive")
        if scheme == "anytls":
            return parse_userinfo_url(raw, source, "anytls")
    except (ValueError, TypeError):
        return None
    return None


def link_tokens(text: str) -> list[str]:
    tokens: list[str] = []
    schemes = (
        "vless",
        "vmess",
        "ss",
        "trojan",
        "hy2",
        "hysteria2",
        "tuic",
        "socks",
        "socks5",
        "naive+https",
        "naive+quic",
        "anytls",
    )
    for line in text.splitlines():
        candidate = line.strip().strip("- ").strip("'\"")
        if any(candidate.lower().startswith(f"{scheme}://") for scheme in schemes):
            tokens.append(candidate)
            continue
        for match in re.finditer(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s'\"<>]+", line):
            url = match.group(0).strip()
            if any(url.lower().startswith(f"{scheme}://") for scheme in schemes):
                tokens.append(url)
    return list(dict.fromkeys(tokens))


def import_yaml() -> Any:
    try:
        import yaml  # type: ignore

        return yaml
    except Exception:
        return None


def clash_proxy_to_node(proxy: dict[str, Any], source: str) -> Node | None:
    protocol = str(proxy.get("type", "")).lower()
    if protocol in {"ss", "shadowsocks"}:
        normalized = "shadowsocks"
    elif protocol in {"socks5", "socks"}:
        normalized = "socks"
    elif protocol in {"hysteria2", "hy2"}:
        normalized = "hysteria2"
    else:
        normalized = protocol
    if normalized not in {
        "vless",
        "vmess",
        "trojan",
        "shadowsocks",
        "hysteria2",
        "tuic",
        "socks",
        "naive",
        "anytls",
    }:
        return None

    transport = guess_transport({}, str(proxy.get("network", "")))
    settings: dict[str, Any] = {
        "uuid": proxy.get("uuid") or proxy.get("id") or "",
        "password": proxy.get("password") or "",
        "username": proxy.get("username") or "",
        "method": proxy.get("cipher") or proxy.get("method") or "",
        "alter_id": safe_int(proxy.get("alterId") or proxy.get("alter-id") or proxy.get("aid"), 0),
        "security": proxy.get("cipher") or proxy.get("security") or "auto",
        "tls": truthy(proxy.get("tls")),
        "sni": proxy.get("servername") or proxy.get("sni") or "",
        "flow": proxy.get("flow") or "",
        "allow_insecure": truthy(proxy.get("skip-cert-verify") or proxy.get("allow-insecure")),
        "raw_yaml": proxy,
    }
    reality_opts = proxy.get("reality-opts") or {}
    ws_opts = proxy.get("ws-opts") or {}
    grpc_opts = proxy.get("grpc-opts") or {}
    if isinstance(reality_opts, dict):
        settings["public_key"] = reality_opts.get("public-key") or reality_opts.get("publicKey") or ""
        settings["short_id"] = reality_opts.get("short-id") or reality_opts.get("shortId") or ""
        settings["fingerprint"] = reality_opts.get("fingerprint") or proxy.get("client-fingerprint") or ""
    if isinstance(ws_opts, dict):
        settings["path"] = ws_opts.get("path") or ""
        headers = ws_opts.get("headers") or {}
        if isinstance(headers, dict):
            settings["host"] = headers.get("Host") or headers.get("host") or ""
    if isinstance(grpc_opts, dict):
        normalized_grpc_opts = {str(key).lower(): value for key, value in grpc_opts.items()}
        settings["service_name"] = (
            normalized_grpc_opts.get("grpc-service-name")
            or normalized_grpc_opts.get("servicename")
            or normalized_grpc_opts.get("service_name")
            or normalized_grpc_opts.get("service-name")
            or ""
        )

    security = guess_security({k: str(v) for k, v in settings.items()}, settings.get("tls"))
    raw = json.dumps(proxy, sort_keys=True, ensure_ascii=False)
    return build_node(
        name=str(proxy.get("name") or ""),
        protocol=normalized,
        server=str(proxy.get("server") or ""),
        port=safe_int(proxy.get("port")),
        source=source,
        raw=raw,
        settings=settings,
        transport=transport,
        security=security,
    )


def parse_clash_yaml(text: str, source: str) -> list[Node]:
    yaml = import_yaml()
    if not yaml:
        return []
    try:
        data = yaml.safe_load(text)
    except Exception:
        return []
    if not isinstance(data, dict) or not isinstance(data.get("proxies"), list):
        return []
    nodes = []
    for item in data["proxies"]:
        if isinstance(item, dict):
            node = clash_proxy_to_node(item, source)
            if node:
                nodes.append(node)
    return nodes


def parse_subscription_payload(payload: str, source: str = "手动导入") -> list[Node]:
    candidates = [payload]
    decoded = decode_base64_text(payload)
    if decoded and decoded not in candidates:
        candidates.append(decoded)

    nodes: dict[str, Node] = {}
    for text in candidates:
        for node in parse_clash_yaml(text, source):
            nodes[node.id] = node
        for token in link_tokens(text):
            node = parse_link(token, source)
            if node:
                nodes[node.id] = node
    return list(nodes.values())


def empty_state() -> dict[str, Any]:
    return {
        "subscriptions": [],
        "nodes": [],
        "bridges": [],
        "browser_instances": [],
        "tests": {},
        "test_history": {},
        "request_logs": [],
        "settings": {
            "allow_lan": False,
            "lan_security": normalize_lan_security({}),
            "subscriptions": normalize_subscription_settings({}),
            "speed_test": normalize_speed_test_settings({}),
            "logs": normalize_log_settings({}),
            "smart_gateway": normalize_smart_gateway_settings({}),
            # 自动切换默认关闭；绑定浏览器记录的节点始终禁止自动改变出口。
            "health": {"auto_switch": False},
        },
    }


def apply_pending_request_log_updates(state: dict[str, Any]) -> None:
    with REQUEST_LOG_PENDING_LOCK:
        pending = {log_id: dict(updates) for log_id, updates in REQUEST_LOG_PENDING_UPDATES.items()}
    if not pending:
        return
    for entry in state.get("request_logs", []):
        log_id = str(entry.get("id") or "")
        if log_id in pending and entry.get("status") not in REQUEST_LOG_TERMINAL_STATUSES:
            entry.update(pending[log_id])


def state_backup_path() -> Path:
    return STATE_PATH.with_name(f"{STATE_PATH.name}.bak")


def _normalize_loaded_state(state: Any, *, reset_running: bool = False) -> dict[str, Any]:
    if not isinstance(state, dict):
        state = {}
    for field_name in ("subscriptions", "nodes", "bridges", "browser_instances", "request_logs"):
        value = state.get(field_name)
        state[field_name] = [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []
    if not isinstance(state.get("tests"), dict):
        state["tests"] = {}
    if not isinstance(state.get("test_history"), dict):
        state["test_history"] = {}
    settings = state.get("settings")
    state["settings"] = settings if isinstance(settings, dict) else {}
    # 局域网共享是设备本地安全设置：只接受持久化的 JSON 布尔值，旧状态默认关闭。
    state["settings"]["allow_lan"] = state["settings"].get("allow_lan") is True
    state["settings"]["lan_security"] = normalize_lan_security(state["settings"].get("lan_security"))
    state["settings"]["subscriptions"] = normalize_subscription_settings(
        state["settings"].get("subscriptions")
    )
    state["settings"]["speed_test"] = normalize_speed_test_settings(state["settings"].get("speed_test"))
    state["settings"]["logs"] = cache_log_settings(state["settings"].get("logs"))
    state["settings"]["smart_gateway"] = normalize_smart_gateway_settings(
        state["settings"].get("smart_gateway")
    )
    health = state["settings"].get("health")
    state["settings"]["health"] = {"auto_switch": isinstance(health, dict) and health.get("auto_switch") is True}
    for node in state["nodes"]:
        normalize_node_core_settings(node)
        node["favorite"] = node.get("favorite") is True
        node["remark"] = str(node.get("remark") or "")[:500]
        override = str(node.get("region_override") or "")
        if override in REGION_CATALOG:
            node["region"] = REGION_CATALOG[override][0]
        elif override == "ZZ":
            node["region"] = "未分类"
        elif not node.get("region") or node.get("region") == "未分类":
            node["region"] = node_region(str(node.get("name") or ""), str(node.get("server") or ""))
    for subscription in state["subscriptions"]:
        normalize_subscription_schedule(subscription)
    for result in state["tests"].values():
        if not isinstance(result, dict):
            continue
        result["attempt_count"] = max(1, safe_int(result.get("attempt_count"), 1))
        default_successes = 1 if result.get("ok") else 0
        result["success_count"] = min(
            result["attempt_count"],
            max(0, safe_int(result.get("success_count"), default_successes)),
        )
        result["success_rate"] = round(result["success_count"] / result["attempt_count"], 4)
    for bridge in state["bridges"]:
        bridge["pool_health"] = normalize_pool_health(bridge.get("pool_health"))
        health = bridge.get("health") if isinstance(bridge.get("health"), dict) else {}
        bridge["health"] = {
            "status": health.get("status") if health.get("status") in {"unknown", "ok", "error"} else "unknown",
            "checked_at": max(0, safe_int(health.get("checked_at"), 0)),
            "error": str(health.get("error") or "")[:500],
        }
        if "enabled" not in bridge:
            bridge["enabled"] = bridge.get("status") in ACTIVE_BRIDGE_STATUSES
        else:
            bridge["enabled"] = bool(bridge.get("enabled"))
        if bridge.get("port") and not bridge.get("socks_url"):
            bridge["socks_url"] = f"socks5://127.0.0.1:{bridge.get('port')}"
        if bridge.get("port") and not bridge.get("http_url"):
            bridge["http_url"] = f"http://127.0.0.1:{bridge.get('port')}"
        traffic = traffic_defaults(bridge.get("upload_bytes"), bridge.get("download_bytes"))
        bridge.update(traffic)
    if reset_running:
        for bridge in state["bridges"]:
            if bridge.get("status") in ACTIVE_BRIDGE_STATUSES:
                bridge["status"] = "stopped"
                bridge["pid"] = None
    normalize_browser_instances(state)
    if reset_running:
        for instance in state["browser_instances"]:
            if instance.get("status") in ACTIVE_BROWSER_STATUSES:
                instance["status"] = "stopped"
                instance["pid"] = None
    apply_pending_request_log_updates(state)
    return state


def _read_state_candidate(path: Path, *, reset_running: bool = False) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return _normalize_loaded_state(json.load(f), reset_running=reset_running)


def _state_file_exists(path: Path) -> bool:
    try:
        path.stat()
    except FileNotFoundError:
        return False
    return True


def load_state(reset_running: bool = False) -> dict[str, Any]:
    ensure_dirs()
    primary = STATE_PATH
    backup = state_backup_path()
    if not _state_file_exists(primary):
        if _state_file_exists(backup):
            try:
                recovered = _read_state_candidate(backup, reset_running=reset_running)
            except Exception as exc:
                raise RuntimeError(f"状态文件和备份均不可读: {exc}") from exc
            log_event("主状态文件缺失，已从备份恢复", level="warn", category="状态")
            return recovered
        return empty_state()

    try:
        return _read_state_candidate(primary, reset_running=reset_running)
    except Exception as primary_error:
        if _state_file_exists(backup):
            try:
                recovered = _read_state_candidate(backup, reset_running=reset_running)
            except Exception as backup_error:
                raise RuntimeError(f"状态文件损坏且备份不可用: {backup_error}") from primary_error
            log_event("状态文件损坏，已从备份恢复", level="error", category="状态")
            return recovered
        raise RuntimeError(f"状态文件不可读，已拒绝回落为空状态: {primary_error}") from primary_error


def _fsync_directory(path: Path) -> None:
    try:
        flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            flags |= os.O_DIRECTORY
        fd = os.open(str(path), flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_private_atomic(path: Path, data: bytes, *, temp_path: Path, mode: int = 0o600) -> None:
    try:
        with temp_path.open("wb") as f:
            if hasattr(os, "fchmod"):
                try:
                    os.fchmod(f.fileno(), mode)
                except OSError:
                    pass
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        try:
            temp_path.chmod(mode)
        except OSError:
            pass
        temp_path.replace(path)
        try:
            path.chmod(mode)
        except OSError:
            pass
        _fsync_directory(path.parent)
    except BaseException:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _valid_state_bytes(raw: bytes) -> bool:
    try:
        return isinstance(json.loads(raw.decode("utf-8")), dict)
    except Exception:
        return False


def save_state(state: dict[str, Any]) -> None:
    ensure_dirs()
    data = json.dumps(state, ensure_ascii=False, indent=2).encode("utf-8")
    backup = state_backup_path()
    if _state_file_exists(STATE_PATH):
        try:
            previous = STATE_PATH.read_bytes()
        except Exception as exc:
            raise RuntimeError(f"无法读取现有状态，已拒绝覆盖: {exc}") from exc
        if not _valid_state_bytes(previous):
            if not _state_file_exists(backup):
                raise RuntimeError("现有状态文件损坏且没有可用备份，已拒绝覆盖")
        else:
            _write_private_atomic(
                backup,
                previous,
                temp_path=backup.with_suffix(backup.suffix + ".tmp"),
            )
    _write_private_atomic(STATE_PATH, data, temp_path=STATE_PATH.with_suffix(".tmp"))
    notify_status_change()


def traffic_defaults(upload_bytes: Any = 0, download_bytes: Any = 0) -> dict[str, int]:
    upload = max(0, safe_int(upload_bytes, 0))
    download = max(0, safe_int(download_bytes, 0))
    return {"upload_bytes": upload, "download_bytes": download, "total_bytes": upload + download}


def init_bridge_traffic(bridge_id: str, upload_bytes: Any = 0, download_bytes: Any = 0) -> dict[str, int]:
    traffic = traffic_defaults(upload_bytes, download_bytes)
    with TRAFFIC_LOCK:
        TRAFFIC_TABLE[bridge_id] = dict(traffic)
    return traffic


def add_bridge_traffic(bridge_id: str, key: str, byte_count: int) -> None:
    if byte_count <= 0:
        return
    if key not in {"upload_bytes", "download_bytes"}:
        return
    with TRAFFIC_LOCK:
        traffic = TRAFFIC_TABLE.setdefault(bridge_id, traffic_defaults())
        traffic[key] += byte_count
        traffic["total_bytes"] = traffic["upload_bytes"] + traffic["download_bytes"]


def bridge_traffic_snapshot(bridge_id: str) -> dict[str, int]:
    with TRAFFIC_LOCK:
        return dict(TRAFFIC_TABLE.get(bridge_id, traffic_defaults()))


def live_bridge_traffic_snapshot(bridge_id: str) -> dict[str, int] | None:
    with TRAFFIC_LOCK:
        traffic = TRAFFIC_TABLE.get(bridge_id)
        return dict(traffic) if traffic is not None else None


def pop_bridge_traffic(bridge_id: str) -> dict[str, int]:
    with TRAFFIC_LOCK:
        return dict(TRAFFIC_TABLE.pop(bridge_id, traffic_defaults()))


def pop_bridge_traffic_if_matches(bridge_id: str, traffic: dict[str, int]) -> bool:
    with TRAFFIC_LOCK:
        if TRAFFIC_TABLE.get(bridge_id) != traffic:
            return False
        TRAFFIC_TABLE.pop(bridge_id, None)
        return True


def append_request_log(entry: dict[str, Any]) -> None:
    with STATE_LOCK:
        state = load_state(reset_running=False)
        logs = state.setdefault("request_logs", [])
        logs.insert(0, entry)
        del logs[REQUEST_LOG_LIMIT:]
        save_state(state)


def update_request_log(log_id: str, updates: dict[str, Any]) -> None:
    if updates == {"status": "connected"}:
        # Serialize the coalesced intermediate update with terminal persistence.
        # A late "connected" callback must never resurrect a closed/failed entry.
        with STATE_LOCK:
            state = load_state(reset_running=False)
            entry = next(
                (item for item in state.setdefault("request_logs", []) if item.get("id") == log_id),
                None,
            )
            if entry is None or entry.get("status") in REQUEST_LOG_TERMINAL_STATUSES:
                return
            with REQUEST_LOG_PENDING_LOCK:
                REQUEST_LOG_PENDING_UPDATES.setdefault(log_id, {}).update(updates)
        return
    with STATE_LOCK:
        # STATE_LOCK -> REQUEST_LOG_PENDING_LOCK is the same order used by
        # load_state/apply_pending_request_log_updates. Consume pending before the
        # read+save transaction so no other state write can reapply stale data.
        with REQUEST_LOG_PENDING_LOCK:
            REQUEST_LOG_PENDING_UPDATES.pop(log_id, None)
        state = load_state(reset_running=False)
        for entry in state.setdefault("request_logs", []):
            if entry.get("id") == log_id:
                entry.update(updates)
                break
        save_state(state)


def clear_request_logs() -> dict[str, Any]:
    with STATE_LOCK:
        state = load_state(reset_running=False)
        state["request_logs"] = []
        save_state(state)
    with REQUEST_LOG_PENDING_LOCK:
        REQUEST_LOG_PENDING_UPDATES.clear()
    return {"ok": True}


def _log_query_entries(kind: str) -> list[dict[str, Any]]:
    if kind == "request":
        with STATE_LOCK:
            return [dict(item) for item in load_state(reset_running=False).get("request_logs", [])]
    if kind == "system":
        return [dict(item) for item in system_log_entries()]
    raise ValueError("日志类型只能是 request 或 system")


def _log_entry_matches(entry: dict[str, Any], kind: str, params: dict[str, list[str]]) -> bool:
    search = first(params, "search", default="").strip().lower()[:256]
    if kind == "request":
        node = first(params, "node", default="all").strip()
        status = first(params, "status", default="all").strip().lower()
        if status not in {"all", "connecting", "connected", "closed", "failed"}:
            raise ValueError("连接状态筛选值无效")
        if node and node != "all" and node not in {
            str(entry.get("node_id") or ""),
            str(entry.get("node_name") or ""),
        }:
            return False
        if status != "all" and str(entry.get("status") or "") != status:
            return False
        target = entry.get("target") or f"{entry.get('target_host') or ''}:{entry.get('target_port') or ''}"
        values = (
            target,
            entry.get("target_host"),
            entry.get("target_port"),
            entry.get("node_name"),
            entry.get("client"),
            entry.get("status"),
            entry.get("message"),
            entry.get("command"),
            entry.get("proxy_protocol"),
        )
    else:
        level = first(params, "level", default="all").strip().lower()
        if level not in {"all", "info", "warn", "error"}:
            raise ValueError("系统日志级别筛选值无效")
        if level != "all" and str(entry.get("level") or "") != level:
            return False
        values = (entry.get("message"), entry.get("category"), entry.get("level"))
    return not search or any(search in str(value or "").lower() for value in values)


def query_logs(params: dict[str, list[str]]) -> dict[str, Any]:
    kind = first(params, "kind", default="request").strip().lower()
    page = safe_int(first(params, "page", default="1"), -1)
    limit = safe_int(first(params, "limit", default=str(LOG_PAGE_SIZE_DEFAULT)), -1)
    if page < 1:
        raise ValueError("page 必须大于等于 1")
    if not 1 <= limit <= LOG_PAGE_SIZE_MAX:
        raise ValueError(f"limit 必须在 1..{LOG_PAGE_SIZE_MAX} 之间")
    entries = _log_query_entries(kind)
    filtered = [entry for entry in entries if _log_entry_matches(entry, kind, params)]
    filtered_total = len(filtered)
    page_count = max(1, (filtered_total + limit - 1) // limit)
    page = min(page, page_count)
    start = (page - 1) * limit
    return {
        "kind": kind,
        "items": filtered[start : start + limit],
        "total": len(entries),
        "filtered_total": filtered_total,
        "page": page,
        "limit": limit,
        "page_count": page_count,
    }


def _csv_safe_cell(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    return f"'{value}" if value.startswith(("=", "+", "-", "@")) else value


def export_logs(params: dict[str, list[str]]) -> tuple[bytes, str, str]:
    kind = first(params, "kind", default="request").strip().lower()
    export_format = first(params, "format", default="json").strip().lower()
    entries = [
        entry
        for entry in _log_query_entries(kind)
        if _log_entry_matches(entry, kind, params)
    ]
    stamp = time.strftime("%Y%m%d-%H%M%S")
    if export_format == "json":
        data = json.dumps(
            {"kind": kind, "count": len(entries), "exported_at": now_ts(), "logs": entries},
            ensure_ascii=False,
            indent=2,
        ).encode("utf-8")
        return data, "application/json; charset=utf-8", f"proxybridge-{kind}-logs-{stamp}.json"
    if export_format != "csv":
        raise ValueError("导出格式只能是 csv 或 json")
    fields = (
        [
            "id", "started_at", "status", "node_id", "node_name", "client", "target",
            "target_host", "target_port", "proxy_protocol", "command", "upload_bytes",
            "download_bytes", "duration_ms", "message",
        ]
        if kind == "request"
        else ["ts", "level", "category", "message"]
    )
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for entry in entries:
        writer.writerow({field: _csv_safe_cell(entry.get(field, "")) for field in fields})
    return (
        ("\ufeff" + output.getvalue()).encode("utf-8"),
        "text/csv; charset=utf-8",
        f"proxybridge-{kind}-logs-{stamp}.csv",
    )


def public_node(node: dict[str, Any]) -> dict[str, Any]:
    settings = node.get("settings") or {}
    options = compatible_cores(node)
    return {
        "id": node.get("id"),
        "name": node.get("name"),
        "protocol": node.get("protocol"),
        "server": node.get("server"),
        "port": node.get("port"),
        "region": node.get("region"),
        "region_code": node_region_code(node.get("region")),
        "region_override": str(node.get("region_override") or ""),
        "favorite": node.get("favorite") is True,
        "remark": str(node.get("remark") or ""),
        "source": node.get("source"),
        "tags": node.get("tags", []),
        "transport": node.get("transport"),
        "security": node.get("security"),
        "preferred_core": node.get("preferred_core"),
        "fallback_enabled": node.get("fallback_enabled") is True,
        "fallback_core": next((core for core in options if core != node.get("preferred_core")), None),
        "core_options": options,
        "has_secret": bool(settings.get("password") or settings.get("uuid")),
        # 订阅中已不存在、仅因浏览器记录引用而保留的节点；界面提示换绑，代理池不再使用。
        "browser_preserved": node.get(webdav_sync.LOCAL_BROWSER_NODE_MARKER) is True,
    }


def merge_test_result(previous: Any, current: dict[str, Any]) -> dict[str, Any]:
    prior = previous if isinstance(previous, dict) else {}
    attempts = max(0, safe_int(prior.get("attempt_count"), 0)) + 1
    successes = max(0, safe_int(prior.get("success_count"), 1 if prior.get("ok") else 0))
    if current.get("ok"):
        successes += 1
    merged = dict(current)
    merged.update(
        {
            "attempt_count": attempts,
            "success_count": min(attempts, successes),
            "success_rate": round(min(attempts, successes) / attempts, 4),
        }
    )
    return merged


def public_test_results(state: dict[str, Any], current_time: int | None = None) -> dict[str, dict[str, Any]]:
    now = now_ts() if current_time is None else current_time
    settings = normalize_speed_test_settings((state.get("settings") or {}).get("speed_test"))
    stale_after = settings["stale_after_seconds"]
    results: dict[str, dict[str, Any]] = {}
    for node_id, raw in (state.get("tests") or {}).items():
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        tested_at = max(0, safe_int(item.get("tested_at"), 0))
        age_seconds = max(0, now - tested_at) if tested_at else None
        item["age_seconds"] = age_seconds
        item["stale"] = age_seconds is None or age_seconds >= stale_after
        results[str(node_id)] = item
    return results


def speed_test_settings_payload(state: dict[str, Any] | None = None) -> dict[str, Any]:
    if state is None:
        with STATE_LOCK:
            state = load_state()
    return normalize_speed_test_settings((state.get("settings") or {}).get("speed_test"))


def update_speed_test_settings(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload.get("targets"), list):
        raise ValueError("测速目标必须是 URL 数组")
    if len(payload["targets"]) > SPEED_TEST_TARGET_LIMIT:
        raise ValueError(f"测速目标最多 {SPEED_TEST_TARGET_LIMIT} 个")
    normalized = normalize_speed_test_settings(payload)
    if len(normalized["targets"]) != len({str(item).strip() for item in payload["targets"] if str(item).strip()}):
        for item in payload["targets"]:
            normalize_speed_test_target(item)
    with STATE_LOCK:
        state = load_state()
        state.setdefault("settings", {})["speed_test"] = normalized
        save_state(state)
    return normalized


def log_settings_payload(state: dict[str, Any] | None = None) -> dict[str, int]:
    if state is None:
        with STATE_LOCK:
            state = load_state()
    return normalize_log_settings((state.get("settings") or {}).get("logs"))


def update_log_settings(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("日志设置必须是对象")
    required = {
        "retention_days": (LOG_RETENTION_DAYS_MIN, LOG_RETENTION_DAYS_MAX),
        "max_file_bytes": (LOG_FILE_MAX_BYTES_MIN, LOG_FILE_MAX_BYTES_MAX),
        "disk_quota_bytes": (LOG_DISK_QUOTA_BYTES_MIN, LOG_DISK_QUOTA_BYTES_MAX),
    }
    for field_name, (minimum, maximum) in required.items():
        value = safe_int(payload.get(field_name), -1)
        if not minimum <= value <= maximum:
            raise ValueError(f"{field_name} 必须在 {minimum}..{maximum} 之间")
    normalized = normalize_log_settings(payload)
    with STATE_LOCK:
        state = load_state()
        state.setdefault("settings", {})["logs"] = normalized
        save_state(state)
    cache_log_settings(normalized)
    cleanup = cleanup_runtime_logs(normalized)
    return {**normalized, "storage": runtime_log_storage_payload(), "cleanup": cleanup}


def _is_executable_file(path: Path) -> bool:
    if not path.is_file():
        return False
    if os.name == "nt":
        return True
    return os.access(path, os.X_OK)


def _core_binary_candidates(base_name: str) -> list[str]:
    if os.name == "nt":
        return [f"{base_name}.exe", base_name]
    return [base_name, f"{base_name}.exe"]


def core_paths() -> dict[str, str | None]:
    core_dir = Path(os.environ.get("PROXY_BRIDGE_CORE_DIR", APP_ROOT / "bin")).expanduser()

    def executable(path: Path) -> str | None:
        return str(path) if _is_executable_file(path) else None

    def first_existing(*paths: Path) -> str | None:
        for path in paths:
            found = executable(path)
            if found:
                return found
        return None

    sing_names = _core_binary_candidates("sing-box")
    xray_names = _core_binary_candidates("xray") + _core_binary_candidates("xray-core")

    sing_box = (
        executable(Path(os.environ.get("PROXY_BRIDGE_SING_BOX", "")))
        or first_existing(*(core_dir / name for name in sing_names))
        or first_existing(*(proxy_core_bin_dir() / name for name in sing_names))
        or next((shutil.which(name) for name in sing_names if shutil.which(name)), None)
    )
    xray = (
        executable(Path(os.environ.get("PROXY_BRIDGE_XRAY", "")))
        or first_existing(*(core_dir / name for name in xray_names))
        or first_existing(*(proxy_core_bin_dir() / name for name in xray_names))
        or next((shutil.which(name) for name in xray_names if shutil.which(name)), None)
    )
    return {"sing-box": sing_box, "xray": xray}


SING_BOX_RELEASES_API = "https://api.github.com/repos/SagerNet/sing-box/releases/latest"
XRAY_RELEASES_API = "https://api.github.com/repos/XTLS/Xray-core/releases/latest"


def _require_https_url(url: str, label: str) -> urllib.parse.SplitResult:
    try:
        parsed = urllib.parse.urlsplit(url)
        parsed.port
    except ValueError as error:
        raise RuntimeError(f"{label} URL 格式无效") from error
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise RuntimeError(f"{label}必须使用不含凭据的 HTTPS URL")
    return parsed


class _HTTPSOnlyRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
        _require_https_url(newurl, "重定向目标")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open_https_url(request: urllib.request.Request, *, timeout: float) -> Any:
    _require_https_url(str(request.full_url), "请求")
    opener = urllib.request.build_opener(_HTTPSOnlyRedirectHandler())
    return opener.open(request, timeout=timeout)


def _response_https_url(response: Any, fallback: str) -> str:
    geturl = getattr(response, "geturl", None)
    final_url = str(geturl() if callable(geturl) else fallback)
    _require_https_url(final_url, "响应")
    return final_url


def _optional_nonnegative_int(value: Any, label: str) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError) as error:
        raise RuntimeError(f"{label}无效") from error
    if parsed < 0:
        raise RuntimeError(f"{label}无效")
    return parsed


def _github_json(
    url: str,
    *,
    retry_count: int = DOWNLOAD_RETRY_COUNT,
    retry_backoff: float = DOWNLOAD_RETRY_BACKOFF,
) -> Any:
    _require_https_url(url, "GitHub API ")
    attempts = max(1, retry_count)
    last_error: BaseException | None = None
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(
                url,
                headers={"User-Agent": "ProxyBridge/0.1", "Accept": "application/vnd.github+json"},
            )
            with _open_https_url(request, timeout=30) as response:
                _response_https_url(response, url)
                headers = getattr(response, "headers", {})
                declared_size = _optional_nonnegative_int(headers.get("Content-Length"), "GitHub API 响应长度")
                if declared_size is not None and declared_size > GITHUB_JSON_RESPONSE_LIMIT:
                    raise RuntimeError("GitHub API 响应过大")
                data = response.read(GITHUB_JSON_RESPONSE_LIMIT + 1)
                if len(data) > GITHUB_JSON_RESPONSE_LIMIT:
                    raise RuntimeError("GitHub API 响应过大")
                if declared_size is not None and len(data) != declared_size:
                    raise RuntimeError(
                        f"GitHub API 响应长度不匹配:期望 {declared_size}，实际 {len(data)}"
                    )
                return json.loads(data.decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as error:
            last_error = error
            if error.code not in {408, 429, 500, 502, 503, 504}:
                raise
        except (urllib.error.URLError, OSError, RuntimeError, json.JSONDecodeError) as error:
            last_error = error
        if attempt + 1 < attempts and retry_backoff > 0:
            time.sleep(retry_backoff * (2**attempt))
    raise RuntimeError(f"GitHub API 请求失败（已重试 {attempts} 次）: {last_error}") from last_error


def _asset_url(asset: dict[str, Any]) -> str:
    return str(asset.get("browser_download_url") or "")


def _asset_name(asset: dict[str, Any]) -> str:
    return str(asset.get("name") or "")


def _safe_asset_name(asset: dict[str, Any]) -> str:
    name = _asset_name(asset)
    if not name or name in {".", ".."} or Path(name).name != name or "/" in name or "\\" in name:
        raise RuntimeError("GitHub release 资源名称不安全")
    return name


def _sing_box_os_terms(host_os: str) -> tuple[str, ...]:
    if host_os == "darwin":
        return ("darwin", "macos", "apple")
    if host_os == "windows":
        return ("windows", "win32", "win64")
    if host_os == "linux":
        return ("linux",)
    return (host_os,)


def _xray_os_terms(host_os: str) -> tuple[str, ...]:
    if host_os == "darwin":
        return ("macos", "darwin")
    if host_os == "windows":
        return ("windows", "win")
    if host_os == "linux":
        return ("linux",)
    return (host_os,)


def _foreign_os_skip_terms(host_os: str) -> tuple[str, ...]:
    foreign = {
        "darwin": ("windows", "linux", "android", "openwrt"),
        "windows": ("darwin", "macos", "apple", "linux", "android", "openwrt"),
        "linux": ("darwin", "macos", "apple", "windows", "android", "openwrt"),
    }
    return foreign.get(host_os, ("android", "openwrt"))


def _pick_sing_box_asset(
    release: dict[str, Any],
    host_arch: str,
    host_os: str | None = None,
) -> dict[str, Any] | None:
    assets = release.get("assets") or []
    os_token = host_os or host_os_token()
    arch_terms = ("arm64", "aarch64") if host_arch == "arm64" else ("amd64", "x86_64", "x64")
    os_terms = _sing_box_os_terms(os_token)
    skip_terms = _foreign_os_skip_terms(os_token) + (".apk", ".ipk", ".deb", ".rpm")
    archive_terms = (".tar.gz", ".tgz", ".zip")

    def score(asset: dict[str, Any]) -> int:
        name = _asset_name(asset).lower()
        if not _asset_url(asset) or not name.startswith("sing-box"):
            return -1
        if not any(name.endswith(term) for term in archive_terms):
            return -1
        if not any(term in name for term in os_terms):
            return -1
        if not any(term in name for term in arch_terms):
            return -1
        if any(skip in name for skip in skip_terms):
            return -1
        value = 10
        if os_token == "darwin" and "darwin" in name:
            value += 20
        if os_token == "darwin" and "apple" in name:
            value += 10
        if os_token == "windows" and "windows" in name:
            value += 20
        if os_token == "linux" and "linux" in name:
            value += 20
        if name.endswith(".tar.gz") or name.endswith(".tgz"):
            value += 5
        if os_token == "windows" and name.endswith(".zip"):
            value += 5
        return value

    candidates = [(score(asset), asset) for asset in assets]
    candidates = [item for item in candidates if item[0] >= 0]
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1] if candidates else None


def _pick_xray_asset(
    release: dict[str, Any],
    host_arch: str,
    host_os: str | None = None,
) -> dict[str, Any] | None:
    assets = release.get("assets") or []
    os_token = host_os or host_os_token()
    os_terms = _xray_os_terms(os_token)

    def score(asset: dict[str, Any]) -> int:
        name = _asset_name(asset).lower()
        if not _asset_url(asset) or not name.endswith(".zip"):
            return -1
        if not name.startswith("xray"):
            return -1
        if not any(term in name for term in os_terms):
            return -1
        if any(skip in name for skip in _foreign_os_skip_terms(os_token)):
            return -1
        if host_arch == "arm64":
            if not any(term in name for term in ("arm64", "aarch64", "arm64-v8a")):
                return -1
            return 20
        if any(term in name for term in ("arm64", "aarch64", "arm64-v8a")):
            return -1
        if any(term in name for term in ("64", "amd64", "x64", "x86_64")):
            return 20
        return -1

    candidates = [(score(asset), asset) for asset in assets]
    candidates = [item for item in candidates if item[0] >= 0]
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1] if candidates else None


def _asset_sha256(asset: dict[str, Any]) -> str | None:
    digest = str(asset.get("digest") or "").strip().lower()
    if not digest:
        return None
    algorithm, separator, value = digest.partition(":")
    if separator != ":" or algorithm != "sha256" or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise RuntimeError("GitHub release 资源摘要格式无效")
    return value


def _download_asset(
    asset: dict[str, Any],
    target: Path,
    *,
    retry_count: int = DOWNLOAD_RETRY_COUNT,
    retry_backoff: float = DOWNLOAD_RETRY_BACKOFF,
) -> None:
    url = _asset_url(asset)
    if not url:
        raise RuntimeError("GitHub release 资源缺少下载地址")
    _require_https_url(url, "GitHub release 资源")
    expected_sha256 = _asset_sha256(asset)
    expected_size = _optional_nonnegative_int(asset.get("size"), "GitHub release 资源大小")
    if expected_size is not None and expected_size > DOWNLOAD_RESPONSE_LIMIT:
        raise RuntimeError(f"资源超过 {DOWNLOAD_RESPONSE_LIMIT} 字节限制")
    attempts = max(1, retry_count)
    target.parent.mkdir(parents=True, exist_ok=True)
    last_error: BaseException | None = None

    for attempt in range(attempts):
        part_path = target.with_name(f".{target.name}.{os.getpid()}.{secrets.token_hex(4)}.part")
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "ProxyBridge/0.1"})
            with _open_https_url(request, timeout=600) as response:
                _response_https_url(response, url)
                headers = getattr(response, "headers", {})
                declared_size = _optional_nonnegative_int(headers.get("Content-Length"), "下载响应长度")
                if declared_size is not None and declared_size > DOWNLOAD_RESPONSE_LIMIT:
                    raise RuntimeError(f"下载超过 {DOWNLOAD_RESPONSE_LIMIT} 字节限制")
                digest = hashlib.sha256()
                downloaded = 0
                with part_path.open("wb") as out:
                    if hasattr(os, "fchmod"):
                        try:
                            os.fchmod(out.fileno(), 0o600)
                        except OSError:
                            pass
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        out.write(chunk)
                        digest.update(chunk)
                        downloaded += len(chunk)
                        if downloaded > DOWNLOAD_RESPONSE_LIMIT:
                            raise RuntimeError(f"下载超过 {DOWNLOAD_RESPONSE_LIMIT} 字节限制")
                    out.flush()
                    os.fsync(out.fileno())
            if declared_size is not None and downloaded != declared_size:
                raise RuntimeError(f"下载长度不匹配: 期望 {declared_size}，实际 {downloaded}")
            if expected_size is not None and downloaded != expected_size:
                raise RuntimeError(f"资源长度不匹配: 期望 {expected_size}，实际 {downloaded}")
            actual_sha256 = digest.hexdigest()
            if expected_sha256 and not hmac.compare_digest(actual_sha256, expected_sha256):
                raise RuntimeError("资源 SHA-256 校验失败")
            part_path.replace(target)
            try:
                target.chmod(0o600)
            except OSError:
                pass
            _fsync_directory(target.parent)
            return
        except urllib.error.HTTPError as error:
            last_error = error
            if error.code not in {408, 429, 500, 502, 503, 504}:
                raise
        except (urllib.error.URLError, OSError, RuntimeError) as error:
            last_error = error
        finally:
            part_path.unlink(missing_ok=True)
        if attempt + 1 < attempts and retry_backoff > 0:
            time.sleep(retry_backoff * (2**attempt))

    raise RuntimeError(f"下载资源失败（已重试 {attempts} 次）: {last_error}") from last_error


def _safe_extract_zip(archive_path: Path, dest: Path) -> None:
    root = dest.resolve()
    with zipfile.ZipFile(archive_path) as archive:
        members = archive.infolist()
        if len(members) > ARCHIVE_MEMBER_LIMIT:
            raise RuntimeError("压缩包文件数量过多")
        extracted_size = 0
        for member in members:
            extracted_size += max(0, int(member.file_size))
            if extracted_size > ARCHIVE_EXTRACTED_SIZE_LIMIT:
                raise RuntimeError("压缩包解压后体积超过限制")
            target = (dest / member.filename).resolve()
            if target != root and root not in target.parents:
                raise RuntimeError("压缩包包含非法路径")
        archive.extractall(dest)
        # zipfile 不会可靠恢复 Unix 可执行位；Linux 指纹内核的 chrome 与
        # chrome-wrapper 必须保持可执行，否则下载成功后 worker 仍无法启动。
        executable_names = {"chrome", "chromium", "chrome-wrapper"}
        for member in members:
            if member.is_dir():
                continue
            target = (dest / member.filename).resolve()
            archived_mode = (member.external_attr >> 16) & 0o777
            if archived_mode:
                target.chmod(archived_mode)
            if target.name in executable_names and os.name != "nt" and not os.access(target, os.X_OK):
                target.chmod(stat.S_IMODE(target.stat().st_mode) | 0o755)


def _safe_extract_tar(archive_path: Path, dest: Path) -> None:
    root = dest.resolve()
    with tarfile.open(archive_path) as archive:
        members = archive.getmembers()
        if len(members) > ARCHIVE_MEMBER_LIMIT:
            raise RuntimeError("压缩包文件数量过多")
        extracted_size = 0
        for member in members:
            if not (member.isfile() or member.isdir()):
                raise RuntimeError("压缩包包含非法类型")
            if member.isfile():
                extracted_size += max(0, int(member.size))
                if extracted_size > ARCHIVE_EXTRACTED_SIZE_LIMIT:
                    raise RuntimeError("压缩包解压后体积超过限制")
            target = (dest / member.name).resolve()
            if target != root and root not in target.parents:
                raise RuntimeError("压缩包包含非法路径")
        archive.extractall(dest)


def _extract_archive(archive_path: Path, dest: Path) -> None:
    name = archive_path.name.lower()
    try:
        if name.endswith(".zip"):
            _safe_extract_zip(archive_path, dest)
        elif name.endswith((".tar.gz", ".tgz", ".tar.xz", ".tar")):
            _safe_extract_tar(archive_path, dest)
        else:
            raise RuntimeError(f"不支持的核心压缩包格式:{archive_path.name}")
    except (zipfile.BadZipFile, tarfile.TarError, EOFError) as error:
        raise RuntimeError("压缩包损坏，或文件内容与扩展名不匹配") from error


def _find_extracted_file(root: Path, names: set[str]) -> Path | None:
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.name in names:
            return path
    return None


def _stage_install_copy(source: Path, dest: Path, mode: int) -> Path:
    dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp_path = dest.with_name(f".{dest.name}.install-{secrets.token_hex(6)}")
    try:
        shutil.copy2(source, temp_path)
        temp_path.chmod(mode)
        # Windows 的 fsync 需要可写句柄；只读句柄会报 Bad file descriptor。
        with temp_path.open("rb+") as handle:
            os.fsync(handle.fileno())
        return temp_path
    except BaseException:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _stage_install_bytes(data: bytes, dest: Path, mode: int) -> Path:
    dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp_path = dest.with_name(f".{dest.name}.install-{secrets.token_hex(6)}")
    _write_private_atomic(
        temp_path,
        data,
        temp_path=temp_path.with_name(f"{temp_path.name}.tmp"),
        mode=mode,
    )
    return temp_path


def _replace_install_files(staged_targets: list[tuple[Path, Path]]) -> None:
    targets = [target for _, target in staged_targets]
    if len(set(targets)) != len(targets):
        raise ValueError("核心安装目标不能重复")
    backup_token = secrets.token_hex(6)
    backups: dict[Path, Path | None] = {}
    installed: set[Path] = set()
    parents = {target.parent for _, target in staged_targets}
    try:
        for staged, target in staged_targets:
            backup: Path | None = None
            if target.exists() or target.is_symlink():
                backup = target.with_name(f".{target.name}.backup-{backup_token}")
                target.replace(backup)
            backups[target] = backup
            staged.replace(target)
            installed.add(target)
        for parent in parents:
            _fsync_directory(parent)
    except BaseException as error:
        rollback_errors: list[BaseException] = []
        for target, backup in reversed(list(backups.items())):
            try:
                if target in installed and (target.exists() or target.is_symlink()):
                    target.unlink()
                if backup is not None and (backup.exists() or backup.is_symlink()):
                    backup.replace(target)
            except BaseException as rollback_error:
                rollback_errors.append(rollback_error)
        for parent in parents:
            _fsync_directory(parent)
        if rollback_errors:
            details = "; ".join(str(item) for item in rollback_errors)
            raise RuntimeError(f"核心安装失败且旧版本恢复不完整: {details}") from error
        raise
    else:
        for backup in backups.values():
            if backup is None:
                continue
            try:
                backup.unlink(missing_ok=True)
            except OSError:
                pass
        for parent in parents:
            _fsync_directory(parent)
    finally:
        for staged, _ in staged_targets:
            try:
                staged.unlink(missing_ok=True)
            except OSError:
                pass


def _install_files_atomically(
    copies: list[tuple[Path, Path, int]],
    generated: list[tuple[Path, bytes, int]],
) -> None:
    staged_targets: list[tuple[Path, Path]] = []
    try:
        for source, target, mode in copies:
            staged_targets.append((_stage_install_copy(source, target, mode), target))
        for target, data, mode in generated:
            staged_targets.append((_stage_install_bytes(data, target, mode), target))
        _replace_install_files(staged_targets)
    finally:
        for staged, _ in staged_targets:
            try:
                staged.unlink(missing_ok=True)
            except OSError:
                pass


def _install_executable(source: Path, dest: Path) -> None:
    _install_files_atomically([(source, dest, 0o755)], [])


def _xray_wrapper_data() -> bytes:
    return (
        '#!/bin/sh\n'
        'HERE="$(cd "$(dirname "$0")" && pwd)"\n'
        'exec "$HERE/xray-core" "$@"\n'
    ).encode("utf-8")


def _validate_uploaded_core_binary(core: str, binary: Path) -> str:
    """执行上传包中的 version 子命令，拒绝错误平台或伪装成核心的文件。"""
    if os.name != "nt":
        binary.chmod(stat.S_IMODE(binary.stat().st_mode) | 0o755)
    try:
        version = _core_version_line(str(binary), core)
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError(f"上传的 {core} 无法在当前系统运行，请检查 OS 与 CPU 架构") from error
    marker = "sing-box" if core == "sing-box" else "xray"
    if marker not in version.casefold():
        raise RuntimeError(f"上传包中的可执行文件不是有效的 {core}")
    return version


def _install_core_from_archive(
    core: str,
    archive_path: Path,
    source_name: str,
    tag: str,
    *,
    validate_binary: bool = False,
) -> str:
    if core not in {"sing-box", "xray"}:
        raise ValueError("核心只能是 sing-box 或 xray")
    ensure_dirs()
    with tempfile.TemporaryDirectory(prefix=f"proxybridge-{core}-", dir=str(proxy_core_root())) as temp_dir:
        extract_root = Path(temp_dir) / "extract"
        extract_root.mkdir()
        _extract_archive(archive_path, extract_root)
        if core == "sing-box":
            binary = _find_extracted_file(extract_root, set(_core_binary_candidates("sing-box")))
            if not binary:
                raise RuntimeError("sing-box 压缩包中未找到 sing-box 可执行文件")
            validated_version = _validate_uploaded_core_binary(core, binary) if validate_binary else ""
            target = proxy_core_bin_dir() / ("sing-box.exe" if os.name == "nt" else "sing-box")
            version_path = proxy_core_root() / "sing-box.version"
            version_data = f"{tag or validated_version}\n{source_name}\n".encode("utf-8")
            _install_files_atomically(
                [(binary, target, 0o755)],
                [(version_path, version_data, 0o600)],
            )
            return str(target)

        binary = _find_extracted_file(
            extract_root,
            set(_core_binary_candidates("xray") + _core_binary_candidates("xray-core")),
        )
        if not binary:
            raise RuntimeError("Xray 压缩包中未找到 xray 可执行文件")
        validated_version = _validate_uploaded_core_binary(core, binary) if validate_binary else ""
        version_path = proxy_core_root() / "xray.version"
        version_data = f"{tag or validated_version}\n{source_name}\n".encode("utf-8")
        if os.name == "nt":
            target = proxy_core_bin_dir() / "xray.exe"
            _install_files_atomically(
                [(binary, target, 0o755)],
                [(version_path, version_data, 0o600)],
            )
            return str(target)

        core_target = proxy_core_bin_dir() / "xray-core"
        wrapper_target = proxy_core_bin_dir() / "xray"
        _install_files_atomically(
            [(binary, core_target, 0o755)],
            [
                (wrapper_target, _xray_wrapper_data(), 0o755),
                (version_path, version_data, 0o600),
            ],
        )
        return str(wrapper_target)


def _install_core_from_asset(core: str, asset: dict[str, Any], tag: str) -> str:
    ensure_dirs()
    with tempfile.TemporaryDirectory(prefix=f"proxybridge-{core}-", dir=str(proxy_core_root())) as temp_dir:
        archive_path = Path(temp_dir) / _safe_asset_name(asset)
        _download_asset(asset, archive_path)
        return _install_core_from_archive(core, archive_path, _asset_name(asset), tag)


def ensure_core_installed(core: str) -> str:
    paths = core_paths()
    if paths.get(core):
        return str(paths[core])
    if core not in {"sing-box", "xray"}:
        raise ValueError("核心只能是 sing-box 或 xray")

    with CORE_INSTALL_LOCK:
        paths = core_paths()
        if paths.get(core):
            return str(paths[core])
        host_arch = host_arch_token()
        host_os = host_os_token()
        if core == "sing-box":
            release = _github_json(SING_BOX_RELEASES_API)
            asset = _pick_sing_box_asset(release if isinstance(release, dict) else {}, host_arch, host_os)
        else:
            release = _github_json(XRAY_RELEASES_API)
            asset = _pick_xray_asset(release if isinstance(release, dict) else {}, host_arch, host_os)
        if not asset:
            raise RuntimeError(f"未找到适配 {host_os}/{host_arch} 的 {core} release 资源")
        tag = str(release.get("tag_name") or release.get("name") or "")
        log_event(f"正在下载 {core}:{_asset_name(asset)}", category="核心")
        installed = _install_core_from_asset(core, asset, tag)
        log_event(f"已安装 {core}:{installed}", category="核心")
        return installed


def ensure_proxy_cores_installed() -> None:
    for core in ("sing-box", "xray"):
        ensure_core_installed(core)


def start_core_auto_install() -> None:
    if CORE_AUTO_INSTALL_STARTED.is_set():
        return
    if all(core_paths().get(core) for core in ("sing-box", "xray")):
        return
    CORE_AUTO_INSTALL_STARTED.set()

    def worker() -> None:
        try:
            ensure_proxy_cores_installed()
        except Exception as exc:
            log_event(f"自动安装代理核心失败:{exc}", level="error", category="核心")

    threading.Thread(target=worker, name="proxy-core-auto-install", daemon=True).start()


def chrome_path() -> str | None:
    env_path = os.environ.get("PROXY_BRIDGE_CHROME")
    candidates: list[Path] = []
    if env_path:
        candidates.append(Path(env_path).expanduser())
    os_token = host_os_token()
    if os_token == "darwin":
        candidates.extend(
            [
                Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
                Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            ]
        )
    elif os_token == "windows":
        local_app = os.environ.get("LOCALAPPDATA", "")
        program_files = os.environ.get("PROGRAMFILES", r"C:\Program Files")
        program_files_x86 = os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")
        for root in (local_app, program_files, program_files_x86):
            if not root:
                continue
            candidates.append(Path(root) / "Google" / "Chrome" / "Application" / "chrome.exe")
            candidates.append(Path(root) / "Chromium" / "Application" / "chrome.exe")
    else:
        candidates.extend(
            [
                Path("/usr/bin/google-chrome"),
                Path("/usr/bin/google-chrome-stable"),
                Path("/usr/bin/chromium"),
                Path("/usr/bin/chromium-browser"),
                Path("/snap/bin/chromium"),
            ]
        )
    for candidate in candidates:
        if _is_executable_file(candidate):
            return str(candidate)
    for name in (
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium-browser",
        "chrome",
        "chrome.exe",
    ):
        found = shutil.which(name)
        if found:
            return found
    return None


def fingerprint_chromium_root() -> Path:
    # 指纹内核(fingerprint-chromium,基于 Ungoogled Chromium)统一安装在工作目录下,
    # 与本机 Google Chrome 完全分离,避免污染用户日常浏览器。
    return WORK_ROOT / "fingerprint-chromium"


def _bundle_executable(app_dir: Path) -> Path | None:
    # 从 macOS .app 包中定位真正的可执行二进制:Contents/MacOS/ 下的第一个可执行文件。
    macos_dir = app_dir / "Contents" / "MacOS"
    if not macos_dir.is_dir():
        return None
    for entry in sorted(macos_dir.iterdir()):
        if entry.is_file() and os.access(entry, os.X_OK):
            return entry
    return None


# 多版本指纹内核：每个版本安装在 fingerprint-chromium/versions/<内核 ID>/，
# 该目录下的 .proxybridge-kernel.json 记录版本、来源与可执行文件相对路径。
# 升级前直接装在 fingerprint-chromium/ 下的内核视为 ID「legacy」继续可用。
FINGERPRINT_KERNEL_METADATA_FILE = ".proxybridge-kernel.json"
# 设备本地的默认内核选择（JSON：{"id": "<内核 ID>"}；空 ID 表示自动选择最新版本），不参与 WebDAV 同步。
FINGERPRINT_DEFAULT_KERNEL_FILE = ".proxybridge-default-kernel.json"
FINGERPRINT_KERNEL_LIST_CACHE: dict[str, Any] = {"key": None, "kernels": None, "checked_at": 0.0}


def fingerprint_kernel_versions_root() -> Path:
    return fingerprint_chromium_root() / "versions"


def invalidate_fingerprint_chromium_cache() -> None:
    with PATH_CACHE_LOCK:
        FINGERPRINT_CHROMIUM_CACHE.update({"key": None, "path": None, "checked_at": 0.0})
        FINGERPRINT_KERNEL_LIST_CACHE.update({"key": None, "kernels": None, "checked_at": 0.0})


def _fingerprint_chromium_cache_key() -> tuple[str | None, str, str]:
    return (
        os.environ.get("PROXY_BRIDGE_FP_CHROMIUM"),
        str(fingerprint_chromium_root()),
        str(Path.home() / "Applications"),
    )


def _fingerprint_binary_names() -> tuple[str, ...]:
    if os.name == "nt":
        return ("chrome.exe", "chromium.exe", "Chromium.exe", "chrome", "chromium")
    return ("chrome", "chromium", "Chromium", "chrome.exe", "chromium.exe")


def _resolve_fingerprint_executable_in_root(root: Path) -> str | None:
    if not root.exists():
        return None
    if root.is_file() and _is_executable_file(root):
        return str(root)
    if root.suffix == ".app" and root.is_dir():
        exe = _bundle_executable(root)
        if exe:
            return str(exe)
    if root.is_dir():
        direct_app = root / "Chromium.app"
        app_candidates = [direct_app] if direct_app.is_dir() else []
        app_candidates.extend(app_dir for app_dir in root.glob("**/Chromium.app") if app_dir != direct_app)
        for app_dir in app_candidates:
            exe = _bundle_executable(app_dir)
            if exe:
                return str(exe)
        for name in _fingerprint_binary_names():
            direct = root / name
            if _is_executable_file(direct):
                return str(direct)
        for name in _fingerprint_binary_names():
            for candidate in sorted(root.rglob(name)):
                if _is_executable_file(candidate):
                    return str(candidate)
    return None


def _resolve_legacy_fingerprint_executable() -> str | None:
    # 升级前的安装位直接位于 fingerprint-chromium/ 下；跳过 versions/ 与安装临时目录。
    root = fingerprint_chromium_root()
    if root.is_dir():
        preferred = [root / "Chromium.app", root / "chromium"]
        others = sorted(
            child
            for child in root.iterdir()
            if child not in preferred and child.name != "versions" and not child.name.startswith((".", "_"))
        )
        binary_names = set(_fingerprint_binary_names())
        for child in [*preferred, *others]:
            # Windows 上任何文件都视为可执行；根目录的散落文件只认浏览器可执行文件名，
            # 避免把下载残留的安装包误认成内核。
            if not child.exists() or (child.is_file() and (child.name not in binary_names or not _is_executable_file(child))):
                continue
            resolved = _resolve_fingerprint_executable_in_root(child)
            if resolved:
                return resolved
    # 系统应用目录只检查常见直接安装位置。递归扫描整个 /Applications 在状态轮询中代价太高。
    if host_os_token() == "darwin":
        for app_dir in (Path("/Applications/Chromium.app"), Path.home() / "Applications/Chromium.app"):
            exe = _bundle_executable(app_dir)
            if exe:
                return str(exe)
    return None


def _read_fingerprint_kernel_metadata(kernel_dir: Path) -> dict[str, Any]:
    try:
        loaded = json.loads((kernel_dir / FINGERPRINT_KERNEL_METADATA_FILE).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _versioned_fingerprint_executable(kernel_dir: Path, metadata: dict[str, Any]) -> str | None:
    relative = str(metadata.get("executable") or "")
    if relative:
        candidate = (kernel_dir / relative).resolve()
        try:
            candidate.relative_to(kernel_dir.resolve())
        except ValueError:
            return None
        return str(candidate) if _is_executable_file(candidate) else None
    # 元数据缺失时退回浅层查找；不做递归扫描，避免状态轮询遍历整个内核目录。
    for child in (kernel_dir / "Chromium.app", kernel_dir / "chromium"):
        if child.suffix == ".app":
            exe = _bundle_executable(child)
            if exe:
                return str(exe)
        else:
            for name in _fingerprint_binary_names():
                if _is_executable_file(child / name):
                    return str(child / name)
    return None


def _app_bundle_version(executable: str) -> str:
    app_root = next((parent for parent in Path(executable).parents if parent.suffix == ".app"), None)
    if app_root is None:
        return ""
    try:
        import plistlib

        with (app_root / "Contents" / "Info.plist").open("rb") as handle:
            info = plistlib.load(handle)
    except Exception:
        return ""
    return str(info.get("CFBundleShortVersionString") or "")[:40] if isinstance(info, dict) else ""


def read_default_fingerprint_kernel_id() -> str:
    try:
        loaded = json.loads((fingerprint_chromium_root() / FINGERPRINT_DEFAULT_KERNEL_FILE).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return ""
    if not isinstance(loaded, dict):
        return ""
    return browser_fingerprint.normalize_kernel_id(loaded.get("id"), strict=False)


def _scan_fingerprint_kernels() -> list[dict[str, Any]]:
    kernels: list[dict[str, Any]] = []
    env_path = os.environ.get("PROXY_BRIDGE_FP_CHROMIUM")
    if env_path:
        resolved = _resolve_fingerprint_executable_in_root(Path(env_path).expanduser())
        if resolved:
            kernels.append(
                {
                    "id": "env",
                    "version": _app_bundle_version(resolved),
                    "label": "环境变量 PROXY_BRIDGE_FP_CHROMIUM",
                    "path": resolved,
                    "source": "env",
                }
            )
    versions_root = fingerprint_kernel_versions_root()
    versioned: list[dict[str, Any]] = []
    if versions_root.is_dir():
        for kernel_dir in versions_root.iterdir():
            if not kernel_dir.is_dir() or browser_fingerprint.KERNEL_ID_PATTERN.fullmatch(kernel_dir.name) is None:
                continue
            if kernel_dir.name in {"env", "legacy"}:
                continue
            metadata = _read_fingerprint_kernel_metadata(kernel_dir)
            executable = _versioned_fingerprint_executable(kernel_dir, metadata)
            if not executable:
                continue
            versioned.append(
                {
                    "id": kernel_dir.name,
                    "version": str(metadata.get("version") or "")[:40],
                    "label": str(metadata.get("version") or kernel_dir.name)[:64],
                    "path": executable,
                    "source": str(metadata.get("source") or "")[:40],
                    "asset": str(metadata.get("asset") or "")[:180],
                    "installed_at": safe_int(metadata.get("installed_at"), 0) or None,
                    "arch": str(metadata.get("arch") or "")[:20],
                }
            )
    versioned.sort(
        key=lambda item: (browser_fingerprint.version_sort_key(item["version"] or item["id"]), item["id"]),
        reverse=True,
    )
    kernels.extend(versioned)
    legacy = _resolve_legacy_fingerprint_executable()
    if legacy:
        kernels.append(
            {
                "id": "legacy",
                "version": _app_bundle_version(legacy),
                "label": "早期安装",
                "path": legacy,
                "source": "legacy",
            }
        )
    return kernels


def list_fingerprint_kernels(force_refresh: bool = False) -> list[dict[str, Any]]:
    key = (*_fingerprint_chromium_cache_key(), read_default_fingerprint_kernel_id())
    now = time.monotonic()
    with PATH_CACHE_LOCK:
        cached = FINGERPRINT_KERNEL_LIST_CACHE.get("kernels")
        if (
            not force_refresh
            and cached is not None
            and FINGERPRINT_KERNEL_LIST_CACHE.get("key") == key
            and now - float(FINGERPRINT_KERNEL_LIST_CACHE.get("checked_at") or 0.0) < FINGERPRINT_CHROMIUM_CACHE_TTL
            and all(Path(str(item.get("path") or "")).exists() for item in cached)
        ):
            kernels = [dict(item) for item in cached]
        else:
            kernels = _scan_fingerprint_kernels()
            FINGERPRINT_KERNEL_LIST_CACHE.update({"key": key, "kernels": kernels, "checked_at": now})
            kernels = [dict(item) for item in kernels]
    default_id = _default_fingerprint_kernel_id(kernels)
    for item in kernels:
        item["default"] = item["id"] == default_id
    return kernels


def _default_fingerprint_kernel_id(kernels: list[dict[str, Any]]) -> str:
    # 默认内核优先级：环境变量 > 用户设定的默认版本 > 最新已安装版本 > 早期安装。
    ids = [str(item.get("id") or "") for item in kernels]
    if "env" in ids:
        return "env"
    configured = read_default_fingerprint_kernel_id()
    if configured and configured in ids:
        return configured
    return ids[0] if ids else ""


def fingerprint_kernel_ids() -> set[str]:
    return {str(item["id"]) for item in list_fingerprint_kernels()}


def _resolve_fingerprint_chromium_path() -> str | None:
    # 定位默认指纹内核可执行文件。优先级:环境变量 > 默认/最新版本 > 早期安装位 > 系统 Applications。
    # 指纹内核必须是打过补丁的 Chromium(而非本机 Google Chrome),否则无法在底层伪造指纹。
    kernels = list_fingerprint_kernels()
    default_id = _default_fingerprint_kernel_id(kernels)
    return next((str(item["path"]) for item in kernels if item["id"] == default_id), None)


def fingerprint_chromium_path(force_refresh: bool = False, kernel_id: str | None = None) -> str | None:
    if kernel_id:
        # 指定版本不走默认路径缓存；版本列表本身已有缓存。
        kernels = list_fingerprint_kernels(force_refresh=force_refresh)
        return next((str(item["path"]) for item in kernels if item["id"] == kernel_id), None)
    key = _fingerprint_chromium_cache_key()
    now = time.monotonic()
    with PATH_CACHE_LOCK:
        cached_path = FINGERPRINT_CHROMIUM_CACHE.get("path")
        if (
            not force_refresh
            and FINGERPRINT_CHROMIUM_CACHE.get("key") == key
            and now - float(FINGERPRINT_CHROMIUM_CACHE.get("checked_at") or 0.0) < FINGERPRINT_CHROMIUM_CACHE_TTL
            and (not cached_path or Path(str(cached_path)).exists())
        ):
            return str(cached_path) if cached_path else None

        if force_refresh:
            FINGERPRINT_KERNEL_LIST_CACHE.update({"key": None, "kernels": None, "checked_at": 0.0})
        resolved = _resolve_fingerprint_chromium_path()
        normalized = str(resolved) if resolved else None
        FINGERPRINT_CHROMIUM_CACHE.update({"key": key, "path": normalized, "checked_at": now})
        return normalized


def resolve_browser_kernel(kernel_version: str) -> tuple[str, str]:
    """返回 (内核 ID, 可执行路径)。固定版本缺失时报错而不是静默换内核，避免指纹漂移。"""
    if kernel_version:
        path = fingerprint_chromium_path(kernel_id=kernel_version)
        if not path:
            raise RuntimeError(
                f"该浏览器固定使用指纹内核 {kernel_version}，但本机未安装。"
                "请在「内核管理」安装该版本，或在浏览器配置中改为默认内核。"
            )
        return kernel_version, path
    path = fingerprint_chromium_path()
    if not path:
        raise RuntimeError(
            "未找到指纹内核 fingerprint-chromium。请点击「下载指纹内核」自动安装,"
            "或手动下载后设置 PROXY_BRIDGE_FP_CHROMIUM 指向 Chromium 可执行文件。"
        )
    kernels = list_fingerprint_kernels()
    kernel_id = next((str(item["id"]) for item in kernels if str(item["path"]) == path), "")
    return kernel_id, path


def allow_lan_access(state: dict[str, Any]) -> bool:
    if docker_mode():
        # Published Docker ports arrive through the container bridge, so the outer
        # proxy listener must accept RFC1918 clients inside the container.
        return True
    settings = state.get("settings")
    return bool(isinstance(settings, dict) and settings.get("allow_lan") is True)


def proxy_listen_host(state: dict[str, Any]) -> str:
    return LAN_PROXY_LISTEN_HOST if allow_lan_access(state) else LOCAL_PROXY_LISTEN_HOST


def _usable_lan_ipv4(value: Any) -> str | None:
    try:
        address = ipaddress.IPv4Address(str(value or "").strip())
    except ipaddress.AddressValueError:
        return None
    if (
        not any(address in network for network in PRIVATE_IPV4_NETWORKS)
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    ):
        return None
    return str(address)


def proxy_client_allowed(value: Any, allowed_cidrs: Any = None) -> bool:
    try:
        address = ipaddress.IPv4Address(str(value or "").strip())
    except ipaddress.AddressValueError:
        return False
    if address.is_loopback:
        return True
    networks = normalize_lan_cidrs(allowed_cidrs)
    return any(address in ipaddress.IPv4Network(network) for network in networks)


def _remote_browser_session_allows_proxy(
    sessions: Any,
    address: ipaddress.IPv4Address,
    proxy_port: int,
) -> bool:
    if not isinstance(sessions, dict):
        return False
    for session in sessions.values():
        if session.get("status") not in REMOTE_BROWSER_LIVE_SESSION_STATUSES:
            continue
        try:
            worker_ip = ipaddress.IPv4Address(str(session.get("worker_ip") or ""))
        except ipaddress.AddressValueError:
            continue
        if worker_ip == address and safe_int(session.get("proxy_port"), 0) == proxy_port:
            return True
    return False


def remote_browser_worker_client(value: Any, proxy_port: Any = None) -> bool:
    """只允许 Broker 报告的 worker IP 使用分配给它的节点代理端口。"""
    if not remote_browser_feature_enabled():
        return False
    try:
        address = ipaddress.IPv4Address(str(value or "").strip())
    except ipaddress.AddressValueError:
        return False
    expected_port = safe_int(proxy_port, 0)
    if expected_port <= 0:
        return False
    sessions, _error = remote_browser_sessions()
    if _remote_browser_session_allows_proxy(sessions, address, expected_port):
        return True
    if not any_remote_browser_start_pending():
        return False
    # 停止后重新启动时，状态轮询可能在新 worker 创建前缓存了空快照。
    # 仅在启动事务仍有效时绕过该 TTL 缓存一次，继续按精确 worker IP 与端口授权。
    refreshed_sessions, _refresh_error = remote_browser_sessions(force=True)
    return _remote_browser_session_allows_proxy(refreshed_sessions, address, expected_port)


def register_active_proxy_client(
    *,
    proxy: "Socks5LogProxy",
    client: socket.socket,
    address: tuple[str, int],
    is_lan: bool,
    max_connections: int,
) -> str | None:
    with PROXY_CLIENT_LOCK:
        if is_lan:
            active_lan = sum(1 for item in ACTIVE_PROXY_CLIENTS.values() if item.get("is_lan"))
            if active_lan >= max_connections:
                return None
        client_id = stable_id("proxy-client", proxy.bridge_id, address[0], str(address[1]), str(time.time_ns()))
        ACTIVE_PROXY_CLIENTS[client_id] = {
            "id": client_id,
            "bridge_id": proxy.bridge_id,
            "node_id": proxy.node_id,
            "node_name": proxy.node_name,
            "address": str(address[0]),
            "port": safe_int(address[1], 0),
            "protocol": "detecting",
            "connected_at": now_ts(),
            "is_lan": is_lan,
            "socket": client,
        }
        return client_id


def update_active_proxy_client(client_id: str, **updates: Any) -> None:
    with PROXY_CLIENT_LOCK:
        client = ACTIVE_PROXY_CLIENTS.get(client_id)
        if client is not None:
            client.update(updates)


def active_proxy_client_id(client_socket: socket.socket) -> str:
    with PROXY_CLIENT_LOCK:
        return next(
            (
                client_id
                for client_id, item in ACTIVE_PROXY_CLIENTS.items()
                if item.get("socket") is client_socket
            ),
            "",
        )


def unregister_active_proxy_client(client_id: str) -> None:
    with PROXY_CLIENT_LOCK:
        ACTIVE_PROXY_CLIENTS.pop(client_id, None)


def active_proxy_clients_payload() -> dict[str, Any]:
    with PROXY_CLIENT_LOCK:
        clients = [
            {key: value for key, value in item.items() if key != "socket"}
            for item in ACTIVE_PROXY_CLIENTS.values()
        ]
    clients.sort(key=lambda item: (safe_int(item.get("connected_at"), 0), str(item.get("id") or "")))
    return {
        "count": len(clients),
        "lan_count": sum(1 for item in clients if item.get("is_lan")),
        "clients": clients,
    }


def kick_proxy_client(client_id: str) -> dict[str, Any]:
    with PROXY_CLIENT_LOCK:
        item = ACTIVE_PROXY_CLIENTS.pop(str(client_id or ""), None)
    if item is None:
        raise ValueError("客户端连接不存在或已断开")
    client = item.get("socket")
    if isinstance(client, socket.socket):
        try:
            client.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        client.close()
    else:
        try:
            client.close()
        except (AttributeError, OSError):
            pass
    return {"ok": True, "id": str(item.get("id") or client_id)}


def _discover_lan_ipv4_addresses() -> list[str]:
    """Return usable IPv4 addresses, preferring the interface used by the default route.

    The UDP sockets are only used for local route selection; no packet is sent.
    Hostname resolution supplies additional active-interface addresses on multi-homed Macs.
    """
    candidates: list[str] = []

    def add(value: Any) -> None:
        normalized = _usable_lan_ipv4(value)
        if normalized and normalized not in candidates:
            candidates.append(normalized)

    # On macOS, prefer addresses on physical Ethernet/Wi-Fi interfaces. The default
    # route may point at a VPN/TUN interface whose address is not reachable by LAN peers.
    if sys.platform == "darwin" and Path("/sbin/ifconfig").exists():
        try:
            result = subprocess.run(
                ["/sbin/ifconfig"],
                capture_output=True,
                text=True,
                timeout=1.0,
                check=False,
            )
            interface = ""
            interface_up = False
            for line in result.stdout.splitlines():
                header = re.match(r"^([A-Za-z0-9._-]+):\s+flags=[^<]*<([^>]*)>", line)
                if header:
                    interface = header.group(1)
                    interface_up = "UP" in header.group(2).split(",")
                    continue
                address_match = re.match(r"\s+inet\s+([0-9.]+)\b", line)
                if interface_up and interface.startswith("en") and address_match:
                    add(address_match.group(1))
        except (OSError, subprocess.SubprocessError):
            pass

    for target in (("192.0.2.1", 9), ("198.51.100.1", 9)):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                probe.connect(target)
                add(probe.getsockname()[0])
        except OSError:
            continue
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET, socket.SOCK_STREAM):
            add(info[4][0])
    except OSError:
        pass
    return candidates


def lan_ipv4_addresses(force_refresh: bool = False) -> list[str]:
    now = time.monotonic()
    with LAN_ADDRESS_CACHE_LOCK:
        checked_at = float(LAN_ADDRESS_CACHE.get("checked_at") or 0.0)
        if not force_refresh and now - checked_at < LAN_ADDRESS_CACHE_TTL:
            return list(LAN_ADDRESS_CACHE.get("addresses") or [])
        addresses = _discover_lan_ipv4_addresses()
        LAN_ADDRESS_CACHE.update({"addresses": list(addresses), "checked_at": now})
        return addresses


def bridge_network_payload(
    bridge: dict[str, Any],
    *,
    allow_lan: bool,
    lan_addresses: list[str],
) -> dict[str, Any]:
    result = dict(bridge)
    port = safe_int(bridge.get("port"), 0)
    shared_addresses = lan_addresses if allow_lan and port > 0 else []
    result.update(
        {
            "listen_host": LAN_PROXY_LISTEN_HOST if allow_lan else LOCAL_PROXY_LISTEN_HOST,
            "lan_socks_urls": [f"socks5://{address}:{port}" for address in shared_addresses],
            "lan_http_urls": [f"http://{address}:{port}" for address in shared_addresses],
            "lan_socks_url": f"socks5://{shared_addresses[0]}:{port}" if shared_addresses else "",
            "lan_http_url": f"http://{shared_addresses[0]}:{port}" if shared_addresses else "",
        }
    )
    return result


def network_settings_payload(
    state: dict[str, Any] | None = None,
    *,
    lan_addresses: list[str] | None = None,
) -> dict[str, Any]:
    if state is None:
        with STATE_LOCK:
            state = load_state()
    docker_managed = docker_mode()
    allow_lan = allow_lan_access(state)
    addresses = [] if docker_managed else (
        list(lan_addresses) if lan_addresses is not None else lan_ipv4_addresses()
    )
    proxy_urls: list[dict[str, Any]] = []
    if docker_managed:
        for bridge in state.get("bridges", []):
            port = safe_int(bridge.get("port"), 0)
            if bridge.get("status") not in ACTIVE_BRIDGE_STATUSES or port <= 0:
                continue
            proxy_urls.append(
                {
                    "bridge_id": str(bridge.get("id") or ""),
                    "node_name": str(bridge.get("node_name") or ""),
                    "port": port,
                    "socks_url": str(bridge.get("socks_url") or f"socks5://127.0.0.1:{port}"),
                    "http_url": str(bridge.get("http_url") or f"http://127.0.0.1:{port}"),
                    "socks_urls": [str(bridge.get("socks_url") or f"socks5://127.0.0.1:{port}")],
                    "http_urls": [str(bridge.get("http_url") or f"http://127.0.0.1:{port}")],
                }
            )
    elif allow_lan:
        for bridge in state.get("bridges", []):
            port = safe_int(bridge.get("port"), 0)
            if bridge.get("status") not in ACTIVE_BRIDGE_STATUSES or port <= 0:
                continue
            proxy_urls.append(
                {
                    "bridge_id": str(bridge.get("id") or ""),
                    "node_name": str(bridge.get("node_name") or ""),
                    "port": port,
                    "socks_url": f"socks5://{addresses[0]}:{port}" if addresses else "",
                    "http_url": f"http://{addresses[0]}:{port}" if addresses else "",
                    "socks_urls": [f"socks5://{address}:{port}" for address in addresses],
                    "http_urls": [f"http://{address}:{port}" for address in addresses],
                }
            )
    security = public_lan_security((state.get("settings") or {}).get("lan_security"))
    return {
        "allow_lan": allow_lan,
        "listen_host": LAN_PROXY_LISTEN_HOST if allow_lan else LOCAL_PROXY_LISTEN_HOST,
        "lan_addresses": addresses,
        "primary_lan_address": addresses[0] if addresses else "",
        "console_shared": False,
        "client_scope": "docker_published_ports" if docker_managed else "loopback_and_private_ipv4",
        "configurable": not docker_managed,
        "range_configurable": not docker_managed,
        "security_configurable": True,
        "docker_managed": docker_managed,
        "proxy_urls": proxy_urls,
        "security": security,
        **active_proxy_clients_payload(),
    }


def status_payload() -> dict[str, Any]:
    remote_enabled = remote_browser_feature_enabled()
    remote_sessions, remote_error = (
        remote_browser_sessions() if remote_enabled else ({}, "")
    )
    with SHUTDOWN_LOCK:
        return _status_payload(
            remote_sessions_snapshot=remote_sessions,
            remote_error_snapshot=remote_error,
        )


def _status_payload(
    *,
    remote_sessions_snapshot: Any = REMOTE_BROWSER_SESSIONS_UNSET,
    remote_error_snapshot: str = "",
) -> dict[str, Any]:
    synchronize_process_state()
    browser_enabled = browser_feature_enabled()
    docker_enabled = docker_mode()
    remote_browser_enabled = remote_browser_feature_enabled()
    if remote_browser_enabled and remote_sessions_snapshot is REMOTE_BROWSER_SESSIONS_UNSET:
        remote_sessions, remote_error = remote_browser_sessions()
    elif remote_browser_enabled:
        remote_sessions = remote_sessions_snapshot
        remote_error = str(remote_error_snapshot or "")
    else:
        remote_sessions, remote_error = {}, ""
    remote_runtime = {
        "configured": remote_browser_enabled,
        "available": remote_sessions is not None if remote_browser_enabled else False,
        "active_sessions": (
            sum(
                item.get("status") in REMOTE_BROWSER_LIVE_SESSION_STATUSES
                for item in remote_sessions.values()
            )
            if isinstance(remote_sessions, dict)
            else 0
        ),
        "error": remote_error,
    }
    lan_addresses = [] if docker_enabled else lan_ipv4_addresses()
    system_entries = system_log_entries()
    recent_errors = [item for item in system_entries
                     if item.get("level") == "error" and float(item.get("ts") or 0) >= time.time() - 86400]
    error_groups: dict[tuple[str, str], dict[str, Any]] = {}
    for entry in recent_errors:
        message = str(entry.get("message") or "")
        matched_node = re.search("「([^」]+)」", message)
        node_name = matched_node.group(1) if matched_node else ""
        category = str(entry.get("category") or "系统")
        key = (category, node_name)
        group = error_groups.setdefault(key, {"category": category, "node_name": node_name,
                                              "message": message, "count": 0})
        group["count"] += 1
    with STATE_LOCK:
        state = load_state()
        changed = sync_process_status(state)
        if browser_enabled:
            changed |= sync_browser_instance_status(
                state,
                remote_sessions_snapshot=remote_sessions,
            )
        token, token_changed = ensure_pool_token(state)
        members = pool_members(state, ready_only=False)
        if changed or token_changed:
            save_state(state)
        network = network_settings_payload(state, lan_addresses=lan_addresses)
        bridges = [
            bridge_network_payload(
                bridge,
                allow_lan=bool(network["allow_lan"]),
                lan_addresses=list(network["lan_addresses"]),
            )
            for bridge in state["bridges"]
        ]
        return {
            "platform": {
                "os": host_os_token(),
                "arch": host_arch_token(),
                "fingerprint_platform": fingerprint_platform_token() if browser_enabled else None,
                "work_root": str(WORK_ROOT),
                "shell_mode": runtime_mode(),
            },
            "runtime": {
                "mode": runtime_mode(),
                "docker": docker_enabled,
                "proxy_port_range": list(docker_proxy_port_range()) if docker_enabled else None,
            },
            "features": {
                "browser": browser_enabled,
                "browser_profile_sync": browser_enabled,
                "remote_browser": remote_browser_enabled,
                "browser_kernel_download": browser_enabled,
                # CDP 调试端口、Cookie 导入导出与「在运行中的浏览器打开网址」仅本机浏览器支持。
                "browser_cdp": browser_enabled and not remote_browser_enabled,
                "docker": docker_enabled,
            },
            "cores": core_paths(),
            "chrome": chrome_path() if browser_enabled and not remote_browser_enabled else None,
            "fingerprint_chromium": fingerprint_chromium_path() if browser_enabled else None,
            "fingerprint_kernels": list_fingerprint_kernels() if browser_enabled else [],
            "fingerprint_default_kernel": read_default_fingerprint_kernel_id() if browser_enabled else "",
            "browser_extensions": browser_extension_library() if browser_enabled else [],
            "fingerprint_check_sites": dict(FINGERPRINT_CHECK_SITES) if browser_enabled else {},
            "remote_browser": remote_runtime,
            "active_browsers": active_browser_node_ids_from_state(state) if browser_enabled else [],
            "browser_instances": public_browser_instances(state) if browser_enabled else [],
            "subscriptions": state["subscriptions"],
            "nodes": [
                public_node(node)
                for node in state["nodes"]
                if browser_enabled
                or node.get(webdav_sync.LOCAL_BROWSER_NODE_MARKER) is not True
            ],
            "bridges": bridges,
            "tests": public_test_results(state),
            "health_settings": dict(state["settings"]["health"]),
            "regions": [{"code": code, "name": item[0]} for code, item in REGION_CATALOG.items()],
            "speed_test": {
                "settings": speed_test_settings_payload(state),
                **speed_test_task_payload(),
            },
            "subscription_settings": normalize_subscription_settings(
                (state.get("settings") or {}).get("subscriptions")
            ),
            "subscription_refresh": subscription_refresh_task_payload(),
            "logs": {
                "settings": log_settings_payload(state),
                "storage": runtime_log_storage_payload(),
                "request_total": len(state.get("request_logs", [])),
                "system_total": len(system_entries),
                "system_error_total": sum(item.get("level") == "error" for item in system_entries),
                "system_error_24h": len(recent_errors),
                "error_groups": sorted(error_groups.values(), key=lambda item: item["count"], reverse=True)[:5],
            },
            "request_logs": state.get("request_logs", [])[:200],
            "system_logs": system_entries[:300],
            "pool": {
                "member_count": len(members),
                "base_url": f"{console_base_url()}/pool/{token}",
                "links": pool_links(token),
                "members": public_pool_members(state, members),
                "strategies": ["random", "round_robin", "health", "latency"],
            },
            "sync": webdav_sync.public_webdav_settings(
                webdav_sync.normalize_webdav_settings((state.get("settings") or {}).get("webdav")),
                sync_browser_data=browser_enabled,
            ),
            "sync_openssl": webdav_sync.openssl_available(),
            "network": network,
            "smart_gateway": smart_gateway_status_payload(state, lan_addresses=lan_addresses),
            "version": APP_VERSION,
        }


def _diagnostic_payload(
    status: str,
    message: str,
    *,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if status not in {"ok", "warn", "error", "skipped"}:
        raise ValueError("诊断状态无效")
    return {
        "status": status,
        "message": str(message)[:500],
        "details": details or {},
    }


def _run_diagnostic_check(
    check_id: str,
    label: str,
    category: str,
    check: Any,
) -> dict[str, Any]:
    started = time.monotonic()
    try:
        payload = check()
        if not isinstance(payload, dict):
            raise RuntimeError("诊断结果格式无效")
        result = _diagnostic_payload(
            str(payload.get("status") or "error"),
            str(payload.get("message") or "检查未返回说明"),
            details=payload.get("details") if isinstance(payload.get("details"), dict) else {},
        )
    except Exception as error:
        result = _diagnostic_payload(
            "error",
            f"{label}检查失败（{type(error).__name__}）",
        )
    result.update(
        {
            "id": check_id,
            "label": label,
            "category": category,
            "latency_ms": max(0, int(round((time.monotonic() - started) * 1000))),
        }
    )
    return result


def _core_version_line(path: str, core: str) -> str:
    command = [path, "version"]
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=3,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    output = "\n".join(part for part in (completed.stdout, completed.stderr) if part).strip()
    line = next((item.strip() for item in output.splitlines() if item.strip()), "")
    if completed.returncode != 0 and not line:
        raise RuntimeError(f"{core} version 返回 {completed.returncode}")
    return line[:180] or "版本未知"


def cores_status_payload() -> dict[str, Any]:
    paths = {**core_paths(), "fingerprint-chromium": fingerprint_chromium_path() if browser_feature_enabled() else None}
    cores = []
    for core, path in paths.items():
        version, error = "", ""
        if path:
            try:
                if core == "fingerprint-chromium":
                    result = subprocess.run([str(path), "--version"], capture_output=True, text=True,
                                            timeout=3, check=False, **({"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}))
                    version = (result.stdout or result.stderr or "版本未知").splitlines()[0][:180]
                else:
                    version = _core_version_line(str(path), core)
            except Exception as exc:
                error = str(exc)[:300]
        cores.append({"core": core, "path": path, "version": version,
                      "installed": bool(path), "status": "ready" if path else "missing", "error": error})
    return {"cores": cores}


def update_proxy_core(payload: dict[str, Any]) -> dict[str, Any]:
    core = str(payload.get("core") or "")
    if core not in {"sing-box", "xray"}:
        raise ValueError("核心只能是 sing-box 或 xray")
    with BRIDGE_START_LOCK:
        with CORE_INSTALL_LOCK:
            _assert_kernel_upload_target_idle(core)
            if SHUTTING_DOWN.is_set():
                raise RuntimeError("程序正在退出")
            release = _github_json(SING_BOX_RELEASES_API if core == "sing-box" else XRAY_RELEASES_API)
            picker = _pick_sing_box_asset if core == "sing-box" else _pick_xray_asset
            asset = picker(release if isinstance(release, dict) else {}, host_arch_token(), host_os_token())
            if not asset:
                raise RuntimeError("未找到适配当前系统的官方核心包")
            installed = _install_core_from_asset(core, asset, str(release.get("tag_name") or ""))
    notify_status_change()
    return {"ok": True, "core": core, "path": installed, "version": _core_version_line(installed, core)}


def _diagnose_cores() -> dict[str, Any]:
    paths = core_paths()
    details: dict[str, Any] = {}
    unavailable: list[str] = []
    unknown_versions: list[str] = []
    for core in ("sing-box", "xray"):
        path = paths.get(core)
        if not path:
            details[core] = {"installed": False, "version": ""}
            unavailable.append(core)
            continue
        try:
            version = _core_version_line(path, core)
        except Exception:
            version = "版本读取失败"
            unknown_versions.append(core)
        details[core] = {
            "installed": True,
            "binary": Path(path).name,
            "version": version,
        }
    if unavailable:
        return _diagnostic_payload(
            "warn",
            f"缺少核心：{'、'.join(unavailable)}；对应节点及回退能力不可用",
            details=details,
        )
    if unknown_versions:
        return _diagnostic_payload(
            "warn",
            f"核心已安装，但无法读取版本：{'、'.join(unknown_versions)}",
            details=details,
        )
    return _diagnostic_payload("ok", "sing-box 与 xray 均可执行", details=details)


def _diagnose_ports() -> dict[str, Any]:
    def listening(port: int) -> bool:
        if not 1 <= port <= 65535:
            return False
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.2)
            return probe.connect_ex(("127.0.0.1", port)) == 0

    host = str(os.environ.get("PROXY_BRIDGE_HOST") or "127.0.0.1").strip() or "127.0.0.1"
    console_port = safe_int(os.environ.get("PROXY_BRIDGE_PORT"), DEFAULT_SERVER_PORT)
    with STATE_LOCK:
        state = load_state()
        active = [
            {
                "bridge_id": str(bridge.get("id") or ""),
                "port": safe_int(bridge.get("port"), 0),
                "status": str(bridge.get("status") or ""),
            }
            for bridge in state.get("bridges", [])
            if bridge.get("status") in ACTIVE_BRIDGE_STATUSES
        ]
    ports = [entry["port"] for entry in active]
    problems: list[str] = []
    details: dict[str, Any] = {
        "console_bind": host,
        "console_port": console_port,
        "console_listening": listening(console_port),
        "active_proxy_ports": [
            {"port": entry["port"], "listening": listening(entry["port"])} for entry in active
        ],
        "docker": docker_mode(),
    }
    if not 1 <= console_port <= 65535:
        problems.append("控制台端口无效")
    if any(not 1 <= port <= 65535 for port in ports):
        problems.append("存在无效代理端口")
    if len(ports) != len(set(ports)):
        problems.append("运行中的代理端口存在重复")
    if not details["console_listening"]:
        problems.append("控制台端口未监听")
    if any(not entry["listening"] for entry in details["active_proxy_ports"]):
        problems.append("存在未监听的活动代理端口")
    if docker_mode():
        start, end = docker_proxy_port_range()
        details["docker_proxy_port_range"] = [start, end]
        if any(not start <= port <= end for port in ports):
            problems.append("代理端口超出 Docker 发布范围")
    elif host not in {"127.0.0.1", "localhost", "::1"}:
        problems.append("控制台绑定到非回环地址")
    if problems:
        return _diagnostic_payload("warn", "；".join(problems), details=details)
    mode = "Docker 端口发布范围一致" if docker_mode() else "控制台仅绑定本机"
    return _diagnostic_payload("ok", f"{mode}，{len(ports)} 个活动代理端口配置有效", details=details)


def _diagnose_storage() -> dict[str, Any]:
    entries = (
        ("work_root", WORK_ROOT, True, 0o700),
        ("runtime_root", RUNTIME_ROOT, True, 0o700),
        ("state", STATE_PATH, False, 0o600),
    )
    details: dict[str, Any] = {"work_root": str(WORK_ROOT), "entries": {}}
    errors: list[str] = []
    warnings: list[str] = []
    for label, path, is_directory, expected_mode in entries:
        exists = path.is_dir() if is_directory else path.is_file()
        access_path = path if exists else path.parent
        entry: dict[str, Any] = {
            "exists": exists,
            "readable": os.access(access_path, os.R_OK),
            "writable": os.access(access_path, os.W_OK),
        }
        if exists and os.name != "nt":
            mode = path.stat().st_mode & 0o777
            entry["mode"] = f"{mode:03o}"
            if mode & 0o077:
                warnings.append(f"{label} 权限宽于 {expected_mode:03o}")
        if not entry["readable"] or not entry["writable"]:
            errors.append(f"{label} 不可读写")
        details["entries"][label] = entry
    usage_root = WORK_ROOT if WORK_ROOT.exists() else WORK_ROOT.parent
    disk = shutil.disk_usage(usage_root)
    details["disk"] = {
        "free_bytes": disk.free,
        "total_bytes": disk.total,
        "free_percent": round((disk.free / disk.total) * 100, 1) if disk.total else 0,
    }
    if disk.free < 256 * 1024 * 1024 or (disk.total and disk.free / disk.total < 0.05):
        warnings.append("数据盘剩余空间偏低")
    if errors:
        return _diagnostic_payload("error", "；".join(errors), details=details)
    if warnings:
        return _diagnostic_payload("warn", "；".join(warnings), details=details)
    return _diagnostic_payload("ok", "数据目录可读写，权限与磁盘空间正常", details=details)


def _diagnose_github() -> dict[str, Any]:
    started = time.monotonic()
    connection = http.client.HTTPSConnection(
        DIAGNOSTIC_GITHUB_HOST,
        timeout=DIAGNOSTIC_NETWORK_TIMEOUT,
        context=ssl.create_default_context(),
    )
    try:
        connection.request(
            "HEAD",
            DIAGNOSTIC_GITHUB_PATH,
            headers={"User-Agent": "ProxyBridge/0.2", "Accept": "application/vnd.github+json"},
        )
        response = connection.getresponse()
        status_code = int(response.status)
        response.read(0)
        details = {
            "host": DIAGNOSTIC_GITHUB_HOST,
            "status_code": status_code,
            "rate_limit_remaining": response.getheader("X-RateLimit-Remaining") or "",
        }
    finally:
        connection.close()
    latency_ms = int(round((time.monotonic() - started) * 1000))
    details["request_latency_ms"] = latency_ms
    if 200 <= status_code < 400:
        return _diagnostic_payload("ok", f"GitHub API 可达（{latency_ms} ms）", details=details)
    if status_code in {403, 429}:
        return _diagnostic_payload("warn", f"GitHub API 可达，但受到限流（HTTP {status_code}）", details=details)
    return _diagnostic_payload("error", f"GitHub API 返回 HTTP {status_code}", details=details)


def _diagnose_webdav() -> dict[str, Any]:
    with STATE_LOCK:
        state = load_state()
        cfg = webdav_sync.normalize_webdav_settings((state.get("settings") or {}).get("webdav"))
    if not cfg.get("enabled") or not cfg.get("url"):
        return _diagnostic_payload("skipped", "WebDAV 未启用")
    parsed = urllib.parse.urlsplit(str(cfg["url"]))
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return _diagnostic_payload(
            "warn",
            "WebDAV 未使用无凭据 HTTPS URL；为保护账号，诊断未发起连接",
        )
    if cfg.get("insecure_ssl"):
        return _diagnostic_payload(
            "warn",
            "WebDAV 已关闭 TLS 证书校验；为保护账号，诊断未发起连接",
            details={"host": str(parsed.hostname or "")[:253], "tls_verification": False},
        )
    client = webdav_sync.WebDAVClient(
        str(cfg["url"]),
        str(cfg.get("username") or ""),
        str(cfg.get("password") or ""),
        timeout=DIAGNOSTIC_NETWORK_TIMEOUT,
        insecure_ssl=False,
    )
    result = client.test_connection()
    details = {
        "host": str(parsed.hostname or "")[:253],
        "status_code": safe_int(result.get("status"), 0),
        "tls_verification": not bool(cfg.get("insecure_ssl")),
    }
    return _diagnostic_payload("ok", "WebDAV 认证与远端 collection 正常", details=details)


def _selected_diagnostic_bridge(bridge_id: str) -> dict[str, Any] | None:
    with STATE_LOCK:
        state = load_state()
        candidates = [
            dict(bridge)
            for bridge in state.get("bridges", [])
            if bridge.get("status") in ACTIVE_BRIDGE_STATUSES and safe_int(bridge.get("port"), 0) > 0
        ]
    if bridge_id:
        return next((bridge for bridge in candidates if str(bridge.get("id") or "") == bridge_id), None)
    return candidates[0] if candidates else None


def _diagnostic_proxy_ip_lookup(bridge: dict[str, Any]) -> dict[str, Any]:
    local_port = safe_int(bridge.get("port"), 0)
    sock = socket.create_connection(("127.0.0.1", local_port), timeout=DIAGNOSTIC_NETWORK_TIMEOUT)
    transport: socket.socket = sock
    try:
        sock.settimeout(DIAGNOSTIC_NETWORK_TIMEOUT)
        sock.sendall(b"\x05\x01\x00")
        if read_exact(sock, 2) != b"\x05\x00":
            raise RuntimeError("本地 SOCKS5 握手失败")
        host_bytes = DIAGNOSTIC_OUTBOUND_IP_HOST.encode("ascii")
        request = b"\x05\x01\x00\x03" + bytes([len(host_bytes)]) + host_bytes + (443).to_bytes(2, "big")
        sock.sendall(request)
        reply = read_exact(sock, 4)
        if reply[0] != 5 or reply[1] != 0:
            raise RuntimeError("代理无法连接固定诊断目标")
        if reply[3] == 1:
            read_exact(sock, 4)
        elif reply[3] == 3:
            read_exact(sock, read_exact(sock, 1)[0])
        elif reply[3] == 4:
            read_exact(sock, 16)
        else:
            raise RuntimeError("SOCKS5 返回未知地址类型")
        read_exact(sock, 2)
        transport = ssl.create_default_context().wrap_socket(sock, server_hostname=DIAGNOSTIC_OUTBOUND_IP_HOST)
        transport.settimeout(DIAGNOSTIC_NETWORK_TIMEOUT)
        transport.sendall(
            (
                f"GET {DIAGNOSTIC_OUTBOUND_IP_PATH} HTTP/1.1\r\n"
                f"Host: {DIAGNOSTIC_OUTBOUND_IP_HOST}\r\n"
                "Accept: application/json\r\n"
                "Connection: close\r\n"
                "User-Agent: ProxyBridge/0.2\r\n\r\n"
            ).encode("ascii")
        )
        response = http.client.HTTPResponse(transport)
        response.begin()
        body = response.read(DIAGNOSTIC_RESPONSE_LIMIT + 1)
        if len(body) > DIAGNOSTIC_RESPONSE_LIMIT:
            raise RuntimeError("出口诊断响应过大")
        if response.status != 200:
            raise RuntimeError("出口诊断目标返回异常")
        payload = json.loads(body.decode("utf-8"))
        address = str(payload.get("ip") or "").strip()
        ipaddress.ip_address(address)
        return {
            "ip": address,
            "bridge_id": str(bridge.get("id") or ""),
            "node_name": str(bridge.get("node_name") or "")[:120],
            "core": str(bridge.get("core") or ""),
            "remote_dns": True,
        }
    finally:
        try:
            transport.close()
        finally:
            if transport is not sock:
                sock.close()


def run_diagnostics(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise ValueError("诊断参数必须是对象")
    if not DIAGNOSTIC_RUN_SEMAPHORE.acquire(blocking=False):
        raise RuntimeError("诊断任务繁忙，请稍后重试")
    try:
        return _run_diagnostics(payload)
    finally:
        DIAGNOSTIC_RUN_SEMAPHORE.release()


def _run_diagnostics(payload: dict[str, Any]) -> dict[str, Any]:
    include_network = truthy(payload.get("network"))
    bridge_id = str(payload.get("bridge_id") or "").strip()
    if len(bridge_id) > 128:
        raise ValueError("代理 ID 无效")

    def skipped_network(label: str) -> dict[str, Any]:
        return _diagnostic_payload("skipped", f"未启用网络诊断，未访问{label}")

    bridge = _selected_diagnostic_bridge(bridge_id) if include_network else None
    if include_network and bridge_id and bridge is None:
        raise ValueError("指定的本地代理未运行或不存在")
    proxy_cache: dict[str, Any] = {}
    proxy_cache_lock = threading.Lock()

    def proxy_result() -> dict[str, Any] | None:
        if not include_network:
            return None
        if bridge is None:
            return None
        with proxy_cache_lock:
            if "error" in proxy_cache:
                raise RuntimeError("代理网络诊断失败") from proxy_cache["error"]
            if "result" not in proxy_cache:
                try:
                    proxy_cache["result"] = _diagnostic_proxy_ip_lookup(bridge)
                except Exception as error:
                    proxy_cache["error"] = error
                    raise RuntimeError("代理网络诊断失败") from error
            return proxy_cache["result"]

    def github_check() -> dict[str, Any]:
        return _diagnose_github() if include_network else skipped_network("GitHub")

    def webdav_check() -> dict[str, Any]:
        return _diagnose_webdav() if include_network else skipped_network("WebDAV")

    def outbound_check() -> dict[str, Any]:
        if not include_network:
            return skipped_network("出口 IP 服务")
        result = proxy_result()
        if result is None:
            return _diagnostic_payload("skipped", "没有可用于出口检测的运行中代理")
        return _diagnostic_payload(
            "ok",
            f"代理出口 IP：{result['ip']}",
            details=result,
        )

    def dns_check() -> dict[str, Any]:
        if not include_network:
            return skipped_network("DNS 解析目标")
        result = proxy_result()
        if result is None:
            return _diagnostic_payload("skipped", "没有可用于 DNS 路径检测的运行中代理")
        return _diagnostic_payload(
            "warn",
            "代理请求使用 SOCKS5 域名转发；本检查无法观察系统或浏览器的其他 DNS 请求",
            details={
                "bridge_id": result["bridge_id"],
                "core": result["core"],
                "scope": "仅覆盖 ProxyBridge 代理请求，不覆盖系统其他应用",
            },
        )

    checks = (
        ("cores", "核心版本", "local", _diagnose_cores),
        ("ports", "端口映射", "local", _diagnose_ports),
        ("storage", "数据目录", "local", _diagnose_storage),
        ("github", "GitHub 连通性", "network", github_check),
        ("webdav", "WebDAV", "network", webdav_check),
        ("outbound", "代理真实出口", "network", outbound_check),
        ("dns", "DNS 泄漏风险", "network", dns_check),
    )
    with concurrent.futures.ThreadPoolExecutor(max_workers=5, thread_name_prefix="diagnostic") as executor:
        futures = [
            executor.submit(_run_diagnostic_check, check_id, label, category, check)
            for check_id, label, category, check in checks
        ]
        results = [future.result() for future in futures]
    summary = {status: sum(item["status"] == status for item in results) for status in ("ok", "warn", "error", "skipped")}
    return {
        "generated_at": now_ts(),
        "network_requested": include_network,
        "selected_bridge_id": str(bridge.get("id") or "") if bridge else "",
        "results": results,
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# Proxy pool: derived view of active local bridges + tokenized discovery URLs
# ---------------------------------------------------------------------------


def new_pool_token() -> str:
    return secrets.token_urlsafe(24)


def ensure_pool_token(state: dict[str, Any]) -> tuple[str, bool]:
    """Return (token, changed). Caller must save_state if changed under STATE_LOCK."""
    settings = state.get("settings")
    if not isinstance(settings, dict):
        settings = {}
        state["settings"] = settings
    # Ensure webdav settings block exists (device_id etc.).
    settings["webdav"] = webdav_sync.normalize_webdav_settings(settings.get("webdav"))
    token = str(settings.get("pool_token") or "")
    if not (POOL_TOKEN_MIN_LEN <= len(token) <= POOL_TOKEN_MAX_LEN) or POOL_TOKEN_PATTERN.fullmatch(token) is None:
        token = new_pool_token()
        settings["pool_token"] = token
        return token, True
    return token, False


def browser_process_is_active(instance_id: str) -> bool:
    if remote_browser_feature_enabled():
        sessions, _error = remote_browser_sessions()
        if sessions is None:
            # Fail closed for WebDAV: never archive a Profile while worker status is unknown.
            return True
        return bool(
            sessions.get(instance_id, {}).get("status")
            in REMOTE_BROWSER_LIVE_SESSION_STATUSES
        )
    with PROCESS_LOCK:
        process = BROWSER_PROCESS_TABLE.get(instance_id)
    return bool(process and process.poll() is None)


def get_sync_engine() -> webdav_sync.SyncEngine:
    global SYNC_ENGINE
    if SYNC_ENGINE is None:
        SYNC_ENGINE = webdav_sync.SyncEngine(
            work_root=WORK_ROOT,
            state_path=STATE_PATH,
            browser_profiles_root=browser_profiles_root(),
            load_state=load_state,
            save_state=save_state,
            state_lock=STATE_LOCK,
            browser_instance_profile_path=browser_instance_profile_path,
            browser_runtime_is_active=browser_process_is_active,
            log=log_event,
            sync_browser_data=browser_feature_enabled(),
        )
    return SYNC_ENGINE


def sync_status_payload() -> dict[str, Any]:
    engine = get_sync_engine()
    with STATE_LOCK:
        state = load_state()
        cfg = engine.get_settings(state)
        state.setdefault("settings", {})["webdav"] = cfg
        save_state(state)
    return {
        "settings": engine.public_settings(cfg),
        "openssl": webdav_sync.openssl_available(),
    }


def mark_sync_local_change() -> None:
    try:
        get_sync_engine().mark_local_change()
    except Exception:
        pass


def maybe_auto_upload_browser_instances(instance_ids: list[str]) -> None:
    """Queue stopped browser Profiles for WebDAV without blocking lifecycle APIs."""
    unique_ids = [instance_id for instance_id in dict.fromkeys(instance_ids) if instance_id]
    if not unique_ids:
        return
    try:
        engine = get_sync_engine()
    except Exception:
        return
    for instance_id in unique_ids:
        try:
            engine.maybe_auto_upload_instance(instance_id)
        except Exception:
            pass


def pool_tokens_equal(provided: str, expected: str) -> bool:
    if not provided or not expected:
        return False
    return hmac.compare_digest(provided, expected)


def console_base_url() -> str:
    port = safe_int(os.environ.get("PROXY_BRIDGE_PORT"), DEFAULT_SERVER_PORT)
    if port <= 0 or port > 65535:
        port = DEFAULT_SERVER_PORT
    # Always use 127.0.0.1 even if the user opens the console via localhost.
    return f"http://127.0.0.1:{port}"


def pool_links(token: str) -> dict[str, str]:
    base = f"{console_base_url()}/pool/{token}"
    return {
        "list_json": f"{base}/list.json",
        "proxies_txt": f"{base}/proxies.txt",
        "get_random": f"{base}/get?strategy=random",
        "get_round_robin": f"{base}/get?strategy=round_robin",
        "get_health": f"{base}/get?strategy=health",
        "get_random_socks5": f"{base}/get?strategy=random&protocol=socks5",
        "get_random_http": f"{base}/get?strategy=random&protocol=http",
    }


def is_pool_member(bridge: dict[str, Any]) -> bool:
    """List membership: discoverable active local proxy (may still be starting)."""
    if bridge.get("status") not in ACTIVE_BRIDGE_STATUSES:
        return False
    port = safe_int(bridge.get("port"), 0)
    return port > 0


def is_pick_ready_member(bridge: dict[str, Any]) -> bool:
    """Pick readiness: prefer actually serving endpoints."""
    if not is_pool_member(bridge):
        return False
    if bridge.get("status") != "running":
        return False
    bridge_id = str(bridge.get("id") or "")
    return bool(bridge_id and bridge_runtime_is_active(bridge_id) and pool_member_available(bridge))


def pool_member_availability(bridge: dict[str, Any], current_time: int | None = None) -> dict[str, Any]:
    current = now_ts() if current_time is None else current_time
    health = normalize_pool_health(bridge.get("pool_health"))
    manual_until = health["manual_excluded_until"]
    circuit_until = health["circuit_open_until"]
    probe_until = health["half_open_probe_until"]
    if manual_until > current:
        state = "excluded"
        excluded_until = manual_until
    elif circuit_until > current:
        state = "open"
        excluded_until = circuit_until
    elif circuit_until > 0 and health["consecutive_failures"] >= POOL_CIRCUIT_FAILURE_THRESHOLD:
        state = "half_open"
        excluded_until = 0
    else:
        state = "closed"
        excluded_until = 0
    return {
        "available": state == "closed" or (state == "half_open" and probe_until <= current),
        "circuit_state": state,
        "excluded_until": excluded_until,
        "manual_excluded_until": manual_until,
        "probe_in_progress": state == "half_open" and probe_until > current,
    }


def pool_member_available(bridge: dict[str, Any], current_time: int | None = None) -> bool:
    return bool(pool_member_availability(bridge, current_time)["available"])


def pool_health_metrics(
    bridge: dict[str, Any],
    test_result: dict[str, Any] | None = None,
    current_time: int | None = None,
) -> dict[str, Any]:
    health = normalize_pool_health(bridge.get("pool_health"))
    test = test_result if isinstance(test_result, dict) else {}
    latency = health["latency_ewma_ms"] or safe_int(test.get("latency_ms"), 0) or None
    success_rate: float | None = None
    try:
        if test.get("success_rate") is not None:
            success_rate = min(1.0, max(0.0, float(test["success_rate"])))
    except (TypeError, ValueError):
        success_rate = None
    score = 80.0 if success_rate is None else 50.0 + success_rate * 50.0
    if latency is not None:
        score -= min(30.0, max(0.0, latency / 50.0))
    score -= min(60.0, health["consecutive_failures"] * 15.0)
    if test.get("stale"):
        score -= 10.0
    availability = pool_member_availability(bridge, current_time)
    if not availability["available"]:
        score = 0.0
    return {
        "latency_ms": latency,
        "success_rate": success_rate,
        "health_score": round(max(0.0, min(100.0, score)), 1),
        "consecutive_failures": health["consecutive_failures"],
        "last_success_at": health["last_success_at"],
        "last_failure_at": health["last_failure_at"],
        "last_error": health["last_error"],
        **availability,
    }


def public_pool_member(
    bridge: dict[str, Any],
    test_result: dict[str, Any] | None = None,
    current_time: int | None = None,
) -> dict[str, Any]:
    port = safe_int(bridge.get("port"), 0)
    socks = bridge.get("socks_url") or (f"socks5://127.0.0.1:{port}" if port else "")
    http = bridge.get("http_url") or (f"http://127.0.0.1:{port}" if port else "")
    return {
        "id": bridge.get("id"),
        "node_id": bridge.get("node_id"),
        "node_name": bridge.get("node_name"),
        "region": bridge.get("region") or "",
        "node_protocol": bridge.get("protocol") or "",
        "port": port,
        "socks_url": socks,
        "http_url": http,
        "supported_proxy_protocols": bridge.get("supported_proxy_protocols")
        or ["socks5", "http", "https-connect"],
        "status": bridge.get("status"),
        **pool_health_metrics(bridge, test_result, current_time),
    }


def public_pool_members(
    state: dict[str, Any],
    members: list[dict[str, Any]],
    current_time: int | None = None,
) -> list[dict[str, Any]]:
    tests = public_test_results(state, current_time)
    return [
        public_pool_member(member, tests.get(str(member.get("node_id") or "")), current_time)
        for member in members
    ]


def pool_members(
    state: dict[str, Any],
    *,
    region: str | None = None,
    q: str | None = None,
    ready_only: bool = False,
) -> list[dict[str, Any]]:
    """Membership only. Does NOT take protocol (output preference is separate)."""
    region_filter = (region or "").strip()
    query = (q or "").strip().lower()
    members: list[dict[str, Any]] = []
    for bridge in state.get("bridges", []):
        if not isinstance(bridge, dict):
            continue
        if ready_only:
            if not is_pick_ready_member(bridge):
                continue
        elif not is_pool_member(bridge):
            continue
        if region_filter and str(bridge.get("region") or "") != region_filter:
            continue
        if query and query not in str(bridge.get("node_name") or "").lower():
            continue
        members.append(bridge)
    members.sort(
        key=lambda item: (
            str(item.get("node_name") or "").lower(),
            safe_int(item.get("port"), 0),
            str(item.get("id") or ""),
        )
    )
    return members


def parse_protocol_param(raw: str | None) -> str | None:
    if raw is None or raw == "":
        return None
    value = raw.strip().lower()
    if value not in ALLOWED_PROXY_PROTOCOLS:
        raise ValueError("protocol 只能是 socks5 或 http")
    return value


def select_proxy_url(member: dict[str, Any], protocol: str | None) -> str:
    preferred = protocol or "socks5"
    if preferred == "http":
        return str(member.get("http_url") or "")
    return str(member.get("socks_url") or "")


def render_proxies_txt(members: list[dict[str, Any]], protocol: str | None) -> str:
    lines: list[str] = []
    for member in members:
        if protocol is None:
            socks = str(member.get("socks_url") or "")
            http = str(member.get("http_url") or "")
            if socks:
                lines.append(socks)
            if http:
                lines.append(http)
        else:
            url = select_proxy_url(member, protocol)
            if url:
                lines.append(url)
    return "".join(f"{line}\n" for line in lines)


def pick_round_robin(members: list[dict[str, Any]]) -> dict[str, Any]:
    global POOL_RR_COUNTER
    if not members:
        raise ValueError("代理池为空")
    with POOL_RR_LOCK:
        idx = POOL_RR_COUNTER % len(members)
        POOL_RR_COUNTER += 1
    return members[idx]


def pick_pool_member(members: list[dict[str, Any]], strategy: str) -> dict[str, Any]:
    if not members:
        raise ValueError("代理池为空")
    if strategy == "random":
        return random.choice(members)
    if strategy == "round_robin":
        return pick_round_robin(members)
    if strategy == "health":
        return min(
            members,
            key=lambda member: (
                -float(member.get("health_score") or 0),
                safe_int(member.get("latency_ms"), 2**31 - 1),
                str(member.get("id") or ""),
            ),
        )
    if strategy == "latency":
        return min(
            members,
            key=lambda member: (
                safe_int(member.get("latency_ms"), 2**31 - 1),
                -float(member.get("health_score") or 0),
                str(member.get("id") or ""),
            ),
        )
    raise ValueError("strategy 只能是 random、round_robin、health 或 latency")


def pool_snapshot(
    *,
    ready_only: bool = False,
    region: str | None = None,
    q: str | None = None,
) -> dict[str, Any]:
    """Normative read path. Never call synchronize_process_state while holding only STATE_LOCK."""
    with SHUTDOWN_LOCK:
        synchronize_process_state()
        with STATE_LOCK:
            state = load_state()
            changed = sync_process_status(state)
            token, token_changed = ensure_pool_token(state)
            members = pool_members(state, region=region, q=q, ready_only=ready_only)
            if changed or token_changed:
                save_state(state)
            return {
                "token": token,
                "members": public_pool_members(state, members),
                "updated_at": float(now_ts()),
            }


def pool_admin_payload() -> dict[str, Any]:
    snapshot = pool_snapshot(ready_only=False)
    token = str(snapshot["token"])
    members = snapshot["members"]
    return {
        "member_count": len(members),
        "base_url": f"{console_base_url()}/pool/{token}",
        "links": pool_links(token),
        "members": members,
        "strategies": ["random", "round_robin", "health", "latency"],
        "formats": ["list.json", "proxies.txt", "get"],
    }


def update_pool_health_record(
    health: Any,
    *,
    ok: bool,
    latency_ms: Any = None,
    error: Any = "",
    current_time: int | None = None,
) -> dict[str, Any]:
    current = now_ts() if current_time is None else current_time
    updated = normalize_pool_health(health)
    updated["half_open_probe_until"] = 0
    latency = safe_int(latency_ms, 0)
    if latency > 0:
        previous_latency = safe_int(updated.get("latency_ewma_ms"), 0)
        updated["latency_ewma_ms"] = latency if previous_latency <= 0 else round(previous_latency * 0.7 + latency * 0.3)
    if ok:
        updated.update(
            {
                "consecutive_failures": 0,
                "last_success_at": current,
                "circuit_open_until": 0,
                "last_error": "",
            }
        )
        return updated
    failures = updated["consecutive_failures"] + 1
    updated.update(
        {
            "consecutive_failures": failures,
            "last_failure_at": current,
            "last_error": str(error or "代理请求失败")[:500],
        }
    )
    if failures >= POOL_CIRCUIT_FAILURE_THRESHOLD:
        exponent = min(10, failures - POOL_CIRCUIT_FAILURE_THRESHOLD)
        cooldown = min(POOL_CIRCUIT_MAX_SECONDS, POOL_CIRCUIT_BASE_SECONDS * (2**exponent))
        updated["circuit_open_until"] = current + cooldown
    return updated


def update_pool_member_feedback(payload: dict[str, Any]) -> dict[str, Any]:
    bridge_id = str(payload.get("bridge_id") or payload.get("id") or "")
    if not isinstance(payload.get("ok"), bool):
        raise ValueError("ok 必须是布尔值")
    latency = safe_int(payload.get("latency_ms"), 0)
    if latency < 0 or latency > 10 * 60 * 1000:
        raise ValueError("latency_ms 超出范围")
    with STATE_LOCK:
        state = load_state()
        bridge = next((item for item in state.get("bridges", []) if item.get("id") == bridge_id), None)
        if bridge is None:
            raise ValueError("代理池成员不存在")
        bridge["pool_health"] = update_pool_health_record(
            bridge.get("pool_health"),
            ok=payload["ok"],
            latency_ms=latency or None,
            error=payload.get("error") or payload.get("message") or "",
        )
        save_state(state)
        tests = public_test_results(state)
        member = public_pool_member(bridge, tests.get(str(bridge.get("node_id") or "")))
    return {"ok": True, "member": member}


def set_pool_member_exclusion(payload: dict[str, Any]) -> dict[str, Any]:
    bridge_id = str(payload.get("bridge_id") or payload.get("id") or "")
    duration = safe_int(payload.get("duration_seconds"), -1)
    if not 0 <= duration <= POOL_MANUAL_EXCLUDE_MAX_SECONDS:
        raise ValueError(f"排除时长必须在 0..{POOL_MANUAL_EXCLUDE_MAX_SECONDS} 秒之间")
    with STATE_LOCK:
        state = load_state()
        bridge = next((item for item in state.get("bridges", []) if item.get("id") == bridge_id), None)
        if bridge is None:
            raise ValueError("代理池成员不存在")
        health = normalize_pool_health(bridge.get("pool_health"))
        health["manual_excluded_until"] = now_ts() + duration if duration else 0
        health["half_open_probe_until"] = 0
        bridge["pool_health"] = health
        save_state(state)
        tests = public_test_results(state)
        member = public_pool_member(bridge, tests.get(str(bridge.get("node_id") or "")))
    return {"ok": True, "member": member}


def rotate_pool_token() -> dict[str, Any]:
    with SHUTDOWN_LOCK:
        with STATE_LOCK:
            state = load_state()
            settings = state.get("settings")
            if not isinstance(settings, dict):
                settings = {}
                state["settings"] = settings
            token = new_pool_token()
            settings["pool_token"] = token
            save_state(state)
            return {
                "ok": True,
                "base_url": f"{console_base_url()}/pool/{token}",
                "links": pool_links(token),
            }


def _redact_pool_token_in_log_arg(value: Any) -> Any:
    if not isinstance(value, str) or "/pool/" not in value:
        return value
    # Groups: prefix, "/pool/", token, optional rest → keep prefix + /pool/ + *** + rest
    return _POOL_PATH_REDACT.sub(r"\1\2***\4", value)


def parse_pool_path(path: str) -> tuple[str | None, str | None, str | None]:
    """Return (token, action, error_kind).

    error_kind is None on success; otherwise 'not_found' or 'forbidden'.
    """
    raw = urllib.parse.unquote(path or "")
    if raw in {"/pool", "/pool/"}:
        return None, None, "not_found"
    if not raw.startswith("/pool/"):
        return None, None, "not_found"
    remainder = raw[len("/pool/") :]
    # Preserve empty token segment for "/pool//list.json".
    parts = remainder.split("/")
    if parts and parts[-1] == "":
        parts = parts[:-1]
    if len(parts) != 2:
        if parts and parts[0] == "":
            return None, None, "forbidden"
        return None, None, "not_found"
    token, action = parts[0], parts[1]
    if token == "":
        return None, None, "forbidden"
    if POOL_TOKEN_PATTERN.fullmatch(token) is None:
        return None, None, "forbidden"
    if action not in {"list.json", "proxies.txt", "get"}:
        return None, None, "not_found"
    return token, action, None


def _public_subscription_addresses(
    url: str,
    *,
    allow_non_public: bool = False,
) -> tuple[urllib.parse.SplitResult, list[str]]:
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("订阅 URL 格式无效") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc or not parsed.hostname:
        raise ValueError("订阅 URL 必须是 http(s)://host/path")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("订阅 URL 不能包含用户名或密码")
    service_port = port or (443 if parsed.scheme.lower() == "https" else 80)
    try:
        resolved = socket.getaddrinfo(parsed.hostname, service_port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError(f"订阅 URL 域名解析失败: {parsed.hostname}") from exc
    addresses = sorted({str(item[4][0]).split("%", 1)[0] for item in resolved if item[4]})
    if not addresses:
        raise ValueError(f"订阅 URL 域名没有可用地址: {parsed.hostname}")
    for value in addresses:
        try:
            address = ipaddress.ip_address(value)
        except ValueError as exc:
            raise ValueError(f"订阅 URL 解析地址无效: {value}") from exc
        if address.is_unspecified or address.is_multicast:
            raise ValueError(f"订阅 URL 解析到不可用地址: {value}")
        if not allow_non_public and not address.is_global:
            raise ValueError(
                f"订阅 URL 不允许访问非公网地址: {value}；"
                "如需访问 TUN fake-IP 或局域网地址，请在订阅页开启“允许非公网订阅地址”"
            )
    return parsed, addresses


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host: str, port: int, *, pinned_address: str, timeout: float) -> None:
        super().__init__(host, port=port, timeout=timeout)
        self.pinned_address = pinned_address

    def connect(self) -> None:
        self.sock = self._create_connection(
            (self.pinned_address, self.port),
            self.timeout,
            self.source_address,
        )
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, port: int, *, pinned_address: str, timeout: float) -> None:
        super().__init__(host, port=port, timeout=timeout, context=ssl.create_default_context())
        self.pinned_address = pinned_address

    def connect(self) -> None:
        self.sock = self._create_connection(
            (self.pinned_address, self.port),
            self.timeout,
            self.source_address,
        )
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)


def _open_subscription_response(
    parsed: urllib.parse.SplitResult,
    addresses: list[str],
    *,
    deadline: float,
    request_headers: dict[str, str] | None = None,
) -> tuple[http.client.HTTPConnection, http.client.HTTPResponse, threading.Timer]:
    scheme = parsed.scheme.lower()
    port = parsed.port or (443 if scheme == "https" else 80)
    target = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
    errors: list[BaseException] = []
    for address in addresses:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("订阅下载超时")
        timeout = min(SUBSCRIPTION_IO_TIMEOUT, remaining)
        connection: http.client.HTTPConnection
        if scheme == "https":
            connection = _PinnedHTTPSConnection(
                str(parsed.hostname),
                port,
                pinned_address=address,
                timeout=timeout,
            )
        else:
            connection = _PinnedHTTPConnection(
                str(parsed.hostname),
                port,
                pinned_address=address,
                timeout=timeout,
            )
        try:
            headers = {
                "Host": parsed.netloc,
                "User-Agent": "ProxyBridge/0.1 (+https://localhost)",
                "Accept": "*/*",
                "Connection": "close",
            }
            for name, value in (request_headers or {}).items():
                if name.lower() in {"if-none-match", "if-modified-since"} and value:
                    headers[name] = str(value)[:512]
            connection.request(
                "GET",
                target,
                headers=headers,
            )
            remaining = deadline - time.monotonic()
            if remaining <= 0 or connection.sock is None:
                raise TimeoutError("订阅下载超时")
            transport = connection.sock

            def close_on_deadline() -> None:
                try:
                    transport.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                transport.close()

            deadline_timer = threading.Timer(remaining, close_on_deadline)
            deadline_timer.daemon = True
            deadline_timer.start()
            try:
                response = connection.getresponse()
            except BaseException:
                deadline_timer.cancel()
                raise
            return connection, response, deadline_timer
        except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
            errors.append(exc)
            connection.close()
            if time.monotonic() >= deadline:
                raise TimeoutError("订阅下载超时") from exc
    detail = str(errors[-1]) if errors else "没有可用地址"
    raise RuntimeError(f"订阅 URL 连接失败: {detail}") from (errors[-1] if errors else None)


def parse_subscription_userinfo(value: Any) -> dict[str, int]:
    usage: dict[str, int] = {}
    for part in str(value or "").split(";"):
        key, separator, raw_value = part.strip().partition("=")
        if separator and key.lower() in {"upload", "download", "total", "expire"}:
            try:
                number = int(raw_value.strip())
            except ValueError:
                continue
            if number >= 0:
                usage[key.lower()] = number
    return usage


def subscription_filter_settings(payload: dict[str, Any]) -> dict[str, Any]:
    if "filter_regex" in payload and not isinstance(payload["filter_regex"], bool):
        raise ValueError("filter_regex 必须是布尔值")
    rules: dict[str, Any] = {"filter_regex": payload.get("filter_regex") is True}
    for key in ("filter_include", "filter_exclude"):
        value = payload.get(key) or ""
        if isinstance(value, list):
            value = "\n".join(str(item) for item in value)
        if not isinstance(value, str) or len(value) > 4096:
            raise ValueError("过滤规则必须是长度不超过 4096 的文本")
        rules[key] = value.strip()
    return rules


def filter_subscription_nodes(nodes: list[Node], rules: dict[str, Any]) -> list[Node]:
    compiled: dict[str, list[Any]] = {}
    for key in ("filter_include", "filter_exclude"):
        separator = r"\n" if rules["filter_regex"] else r"[\n,，]"
        values = [item.strip() for item in re.split(separator, rules[key]) if item.strip()]
        if rules["filter_regex"]:
            # 禁止重复分组再次重复及回溯引用，避免导入正则阻塞管理线程。
            try:
                from re import _parser as regex_parser
            except ImportError:
                import sre_parse as regex_parser

            def validate_pattern(tokens: Any, repeated: bool = False) -> None:
                for operation, argument in tokens:
                    token = str(operation)
                    if token in {"GROUPREF", "GROUPREF_EXISTS"}:
                        raise ValueError("过滤正则不支持回溯引用")
                    if token in {"MAX_REPEAT", "MIN_REPEAT", "POSSESSIVE_REPEAT"}:
                        if repeated:
                            raise ValueError("过滤正则不支持嵌套重复")
                        validate_pattern(argument[2], True)
                    elif token == "SUBPATTERN":
                        validate_pattern(argument[-1], repeated)
                    elif token == "BRANCH":
                        if repeated:
                            raise ValueError("过滤正则不支持重复分组中的分支")
                        for branch in argument[1]:
                            validate_pattern(branch, repeated)
                    elif token in {"ASSERT", "ASSERT_NOT"}:
                        validate_pattern(argument[1], repeated)

            try:
                for value in values:
                    validate_pattern(regex_parser.parse(value, re.IGNORECASE))
                compiled[key] = [re.compile(value, re.IGNORECASE) for value in values]
            except re.error as exc:
                raise ValueError(f"过滤正则无效：{exc}") from exc
        else:
            compiled[key] = [value.casefold() for value in values]

    def matches(items: list[Any], name: str) -> bool:
        return any(item.search(name) if rules["filter_regex"] else item in name.casefold() for item in items)

    return [
        node for node in nodes
        if (not compiled["filter_include"] or matches(compiled["filter_include"], node.name))
        and not matches(compiled["filter_exclude"], node.name)
    ]


def preview_subscription(payload: dict[str, Any]) -> dict[str, Any]:
    rules = subscription_filter_settings(payload)
    raw = str(payload.get("raw") or payload.get("content") or "").strip()
    url = str(payload.get("url") or "").strip()
    if not raw and url:
        with STATE_LOCK:
            settings = normalize_subscription_settings((load_state().get("settings") or {}).get("subscriptions"))
        raw = str(fetch_subscription_response(url, allow_non_public=settings["allow_non_public_urls"]).get("text") or "")
    if not raw:
        raise ValueError("需要订阅 URL 或原始内容")
    nodes = parse_subscription_payload(raw, str(payload.get("name") or "预览"))
    matched = filter_subscription_nodes(nodes, rules)
    return {"total": len(nodes), "matched": len(matched), "excluded": len(nodes) - len(matched),
            "nodes": [public_node(asdict(node)) for node in matched]}


def fetch_subscription_response(
    url: str,
    *,
    etag: str = "",
    last_modified: str = "",
    allow_non_public: bool = False,
) -> dict[str, Any]:
    current_url = url
    deadline = time.monotonic() + SUBSCRIPTION_FETCH_TIMEOUT
    request_headers: dict[str, str] = {}
    if etag:
        request_headers["If-None-Match"] = str(etag)[:512]
    if last_modified:
        request_headers["If-Modified-Since"] = str(last_modified)[:512]
    for redirect_count in range(SUBSCRIPTION_REDIRECT_LIMIT + 1):
        if time.monotonic() >= deadline:
            raise TimeoutError("订阅下载超时")
        parsed, addresses = _public_subscription_addresses(
            current_url,
            allow_non_public=allow_non_public,
        )
        connection, response, deadline_timer = _open_subscription_response(
            parsed,
            addresses,
            deadline=deadline,
            request_headers=request_headers,
        )
        try:
            if response.status in {301, 302, 303, 307, 308}:
                location = response.headers.get("Location")
                if not location:
                    raise ValueError("订阅重定向缺少 Location")
                if redirect_count >= SUBSCRIPTION_REDIRECT_LIMIT:
                    raise ValueError("订阅重定向次数过多")
                redirected_url = urllib.parse.urljoin(current_url, location)
                redirected = urllib.parse.urlsplit(redirected_url)
                if parsed.scheme.lower() == "https" and redirected.scheme.lower() != "https":
                    raise ValueError("HTTPS 订阅不允许降级重定向到 HTTP")
                current_origin = (
                    parsed.scheme.lower(),
                    str(parsed.hostname or "").lower(),
                    parsed.port or (443 if parsed.scheme.lower() == "https" else 80),
                )
                redirected_origin = (
                    redirected.scheme.lower(),
                    str(redirected.hostname or "").lower(),
                    redirected.port or (443 if redirected.scheme.lower() == "https" else 80),
                )
                if current_origin != redirected_origin:
                    request_headers = {}
                current_url = redirected_url
                continue
            response_etag = str(response.headers.get("ETag") or "")[:512]
            response_last_modified = str(response.headers.get("Last-Modified") or "")[:512]
            if response.status == 304:
                return {
                    "not_modified": True,
                    "text": None,
                    "etag": response_etag or str(etag),
                    "last_modified": response_last_modified or str(last_modified),
                    "final_url": current_url,
                    "usage": parse_subscription_userinfo(response.headers.get("subscription-userinfo")),
                }
            if not 200 <= response.status < 300:
                raise RuntimeError(f"订阅 URL 返回 HTTP {response.status}")
            declared_length = safe_int(response.headers.get("Content-Length"), 0)
            if declared_length > SUBSCRIPTION_RESPONSE_LIMIT:
                raise ValueError(f"订阅响应超过 {SUBSCRIPTION_RESPONSE_LIMIT} 字节限制")
            chunks: list[bytes] = []
            total = 0
            while True:
                if time.monotonic() >= deadline:
                    raise TimeoutError("订阅下载超时")
                try:
                    chunk = response.read1(min(65536, SUBSCRIPTION_RESPONSE_LIMIT - total + 1))
                except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("订阅下载超时") from exc
                    raise
                if not chunk:
                    break
                total += len(chunk)
                if total > SUBSCRIPTION_RESPONSE_LIMIT:
                    raise ValueError(f"订阅响应超过 {SUBSCRIPTION_RESPONSE_LIMIT} 字节限制")
                chunks.append(chunk)
            data = b"".join(chunks)
            charset = response.headers.get_content_charset() or "utf-8"
            try:
                text = data.decode(charset, errors="replace")
            except LookupError:
                text = data.decode("utf-8", errors="replace")
            return {
                "not_modified": False,
                "text": text,
                "etag": response_etag,
                "last_modified": response_last_modified,
                "final_url": current_url,
                "usage": parse_subscription_userinfo(response.headers.get("subscription-userinfo")),
            }
        finally:
            deadline_timer.cancel()
            response.close()
            connection.close()
    raise ValueError("订阅重定向次数过多")


def fetch_subscription(url: str) -> str:
    result = fetch_subscription_response(url)
    return str(result.get("text") or "")


def referenced_node_ids(subscriptions: list[dict[str, Any]]) -> set[str]:
    return {
        str(node_id)
        for subscription in subscriptions
        if isinstance(subscription, dict)
        for node_id in subscription.get("node_ids") or []
        if node_id
    }


def _subscription_replacement_plan(
    state: dict[str, Any],
    *,
    url: str,
    subscription: dict[str, Any],
    node_dicts: list[dict[str, Any]],
) -> set[str]:
    replaced_subscriptions = [s for s in state["subscriptions"] if url and s.get("url") == url]
    replaced_node_ids = referenced_node_ids(replaced_subscriptions)
    remaining_subscriptions = [s for s in state["subscriptions"] if s.get("url") != url or not url]
    retained_node_ids = referenced_node_ids(remaining_subscriptions)
    new_node_ids = {str(node.get("id") or "") for node in node_dicts if node.get("id")}
    return replaced_node_ids - retained_node_ids - new_node_ids


def _apply_subscription_replacement(
    state: dict[str, Any],
    *,
    url: str,
    subscription: dict[str, Any],
    node_dicts: list[dict[str, Any]],
    remove_ids: set[str] | None = None,
) -> set[str]:
    if remove_ids is None:
        remove_ids = _subscription_replacement_plan(
            state,
            url=url,
            subscription=subscription,
            node_dicts=node_dicts,
        )
    preserved_browser_node_ids = _browser_node_ids_to_preserve(state, set(remove_ids))
    state["subscriptions"] = [s for s in state["subscriptions"] if s.get("url") != url or not url]
    state["subscriptions"].append(subscription)
    existing: dict[str, dict[str, Any]] = {}
    for node in state["nodes"]:
        node_id = str(node.get("id") or "")
        if node_id in remove_ids and node_id not in preserved_browser_node_ids:
            continue
        record = dict(node)
        if node_id in preserved_browser_node_ids:
            record[webdav_sync.LOCAL_BROWSER_NODE_MARKER] = True
        existing[node_id] = record
    for node in node_dicts:
        record = dict(node)
        previous = existing.get(str(record.get("id") or ""))
        if isinstance(previous, dict):
            record["preferred_core"] = previous.get("preferred_core")
            record["fallback_enabled"] = previous.get("fallback_enabled") is True
            for field_name in ("favorite", "remark", "region_override"):
                if field_name in previous:
                    record[field_name] = previous[field_name]
            override = record.get("region_override")
            if override in REGION_CATALOG:
                record["region"] = REGION_CATALOG[override][0]
        normalize_node_core_settings(record)
        existing[record["id"]] = record
    state["nodes"] = list(existing.values())
    return set(remove_ids)


def _browser_node_ids_to_preserve(
    state: dict[str, Any],
    node_ids: set[str],
    *,
    purge_browser_records: bool = False,
) -> set[str]:
    # 订阅刷新改变节点 ID（机场改参数）时保留浏览器记录与 Profile，原节点标记为保留节点，
    # 由用户换绑；只有手动删除订阅并明确勾选时才连同 Profile 一起删除。
    if purge_browser_records and browser_feature_enabled():
        return set()
    candidates = {str(node_id) for node_id in node_ids if node_id}
    return {
        str(instance.get("node_id") or "")
        for instance in state.get("browser_instances", [])
        if isinstance(instance, dict) and str(instance.get("node_id") or "") in candidates
    }


def _runtime_snapshot_for_nodes(state: dict[str, Any], node_ids: set[str]) -> dict[str, Any]:
    node_ids = {str(node_id) for node_id in node_ids if node_id}
    with PROCESS_LOCK:
        bridge_ids = {
            str(bridge_id)
            for bridge_id, owner_node_id in BRIDGE_NODE_TABLE.items()
            if str(owner_node_id or "") in node_ids
        }
    bridges = [
        dict(bridge)
        for bridge in state.get("bridges", [])
        if isinstance(bridge, dict) and str(bridge.get("node_id") or "") in node_ids
    ]
    bridge_ids.update(str(bridge.get("id") or "") for bridge in bridges if bridge.get("id"))
    instances = []
    if browser_feature_enabled():
        instances = [
            dict(instance)
            for instance in state.get("browser_instances", [])
            if isinstance(instance, dict) and str(instance.get("node_id") or "") in node_ids
        ]
    return {
        "node_ids": node_ids,
        "bridges": bridges,
        "bridge_ids": {bridge_id for bridge_id in bridge_ids if bridge_id},
        "browser_instances": instances,
    }


def _stop_runtime_for_nodes(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Stop removed-node processes without holding STATE_LOCK."""
    killed_browsers = 0
    traffic_by_bridge: dict[str, dict[str, int]] = {}
    failures: list[tuple[str, BaseException]] = []
    instances = snapshot.get("browser_instances") or []
    for instance in instances:
        instance_id = str(instance.get("id") or "")
        if not instance_id:
            continue
        try:
            killed_browsers += stop_browser_instance_process(instance_id)
        except BaseException as error:
            failures.append((f"浏览器 {instance_id}", error))
    if browser_feature_enabled():
        for node_id in snapshot.get("node_ids") or set():
            try:
                killed_browsers += stop_untracked_browser_processes_for_node(str(node_id))
            except BaseException as error:
                failures.append((f"节点 {node_id} 浏览器", error))

    for bridge_id in sorted(snapshot.get("bridge_ids") or set()):
        try:
            live_traffic = live_bridge_traffic_snapshot(bridge_id)
            stopped_traffic = stop_process(bridge_id, pop_traffic=False)
            if live_traffic is not None:
                traffic_by_bridge[bridge_id] = stopped_traffic
        except BaseException as error:
            failures.append((f"代理 {bridge_id}", error))
    if failures:
        details = "; ".join(f"{label}: {error}" for label, error in failures)
        raise RuntimeError(f"清理已删除节点资源失败（{len(failures)} 项）: {details}") from failures[0][1]
    return {
        "killed_browsers": killed_browsers,
        "traffic_by_bridge": traffic_by_bridge,
        "browser_instances": instances,
    }


def _mark_removed_nodes_stopped(
    state: dict[str, Any],
    node_ids: set[str],
    traffic_by_bridge: dict[str, dict[str, int]],
    *,
    preserved_node_ids: set[str] | None = None,
) -> None:
    node_ids = {str(node_id) for node_id in node_ids if node_id}
    preserved = {str(node_id) for node_id in (preserved_node_ids or set()) if node_id}
    stopped_at = now_ts()
    if browser_feature_enabled():
        removed = node_ids - preserved
        state["browser_instances"] = [
            item
            for item in state.get("browser_instances", [])
            if not (isinstance(item, dict) and str(item.get("node_id") or "") in removed)
        ]
        for item in state["browser_instances"]:
            if str(item.get("node_id") or "") in preserved and item.get("status") in ACTIVE_BROWSER_STATUSES:
                # 运行时清理已结束这些浏览器；记录保留，状态改为已停止。
                item.update(
                    {
                        "status": "stopped",
                        "pid": None,
                        "remote_url": "",
                        "remote_container_id": "",
                        "remote_host_port": None,
                        "last_stopped_at": stopped_at,
                        "last_used_at": stopped_at,
                    }
                )
    for bridge in state.get("bridges", []):
        if not isinstance(bridge, dict) or str(bridge.get("node_id") or "") not in node_ids:
            continue
        bridge_id = str(bridge.get("id") or "")
        traffic = traffic_by_bridge.get(bridge_id) or traffic_defaults(
            bridge.get("upload_bytes"),
            bridge.get("download_bytes"),
        )
        bridge.update({**traffic, "status": "stopped", "enabled": False, "pid": None, "stopped_at": stopped_at})
        bridge.pop("error", None)


def _remove_profiles_for_nodes(node_ids: set[str]) -> int:
    removed = 0
    for node_id in {str(node_id) for node_id in node_ids if node_id}:
        root = browser_node_profile_root(node_id)
        if not root.exists():
            continue
        shutil.rmtree(root, ignore_errors=True)
        if not root.exists():
            removed += 1
        else:
            log_event(f"清理节点 Profile 失败: {node_id}", level="warn", category="浏览器")
    return removed


def subscription_node_diff(
    state: dict[str, Any],
    previous: dict[str, Any] | None,
    node_dicts: list[dict[str, Any]],
) -> dict[str, Any]:
    old_ids = {str(item) for item in (previous or {}).get("node_ids") or [] if item}
    old_nodes = {
        str(node.get("id") or ""): node
        for node in state.get("nodes", [])
        if isinstance(node, dict) and str(node.get("id") or "") in old_ids
    }
    new_nodes = {str(node.get("id") or ""): node for node in node_dicts if node.get("id")}
    old_by_name = {str(node.get("name") or ""): node_id for node_id, node in old_nodes.items()}
    new_by_name = {str(node.get("name") or ""): node_id for node_id, node in new_nodes.items()}
    changed_names = sorted(
        name
        for name in old_by_name.keys() & new_by_name.keys()
        if old_by_name[name] != new_by_name[name]
    )
    changed_old_ids = {old_by_name[name] for name in changed_names}
    changed_new_ids = {new_by_name[name] for name in changed_names}
    added_ids = sorted(set(new_nodes) - set(old_nodes) - changed_new_ids)
    removed_ids = sorted(set(old_nodes) - set(new_nodes) - changed_old_ids)
    return {
        "added": len(added_ids),
        "removed": len(removed_ids),
        "changed": len(changed_names),
        "added_names": [str(new_nodes[node_id].get("name") or node_id) for node_id in added_ids][:50],
        "removed_names": [str(old_nodes[node_id].get("name") or node_id) for node_id in removed_ids][:50],
        "changed_names": changed_names[:50],
    }


def _subscription_next_refresh(interval_minutes: int, failures: int = 0, *, current_time: int | None = None) -> int:
    if interval_minutes <= 0:
        return 0
    base = interval_minutes * 60
    delay = min(base * (2 ** max(0, failures - 1)), SUBSCRIPTION_BACKOFF_MAX_SECONDS)
    return (now_ts() if current_time is None else current_time) + delay


def _ensure_subscription_refresh_allowed() -> None:
    if SHUTTING_DOWN.is_set():
        raise RuntimeError("程序正在退出，订阅刷新已取消")


def _append_subscription_refresh_history(
    subscription_id: str,
    *,
    status: str,
    duration_ms: int = 0,
    changes: dict[str, Any] | None = None,
    error: Any = "",
) -> dict[str, Any] | None:
    if status not in {"updated", "not_modified", "failed", "cancelled"}:
        raise ValueError("订阅刷新历史状态无效")
    raw_changes = changes if isinstance(changes, dict) else {}
    entry = {
        "at": now_ts(),
        "status": status,
        "duration_ms": max(0, safe_int(duration_ms, 0)),
        "changes": {
            key: max(0, safe_int(raw_changes.get(key), 0))
            for key in ("added", "removed", "changed")
        },
        "error": str(error or "")[:1000],
    }
    with STATE_LOCK:
        state = load_state()
        subscription = next(
            (item for item in state.get("subscriptions", []) if str(item.get("id") or "") == subscription_id),
            None,
        )
        if subscription is None:
            return None
        history = subscription.get("refresh_history")
        history = list(history) if isinstance(history, list) else []
        history.append(entry)
        subscription["refresh_history"] = history[-SUBSCRIPTION_REFRESH_HISTORY_LIMIT:]
        save_state(state)
    return entry


def import_subscription(payload: dict[str, Any], *, fetch_metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    name = clean_name(str(payload.get("name") or ""), "未命名订阅")
    url = str(payload.get("url") or "").strip()
    raw = str(payload.get("raw") or "").strip()
    with STATE_LOCK:
        old_subscription = next((s for s in load_state()["subscriptions"] if url and s.get("url") == url), {})
        rules = subscription_filter_settings({**old_subscription, **payload})
    filter_subscription_nodes([], rules)
    # 响应头元数据仅由服务端下载流程传入，不接受 JSON 请求伪造流量、到期或缓存字段。
    fetch_metadata = fetch_metadata if isinstance(fetch_metadata, dict) else {}
    if url and not raw:
        with STATE_LOCK:
            state = load_state()
            subscription_settings = normalize_subscription_settings(
                (state.get("settings") or {}).get("subscriptions")
            )
        fetched = fetch_subscription_response(
            url,
            allow_non_public=subscription_settings["allow_non_public_urls"],
        )
        raw = str(fetched.get("text") or "")
        fetch_metadata = fetched
    if not raw:
        raise ValueError("需要订阅 URL 或原始订阅内容")

    nodes = parse_subscription_payload(raw, name)
    if not nodes:
        decoded = decode_base64_text(raw)
        looks_like_clash = any("proxies:" in candidate.lower() for candidate in (raw, decoded or ""))
        if looks_like_clash and import_yaml() is None:
            raise RuntimeError("无法解析 Clash YAML 订阅:缺少 PyYAML 解析器")
        raise ValueError("订阅中没有可用节点，已保留原有订阅状态")
    nodes = filter_subscription_nodes(nodes, rules)
    raw_digest = hashlib.sha256(raw.encode("utf-8", "ignore")).hexdigest()
    imported_at = now_ts()
    node_dicts = [asdict(node) for node in nodes]
    node_ids = [node.id for node in nodes]
    with SUBSCRIPTION_LOCK:
        with SHUTDOWN_LOCK:
            _ensure_subscription_refresh_allowed()
            with STATE_LOCK:
                state = load_state()
                previous = next((s for s in state["subscriptions"] if url and s.get("url") == url), None)
                previous = dict(previous) if isinstance(previous, dict) else None
                subscription_id = str((previous or {}).get("id") or stable_id(name, url, str(time.time_ns()), raw_digest))
                interval = safe_int(
                    payload.get("refresh_interval_minutes"),
                    safe_int((previous or {}).get("refresh_interval_minutes"), 0),
                )
                if interval != 0:
                    interval = min(max(interval, SUBSCRIPTION_REFRESH_MINUTES_MIN), SUBSCRIPTION_REFRESH_MINUTES_MAX)
                changes = subscription_node_diff(state, previous, node_dicts)
                subscription = {
                    **rules,
                    "id": subscription_id,
                    "name": name,
                    "url": url,
                    "last_update": imported_at,
                    "last_attempt": imported_at,
                    "node_count": len(nodes),
                    "node_ids": node_ids,
                    "refresh_interval_minutes": interval,
                    "refresh_failures": 0,
                    "next_refresh_at": _subscription_next_refresh(interval, current_time=imported_at),
                    "etag": str(fetch_metadata.get("etag") or (previous or {}).get("etag") or "")[:512],
                    "last_modified": str(
                        fetch_metadata.get("last_modified") or (previous or {}).get("last_modified") or ""
                    )[:512],
                    "last_error": "",
                    "last_changes": changes,
                    "refresh_history": list((previous or {}).get("refresh_history") or [])[-SUBSCRIPTION_REFRESH_HISTORY_LIMIT:],
                    "usage": {**((previous or {}).get("usage") or {}), **(fetch_metadata.get("usage") or {})},
                }
                remove_ids = _subscription_replacement_plan(
                    state,
                    url=url,
                    subscription=subscription,
                    node_dicts=node_dicts,
                )
                runtime_snapshot = _runtime_snapshot_for_nodes(state, remove_ids)
        # 停止远程 worker 可能需要等待 Broker；不能占用关机屏障，否则 SIGTERM
        # 无法及时开始兜底回收。提交订阅变更前会重新检查关机状态。
        runtime_cleanup = _stop_runtime_for_nodes(runtime_snapshot) if remove_ids else {
            "killed_browsers": 0,
            "traffic_by_bridge": {},
            "browser_instances": [],
        }
        with SHUTDOWN_LOCK:
            _ensure_subscription_refresh_allowed()
            with STATE_LOCK:
                state = load_state()
                _apply_subscription_replacement(
                    state,
                    url=url,
                    subscription=subscription,
                    node_dicts=node_dicts,
                    remove_ids=remove_ids,
                )
                preserved_node_ids = {
                    str(node.get("id") or "")
                    for node in state["nodes"]
                    if str(node.get("id") or "") in remove_ids
                }
                _mark_removed_nodes_stopped(
                    state,
                    remove_ids,
                    runtime_cleanup["traffic_by_bridge"],
                    preserved_node_ids=preserved_node_ids,
                )
                save_state(state)
            release_persisted_bridge_traffic(runtime_cleanup["traffic_by_bridge"])
        purge_ids = remove_ids - preserved_node_ids
        removed_profiles = _remove_profiles_for_nodes(purge_ids) if purge_ids and browser_feature_enabled() else 0
    mark_sync_local_change()
    return {
        "subscription": subscription,
        "nodes": [public_node(node) for node in node_dicts],
        "removed_node_ids": sorted(remove_ids),
        "killed_browsers": runtime_cleanup["killed_browsers"],
        "removed_profiles": removed_profiles,
        "changes": subscription["last_changes"],
    }


def refresh_subscription(subscription_id: str) -> dict[str, Any]:
    with SUBSCRIPTION_LOCK:
        with STATE_LOCK:
            state = load_state()
            subscription = next((dict(s) for s in state["subscriptions"] if s.get("id") == subscription_id), None)
            subscription_settings = normalize_subscription_settings(
                (state.get("settings") or {}).get("subscriptions")
            )
        if not subscription:
            raise ValueError("订阅不存在")
        url = str(subscription.get("url") or "")
        if not url:
            raise ValueError("这个订阅没有 URL，无法刷新")
        with SHUTDOWN_LOCK:
            _ensure_subscription_refresh_allowed()
        attempted_at = now_ts()
        started_at = time.monotonic()
        try:
            fetched = fetch_subscription_response(
                url,
                etag=str(subscription.get("etag") or ""),
                last_modified=str(subscription.get("last_modified") or ""),
                allow_non_public=subscription_settings["allow_non_public_urls"],
            )
            if fetched.get("not_modified"):
                with SHUTDOWN_LOCK:
                    _ensure_subscription_refresh_allowed()
                    with STATE_LOCK:
                        state = load_state()
                        current = next((s for s in state["subscriptions"] if s.get("id") == subscription_id), None)
                        if current is None:
                            raise ValueError("订阅不存在")
                        interval = safe_int(current.get("refresh_interval_minutes"), 0)
                        current.update(
                            {
                                "last_attempt": attempted_at,
                                "refresh_failures": 0,
                                "next_refresh_at": _subscription_next_refresh(interval, current_time=attempted_at),
                                "etag": str(fetched.get("etag") or current.get("etag") or "")[:512],
                                "last_modified": str(
                                    fetched.get("last_modified") or current.get("last_modified") or ""
                                )[:512],
                                "last_error": "",
                                "last_changes": {"added": 0, "removed": 0, "changed": 0},
                            }
                        )
                        if fetched.get("usage"):
                            current["usage"] = {**(current.get("usage") or {}), **fetched["usage"]}
                        save_state(state)
                        result = {
                            "subscription": dict(current),
                            "nodes": [],
                            "not_modified": True,
                            "changes": current["last_changes"],
                        }
            else:
                result = import_subscription(
                    {
                        "name": subscription.get("name"),
                        "url": url,
                        "raw": str(fetched.get("text") or ""),
                        "refresh_interval_minutes": subscription.get("refresh_interval_minutes"),
                    },
                    fetch_metadata=fetched,
                )
            with SHUTDOWN_LOCK:
                _ensure_subscription_refresh_allowed()
                _append_subscription_refresh_history(
                    subscription_id,
                    status="not_modified" if result.get("not_modified") else "updated",
                    duration_ms=int(round((time.monotonic() - started_at) * 1000)),
                    changes=result.get("changes"),
                )
            return result
        except Exception as exc:
            with SHUTDOWN_LOCK:
                _ensure_subscription_refresh_allowed()
                with STATE_LOCK:
                    state = load_state()
                    current = next((s for s in state["subscriptions"] if s.get("id") == subscription_id), None)
                    if current is not None:
                        failures = max(0, safe_int(current.get("refresh_failures"), 0)) + 1
                        interval = safe_int(current.get("refresh_interval_minutes"), 0)
                        current.update(
                            {
                                "last_attempt": attempted_at,
                                "refresh_failures": failures,
                                "next_refresh_at": _subscription_next_refresh(interval, failures, current_time=attempted_at),
                                "last_error": str(exc)[:1000],
                            }
                        )
                        save_state(state)
                try:
                    _append_subscription_refresh_history(
                        subscription_id,
                        status="failed",
                        duration_ms=int(round((time.monotonic() - started_at) * 1000)),
                        error=exc,
                    )
                except Exception as history_error:
                    log_event(f"记录订阅刷新历史失败：{history_error}", level="warn", category="订阅")
            raise


def subscription_refresh_task_payload(task_id: str | None = None) -> dict[str, Any]:
    with SUBSCRIPTION_REFRESH_TASK_LOCK:
        if task_id:
            task = SUBSCRIPTION_REFRESH_TASKS.get(task_id)
            if task is None:
                raise ValueError("订阅刷新任务不存在")
            return json.loads(json.dumps(task, ensure_ascii=False))
        return {
            "tasks": json.loads(json.dumps(list(SUBSCRIPTION_REFRESH_TASKS.values()), ensure_ascii=False))
        }


def _update_subscription_refresh_task(task_id: str, **updates: Any) -> None:
    with SUBSCRIPTION_REFRESH_TASK_LOCK:
        task = SUBSCRIPTION_REFRESH_TASKS.get(task_id)
        if task is not None:
            task.update(updates)
            task["updated_at"] = now_ts()


def _cancel_pending_subscription_refresh_items(
    task_id: str,
    subscription_ids: list[str],
    *,
    start_index: int = 0,
    include_active: bool = False,
    error: str,
) -> list[str]:
    pending_history_ids: list[str] = []
    with SUBSCRIPTION_REFRESH_TASK_LOCK:
        task = SUBSCRIPTION_REFRESH_TASKS.get(task_id)
        if task is None:
            return pending_history_ids
        active_index = task.get("active_index")
        active_index = active_index if isinstance(active_index, int) else -1
        items = task.get("items")
        if not isinstance(items, list):
            items = []
            task["items"] = items
        for index in range(max(0, start_index), len(subscription_ids)):
            subscription_id = subscription_ids[index]
            if not include_active and index == active_index:
                continue
            if index >= len(items):
                items.extend([None] * (index + 1 - len(items)))
            if items[index] is not None:
                continue
            items[index] = {
                "subscription_id": subscription_id,
                "status": "cancelled",
                "error": error,
            }
            pending_history_ids.append(subscription_id)
        task["completed"] = sum(1 for item in items if item is not None)
        task["updated_at"] = now_ts()
    return pending_history_ids


def _record_subscription_refresh_cancellations(subscription_ids: list[str], error: str) -> None:
    for subscription_id in subscription_ids:
        try:
            _append_subscription_refresh_history(
                subscription_id,
                status="cancelled",
                error=error,
            )
        except Exception as history_error:
            log_event(f"记录订阅取消历史失败：{history_error}", level="warn", category="订阅")


def _run_subscription_refresh_task(task_id: str, subscription_ids: list[str]) -> None:
    cancel_event = SUBSCRIPTION_REFRESH_CANCEL_EVENTS[task_id]
    cancelled_before_completion = False
    _update_subscription_refresh_task(
        task_id,
        status=(
            "cancelled"
            if cancel_event.is_set() and SHUTTING_DOWN.is_set()
            else "cancelling"
            if cancel_event.is_set()
            else "running"
        ),
        started_at=now_ts(),
    )
    try:
        for index, subscription_id in enumerate(subscription_ids):
            if cancel_event.is_set() or SHUTTING_DOWN.is_set():
                cancelled_before_completion = True
                pending_history_ids = _cancel_pending_subscription_refresh_items(
                    task_id,
                    subscription_ids,
                    start_index=index,
                    include_active=False,
                    error="订阅刷新已取消",
                )
                _record_subscription_refresh_cancellations(pending_history_ids, "订阅刷新已取消")
                break
            with SUBSCRIPTION_REFRESH_TASK_LOCK:
                task = SUBSCRIPTION_REFRESH_TASKS.get(task_id)
                if task is None:
                    break
                if index < len(task.get("items") or []) and task["items"][index] is not None:
                    continue
                task["active_index"] = index
                task["updated_at"] = now_ts()
            started = time.monotonic()
            try:
                result = refresh_subscription(subscription_id)
                changes = result.get("changes") or {}
                item = {
                    "subscription_id": subscription_id,
                    "status": "not_modified" if result.get("not_modified") else "updated",
                    "duration_ms": int(round((time.monotonic() - started) * 1000)),
                    "changes": {
                        key: max(0, safe_int(changes.get(key), 0))
                        for key in ("added", "removed", "changed")
                    },
                }
            except Exception as exc:
                item = {
                    "subscription_id": subscription_id,
                    "status": "failed",
                    "duration_ms": int(round((time.monotonic() - started) * 1000)),
                    "error": str(exc)[:1000],
                }
            with SHUTDOWN_LOCK:
                with SUBSCRIPTION_REFRESH_TASK_LOCK:
                    task = SUBSCRIPTION_REFRESH_TASKS.get(task_id)
                    if task is not None:
                        task["active_index"] = None
                        if index >= len(task["items"]):
                            task["items"].extend([None] * (index + 1 - len(task["items"])))
                        if task["items"][index] is None:
                            task["items"][index] = item
                        task["completed"] = sum(1 for entry in task["items"] if entry is not None)
                        task["updated_at"] = now_ts()
        with SUBSCRIPTION_REFRESH_TASK_LOCK:
            task = SUBSCRIPTION_REFRESH_TASKS.get(task_id)
            if task is not None:
                task["active_index"] = None
                task["status"] = (
                    "cancelled"
                    if task.get("status") == "cancelled"
                    or cancelled_before_completion
                    or cancel_event.is_set()
                    or SHUTTING_DOWN.is_set()
                    else "completed"
                )
                task["finished_at"] = now_ts()
                task["updated_at"] = now_ts()
    finally:
        with SUBSCRIPTION_REFRESH_TASK_LOCK:
            SUBSCRIPTION_REFRESH_CANCEL_EVENTS.pop(task_id, None)


def start_subscription_refresh_task(payload: dict[str, Any]) -> dict[str, Any]:
    raw_ids = payload.get("subscription_ids")
    if raw_ids is None and payload.get("ids") is not None:
        raw_ids = payload.get("ids")
    if not isinstance(raw_ids, list):
        raise ValueError("subscription_ids 必须是数组")
    subscription_ids = list(dict.fromkeys(str(item or "") for item in raw_ids if str(item or "")))
    if not subscription_ids:
        raise ValueError("没有可刷新的订阅")
    if len(subscription_ids) > SUBSCRIPTION_BATCH_LIMIT:
        raise ValueError(f"单次最多刷新 {SUBSCRIPTION_BATCH_LIMIT} 个订阅")
    with SHUTDOWN_LOCK:
        _ensure_subscription_refresh_allowed()
        with STATE_LOCK:
            state = load_state()
            known_ids = {str(item.get("id") or "") for item in state.get("subscriptions", [])}
        unknown = [item for item in subscription_ids if item not in known_ids]
        if unknown:
            raise ValueError(f"订阅不存在: {unknown[0]}")
        task_id = stable_id("subscription-refresh-task", str(time.time_ns()), *subscription_ids)
        now = now_ts()
        task = {
            "id": task_id,
            "status": "queued",
            "subscription_ids": subscription_ids,
            "total": len(subscription_ids),
            "completed": 0,
            "items": [None] * len(subscription_ids),
            "active_index": None,
            "created_at": now,
            "updated_at": now,
        }
        cancel_event = threading.Event()
        with SUBSCRIPTION_REFRESH_TASK_LOCK:
            if any(
                item.get("status") in {"queued", "running", "cancelling"}
                for item in SUBSCRIPTION_REFRESH_TASKS.values()
            ):
                raise RuntimeError("已有批量订阅刷新任务正在运行")
            SUBSCRIPTION_REFRESH_TASKS[task_id] = task
            SUBSCRIPTION_REFRESH_CANCEL_EVENTS[task_id] = cancel_event
            while len(SUBSCRIPTION_REFRESH_TASKS) > SUBSCRIPTION_REFRESH_TASK_LIMIT:
                SUBSCRIPTION_REFRESH_TASKS.popitem(last=False)
    threading.Thread(
        target=_run_subscription_refresh_task,
        args=(task_id, subscription_ids),
        name=f"subscription-refresh-task-{task_id[:8]}",
        daemon=True,
    ).start()
    return subscription_refresh_task_payload(task_id)


def cancel_subscription_refresh_task(task_id: str) -> dict[str, Any]:
    with SUBSCRIPTION_REFRESH_TASK_LOCK:
        task = SUBSCRIPTION_REFRESH_TASKS.get(task_id)
        if task is None:
            raise ValueError("订阅刷新任务不存在")
        cancel_event = SUBSCRIPTION_REFRESH_CANCEL_EVENTS.get(task_id)
        if cancel_event is not None:
            cancel_event.set()
        if task.get("status") in {"queued", "running"}:
            task["status"] = "cancelling"
            task["updated_at"] = now_ts()
    return subscription_refresh_task_payload(task_id)


def update_subscription_schedule(payload: dict[str, Any]) -> dict[str, Any]:
    subscription_id = str(payload.get("id") or "")
    if "refresh_interval_minutes" not in payload:
        raise ValueError("缺少刷新周期")
    interval = safe_int(payload.get("refresh_interval_minutes"), -1)
    if interval != 0 and not SUBSCRIPTION_REFRESH_MINUTES_MIN <= interval <= SUBSCRIPTION_REFRESH_MINUTES_MAX:
        raise ValueError(
            f"刷新周期只能为 0（关闭）或 {SUBSCRIPTION_REFRESH_MINUTES_MIN}..{SUBSCRIPTION_REFRESH_MINUTES_MAX} 分钟"
        )
    with STATE_LOCK:
        state = load_state()
        subscription = next((s for s in state["subscriptions"] if s.get("id") == subscription_id), None)
        if subscription is None:
            raise ValueError("订阅不存在")
        subscription["refresh_interval_minutes"] = interval
        subscription["refresh_failures"] = 0
        subscription["next_refresh_at"] = _subscription_next_refresh(interval)
        subscription["last_error"] = ""
        save_state(state)
        result = dict(subscription)
    SUBSCRIPTION_SCHEDULER_WAKE.set()
    return result


def update_subscription_details(payload: dict[str, Any]) -> dict[str, Any]:
    subscription_id = str(payload.get("id") or "")
    with SUBSCRIPTION_LOCK:
        with STATE_LOCK:
            state = load_state()
            subscription = next((s for s in state["subscriptions"] if s.get("id") == subscription_id), None)
            if subscription is None:
                raise ValueError("订阅不存在")
            rules = subscription_filter_settings({**subscription, **payload})
            filter_subscription_nodes([], rules)
            rules_changed = any(subscription.get(key) != value for key, value in rules.items())
            subscription.update(rules)
            if "name" in payload:
                subscription["name"] = clean_name(str(payload["name"] or ""), "未命名订阅")
            if rules_changed:
                # 过滤条件变化后不再使用 304 缓存，下一次刷新必须取得完整节点列表。
                subscription["etag"] = ""
                subscription["last_modified"] = ""
            save_state(state)
            result = dict(subscription)
    mark_sync_local_change()
    return {"ok": True, "subscription": result, "refresh_required": rules_changed}


def update_subscription_settings(payload: dict[str, Any]) -> dict[str, bool]:
    if not isinstance(payload.get("allow_non_public_urls"), bool):
        raise ValueError("allow_non_public_urls 必须是布尔值")
    settings = normalize_subscription_settings(payload)
    with STATE_LOCK:
        state = load_state()
        state.setdefault("settings", {})["subscriptions"] = settings
        save_state(state)
    return settings


def due_subscription_ids(current_time: int | None = None) -> list[str]:
    current = now_ts() if current_time is None else current_time
    with STATE_LOCK:
        state = load_state()
        return [
            str(subscription.get("id") or "")
            for subscription in state.get("subscriptions", [])
            if subscription.get("url")
            and safe_int(subscription.get("refresh_interval_minutes"), 0) > 0
            and 0 < safe_int(subscription.get("next_refresh_at"), 0) <= current
            and subscription.get("id")
        ]


def run_due_subscription_refreshes() -> int:
    refreshed = 0
    for subscription_id in due_subscription_ids():
        if SHUTTING_DOWN.is_set():
            break
        try:
            result = refresh_subscription(subscription_id)
            changes = result.get("changes") or {}
            if result.get("not_modified"):
                message = "订阅自动刷新：远端未变化"
            else:
                message = (
                    "订阅自动刷新完成："
                    f"新增 {safe_int(changes.get('added'), 0)}、"
                    f"删除 {safe_int(changes.get('removed'), 0)}、"
                    f"变更 {safe_int(changes.get('changed'), 0)}"
                )
            log_event(message, category="订阅")
            refreshed += 1
        except Exception as exc:
            log_event(f"订阅自动刷新失败，已保留旧节点：{exc}", level="warn", category="订阅")
    return refreshed


def subscription_scheduler_loop() -> None:
    while not SHUTTING_DOWN.is_set():
        try:
            run_due_subscription_refreshes()
        except Exception as exc:
            log_event(f"订阅自动刷新调度异常：{exc}", level="warn", category="订阅")
        SUBSCRIPTION_SCHEDULER_WAKE.wait(timeout=30.0)
        SUBSCRIPTION_SCHEDULER_WAKE.clear()


def start_subscription_scheduler() -> None:
    global SUBSCRIPTION_SCHEDULER_THREAD
    with SUBSCRIPTION_SCHEDULER_LOCK:
        if SUBSCRIPTION_SCHEDULER_THREAD is not None and SUBSCRIPTION_SCHEDULER_THREAD.is_alive():
            return
        thread = threading.Thread(
            target=subscription_scheduler_loop,
            name="subscription-refresh-scheduler",
            daemon=True,
        )
        SUBSCRIPTION_SCHEDULER_THREAD = thread
        thread.start()


def delete_subscription(subscription_id: str, *, delete_browser_profiles: bool = False) -> dict[str, Any]:
    with SUBSCRIPTION_LOCK:
        with SHUTDOWN_LOCK:
            _ensure_subscription_refresh_allowed()
            with STATE_LOCK:
                state = load_state()
                subscription = next((s for s in state["subscriptions"] if s.get("id") == subscription_id), None)
                if not subscription:
                    raise ValueError("订阅不存在")
                remaining = [s for s in state["subscriptions"] if s.get("id") != subscription_id]
                remove_ids = {
                    str(node_id)
                    for node_id in (set(subscription.get("node_ids") or []) - referenced_node_ids(remaining))
                    if node_id
                }
                runtime_snapshot = _runtime_snapshot_for_nodes(state, remove_ids)
        # 与订阅替换相同，Broker 网络调用必须位于 SHUTDOWN_LOCK 之外。
        runtime_cleanup = _stop_runtime_for_nodes(runtime_snapshot) if remove_ids else {
            "killed_browsers": 0,
            "traffic_by_bridge": {},
            "browser_instances": [],
        }
        with SHUTDOWN_LOCK:
            _ensure_subscription_refresh_allowed()
            with STATE_LOCK:
                state = load_state()
                state["subscriptions"] = [s for s in state["subscriptions"] if s.get("id") != subscription_id]
                current_remove_ids = {
                    str(node_id)
                    for node_id in (set(subscription.get("node_ids") or []) - referenced_node_ids(state["subscriptions"]))
                    if node_id
                }
                preserved_browser_node_ids = _browser_node_ids_to_preserve(
                    state,
                    current_remove_ids,
                    purge_browser_records=delete_browser_profiles,
                )
                retained_nodes: list[dict[str, Any]] = []
                for node in state["nodes"]:
                    node_id = str(node.get("id") or "")
                    if node_id in current_remove_ids and node_id not in preserved_browser_node_ids:
                        continue
                    record = dict(node)
                    if node_id in preserved_browser_node_ids:
                        record[webdav_sync.LOCAL_BROWSER_NODE_MARKER] = True
                    retained_nodes.append(record)
                state["nodes"] = retained_nodes
                _mark_removed_nodes_stopped(
                    state,
                    current_remove_ids,
                    runtime_cleanup["traffic_by_bridge"],
                    preserved_node_ids=preserved_browser_node_ids,
                )
                save_state(state)
            release_persisted_bridge_traffic(runtime_cleanup["traffic_by_bridge"])
            purge_summary = {
                "removed_instances": [
                    str(item.get("id") or "")
                    for item in runtime_snapshot["browser_instances"]
                    if item.get("id") and str(item.get("node_id") or "") not in preserved_browser_node_ids
                ],
                "preserved_instances": [
                    str(item.get("id") or "")
                    for item in state.get("browser_instances", [])
                    if str(item.get("node_id") or "") in preserved_browser_node_ids
                ],
                "killed_browsers": runtime_cleanup["killed_browsers"],
                "removed_profiles": 0,
            }
        purge_summary["removed_profiles"] = (
            _remove_profiles_for_nodes(remove_ids - preserved_browser_node_ids) if browser_feature_enabled() else 0
        )

    mark_sync_local_change()
    # WebDAV enabled → push deletion (subscriptions + remote Profile packages).
    try:
        get_sync_engine().maybe_auto_push(reason="删除订阅")
    except Exception:
        pass
    return {
        "ok": True,
        "removed_node_ids": sorted(remove_ids),
        **purge_summary,
    }


def find_node(state: dict[str, Any], node_id: str) -> dict[str, Any]:
    node = next((n for n in state["nodes"] if n.get("id") == node_id), None)
    if not node:
        raise ValueError("节点不存在")
    return node


def update_node_core_settings(payload: dict[str, Any]) -> dict[str, Any]:
    node_id = str(payload.get("node_id") or payload.get("id") or "")
    selected = str(payload.get("preferred_core") or "")
    if selected not in {"sing-box", "xray"}:
        raise ValueError("首选核心只能是 sing-box 或 xray")
    if not isinstance(payload.get("fallback_enabled"), bool):
        raise ValueError("fallback_enabled 必须是布尔值")
    with STATE_LOCK:
        state = load_state()
        node = find_node(state, node_id)
        options = compatible_cores(node)
        if selected not in options:
            raise ValueError(f"当前节点配置不兼容 {selected}")
        if payload["fallback_enabled"] and len(options) < 2:
            raise ValueError("当前节点没有兼容的备用核心")
        node["preferred_core"] = selected
        node["fallback_enabled"] = payload["fallback_enabled"]
        normalize_node_core_settings(node)
        save_state(state)
        public = public_node(node)
    mark_sync_local_change()
    return {"ok": True, "node": public}


def update_node_details(payload: dict[str, Any]) -> dict[str, Any]:
    with STATE_LOCK:
        state = load_state()
        node = find_node(state, str(payload.get("node_id") or ""))
        if "favorite" in payload:
            if not isinstance(payload["favorite"], bool):
                raise ValueError("favorite 必须是布尔值")
            node["favorite"] = payload["favorite"]
        if "remark" in payload:
            if not isinstance(payload["remark"], str) or len(payload["remark"]) > 500:
                raise ValueError("备注必须是不超过 500 字的文本")
            node["remark"] = payload["remark"].strip()
        if "region" in payload:
            value = str(payload["region"] or "").strip()
            code = value.upper() if value.upper() in REGION_CATALOG else node_region_code(value)
            if value in {"", "auto"}:
                node.pop("region_override", None)
                node["region"] = node_region(str(node.get("name") or ""), str(node.get("server") or ""))
            elif code == "ZZ" and value not in {"未分类", "ZZ", "zz"}:
                raise ValueError("不支持该地区，请使用地区列表或 auto")
            else:
                node["region_override"] = code
                node["region"] = REGION_CATALOG[code][0] if code in REGION_CATALOG else "未分类"
        save_state(state)
        result = public_node(node)
    mark_sync_local_change()
    return {"ok": True, "node": result}


def active_bridge_for_node(state: dict[str, Any], node_id: str) -> dict[str, Any] | None:
    bridges = [bridge for bridge in state.get("bridges", []) if bridge.get("node_id") == node_id]
    bridges.sort(key=lambda bridge: safe_int(bridge.get("started_at"), 0), reverse=True)
    return next(
        (
            bridge
            for bridge in bridges
            if bridge.get("status") in ACTIVE_BRIDGE_STATUSES and safe_int(bridge.get("port"), 0) > 0
        ),
        None,
    )


def remembered_port_for_node(state: dict[str, Any], node_id: str) -> int | None:
    bridges = [
        bridge
        for bridge in state.get("bridges", [])
        if bridge.get("node_id") == node_id and safe_int(bridge.get("port"), 0) > 0
    ]
    bridges.sort(
        key=lambda bridge: max(safe_int(bridge.get("started_at"), 0), safe_int(bridge.get("stopped_at"), 0)),
        reverse=True,
    )
    if not bridges:
        return None
    return safe_int(bridges[0].get("port"), 0) or None


def is_port_free(port: int, listen_host: str = LOCAL_PROXY_LISTEN_HOST) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((listen_host, port))
        except OSError:
            return False
    return True


def next_free_port(
    preferred: int | None = None,
    listen_host: str = LOCAL_PROXY_LISTEN_HOST,
) -> int:
    start = preferred or DEFAULT_SOCKS_PORT
    for port in range(start, start + 1000):
        free = (
            is_port_free(port)
            if listen_host == LOCAL_PROXY_LISTEN_HOST
            else is_port_free(port, listen_host)
        )
        if free:
            return port
    raise RuntimeError("没有可用的本地端口")


def docker_proxy_port_range() -> tuple[int, int]:
    start = safe_int(os.environ.get("PROXY_BRIDGE_PROXY_PORT_START"), DEFAULT_SOCKS_PORT)
    end = safe_int(os.environ.get("PROXY_BRIDGE_PROXY_PORT_END"), DEFAULT_DOCKER_PROXY_PORT_END)
    # Public ports reserve +1000 for the loopback-only core inbound.
    if not 1 <= start <= 64535 or not start <= end <= 64535:
        raise RuntimeError("Docker 代理端口范围无效")
    return start, end


def next_free_public_proxy_port(
    preferred: int | None = None,
    listen_host: str = LAN_PROXY_LISTEN_HOST,
) -> int:
    """Pick a public proxy port from the range published by Docker."""
    start, end = docker_proxy_port_range()
    candidate = preferred if preferred is not None and start <= preferred <= end else start
    ports = list(range(candidate, end + 1)) + list(range(start, candidate))
    for port in ports:
        if is_port_free(port, listen_host):
            return port
    raise RuntimeError(f"Docker 已发布范围 {start}-{end} 内没有可用的代理端口")


def wait_for_port(port: int, timeout: float = 2.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.15)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.05)
    return False


def tls_config(node: dict[str, Any], default_enabled: bool = False) -> dict[str, Any] | None:
    settings = node.get("settings") or {}
    security = str(node.get("security") or "").lower()
    enabled = default_enabled or security in {"tls", "reality"} or truthy(settings.get("tls"))
    if not enabled:
        return None
    tls: dict[str, Any] = {"enabled": True}
    server_name = settings.get("sni") or settings.get("servername") or settings.get("host")
    if server_name:
        tls["server_name"] = server_name
    if truthy(settings.get("allow_insecure")):
        tls["insecure"] = True
    fingerprint = settings.get("fingerprint")
    if fingerprint:
        tls["utls"] = {"enabled": True, "fingerprint": fingerprint}
    public_key = settings.get("public_key")
    if security == "reality" or public_key:
        reality = {"enabled": True}
        if public_key:
            reality["public_key"] = public_key
        if settings.get("short_id"):
            reality["short_id"] = settings["short_id"]
        tls["reality"] = reality
    return tls


def transport_config(node: dict[str, Any]) -> dict[str, Any] | None:
    settings = node.get("settings") or {}
    transport = str(node.get("transport") or "").lower()
    if transport == "ws":
        ws: dict[str, Any] = {"type": "ws"}
        if settings.get("path"):
            ws["path"] = settings["path"]
        if settings.get("host"):
            ws["headers"] = {"Host": settings["host"]}
        return ws
    if transport == "grpc":
        return {"type": "grpc", "service_name": settings.get("serviceName") or settings.get("service_name") or ""}
    if transport in {"http", "h2"}:
        return {"type": "http", "path": settings.get("path") or "/"}
    return None


def sing_box_outbound(node: dict[str, Any]) -> dict[str, Any]:
    settings = node.get("settings") or {}
    protocol = node["protocol"]
    common: dict[str, Any] = {
        "type": protocol,
        "tag": "proxy",
        "server": node["server"],
        "server_port": int(node["port"]),
    }
    if protocol == "vless":
        outbound = {
            **common,
            "type": "vless",
            "uuid": settings.get("uuid", ""),
            "flow": settings.get("flow") or "",
        }
        if not outbound["flow"]:
            outbound.pop("flow")
    elif protocol == "vmess":
        outbound = {
            **common,
            "type": "vmess",
            "uuid": settings.get("uuid", ""),
            "alter_id": safe_int(settings.get("alter_id"), 0),
            "security": settings.get("security") or "auto",
        }
    elif protocol == "shadowsocks":
        outbound = {
            **common,
            "type": "shadowsocks",
            "method": settings.get("method", ""),
            "password": settings.get("password", ""),
        }
    elif protocol == "trojan":
        outbound = {**common, "type": "trojan", "password": settings.get("password", "")}
    elif protocol == "hysteria2":
        outbound = {**common, "type": "hysteria2", "password": settings.get("password", "")}
    elif protocol == "tuic":
        outbound = {
            **common,
            "type": "tuic",
            "uuid": settings.get("uuid", ""),
            "password": settings.get("password", ""),
        }
        if settings.get("congestion_control"):
            outbound["congestion_control"] = settings["congestion_control"]
    elif protocol == "socks":
        outbound = {**common, "type": "socks", "version": "5"}
        if settings.get("username"):
            outbound["username"] = settings["username"]
        if settings.get("password"):
            outbound["password"] = settings["password"]
    elif protocol == "naive":
        outbound = {
            **common,
            "type": "naive",
            "username": settings.get("username", ""),
            "password": settings.get("password", ""),
        }
    elif protocol == "anytls":
        outbound = {**common, "type": "anytls", "password": settings.get("password", "")}
    else:
        raise ValueError(f"暂不支持生成 {protocol} 的 sing-box 配置")

    tls = tls_config(node, default_enabled=protocol in {"trojan", "hysteria2", "tuic", "naive", "anytls"})
    if tls:
        outbound["tls"] = tls
    transport = transport_config(node)
    if transport:
        outbound["transport"] = transport
    return outbound


def sing_box_config(node: dict[str, Any], local_port: int) -> dict[str, Any]:
    return {
        "log": {"level": "info", "timestamp": True},
        "inbounds": [
            {
                "type": "socks",
                "tag": "socks-in",
                "listen": "127.0.0.1",
                "listen_port": local_port,
            }
        ],
        "outbounds": [sing_box_outbound(node), {"type": "direct", "tag": "direct"}],
        "route": {
            "rules": [
                {
                    "inbound": "socks-in",
                    "action": "sniff",
                }
            ],
            "final": "proxy",
        },
    }


def xray_stream_settings(node: dict[str, Any]) -> dict[str, Any]:
    settings = node.get("settings") or {}
    transport = guess_transport({}, str(node.get("transport") or "")) or "tcp"
    security = str(node.get("security") or "").lower()
    stream: dict[str, Any] = {"network": transport, "security": security or "none"}
    if stream["security"] == "reality":
        reality: dict[str, Any] = {}
        server_name = settings.get("sni") or settings.get("host") or node.get("server")
        if server_name:
            reality["serverName"] = server_name
        if settings.get("fingerprint"):
            reality["fingerprint"] = settings["fingerprint"]
        if settings.get("public_key"):
            reality["publicKey"] = settings["public_key"]
        if settings.get("short_id"):
            reality["shortId"] = settings["short_id"]
        if settings.get("spider_x"):
            reality["spiderX"] = settings["spider_x"]
        stream["realitySettings"] = reality
    elif stream["security"] == "tls":
        stream["tlsSettings"] = {
            "serverName": settings.get("sni") or settings.get("host") or node.get("server"),
            "allowInsecure": truthy(settings.get("allow_insecure")),
        }
        if settings.get("fingerprint"):
            stream["tlsSettings"]["fingerprint"] = settings["fingerprint"]
    if transport == "ws":
        ws: dict[str, Any] = {"path": settings.get("path") or "/"}
        if settings.get("host"):
            ws["headers"] = {"Host": settings["host"]}
        stream["wsSettings"] = ws
    elif transport == "grpc":
        stream["grpcSettings"] = {
            "serviceName": settings.get("serviceName") or settings.get("service_name") or "",
        }
    elif transport in {"http", "h2"}:
        stream["network"] = "http"
        http: dict[str, Any] = {"path": settings.get("path") or "/"}
        if settings.get("host"):
            http["host"] = [settings["host"]]
        stream["httpSettings"] = http
    elif transport == "xhttp":
        xhttp: dict[str, Any] = {}
        if settings.get("path"):
            xhttp["path"] = settings["path"]
        if settings.get("host"):
            xhttp["host"] = settings["host"]
        if settings.get("mode"):
            xhttp["mode"] = settings["mode"]
        stream["xhttpSettings"] = xhttp
    return stream


def xray_outbound(node: dict[str, Any]) -> dict[str, Any]:
    settings = node.get("settings") or {}
    protocol = str(node.get("protocol") or "")
    if protocol in {"vless", "vmess"}:
        user: dict[str, Any] = {"id": settings.get("uuid", "")}
        if protocol == "vless":
            user["encryption"] = "none"
            if settings.get("flow"):
                user["flow"] = settings["flow"]
        else:
            user["alterId"] = safe_int(settings.get("alter_id"), 0)
            user["security"] = settings.get("security") or "auto"
        outbound_settings = {
            "vnext": [
                {
                    "address": node["server"],
                    "port": int(node["port"]),
                    "users": [user],
                }
            ]
        }
    elif protocol == "trojan":
        outbound_settings = {
            "servers": [
                {
                    "address": node["server"],
                    "port": int(node["port"]),
                    "password": settings.get("password", ""),
                }
            ]
        }
    elif protocol == "shadowsocks":
        outbound_settings = {
            "servers": [
                {
                    "address": node["server"],
                    "port": int(node["port"]),
                    "method": settings.get("method", ""),
                    "password": settings.get("password", ""),
                }
            ]
        }
    else:
        raise ValueError(f"暂不支持生成 {protocol} 的 xray 配置")
    outbound = {"protocol": protocol, "settings": outbound_settings}
    if protocol in {"vless", "vmess", "trojan"}:
        outbound["streamSettings"] = xray_stream_settings(node)
    return outbound


def xray_config(node: dict[str, Any], local_port: int) -> dict[str, Any]:
    if "xray" not in compatible_cores(node):
        raise ValueError("节点配置不兼容 xray")
    return {
        "log": {"loglevel": "warning"},
        "inbounds": [
            {
                "listen": "127.0.0.1",
                "port": local_port,
                "protocol": "socks",
                "settings": {"auth": "noauth", "udp": True},
            }
        ],
        "outbounds": [xray_outbound(node)],
    }


def generate_config(core: str, node: dict[str, Any], local_port: int) -> dict[str, Any]:
    if core == "xray":
        return xray_config(node, local_port)
    return sing_box_config(node, local_port)


def core_command(core: str, config_path: Path) -> list[str]:
    paths = core_paths()
    binary = paths.get(core) or ensure_core_installed(core)
    if not binary:
        raise FileNotFoundError(f"找不到 {core}，请先安装对应核心")
    if core == "xray":
        return [binary, "run", "-config", str(config_path)]
    return [binary, "run", "-c", str(config_path)]


def _popen_kwargs() -> dict[str, Any]:
    """Process group isolation where supported (POSIX new session / Windows new group)."""
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        return {"creationflags": creationflags} if creationflags else {}
    return {"start_new_session": True}


def terminate_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt" or not hasattr(os, "killpg"):
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except Exception:
        process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except Exception:
            process.kill()
        process.wait(timeout=3)


def _validate_active_bridge_request(
    active_bridge: dict[str, Any],
    *,
    requested_core: str,
    requested_port: int | None,
) -> None:
    active_core = str(active_bridge.get("core") or "")
    active_port = safe_int(active_bridge.get("port"), 0)
    if requested_core != "auto" and requested_core != active_core:
        raise ValueError(f"活动本地代理核心为 {active_core}，与请求的 {requested_core} 冲突")
    if requested_port is not None and requested_port != active_port:
        raise ValueError(f"活动本地代理端口为 {active_port}，与请求的 {requested_port} 冲突")


def _prepare_bridge_start(payload: dict[str, Any]) -> dict[str, Any]:
    node_id = str(payload.get("node_id") or "")
    requested_port = safe_int(payload.get("port"), 0) or None
    if requested_port is not None and not 1 <= requested_port <= 65535:
        raise ValueError("本地代理端口必须在 1..65535 之间")
    if docker_mode() and requested_port is not None:
        start, end = docker_proxy_port_range()
        if not start <= requested_port <= end:
            raise ValueError(f"Docker 模式下本地代理端口必须在已发布范围 {start}..{end} 内")
    requested_core = str(payload.get("core") or "auto")
    if requested_core not in {"auto", "sing-box", "xray"}:
        raise ValueError("核心只能是 sing-box、xray 或 auto")
    preserve_traffic = bool(payload.get("restore"))
    initial_upload = safe_int(payload.get("upload_bytes"), 0) if preserve_traffic else 0
    initial_download = safe_int(payload.get("download_bytes"), 0) if preserve_traffic else 0
    with STATE_LOCK:
        state = load_state()
        node = dict(find_node(state, node_id))
        switch_from = str(payload.get("_health_switch_from") or "")
        if switch_from and not health_switch_allowed(state, switch_from, node_id):
            raise ValueError("自动切换条件已变化，取消切换")
        changed = sync_process_status(state)
        active_bridge = active_bridge_for_node(state, node_id)
        if active_bridge:
            _validate_active_bridge_request(
                active_bridge,
                requested_core=requested_core,
                requested_port=requested_port,
            )
            if changed:
                save_state(state)
            return {"active_bridge": active_bridge}
        with PROCESS_LOCK:
            residual_bridge_ids = [
                bridge_id
                for bridge_id, owner_node_id in BRIDGE_NODE_TABLE.items()
                if owner_node_id == node_id
            ]
        if residual_bridge_ids:
            raise RuntimeError("该节点仍有未清理的本地代理资源，无法重复启动")
        normalize_node_core_settings(node)
        core_attempts = core_attempt_order(node, requested_core)
        core = core_attempts[0]
        preferred_port = requested_port or remembered_port_for_node(state, node_id)
        if docker_mode() and preferred_port is not None:
            start, end = docker_proxy_port_range()
            if not start <= preferred_port <= end:
                preferred_port = None
        listen_host = proxy_listen_host(state)
        lan_security = normalize_lan_security((state.get("settings") or {}).get("lan_security"))
    return {
        "node_id": node_id,
        "node": node,
        "requested_core": requested_core,
        "requested_port": requested_port,
        # 自动切换必须复用原端口；普通启动仍允许寻找相邻空闲端口。
        "require_exact_port": payload.get("_require_exact_port") is True,
        "core": core,
        "core_attempts": core_attempts,
        "preferred_port": preferred_port,
        "listen_host": listen_host,
        "lan_security": lan_security,
        "initial_upload": initial_upload,
        "initial_download": initial_download,
    }


def _prepare_core_command(plan: dict[str, Any]) -> list[str]:
    placeholder = RUNTIME_ROOT / f".{plan['core']}-preflight.json"
    return core_command(str(plan["core"]), placeholder)


def _retarget_core_command(command: list[str], core: str, config_path: Path) -> list[str]:
    prepared = list(command)
    expected_flag = "-config" if core == "xray" else "-c"
    if len(prepared) >= 4 and prepared[-2] == expected_flag:
        prepared[-1] = str(config_path)
    return prepared


def _complete_bridge_plan(plan: dict[str, Any], command: list[str]) -> tuple[dict[str, Any], list[str]]:
    listen_host = str(plan.get("listen_host") or LOCAL_PROXY_LISTEN_HOST)
    if docker_mode():
        local_port = next_free_public_proxy_port(plan.get("preferred_port"), listen_host)
    else:
        local_port = (
            next_free_port(plan.get("preferred_port"), listen_host)
            if listen_host != LOCAL_PROXY_LISTEN_HOST
            else next_free_port(plan.get("preferred_port"))
        )
    if plan.get("require_exact_port") and local_port != plan.get("requested_port"):
        raise ValueError("原代理端口已被占用，取消自动切换")
    core_port = next_free_port(max(local_port + 1000, 23080))
    bridge_id = stable_id(str(plan["node_id"]), str(local_port), str(plan["core"]))
    config = generate_config(str(plan["core"]), dict(plan["node"]), core_port)
    config_path = RUNTIME_ROOT / f"{bridge_id}.json"
    log_path = RUNTIME_ROOT / f"{bridge_id}.log"
    _write_private_atomic(
        config_path,
        json.dumps(config, ensure_ascii=False, indent=2).encode("utf-8"),
        temp_path=config_path.with_suffix(config_path.suffix + ".tmp"),
    )
    completed = dict(plan)
    completed.update(
        {
            "local_port": local_port,
            "core_port": core_port,
            "bridge_id": bridge_id,
            "config": config,
            "config_path": config_path,
            "log_path": log_path,
        }
    )
    return completed, _retarget_core_command(command, str(plan["core"]), config_path)


def _run_prepared_bridge(plan: dict[str, Any], command: list[str]) -> dict[str, Any]:
    node_id = str(plan["node_id"])
    node = dict(plan["node"])
    core = str(plan["core"])
    local_port = safe_int(plan["local_port"], 0)
    core_port = safe_int(plan["core_port"], 0)
    listen_host = str(plan.get("listen_host") or LOCAL_PROXY_LISTEN_HOST)
    bridge_id = str(plan["bridge_id"])
    config_path = Path(plan["config_path"])
    log_path = Path(plan["log_path"])
    rotate_runtime_log(log_path)
    try:
        process = subprocess.Popen(
            command,
            cwd=str(APP_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
            **_popen_kwargs(),
        )
    except OSError as exc:
        raise CoreStartError(f"{core} 进程启动失败: {exc}") from exc
    log_stream = getattr(process, "stdout", None)
    log_pump = (
        threading.Thread(
            target=pump_runtime_log,
            args=(log_stream, log_path),
            kwargs={"bridge_id": bridge_id},
            name=f"core-log-{bridge_id[:12]}",
            daemon=True,
        )
        if log_stream is not None
        else None
    )
    with PROCESS_LOCK:
        PROCESS_TABLE[bridge_id] = process
        BRIDGE_NODE_TABLE[bridge_id] = node_id
        if log_pump is not None:
            RUNTIME_LOG_PUMP_TABLE[bridge_id] = log_pump
    try:
        if log_pump is not None:
            log_pump.start()
        core_started = wait_for_port(core_port, timeout=2.5)
        if process.poll() is not None:
            error = read_tail(log_path)
            raise CoreStartError(error or f"{core} 启动失败")
        if not core_started:
            raise CoreStartError("核心内部端口启动超时")
        proxy = Socks5LogProxy(
            bridge_id=bridge_id,
            node_id=node_id,
            node_name=str(node.get("name") or ""),
            listen_port=local_port,
            upstream_port=core_port,
            security=plan.get("lan_security"),
        )
        proxy.listen_host = listen_host
        proxy.start()
        with PROCESS_LOCK:
            LOG_PROXY_TABLE[bridge_id] = proxy
        started = wait_for_port(local_port, timeout=1.0)
        traffic = init_bridge_traffic(
            bridge_id,
            safe_int(plan.get("initial_upload"), 0),
            safe_int(plan.get("initial_download"), 0),
        )
        bridge = {
            "id": bridge_id,
            "node_id": node_id,
            "node_name": node.get("name"),
            "protocol": node.get("protocol"),
            "region": node.get("region"),
            "core": core,
            "port": local_port,
            "core_port": core_port,
            "listen_host": listen_host,
            "socks_url": f"socks5://127.0.0.1:{local_port}",
            "http_url": f"http://127.0.0.1:{local_port}",
            "supported_proxy_protocols": ["socks5", "http", "https-connect"],
            "status": "running" if started else "starting",
            "enabled": True,
            "pid": process.pid,
            "started_at": now_ts(),
            "pool_health": normalize_pool_health({}),
            "health": {"status": "unknown", "checked_at": 0, "error": ""},
            "config_path": str(config_path),
            "log_path": str(log_path),
            **traffic,
        }
        if plan.get("fallback_from"):
            bridge["fallback_from"] = str(plan["fallback_from"])
        with STATE_LOCK:
            state = load_state()
            current_node = find_node(state, node_id)
            current_active = active_bridge_for_node(state, node_id)
            if current_active and str(current_active.get("id") or "") != bridge_id:
                raise RuntimeError("该节点在启动期间已创建另一个本地代理")
            # Preserve any state changes made while runtime setup was in flight.
            bridge["node_name"] = current_node.get("name")
            bridge["protocol"] = current_node.get("protocol")
            bridge["region"] = current_node.get("region")
            state["bridges"] = [b for b in state["bridges"] if b.get("id") != bridge_id]
            state["bridges"].append(bridge)
            save_state(state)
        return bridge
    except BaseException:
        try:
            stop_process(bridge_id)
        except BaseException:
            pass
        raise


def start_bridge(payload: dict[str, Any]) -> dict[str, Any]:
    with BRIDGE_START_LOCK:
        while True:
            with SHUTDOWN_LOCK:
                if SHUTTING_DOWN.is_set():
                    raise RuntimeError("程序正在退出，不能启动新的本地代理")
                synchronize_process_state()
                plan = _prepare_bridge_start(payload)
                if plan.get("active_bridge"):
                    return dict(plan["active_bridge"])
            expected_attempts = list(plan["core_attempts"])
            restart_plan = False
            failures: list[tuple[str, BaseException]] = []
            for index, core in enumerate(expected_attempts):
                attempt_plan = dict(plan)
                attempt_plan["core"] = core
                try:
                    # Core discovery/download can take minutes. It intentionally runs outside
                    # SHUTDOWN_LOCK; no process is created until the lifecycle barrier is reacquired.
                    command = _prepare_core_command(attempt_plan)
                except (FileNotFoundError, OSError, ValueError) as error:
                    failures.append((core, error))
                    if index + 1 < len(expected_attempts):
                        log_event(f"{core} 预检失败，尝试备用核心: {error}", level="warn", category="核心")
                        continue
                    if len(failures) > 1:
                        details = "; ".join(f"{name}: {failure}" for name, failure in failures)
                        raise RuntimeError(f"所有兼容核心均启动失败: {details}") from error
                    raise

                with SHUTDOWN_LOCK:
                    if SHUTTING_DOWN.is_set():
                        raise RuntimeError("程序正在退出，不能启动新的本地代理")
                    synchronize_process_state()
                    with STATE_LOCK:
                        state = load_state()
                        node = dict(find_node(state, str(plan["node_id"])))
                        switch_from = str(payload.get("_health_switch_from") or "")
                        if switch_from and not health_switch_allowed(state, switch_from, str(plan["node_id"])):
                            raise ValueError("自动切换条件已变化，取消切换")
                        normalize_node_core_settings(node)
                        active_bridge = active_bridge_for_node(state, str(plan["node_id"]))
                        if active_bridge:
                            _validate_active_bridge_request(
                                active_bridge,
                                requested_core=str(plan["requested_core"]),
                                requested_port=plan.get("requested_port"),
                            )
                            return active_bridge
                        with PROCESS_LOCK:
                            residual = any(
                                owner_node_id == plan["node_id"]
                                for owner_node_id in BRIDGE_NODE_TABLE.values()
                            )
                        if residual:
                            raise RuntimeError("该节点仍有未清理的本地代理资源，无法重复启动")
                        desired_attempts = core_attempt_order(node, str(plan["requested_core"]))
                        attempt_plan["lan_security"] = normalize_lan_security(
                            (state.get("settings") or {}).get("lan_security")
                        )
                    if desired_attempts != expected_attempts:
                        restart_plan = True
                        break
                    attempt_plan["node"] = node
                    try:
                        completed_plan, runtime_command = _complete_bridge_plan(attempt_plan, command)
                    except ValueError as error:
                        failures.append((core, error))
                        if index + 1 < len(expected_attempts):
                            log_event(f"{core} 配置生成失败，尝试备用核心: {error}", level="warn", category="核心")
                            continue
                        if len(failures) > 1:
                            details = "; ".join(f"{name}: {failure}" for name, failure in failures)
                            raise RuntimeError(f"所有兼容核心均启动失败: {details}") from error
                        raise
                    if failures:
                        completed_plan["fallback_from"] = failures[0][0]
                    try:
                        bridge = _run_prepared_bridge(completed_plan, runtime_command)
                    except CoreStartError as error:
                        with PROCESS_LOCK:
                            residual = any(
                                owner_node_id == plan["node_id"]
                                for owner_node_id in BRIDGE_NODE_TABLE.values()
                            )
                        if residual:
                            raise RuntimeError(f"{error}；资源清理不完整，已停止备用核心回退") from error
                        failures.append((core, error))
                        if index + 1 < len(expected_attempts):
                            log_event(f"{core} 启动失败，尝试备用核心: {error}", level="warn", category="核心")
                            continue
                        if len(failures) > 1:
                            details = "; ".join(f"{name}: {failure}" for name, failure in failures)
                            raise RuntimeError(f"所有兼容核心均启动失败: {details}") from error
                        raise
                    if failures:
                        bridge.setdefault("fallback_from", failures[0][0])
                    return bridge
            if restart_plan:
                continue
            raise RuntimeError("没有可用的核心启动方案")


def validate_remote_browser_login(payload: dict[str, Any]) -> tuple[str, str]:
    username = str(payload.get("username") or "proxybridge").strip()
    if REMOTE_BROWSER_USERNAME_PATTERN.fullmatch(username) is None:
        raise ValueError("远程浏览器用户名必须为 1–32 位字母、数字、点、下划线或短横线")
    password = str(payload.get("password") or "")
    if not REMOTE_BROWSER_PASSWORD_MIN_LENGTH <= len(password) <= REMOTE_BROWSER_PASSWORD_MAX_LENGTH:
        raise ValueError(
            f"远程浏览器密码必须为 {REMOTE_BROWSER_PASSWORD_MIN_LENGTH}–{REMOTE_BROWSER_PASSWORD_MAX_LENGTH} 个字符"
        )
    if any(ord(char) < 32 or ord(char) == 127 for char in password):
        raise ValueError("远程浏览器密码不能包含控制字符")
    return username, password


def remote_browser_profile_relative(profile_path: Path) -> str:
    try:
        relative = profile_path.resolve().relative_to(WORK_ROOT.resolve())
    except ValueError as exc:
        raise RuntimeError("远程浏览器 Profile 不在工作目录内") from exc
    parts = relative.parts
    if len(parts) not in {2, 3, 4} or parts[0] != "browser_profiles":
        raise RuntimeError("远程浏览器 Profile 路径结构无效")
    if len(parts) == 4 and parts[-1] != "chromium-profile":
        raise RuntimeError("远程浏览器 Profile 兼容布局无效")
    return relative.as_posix()


def remote_browser_fingerprint_relative(kernel_version: str = "") -> str:
    executable = fingerprint_chromium_path(force_refresh=True, kernel_id=kernel_version or None)
    if not executable:
        if kernel_version:
            raise RuntimeError(f"该浏览器固定使用指纹内核 {kernel_version}，但数据目录中未安装该版本。")
        raise RuntimeError(
            "未找到 Linux 指纹内核。请先点击「下载指纹内核」，再启动远程浏览器。"
        )
    root = Path(executable).resolve().parent
    wrapper = root / "chrome-wrapper"
    if not wrapper.is_file() or not os.access(wrapper, os.X_OK):
        search_root = (
            fingerprint_kernel_versions_root() / kernel_version
            if kernel_version and kernel_version not in {"env", "legacy"}
            else fingerprint_chromium_root()
        )
        candidates = [path for path in search_root.glob("**/chrome-wrapper") if path.is_file()]
        wrapper = next((path for path in candidates if os.access(path, os.X_OK)), wrapper)
        root = wrapper.parent
    if not wrapper.is_file() or not os.access(wrapper, os.X_OK):
        raise RuntimeError("指纹内核缺少可执行的 chrome-wrapper，请重新下载内核")
    try:
        relative = root.resolve().relative_to(WORK_ROOT.resolve())
    except ValueError as exc:
        raise RuntimeError("指纹内核不在 Docker 数据目录内，无法挂载到 worker") from exc
    if not relative.parts or relative.parts[0] != "fingerprint-chromium":
        raise RuntimeError("指纹内核目录结构无效")
    return relative.as_posix()


def launch_remote_browser(payload: dict[str, Any]) -> dict[str, Any]:
    username, password = validate_remote_browser_login(payload)
    with SHUTDOWN_LOCK:
        if SHUTTING_DOWN.is_set():
            raise RuntimeError("程序正在退出，不能启动新的浏览器")
        synchronize_process_state()
    remote_sessions_snapshot, _remote_error = remote_browser_sessions()
    node_id = str(payload.get("node_id") or "")
    requested_instance_id = str(payload.get("instance_id") or "")
    with STATE_LOCK:
        state = load_state()
        instance: dict[str, Any] | None = None
        if requested_instance_id:
            instance = find_browser_instance(state, requested_instance_id)
            if not node_id:
                node_id = str(instance.get("node_id") or "")
        if not node_id:
            raise ValueError("缺少节点 ID")
        node = dict(find_node(state, node_id))
        sync_browser_instance_status(
            state,
            remote_sessions_snapshot=remote_sessions_snapshot,
        )
        created = False
        if instance is None:
            instance = create_browser_instance_record(state, node_id)
            state["browser_instances"].append(instance)
            created = True
        elif str(instance.get("node_id") or "") != node_id:
            raise ValueError("浏览器记录与节点不匹配")
        elif instance.get("status") in ACTIVE_BROWSER_STATUSES:
            raise ValueError("浏览器实例已在运行")
        instance.update(
            {
                "status": "starting",
                "pid": None,
                "remote_worker": True,
                "remote_url": "",
                "remote_container_id": "",
                "remote_host_port": None,
                "remote_username": username,
            }
        )
        instance.pop("error", None)
        instance_id = str(instance["id"])
        generation, operation_id = begin_remote_browser_start(instance_id)
        # 不信任状态文件中的绝对路径；始终由经过校验的节点/实例 ID 重建 Profile 路径。
        profile_root = browser_instance_profile_path(
            node_id,
            instance_id,
            legacy=bool(instance.get("legacy")),
        )
        chromium_profile = (
            profile_root / "chromium-profile"
            if instance.get("profile_layout") == "remote-worker-v1"
            else profile_root
        )
        profile_rel = remote_browser_profile_relative(chromium_profile)
        instance["profile_path"] = str(profile_root)
        instance_snapshot = dict(instance)
        save_state(state)
        bridge = active_bridge_for_node(state, node_id)
    if created:
        mark_sync_local_change()

    broker_start_attempted = False
    operation_registered = False
    try:
        if not bridge:
            bridge = start_bridge({"node_id": node_id, "core": "auto"})
        local_port = safe_int(bridge.get("port"), 0)
        if local_port <= 0:
            raise RuntimeError("当前节点没有可用的本地代理端口")
        chromium_profile.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            chromium_profile.chmod(0o700)
        except OSError:
            pass
        config = browser_fingerprint.normalize_fingerprint_config(
            instance_snapshot.get("fingerprint"), browser_fingerprint.legacy_fingerprint_config()
        )
        configure_browser_profile_defaults(chromium_profile)
        apply_browser_profile_preferences(chromium_profile, config)
        kernel_version = str(instance_snapshot.get("kernel_version") or "")
        fingerprint_rel = remote_browser_fingerprint_relative(kernel_version)
        kernel_id = kernel_version or _default_fingerprint_kernel_id(list_fingerprint_kernels())
        extension_ids = sync_profile_extensions(chromium_profile, list(instance_snapshot.get("extensions") or []))
        seed = safe_int(instance.get("fingerprint_seed"), browser_instance_seed(node_id, instance_id))
        region = node.get("region") or node_region(node.get("name") or "", node.get("server") or "")
        locale = browser_fingerprint.resolve_locale(
            config,
            region_locale(region),
            browser_launch_exit_locale(node_id, local_port, config),
        )
        timezone = locale["timezone"]
        # Linux worker 的 --lang 会同时设置 Intl 区域；「跟随系统」时交给 Broker 的默认 en-US。
        language = locale["language"]
        accept_language = (
            browser_fingerprint.chromium_accept_languages(locale["accept_language"], language) if language else ""
        )
        # worker 的 CHROME_CLI 按空白拆分参数；非 ASCII 等字符先做百分号编码，再交给 Broker 白名单复核。
        start_urls = [
            urllib.parse.quote(url, safe=":/?#[]@!$&()*+,;=%-._~")
            for url in instance_snapshot.get("start_urls") or []
        ]
        broker_client = remote_browser_broker_client()
        # 镜像下载可能耗时数分钟，但不会创建容器；先在生命周期操作之外完成，
        # 避免关机为等待一个无副作用的拉取而留下竞态。
        broker_client.prepare()
        if not register_remote_browser_operation(instance_id, generation, "start"):
            raise RuntimeError("远程浏览器启动已被停止或关机操作取消")
        operation_registered = True
        broker_start_attempted = True
        # worker 启动后会立即连接节点代理。先清掉会话缓存，避免刚创建的精确
        # worker IP 因命中旧快照而在最初几秒被代理认证误拒绝。
        invalidate_remote_browser_session_cache()
        broker_result = broker_client.start(
            {
                "instance_id": instance_id,
                "operation_id": operation_id,
                "profile_rel": profile_rel,
                "fingerprint_rel": fingerprint_rel,
                "username": username,
                "password": password,
                "proxy_port": local_port,
                "fingerprint_seed": seed,
                "timezone": timezone,
                "language": language,
                "accept_language": accept_language,
                # worker 运行 Linux；auto 平台在 Broker 中解析为 linux。
                "platform": config["platform"],
                "platform_version": config["platform_version"],
                "brand": config["brand"],
                "brand_version": config["brand_version"],
                "hardware_concurrency": config["hardware_concurrency"],
                "disable_spoofing": config["disable_spoofing"],
                "window_size": config["window_size"],
                "extension_ids": extension_ids,
                "start_urls": start_urls,
            }
        )
        invalidate_remote_browser_session_cache()
        remote_url = normalize_remote_browser_url(broker_result.get("url"))
        if not remote_url:
            raise RuntimeError("浏览器 worker 未返回有效的私有 HTTPS 访问地址")
        remote_container_id = normalize_remote_browser_container_id(
            broker_result.get("container_id")
        )
        remote_host_port = safe_int(broker_result.get("host_port"), 0)
        remote_url_port = urllib.parse.urlsplit(remote_url).port
        try:
            worker_ip = ipaddress.IPv4Address(str(broker_result.get("worker_ip") or ""))
        except ipaddress.AddressValueError:
            worker_ip = None
        if (
            not remote_container_id
            or not 1024 <= remote_host_port <= 65535
            or remote_host_port != remote_url_port
            or broker_result.get("status") != "running"
            or broker_result.get("health") != "healthy"
            or safe_int(broker_result.get("proxy_port"), 0) != local_port
            or worker_ip is None
            or not any(worker_ip in network for network in PRIVATE_IPV4_NETWORKS)
        ):
            raise RuntimeError("浏览器 worker 返回的容器状态无效")
        with STATE_LOCK:
            state = load_state()
            saved_instance = find_browser_instance(state, instance_id)
            if (
                SHUTTING_DOWN.is_set()
                or not remote_browser_generation_current(instance_id, generation)
                or saved_instance.get("status") != "starting"
            ):
                raise RuntimeError("远程浏览器启动结果已失效，正在回收 worker")
            started_at = now_ts()
            saved_instance.update(
                {
                    "status": "running",
                    "pid": None,
                    "bridge_id": bridge.get("id"),
                    "proxy": (
                        "socks5://"
                        f"{os.environ.get('PROXY_BRIDGE_BROWSER_PROXY_HOST', 'proxybridge')}:{local_port}"
                    ),
                    "remote_worker": True,
                    "remote_url": remote_url,
                    "remote_container_id": remote_container_id,
                    "remote_host_port": remote_host_port,
                    "remote_username": username,
                    "last_started_at": started_at,
                    "last_stopped_at": None,
                    "last_used_at": started_at,
                    "launch_count": safe_int(saved_instance.get("launch_count"), 0) + 1,
                    "runtime_kernel": kernel_id,
                    "runtime_locale": locale,
                }
            )
            saved_instance.pop("error", None)
            save_state(state)
            saved_instance_snapshot = dict(saved_instance)
        mark_sync_local_change()
    except BaseException as exc:
        if broker_start_attempted:
            try:
                remote_browser_broker_client().stop(instance_id, operation_id)
            except BaseException as cleanup_error:
                log_event(
                    f"回收启动失败的远程浏览器 {instance_id} 失败：{cleanup_error}",
                    level="warn",
                    category="浏览器",
                )
            finally:
                invalidate_remote_browser_session_cache()
        with STATE_LOCK:
            state = load_state()
            try:
                failed_instance = find_browser_instance(state, instance_id)
            except ValueError:
                failed_instance = None
            if (
                failed_instance is not None
                and remote_browser_generation_current(instance_id, generation)
                and failed_instance.get("status") == "starting"
            ):
                failed_instance.update(
                    {
                        "status": "failed",
                        "pid": None,
                        "remote_url": "",
                        "remote_container_id": "",
                        "remote_host_port": None,
                        "error": str(exc)[:1000],
                    }
                )
                save_state(state)
        raise
    finally:
        if operation_registered:
            finish_remote_browser_operation(instance_id, generation, "start")
        finish_remote_browser_start(instance_id, generation)
        prune_remote_browser_lifecycle(instance_id)
    return {
        "ok": True,
        "pid": None,
        "node_id": node_id,
        "node_name": node.get("name") or node_id,
        "instance_id": instance_id,
        "browser_name": instance_snapshot.get("name") or "浏览器",
        "proxy": saved_instance_snapshot["proxy"],
        "bridge_id": bridge.get("id"),
        "profile_path": str(profile_root),
        "fingerprint_seed": seed,
        "kernel_id": kernel_id,
        "region": region,
        "timezone": timezone,
        "language": language,
        "locale": locale,
        "remote": True,
        "remote_url": remote_url,
        "remote_username": username,
        "worker_image": remote_browser_broker_status().get("worker_image"),
        "isolation": "独立 Docker worker + 独立 Profile + 节点代理",
        "fingerprint_note": "已向 worker 传入固定指纹种子；底层指纹能力取决于所配置 worker 镜像中的 Chromium 构建。",
    }


def launch_browser(payload: dict[str, Any]) -> dict[str, Any]:
    require_browser_feature()
    with BROWSER_LAUNCH_LOCK:
        if remote_browser_feature_enabled():
            return launch_remote_browser(payload)
        return _launch_browser(payload)


def _launch_browser(payload: dict[str, Any]) -> dict[str, Any]:
    with SHUTDOWN_LOCK:
        if SHUTTING_DOWN.is_set():
            raise RuntimeError("程序正在退出，不能启动新的浏览器")
        synchronize_process_state()
    node_id = str(payload.get("node_id") or "")
    requested_instance_id = str(payload.get("instance_id") or "")
    extra_urls = browser_fingerprint.normalize_start_urls(payload.get("open_urls"), strict=True)

    with STATE_LOCK:
        state = load_state()
        instance: dict[str, Any] | None = None
        if requested_instance_id:
            instance = find_browser_instance(state, requested_instance_id)
            if not node_id:
                node_id = str(instance.get("node_id") or "")
        if not node_id:
            raise ValueError("缺少节点 ID")
        node = find_node(state, node_id)
        # 方案 B:使用打过补丁的指纹内核(fingerprint-chromium),从底层伪造 Canvas/WebGL/Audio/
        # 字体/屏幕/硬件并发/设备内存/Client Hints 等全部指纹维度;若内核缺失则明确提示去下载。
        # 先解析内核再创建记录，避免内核缺失时留下空记录。
        kernel_id, fp_chromium = resolve_browser_kernel(
            str((instance or {}).get("kernel_version") or "")
        )
        changed = sync_process_status(state)
        changed |= sync_browser_instance_status(state)
        created_browser_record = False
        if instance is None:
            instance = create_browser_instance_record(state, node_id)
            state["browser_instances"].append(instance)
            changed = True
            created_browser_record = True
        elif str(instance.get("node_id") or "") != node_id:
            raise ValueError("浏览器记录与节点不匹配")
        elif instance.get("status") in ACTIVE_BROWSER_STATUSES:
            if changed:
                save_state(state)
            raise ValueError("浏览器实例已在运行")
        if changed:
            save_state(state)
        bridge = active_bridge_for_node(state, node_id)

    if created_browser_record:
        mark_sync_local_change()

    if not bridge:
        bridge = start_bridge({"node_id": node_id, "core": "auto"})

    # 出口查询是网络请求，必须在关机屏障之外完成；失败时回退到节点地区表。
    exit_locale = browser_launch_exit_locale(
        node_id,
        safe_int(bridge.get("port"), 0),
        instance.get("fingerprint") if isinstance(instance.get("fingerprint"), dict) else {},
    )

    # start_bridge() has its own two-phase lifecycle barrier and may spend minutes
    # resolving a core. Never hold SHUTDOWN_LOCK while calling it. Reacquire the
    # barrier only for the final browser-process launch, then re-read all state that
    # may have changed while the bridge was being prepared.
    with SHUTDOWN_LOCK:
        if SHUTTING_DOWN.is_set():
            raise RuntimeError("程序正在退出，不能启动新的浏览器")
        synchronize_process_state()
        with STATE_LOCK:
            state = load_state()
            node = dict(find_node(state, node_id))
            instance = dict(find_browser_instance(state, str(instance["id"])))
            if instance.get("status") in ACTIVE_BROWSER_STATUSES:
                raise ValueError("浏览器实例已在运行")
            bridge = active_bridge_for_node(state, node_id)
            if not bridge:
                raise RuntimeError("本地代理在浏览器启动前已停止")
        return _run_browser_launch(
            node_id=node_id,
            node=node,
            instance=instance,
            bridge=dict(bridge),
            fp_chromium=fp_chromium,
            kernel_id=kernel_id,
            exit_locale=exit_locale,
            extra_urls=extra_urls,
        )


def _run_browser_launch(
    *,
    node_id: str,
    node: dict[str, Any],
    instance: dict[str, Any],
    bridge: dict[str, Any],
    fp_chromium: str,
    kernel_id: str = "",
    exit_locale: tuple[str, str, str] | None = None,
    extra_urls: list[str] | None = None,
) -> dict[str, Any]:
    """Launch and register Chromium while the caller holds SHUTDOWN_LOCK."""
    local_port = safe_int(bridge.get("port"), 0)
    if local_port <= 0:
        raise RuntimeError("当前节点没有可用的本地代理端口")

    instance_id = str(instance["id"])
    legacy = bool(instance.get("legacy"))
    profile_root = Path(str(instance.get("profile_path") or browser_instance_profile_path(node_id, instance_id, legacy)))
    profile_path = (
        profile_root / "chromium-profile"
        if instance.get("profile_layout") == "remote-worker-v1"
        else profile_root
    )
    first_profile_launch = not profile_path.exists()
    profile_path.mkdir(parents=True, exist_ok=True)
    config = browser_fingerprint.normalize_fingerprint_config(
        instance.get("fingerprint"), browser_fingerprint.legacy_fingerprint_config()
    )
    configure_browser_profile_defaults(profile_path)
    apply_browser_profile_preferences(profile_path, config)
    search_extension_path = ensure_google_search_extension()
    extension_ids = sync_profile_extensions(profile_path, list(instance.get("extensions") or []))
    extension_paths = [str(search_extension_path)] + [
        str(profile_path / PROFILE_EXTENSIONS_DIR / extension_id) for extension_id in extension_ids
    ]
    window_token = ""
    window_endpoint = browser_window_closed_endpoint() if browser_stop_on_last_window_enabled() else None
    if window_endpoint:
        window_token = secrets.token_urlsafe(32)
        extension_paths.append(str(prepare_browser_window_helper(instance_id, window_token, window_endpoint)))
    proxy = f"socks5://127.0.0.1:{local_port}"

    # 每条记录有固定指纹种子；时区/语言按配置取出口 IP、节点地区或手动值，使指纹与代理出口一致。
    seed = safe_int(instance.get("fingerprint_seed"), browser_instance_seed(node_id, instance_id, legacy))
    region = node.get("region") or node_region(node.get("name") or "", node.get("server") or "")
    locale = browser_fingerprint.resolve_locale(config, region_locale(region), exit_locale)
    timezone, language = locale["timezone"], locale["language"]
    cdp_enabled = instance.get("cdp_enabled") is True
    if cdp_enabled:
        # 端口 0 让 Chromium 选择随机空闲端口，并把实际端口写入 Profile 的 DevToolsActivePort。
        (profile_path / browser_fingerprint.DEVTOOLS_ACTIVE_PORT_FILE).unlink(missing_ok=True)
    start_urls = list(extra_urls or []) or list(instance.get("start_urls") or [])
    if not start_urls:
        start_urls = [DEFAULT_BROWSER_START_URL if first_profile_launch else "about:blank"]

    command = [
        fp_chromium,
        f"--user-data-dir={profile_path}",
        *browser_fingerprint.fingerprint_switches(
            config,
            seed=seed,
            host_platform=fingerprint_platform_token(),
            locale=locale,
        ),
        # 使用代理时强制 WebRTC 只走代理,禁止非代理 UDP,防止通过 STUN 泄漏真实 IP。
        # 用标准 Chromium 开关 --webrtc-ip-handling-policy + 枚举值(经内核二进制验证存在),
        # 而非独立布尔开关 --disable-non-proxied-udp(该 build 中不存在,会被静默忽略)。
        "--webrtc-ip-handling-policy=disable_non_proxied_udp",
        "--no-first-run",
        "--no-default-browser-check",
        f"--load-extension={','.join(extension_paths)}",
        f"--proxy-server={proxy}",
        *(["--remote-debugging-port=0", "--remote-debugging-address=127.0.0.1"] if cdp_enabled else []),
        "--new-window",
        *start_urls,
    ]
    process = subprocess.Popen(
        command,
        cwd=str(APP_ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **_popen_kwargs(),
    )
    try:
        # 记录浏览器进程,便于后续「停止浏览器」结束整个进程组;同时清理已退出的旧句柄。
        with PROCESS_LOCK:
            procs = [p for p in BROWSER_TABLE.get(node_id, []) if p.poll() is None]
            procs.append(process)
            BROWSER_TABLE[node_id] = procs
            BROWSER_PROCESS_TABLE[instance_id] = process
            BROWSER_LAUNCH_COMMANDS[instance_id] = [arg for arg in command if arg not in start_urls]
            if window_token:
                register_browser_window_token(window_token, instance_id, process)
        with STATE_LOCK:
            state = load_state()
            try:
                saved_instance = find_browser_instance(state, instance_id)
            except ValueError:
                saved_instance = dict(instance)
                state["browser_instances"].append(saved_instance)
            started_at = now_ts()
            saved_instance.update(
                {
                    "status": "running",
                    "profile_path": str(profile_root),
                    "fingerprint_seed": seed,
                    "pid": process.pid,
                    "bridge_id": bridge.get("id"),
                    "proxy": proxy,
                    "remote_worker": False,
                    "remote_url": "",
                    "remote_container_id": "",
                    "remote_host_port": None,
                    "remote_username": "",
                    "last_started_at": started_at,
                    "last_stopped_at": None,
                    "last_used_at": started_at,
                    "launch_count": safe_int(saved_instance.get("launch_count"), 0) + 1,
                    "runtime_kernel": kernel_id,
                    "runtime_locale": locale,
                    "cdp_port": None,
                    "cdp_ws_path": "",
                }
            )
            save_state(state)
    except BaseException:
        terminated = False
        try:
            terminate_process(process)
        except BaseException:
            pass
        else:
            terminated = True
        if terminated:
            try:
                with PROCESS_LOCK:
                    if BROWSER_PROCESS_TABLE.get(instance_id) is process:
                        BROWSER_PROCESS_TABLE.pop(instance_id, None)
                    remove_browser_process_from_node_tables(process)
            except BaseException:
                pass
        raise
    return {
        "ok": True,
        "pid": process.pid,
        "node_id": node_id,
        "node_name": node.get("name") or node_id,
        "instance_id": instance_id,
        "browser_name": instance.get("name") or "浏览器",
        "proxy": proxy,
        "bridge_id": bridge.get("id"),
        "profile_path": str(profile_root),
        "chromium_path": fp_chromium,
        "kernel_id": kernel_id,
        "fingerprint_seed": seed,
        "region": region,
        "timezone": timezone,
        "language": language,
        "locale": locale,
        "cdp_enabled": cdp_enabled,
        "isolation": "fingerprint-chromium 底层指纹伪造 + 独立 Profile + 节点本地代理",
        "fingerprint_note": (
            "已按记录种子派生独立指纹(Canvas/WebGL/Audio/字体/屏幕/硬件并发/设备内存/Client Hints),"
            "时区与语言按记录配置匹配,并已禁止 WebRTC 非代理 UDP。"
        ),
    }


def lookup_browser_exit(port: int) -> dict[str, str]:
    # 只经对应本地 HTTP CONNECT 代理访问出口服务；连接失败不回退直连。
    connection = http.client.HTTPSConnection("127.0.0.1", port, timeout=10,
                                             context=ssl.create_default_context())
    connection.set_tunnel("ipwho.is", 443)
    try:
        connection.request("GET", "/", headers={"Accept": "application/json", "User-Agent": "ProxyBridge"})
        response = connection.getresponse()
        data = response.read(65537)
        if response.status != 200 or len(data) > 65536:
            raise RuntimeError(f"出口服务响应异常：HTTP {response.status}")
        result = json.loads(data)
        ip = str(ipaddress.ip_address(str(result.get("ip") or "")))
        code = str(result.get("country_code") or "").upper()
        if result.get("success") is False or not re.fullmatch(r"[A-Z]{2}", code):
            raise RuntimeError("出口服务未提供有效地区")
        info = {"ip": ip, "region_code": code}
        timezone_info = result.get("timezone")
        timezone_id = str(timezone_info.get("id") or "") if isinstance(timezone_info, dict) else ""
        if browser_fingerprint.TIMEZONE_PATTERN.fullmatch(timezone_id):
            info["timezone"] = timezone_id
        return info
    finally:
        connection.close()


def exit_locale_from_lookup(info: dict[str, Any]) -> tuple[str, str, str] | None:
    """把出口查询结果转换为 (时区, 语言, Accept-Language)；国家不在地区目录时只提供时区。"""
    code = str(info.get("region_code") or "").upper()
    timezone = str(info.get("timezone") or "")
    catalog = REGION_CATALOG.get(code)
    language = accept_language = ""
    if catalog:
        region_timezone, language, accept_language = region_locale(catalog[0])
        timezone = timezone or region_timezone
    if not timezone and not language:
        return None
    return timezone, language, accept_language


def cache_exit_lookup(node_id: str, info: dict[str, Any]) -> None:
    with EXIT_LOCALE_CACHE_LOCK:
        EXIT_LOCALE_CACHE[node_id] = {"info": dict(info), "checked_at": time.monotonic()}


def cached_exit_lookup(node_id: str, max_age: float = EXIT_LOCALE_CACHE_TTL) -> dict[str, Any] | None:
    with EXIT_LOCALE_CACHE_LOCK:
        entry = EXIT_LOCALE_CACHE.get(node_id)
        if not entry or time.monotonic() - float(entry.get("checked_at") or 0.0) > max_age:
            return None
        return dict(entry["info"])


def browser_launch_exit_locale(node_id: str, port: int, config: dict[str, Any]) -> tuple[str, str, str] | None:
    """启动前按需查询节点出口；查询失败返回 None，由调用方回退到节点地区表。"""
    if "exit_ip" not in {config.get("timezone_mode"), config.get("language_mode")} or port <= 0:
        return None
    info = cached_exit_lookup(node_id)
    if info is None:
        try:
            info = lookup_browser_exit(port)
        except Exception as exc:
            log_event(f"启动前出口查询失败，时区/语言改用节点地区：{exc}", level="warn", category="浏览器")
            return None
        cache_exit_lookup(node_id, info)
    return exit_locale_from_lookup(info)


def check_browser_exit(instance_id: str, generation: tuple[Any, ...]) -> None:
    if SHUTTING_DOWN.is_set():
        return
    with STATE_LOCK:
        state = load_state()
        instance = next((item for item in state["browser_instances"] if item.get("id") == instance_id), None)
        if not instance or instance.get("status") not in ACTIVE_BROWSER_STATUSES:
            return
        bridge = next((item for item in state["bridges"] if item.get("id") == instance.get("bridge_id")), None)
        if not bridge or bridge.get("status") != "running":
            return
        node = find_node(state, str(instance.get("node_id") or ""))
        expected = node_region_code(node.get("region"))
        port = safe_int(bridge.get("port"), 0)
    try:
        node_id = str(instance.get("node_id") or "")
        # 启动前刚查过出口时直接复用，避免同一次启动访问两次出口服务。
        lookup = cached_exit_lookup(node_id, max_age=30.0) or lookup_browser_exit(port)
        cache_exit_lookup(node_id, lookup)
        result: dict[str, Any] = {**lookup, "checked_at": now_ts(), "status": "ok"}
        result["expected_region_code"] = expected
        result["mismatch"] = expected != "ZZ" and result["region_code"] != expected
    except Exception as exc:
        result = {"status": "error", "checked_at": now_ts(), "error": str(exc)[:500]}
    with STATE_LOCK:
        state = load_state()
        instance = next((item for item in state["browser_instances"] if item.get("id") == instance_id), None)
        if not instance or tuple(instance.get(key) for key in ("last_started_at", "launch_count", "bridge_id")) != generation:
            return
        if instance.get("status") not in ACTIVE_BROWSER_STATUSES:
            return
        instance["exit_check"] = result
        save_state(state)
    if result.get("mismatch"):
        log_event(f"浏览器出口地区 {result['region_code']} 与节点地区 {expected} 不一致", level="warn", category="浏览器")


def browser_cdp_endpoint(instance: dict[str, Any]) -> tuple[int, str] | None:
    """返回运行中本机浏览器的 CDP (端口, 浏览器 ws 路径)；未开启或尚未就绪时返回 None。"""
    if instance.get("remote_worker") is True or instance.get("cdp_enabled") is not True:
        return None
    port = safe_int(instance.get("cdp_port"), 0)
    ws_path = str(instance.get("cdp_ws_path") or "")
    if 1024 <= port <= 65535 and ws_path:
        return port, ws_path
    node_id = str(instance.get("node_id") or "")
    instance_id = str(instance.get("id") or "")
    if not node_id or not instance_id:
        return None
    profile_root = browser_instance_profile_path(node_id, instance_id, legacy=bool(instance.get("legacy")))
    profile_path = (
        profile_root / "chromium-profile" if instance.get("profile_layout") == "remote-worker-v1" else profile_root
    )
    return browser_fingerprint.read_devtools_active_port(profile_path)


def discover_browser_cdp_endpoint(instance_id: str, generation: tuple[Any, ...]) -> None:
    # Chromium 启动后才写 DevToolsActivePort；最多等待 20 秒，写入状态供界面显示与 Cookie 操作。
    deadline = time.monotonic() + 20.0
    while not SHUTTING_DOWN.is_set() and time.monotonic() < deadline:
        with STATE_LOCK:
            state = load_state()
            instance = next((item for item in state["browser_instances"] if item.get("id") == instance_id), None)
            if (
                not instance
                or instance.get("status") not in ACTIVE_BROWSER_STATUSES
                or tuple(instance.get(key) for key in ("last_started_at", "launch_count", "bridge_id")) != generation
            ):
                return
            endpoint = browser_cdp_endpoint({**instance, "cdp_port": None, "cdp_ws_path": ""})
            if endpoint:
                instance["cdp_port"], instance["cdp_ws_path"] = endpoint
                save_state(state)
                return
        time.sleep(0.5)


def _live_local_browser_instance(instance_id: str) -> dict[str, Any]:
    require_browser_feature()
    if remote_browser_feature_enabled():
        raise ValueError("Docker 远程浏览器暂不支持该操作，请在远程浏览器页面中手动处理")
    with STATE_LOCK:
        state = load_state()
        if sync_browser_instance_status(state):
            save_state(state)
        instance = dict(find_browser_instance(state, instance_id))
    if not _browser_instance_live(instance):
        raise ValueError("浏览器未运行，请先启动")
    return instance


def _require_browser_cdp(instance_id: str) -> tuple[dict[str, Any], tuple[int, str]]:
    instance = _live_local_browser_instance(instance_id)
    if instance.get("cdp_enabled") is not True:
        raise ValueError("请先在浏览器配置中开启 CDP 调试端口，并重新启动浏览器")
    endpoint = browser_cdp_endpoint(instance)
    if not endpoint:
        raise RuntimeError("调试端口尚未就绪；如果是开启 CDP 前启动的浏览器，请重启后再试")
    return instance, endpoint


def _safe_download_name(value: Any, fallback: str) -> str:
    text = re.sub(r"[^\w.-]+", "-", str(value or ""), flags=re.UNICODE).strip("-.")
    return (text or fallback)[:60]


def export_browser_cookies(payload: dict[str, Any]) -> dict[str, Any]:
    instance_id = str(payload.get("instance_id") or "")
    fmt = str(payload.get("format") or "json").lower()
    if fmt not in {"json", "netscape"}:
        raise ValueError("导出格式只能是 json 或 netscape")
    instance, (port, ws_path) = _require_browser_cdp(instance_id)
    try:
        result = browser_fingerprint.cdp_call(port, ws_path, "Storage.getCookies")
    except browser_fingerprint.CdpError as exc:
        raise RuntimeError(f"读取 Cookie 失败：{exc}") from exc
    cookies = [item for item in result.get("cookies") or [] if isinstance(item, dict)]
    content = (
        browser_fingerprint.format_cookies_json(cookies)
        if fmt == "json"
        else browser_fingerprint.format_cookies_netscape(cookies)
    )
    base_name = _safe_download_name(instance.get("label") or instance.get("name"), "browser")
    return {
        "ok": True,
        "count": len(cookies),
        "format": fmt,
        "content": content,
        "filename": f"{base_name}-cookies.{'json' if fmt == 'json' else 'txt'}",
    }


def import_browser_cookies(payload: dict[str, Any]) -> dict[str, Any]:
    instance_id = str(payload.get("instance_id") or "")
    params = browser_fingerprint.parse_cookie_text(str(payload.get("content") or ""), str(payload.get("format") or "auto"))
    _instance, (port, ws_path) = _require_browser_cdp(instance_id)
    try:
        with browser_fingerprint.CdpSession(port, ws_path, timeout=20.0) as session:
            imported, failures = browser_fingerprint.set_cookies(session, params)
    except browser_fingerprint.CdpError as exc:
        raise RuntimeError(f"写入 Cookie 失败：{exc}") from exc
    return {"ok": True, "total": len(params), "imported": imported, "failed": len(failures), "errors": failures[:20]}


def open_browser_url(payload: dict[str, Any]) -> dict[str, Any]:
    """在运行中的本机浏览器打开网址（指纹检测等）；浏览器未运行时带该网址启动。"""
    require_browser_feature()
    instance_id = str(payload.get("instance_id") or "")
    site = str(payload.get("site") or "")
    url = FINGERPRINT_CHECK_SITES.get(site) or browser_fingerprint.validate_start_url(payload.get("url"))
    with STATE_LOCK:
        state = load_state()
        instance = dict(find_browser_instance(state, instance_id))
    if remote_browser_feature_enabled():
        raise ValueError(f"请在远程浏览器中手动打开：{url}")
    if not _browser_instance_live(instance):
        result = launch_browser({"instance_id": instance_id, "open_urls": [url]})
        return {**result, "opened": url, "method": "launch"}
    endpoint = browser_cdp_endpoint(instance)
    if endpoint:
        try:
            browser_fingerprint.cdp_call(*endpoint, "Target.createTarget", {"url": url})
            return {"ok": True, "opened": url, "method": "cdp", "instance_id": instance_id}
        except browser_fingerprint.CdpError as exc:
            log_event(f"CDP 打开网址失败，改用单实例转发：{exc}", level="warn", category="浏览器")
    with PROCESS_LOCK:
        command = list(BROWSER_LAUNCH_COMMANDS.get(instance_id) or [])
    if not command:
        raise RuntimeError("找不到该浏览器的启动参数，请重启浏览器后再试")
    # Chromium 检测到同一 user-data-dir 已在运行时会把网址转交给原进程，新进程随即退出。
    process = subprocess.Popen(
        [*command, url],
        cwd=str(APP_ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **_popen_kwargs(),
    )
    threading.Thread(target=process.wait, name="browser-open-url", daemon=True).start()
    return {"ok": True, "opened": url, "method": "singleton", "instance_id": instance_id}


def schedule_browser_exit_check(result: dict[str, Any]) -> None:
    instance_id = str(result.get("instance_id") or "")
    if not instance_id or SHUTTING_DOWN.is_set():
        return
    with STATE_LOCK:
        state = load_state()
        instance = next((item for item in state["browser_instances"] if item.get("id") == instance_id), None)
        if not instance or instance.get("status") not in ACTIVE_BROWSER_STATUSES:
            return
        generation = tuple(instance.get(key) for key in ("last_started_at", "launch_count", "bridge_id"))
        instance["exit_check"] = {"status": "checking"}
        save_state(state)
        discover_cdp = instance.get("cdp_enabled") is True and instance.get("remote_worker") is not True
    BROWSER_EXIT_EXECUTOR.submit(check_browser_exit, instance_id, generation)
    if discover_cdp:
        BROWSER_EXIT_EXECUTOR.submit(discover_browser_cdp_endpoint, instance_id, generation)


def prune_browser_processes(node_id: str) -> list[subprocess.Popen]:
    # 清理指定节点已退出的浏览器进程句柄,返回仍存活的进程列表。
    with PROCESS_LOCK:
        procs = [p for p in BROWSER_TABLE.get(node_id, []) if p.poll() is None]
        if procs:
            BROWSER_TABLE[node_id] = procs
        else:
            BROWSER_TABLE.pop(node_id, None)
        return list(procs)


def active_browser_node_ids() -> list[str]:
    remote_sessions_snapshot: Any = REMOTE_BROWSER_SESSIONS_UNSET
    if remote_browser_feature_enabled():
        remote_sessions_snapshot, _remote_error = remote_browser_sessions()
    with SHUTDOWN_LOCK:
        return _active_browser_node_ids(
            remote_sessions_snapshot=remote_sessions_snapshot,
        )


def _active_browser_node_ids(
    *,
    remote_sessions_snapshot: Any = REMOTE_BROWSER_SESSIONS_UNSET,
) -> list[str]:
    with STATE_LOCK:
        state = load_state()
        if sync_browser_instance_status(
            state,
            remote_sessions_snapshot=remote_sessions_snapshot,
        ):
            save_state(state)
    return active_browser_node_ids_from_state(state)


def active_browser_node_ids_from_state(state: dict[str, Any]) -> list[str]:
    node_ids: list[str] = []
    for instance in state.get("browser_instances", []):
        node_id = str(instance.get("node_id") or "")
        if node_id and instance.get("status") in ACTIVE_BROWSER_STATUSES:
            node_ids.append(node_id)
    with PROCESS_LOCK:
        fallback_node_ids = list(BROWSER_TABLE.keys())
    for node_id in fallback_node_ids:
        if prune_browser_processes(node_id):
            node_ids.append(node_id)
    return list(dict.fromkeys(node_ids))


def remove_browser_process_from_node_tables(process: subprocess.Popen) -> None:
    for node_id, procs in list(BROWSER_TABLE.items()):
        remaining = [item for item in procs if item is not process]
        if remaining:
            BROWSER_TABLE[node_id] = remaining
        else:
            BROWSER_TABLE.pop(node_id, None)


def remove_registered_browser_process(
    process: subprocess.Popen,
    instance_id: str | None = None,
) -> None:
    with PROCESS_LOCK:
        if instance_id and BROWSER_PROCESS_TABLE.get(instance_id) is process:
            BROWSER_PROCESS_TABLE.pop(instance_id, None)
        remove_browser_process_from_node_tables(process)


def stop_browser_instance_process(instance_id: str) -> int:
    if remote_browser_feature_enabled():
        operation_id = remote_browser_pending_start_tokens([instance_id]).get(instance_id, "")
        generation = invalidate_remote_browser_generations([instance_id]).get(instance_id, 0)
        try:
            return stop_remote_browser_worker(instance_id, generation, operation_id)
        finally:
            prune_remote_browser_lifecycle(instance_id)
    with PROCESS_LOCK:
        process = BROWSER_PROCESS_TABLE.get(instance_id)
    if process is None:
        return 0
    killed = 0
    if process.poll() is None:
        # 浏览器以 start_new_session 启动,终结整个进程组以带走全部子进程。
        terminate_process(process)
        killed = 1
    remove_registered_browser_process(process, instance_id)
    return killed


def stop_remote_browser_worker(
    instance_id: str,
    generation: int,
    cancelled_operation_id: str = "",
) -> int:
    if not register_remote_browser_operation(instance_id, generation, "stop"):
        raise RuntimeError("远程浏览器停止操作已被新的操作或关机取消")
    try:
        result = remote_browser_broker_client().stop(instance_id, cancelled_operation_id)
        return safe_int(result.get("stopped"), 0)
    finally:
        invalidate_remote_browser_session_cache()
        finish_remote_browser_operation(instance_id, generation, "stop")


def _stop_remote_browser(
    payload: dict[str, Any],
    *,
    auto_upload: bool = True,
) -> dict[str, Any]:
    node_id = str(payload.get("node_id") or "")
    instance_id = str(payload.get("instance_id") or "")
    all_instances = payload.get("all_instances") is True
    with STATE_LOCK:
        state = load_state()
        if all_instances:
            target_ids = [
                str(item.get("id") or "")
                for item in state.get("browser_instances", [])
                if isinstance(item, dict) and item.get("id")
            ]
        elif instance_id:
            instance = find_browser_instance(state, instance_id)
            node_id = str(instance.get("node_id") or "")
            target_ids = [instance_id]
        else:
            if not node_id:
                raise ValueError("缺少节点 ID")
            target_ids = [
                str(item.get("id") or "")
                for item in browser_instances_for_node(state, node_id)
                if item.get("id")
            ]
        cancelled_operations = remote_browser_pending_start_tokens(target_ids)
        generations = invalidate_remote_browser_generations(target_ids)
        for target_id in target_ids:
            try:
                target = find_browser_instance(state, target_id)
            except ValueError:
                continue
            target["status"] = "stopping"
            target["pid"] = None
            target.pop("error", None)
        save_state(state)

    stopped: list[str] = []
    killed = 0
    failures: list[tuple[str, BaseException]] = []
    for target_id in target_ids:
        try:
            killed += stop_remote_browser_worker(
                target_id,
                generations[target_id],
                cancelled_operations.get(target_id, ""),
            )
            stopped.append(target_id)
        except BaseException as error:
            failures.append((target_id, error))

    with STATE_LOCK:
        state = load_state()
        stopped_at = now_ts()
        for target_id in target_ids:
            generation = generations[target_id]
            if not remote_browser_generation_current(target_id, generation):
                continue
            try:
                target = find_browser_instance(state, target_id)
            except ValueError:
                continue
            failure = next((error for failed_id, error in failures if failed_id == target_id), None)
            if failure is not None:
                target["status"] = "stopping"
                target["error"] = f"停止远程浏览器失败：{failure}"[:1000]
                continue
            target.update(
                {
                    "status": "stopped",
                    "pid": None,
                    "remote_url": "",
                    "remote_container_id": "",
                    "remote_host_port": None,
                    "last_stopped_at": stopped_at,
                    "last_used_at": stopped_at,
                }
            )
            target.pop("error", None)
        save_state(state)

    for target_id in target_ids:
        prune_remote_browser_lifecycle(target_id)

    if auto_upload:
        maybe_auto_upload_browser_instances(stopped)
    if failures:
        details = "; ".join(f"{target_id}: {error}" for target_id, error in failures)
        raise RuntimeError(f"停止部分远程浏览器失败（{len(failures)} 项）：{details}") from failures[0][1]
    return {
        "ok": True,
        "node_id": node_id,
        "instance_id": instance_id or None,
        "killed_browsers": killed,
        "stopped_bridges": [],
    }


def stop_untracked_browser_processes_for_node(node_id: str) -> int:
    with PROCESS_LOCK:
        procs = list(BROWSER_TABLE.get(node_id, []))
    killed = 0
    failures: list[BaseException] = []
    for process in procs:
        try:
            if process.poll() is None:
                terminate_process(process)
                killed += 1
        except BaseException as error:
            failures.append(error)
            continue
        remove_registered_browser_process(process)
    if failures:
        details = "; ".join(str(error) for error in failures)
        raise RuntimeError(f"停止部分浏览器失败（{len(failures)} 项）: {details}") from failures[0]
    return killed


def stop_browser_instances(state: dict[str, Any], instance_ids: list[str]) -> int:
    if remote_browser_feature_enabled():
        raise RuntimeError("远程浏览器必须使用锁外两阶段停止流程")
    killed = 0
    stopped_at = now_ts()
    for instance_id in dict.fromkeys(instance_ids):
        try:
            instance = find_browser_instance(state, instance_id)
        except ValueError:
            continue
        killed += stop_browser_instance_process(instance_id)
        if instance.get("status") != "stopped" or instance.get("pid") or instance.get("remote_url"):
            instance.update(
                {
                    "status": "stopped",
                    "pid": None,
                    "remote_url": "",
                    "remote_container_id": "",
                    "remote_host_port": None,
                    "last_stopped_at": stopped_at,
                    "last_used_at": stopped_at,
                }
            )
    return killed


def stop_browser_processes_for_node(node_id: str, state: dict[str, Any] | None = None) -> int:
    if remote_browser_feature_enabled():
        if state is not None:
            raise RuntimeError("远程浏览器不能在 STATE_LOCK 内停止")
        return safe_int(_stop_remote_browser({"node_id": node_id}).get("killed_browsers"), 0)
    if state is None:
        with STATE_LOCK:
            state = load_state()
            killed = stop_browser_processes_for_node(node_id, state)
            save_state(state)
            return killed
    instance_ids = [
        str(instance.get("id") or "")
        for instance in state.get("browser_instances", [])
        if str(instance.get("node_id") or "") == node_id
    ]
    killed = stop_browser_instances(state, instance_ids)
    killed += stop_untracked_browser_processes_for_node(node_id)
    return killed


def stop_browser(payload: dict[str, Any]) -> dict[str, Any]:
    require_browser_feature()
    if remote_browser_feature_enabled():
        with SHUTDOWN_LOCK:
            if SHUTTING_DOWN.is_set():
                raise RuntimeError("程序正在退出，不能停止浏览器")
        return _stop_remote_browser(payload)
    with SHUTDOWN_LOCK:
        return _stop_browser(payload)


def _stop_browser(
    payload: dict[str, Any],
    *,
    auto_upload: bool = True,
) -> dict[str, Any]:
    # 停止某节点的指纹浏览器:只结束浏览器进程组,保留该节点已启用的本地代理端口。
    node_id = str(payload.get("node_id") or "")
    instance_id = str(payload.get("instance_id") or "")
    stopped_instance_ids: list[str] = []
    with STATE_LOCK:
        state = load_state()
        if instance_id:
            instance = find_browser_instance(state, instance_id)
            node_id = str(instance.get("node_id") or "")
            killed = stop_browser_instances(state, [instance_id])
            stopped_instance_ids = [instance_id]
        else:
            if not node_id:
                raise ValueError("缺少节点 ID")
            stopped_instance_ids = [
                str(item.get("id") or "")
                for item in browser_instances_for_node(state, node_id)
                if item.get("id")
            ]
            killed = stop_browser_processes_for_node(node_id, state)
        save_state(state)
    # v1.1: optional auto-upload after browser stop.
    if auto_upload:
        maybe_auto_upload_browser_instances(stopped_instance_ids)
    return {
        "ok": True,
        "node_id": node_id,
        "instance_id": instance_id or None,
        "killed_browsers": killed,
        "stopped_bridges": [],
    }


def remote_browser_credentials(payload: dict[str, Any]) -> dict[str, Any]:
    """按需读取运行中远程 Worker 的访问凭证，绝不写入持久状态。"""
    if not remote_browser_feature_enabled():
        raise RuntimeError("只有 Docker 远程浏览器模式可以查看访问凭证")
    instance_id = str(payload.get("instance_id") or payload.get("id") or "")
    if not instance_id:
        raise ValueError("缺少浏览器实例 ID")
    with STATE_LOCK:
        state = load_state()
        instance = find_browser_instance(state, instance_id)
        if instance.get("remote_worker") is not True or instance.get("status") != "running":
            raise ValueError("仅运行中的远程浏览器可以查看访问凭证")
        instance_snapshot = dict(instance)

    # Broker 网络访问必须位于 STATE_LOCK / SHUTDOWN_LOCK 外，避免 Docker Engine
    # 暂时无响应时阻塞状态接口、停止流程或程序退出。
    result = remote_browser_broker_client().credentials(instance_id)
    if str(result.get("instance_id") or "") != instance_id or not result.get("username"):
        raise RuntimeError("浏览器 Broker 返回的凭证实例无效")
    remote_url = normalize_remote_browser_url(result.get("url"))
    if not remote_url:
        raise RuntimeError("浏览器 Broker 返回的远程访问地址无效")
    username, password = validate_remote_browser_login(
        {
            "username": result.get("username"),
            "password": result.get("password"),
        }
    )
    return {
        "ok": True,
        "instance_id": instance_id,
        "browser_name": str(instance_snapshot.get("name") or "远程浏览器"),
        "remote_url": remote_url,
        "username": username,
        "password": password,
    }


def _validated_browser_profile_target(instance: dict[str, Any]) -> Path:
    """只由状态中的逻辑 ID 重建删除目标，拒绝绝对路径和符号链接逃逸。"""
    node_id = str(instance.get("node_id") or "")
    instance_id = str(instance.get("id") or "")
    if not node_id:
        raise ValueError("浏览器记录缺少节点 ID")
    if (
        BROWSER_PROFILE_PART_PATTERN.fullmatch(instance_id) is None
        or instance_id in {".", ".."}
    ):
        raise ValueError("浏览器实例 ID 无效，已拒绝删除 Profile")
    legacy = bool(instance.get("legacy"))
    node_root = browser_node_profile_root(node_id)
    candidate = browser_instance_profile_path(node_id, instance_id, legacy=legacy)
    if node_root.is_symlink() or candidate.is_symlink():
        raise RuntimeError("浏览器 Profile 路径包含符号链接，已拒绝删除")
    profiles_root = browser_profiles_root().resolve(strict=False)
    resolved = candidate.resolve(strict=False)
    if resolved == profiles_root or profiles_root not in resolved.parents:
        raise RuntimeError("浏览器 Profile 路径越出数据目录，已拒绝删除")
    return resolved


def _retry_readonly_profile_removal(function: Any, path: str, error_info: Any) -> None:
    """Windows Profile 文件可能带只读位；清除后只重试当前失败项。"""
    try:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
        function(path)
    except BaseException:
        raise error_info[1]


def _remove_profile_entry(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        try:
            path.unlink()
        except PermissionError:
            path.chmod(stat.S_IWRITE | stat.S_IREAD)
            path.unlink()
        return
    shutil.rmtree(path, onerror=_retry_readonly_profile_removal)


def _delete_browser_profile_data(
    instance: dict[str, Any],
    all_instances: list[dict[str, Any]],
) -> bool:
    """物理删除单个实例的数据；legacy 布局保留同节点的独立实例目录。"""
    target = _validated_browser_profile_target(instance)
    if not target.exists():
        return False
    node_id = str(instance.get("node_id") or "")
    instance_id = str(instance.get("id") or "")
    if not instance.get("legacy"):
        _remove_profile_entry(target)
        node_root = target.parent
        if node_root.is_dir() and not any(node_root.iterdir()):
            _remove_profile_entry(node_root)
        return True

    preserve_names: set[str] = set()
    for other in all_instances:
        if (
            not isinstance(other, dict)
            or str(other.get("id") or "") == instance_id
            or str(other.get("node_id") or "") != node_id
            or other.get("legacy")
        ):
            continue
        other_id = str(other.get("id") or "")
        if (
            BROWSER_PROFILE_PART_PATTERN.fullmatch(other_id) is None
            or other_id in {".", ".."}
        ):
            raise ValueError("同节点浏览器实例 ID 无效，无法安全删除默认 Profile")
        preserve_names.add(other_id)

    for child in list(target.iterdir()):
        if child.name in preserve_names:
            continue
        _remove_profile_entry(child)
    if target.exists() and not any(target.iterdir()):
        _remove_profile_entry(target)
    return True


def _browser_instance_delete_snapshot(instance_id: str) -> dict[str, Any]:
    with STATE_LOCK:
        state = load_state()
        instance = dict(find_browser_instance(state, instance_id))
    # 在停止进程之前先完成路径校验；恶意或损坏记录不能借删除动作影响运行时。
    _validated_browser_profile_target(instance)
    return instance


def _finalize_browser_instance_delete(
    instance_snapshot: dict[str, Any],
) -> tuple[bool, dict[str, Any]]:
    instance_id = str(instance_snapshot.get("id") or "")
    with STATE_LOCK:
        state = load_state()
        current = dict(find_browser_instance(state, instance_id))
        if (
            str(current.get("node_id") or "") != str(instance_snapshot.get("node_id") or "")
            or bool(current.get("legacy")) != bool(instance_snapshot.get("legacy"))
        ):
            raise RuntimeError("浏览器记录在删除期间发生变化，已取消物理删除")
        records = [dict(item) for item in state.get("browser_instances", []) if isinstance(item, dict)]

    profile_deleted = _delete_browser_profile_data(current, records)

    with STATE_LOCK:
        state = load_state()
        current = find_browser_instance(state, instance_id)
        if (
            str(current.get("node_id") or "") != str(instance_snapshot.get("node_id") or "")
            or bool(current.get("legacy")) != bool(instance_snapshot.get("legacy"))
        ):
            raise RuntimeError("浏览器记录在删除期间发生变化，已拒绝移除状态")
        state["browser_instances"] = [
            item
            for item in state.get("browser_instances", [])
            if not (isinstance(item, dict) and str(item.get("id") or "") == instance_id)
        ]
        # 删除最后一条引用后，仅为浏览器保留的节点也一并移除。
        prune_preserved_browser_nodes(state)
        save_state(state)
    return profile_deleted, dict(current)


def delete_browser_instance(payload: dict[str, Any]) -> dict[str, Any]:
    """停止并永久删除一个浏览器记录、Profile 及其 WebDAV 远端副本。"""
    require_browser_feature()
    instance_id = str(payload.get("instance_id") or payload.get("id") or "")
    if not instance_id:
        raise ValueError("缺少浏览器实例 ID")

    with BROWSER_LAUNCH_LOCK:
        if remote_browser_feature_enabled():
            with SHUTDOWN_LOCK:
                if SHUTTING_DOWN.is_set():
                    raise RuntimeError("程序正在退出，不能删除浏览器记录")
                instance_snapshot = _browser_instance_delete_snapshot(instance_id)
            # Broker 网络调用不能持有 SHUTDOWN_LOCK / STATE_LOCK。
            stopped = _stop_remote_browser(
                {"instance_id": instance_id},
                auto_upload=False,
            )
            with SHUTDOWN_LOCK:
                profile_deleted, deleted_instance = _finalize_browser_instance_delete(
                    instance_snapshot
                )
        else:
            with SHUTDOWN_LOCK:
                if SHUTTING_DOWN.is_set():
                    raise RuntimeError("程序正在退出，不能删除浏览器记录")
                instance_snapshot = _browser_instance_delete_snapshot(instance_id)
                stopped = _stop_browser(
                    {"instance_id": instance_id},
                    auto_upload=False,
                )
                profile_deleted, deleted_instance = _finalize_browser_instance_delete(
                    instance_snapshot
                )

    mark_sync_local_change()
    try:
        # 全量 push 会发布不含该记录的新 state，并清理远端孤立 Profile 包。
        get_sync_engine().maybe_auto_push(reason="删除浏览器记录")
    except Exception:
        pass
    return {
        "ok": True,
        "instance_id": instance_id,
        "browser_name": str(deleted_instance.get("name") or "浏览器"),
        "profile_deleted": profile_deleted,
        "killed_browsers": safe_int(stopped.get("killed_browsers"), 0),
    }


FP_CHROMIUM_RELEASES_API = "https://api.github.com/repos/adryfish/fingerprint-chromium/releases"


def _fingerprint_asset_arch(name: str) -> str | None:
    lowered = name.lower()
    if any(term in lowered for term in ("arm64", "aarch64")):
        return "arm64"
    if any(term in lowered for term in ("amd64", "x86_64", "x86-64", "x64")):
        return "x86_64"
    return None


def _pick_fingerprint_asset(
    releases: list[dict[str, Any]],
    host_os: str | None = None,
    host_arch: str | None = None,
    *,
    strict_arch: bool = False,
) -> dict[str, Any] | None:
    """Pick the newest fingerprint-chromium package for the current OS.

    macOS packages are typically `*_macos.dmg` and often omit arch tokens.
    Windows/Linux packages, when published, are usually zip/tar archives with
    `windows`/`linux` and arch markers in the file name.
    """
    os_token = host_os or host_os_token()
    desired_arch = host_arch or host_arch_token()

    def asset_score(name: str) -> int:
        lowered = name.lower()
        if os_token == "darwin":
            if not (lowered.endswith(".dmg") and any(term in lowered for term in ("macos", "darwin"))):
                return -1
            return 100
        if os_token == "windows":
            if not any(term in lowered for term in ("windows", "win64", "win32")):
                return -1
            if not lowered.endswith((".zip", ".7z")):
                return -1
        elif os_token == "linux":
            if "linux" not in lowered:
                return -1
            if not lowered.endswith((".zip", ".tar.gz", ".tgz", ".tar.xz", ".tar")):
                return -1
        else:
            return -1
        if any(skip in lowered for skip in _foreign_os_skip_terms(os_token)):
            return -1
        asset_arch = _fingerprint_asset_arch(lowered)
        if strict_arch and asset_arch != desired_arch:
            return -1
        value = 10
        if asset_arch == desired_arch:
            value += 20
        if lowered.endswith(".zip"):
            value += 5
        return value

    best: dict[str, Any] | None = None
    best_score = -1
    ordered_releases = sorted(
        enumerate(releases),
        key=lambda item: (
            str(item[1].get("published_at") or item[1].get("created_at") or ""),
            -item[0],
        ),
        reverse=True,
    )
    for _, release in ordered_releases:
        if release.get("draft") or release.get("prerelease"):
            continue
        for asset in release.get("assets") or []:
            name = str(asset.get("name") or "")
            score = asset_score(name)
            if score < 0:
                continue
            url = asset.get("browser_download_url")
            if not url:
                continue
            if score > best_score:
                best_score = score
                best = {
                    **asset,
                    "name": name,
                    "url": str(url),
                    "browser_download_url": str(url),
                    "tag": str(release.get("tag_name") or ""),
                }
        if best is not None and os_token == "darwin":
            # Preserve previous behavior: first non-prerelease macOS dmg wins.
            return best
    return best


def _verify_macos_binary_arch(app_path: Path) -> str | None:
    # 用 file 命令读取内核二进制的架构(如 arm64 / x86_64),用于与主机架构做兼容性判断。
    exe = _bundle_executable(app_path)
    if not exe:
        return None
    probe = subprocess.run(["file", "-b", str(exe)], capture_output=True, text=True)
    text = (probe.stdout or "").lower()
    if "arm64" in text:
        return "arm64"
    if "x86_64" in text:
        return "x86_64"
    return None


def _replace_installed_path(staged: Path, target: Path) -> None:
    backup = target.with_name(f".{target.name}.backup-{secrets.token_hex(6)}")
    moved_existing = False
    try:
        if target.exists():
            target.replace(backup)
            moved_existing = True
        staged.replace(target)
    except BaseException:
        if moved_existing and not target.exists() and backup.exists():
            backup.replace(target)
        raise
    finally:
        if staged.exists():
            if staged.is_dir():
                shutil.rmtree(staged, ignore_errors=True)
            else:
                staged.unlink(missing_ok=True)
    if backup.exists():
        if backup.is_dir():
            shutil.rmtree(backup, ignore_errors=True)
        else:
            backup.unlink(missing_ok=True)


def _install_from_dmg(dmg_path: Path, dest_root: Path) -> Path:
    # 挂载 dmg,复制其中的 Chromium.app 到目标目录,再卸载。全程使用 macOS 自带 hdiutil。
    if host_os_token() != "darwin":
        raise RuntimeError("仅 macOS 支持从 dmg 安装指纹内核")
    mount_point = dest_root / "_mnt"
    if mount_point.exists():
        subprocess.run(["hdiutil", "detach", str(mount_point), "-force"], capture_output=True)
        shutil.rmtree(mount_point, ignore_errors=True)
    mount_point.mkdir(parents=True, exist_ok=True)
    attach = subprocess.run(
        ["hdiutil", "attach", str(dmg_path), "-nobrowse", "-noverify", "-mountpoint", str(mount_point)],
        capture_output=True,
        text=True,
    )
    if attach.returncode != 0:
        shutil.rmtree(mount_point, ignore_errors=True)
        raise RuntimeError(f"挂载 dmg 失败:{attach.stderr.strip() or attach.stdout.strip()}")
    try:
        source_app = next(mount_point.glob("**/Chromium.app"), None)
        if not source_app:
            raise RuntimeError("dmg 中未找到 Chromium.app")
        target_app = dest_root / "Chromium.app"
        staged_app = dest_root / f".Chromium.app.install-{secrets.token_hex(6)}"
        try:
            shutil.copytree(source_app, staged_app, symlinks=True)
            _replace_installed_path(staged_app, target_app)
        finally:
            shutil.rmtree(staged_app, ignore_errors=True)
    finally:
        subprocess.run(["hdiutil", "detach", str(mount_point), "-force"], capture_output=True)
        shutil.rmtree(mount_point, ignore_errors=True)
    # 去除 Gatekeeper 隔离属性,避免首次启动被 macOS 拦截("已损坏/无法验证开发者")。
    subprocess.run(["xattr", "-dr", "com.apple.quarantine", str(target_app)], capture_output=True)
    return target_app


def _install_fingerprint_from_archive(
    archive_path: Path,
    dest_root: Path,
    *,
    require_wrapper: bool = False,
) -> Path:
    extract_root = dest_root / "_extract"
    if extract_root.exists():
        shutil.rmtree(extract_root, ignore_errors=True)
    extract_root.mkdir(parents=True, exist_ok=True)
    try:
        _extract_archive(archive_path, extract_root)
        resolved = _resolve_fingerprint_executable_in_root(extract_root)
        if not resolved:
            raise RuntimeError("压缩包中未找到 Chromium/chrome 可执行文件")
        resolved_path = Path(resolved)
        if require_wrapper:
            wrapper = resolved_path.parent / "chrome-wrapper"
            if not wrapper.is_file() or (os.name != "nt" and not os.access(wrapper, os.X_OK)):
                raise RuntimeError("远程浏览器指纹内核包缺少可执行的 chrome-wrapper")
        # Prefer keeping a Chromium.app bundle layout when present.
        app_root = next((parent for parent in resolved_path.parents if parent.name.endswith(".app")), None)
        if app_root is not None:
            target_app = dest_root / app_root.name
            staged_app = dest_root / f".{app_root.name}.install-{secrets.token_hex(6)}"
            try:
                shutil.copytree(app_root, staged_app, symlinks=True)
                _replace_installed_path(staged_app, target_app)
            finally:
                shutil.rmtree(staged_app, ignore_errors=True)
            return target_app

        target_dir = dest_root / "chromium"
        # Copy the directory that contains the executable so shared libs remain nearby.
        source_dir = resolved_path.parent
        staged_dir = dest_root / f".chromium.install-{secrets.token_hex(6)}"
        try:
            shutil.copytree(source_dir, staged_dir, symlinks=True)
            _replace_installed_path(staged_dir, target_dir)
        finally:
            shutil.rmtree(staged_dir, ignore_errors=True)
        return target_dir / resolved_path.name
    finally:
        shutil.rmtree(extract_root, ignore_errors=True)


def fingerprint_kernel_id_for(asset_name: str, tag: str = "", sha256: str = "") -> str:
    """由安装包文件名/Release tag 推导内核 ID；无法识别版本时用 tag 或文件摘要区分。"""
    version = browser_fingerprint.fingerprint_version_from_name(asset_name) or browser_fingerprint.fingerprint_version_from_name(tag)
    if version:
        return version
    cleaned_tag = re.sub(r"[^A-Za-z0-9._-]+", "-", str(tag or "")).strip("-._")[:48]
    if cleaned_tag and browser_fingerprint.KERNEL_ID_PATTERN.fullmatch(cleaned_tag) and cleaned_tag not in {"env", "legacy", "upload"}:
        return cleaned_tag
    if sha256:
        return f"upload-{sha256[:12]}"
    return f"custom-{int(time.time())}"


def _install_fingerprint_package(
    package_path: Path,
    source_name: str,
    version: str,
    *,
    kernel_id: str | None = None,
    source: str = "download",
) -> dict[str, Any]:
    host_os = host_os_token()
    host_arch = host_arch_token()
    fingerprint_arch = "x86_64" if remote_browser_feature_enabled() else host_arch
    kernel_id = kernel_id or fingerprint_kernel_id_for(source_name, version)
    if browser_fingerprint.KERNEL_ID_PATTERN.fullmatch(kernel_id) is None or kernel_id in {"env", "legacy"}:
        raise RuntimeError("指纹内核版本标识无效")
    versions_root = fingerprint_kernel_versions_root()
    versions_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    dest_root = versions_root / kernel_id
    dest_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    lowered = source_name.lower()
    if lowered.endswith(".dmg"):
        target = _install_from_dmg(package_path, dest_root)
    else:
        target = _install_fingerprint_from_archive(
            package_path,
            dest_root,
            require_wrapper=remote_browser_feature_enabled(),
        )

    # 安装后校验 macOS 二进制架构；Docker 远程包在安装前已按文件名严格限定 x86_64，
    # arm64 主服务不应尝试直接执行给 amd64 worker 使用的内核。
    binary_arch = _verify_macos_binary_arch(target) if host_os == "darwin" else None
    arch_warning = ""
    if binary_arch and binary_arch != host_arch:
        if host_arch == "x86_64" and binary_arch == "arm64":
            arch_warning = "警告:导入的是 arm64 内核,但本机是 Intel(x86_64),无法运行。"
        elif host_arch == "arm64" and binary_arch == "x86_64":
            arch_warning = "提示:导入的是 x86_64 内核,将通过 Rosetta 在 Apple Silicon 上运行。"

    executable = _resolve_fingerprint_executable_in_root(Path(target))
    detected_version = browser_fingerprint.fingerprint_version_from_name(source_name) or browser_fingerprint.fingerprint_version_from_name(version)
    if not detected_version and executable:
        detected_version = browser_fingerprint.fingerprint_version_from_name(_app_bundle_version(executable))
    if not detected_version and executable:
        # Windows 发布包在 chrome.exe 旁有以完整版本号命名的资源目录。
        detected_version = next(
            (
                child.name
                for child in sorted(Path(executable).parent.iterdir())
                if child.is_dir() and re.fullmatch(r"\d{2,4}\.\d+\.\d+\.\d+", child.name)
            ),
            "",
        )
    if (
        detected_version
        and kernel_id != detected_version
        and browser_fingerprint.KERNEL_ID_PATTERN.fullmatch(detected_version)
        and not (versions_root / detected_version).exists()
    ):
        # 内核 ID 统一使用版本号，确保同步到其他设备后「固定版本」仍能对应到同一内核。
        renamed = versions_root / detected_version
        dest_root.replace(renamed)
        if executable:
            executable = str(renamed / Path(executable).resolve().relative_to(dest_root.resolve()))
        dest_root = renamed
        kernel_id = detected_version
    metadata = {
        "id": kernel_id,
        "version": detected_version or kernel_id,
        "asset": source_name,
        "tag": version,
        "source": source,
        "installed_at": now_ts(),
        "arch": binary_arch or fingerprint_arch,
        "executable": Path(executable).resolve().relative_to(dest_root.resolve()).as_posix() if executable else "",
    }
    metadata_path = dest_root / FINGERPRINT_KERNEL_METADATA_FILE
    temporary = metadata_path.with_suffix(".tmp")
    temporary.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(metadata_path)

    invalidate_fingerprint_chromium_cache()
    resolved = fingerprint_chromium_path(force_refresh=True, kernel_id=kernel_id)
    if not resolved:
        raise RuntimeError("指纹内核安装后未找到可执行文件")
    return {
        "ok": True,
        "already_installed": False,
        "path": resolved,
        "version": version,
        "kernel_id": kernel_id,
        "kernel_version": metadata["version"],
        "host_os": host_os,
        "host_arch": host_arch,
        "fingerprint_arch": fingerprint_arch,
        "binary_arch": binary_arch,
        "arch_warning": arch_warning,
        "asset": source_name,
    }


def download_fingerprint_chromium(payload: dict[str, Any]) -> dict[str, Any]:
    require_browser_feature()
    with FINGERPRINT_INSTALL_LOCK:
        return _download_fingerprint_chromium(payload)


def _fingerprint_release_target() -> tuple[str, str, bool]:
    host_os = host_os_token()
    # Docker worker 与其 Linux 指纹内核必须使用同一架构。当前 worker 固定
    # linux/amd64，因此 arm64 主服务容器也必须选择 x86_64 指纹包。
    fingerprint_arch = "x86_64" if remote_browser_feature_enabled() else host_arch_token()
    return host_os, fingerprint_arch, remote_browser_feature_enabled()


def list_fingerprint_kernel_releases() -> dict[str, Any]:
    """列出上游 Release 中适配本机的版本，供「选择版本下载」使用。"""
    require_browser_feature()
    host_os, fingerprint_arch, strict_arch = _fingerprint_release_target()
    try:
        releases = _github_json(f"{FP_CHROMIUM_RELEASES_API}?per_page=30")
    except Exception as error:
        raise RuntimeError(f"获取指纹内核版本列表失败:{error}") from error
    installed = fingerprint_kernel_ids()
    result: list[dict[str, Any]] = []
    for release in releases if isinstance(releases, list) else []:
        if not isinstance(release, dict):
            continue
        asset = _pick_fingerprint_asset([release], host_os=host_os, host_arch=fingerprint_arch, strict_arch=strict_arch)
        if not asset:
            continue
        tag = str(release.get("tag_name") or "")
        kernel_id = fingerprint_kernel_id_for(str(asset.get("name") or ""), tag)
        result.append(
            {
                "tag": tag,
                "kernel_id": kernel_id,
                "asset": str(asset.get("name") or ""),
                "size": safe_int(asset.get("size"), 0) or None,
                "published_at": str(release.get("published_at") or "")[:40],
                "installed": kernel_id in installed,
            }
        )
    return {"ok": True, "os": host_os, "arch": fingerprint_arch, "releases": result[:30]}


def _download_fingerprint_chromium(payload: dict[str, Any]) -> dict[str, Any]:
    # 一键下载指纹内核:查询 GitHub Releases,按当前 OS 选择包并安装到 versions/<版本>/。
    # payload.tag 指定 Release；payload.latest 表示即使已安装其他版本也下载最新版；
    # 都未提供时保持旧语义：已有可用内核则直接返回。
    requested_tag = str(payload.get("tag") or "").strip()
    force = truthy(payload.get("force"))
    existing = fingerprint_chromium_path()
    if existing and not force and not requested_tag and not truthy(payload.get("latest")):
        return {"ok": True, "already_installed": True, "path": existing}

    host_os, fingerprint_arch, strict_arch = _fingerprint_release_target()
    try:
        releases = _github_json(f"{FP_CHROMIUM_RELEASES_API}?per_page=30")
    except Exception as error:
        raise RuntimeError(f"获取指纹内核版本列表失败:{error}") from error
    candidates = releases if isinstance(releases, list) else []
    if requested_tag:
        candidates = [item for item in candidates if isinstance(item, dict) and str(item.get("tag_name") or "") == requested_tag]
        if not candidates:
            raise RuntimeError(f"上游没有名为 {requested_tag} 的 Release")

    asset = _pick_fingerprint_asset(
        candidates,
        host_os=host_os,
        host_arch=fingerprint_arch,
        strict_arch=strict_arch,
    )
    if not asset:
        raise RuntimeError(
            f"未找到适配 {host_os}/{fingerprint_arch} 的指纹内核包。"
            "该项目并非每个版本都为全部平台发布安装包;可稍后重试,"
            "或手动下载后设置 PROXY_BRIDGE_FP_CHROMIUM 指向 Chromium 可执行文件。"
        )
    kernel_id = fingerprint_kernel_id_for(str(asset["name"]), str(asset.get("tag") or ""))
    installed_path = fingerprint_chromium_path(kernel_id=kernel_id)
    if installed_path and not force:
        return {"ok": True, "already_installed": True, "path": installed_path, "kernel_id": kernel_id}
    if installed_path:
        # 原地替换同一版本前必须确认没有浏览器正在使用内核文件。
        _assert_kernel_upload_target_idle("fingerprint-chromium")

    dest_root = fingerprint_chromium_root()
    dest_root.mkdir(parents=True, exist_ok=True)
    package_path = dest_root / _safe_asset_name(asset)
    try:
        _download_asset(asset, package_path)
    except Exception as error:
        package_path.unlink(missing_ok=True)
        raise RuntimeError(f"下载指纹内核失败:{error}") from error

    try:
        return _install_fingerprint_package(
            package_path,
            str(asset["name"]),
            str(asset.get("tag") or ""),
            kernel_id=kernel_id,
            source="download",
        )
    finally:
        package_path.unlink(missing_ok=True)


def set_default_fingerprint_kernel(payload: dict[str, Any]) -> dict[str, Any]:
    require_browser_feature()
    kernel_id = browser_fingerprint.normalize_kernel_id(payload.get("id"), strict=True)
    if kernel_id and kernel_id not in fingerprint_kernel_ids():
        raise ValueError(f"指纹内核版本 {kernel_id} 未安装")
    root = fingerprint_chromium_root()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = root / FINGERPRINT_DEFAULT_KERNEL_FILE
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps({"id": kernel_id}, ensure_ascii=False), encoding="utf-8")
    temporary.replace(target)
    invalidate_fingerprint_chromium_cache()
    return {"ok": True, "default": kernel_id, "kernels": list_fingerprint_kernels(force_refresh=True)}


def delete_fingerprint_kernel(payload: dict[str, Any]) -> dict[str, Any]:
    """删除一个已安装的内核版本；有记录固定使用或有浏览器运行时拒绝。"""
    require_browser_feature()
    kernel_id = browser_fingerprint.normalize_kernel_id(payload.get("id"), strict=True)
    if not kernel_id:
        raise ValueError("缺少内核版本")
    if kernel_id == "env":
        raise ValueError("环境变量指定的内核请修改 PROXY_BRIDGE_FP_CHROMIUM，而不是在这里删除")
    with BROWSER_LAUNCH_LOCK:
        with FINGERPRINT_INSTALL_LOCK:
            if kernel_id not in fingerprint_kernel_ids():
                raise ValueError(f"指纹内核版本 {kernel_id} 未安装")
            with STATE_LOCK:
                state = load_state()
                pinned = [item for item in state.get("browser_instances", []) if item.get("kernel_version") == kernel_id]
            if pinned:
                raise ValueError(f"有 {len(pinned)} 条浏览器记录固定使用该版本，请先修改它们的内核设置")
            _assert_kernel_upload_target_idle("fingerprint-chromium")
            if kernel_id == "legacy":
                root = fingerprint_chromium_root()
                targets = [
                    child
                    for child in root.iterdir()
                    if child.name not in {"versions", FINGERPRINT_DEFAULT_KERNEL_FILE} and not child.name.startswith(".")
                ]
            else:
                targets = [fingerprint_kernel_versions_root() / kernel_id]
            for target in targets:
                if target.is_dir() and not target.is_symlink():
                    shutil.rmtree(target)
                else:
                    target.unlink(missing_ok=True)
            invalidate_fingerprint_chromium_cache()
    return {"ok": True, "deleted": kernel_id, "kernels": list_fingerprint_kernels(force_refresh=True)}


def _safe_uploaded_filename(encoded_name: Any) -> str:
    raw = str(encoded_name or "")
    if not raw or len(raw) > 720 or any(ord(char) < 32 or ord(char) == 127 for char in raw):
        raise ValueError("上传文件名无效")
    try:
        name = urllib.parse.unquote(raw, encoding="utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise ValueError("上传文件名编码无效") from error
    if (
        not name
        or len(name) > 180
        or name in {".", ".."}
        or Path(name).name != name
        or "/" in name
        or "\\" in name
        or any(ord(char) < 32 or ord(char) == 127 for char in name)
    ):
        raise ValueError("上传文件名无效")
    return name


def _validate_uploaded_package_name(kind: str, filename: str) -> None:
    if kind not in CORE_UPLOAD_KINDS:
        raise ValueError("上传目标只能是 sing-box、xray 或 fingerprint-chromium")
    host_os = host_os_token()
    host_arch = host_arch_token()
    fake_asset = {
        "name": filename,
        "browser_download_url": "https://upload.invalid/package",
    }
    if kind == "sing-box":
        release = {"assets": [fake_asset]}
        if _pick_sing_box_asset(release, host_arch, host_os) is None:
            raise ValueError(
                f"请选择保留官方文件名、适配 {host_os}/{host_arch} 的 sing-box .tar.gz/.tgz/.zip 包"
            )
        return
    if kind == "xray":
        release = {"assets": [fake_asset]}
        if _pick_xray_asset(release, host_arch, host_os) is None:
            raise ValueError(
                f"请选择保留官方文件名、适配 {host_os}/{host_arch} 的 Xray .zip 包"
            )
        return

    if filename.lower().endswith(".7z"):
        raise ValueError("当前不支持直接导入 .7z，请下载 fingerprint-chromium 的 zip/tar/dmg 包")
    fingerprint_arch = "x86_64" if remote_browser_feature_enabled() else host_arch
    releases = [{"tag_name": "upload", "assets": [fake_asset]}]
    if _pick_fingerprint_asset(
        releases,
        host_os=host_os,
        host_arch=fingerprint_arch,
        strict_arch=remote_browser_feature_enabled(),
    ) is None:
        raise ValueError(
            "请选择保留官方文件名、适配 "
            f"{host_os}/{fingerprint_arch} 的 fingerprint-chromium 安装包"
        )


def _assert_kernel_upload_target_idle(kind: str, *, fingerprint_kernel_id: str | None = None) -> None:
    if (
        kind == "fingerprint-chromium"
        and fingerprint_kernel_id
        and fingerprint_kernel_id not in fingerprint_kernel_ids()
    ):
        # 新版本安装到独立目录，不会替换正在运行的内核文件。
        return
    with STATE_LOCK:
        state = load_state()
        matching_bridge_ids = {
            str(item.get("id") or "")
            for item in state.get("bridges", [])
            if isinstance(item, dict)
            and str(item.get("core") or "") == kind
            and item.get("id")
        }
        active_bridges = {
            str(item.get("id") or "")
            for item in state.get("bridges", [])
            if isinstance(item, dict)
            and item.get("status") in ACTIVE_BRIDGE_STATUSES
            and str(item.get("core") or "") == kind
            and item.get("id")
        }
        active_browsers = {
            str(item.get("id") or "")
            for item in state.get("browser_instances", [])
            if isinstance(item, dict) and item.get("status") in ACTIVE_BROWSER_STATUSES
        }

    with PROCESS_LOCK:
        if kind in {"sing-box", "xray"}:
            active_bridges.update(
                bridge_id
                for bridge_id in matching_bridge_ids
                if (process := PROCESS_TABLE.get(bridge_id)) is not None and process.poll() is None
            )
            active_tests = [
                test_id
                for test_id, process in TEST_PROCESS_TABLE.items()
                if process.poll() is None
            ]
            if active_bridges:
                raise RuntimeError(f"请先停止正在使用 {kind} 的本地代理，再替换核心")
            if active_tests:
                raise RuntimeError("请等待所有节点测速结束，再替换代理核心")
            return
        active_browsers.update(
            instance_id
            for instance_id, process in BROWSER_PROCESS_TABLE.items()
            if process.poll() is None
        )
        active_browsers.update(
            str(node_id)
            for node_id, processes in BROWSER_TABLE.items()
            if any(process.poll() is None for process in processes)
        )
    if remote_browser_feature_enabled():
        sessions, broker_error = remote_browser_sessions(force=True)
        if sessions is None:
            raise RuntimeError(
                "无法确认远程浏览器 Worker 是否已停止，请先恢复 Browser Broker："
                f"{broker_error or '状态不可用'}"
            )
        active_browsers.update(
            instance_id
            for instance_id, session in sessions.items()
            if session.get("status") in REMOTE_BROWSER_LIVE_SESSION_STATUSES
        )
    if active_browsers:
        raise RuntimeError("请先停止所有浏览器实例，再替换 fingerprint-chromium")


def install_uploaded_kernel(
    kind: str,
    package_path: Path,
    filename: str,
    *,
    size: int,
    sha256: str,
) -> dict[str, Any]:
    _validate_uploaded_package_name(kind, filename)
    if kind in {"sing-box", "xray"}:
        # 与 start_bridge() 使用相同的锁顺序，上传安装期间禁止新 Bridge 抢占旧核心。
        with BRIDGE_START_LOCK:
            with CORE_INSTALL_LOCK:
                _assert_kernel_upload_target_idle(kind)
                installed = _install_core_from_archive(
                    kind,
                    package_path,
                    filename,
                    "",
                    validate_binary=True,
                )
                version = _core_version_line(installed, kind)
        return {
            "ok": True,
            "kind": kind,
            "filename": filename,
            "size": size,
            "sha256": sha256,
            "path": installed,
            "version": version,
        }

    require_browser_feature()
    kernel_id = fingerprint_kernel_id_for(filename, "", sha256)
    # 阻止上传校验后又启动新实例；锁顺序与浏览器启动路径保持一致。
    with BROWSER_LAUNCH_LOCK:
        with FINGERPRINT_INSTALL_LOCK:
            _assert_kernel_upload_target_idle(kind, fingerprint_kernel_id=kernel_id)
            result = _install_fingerprint_package(
                package_path,
                filename,
                "本地上传",
                kernel_id=kernel_id,
                source="upload",
            )
    return {
        **result,
        "kind": kind,
        "filename": filename,
        "size": size,
        "sha256": sha256,
    }


def _single_upload_header(headers: Any, name: str, *, required: bool = True) -> str:
    values = headers.get_all(name, [])
    if len(values) > 1:
        raise ValueError(f"{name} 不能重复")
    if not values:
        if required:
            raise ValueError(f"缺少 {name}")
        return ""
    value = str(values[0])
    if not value or any(char in value for char in "\r\n\0"):
        raise ValueError(f"{name} 无效")
    return value


def _parse_core_upload_headers(headers: Any) -> tuple[str, str, int, str]:
    transfer_encoding = _single_upload_header(headers, "Transfer-Encoding", required=False)
    if transfer_encoding:
        raise ValueError("内核上传不支持 Transfer-Encoding")

    content_encoding = _single_upload_header(headers, "Content-Encoding", required=False)
    if content_encoding and content_encoding.strip().lower() != "identity":
        raise CoreUploadUnsupportedMediaType("内核上传不支持压缩的 HTTP 请求正文")

    content_type = _single_upload_header(headers, "Content-Type")
    if content_type.strip().lower() != "application/octet-stream":
        raise CoreUploadUnsupportedMediaType("内核上传仅支持 application/octet-stream")

    length_values = headers.get_all("Content-Length", [])
    if not length_values:
        raise CoreUploadLengthRequired("内核上传必须提供 Content-Length")
    if len(length_values) != 1:
        raise ValueError("Content-Length 不能重复")
    raw_length = str(length_values[0])
    if re.fullmatch(r"[0-9]+", raw_length) is None:
        raise ValueError("Content-Length 无效")
    length = int(raw_length)
    if length < 1:
        raise ValueError("不能上传空文件")
    if length > CORE_UPLOAD_LIMIT:
        raise CoreUploadTooLarge(f"上传文件不能超过 {CORE_UPLOAD_LIMIT // (1024 ** 3)} GiB")

    kind = _single_upload_header(headers, CORE_UPLOAD_KIND_HEADER).strip().lower()
    if kind not in CORE_UPLOAD_KINDS:
        raise ValueError("上传目标只能是 sing-box、xray 或 fingerprint-chromium")
    filename = _safe_uploaded_filename(_single_upload_header(headers, CORE_UPLOAD_NAME_HEADER))

    expected_sha256 = _single_upload_header(
        headers,
        CORE_UPLOAD_SHA256_HEADER,
        required=False,
    ).strip().lower()
    if expected_sha256 and re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None:
        raise ValueError("SHA-256 必须是 64 位十六进制字符串")
    return kind, filename, length, expected_sha256


def _uploaded_package_suffix(filename: str) -> str:
    lowered = filename.lower()
    for suffix in (".tar.gz", ".tar.xz", ".tgz", ".zip", ".tar", ".dmg"):
        if lowered.endswith(suffix):
            return suffix
    return Path(filename).suffix[:20]


def _stream_core_upload(
    source: Any,
    destination: Any,
    length: int,
    expected_sha256: str = "",
) -> str:
    remaining = length
    digest = hashlib.sha256()
    while remaining:
        try:
            chunk = source.read(min(CORE_UPLOAD_CHUNK_SIZE, remaining))
        except TimeoutError as error:
            raise RuntimeError("上传读取超时，请检查网络后重试") from error
        if not chunk:
            raise ValueError("上传数据不完整，请重新选择文件后重试")
        if len(chunk) > remaining:
            raise ValueError("上传数据长度与 Content-Length 不一致")
        destination.write(chunk)
        digest.update(chunk)
        remaining -= len(chunk)
    actual_sha256 = digest.hexdigest()
    if expected_sha256 and not hmac.compare_digest(actual_sha256, expected_sha256):
        raise ValueError("SHA-256 校验失败，文件可能不完整或选择了错误的校验值")
    return actual_sha256


def runtime_log_backup_path(path: Path, index: int) -> Path:
    return path.with_name(f"{path.name}.{index}")


def runtime_log_archive_path(path: Path, day: str) -> Path:
    return path.with_name(f"{path.name}.{day}")


def _runtime_log_files() -> list[Path]:
    try:
        return [item for item in RUNTIME_ROOT.iterdir() if item.is_file() and ".log" in item.name]
    except FileNotFoundError:
        return []


def runtime_log_storage_payload() -> dict[str, Any]:
    files = _runtime_log_files()
    total = 0
    for path in files:
        try:
            total += path.stat().st_size
        except OSError:
            continue
    settings = runtime_log_settings_snapshot()
    return {
        "used_bytes": total,
        "quota_bytes": settings["disk_quota_bytes"],
        "file_count": len(files),
    }


def cleanup_runtime_logs(settings: dict[str, Any] | None = None, current_time: float | None = None) -> dict[str, Any]:
    normalized = normalize_log_settings(settings or runtime_log_settings_snapshot())
    now = time.time() if current_time is None else current_time
    cutoff = now - normalized["retention_days"] * 24 * 60 * 60
    with PROCESS_LOCK:
        active_names = {f"{bridge_id}.log" for bridge_id in PROCESS_TABLE}
    deleted = 0
    freed = 0
    with LOG_ROTATION_LOCK:
        files: list[tuple[Path, os.stat_result]] = []
        for path in _runtime_log_files():
            try:
                stat = path.stat()
            except OSError:
                continue
            if path.name not in active_names and stat.st_mtime < cutoff:
                try:
                    path.unlink()
                    deleted += 1
                    freed += stat.st_size
                except OSError:
                    files.append((path, stat))
                else:
                    continue
                continue
            files.append((path, stat))
        total = sum(stat.st_size for _, stat in files)
        quota = normalized["disk_quota_bytes"]
        for path, stat in sorted(files, key=lambda item: (item[1].st_mtime, item[0].name)):
            if total <= quota:
                break
            if path.name in active_names:
                continue
            try:
                path.unlink()
            except OSError:
                continue
            total -= stat.st_size
            deleted += 1
            freed += stat.st_size
    return {"deleted_files": deleted, "freed_bytes": freed, "used_bytes": max(0, total)}


def rotate_runtime_log_daily(path: Path, current_time: float | None = None) -> bool:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return False
    now = time.time() if current_time is None else current_time
    current_day = time.strftime("%Y-%m-%d", time.localtime(now))
    file_day = time.strftime("%Y-%m-%d", time.localtime(stat.st_mtime))
    if not stat.st_size or file_day == current_day:
        return False
    with LOG_ROTATION_LOCK:
        try:
            stat = path.stat()
        except FileNotFoundError:
            return False
        file_day = time.strftime("%Y-%m-%d", time.localtime(stat.st_mtime))
        if not stat.st_size or file_day == current_day:
            return False
        archive = runtime_log_archive_path(path, file_day)
        suffix = 1
        while archive.exists():
            archive = runtime_log_archive_path(path, f"{file_day}.{suffix}")
            suffix += 1
        path.replace(archive)
        try:
            os.chmod(archive, 0o600)
        except OSError:
            pass
        return True


def rotate_runtime_log(
    path: Path,
    *,
    max_bytes: int | None = None,
    backup_count: int | None = None,
    incoming_bytes: int = 0,
) -> bool:
    max_bytes = runtime_log_settings_snapshot()["max_file_bytes"] if max_bytes is None else max_bytes
    backup_count = CORE_LOG_BACKUP_COUNT if backup_count is None else backup_count
    if max_bytes <= 0 or backup_count <= 0:
        return False
    try:
        if path.stat().st_size + max(0, incoming_bytes) <= max_bytes:
            return False
    except FileNotFoundError:
        return False
    with LOG_ROTATION_LOCK:
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            return False
        if size + max(0, incoming_bytes) <= max_bytes:
            return False
        oldest = runtime_log_backup_path(path, backup_count)
        oldest.unlink(missing_ok=True)
        for index in range(backup_count - 1, 0, -1):
            source = runtime_log_backup_path(path, index)
            if source.exists():
                source.replace(runtime_log_backup_path(path, index + 1))
        with path.open("rb") as source:
            source.seek(max(0, size - max_bytes), os.SEEK_SET)
            archived = source.read(max_bytes)
        backup = runtime_log_backup_path(path, 1)
        _write_private_atomic(
            backup,
            archived,
            temp_path=backup.with_suffix(backup.suffix + ".tmp"),
        )
        with path.open("r+b") as current:
            if hasattr(os, "fchmod"):
                try:
                    os.fchmod(current.fileno(), 0o600)
                except OSError:
                    pass
            current.truncate(0)
            current.flush()
            os.fsync(current.fileno())
        return True


def pump_runtime_log(
    stream: Any,
    path: Path,
    *,
    max_bytes: int | None = None,
    backup_count: int | None = None,
    bridge_id: str | None = None,
) -> None:
    settings = runtime_log_settings_snapshot()
    max_bytes = settings["max_file_bytes"] if max_bytes is None else max_bytes
    backup_count = CORE_LOG_BACKUP_COUNT if backup_count is None else backup_count
    chunk_size = max(1, min(65536, max_bytes))
    last_cleanup_at = 0.0
    try:
        while True:
            chunk = stream.read(chunk_size)
            if not chunk:
                break
            if isinstance(chunk, str):
                chunk = chunk.encode("utf-8", errors="replace")
            with LOG_ROTATION_LOCK:
                rotate_runtime_log_daily(path)
                rotate_runtime_log(
                    path,
                    max_bytes=max_bytes,
                    backup_count=backup_count,
                    incoming_bytes=len(chunk),
                )
                with path.open("ab") as log_file:
                    if hasattr(os, "fchmod"):
                        try:
                            os.fchmod(log_file.fileno(), 0o600)
                        except OSError:
                            pass
                    log_file.write(chunk)
                    log_file.flush()
            monotonic_now = time.monotonic()
            if monotonic_now - last_cleanup_at >= 60:
                cleanup_runtime_logs(settings)
                last_cleanup_at = monotonic_now
    except Exception as error:
        try:
            log_event(f"核心日志写入已停止: {error}", level="error", category="核心")
        except Exception:
            pass
    finally:
        try:
            stream.close()
        except Exception:
            pass
        if bridge_id:
            current_thread = threading.current_thread()
            with PROCESS_LOCK:
                if RUNTIME_LOG_PUMP_TABLE.get(bridge_id) is current_thread:
                    RUNTIME_LOG_PUMP_TABLE.pop(bridge_id, None)


def read_tail(path: Path, limit: int = 6000) -> str:
    try:
        with LOG_ROTATION_LOCK:
            with path.open("rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - max(0, limit)), os.SEEK_SET)
                data = f.read(max(0, limit))
            if size > CORE_LOG_MAX_BYTES:
                rotate_runtime_log(path)
        return data.decode("utf-8", errors="replace").strip()
    except Exception:
        return ""


def core_log_payload(core: str) -> dict[str, Any]:
    with SHUTDOWN_LOCK:
        return _core_log_payload(core)


def _core_log_payload(core: str) -> dict[str, Any]:
    core = core.strip()
    if core not in {"sing-box", "xray"}:
        raise ValueError("核心只能是 sing-box 或 xray")

    synchronize_process_state()
    with STATE_LOCK:
        state = load_state()
        if sync_process_status(state):
            save_state(state)
        bridges = [dict(bridge) for bridge in state.get("bridges", []) if bridge.get("core") == core]

    entries = []
    hidden_empty_count = 0
    sorted_bridges = sorted(bridges, key=lambda item: safe_int(item.get("started_at"), 0), reverse=True)
    for bridge in sorted_bridges:
        bridge_id = str(bridge.get("id") or "")
        if not bridge_id:
            continue
        log_path = RUNTIME_ROOT / f"{bridge_id}.log"
        log = read_tail(log_path, CORE_LOG_TAIL_LIMIT)
        status = str(bridge.get("status") or "-")
        if not log and status not in CORE_LOG_VISIBLE_STATUSES:
            hidden_empty_count += 1
            continue
        entries.append(
            {
                "id": bridge_id,
                "node_id": bridge.get("node_id"),
                "node_name": bridge.get("node_name") or bridge.get("node_id") or "未知节点",
                "port": bridge.get("port"),
                "status": status,
                "started_at": bridge.get("started_at"),
                "log_path": str(log_path),
                "log": log,
                "has_log": bool(log),
            }
        )

    visible_count = len(entries)
    entries = entries[:CORE_LOG_ENTRY_LIMIT]
    return {
        "core": core,
        "core_path": core_paths().get(core),
        "entries": entries,
        "total_count": len(bridges),
        "visible_count": visible_count,
        "hidden_empty_count": hidden_empty_count,
        "truncated_count": max(0, visible_count - len(entries)),
        "generated_at": now_ts(),
        "tail_limit": CORE_LOG_TAIL_LIMIT,
    }


def stop_process(bridge_id: str, *, pop_traffic: bool = True) -> dict[str, int]:
    with PROCESS_LOCK:
        proxy = LOG_PROXY_TABLE.get(bridge_id)
        process = PROCESS_TABLE.get(bridge_id)
        log_pump = RUNTIME_LOG_PUMP_TABLE.get(bridge_id)
        owner_node_id = BRIDGE_NODE_TABLE.get(bridge_id)
    if proxy:
        proxy.stop()
        with PROCESS_LOCK:
            if LOG_PROXY_TABLE.get(bridge_id) is proxy:
                LOG_PROXY_TABLE.pop(bridge_id, None)
    if process:
        termination_error: BaseException | None = None
        terminated = False
        try:
            terminate_process(process)
            terminated = True
        except BaseException as error:
            termination_error = error
            terminated = process.poll() is not None
        if terminated:
            if log_pump is not None and log_pump.is_alive():
                log_pump.join(timeout=3)
            with PROCESS_LOCK:
                if PROCESS_TABLE.get(bridge_id) is process:
                    PROCESS_TABLE.pop(bridge_id, None)
                if (
                    log_pump is not None
                    and RUNTIME_LOG_PUMP_TABLE.get(bridge_id) is log_pump
                    and not log_pump.is_alive()
                ):
                    RUNTIME_LOG_PUMP_TABLE.pop(bridge_id, None)
        elif log_pump is not None and not log_pump.is_alive():
            with PROCESS_LOCK:
                if RUNTIME_LOG_PUMP_TABLE.get(bridge_id) is log_pump:
                    RUNTIME_LOG_PUMP_TABLE.pop(bridge_id, None)
        if termination_error is not None and not terminated:
            raise termination_error
    elif log_pump is not None and not log_pump.is_alive():
        with PROCESS_LOCK:
            if RUNTIME_LOG_PUMP_TABLE.get(bridge_id) is log_pump:
                RUNTIME_LOG_PUMP_TABLE.pop(bridge_id, None)
    with PROCESS_LOCK:
        if (
            bridge_id not in PROCESS_TABLE
            and bridge_id not in LOG_PROXY_TABLE
            and BRIDGE_NODE_TABLE.get(bridge_id) == owner_node_id
        ):
            BRIDGE_NODE_TABLE.pop(bridge_id, None)
    if pop_traffic:
        return pop_bridge_traffic(bridge_id)
    return bridge_traffic_snapshot(bridge_id)


def persist_stopped_bridges(
    traffic_by_bridge: dict[str, dict[str, int]],
    *,
    disable: bool,
) -> dict[str, dict[str, int]]:
    if not traffic_by_bridge:
        return {}
    persisted_live_traffic: dict[str, dict[str, int]] = {}
    with STATE_LOCK:
        state = load_state()
        stopped_at = now_ts()
        for bridge in state.get("bridges", []):
            bridge_id = str(bridge.get("id") or "")
            if bridge_id not in traffic_by_bridge:
                continue
            live_traffic = live_bridge_traffic_snapshot(bridge_id)
            traffic = live_traffic or traffic_defaults(
                bridge.get("upload_bytes"),
                bridge.get("download_bytes"),
            )
            updates: dict[str, Any] = {
                **traffic,
                "status": "stopped",
                "pid": None,
                "stopped_at": stopped_at,
            }
            if disable:
                updates["enabled"] = False
            elif not SHUTTING_DOWN.is_set():
                updates["health"] = {"status": "error", "checked_at": stopped_at,
                                     "error": "代理核心或监听器意外退出"}
                log_event(f"运行中代理「{bridge.get('node_name') or bridge.get('node_id') or bridge_id}」意外退出",
                          level="error", category="探活")
            bridge.update(updates)
            bridge.pop("error", None)
            if live_traffic is not None:
                persisted_live_traffic[bridge_id] = live_traffic
        save_state(state)
    return persisted_live_traffic


def release_persisted_bridge_traffic(traffic_by_bridge: dict[str, dict[str, int]]) -> None:
    for bridge_id, traffic in traffic_by_bridge.items():
        pop_bridge_traffic_if_matches(bridge_id, traffic)


def restore_enabled_bridges() -> None:
    with STATE_LOCK:
        state = load_state()
        candidates = []
        for bridge in state.get("bridges", []):
            if bridge.get("enabled") or bridge.get("status") in ACTIVE_BRIDGE_STATUSES:
                bridge["enabled"] = True
                bridge["status"] = "starting"
                bridge["pid"] = None
                bridge.pop("error", None)
                candidates.append(dict(bridge))
        if candidates:
            save_state(state)

    for previous in candidates:
        node_id = str(previous.get("node_id") or "")
        if not node_id:
            continue
        previous_id = str(previous.get("id") or "")
        requested_port = safe_int(previous.get("port"), 0) or None
        requested_core = str(previous.get("core") or "auto")
        try:
            restored = start_bridge(
                {
                    "node_id": node_id,
                    "port": requested_port,
                    "core": requested_core,
                    "restore": True,
                    "upload_bytes": previous.get("upload_bytes", 0),
                    "download_bytes": previous.get("download_bytes", 0),
                }
            )
            if previous_id and restored.get("id") != previous_id:
                with STATE_LOCK:
                    state = load_state()
                    state["bridges"] = [b for b in state.get("bridges", []) if b.get("id") != previous_id]
                    save_state(state)
            log_event(f"已恢复本地代理:{restored.get('node_name')} → {restored.get('socks_url')}", category="代理")
        except Exception as exc:
            with STATE_LOCK:
                state = load_state()
                for bridge in state.get("bridges", []):
                    if bridge.get("id") == previous_id or (
                        bridge.get("node_id") == node_id and safe_int(bridge.get("port"), 0) == safe_int(previous.get("port"), 0)
                    ):
                        bridge["enabled"] = True
                        bridge["status"] = "failed"
                        bridge["pid"] = None
                        bridge["error"] = str(exc)
                save_state(state)
            log_event(f"恢复本地代理失败:{previous.get('node_name') or node_id}:{exc}", level="error", category="代理")


def restore_enabled_smart_gateway() -> None:
    with STATE_LOCK:
        state = load_state()
        settings = normalize_smart_gateway_settings((state.get("settings") or {}).get("smart_gateway"))
    if not settings.get("enabled"):
        return
    if docker_mode():
        settings["enabled"] = False
        settings["last_error"] = "Docker 模式未发布智能网关固定端口"
        with STATE_LOCK:
            state = load_state()
            state.setdefault("settings", {})["smart_gateway"] = settings
            save_state(state)
        log_event("智能网关恢复跳过：Docker 模式未发布固定端口", level="warn", category="代理")
        return
    try:
        start_smart_gateway(settings)
        log_event("已恢复智能网关", category="代理")
    except Exception as exc:
        settings["last_error"] = str(exc)[:1000]
        with STATE_LOCK:
            state = load_state()
            state.setdefault("settings", {})["smart_gateway"] = settings
            save_state(state)
        log_event(f"恢复智能网关失败：{exc}", level="error", category="代理")


def read_exact(sock: socket.socket, length: int) -> bytes:
    chunks = []
    remaining = length
    while remaining > 0:
        chunk = sock.recv(remaining)
        if not chunk:
            raise RuntimeError("SOCKS5 连接提前关闭")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


class Socks5LogProxy:
    def __init__(
        self,
        *,
        bridge_id: str,
        node_id: str,
        node_name: str,
        listen_port: int,
        upstream_port: int,
        listen_host: str = LOCAL_PROXY_LISTEN_HOST,
        max_clients: int = PROXY_MAX_CLIENTS,
        handshake_timeout: float = PROXY_HANDSHAKE_TIMEOUT,
        relay_idle_timeout: float = PROXY_RELAY_IDLE_TIMEOUT,
        security: dict[str, Any] | None = None,
    ) -> None:
        if listen_host not in {LOCAL_PROXY_LISTEN_HOST, LAN_PROXY_LISTEN_HOST}:
            raise ValueError("本地代理监听地址无效")
        if max_clients <= 0:
            raise ValueError("本地代理最大连接数必须大于 0")
        if handshake_timeout <= 0 or relay_idle_timeout <= 0:
            raise ValueError("本地代理超时必须大于 0")
        self.bridge_id = bridge_id
        self.node_id = node_id
        self.node_name = node_name
        self.listen_port = listen_port
        self.upstream_port = upstream_port
        self.listen_host = listen_host
        self.capacity_limit = int(max_clients)
        self.max_clients = self.capacity_limit
        self.handshake_timeout = float(handshake_timeout)
        self.relay_idle_timeout = float(relay_idle_timeout)
        self.security = normalize_lan_security(security)
        self.max_clients = min(self.capacity_limit, self.security["max_connections"])
        self._stop = threading.Event()
        self._server: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._client_lock = threading.RLock()
        self._client_sockets: set[socket.socket] = set()
        self._upstream_sockets: set[socket.socket] = set()
        self._client_threads: set[threading.Thread] = set()

    def security_snapshot(self) -> dict[str, Any]:
        with self._client_lock:
            return dict(self.security)

    def update_security(self, security: dict[str, Any], *, disconnect_lan: bool = False) -> int:
        normalized = normalize_lan_security(security)
        with self._client_lock:
            self.security = normalized
            self.max_clients = min(self.capacity_limit, normalized["max_connections"])
        if not disconnect_lan:
            return 0
        with PROXY_CLIENT_LOCK:
            client_ids = [
                client_id
                for client_id, item in ACTIVE_PROXY_CLIENTS.items()
                if item.get("bridge_id") == self.bridge_id and item.get("is_lan")
            ]
        disconnected = 0
        for client_id in client_ids:
            try:
                kick_proxy_client(client_id)
                disconnected += 1
            except ValueError:
                pass
        return disconnected

    def _client_is_lan(self, address: tuple[str, int]) -> bool:
        try:
            return not ipaddress.IPv4Address(address[0]).is_loopback
        except ipaddress.AddressValueError:
            return True

    def _client_requires_auth(self, address: tuple[str, int]) -> bool:
        if remote_browser_worker_client(address[0], self.listen_port):
            return False
        return self._client_is_lan(address) and self.security_snapshot()["auth_mode"] != "none"

    def _credentials_match(self, username: str, password: str) -> bool:
        security = self.security_snapshot()
        if security["auth_mode"] == "basic":
            expected_username = security["username"]
            expected_password = security["password"]
        elif security["auth_mode"] == "token":
            expected_username = "token"
            expected_password = security["token"]
        else:
            return True
        return hmac.compare_digest(username, expected_username) and hmac.compare_digest(password, expected_password)

    def _authenticate_socks5(self, client: socket.socket) -> bool:
        version = read_exact(client, 1)
        if version != b"\x01":
            return False
        username_length = read_exact(client, 1)[0]
        username = read_exact(client, username_length).decode("utf-8", errors="replace")
        password_length = read_exact(client, 1)[0]
        password = read_exact(client, password_length).decode("utf-8", errors="replace")
        authenticated = self._credentials_match(username, password)
        client.sendall(b"\x01\x00" if authenticated else b"\x01\x01")
        return authenticated

    def _http_authenticated(self, request: dict[str, Any]) -> bool:
        security = self.security_snapshot()
        if security["auth_mode"] == "none":
            return True
        authorization = str(request.get("header_map", {}).get("proxy-authorization") or "").strip()
        if security["auth_mode"] == "token" and authorization.lower().startswith("bearer "):
            return hmac.compare_digest(authorization[7:].strip(), security["token"])
        if not authorization.lower().startswith("basic "):
            return False
        try:
            decoded = base64.b64decode(authorization[6:].strip(), validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return False
        username, separator, password = decoded.partition(":")
        return bool(separator and self._credentials_match(username, password))

    def start(self) -> None:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind((self.listen_host, self.listen_port))
            server.listen(128)
            server.settimeout(0.5)
        except BaseException:
            server.close()
            raise
        self._server = server
        self._thread = threading.Thread(target=self._serve, name=f"request-log-proxy-{self.bridge_id}", daemon=True)
        self._thread.start()

    def _start_after_rebind(self) -> None:
        # macOS can retain a just-closed wildcard listener briefly after an accepted
        # connection. Retry only EADDRINUSE for a bounded window; other bind errors are final.
        deadline = time.monotonic() + PROXY_REBIND_RETRY_TIMEOUT
        while True:
            try:
                self.start()
                return
            except OSError as exc:
                if exc.errno != errno.EADDRINUSE or time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)

    def rebind(self, listen_host: str) -> None:
        """Move the public listener to another host, restoring the old listener on failure."""
        if listen_host not in {LOCAL_PROXY_LISTEN_HOST, LAN_PROXY_LISTEN_HOST}:
            raise ValueError("本地代理监听地址无效")
        if listen_host == self.listen_host and self.is_alive():
            return
        old_host = self.listen_host
        try:
            self.stop()
            self.listen_host = listen_host
            self._stop = threading.Event()
            self._start_after_rebind()
        except BaseException as rebind_error:
            self.listen_host = old_host
            self._stop = threading.Event()
            try:
                self._start_after_rebind()
            except BaseException as rollback_error:
                raise RuntimeError(
                    f"代理监听切换失败且无法恢复 {old_host}: {rollback_error}"
                ) from rebind_error
            raise

    def stop(self) -> None:
        self._stop.set()
        server = self._server
        accept_thread = self._thread
        if server:
            try:
                server.close()
            except OSError:
                pass
        with self._client_lock:
            clients = list(self._client_sockets)
            upstreams = list(self._upstream_sockets)
        for active_socket in clients + upstreams:
            try:
                active_socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                active_socket.close()
            except OSError:
                pass
        current = threading.current_thread()
        if accept_thread and accept_thread.is_alive() and accept_thread is not current:
            accept_thread.join(timeout=1.0)
        deadline = time.monotonic() + 3.0
        while True:
            with self._client_lock:
                handlers = [
                    thread
                    for thread in self._client_threads
                    if thread is not current and thread.is_alive()
                ]
            if not handlers:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            for thread in handlers:
                thread.join(timeout=max(0.0, deadline - time.monotonic()))
        with self._client_lock:
            lingering = [thread for thread in self._client_threads if thread.is_alive()]
        if (accept_thread and accept_thread.is_alive()) or lingering:
            raise RuntimeError("本地代理连接未能及时停止")
        with self._client_lock:
            self._client_sockets.clear()
            self._upstream_sockets.clear()
            self._client_threads = {thread for thread in self._client_threads if thread.is_alive()}
        self._server = None
        self._thread = None

    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and not self._stop.is_set())

    def _track_upstream(self, upstream: socket.socket) -> None:
        with self._client_lock:
            if self._stop.is_set():
                try:
                    upstream.close()
                except OSError:
                    pass
                raise RuntimeError("本地代理正在停止")
            self._upstream_sockets.add(upstream)

    def _close_upstream(self, upstream: socket.socket) -> None:
        with self._client_lock:
            self._upstream_sockets.discard(upstream)
        try:
            upstream.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            upstream.close()
        except OSError:
            pass

    def _serve(self) -> None:
        assert self._server is not None
        while not self._stop.is_set():
            try:
                client, address = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            security = self.security_snapshot()
            is_lan = self._client_is_lan(address)
            if (
                self.listen_host == LAN_PROXY_LISTEN_HOST
                and not remote_browser_worker_client(address[0], self.listen_port)
                and not proxy_client_allowed(address[0], security["allowed_cidrs"])
            ):
                client.close()
                continue
            with self._client_lock:
                if self._stop.is_set():
                    client.close()
                    break
                if len(self._client_threads) >= self.max_clients:
                    client.close()
                    continue
            client_id = register_active_proxy_client(
                proxy=self,
                client=client,
                address=address,
                is_lan=is_lan,
                max_connections=security["max_connections"],
            )
            if client_id is None:
                client.close()
                continue
            with self._client_lock:
                if self._stop.is_set() or len(self._client_threads) >= self.max_clients:
                    unregister_active_proxy_client(client_id)
                    client.close()
                    if self._stop.is_set():
                        break
                    continue
                thread = threading.Thread(
                    target=self._run_client_handler,
                    args=(client, address, client_id),
                    daemon=True,
                )
                self._client_sockets.add(client)
                self._client_threads.add(thread)
            try:
                thread.start()
            except BaseException:
                with self._client_lock:
                    self._client_sockets.discard(client)
                    self._client_threads.discard(thread)
                unregister_active_proxy_client(client_id)
                client.close()
                raise

    def _run_client_handler(self, client: socket.socket, address: tuple[str, int], client_id: str) -> None:
        thread = threading.current_thread()
        try:
            self._handle_client(client, address)
        finally:
            unregister_active_proxy_client(client_id)
            with self._client_lock:
                self._client_sockets.discard(client)
                self._client_threads.discard(thread)

    def _handle_client(self, client: socket.socket, address: tuple[str, int]) -> None:
        try:
            client.settimeout(self.handshake_timeout)
            first = read_exact(client, 1)
            client_id = active_proxy_client_id(client)
            if first == b"\x05":
                update_active_proxy_client(client_id, protocol="socks5")
                self._handle_socks5_client(client, address, first)
            elif first.isalpha():
                update_active_proxy_client(client_id, protocol="http")
                self._handle_http_proxy_client(client, address, first)
        except Exception:
            pass
        finally:
            try:
                client.close()
            except OSError:
                pass

    def _handle_socks5_client(self, client: socket.socket, address: tuple[str, int], first: bytes) -> None:
        log_id = ""
        started = time.monotonic()
        upstream: socket.socket | None = None
        counters = {"upload": 0, "download": 0}
        client_label = f"{address[0]}:{address[1]}"
        try:
            header = first + read_exact(client, 1)
            if header[0] != 5:
                return
            methods = read_exact(client, header[1])
            required_method = 2 if self._client_requires_auth(address) else 0
            if required_method not in methods:
                client.sendall(b"\x05\xff")
                return
            client.sendall(bytes((5, required_method)))
            if required_method == 2 and not self._authenticate_socks5(client):
                return

            request_head = read_exact(client, 4)
            if request_head[0] != 5:
                return
            command = request_head[1]
            target_host, target_port, request_tail = self._read_address(client, request_head[3])
            target = f"{target_host}:{target_port}"
            log_id = stable_id("request", self.bridge_id, client_label, target, str(time.time_ns()))
            append_request_log(
                {
                    "id": log_id,
                    "bridge_id": self.bridge_id,
                    "node_id": self.node_id,
                    "node_name": self.node_name,
                    "client": client_label,
                    "target_host": target_host,
                    "target_port": target_port,
                    "target": target,
                    "command": "connect" if command == 1 else str(command),
                    "proxy_protocol": "socks5",
                    "status": "connecting",
                    "started_at": now_ts(),
                    "upload_bytes": 0,
                    "download_bytes": 0,
                }
            )
            if command != 1:
                self._send_socks_reply(client, 7)
                update_request_log(log_id, {"status": "failed", "message": "仅支持 SOCKS5 CONNECT", "ended_at": now_ts()})
                return

            upstream, reply = self._connect_upstream_for_socks_request(request_head + request_tail)
            client.sendall(reply)
            if len(reply) < 2 or reply[1] != 0:
                update_request_log(
                    log_id,
                    {
                        "status": "failed",
                        "message": f"核心返回 SOCKS5 状态码 {reply[1] if len(reply) > 1 else '-'}",
                        "ended_at": now_ts(),
                    },
                )
                return

            update_request_log(log_id, {"status": "connected"})
            client.settimeout(None)
            upstream.settimeout(None)
            counters = self._relay(client, upstream)
            elapsed_ms = int(round((time.monotonic() - started) * 1000))
            update_request_log(
                log_id,
                {
                    "status": "closed",
                    "ended_at": now_ts(),
                    "duration_ms": elapsed_ms,
                    "upload_bytes": counters["upload"],
                    "download_bytes": counters["download"],
                },
            )
        except Exception as exc:
            if log_id:
                elapsed_ms = int(round((time.monotonic() - started) * 1000))
                update_request_log(
                    log_id,
                    {
                        "status": "failed",
                        "message": str(exc),
                        "ended_at": now_ts(),
                        "duration_ms": elapsed_ms,
                        "upload_bytes": counters["upload"],
                        "download_bytes": counters["download"],
                    },
                )
            try:
                self._send_socks_reply(client, 1)
            except OSError:
                pass
        finally:
            if upstream:
                self._close_upstream(upstream)

    def _handle_http_proxy_client(self, client: socket.socket, address: tuple[str, int], first: bytes) -> None:
        log_id = ""
        started = time.monotonic()
        upstream: socket.socket | None = None
        response_started = False
        counters = {"upload": 0, "download": 0}
        client_label = f"{address[0]}:{address[1]}"
        try:
            header_bytes, body_prefix = self._read_http_request(client, first)
            request = self._parse_http_request(header_bytes)
            if self._client_requires_auth(address) and not self._http_authenticated(request):
                self._send_http_auth_required(client)
                response_started = True
                return
            target_host, target_port, forward_head, is_connect = self._http_forward_plan(request)
            target = f"{target_host}:{target_port}"
            command = "connect" if is_connect else str(request["method"]).lower()
            log_id = stable_id("request", self.bridge_id, client_label, target, str(time.time_ns()))
            append_request_log(
                {
                    "id": log_id,
                    "bridge_id": self.bridge_id,
                    "node_id": self.node_id,
                    "node_name": self.node_name,
                    "client": client_label,
                    "target_host": target_host,
                    "target_port": target_port,
                    "target": target,
                    "command": command,
                    "proxy_protocol": "http",
                    "status": "connecting",
                    "started_at": now_ts(),
                    "upload_bytes": 0,
                    "download_bytes": 0,
                }
            )

            upstream = self._open_upstream_tunnel(target_host, target_port)
            if is_connect:
                client.sendall(b"HTTP/1.1 200 Connection Established\r\nProxy-Agent: ProxyBridge\r\n\r\n")
                response_started = True
                if body_prefix:
                    upstream.sendall(body_prefix)
                    self._record_transfer(counters, "upload", len(body_prefix))
            else:
                upstream.sendall(forward_head)
                self._record_transfer(counters, "upload", len(forward_head))
                if body_prefix:
                    upstream.sendall(body_prefix)
                    self._record_transfer(counters, "upload", len(body_prefix))

            update_request_log(log_id, {"status": "connected"})
            client.settimeout(None)
            upstream.settimeout(None)
            relay_counters = self._relay(client, upstream)
            counters["upload"] += relay_counters["upload"]
            counters["download"] += relay_counters["download"]
            elapsed_ms = int(round((time.monotonic() - started) * 1000))
            update_request_log(
                log_id,
                {
                    "status": "closed",
                    "ended_at": now_ts(),
                    "duration_ms": elapsed_ms,
                    "upload_bytes": counters["upload"],
                    "download_bytes": counters["download"],
                },
            )
        except Exception as exc:
            if log_id:
                elapsed_ms = int(round((time.monotonic() - started) * 1000))
                update_request_log(
                    log_id,
                    {
                        "status": "failed",
                        "message": str(exc),
                        "ended_at": now_ts(),
                        "duration_ms": elapsed_ms,
                        "upload_bytes": counters["upload"],
                        "download_bytes": counters["download"],
                    },
                )
            if not response_started:
                try:
                    self._send_http_error(client, 502 if log_id else 400, str(exc) or "Proxy Error")
                except OSError:
                    pass
        finally:
            if upstream:
                self._close_upstream(upstream)

    def _connect_upstream_for_socks_request(self, request: bytes) -> tuple[socket.socket, bytes]:
        upstream = socket.create_connection(("127.0.0.1", self.upstream_port), timeout=12.0)
        try:
            self._track_upstream(upstream)
            upstream.settimeout(12.0)
            upstream.sendall(b"\x05\x01\x00")
            upstream_greeting = read_exact(upstream, 2)
            if upstream_greeting != b"\x05\x00":
                raise RuntimeError("核心 SOCKS5 握手失败")
            upstream.sendall(request)
            reply = self._read_socks_reply(upstream)
            return upstream, reply
        except Exception:
            self._close_upstream(upstream)
            raise

    def _open_upstream_tunnel(self, host: str, port: int) -> socket.socket:
        request = b"\x05\x01\x00" + self._socks_address_tail(host, port)
        upstream, reply = self._connect_upstream_for_socks_request(request)
        if len(reply) < 2 or reply[1] != 0:
            self._close_upstream(upstream)
            raise RuntimeError(f"核心返回 SOCKS5 状态码 {reply[1] if len(reply) > 1 else '-'}")
        return upstream

    def _socks_address_tail(self, host: str, port: int) -> bytes:
        host = host.strip().strip("[]")
        if port <= 0 or port > 65535:
            raise RuntimeError("目标端口不合法")
        try:
            return b"\x01" + socket.inet_pton(socket.AF_INET, host) + port.to_bytes(2, "big")
        except OSError:
            pass
        try:
            return b"\x04" + socket.inet_pton(socket.AF_INET6, host) + port.to_bytes(2, "big")
        except OSError:
            pass
        raw = host.encode("idna")
        if not raw or len(raw) > 255:
            raise RuntimeError("目标域名不合法")
        return b"\x03" + bytes([len(raw)]) + raw + port.to_bytes(2, "big")

    def _read_http_request(self, client: socket.socket, first: bytes) -> tuple[bytes, bytes]:
        data = bytearray(first)
        while b"\r\n\r\n" not in data:
            if len(data) > HTTP_PROXY_HEADER_LIMIT:
                raise RuntimeError("HTTP 代理请求头过大")
            chunk = client.recv(4096)
            if not chunk:
                raise RuntimeError("HTTP 代理请求提前关闭")
            data.extend(chunk)
        header, body_prefix = bytes(data).split(b"\r\n\r\n", 1)
        if len(header) + 4 > HTTP_PROXY_HEADER_LIMIT:
            raise RuntimeError("HTTP 代理请求头过大")
        return header + b"\r\n\r\n", body_prefix

    def _parse_http_request(self, header_bytes: bytes) -> dict[str, Any]:
        text = header_bytes.decode("iso-8859-1", errors="replace")
        lines = text.split("\r\n")
        parts = lines[0].split(" ", 2)
        if len(parts) != 3 or not parts[2].startswith("HTTP/"):
            raise RuntimeError("HTTP 代理请求行不合法")
        method, target, version = parts[0].upper(), parts[1], parts[2]
        headers: list[tuple[str, str]] = []
        header_map: dict[str, str] = {}
        for line in lines[1:]:
            if not line:
                continue
            if ":" not in line:
                raise RuntimeError("HTTP 代理请求头不合法")
            name, value = line.split(":", 1)
            name = name.strip()
            value = value.strip()
            headers.append((name, value))
            header_map[name.lower()] = value
        return {"method": method, "target": target, "version": version, "headers": headers, "header_map": header_map}

    def _http_forward_plan(self, request: dict[str, Any]) -> tuple[str, int, bytes, bool]:
        method = str(request["method"])
        target = str(request["target"])
        headers = request["header_map"]
        if method == "CONNECT":
            host, port = self._split_host_port(target, 443)
            return host, port, b"", True

        parsed = urllib.parse.urlsplit(target)
        if parsed.scheme and parsed.netloc:
            scheme = parsed.scheme.lower()
            if scheme not in {"http", "https"}:
                raise RuntimeError(f"不支持的 HTTP 代理协议:{scheme}")
            if scheme == "https":
                raise RuntimeError("HTTPS 代理请求需要使用 CONNECT 方法")
            if not parsed.hostname:
                raise RuntimeError("HTTP 代理目标缺少主机名")
            host = parsed.hostname
            port = parsed.port or 80
            path = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
        else:
            host, port = self._split_host_port(str(headers.get("host") or ""), 80)
            path = target or "/"

        return host, port, self._build_http_forward_head(request, host, port, path), False

    def _build_http_forward_head(self, request: dict[str, Any], host: str, port: int, path: str) -> bytes:
        lines = [f"{request['method']} {path} {request['version']}\r\n"]
        has_host = False
        for name, value in request["headers"]:
            lowered = str(name).lower()
            if lowered in {"connection", "keep-alive", "proxy-authorization", "proxy-connection"}:
                continue
            if lowered == "host":
                has_host = True
            lines.append(f"{name}: {value}\r\n")
        if not has_host:
            lines.append(f"Host: {self._format_host_header(host, port)}\r\n")
        lines.append("Connection: close\r\n")
        lines.append("\r\n")
        return "".join(lines).encode("iso-8859-1", errors="replace")

    def _split_host_port(self, authority: str, default_port: int) -> tuple[str, int]:
        value = authority.strip()
        if not value:
            raise RuntimeError("HTTP 代理目标缺少 Host")
        if "@" in value:
            value = value.rsplit("@", 1)[-1]
        if value.startswith("["):
            end = value.find("]")
            if end < 0:
                raise RuntimeError("IPv6 Host 格式不合法")
            host = value[1:end]
            rest = value[end + 1 :]
            port = safe_int(rest[1:], default_port) if rest.startswith(":") else default_port
        else:
            host = value
            port = default_port
            if value.count(":") == 1:
                maybe_host, maybe_port = value.rsplit(":", 1)
                if maybe_port.isdigit():
                    host = maybe_host
                    port = safe_int(maybe_port, default_port)
        host = host.strip()
        if not host:
            raise RuntimeError("HTTP 代理目标缺少 Host")
        if port <= 0 or port > 65535:
            raise RuntimeError("HTTP 代理目标端口不合法")
        return host, port

    def _format_host_header(self, host: str, port: int) -> str:
        display_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
        return f"{display_host}:{port}"

    def _read_address(self, sock: socket.socket, atyp: int) -> tuple[str, int, bytes]:
        if atyp == 1:
            raw = read_exact(sock, 4)
            host = socket.inet_ntoa(raw)
            tail = raw
        elif atyp == 3:
            size_raw = read_exact(sock, 1)
            raw = read_exact(sock, size_raw[0])
            host = raw.decode("ascii", errors="replace")
            tail = size_raw + raw
        elif atyp == 4:
            raw = read_exact(sock, 16)
            host = socket.inet_ntop(socket.AF_INET6, raw)
            tail = raw
        else:
            raise RuntimeError("SOCKS5 地址类型不支持")
        port_raw = read_exact(sock, 2)
        port = int.from_bytes(port_raw, "big")
        return host, port, tail + port_raw

    def _read_socks_reply(self, sock: socket.socket) -> bytes:
        head = read_exact(sock, 4)
        atyp = head[3]
        if atyp == 1:
            tail = read_exact(sock, 4)
        elif atyp == 3:
            size = read_exact(sock, 1)
            tail = size + read_exact(sock, size[0])
        elif atyp == 4:
            tail = read_exact(sock, 16)
        else:
            raise RuntimeError("核心返回了未知 SOCKS5 地址类型")
        return head + tail + read_exact(sock, 2)

    def _send_socks_reply(self, client: socket.socket, status: int) -> None:
        client.sendall(b"\x05" + bytes([status]) + b"\x00\x01" + b"\x00\x00\x00\x00\x00\x00")

    def _send_http_error(self, client: socket.socket, status: int, message: str) -> None:
        reason = {400: "Bad Request", 502: "Bad Gateway"}.get(status, "Proxy Error")
        body = f"{status} {reason}: {message}\n".encode("utf-8", errors="replace")
        head = (
            f"HTTP/1.1 {status} {reason}\r\n"
            "Connection: close\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\n"
            "\r\n"
        ).encode("ascii")
        client.sendall(head + body)

    def _send_http_auth_required(self, client: socket.socket) -> None:
        security = self.security_snapshot()
        scheme = "Bearer" if security["auth_mode"] == "token" else 'Basic realm="ProxyBridge"'
        body = b"Proxy Authentication Required\n"
        head = (
            "HTTP/1.1 407 Proxy Authentication Required\r\n"
            f"Proxy-Authenticate: {scheme}\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n\r\n"
        ).encode("ascii")
        client.sendall(head + body)

    def _record_transfer(self, counters: dict[str, int], key: str, byte_count: int) -> None:
        counters[key] += byte_count
        traffic_key = "upload_bytes" if key == "upload" else "download_bytes"
        add_bridge_traffic(self.bridge_id, traffic_key, byte_count)

    def _relay(self, client: socket.socket, upstream: socket.socket) -> dict[str, int]:
        counters = {"upload": 0, "download": 0}
        relay_aborted = threading.Event()
        errors: list[Exception] = []
        errors_lock = threading.Lock()
        activity_lock = threading.Lock()
        last_activity = time.monotonic()

        def shutdown_pair() -> None:
            for sock in (client, upstream):
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

        def abort_relay() -> None:
            relay_aborted.set()
            shutdown_pair()

        def record_activity() -> None:
            nonlocal last_activity
            with activity_lock:
                last_activity = time.monotonic()

        def idle_expired() -> bool:
            with activity_lock:
                return time.monotonic() - last_activity >= self.relay_idle_timeout

        def pump(src: socket.socket, dst: socket.socket, key: str) -> None:
            try:
                src.settimeout(min(1.0, self.relay_idle_timeout))
                while not relay_aborted.is_set():
                    if self._stop.is_set():
                        abort_relay()
                        return
                    try:
                        data = src.recv(65536)
                    except socket.timeout:
                        if idle_expired():
                            abort_relay()
                            return
                        continue
                    if not data:
                        dst.shutdown(socket.SHUT_WR)
                        return
                    record_activity()
                    dst.sendall(data)
                    self._record_transfer(counters, key, len(data))
            except OSError:
                abort_relay()
            except Exception as exc:
                with errors_lock:
                    errors.append(exc)
                abort_relay()

        upload = threading.Thread(target=pump, args=(client, upstream, "upload"), daemon=True)
        download = threading.Thread(target=pump, args=(upstream, client, "download"), daemon=True)
        upload.start()
        download.start()
        upload.join()
        download.join()
        if errors:
            raise errors[0]
        return counters


class SmartGatewayProxy(Socks5LogProxy):
    def __init__(
        self,
        *,
        listen_port: int,
        listen_host: str = LOCAL_PROXY_LISTEN_HOST,
        max_clients: int = SMART_GATEWAY_MAX_CLIENTS,
        strategy: str = "health",
        region: str = "",
        query: str = "",
        security: dict[str, Any] | None = None,
    ) -> None:
        if strategy not in SMART_GATEWAY_STRATEGIES:
            raise ValueError("智能网关策略无效")
        super().__init__(
            bridge_id=SMART_GATEWAY_ID,
            node_id="",
            node_name="智能网关",
            listen_port=listen_port,
            upstream_port=0,
            listen_host=listen_host,
            max_clients=max_clients,
            security=security,
        )
        self.strategy = strategy
        self.region = str(region or "")[:128]
        self.query = str(query or "")[:128]
        self._metrics_lock = threading.Lock()
        self._allocations: dict[str, dict[str, Any]] = {}
        self._recent_switches: collections.deque[dict[str, Any]] = collections.deque(maxlen=20)
        self._last_member_id = ""

    def metrics_snapshot(self) -> dict[str, Any]:
        with self._metrics_lock:
            total = sum(item["connections"] for item in self._allocations.values())
            return {"allocations": [{**item, "share": item["connections"] / total if total else 0}
                                    for item in self._allocations.values()],
                    "recent_switches": list(reversed(self._recent_switches))}

    def settings_snapshot(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "region": self.region,
            "query": self.query,
            "listen_host": self.listen_host,
            "listen_port": self.listen_port,
        }

    def update_gateway_settings(
        self,
        *,
        strategy: str,
        region: str,
        query: str,
    ) -> None:
        if strategy not in SMART_GATEWAY_STRATEGIES:
            raise ValueError("智能网关策略无效")
        self.strategy = strategy
        self.region = str(region or "")[:128]
        self.query = str(query or "")[:128]

    def _gateway_candidates(self) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        with STATE_LOCK:
            state = load_state()
            raw_members = pool_members(
                state,
                region=self.region,
                q=self.query,
                ready_only=True,
            )
            public_members = public_pool_members(state, raw_members)
        raw_by_id = {str(item.get("id") or ""): item for item in raw_members}
        return [
            (raw_by_id[str(public.get("id") or "")], public)
            for public in public_members
            if str(public.get("id") or "") in raw_by_id
        ]

    def _record_gateway_health(
        self,
        bridge_id: str,
        *,
        ok: bool,
        latency_ms: int | None = None,
        error: Any = "",
    ) -> None:
        try:
            with STATE_LOCK:
                state = load_state()
                bridge = next(
                    (item for item in state.get("bridges", []) if str(item.get("id") or "") == bridge_id),
                    None,
                )
                if bridge is None:
                    return
                bridge["pool_health"] = update_pool_health_record(
                    bridge.get("pool_health"),
                    ok=ok,
                    latency_ms=latency_ms,
                    error=error,
                )
                save_state(state)
        except Exception as exc:
            log_event(f"智能网关更新节点健康状态失败：{exc}", level="warn", category="代理")

    def _connect_to_gateway_member(
        self,
        bridge: dict[str, Any],
        request: bytes,
    ) -> tuple[socket.socket, bytes]:
        bridge_id = str(bridge.get("id") or "")
        core_port = safe_int(bridge.get("core_port"), 0)
        if core_port <= 0:
            raise RuntimeError("代理池成员缺少核心端口")
        started = time.monotonic()
        upstream: socket.socket | None = None
        try:
            upstream = socket.create_connection(("127.0.0.1", core_port), timeout=12.0)
            self._track_upstream(upstream)
            upstream.settimeout(12.0)
            upstream.sendall(b"\x05\x01\x00")
            if read_exact(upstream, 2) != b"\x05\x00":
                raise RuntimeError("代理池成员 SOCKS5 握手失败")
            upstream.sendall(request)
            reply = self._read_socks_reply(upstream)
            if len(reply) < 2 or reply[1] != 0:
                raise RuntimeError(f"代理池成员返回 SOCKS5 状态码 {reply[1] if len(reply) > 1 else '-'}")
            self._record_gateway_health(
                bridge_id,
                ok=True,
                latency_ms=int(round((time.monotonic() - started) * 1000)),
            )
            with self._metrics_lock:
                allocation = self._allocations.setdefault(bridge_id, {
                    "bridge_id": bridge_id, "node_id": bridge.get("node_id"),
                    "node_name": bridge.get("node_name"), "connections": 0})
                allocation["connections"] += 1
                if bridge_id != self._last_member_id:
                    self._recent_switches.append({"at": now_ts(), "from": self._last_member_id,
                                                  "to": bridge_id, "node_name": bridge.get("node_name")})
                    self._last_member_id = bridge_id
            return upstream, reply
        except Exception as exc:
            self._record_gateway_health(bridge_id, ok=False, error=exc)
            if upstream is not None:
                self._close_upstream(upstream)
            raise

    def _connect_upstream_for_socks_request(self, request: bytes) -> tuple[socket.socket, bytes]:
        candidates = self._gateway_candidates()
        if not candidates:
            raise RuntimeError("智能网关没有可用代理")
        remaining = list(candidates)
        last_error: BaseException | None = None
        while remaining:
            public_candidates = [public for _raw, public in remaining]
            picked = pick_pool_member(public_candidates, self.strategy)
            picked_id = str(picked.get("id") or "")
            selected = next(
                ((raw, public) for raw, public in remaining if str(public.get("id") or "") == picked_id),
                None,
            )
            if selected is None:
                break
            remaining.remove(selected)
            try:
                return self._connect_to_gateway_member(selected[0], request)
            except Exception as exc:
                last_error = exc
        raise RuntimeError(f"智能网关所有代理节点均连接失败：{last_error}") from last_error

    def _record_transfer(self, counters: dict[str, int], key: str, byte_count: int) -> None:
        counters[key] += byte_count


def _smart_gateway_settings_from_payload(
    payload: dict[str, Any] | None,
    current: dict[str, Any],
    *,
    enabled_default: bool | None = None,
) -> dict[str, Any]:
    raw = payload if isinstance(payload, dict) else {}
    nested = raw.get("settings") if isinstance(raw.get("settings"), dict) else {}
    merged = {**current, **nested, **{key: value for key, value in raw.items() if key in {
        "enabled", "port", "strategy", "region", "query", "max_connections"
    }}}
    if enabled_default is not None and "enabled" not in merged:
        merged["enabled"] = enabled_default
    if "enabled" in merged and not isinstance(merged.get("enabled"), bool):
        raise ValueError("智能网关 enabled 必须是布尔值")
    port = safe_int(merged.get("port"), SMART_GATEWAY_DEFAULT_PORT)
    if not 1 <= port <= 65535:
        raise ValueError("智能网关端口必须在 1..65535 之间")
    strategy = str(merged.get("strategy") or "health").strip().lower()
    if strategy not in SMART_GATEWAY_STRATEGIES:
        raise ValueError("智能网关策略只能是 random、round_robin、health 或 latency")
    max_connections = safe_int(merged.get("max_connections"), SMART_GATEWAY_MAX_CLIENTS)
    if not 1 <= max_connections <= SMART_GATEWAY_MAX_CLIENTS:
        raise ValueError(f"智能网关连接数上限必须在 1..{SMART_GATEWAY_MAX_CLIENTS} 之间")
    return {
        "enabled": merged.get("enabled") is True,
        "port": port,
        "strategy": strategy,
        "region": str(merged.get("region") or "")[:128],
        "query": str(merged.get("query") or "")[:128],
        "max_connections": max_connections,
        "last_error": str(current.get("last_error") or "")[:1000],
    }


def _smart_gateway_port_conflict(
    state: dict[str, Any],
    port: int,
    listen_host: str,
    *,
    current_proxy: SmartGatewayProxy | None = None,
) -> str | None:
    for bridge in state.get("bridges", []):
        if not isinstance(bridge, dict) or bridge.get("status") not in ACTIVE_BRIDGE_STATUSES:
            continue
        if safe_int(bridge.get("port"), 0) == port:
            return f"端口 {port} 已被本地代理占用"
    with SMART_GATEWAY_LOCK:
        existing = SMART_GATEWAY_TABLE.get(SMART_GATEWAY_ID)
    if existing is not None and existing is not current_proxy and existing.is_alive():
        if safe_int(existing.listen_port, 0) == port:
            return f"端口 {port} 已被智能网关占用"
    hosts = {listen_host}
    if listen_host == LOCAL_PROXY_LISTEN_HOST:
        hosts.add(LAN_PROXY_LISTEN_HOST)
    if any(not is_port_free(port, host) for host in hosts):
        return f"端口 {port} 已被其他进程占用"
    return None


def smart_gateway_status_payload(
    state: dict[str, Any] | None = None,
    *,
    lan_addresses: list[str] | None = None,
) -> dict[str, Any]:
    if state is None:
        with STATE_LOCK:
            state = load_state()
    settings = normalize_smart_gateway_settings((state.get("settings") or {}).get("smart_gateway"))
    with SMART_GATEWAY_LOCK:
        proxy = SMART_GATEWAY_TABLE.get(SMART_GATEWAY_ID)
    running = bool(proxy and proxy.is_alive())
    if running and proxy is not None:
        runtime_settings = proxy.settings_snapshot()
        listen_host = str(runtime_settings.get("listen_host") or LOCAL_PROXY_LISTEN_HOST)
        port = safe_int(runtime_settings.get("listen_port"), settings["port"])
    else:
        listen_host = proxy_listen_host(state)
        port = settings["port"]
    gateway_addresses = [LOCAL_PROXY_LISTEN_HOST]
    if listen_host == LAN_PROXY_LISTEN_HOST:
        gateway_addresses = list(lan_addresses) if lan_addresses is not None else lan_ipv4_addresses()
        if not gateway_addresses:
            gateway_addresses = [LOCAL_PROXY_LISTEN_HOST]
    raw_members = pool_members(
        state,
        region=settings["region"],
        q=settings["query"],
        ready_only=True,
    )
    with PROXY_CLIENT_LOCK:
        clients = [
            {key: value for key, value in item.items() if key != "socket"}
            for item in ACTIVE_PROXY_CLIENTS.values()
            if item.get("bridge_id") == SMART_GATEWAY_ID
        ]
    public_settings = {
        key: settings[key]
        for key in ("enabled", "port", "strategy", "region", "query", "max_connections", "last_error")
    }
    public_settings.update({"listen_host": listen_host, "running": running})
    return {
        "settings": public_settings,
        **(proxy.metrics_snapshot() if running and proxy and hasattr(proxy, "metrics_snapshot")
           else {"allocations": [], "recent_switches": []}),
        "enabled": bool(settings["enabled"]),
        "running": running,
        "port": port,
        "listen_host": listen_host,
        "strategy": settings["strategy"],
        "region": settings["region"],
        "query": settings["query"],
        "max_connections": settings["max_connections"],
        "last_error": settings["last_error"],
        "available_members": len(raw_members),
        "client_count": len(clients),
        "clients": clients,
        "socks_url": f"socks5://{gateway_addresses[0]}:{port}",
        "http_url": f"http://{gateway_addresses[0]}:{port}",
        "socks_urls": [f"socks5://{address}:{port}" for address in gateway_addresses],
        "http_urls": [f"http://{address}:{port}" for address in gateway_addresses],
    }


def _persist_smart_gateway_settings(settings: dict[str, Any]) -> None:
    with STATE_LOCK:
        state = load_state()
        state.setdefault("settings", {})["smart_gateway"] = normalize_smart_gateway_settings(settings)
        save_state(state)


def _running_smart_gateway_settings(proxy: SmartGatewayProxy) -> dict[str, Any]:
    snapshot = proxy.settings_snapshot()
    return {
        "enabled": True,
        "port": safe_int(snapshot.get("listen_port"), SMART_GATEWAY_DEFAULT_PORT),
        "strategy": str(snapshot.get("strategy") or "health"),
        "region": str(snapshot.get("region") or "")[:128],
        "query": str(snapshot.get("query") or "")[:128],
        "max_connections": clamp_int(
            getattr(proxy, "capacity_limit", SMART_GATEWAY_MAX_CLIENTS),
            SMART_GATEWAY_MAX_CLIENTS,
            1,
            SMART_GATEWAY_MAX_CLIENTS,
        ),
        "last_error": "",
    }


def _apply_running_smart_gateway_settings(
    proxy: SmartGatewayProxy,
    settings: dict[str, Any],
) -> None:
    normalized = normalize_smart_gateway_settings(settings)
    target_port = normalized["port"]
    port_changed = safe_int(proxy.listen_port, 0) != target_port
    if proxy.is_alive() and port_changed:
        proxy.stop()
    proxy.listen_port = target_port
    proxy.update_gateway_settings(
        strategy=normalized["strategy"],
        region=normalized["region"],
        query=normalized["query"],
    )
    proxy.capacity_limit = normalized["max_connections"]
    proxy.max_clients = min(
        normalized["max_connections"],
        proxy.security_snapshot()["max_connections"],
    )
    if not proxy.is_alive():
        proxy._stop = threading.Event()
        proxy._start_after_rebind()


def _reconcile_smart_gateway_after_persist_error(
    proxy: SmartGatewayProxy | None,
    fallback: dict[str, Any],
    persist_error: BaseException,
) -> None:
    state_read_error: BaseException | None = None
    try:
        with STATE_LOCK:
            state = load_state()
            persisted = normalize_smart_gateway_settings(
                (state.get("settings") or {}).get("smart_gateway")
            )
    except BaseException as read_error:
        persisted = normalize_smart_gateway_settings(fallback)
        state_read_error = read_error

    try:
        if persisted["enabled"]:
            if proxy is None:
                raise RuntimeError("缺少可恢复的智能网关实例")
            _apply_running_smart_gateway_settings(proxy, persisted)
            with SMART_GATEWAY_LOCK:
                SMART_GATEWAY_TABLE[SMART_GATEWAY_ID] = proxy
        else:
            if proxy is not None and proxy.is_alive():
                proxy.stop()
            with SMART_GATEWAY_LOCK:
                if proxy is None or SMART_GATEWAY_TABLE.get(SMART_GATEWAY_ID) is proxy:
                    SMART_GATEWAY_TABLE.pop(SMART_GATEWAY_ID, None)
    except BaseException as reconcile_error:
        raise RuntimeError(
            f"智能网关设置保存失败，且运行状态协调失败：{reconcile_error}"
        ) from persist_error
    if state_read_error is not None:
        raise RuntimeError(
            f"智能网关设置保存失败，状态文件不可读，已恢复调用前配置：{state_read_error}"
        ) from persist_error


def _update_running_smart_gateway(
    proxy: SmartGatewayProxy,
    desired: dict[str, Any],
    state: dict[str, Any],
) -> None:
    previous = _running_smart_gateway_settings(proxy)
    if desired["port"] != previous["port"]:
        conflict = _smart_gateway_port_conflict(
            state,
            desired["port"],
            proxy_listen_host(state),
            current_proxy=proxy,
        )
        if conflict:
            raise RuntimeError(conflict)
    try:
        _apply_running_smart_gateway_settings(proxy, desired)
    except BaseException as update_error:
        try:
            _apply_running_smart_gateway_settings(proxy, previous)
        except BaseException as rollback_error:
            raise RuntimeError(
                f"智能网关设置更新失败，且旧监听恢复失败：{rollback_error}"
            ) from update_error
        raise
    try:
        _persist_smart_gateway_settings(desired)
    except BaseException as persist_error:
        _reconcile_smart_gateway_after_persist_error(proxy, previous, persist_error)
        raise


def start_smart_gateway(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    if docker_mode():
        raise RuntimeError("Docker 模式未发布智能网关固定端口，请在宿主机模式下启用")
    raw_payload = payload if isinstance(payload, dict) else {}
    with BRIDGE_START_LOCK:
        with SHUTDOWN_LOCK:
            if SHUTTING_DOWN.is_set():
                raise RuntimeError("程序正在退出，不能启动智能网关")
            synchronize_process_state()
            with STATE_LOCK:
                state = load_state()
                previous = normalize_smart_gateway_settings(
                    (state.get("settings") or {}).get("smart_gateway")
                )
                desired = _smart_gateway_settings_from_payload(raw_payload, previous, enabled_default=True)
                desired["enabled"] = True
                listen_host = proxy_listen_host(state)
            with SMART_GATEWAY_LOCK:
                existing = SMART_GATEWAY_TABLE.get(SMART_GATEWAY_ID)
            if existing is not None and existing.is_alive():
                if safe_int(existing.listen_port, 0) != desired["port"]:
                    raise RuntimeError("智能网关已运行，请先停止后再修改端口")
                _update_running_smart_gateway(existing, desired, state)
                return smart_gateway_status_payload()
            conflict = _smart_gateway_port_conflict(state, desired["port"], listen_host)
            if conflict:
                raise RuntimeError(conflict)
            proxy = SmartGatewayProxy(
                listen_port=desired["port"],
                listen_host=listen_host,
                max_clients=desired["max_connections"],
                strategy=desired["strategy"],
                region=desired["region"],
                query=desired["query"],
                security=normalize_lan_security((state.get("settings") or {}).get("lan_security")),
            )
            proxy.start()
            with SMART_GATEWAY_LOCK:
                SMART_GATEWAY_TABLE[SMART_GATEWAY_ID] = proxy
            try:
                _persist_smart_gateway_settings(desired)
            except BaseException as persist_error:
                _reconcile_smart_gateway_after_persist_error(proxy, previous, persist_error)
                raise
            mark_sync_local_change()
            return smart_gateway_status_payload()


def _stop_smart_gateway_locked(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    with STATE_LOCK:
        state = load_state()
        current = normalize_smart_gateway_settings(
            (state.get("settings") or {}).get("smart_gateway")
        )
    desired = normalize_smart_gateway_settings(settings if settings is not None else current)
    desired["enabled"] = False
    desired["last_error"] = ""
    with SMART_GATEWAY_LOCK:
        proxy = SMART_GATEWAY_TABLE.get(SMART_GATEWAY_ID)
    fallback = _running_smart_gateway_settings(proxy) if proxy is not None and proxy.is_alive() else current
    if proxy is not None:
        proxy.stop()
        with SMART_GATEWAY_LOCK:
            if SMART_GATEWAY_TABLE.get(SMART_GATEWAY_ID) is proxy:
                SMART_GATEWAY_TABLE.pop(SMART_GATEWAY_ID, None)
    try:
        _persist_smart_gateway_settings(desired)
    except BaseException as persist_error:
        _reconcile_smart_gateway_after_persist_error(proxy, fallback, persist_error)
        raise
    mark_sync_local_change()
    return smart_gateway_status_payload()


def stop_smart_gateway() -> dict[str, Any]:
    with BRIDGE_START_LOCK:
        with SHUTDOWN_LOCK:
            return _stop_smart_gateway_locked()


def rebind_running_smart_gateway(listen_host: str) -> list[str]:
    with SMART_GATEWAY_LOCK:
        proxy = SMART_GATEWAY_TABLE.get(SMART_GATEWAY_ID)
    if proxy is None or not proxy.is_alive():
        return []
    old_host = str(proxy.listen_host)
    if old_host == listen_host and proxy.is_alive():
        return []
    proxy.rebind(listen_host)
    return [SMART_GATEWAY_ID]


def update_running_smart_gateway_security(
    security: dict[str, Any],
    *,
    disconnect_lan: bool,
) -> dict[str, int]:
    with SMART_GATEWAY_LOCK:
        proxy = SMART_GATEWAY_TABLE.get(SMART_GATEWAY_ID)
    if proxy is None:
        return {"updated_count": 0, "disconnected_count": 0}
    previous = normalize_lan_security(proxy.security_snapshot())
    normalized = normalize_lan_security(security)
    if previous == normalized:
        return {"updated_count": 0, "disconnected_count": 0}
    disconnected = proxy.update_security(normalized, disconnect_lan=disconnect_lan)
    return {"updated_count": 1, "disconnected_count": safe_int(disconnected, 0)}


def update_smart_gateway_settings(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("智能网关设置必须是对象")
    with STATE_LOCK:
        state = load_state()
        current = normalize_smart_gateway_settings((state.get("settings") or {}).get("smart_gateway"))
    desired = _smart_gateway_settings_from_payload(payload, current)
    enabled = desired["enabled"] if "enabled" in payload or isinstance(payload.get("settings"), dict) and "enabled" in payload.get("settings", {}) else current["enabled"]
    desired["enabled"] = enabled
    with SMART_GATEWAY_LOCK:
        running = SMART_GATEWAY_TABLE.get(SMART_GATEWAY_ID)
    if enabled and not (running and running.is_alive()):
        return start_smart_gateway(desired)
    if not enabled and running and running.is_alive():
        with BRIDGE_START_LOCK:
            with SHUTDOWN_LOCK:
                return _stop_smart_gateway_locked(desired)
    if running and running.is_alive():
        with BRIDGE_START_LOCK:
            with SHUTDOWN_LOCK:
                with STATE_LOCK:
                    state = load_state()
                _update_running_smart_gateway(running, desired, state)
        return smart_gateway_status_payload()
    _persist_smart_gateway_settings(desired)
    return smart_gateway_status_payload()


def choose_speed_test_target(settings: dict[str, Any] | None = None) -> str:
    global SPEED_TEST_TARGET_COUNTER
    if settings is None:
        with STATE_LOCK:
            state = load_state()
            settings = normalize_speed_test_settings((state.get("settings") or {}).get("speed_test"))
    else:
        settings = normalize_speed_test_settings(settings)
    targets = list(settings["targets"])
    if settings["rotation"] == "random":
        return random.choice(targets)
    with SPEED_TEST_TARGET_LOCK:
        target = targets[SPEED_TEST_TARGET_COUNTER % len(targets)]
        SPEED_TEST_TARGET_COUNTER += 1
    return target


def socks5_http_probe(
    local_port: int,
    target_url: str | None = None,
    timeout: float = 10.0,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    normalized_target = normalize_speed_test_target(target_url or choose_speed_test_target())
    parsed = urllib.parse.urlsplit(normalized_target)
    target_host = str(parsed.hostname or "")
    target_port = parsed.port or (443 if parsed.scheme == "https" else 80)
    path = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
    host_header = parsed.netloc
    started = time.monotonic()
    probe_done = threading.Event()
    transport_holder: list[socket.socket | None] = [None]

    def cancel_probe() -> None:
        if cancel_event is None:
            return
        while not probe_done.is_set():
            if cancel_event.wait(timeout=0.1):
                transport = transport_holder[0]
                if transport is not None:
                    try:
                        transport.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    transport.close()
                return

    with socket.create_connection(("127.0.0.1", local_port), timeout=timeout) as sock:
        transport_holder[0] = sock
        watcher: threading.Thread | None = None
        if cancel_event is not None:
            watcher = threading.Thread(target=cancel_probe, name="speed-test-cancel", daemon=True)
            watcher.start()
        try:
            sock.settimeout(timeout)
            sock.sendall(b"\x05\x01\x00")
            greeting = read_exact(sock, 2)
            if greeting != b"\x05\x00":
                raise RuntimeError("本地 SOCKS5 握手失败")

            host_bytes = target_host.encode("idna")
            if len(host_bytes) > 255:
                raise RuntimeError("探测域名过长")
            request = b"\x05\x01\x00\x03" + bytes([len(host_bytes)]) + host_bytes + target_port.to_bytes(2, "big")
            sock.sendall(request)
            reply = read_exact(sock, 4)
            if reply[0] != 5 or reply[1] != 0:
                raise RuntimeError(f"节点无法连接探测目标，SOCKS5 状态码 {reply[1]}")
            atyp = reply[3]
            if atyp == 1:
                read_exact(sock, 4)
            elif atyp == 3:
                size = read_exact(sock, 1)[0]
                read_exact(sock, size)
            elif atyp == 4:
                read_exact(sock, 16)
            else:
                raise RuntimeError("SOCKS5 返回了未知地址类型")
            read_exact(sock, 2)

            transport: socket.socket = sock
            if parsed.scheme == "https":
                transport = ssl.create_default_context().wrap_socket(sock, server_hostname=target_host)
                transport_holder[0] = transport
                transport.settimeout(timeout)
            http_request = (
                f"GET {path} HTTP/1.1\r\n"
                f"Host: {host_header}\r\n"
                "Connection: close\r\n"
                "User-Agent: ProxyBridge/0.2\r\n"
                "\r\n"
            ).encode("ascii")
            transport.sendall(http_request)
            response = b""
            while b"\r\n\r\n" not in response and len(response) < 4096:
                chunk = transport.recv(1024)
                if not chunk:
                    break
                response += chunk
            status_line = response.split(b"\r\n", 1)[0].decode("iso-8859-1", errors="replace")
            match = re.match(r"HTTP/\d(?:\.\d)?\s+(\d+)", status_line)
            if not match:
                raise RuntimeError("探测目标没有返回有效 HTTP 响应")
            status_code = int(match.group(1))
            elapsed_ms = int(round((time.monotonic() - started) * 1000))
            ok = 200 <= status_code < 400
            return {
                "ok": ok,
                "latency_ms": elapsed_ms,
                "target": normalized_target,
                "status_code": status_code,
                "message": "通信正常" if ok else f"HTTP {status_code}",
            }
        finally:
            probe_done.set()
            if watcher is not None:
                watcher.join(timeout=0.2)


def persist_test_result(result: dict[str, Any]) -> dict[str, Any]:
    node_id = str(result.get("node_id") or "")
    with STATE_LOCK:
        state = load_state()
        previous = state.setdefault("tests", {}).get(node_id)
        merged = merge_test_result(previous, result)
        state["tests"][node_id] = merged
        # 每节点保留最近 20 次测速；旧状态无历史时使用空列表，不改现有累计成功率。
        history = state.setdefault("test_history", {}).get(node_id)
        state["test_history"][node_id] = [dict(result), *(history if isinstance(history, list) else [])][:20]
        for bridge in state.get("bridges", []):
            if str(bridge.get("node_id") or "") == node_id:
                bridge["pool_health"] = update_pool_health_record(
                    bridge.get("pool_health"),
                    ok=bool(result.get("ok")),
                    latency_ms=result.get("latency_ms"),
                    error=result.get("message"),
                )
        save_state(state)
    return merged


def update_health_settings(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload.get("auto_switch"), bool):
        raise ValueError("auto_switch 必须是布尔值")
    with STATE_LOCK:
        state = load_state()
        state["settings"]["health"] = {"auto_switch": payload["auto_switch"]}
        save_state(state)
    return {"ok": True, "settings": dict(state["settings"]["health"])}


def check_running_bridges() -> int:
    with STATE_LOCK:
        state = load_state()
        bridges = [dict(item) for item in state["bridges"]
                   if item.get("status") == "running" or (
                       item.get("status") == "stopped" and item.get("enabled")
                       and (item.get("health") or {}).get("status") == "error")]
        settings = normalize_speed_test_settings(state["settings"].get("speed_test"))
    if not bridges or SHUTTING_DOWN.is_set():
        return 0

    def probe(bridge: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        if bridge.get("status") == "stopped":
            return bridge, {"ok": False, "message": "代理核心或监听器意外退出"}
        try:
            # 核心端口仅监听回环且无 LAN 认证，不把用户令牌加入探测或日志。
            result = socks5_http_probe(safe_int(bridge.get("core_port"), 0),
                                       target_url=choose_speed_test_target(settings), timeout=8.0,
                                       cancel_event=SHUTTING_DOWN)
        except Exception as exc:
            result = {"ok": False, "message": str(exc)[:500]}
        return bridge, result

    checked = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=3, thread_name_prefix="bridge-health") as executor:
        futures = [executor.submit(probe, bridge) for bridge in bridges]
        for future in concurrent.futures.as_completed(futures):
            before, result = future.result()
            if SHUTTING_DOWN.is_set():
                break
            with STATE_LOCK:
                state = load_state()
                bridge = next((item for item in state["bridges"] if item.get("id") == before.get("id")), None)
                if not bridge or bridge.get("status") != before.get("status") or any(
                    bridge.get(key) != before.get(key) for key in ("started_at", "pid", "core_port")
                ):
                    continue
                previous_status = (bridge.get("health") or {}).get("status")
                bridge["health"] = {"status": "ok" if result.get("ok") else "error",
                                    "checked_at": now_ts(),
                                    "error": "" if result.get("ok") else str(result.get("message") or "探活失败")}
                bridge["pool_health"] = update_pool_health_record(
                    bridge.get("pool_health"), ok=bool(result.get("ok")),
                    latency_ms=result.get("latency_ms"), error=result.get("message"))
                save_state(state)
                checked += 1
                failed = not result.get("ok")
            if failed:
                if previous_status != "error":
                    log_event(f"运行中代理「{before.get('node_name', '')}」探活失败：{result.get('message', '')}",
                              level="error", category="探活")
                maybe_start_health_replacement(before)
            elif result.get("ok") and previous_status == "error":
                log_event(f"运行中代理「{before.get('node_name', '')}」已恢复", category="探活")
    return checked


def health_switch_allowed(state: dict[str, Any], source_id: str, target_id: str) -> bool:
    if not state["settings"]["health"]["auto_switch"] or source_id == target_id:
        return False
    if any(item.get("node_id") in {source_id, target_id} for item in state["browser_instances"]):
        return False
    by_id = {str(item.get("id") or ""): item for item in state["nodes"]}
    source, target = by_id.get(source_id), by_id.get(target_id)
    if not source or not target or source.get("region") in {None, "", "未分类"} or source.get("region") != target.get("region"):
        return False
    result = public_test_results(state).get(target_id) or {}
    return bool(result.get("ok") and not result.get("stale"))


def maybe_start_health_replacement(failed_bridge: dict[str, Any]) -> None:
    node_id = str(failed_bridge.get("node_id") or "")
    with STATE_LOCK:
        state = load_state()
        if not state["settings"]["health"]["auto_switch"]:
            return
        # 即使浏览器暂时关闭，也保留它的节点出口绑定。
        if any(item.get("node_id") == node_id for item in state["browser_instances"]):
            return
        node = next((item for item in state["nodes"] if item.get("id") == node_id), None)
        if not node or node.get("region") in {None, "", "未分类"}:
            return
        tests = public_test_results(state)
        candidates = [item for item in state["nodes"] if item.get("id") != node_id
                      and item.get("region") == node["region"]
                      and tests.get(item.get("id"), {}).get("ok")
                      and not tests.get(item.get("id"), {}).get("stale")
                      and active_bridge_for_node(state, str(item.get("id") or "")) is None
                      and not any(browser.get("node_id") == item.get("id") for browser in state["browser_instances"])]
        candidates.sort(key=lambda item: tests[item["id"]].get("latency_ms") or float("inf"))
    if not candidates or SHUTTING_DOWN.is_set():
        return
    target = candidates[0]
    try:
        # 下载预检在停旧端口之前完成；切换失败尝试恢复原节点和原端口。
        ensure_core_installed(str(target.get("preferred_core") or "sing-box"))
        with BROWSER_LAUNCH_LOCK:
            with BRIDGE_START_LOCK:
                with STATE_LOCK:
                    state = load_state()
                    current = next((item for item in state["bridges"] if item.get("id") == failed_bridge.get("id")), None)
                    if not current or current.get("status") != failed_bridge.get("status") or any(
                        current.get(key) != failed_bridge.get(key) for key in ("started_at", "pid", "core_port")
                    ):
                        return
                    if current.get("status") == "stopped" and not current.get("enabled"):
                        return
                    if not health_switch_allowed(state, node_id, target["id"]) or active_bridge_for_node(state, target["id"]) is not None:
                        return
                    if (current.get("health") or {}).get("status") != "error":
                        return
                    port = safe_int(current.get("port"), 0)
                stop_bridge(str(failed_bridge["id"]))
                try:
                    replacement = start_bridge({"node_id": target["id"], "core": "auto", "port": port,
                                                "_require_exact_port": True, "_health_switch_from": node_id})
                except Exception:
                    if not SHUTTING_DOWN.is_set():
                        start_bridge({"node_id": node_id, "core": "auto", "port": port, "_require_exact_port": True})
                    raise
        log_event(f"端口 :{port} 已切换至同地区节点「{replacement.get('node_name', '')}」，后续连接使用新出口",
                  level="warn", category="探活")
    except Exception as exc:
        log_event(f"自动切换失败：{exc}", level="error", category="探活")


def start_bridge_health_monitor() -> None:
    global BRIDGE_HEALTH_THREAD
    with BRIDGE_HEALTH_LOCK:
        if BRIDGE_HEALTH_THREAD is not None and BRIDGE_HEALTH_THREAD.is_alive():
            return

        def monitor() -> None:
            while not SHUTTING_DOWN.wait(BRIDGE_HEALTH_INTERVAL):
                try:
                    check_running_bridges()
                except Exception as exc:
                    log_event(f"运行探活异常：{exc}", level="warn", category="探活")

        BRIDGE_HEALTH_THREAD = threading.Thread(target=monitor, name="bridge-health-monitor", daemon=True)
        BRIDGE_HEALTH_THREAD.start()


def test_node(
    payload: dict[str, Any],
    *,
    cancel_event: threading.Event | None = None,
    persist_result: bool = True,
) -> dict[str, Any]:
    node_id = str(payload.get("node_id") or "")
    requested_core = str(payload.get("core") or "auto")
    if requested_core not in {"auto", "sing-box", "xray"}:
        raise ValueError("核心只能是 sing-box、xray 或 auto")
    process: subprocess.Popen | None = None
    test_id = ""
    log_path: Path | None = None
    result: dict[str, Any]
    while True:
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError("测速已取消")
        with SHUTDOWN_LOCK:
            if SHUTTING_DOWN.is_set():
                raise RuntimeError("程序正在退出，不能启动新的节点测试")
            with STATE_LOCK:
                state = load_state()
                node = dict(find_node(state, node_id))
                core = node.get("preferred_core") if requested_core == "auto" else requested_core
                if core not in {"sing-box", "xray"}:
                    raise ValueError("核心只能是 sing-box、xray 或 auto")

        # Core discovery/download is slow and must not block state reads or shutdown.
        placeholder = RUNTIME_ROOT / f".{core}-test-preflight.json"
        prepared_command = core_command(core, placeholder)

        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError("测速已取消")
        with SHUTDOWN_LOCK:
            if SHUTTING_DOWN.is_set():
                raise RuntimeError("程序正在退出，不能启动新的节点测试")
            with STATE_LOCK:
                state = load_state()
                node = dict(find_node(state, node_id))
                desired_core = node.get("preferred_core") if requested_core == "auto" else requested_core
            if desired_core != core:
                continue
            local_port = next_free_port(22080)
            test_id = stable_id("test", node_id, str(time.time_ns()), str(local_port))
            config = generate_config(core, node, local_port)
            config_path = RUNTIME_ROOT / f"{test_id}.json"
            log_path = RUNTIME_ROOT / f"{test_id}.log"
            _write_private_atomic(
                config_path,
                json.dumps(config, ensure_ascii=False, indent=2).encode("utf-8"),
                temp_path=config_path.with_suffix(config_path.suffix + ".tmp"),
            )
            command = _retarget_core_command(prepared_command, core, config_path)
            rotate_runtime_log(log_path)
            log_file = log_path.open("ab")
            try:
                if hasattr(os, "fchmod"):
                    try:
                        os.fchmod(log_file.fileno(), 0o600)
                    except OSError:
                        pass
                # 与本地核心上传共用安装锁：上传事务完成前不启动测速进程；一旦
                # 进程登记完成，上传侧会检测 TEST_PROCESS_TABLE 并要求等待测速结束。
                with CORE_INSTALL_LOCK:
                    process = subprocess.Popen(
                        command,
                        cwd=str(APP_ROOT),
                        stdout=log_file,
                        stderr=subprocess.STDOUT,
                        **_popen_kwargs(),
                    )
                    with PROCESS_LOCK:
                        TEST_PROCESS_TABLE[test_id] = process
            finally:
                log_file.close()
            break

    try:
        port_ready = False
        port_deadline = time.monotonic() + 3.0
        while time.monotonic() < port_deadline:
            if cancel_event is not None and cancel_event.is_set():
                raise RuntimeError("测速已取消")
            if wait_for_port(local_port, timeout=min(0.25, port_deadline - time.monotonic())):
                port_ready = True
                break
        if not port_ready:
            if process.poll() is not None:
                raise RuntimeError(read_tail(log_path) or f"{core} 启动失败")
            raise RuntimeError("本地 SOCKS5 测试端口启动超时")
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError("测速已取消")
        target_url = str(payload.get("target_url") or "").strip()
        if cancel_event is None:
            probe = socks5_http_probe(local_port, target_url) if target_url else socks5_http_probe(local_port)
        else:
            probe = (
                socks5_http_probe(local_port, target_url, cancel_event=cancel_event)
                if target_url
                else socks5_http_probe(local_port, cancel_event=cancel_event)
            )
        result = {
            **probe,
            "node_id": node_id,
            "node_name": node.get("name", ""),
            "core": core,
            "tested_at": now_ts(),
        }
    except Exception as exc:
        result = {
            "ok": False,
            "node_id": node_id,
            "node_name": node.get("name", ""),
            "core": core,
            "latency_ms": None,
            "status_code": None,
            "target": str(payload.get("target_url") or "") or "",
            "message": str(exc),
            "tested_at": now_ts(),
        }
    finally:
        if process is not None:
            with SHUTDOWN_LOCK:
                if not SHUTTING_DOWN.is_set():
                    with PROCESS_LOCK:
                        owned = TEST_PROCESS_TABLE.get(test_id) is process
                    if owned:
                        terminate_process(process)
                        with PROCESS_LOCK:
                            if TEST_PROCESS_TABLE.get(test_id) is process:
                                TEST_PROCESS_TABLE.pop(test_id, None)

    with SHUTDOWN_LOCK:
        if SHUTTING_DOWN.is_set():
            return result
        if cancel_event is not None and cancel_event.is_set():
            return {**result, "cancelled": True, "message": "测速已取消"}
        if persist_result:
            result = persist_test_result(result)
    return result


def speed_test_task_payload(task_id: str | None = None) -> dict[str, Any]:
    with SPEED_TEST_TASK_LOCK:
        if task_id:
            task = SPEED_TEST_TASKS.get(task_id)
            if task is None:
                raise ValueError("测速任务不存在")
            return json.loads(json.dumps(task, ensure_ascii=False))
        tasks = list(SPEED_TEST_TASKS.values())
        return {"tasks": json.loads(json.dumps(tasks, ensure_ascii=False))}


def _update_speed_test_task(task_id: str, **updates: Any) -> None:
    with SPEED_TEST_TASK_LOCK:
        task = SPEED_TEST_TASKS.get(task_id)
        if task is not None:
            task.update(updates)
            task["updated_at"] = now_ts()


def _run_speed_test_task(task_id: str, node_ids: list[str], concurrency: int, retries: int) -> None:
    cancel_event = SPEED_TEST_CANCEL_EVENTS[task_id]

    def run_one(index: int, node_id: str) -> dict[str, Any]:
        attempts = 0
        result: dict[str, Any] = {"ok": False, "node_id": node_id, "message": "测速未开始"}
        while attempts <= retries and not cancel_event.is_set():
            attempts += 1
            try:
                result = test_node(
                    {"node_id": node_id, "core": "auto"},
                    cancel_event=cancel_event,
                    persist_result=False,
                )
            except Exception as exc:
                result = {"ok": False, "node_id": node_id, "message": str(exc), "tested_at": now_ts()}
            if result.get("ok") or cancel_event.is_set():
                break
        if not cancel_event.is_set():
            result = persist_test_result(result)
        result = dict(result)
        result["attempts"] = attempts
        with SPEED_TEST_TASK_LOCK:
            task = SPEED_TEST_TASKS.get(task_id)
            if task is not None:
                task["items"][index] = result
                task["completed"] = sum(1 for item in task["items"] if item is not None)
                task["updated_at"] = now_ts()
        return result

    _update_speed_test_task(
        task_id,
        status="cancelling" if cancel_event.is_set() else "running",
        started_at=now_ts(),
    )
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="speed-test") as executor:
            pending: dict[concurrent.futures.Future[dict[str, Any]], int] = {}
            next_index = 0
            while next_index < len(node_ids) or pending:
                while not cancel_event.is_set() and next_index < len(node_ids) and len(pending) < concurrency:
                    future = executor.submit(run_one, next_index, node_ids[next_index])
                    pending[future] = next_index
                    next_index += 1
                if not pending:
                    break
                done, _ = concurrent.futures.wait(
                    pending,
                    timeout=0.2,
                    return_when=concurrent.futures.FIRST_COMPLETED,
                )
                for future in done:
                    pending.pop(future, None)
                    try:
                        future.result()
                    except Exception:
                        pass
            if cancel_event.is_set():
                with SPEED_TEST_TASK_LOCK:
                    task = SPEED_TEST_TASKS.get(task_id)
                    if task is not None:
                        for index, item in enumerate(task["items"]):
                            if item is None:
                                task["items"][index] = {
                                    "ok": False,
                                    "node_id": node_ids[index],
                                    "message": "测速已取消",
                                    "cancelled": True,
                                }
                        task["completed"] = len(node_ids)
        _update_speed_test_task(
            task_id,
            status="cancelled" if cancel_event.is_set() else "completed",
            finished_at=now_ts(),
        )
    finally:
        with SPEED_TEST_TASK_LOCK:
            SPEED_TEST_CANCEL_EVENTS.pop(task_id, None)


def start_speed_test_task(payload: dict[str, Any]) -> dict[str, Any]:
    raw_node_ids = payload.get("node_ids")
    if not isinstance(raw_node_ids, list):
        raise ValueError("node_ids 必须是数组")
    node_ids = list(dict.fromkeys(str(item or "") for item in raw_node_ids if str(item or "")))
    if not node_ids:
        raise ValueError("没有可测试节点")
    if len(node_ids) > SPEED_TEST_BATCH_NODE_LIMIT:
        raise ValueError(f"单次最多测试 {SPEED_TEST_BATCH_NODE_LIMIT} 个节点")
    with STATE_LOCK:
        state = load_state()
        known_ids = {str(node.get("id") or "") for node in state.get("nodes", [])}
        unknown = [node_id for node_id in node_ids if node_id not in known_ids]
        settings = speed_test_settings_payload(state)
    if unknown:
        raise ValueError(f"节点不存在: {unknown[0]}")
    concurrency = clamp_int(
        payload.get("concurrency"),
        settings["batch_concurrency"],
        SPEED_TEST_BATCH_MIN_CONCURRENCY,
        SPEED_TEST_BATCH_MAX_CONCURRENCY,
    )
    retries = clamp_int(payload.get("retry_failures"), settings["retry_failures"], 0, 2)
    task_id = stable_id("speed-test-task", str(time.time_ns()), *node_ids)
    task = {
        "id": task_id,
        "status": "queued",
        "node_ids": node_ids,
        "total": len(node_ids),
        "completed": 0,
        "concurrency": concurrency,
        "retry_failures": retries,
        "items": [None] * len(node_ids),
        "created_at": now_ts(),
        "updated_at": now_ts(),
    }
    cancel_event = threading.Event()
    with SPEED_TEST_TASK_LOCK:
        for existing in SPEED_TEST_TASKS.values():
            if existing.get("status") in {"queued", "running", "cancelling"}:
                raise RuntimeError("已有批量测速任务正在运行")
        SPEED_TEST_TASKS[task_id] = task
        SPEED_TEST_CANCEL_EVENTS[task_id] = cancel_event
        while len(SPEED_TEST_TASKS) > SPEED_TEST_BATCH_TASK_LIMIT:
            SPEED_TEST_TASKS.popitem(last=False)
    threading.Thread(
        target=_run_speed_test_task,
        args=(task_id, node_ids, concurrency, retries),
        name=f"speed-test-task-{task_id[:8]}",
        daemon=True,
    ).start()
    return speed_test_task_payload(task_id)


def cancel_speed_test_task(task_id: str) -> dict[str, Any]:
    with SPEED_TEST_TASK_LOCK:
        task = SPEED_TEST_TASKS.get(task_id)
        if task is None:
            raise ValueError("测速任务不存在")
        cancel_event = SPEED_TEST_CANCEL_EVENTS.get(task_id)
        if cancel_event is not None:
            cancel_event.set()
        if task.get("status") in {"queued", "running"}:
            task["status"] = "cancelling"
            task["updated_at"] = now_ts()
    return speed_test_task_payload(task_id)


def stop_bridge(bridge_id: str) -> dict[str, Any]:
    if remote_browser_feature_enabled():
        # Share the launch barrier across the browser snapshot and bridge stop so a
        # worker cannot be created between those two phases and lose its proxy.
        with BROWSER_LAUNCH_LOCK:
            with SHUTDOWN_LOCK:
                if SHUTTING_DOWN.is_set():
                    raise RuntimeError("程序正在退出，不能停止本地代理")
            with STATE_LOCK:
                state = load_state()
                bridge_node_id = next(
                    (
                        str(bridge.get("node_id") or "")
                        for bridge in state.get("bridges", [])
                        if bridge.get("id") == bridge_id
                    ),
                    "",
                )
            killed = (
                safe_int(_stop_remote_browser({"node_id": bridge_node_id}).get("killed_browsers"), 0)
                if bridge_node_id
                else 0
            )
            with SHUTDOWN_LOCK:
                if SHUTTING_DOWN.is_set():
                    raise RuntimeError("程序正在退出，本地代理将由关机流程停止")
                return _stop_bridge(bridge_id, browsers_already_stopped=True, killed_browsers=killed)
    with SHUTDOWN_LOCK:
        return _stop_bridge(bridge_id)


def _stop_bridge(
    bridge_id: str,
    *,
    browsers_already_stopped: bool = False,
    killed_browsers: int = 0,
) -> dict[str, Any]:
    with STATE_LOCK:
        state = load_state()
        bridge_node_id = ""
        for bridge in state["bridges"]:
            if bridge.get("id") == bridge_id:
                bridge_node_id = str(bridge.get("node_id") or "")
                break
        browser_instance_ids = [
            str(instance.get("id") or "")
            for instance in state.get("browser_instances", [])
            if str(instance.get("node_id") or "") == bridge_node_id
            and instance.get("id")
            and (
                instance.get("status") in ACTIVE_BROWSER_STATUSES
                or instance.get("pid")
                or (
                    not remote_browser_feature_enabled()
                    and browser_process_is_active(str(instance.get("id") or ""))
                )
            )
        ]

    if not browsers_already_stopped:
        killed_browsers = (
            stop_browser_processes_for_node(bridge_node_id)
            if bridge_node_id and browser_feature_enabled()
            else 0
        )
    # stop_browser_processes_for_node() has persisted the stopped state. Queue the
    # same WebDAV upload used by the dedicated browser-stop path before a later
    # records-modal pull can replace local state with an older remote snapshot.
    maybe_auto_upload_browser_instances(browser_instance_ids)
    traffic = stop_process(bridge_id, pop_traffic=False)
    persisted_traffic = persist_stopped_bridges({bridge_id: traffic}, disable=True)
    release_persisted_bridge_traffic(persisted_traffic)
    return {"ok": True, "killed_browsers": killed_browsers}


def stop_all_bridges() -> dict[str, Any]:
    if remote_browser_feature_enabled():
        # Keep remote browser discovery/stop and bridge shutdown in one launch barrier;
        # otherwise a new worker can appear after the all-instances snapshot.
        with BROWSER_LAUNCH_LOCK:
            with SHUTDOWN_LOCK:
                if SHUTTING_DOWN.is_set():
                    raise RuntimeError("程序正在退出，不能停止全部代理")
            killed = safe_int(
                _stop_remote_browser({"all_instances": True}).get("killed_browsers"),
                0,
            )
            with BRIDGE_START_LOCK:
                with SHUTDOWN_LOCK:
                    if SHUTTING_DOWN.is_set():
                        raise RuntimeError("程序正在退出，代理将由关机流程停止")
                    return _stop_all_bridges(
                        browsers_already_stopped=True,
                        killed_browsers=killed,
                    )
    with BRIDGE_START_LOCK:
        with SHUTDOWN_LOCK:
            return _stop_all_bridges()


def _stop_all_bridges(
    *,
    browsers_already_stopped: bool = False,
    killed_browsers: int = 0,
) -> dict[str, Any]:
    with STATE_LOCK:
        state = load_state()
        active_bridge_ids = {
            str(bridge.get("id") or "")
            for bridge in state.get("bridges", [])
            if bridge.get("id")
            and (bridge.get("status") in ACTIVE_BRIDGE_STATUSES or bridge.get("enabled"))
        }
        browser_node_ids = {
            str(instance.get("node_id") or "")
            for instance in state.get("browser_instances", [])
            if str(instance.get("node_id") or "")
        }
        browser_instance_ids = [
            str(instance.get("id") or "")
            for instance in state.get("browser_instances", [])
            if instance.get("id")
            and (
                instance.get("status") in ACTIVE_BROWSER_STATUSES
                or instance.get("pid")
                or (
                    not remote_browser_feature_enabled()
                    and browser_process_is_active(str(instance.get("id") or ""))
                )
            )
        ]
        gateway_enabled = normalize_smart_gateway_settings(
            (state.get("settings") or {}).get("smart_gateway")
        )["enabled"]

    with SMART_GATEWAY_LOCK:
        gateway_present = SMART_GATEWAY_TABLE.get(SMART_GATEWAY_ID) is not None

    with PROCESS_LOCK:
        browser_node_ids.update(BROWSER_TABLE.keys())
    if not browsers_already_stopped:
        killed_browsers = (
            sum(stop_browser_processes_for_node(node_id) for node_id in browser_node_ids)
            if browser_feature_enabled()
            else 0
        )
    maybe_auto_upload_browser_instances(browser_instance_ids)
    with PROCESS_LOCK:
        bridge_ids = active_bridge_ids | set(PROCESS_TABLE) | set(LOG_PROXY_TABLE)
    with TRAFFIC_LOCK:
        bridge_ids.update(TRAFFIC_TABLE)
    failures: list[tuple[str, BaseException]] = []
    gateway_stopped = False
    if gateway_enabled or gateway_present:
        try:
            _stop_smart_gateway_locked()
            gateway_stopped = True
        except BaseException as error:
            failures.append(("智能网关", error))
    traffic_by_bridge: dict[str, dict[str, int]] = {}
    for bridge_id in sorted(bridge_ids):
        try:
            traffic_by_bridge[bridge_id] = stop_process(bridge_id, pop_traffic=False)
        except BaseException as error:
            failures.append((bridge_id, error))

    persisted_traffic: dict[str, dict[str, int]] = {}
    if traffic_by_bridge:
        try:
            persisted_traffic = persist_stopped_bridges(traffic_by_bridge, disable=True)
        except BaseException as error:
            failures.append(("状态保存", error))
        else:
            release_persisted_bridge_traffic(persisted_traffic)

    if failures:
        details = "; ".join(f"{label}: {error}" for label, error in failures)
        raise RuntimeError(f"停止部分本地代理失败（{len(failures)} 项）: {details}") from failures[0][1]
    return {
        "ok": True,
        "killed_browsers": killed_browsers,
        "gateway_stopped": gateway_stopped,
    }


def rebind_running_log_proxies(listen_host: str) -> list[str]:
    """Rebind every active public proxy listener as one best-effort transaction."""
    with PROCESS_LOCK:
        proxies = list(LOG_PROXY_TABLE.items())
    changed: list[tuple[str, Socks5LogProxy, str]] = []
    current: tuple[str, Socks5LogProxy, str] | None = None
    try:
        for bridge_id, proxy in proxies:
            old_host = str(getattr(proxy, "listen_host", LOCAL_PROXY_LISTEN_HOST))
            if old_host == listen_host and proxy.is_alive():
                continue
            current = (bridge_id, proxy, old_host)
            proxy.rebind(listen_host)
            changed.append(current)
            current = None
    except BaseException as rebind_error:
        rollback_failures: list[str] = []
        rollback_items = list(reversed(changed))
        if current is not None:
            current_proxy = current[1]
            host_changed = str(getattr(current_proxy, "listen_host", "")) != current[2]
            if host_changed or not current_proxy.is_alive():
                rollback_items.insert(0, current)
        for bridge_id, proxy, old_host in rollback_items:
            try:
                proxy.rebind(old_host)
            except BaseException as rollback_error:
                rollback_failures.append(f"{bridge_id}: {rollback_error}")
        if rollback_failures:
            raise RuntimeError(
                "局域网共享切换失败，且部分代理监听恢复失败：" + "; ".join(rollback_failures)
            ) from rebind_error
        raise RuntimeError(f"局域网共享切换失败：{rebind_error}") from rebind_error
    return [bridge_id for bridge_id, _proxy, _old_host in changed]


def update_running_log_proxy_security(
    security: dict[str, Any],
    *,
    disconnect_lan: bool,
) -> dict[str, int]:
    normalized = normalize_lan_security(security)
    with PROCESS_LOCK:
        proxies = list(LOG_PROXY_TABLE.items())
    changed: list[tuple[str, Socks5LogProxy, dict[str, Any]]] = []
    disconnected = 0
    try:
        for bridge_id, proxy in proxies:
            snapshot_method = getattr(proxy, "security_snapshot", None)
            update_method = getattr(proxy, "update_security", None)
            if not callable(snapshot_method) or not callable(update_method):
                continue
            previous = normalize_lan_security(snapshot_method())
            if previous == normalized:
                continue
            disconnected += safe_int(update_method(normalized, disconnect_lan=disconnect_lan), 0)
            changed.append((bridge_id, proxy, previous))
    except BaseException as update_error:
        rollback_failures: list[str] = []
        for bridge_id, proxy, previous in reversed(changed):
            try:
                proxy.update_security(previous, disconnect_lan=True)
            except BaseException as rollback_error:
                rollback_failures.append(f"{bridge_id}: {rollback_error}")
        if rollback_failures:
            raise RuntimeError(
                "LAN 安全策略更新失败，且部分代理恢复失败：" + "; ".join(rollback_failures)
            ) from update_error
        raise RuntimeError(f"LAN 安全策略更新失败：{update_error}") from update_error
    return {"updated_count": len(changed), "disconnected_count": disconnected}


def update_network_settings(payload: dict[str, Any]) -> dict[str, Any]:
    if "allow_lan" not in payload or not isinstance(payload.get("allow_lan"), bool):
        raise ValueError("allow_lan 必须是布尔值")
    desired = bool(payload["allow_lan"])
    if docker_mode() and not desired:
        raise ValueError("Docker 模式下代理监听固定开启，访问范围由端口映射管理")
    if docker_mode():
        desired = True
    desired_host = LAN_PROXY_LISTEN_HOST if desired else LOCAL_PROXY_LISTEN_HOST
    lan_addresses = lan_ipv4_addresses()

    with STATE_LOCK:
        current_state = load_state()
        current_security = normalize_lan_security((current_state.get("settings") or {}).get("lan_security"))
        was_enabled = allow_lan_access(current_state)
    generated_token = ""
    # 新开启共享且未显式选择认证时默认 Token；既有共享和显式认证方式保持兼容。
    security_payload = payload.get("lan_security")
    if "lan_security" in payload and not isinstance(security_payload, dict):
        raise ValueError("lan_security 必须是对象")
    needs_token = security_payload is None or "auth_mode" not in security_payload or (
        security_payload.get("auth_mode") == "token" and not security_payload.get("token") and not current_security["token"])
    explicit_unconfigured_token = (
        isinstance(security_payload, dict) and security_payload.get("auth_mode") == "token"
        and not security_payload.get("token") and not current_security["token"]
    )
    if desired and ((not was_enabled and needs_token) or explicit_unconfigured_token):
        generated_token = secrets.token_urlsafe(24)
        security_payload = {**(security_payload if isinstance(security_payload, dict) else {}),
                            "auth_mode": "token", "token": generated_token}
    desired_security = (
        validate_lan_security(security_payload, current_security)
        if security_payload is not None
        else current_security
    )

    # 与 start_bridge 使用相同锁顺序，避免设置切换与新监听器启动交错。
    with BRIDGE_START_LOCK:
        with SHUTDOWN_LOCK:
            if SHUTTING_DOWN.is_set():
                raise RuntimeError("程序正在退出，不能修改局域网共享设置")
            synchronize_process_state()
            rebound_ids: list[str] = []
            rebound_gateway_ids: list[str] = []
            try:
                rebound_ids = rebind_running_log_proxies(desired_host)
                rebound_gateway_ids = rebind_running_smart_gateway(desired_host)
                security_result = update_running_log_proxy_security(
                    desired_security,
                    disconnect_lan=desired_security != current_security,
                )
                gateway_security_result = update_running_smart_gateway_security(
                    desired_security,
                    disconnect_lan=desired_security != current_security,
                )
                with STATE_LOCK:
                    state = load_state()
                    settings = state.setdefault("settings", {})
                    settings["allow_lan"] = desired
                    settings["lan_security"] = desired_security
                    for bridge in state.get("bridges", []):
                        bridge["listen_host"] = desired_host
                    save_state(state)
                    network = network_settings_payload(state, lan_addresses=lan_addresses)
            except BaseException as update_error:
                persisted_allow_lan: bool | None = None
                persisted_security = normalize_lan_security({})
                state_read_error: BaseException | None = None
                try:
                    with STATE_LOCK:
                        persisted_state = load_state()
                        persisted_allow_lan = allow_lan_access(persisted_state)
                        persisted_security = normalize_lan_security(
                            (persisted_state.get("settings") or {}).get("lan_security")
                        )
                except BaseException as read_error:
                    state_read_error = read_error
                # save_state may fail after the atomic replace has already committed.
                # Reconcile listeners to what is actually readable on disk; if the
                # state cannot be read, fail closed to loopback rather than expose LAN.
                reconciled_host = (
                    LAN_PROXY_LISTEN_HOST
                    if persisted_allow_lan is True
                    else LOCAL_PROXY_LISTEN_HOST
                )
                try:
                    rebind_running_log_proxies(reconciled_host)
                    rebind_running_smart_gateway(reconciled_host)
                    update_running_log_proxy_security(persisted_security, disconnect_lan=True)
                    update_running_smart_gateway_security(persisted_security, disconnect_lan=True)
                except BaseException as reconcile_error:
                    raise RuntimeError(
                        f"设置更新失败，且代理监听与磁盘状态协调失败：{reconcile_error}"
                    ) from update_error
                if state_read_error is not None:
                    raise RuntimeError(
                        f"设置更新失败，状态文件不可读，代理已收紧为仅本机：{state_read_error}"
                    ) from update_error
                raise

    return {
        "ok": True,
        **({"generated_token": generated_token} if generated_token else {}),
        "network": network,
        "rebound_count": len(rebound_ids) + len(rebound_gateway_ids),
        "connections_interrupted": bool(rebound_ids or rebound_gateway_ids),
        "security_updated_count": security_result["updated_count"] + gateway_security_result["updated_count"],
        "disconnected_count": security_result["disconnected_count"] + gateway_security_result["disconnected_count"],
    }


def apply_updates(target: dict[str, Any], updates: dict[str, Any]) -> bool:
    changed = False
    for key, value in updates.items():
        if target.get(key) != value:
            target[key] = value
            changed = True
    return changed


def bridge_runtime_is_active(bridge_id: str) -> bool:
    with PROCESS_LOCK:
        process = PROCESS_TABLE.get(bridge_id)
        proxy = LOG_PROXY_TABLE.get(bridge_id)
    return bool(process and process.poll() is None and proxy and proxy.is_alive())


def synchronize_process_state() -> None:
    with SHUTDOWN_LOCK:
        with STATE_LOCK:
            state = load_state()
            candidates = [
                str(bridge.get("id") or "")
                for bridge in state.get("bridges", [])
                if bridge.get("status") in ACTIVE_BRIDGE_STATUSES and bridge.get("id")
            ]

        cleanup_ids = [bridge_id for bridge_id in candidates if not bridge_runtime_is_active(bridge_id)]
        if not cleanup_ids:
            return

        traffic_by_bridge: dict[str, dict[str, int]] = {}
        failures: list[tuple[str, BaseException]] = []
        for bridge_id in cleanup_ids:
            try:
                traffic_by_bridge[bridge_id] = stop_process(bridge_id, pop_traffic=False)
            except BaseException as error:
                failures.append((bridge_id, error))

        if traffic_by_bridge:
            try:
                persisted_traffic = persist_stopped_bridges(traffic_by_bridge, disable=False)
            except BaseException as error:
                failures.append(("状态保存", error))
            else:
                release_persisted_bridge_traffic(persisted_traffic)

        if failures:
            details = "; ".join(f"{label}: {error}" for label, error in failures)
            raise RuntimeError(f"同步部分本地代理状态失败（{len(failures)} 项）: {details}") from failures[0][1]


def sync_process_status(state: dict[str, Any]) -> bool:
    changed = False
    for bridge in state.get("bridges", []):
        bridge_id = str(bridge.get("id") or "")
        with PROCESS_LOCK:
            process = PROCESS_TABLE.get(bridge_id)
            proxy = LOG_PROXY_TABLE.get(bridge_id)
        if process and process.poll() is None and proxy and proxy.is_alive():
            changed |= apply_updates(
                bridge,
                {
                    "status": "running",
                    "enabled": True,
                    "pid": process.pid,
                    **bridge_traffic_snapshot(str(bridge_id)),
                },
            )
            if "error" in bridge:
                bridge.pop("error", None)
                changed = True
        # 不在这里发布 stopped：进程可能刚好在两阶段同步的快照之后退出。
        # 下一次 synchronize_process_state() 会先排空资源、持久化最终流量，再提交状态。
    return changed


def sync_browser_instance_status(
    state: dict[str, Any],
    *,
    remote_sessions_snapshot: Any = REMOTE_BROWSER_SESSIONS_UNSET,
) -> bool:
    """Reconcile browser instance status with live processes.

    Returns True if any state field changed. Instances that transition from
    running/starting → stopped (user closed/hidden until process exits) are
    queued for a one-shot WebDAV upload when auto-upload is enabled.
    """
    changed = False
    newly_stopped: list[str] = []
    stopped_at = now_ts()
    remote_sessions: dict[str, dict[str, Any]] | None = {}
    if remote_browser_feature_enabled():
        if remote_sessions_snapshot is REMOTE_BROWSER_SESSIONS_UNSET:
            remote_sessions, _remote_error = remote_browser_sessions()
        else:
            remote_sessions = remote_sessions_snapshot
    for instance in state.get("browser_instances", []):
        instance_id = str(instance.get("id") or "")
        if not instance_id:
            continue
        if remote_browser_feature_enabled() and (instance.get("remote_worker") is True or docker_mode()):
            # A broker outage is not evidence that a running worker stopped. Keep the
            # last known state until the broker is reachable again.
            if remote_sessions is None:
                continue
            session = remote_sessions.get(instance_id)
            if (
                instance.get("status") == "starting"
                and remote_browser_start_pending(instance_id)
            ):
                # Broker may already expose a container before launch_remote_browser()
                # validates health and commits the result. Keep the app-side transaction
                # in starting until that owner finishes or a stop invalidates it.
                continue
            if session and session.get("status") in REMOTE_BROWSER_LIVE_SESSION_STATUSES:
                current_status = str(instance.get("status") or "stopped")
                updates = {
                    "status": (
                        current_status
                        if current_status in {"running", "starting", "stopping"}
                        else "starting"
                    ),
                    "pid": None,
                    "remote_worker": True,
                    "remote_url": normalize_remote_browser_url(
                        session.get("url") or instance.get("remote_url") or ""
                    ),
                    "remote_container_id": normalize_remote_browser_container_id(
                        session.get("container_id") or instance.get("remote_container_id") or ""
                    ),
                    "remote_host_port": safe_int(
                        session.get("host_port"), safe_int(instance.get("remote_host_port"), 0)
                    )
                    or None,
                }
                changed |= apply_updates(instance, updates)
                if "error" in instance:
                    instance.pop("error", None)
                    changed = True
            elif instance.get("status") in ACTIVE_BROWSER_STATUSES:
                changed |= apply_updates(
                    instance,
                    {
                        "status": "stopped",
                        "pid": None,
                        "remote_url": "",
                        "remote_container_id": "",
                        "remote_host_port": None,
                        "last_stopped_at": stopped_at,
                        "last_used_at": stopped_at,
                    },
                )
                newly_stopped.append(instance_id)
            continue
        with PROCESS_LOCK:
            process = BROWSER_PROCESS_TABLE.get(instance_id)
        if process and process.poll() is None:
            changed |= apply_updates(instance, {"status": "running", "pid": process.pid})
        elif instance.get("status") in ACTIVE_BROWSER_STATUSES:
            if process is not None:
                with PROCESS_LOCK:
                    BROWSER_PROCESS_TABLE.pop(instance_id, None)
                    remove_browser_process_from_node_tables(process)
            changed |= apply_updates(
                instance,
                {
                    "status": "stopped",
                    "pid": None,
                    "last_stopped_at": stopped_at,
                    "last_used_at": stopped_at,
                },
            )
            newly_stopped.append(instance_id)
    if newly_stopped:
        # Defer upload outside STATE_LOCK: status_payload holds locks; schedule after return.
        maybe_auto_upload_browser_instances(newly_stopped)
    return changed


def copy_to_clipboard(text: str) -> dict[str, Any]:
    os_token = host_os_token()
    try:
        if os_token == "darwin" and shutil.which("pbcopy"):
            proc = subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=False)
            if proc.returncode == 0:
                return {"ok": True}
        elif os_token == "windows":
            # `clip` reads UTF-16LE text from stdin on modern Windows.
            proc = subprocess.run(
                ["clip"],
                input=text.encode("utf-16le"),
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if proc.returncode == 0:
                return {"ok": True}
        else:
            for command in (
                ["wl-copy"],
                ["xclip", "-selection", "clipboard"],
                ["xsel", "--clipboard", "--input"],
            ):
                if not shutil.which(command[0]):
                    continue
                proc = subprocess.run(command, input=text.encode("utf-8"), check=False)
                if proc.returncode == 0:
                    return {"ok": True}
    except OSError:
        pass
    return {"ok": False, "text": text}


def json_response(
    handler: BaseHTTPRequestHandler,
    payload: Any,
    status: int = 200,
    *,
    head_only: bool = False,
    no_store: bool = False,
) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    if no_store:
        handler.send_header("Cache-Control", "no-store")
    send_backend_instance_header(handler)
    handler.end_headers()
    if not head_only:
        handler.wfile.write(data)


def text_response(
    handler: BaseHTTPRequestHandler,
    body: str,
    status: int = 200,
    *,
    head_only: bool = False,
) -> None:
    data = body.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "text/plain; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    send_backend_instance_header(handler)
    handler.end_headers()
    if not head_only:
        handler.wfile.write(data)


def download_response(
    handler: BaseHTTPRequestHandler,
    data: bytes,
    content_type: str,
    filename: str,
) -> None:
    safe_filename = re.sub(r"[^A-Za-z0-9._-]", "_", filename)[:160] or "proxybridge-export"
    handler.send_response(200)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Content-Disposition", f'attachment; filename="{safe_filename}"')
    handler.send_header("X-Content-Type-Options", "nosniff")
    send_backend_instance_header(handler)
    handler.end_headers()
    handler.wfile.write(data)


def send_backend_instance_header(handler: BaseHTTPRequestHandler) -> None:
    token = management_api_token()
    headers = getattr(handler, "headers", None)
    values = headers.get_all(BACKEND_INSTANCE_HEADER, []) if headers is not None else []
    trusted_check = getattr(handler, "is_trusted_request", None)
    trusted = not callable(trusted_check) or bool(trusted_check())
    if trusted and len(values) == 1 and management_tokens_equal(str(values[0]), token):
        handler.send_header(BACKEND_INSTANCE_HEADER, token)


def error_response(handler: BaseHTTPRequestHandler, exc: Exception, status: int = 400) -> None:
    # 统一记录所有 API 错误到系统日志,便于事后排查失败的操作。
    # 脱敏 /pool/{token}/… 防止 token 写入 menubar-backend.log / 系统日志。
    safe_path = _redact_pool_token_in_log_arg(handler.path)
    safe_message = webdav_sync.redact_sensitive_text(exc)
    log_event(f"{handler.command} {safe_path} 失败:{safe_message}", level="error", category="接口")
    json_response(handler, {"error": safe_message}, status)


class AppHandler(BaseHTTPRequestHandler):
    server_version = f"ProxyBridge/{APP_VERSION}"

    def log_message(self, fmt: str, *args: Any) -> None:
        safe_args = tuple(
            webdav_sync.redact_sensitive_text(_redact_pool_token_in_log_arg(arg))
            if isinstance(arg, str)
            else arg
            for arg in args
        )
        sys.stdout.write("[%s] %s\n" % (self.log_date_time_string(), fmt % safe_args))

    def is_trusted_request(self) -> bool:
        request_host = self.headers.get("Host") or ""
        if not _is_trusted_console_host(request_host):
            return False
        if (self.headers.get("Sec-Fetch-Site") or "").strip().lower() == "cross-site":
            return False
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        if not origin or origin != origin.strip() or any(char in origin for char in "\r\n\t"):
            return False
        try:
            parsed = urllib.parse.urlsplit(origin)
        except ValueError:
            return False
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return False
        if parsed.path or parsed.query or parsed.fragment:
            return False
        if not _is_trusted_console_host(parsed.netloc):
            return False
        if not _is_loopback_host(request_host) or not _is_loopback_host(parsed.netloc):
            return _host_ipv4_literal(request_host) == _host_ipv4_literal(parsed.netloc)
        return True

    def is_authenticated_request(self) -> bool:
        values = self.headers.get_all(BACKEND_INSTANCE_HEADER, [])
        return len(values) == 1 and management_tokens_equal(str(values[0]))

    def require_api_authentication(self) -> bool:
        if self.is_authenticated_request():
            return True
        self._send_request_error(403, "管理 API 凭证无效，请从启动入口重新打开控制台")
        return False

    def _send_request_error(self, status: int, message: str) -> None:
        path = urllib.parse.urlsplit(self.path).path
        if not path.startswith("/api/"):
            self.send_error(status)
            return
        if self.command == "HEAD":
            data = json.dumps({"error": message}, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            send_backend_instance_header(self)
            self.end_headers()
            return
        json_response(self, {"error": message}, status)

    def read_json(self) -> dict[str, Any]:
        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length) if raw_length is not None else 0
        except (TypeError, ValueError) as exc:
            raise ValueError("Content-Length 无效") from exc
        if length < 0 or length > API_JSON_BODY_LIMIT:
            raise ValueError(f"Content-Length 必须在 0..{API_JSON_BODY_LIMIT} 之间")
        if length == 0:
            return {}
        data = self.rfile.read(length)
        return json.loads(data.decode("utf-8"))

    def handle_browser_window_closed(self) -> None:
        raw_length = str(self.headers.get("Content-Length") or "0")
        length = int(raw_length) if re.fullmatch(r"[0-9]{1,4}", raw_length) else -1
        if 0 < length <= 256:
            try:
                token = self.rfile.read(length).decode("ascii", errors="ignore")
                handle_browser_last_window_closed(token)
            except Exception as exc:
                log_event(f"处理浏览器窗口关闭通知失败：{exc}", level="warn", category="浏览器")
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def handle_browser_extension_upload(self) -> None:
        require_browser_feature()
        content_type = _single_upload_header(self.headers, "Content-Type")
        if content_type.strip().lower() != "application/octet-stream":
            raise CoreUploadUnsupportedMediaType("扩展上传仅支持 application/octet-stream")
        if _single_upload_header(self.headers, "Transfer-Encoding", required=False):
            raise ValueError("扩展上传不支持 Transfer-Encoding")
        raw_length = _single_upload_header(self.headers, "Content-Length", required=False)
        if not raw_length:
            raise CoreUploadLengthRequired("扩展上传必须提供 Content-Length")
        if re.fullmatch(r"[0-9]+", raw_length) is None or int(raw_length) < 1:
            raise ValueError("Content-Length 无效")
        length = int(raw_length)
        if length > BROWSER_EXTENSION_UPLOAD_LIMIT:
            raise CoreUploadTooLarge(f"扩展包不能超过 {BROWSER_EXTENSION_UPLOAD_LIMIT // (1024 * 1024)} MiB")
        filename = _safe_uploaded_filename(_single_upload_header(self.headers, CORE_UPLOAD_NAME_HEADER))
        if not filename.lower().endswith((".zip", ".crx")):
            raise ValueError("扩展包只支持 .zip 或 .crx")
        ensure_dirs()
        with tempfile.TemporaryDirectory(prefix="proxybridge-extension-", dir=str(RUNTIME_ROOT)) as temp_dir:
            staged_path = Path(temp_dir) / "package.bin"
            with staged_path.open("wb") as destination:
                actual_sha256 = _stream_core_upload(self.rfile, destination, length)
            result = install_browser_extension_package(staged_path, filename, actual_sha256)
        extension = result.get("extension") or {}
        log_event(f"导入浏览器扩展：{extension.get('name') or filename}", category="浏览器")
        json_response(self, {**result, "extensions": browser_extension_library()})

    def handle_core_upload(self) -> None:
        kind, filename, length, expected_sha256 = _parse_core_upload_headers(self.headers)
        _validate_uploaded_package_name(kind, filename)
        if kind == "fingerprint-chromium":
            require_browser_feature()
        # 大文件写盘前先给出可恢复的运行状态错误；安装锁内还会再次检查，防止上传期间启动实例。
        # 无法从文件名识别版本时按「新目录」处理，实际内核 ID 在校验摘要后确定。
        _assert_kernel_upload_target_idle(
            kind,
            fingerprint_kernel_id=(
                browser_fingerprint.fingerprint_version_from_name(filename) or "upload-pending"
                if kind == "fingerprint-chromium"
                else None
            ),
        )
        if not CORE_UPLOAD_LOCK.acquire(blocking=False):
            raise CoreUploadBusy("已有内核文件正在上传或安装，请等待完成后重试")
        connection = getattr(self, "connection", None)
        previous_timeout: float | None = None
        timeout_changed = False
        try:
            if connection is not None:
                try:
                    previous_timeout = connection.gettimeout()
                    connection.settimeout(CORE_UPLOAD_IO_TIMEOUT)
                    timeout_changed = True
                except OSError:
                    pass
            ensure_dirs()
            with tempfile.TemporaryDirectory(prefix="proxybridge-upload-", dir=str(RUNTIME_ROOT)) as temp_dir:
                temp_root = Path(temp_dir)
                try:
                    temp_root.chmod(0o700)
                except OSError:
                    pass
                descriptor, staged_name = tempfile.mkstemp(
                    prefix="package-",
                    suffix=_uploaded_package_suffix(filename),
                    dir=temp_dir,
                )
                staged_path = Path(staged_name)
                try:
                    try:
                        os.fchmod(descriptor, 0o600)
                    except (AttributeError, OSError):
                        pass
                    with os.fdopen(descriptor, "wb") as destination:
                        descriptor = -1
                        actual_sha256 = _stream_core_upload(
                            self.rfile,
                            destination,
                            length,
                            expected_sha256,
                        )
                        destination.flush()
                        os.fsync(destination.fileno())
                    result = install_uploaded_kernel(
                        kind,
                        staged_path,
                        filename,
                        size=length,
                        sha256=actual_sha256,
                    )
                finally:
                    if descriptor >= 0:
                        os.close(descriptor)
        finally:
            if timeout_changed:
                try:
                    connection.settimeout(previous_timeout)
                except OSError:
                    pass
            CORE_UPLOAD_LOCK.release()

        log_event(
            f"本地导入 {kind} 完成：{filename} · SHA-256 {actual_sha256[:12]}…",
            category="核心",
        )
        json_response(self, result)

    def handle_pool_request(self, parsed: urllib.parse.SplitResult, *, head_only: bool = False) -> None:
        host = self.headers.get("Host") or ""
        if not _is_loopback_host(host):
            json_response(self, {"error": "无效的代理池凭证"}, 403, head_only=head_only)
            return
        token, action, error_kind = parse_pool_path(parsed.path)
        if error_kind == "forbidden":
            json_response(self, {"error": "无效的代理池凭证"}, 403, head_only=head_only)
            return
        if error_kind == "not_found" or token is None or action is None:
            json_response(self, {"error": "未找到代理池资源"}, 404, head_only=head_only)
            return

        params = urllib.parse.parse_qs(parsed.query)
        try:
            protocol = parse_protocol_param(first(params, "protocol", default="") or None)
            region = first(params, "region", default="").strip() or None
            q = first(params, "q", default="").strip() or None
            strategy = (first(params, "strategy", default="random") or "random").strip().lower()
            response_format = (first(params, "format", default="json") or "json").strip().lower()
            include_starting = truthy(first(params, "include_starting", default=""))
            if action == "get":
                if strategy not in {"random", "round_robin", "health", "latency"}:
                    raise ValueError("strategy 只能是 random、round_robin、health 或 latency")
                if response_format not in {"json", "text"}:
                    raise ValueError("format 只能是 json 或 text")
        except ValueError as exc:
            error_response(self, exc, 400)
            return

        try:
            response_kind = "json"
            response_status = 200
            response_body: Any = None
            with SHUTDOWN_LOCK:
                synchronize_process_state()
                with STATE_LOCK:
                    state = load_state()
                    changed = sync_process_status(state)
                    expected_token, token_changed = ensure_pool_token(state)
                    if changed or token_changed:
                        save_state(state)
                    if not pool_tokens_equal(token, expected_token):
                        response_body = {"error": "无效的代理池凭证"}
                        response_status = 403
                    elif action == "list.json":
                        members = public_pool_members(
                            state,
                            pool_members(state, region=region, q=q, ready_only=False),
                        )
                        payload = {
                            "count": len(members),
                            "updated_at": float(now_ts()),
                            "proxies": members,
                        }
                        if protocol is not None:
                            payload["protocol"] = protocol
                        response_body = payload
                    elif action == "proxies.txt":
                        members = public_pool_members(
                            state,
                            pool_members(state, region=region, q=q, ready_only=False),
                        )
                        response_kind = "text"
                        response_body = render_proxies_txt(members, protocol)
                    else:
                        # action == "get"
                        ready_members = public_pool_members(
                            state,
                            pool_members(state, region=region, q=q, ready_only=True),
                        )
                        candidates = ready_members
                        if not candidates and include_starting:
                            candidates = [
                                member
                                for member in public_pool_members(
                                    state,
                                    pool_members(state, region=region, q=q, ready_only=False),
                                )
                                if member.get("available")
                            ]
                        if not candidates:
                            response_status = 404
                            if response_format == "text":
                                response_kind = "text"
                                response_body = "error: pool empty\n"
                            else:
                                response_body = {"error": "代理池为空"}
                        else:
                            picked = pick_pool_member(candidates, strategy)
                            if picked.get("circuit_state") == "half_open":
                                picked_id = str(picked.get("id") or "")
                                raw_member = next(
                                    (
                                        member
                                        for member in state.get("bridges", [])
                                        if str(member.get("id") or "") == picked_id
                                    ),
                                    None,
                                )
                                if raw_member is not None:
                                    health = normalize_pool_health(raw_member.get("pool_health"))
                                    health["half_open_probe_until"] = now_ts() + POOL_HALF_OPEN_PROBE_SECONDS
                                    raw_member["pool_health"] = health
                                    picked["available"] = False
                                    picked["probe_in_progress"] = True
                                    save_state(state)
                            picked_url = select_proxy_url(picked, protocol)
                            if response_format == "text":
                                response_kind = "text"
                                response_body = f"{picked_url}\n"
                            else:
                                response_body = {
                                    "strategy": strategy,
                                    "protocol": protocol or "socks5",
                                    "picked_url": picked_url,
                                    "proxy": picked,
                                }
            if response_kind == "text":
                text_response(self, str(response_body or ""), response_status, head_only=head_only)
            else:
                json_response(self, response_body, response_status, head_only=head_only)
        except Exception as exc:
            error_response(self, exc, 400)

    def handle_status_events(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        send_backend_instance_header(self)
        self.end_headers()
        self.close_connection = True
        connection = getattr(self, "connection", None)
        if connection is not None:
            connection.settimeout(15.0)
        # 一分钟内主动结束，客户端携带实例请求头重连，避免旧实例长期占用连接。
        deadline = time.monotonic() + 55.0
        signature = ""
        try:
            while not SHUTTING_DOWN.is_set() and time.monotonic() < deadline:
                with STATUS_EVENT_CONDITION:
                    revision = STATUS_EVENT_REVISION
                data = json.dumps(status_payload(), ensure_ascii=False, separators=(",", ":"))
                current = hashlib.sha256(data.encode("utf-8")).hexdigest()
                if current != signature:
                    self.wfile.write(f"event: status\ndata: {data}\n\n".encode("utf-8"))
                    signature = current
                else:
                    self.wfile.write(b": heartbeat\n\n")
                self.wfile.flush()
                with STATUS_EVENT_CONDITION:
                    STATUS_EVENT_CONDITION.wait_for(
                        lambda: STATUS_EVENT_REVISION != revision or SHUTTING_DOWN.is_set(), timeout=3.5)
        except (BrokenPipeError, ConnectionError, OSError):
            return

    def do_GET(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path.startswith("/pool/") or parsed.path in {"/pool", "/pool/"}:
            self.handle_pool_request(parsed, head_only=False)
            return
        if not self.is_trusted_request():
            self._send_request_error(403, "不可信请求来源")
            return
        if parsed.path.startswith("/api/") and not self.require_api_authentication():
            return
        if parsed.path == "/api/events":
            self.handle_status_events()
            return
        if parsed.path == "/api/cores":
            try:
                json_response(self, cores_status_payload())
            except Exception as exc:
                error_response(self, exc, 400)
            return
        if parsed.path == "/api/nodes/history":
            params = urllib.parse.parse_qs(parsed.query)
            node_id = first(params, "node_id", default="")
            with STATE_LOCK:
                state = load_state()
                history = state.get("test_history", {}).get(node_id, [])
            json_response(self, {"node_id": node_id, "history": history})
            return
        if parsed.path == "/api/status":
            try:
                json_response(self, status_payload(), no_store=True)
            except Exception as exc:
                error_response(self, exc, 500)
            return
        if parsed.path == "/api/pool":
            try:
                json_response(self, pool_admin_payload(), no_store=True)
            except Exception as exc:
                error_response(self, exc, 400)
            return
        if parsed.path == "/api/network/clients":
            json_response(self, active_proxy_clients_payload())
            return
        if parsed.path == "/api/sync/status":
            try:
                json_response(self, sync_status_payload())
            except Exception as exc:
                error_response(self, exc, 400)
            return
        if parsed.path == "/api/speed-test/tasks":
            try:
                params = urllib.parse.parse_qs(parsed.query)
                json_response(self, speed_test_task_payload(first(params, "id", default="") or None))
            except Exception as exc:
                error_response(self, exc, 400)
            return
        if parsed.path == "/api/subscriptions/refresh-tasks":
            try:
                params = urllib.parse.parse_qs(parsed.query)
                json_response(self, subscription_refresh_task_payload(first(params, "id", default="") or None))
            except Exception as exc:
                error_response(self, exc, 400)
            return
        if parsed.path == "/api/logs":
            try:
                json_response(self, query_logs(urllib.parse.parse_qs(parsed.query)))
            except Exception as exc:
                error_response(self, exc, 400)
            return
        if parsed.path == "/api/logs/export":
            try:
                data, content_type, filename = export_logs(urllib.parse.parse_qs(parsed.query))
                download_response(self, data, content_type, filename)
            except Exception as exc:
                error_response(self, exc, 400)
            return
        if parsed.path == "/api/core/logs":
            params = urllib.parse.parse_qs(parsed.query)
            core = first(params, "core", default="")
            try:
                json_response(self, core_log_payload(core))
            except Exception as exc:
                error_response(self, exc, 400)
            return
        if parsed.path == "/api/system/logs":
            json_response(self, {"system_logs": system_log_entries()})
            return
        if parsed.path.startswith("/api/log/"):
            bridge_id = parsed.path.rsplit("/", 1)[-1]
            path = RUNTIME_ROOT / f"{bridge_id}.log"
            json_response(self, {"log": read_tail(path)})
            return
        if parsed.path.startswith("/api/config/"):
            error_response(self, ValueError("运行配置包含代理凭据，不提供 HTTP 读取"), 404)
            return
        self.serve_static()

    def do_HEAD(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path.startswith("/pool/") or parsed.path in {"/pool", "/pool/"}:
            self.handle_pool_request(parsed, head_only=True)
            return
        if not self.is_trusted_request():
            self._send_request_error(403, "不可信请求来源")
            return
        if self.path == "/api/status" or parsed.path == "/api/status":
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", "0")
            send_backend_instance_header(self)
            self.end_headers()
            return
        if parsed.path.startswith("/api/") and not self.require_api_authentication():
            return
        self.serve_static(head_only=True)

    def do_POST(self) -> None:
        if urllib.parse.urlsplit(self.path).path == BROWSER_WINDOW_CLOSED_PATH:
            # 来自浏览器内窗口助手扩展：来源是 chrome-extension://，无法携带管理令牌，
            # 仅凭启动时下发的一次性令牌鉴权；无论结果如何都返回 204，不泄露状态。
            self.handle_browser_window_closed()
            return
        if not self.is_trusted_request():
            self._send_request_error(403, "不可信请求来源")
            return
        if not self.require_api_authentication():
            return
        parsed_path = urllib.parse.urlsplit(self.path).path
        if parsed_path == BROWSER_EXTENSION_UPLOAD_PATH:
            try:
                self.handle_browser_extension_upload()
            except CoreUploadTooLarge as exc:
                error_response(self, exc, 413)
            except CoreUploadUnsupportedMediaType as exc:
                error_response(self, exc, 415)
            except CoreUploadLengthRequired as exc:
                error_response(self, exc, 411)
            except Exception as exc:
                error_response(self, exc, 400)
            return
        if parsed_path == CORE_UPLOAD_PATH:
            try:
                self.handle_core_upload()
            except CoreUploadTooLarge as exc:
                error_response(self, exc, 413)
            except CoreUploadUnsupportedMediaType as exc:
                error_response(self, exc, 415)
            except CoreUploadLengthRequired as exc:
                error_response(self, exc, 411)
            except CoreUploadBusy as exc:
                error_response(self, exc, 409)
            except Exception as exc:
                error_response(self, exc, 400)
            return
        if self.headers.get_content_type() != "application/json":
            self._send_request_error(415, "仅支持 application/json")
            return
        try:
            payload = self.read_json()
            if self.path == "/api/subscriptions/import":
                result = import_subscription(payload)
                sub = result.get("subscription", {}) if isinstance(result, dict) else {}
                log_event(f"导入订阅「{sub.get('name', '')}」共 {sub.get('node_count', 0)} 个节点", category="订阅")
                json_response(self, result)
            elif self.path == "/api/subscriptions/preview":
                json_response(self, preview_subscription(payload))
            elif self.path == "/api/subscriptions/update":
                json_response(self, update_subscription_details(payload))
            elif self.path == "/api/subscriptions/refresh":
                result = refresh_subscription(str(payload.get("id") or ""))
                json_response(self, result)
                changes = result.get("changes") or {}
                log_event(
                    "刷新订阅完成："
                    f"新增 {safe_int(changes.get('added'), 0)}、"
                    f"删除 {safe_int(changes.get('removed'), 0)}、"
                    f"变更 {safe_int(changes.get('changed'), 0)}",
                    category="订阅",
                )
            elif self.path == "/api/subscriptions/refresh-batch/start":
                json_response(self, start_subscription_refresh_task(payload), 202)
            elif self.path == "/api/subscriptions/refresh-batch/cancel":
                json_response(self, cancel_subscription_refresh_task(str(payload.get("id") or "")))
            elif self.path == "/api/subscriptions/schedule":
                json_response(self, {"ok": True, "subscription": update_subscription_schedule(payload)})
            elif self.path == "/api/settings/subscriptions":
                settings = update_subscription_settings(payload)
                state_label = "开启" if settings["allow_non_public_urls"] else "关闭"
                log_event(f"非公网订阅地址访问已{state_label}", category="设置")
                json_response(self, {"ok": True, "settings": settings})
            elif self.path == "/api/subscriptions/delete":
                json_response(
                    self,
                    delete_subscription(
                        str(payload.get("id") or ""),
                        delete_browser_profiles=payload.get("delete_browser_profiles") is True,
                    ),
                )
                log_event("删除订阅", category="订阅")
            elif self.path == "/api/bridges/start":
                result = start_bridge(payload)
                log_event(f"启动本地代理:{result.get('node_name', '')} → {result.get('socks_url', '')}", category="代理")
                json_response(self, result)
            elif self.path == "/api/bridges/stop":
                json_response(self, stop_bridge(str(payload.get("id") or "")))
                log_event("停止本地代理", category="代理")
            elif self.path == "/api/bridges/stop-all":
                json_response(self, stop_all_bridges())
                log_event("停止全部本地代理", category="代理")
            elif self.path == "/api/nodes/test":
                result = test_node(payload)
                ok = result.get("ok")
                latency = result.get("latency_ms")
                node_name = result.get("node_name", "")
                if ok:
                    log_event(f"测试节点「{node_name}」通信正常 {latency} ms", category="测试")
                else:
                    log_event(f"测试节点「{node_name}」失败:{result.get('message', '')}", level="warn", category="测试")
                json_response(self, result)
            elif self.path == "/api/nodes/test-batch/start":
                json_response(self, start_speed_test_task(payload), 202)
            elif self.path == "/api/nodes/test-batch/cancel":
                json_response(self, cancel_speed_test_task(str(payload.get("id") or "")))
            elif self.path == "/api/nodes/core":
                json_response(self, update_node_core_settings(payload))
            elif self.path == "/api/nodes/update":
                json_response(self, update_node_details(payload))
            elif self.path == "/api/settings/health":
                json_response(self, update_health_settings(payload))
            elif self.path == "/api/cores/update":
                json_response(self, update_proxy_core(payload))
            elif self.path == "/api/settings/speed-test":
                json_response(self, {"ok": True, "settings": update_speed_test_settings(payload)})
            elif self.path == "/api/settings/logs":
                json_response(self, {"ok": True, "settings": update_log_settings(payload)})
            elif self.path == "/api/diagnostics/run":
                result = run_diagnostics(payload)
                summary = result.get("summary") or {}
                log_event(
                    "诊断完成："
                    f"正常 {safe_int(summary.get('ok'), 0)}、"
                    f"警告 {safe_int(summary.get('warn'), 0)}、"
                    f"错误 {safe_int(summary.get('error'), 0)}、"
                    f"跳过 {safe_int(summary.get('skipped'), 0)}",
                    category="诊断",
                )
                json_response(self, result)
            elif self.path == "/api/browser/launch":
                result = launch_browser(payload)
                try:
                    schedule_browser_exit_check(result)
                except Exception as exc:
                    log_event(f"浏览器已启动，出口自检调度失败：{exc}", level="warn", category="浏览器")
                log_event(f"启动指纹浏览器:{result.get('region', '')} → {result.get('proxy', '')}", category="浏览器")
                json_response(self, result)
            elif self.path == "/api/browser/stop":
                json_response(self, stop_browser(payload))
                log_event("停止指纹浏览器", category="浏览器")
            elif self.path == "/api/browser/credentials":
                result = remote_browser_credentials(payload)
                json_response(self, result, no_store=True)
                log_event(
                    f"查看远程浏览器凭证:{result.get('instance_id') or ''}",
                    category="浏览器",
                )
            elif self.path == "/api/browser/delete":
                result = delete_browser_instance(payload)
                json_response(self, result)
                log_event(
                    f"删除浏览器记录:{result.get('browser_name') or result.get('instance_id') or ''}",
                    category="浏览器",
                )
            elif self.path == "/api/browser/update":
                result = update_browser_instance(payload)
                if set(payload) & {"label", "tag"} and "instance" in result and len(payload) <= 2:
                    label = (result.get("instance") or {}).get("label") or ""
                    log_event(f"更新浏览器标签:{label or '（清空）'}", category="浏览器")
                else:
                    log_event(f"更新浏览器配置 {result.get('updated', 0)} 条", category="浏览器")
                json_response(self, result)
            elif self.path == "/api/browser/download-kernel":
                result = download_fingerprint_chromium(payload)
                log_event(f"指纹内核下载/安装:{result.get('kernel_id') or result.get('version') or '完成'}", category="浏览器")
                json_response(self, result)
            elif self.path == "/api/browser/create":
                result = create_browser_instances(payload)
                log_event(f"新建浏览器记录 {result.get('created', 0)} 条", category="浏览器")
                json_response(self, result)
            elif self.path == "/api/browser/rebind":
                result = rebind_browser_instances(payload)
                log_event(f"换绑浏览器记录 {result.get('updated', 0)} 条", category="浏览器")
                json_response(self, result)
            elif self.path == "/api/browser/regenerate-fingerprint":
                result = regenerate_browser_fingerprints(payload)
                log_event(f"重新生成浏览器指纹 {result.get('updated', 0)} 条", category="浏览器")
                json_response(self, result)
            elif self.path == "/api/browser/open-url":
                result = open_browser_url(payload)
                if result.get("method") == "launch":
                    try:
                        schedule_browser_exit_check(result)
                    except Exception as exc:
                        log_event(f"浏览器已启动，出口自检调度失败：{exc}", level="warn", category="浏览器")
                json_response(self, result)
            elif self.path == "/api/browser/cookies/export":
                result = export_browser_cookies(payload)
                log_event(f"导出浏览器 Cookie {result.get('count', 0)} 条", category="浏览器")
                json_response(self, result, no_store=True)
            elif self.path == "/api/browser/cookies/import":
                result = import_browser_cookies(payload)
                log_event(f"导入浏览器 Cookie {result.get('imported', 0)}/{result.get('total', 0)} 条", category="浏览器")
                json_response(self, result)
            elif self.path == "/api/browser/extensions/delete":
                json_response(self, delete_browser_extension(payload))
            elif self.path == "/api/browser/kernels/releases":
                json_response(self, list_fingerprint_kernel_releases())
            elif self.path == "/api/browser/kernels/default":
                result = set_default_fingerprint_kernel(payload)
                log_event(f"默认指纹内核设为：{result.get('default') or '自动（最新版本）'}", category="浏览器")
                json_response(self, result)
            elif self.path == "/api/browser/kernels/delete":
                result = delete_fingerprint_kernel(payload)
                log_event(f"删除指纹内核版本：{result.get('deleted')}", category="浏览器")
                json_response(self, result)
            elif self.path == "/api/clipboard":
                json_response(self, copy_to_clipboard(str(payload.get("text") or "")))
            elif self.path == "/api/logs/clear":
                json_response(self, clear_request_logs())
                log_event("连接日志已清空", category="系统")
            elif self.path == "/api/system/logs/clear":
                json_response(self, clear_system_logs())
            elif self.path == "/api/pool/token/rotate":
                result = rotate_pool_token()
                log_event("代理池 token 已轮换", category="代理")
                json_response(self, result)
            elif self.path == "/api/pool/feedback":
                json_response(self, update_pool_member_feedback(payload))
            elif self.path == "/api/pool/exclude":
                json_response(self, set_pool_member_exclusion(payload))
            elif self.path == "/api/network/clients/kick":
                json_response(self, kick_proxy_client(str(payload.get("id") or "")))
            elif self.path == "/api/smart-gateway/start":
                json_response(self, start_smart_gateway(payload))
                log_event("智能网关已启动", category="代理")
            elif self.path == "/api/smart-gateway/stop":
                json_response(self, stop_smart_gateway())
                log_event("智能网关已停止", category="代理")
            elif self.path == "/api/smart-gateway/settings":
                json_response(self, update_smart_gateway_settings(payload))
            elif self.path == "/api/settings/network":
                result = update_network_settings(payload)
                state_label = "开启" if result.get("network", {}).get("allow_lan") else "关闭"
                log_event(
                    f"局域网共享已{state_label}，重绑 {result.get('rebound_count', 0)} 个代理端口",
                    category="设置",
                )
                json_response(self, result, no_store=True)
            elif self.path == "/api/sync/settings":
                result = get_sync_engine().update_settings(payload)
                log_event("已更新 WebDAV 同步设置", category="同步")
                json_response(self, {"ok": True, "settings": result})
            elif self.path == "/api/sync/test":
                json_response(self, get_sync_engine().test())
            elif self.path == "/api/sync/push":
                result = get_sync_engine().push()
                log_event(result.get("settings", {}).get("last_status") or "同步上传完成", category="同步")
                json_response(self, result)
            elif self.path == "/api/sync/pull":
                force = truthy(payload.get("force"))
                preserve_local_changes = truthy(payload.get("preserve_local_changes"))
                result = get_sync_engine().pull(
                    force=force,
                    preserve_local_changes=preserve_local_changes,
                    **({"expected_state_sha256": str(payload["expected_state_sha256"])}
                       if "expected_state_sha256" in payload else {}),
                )
                log_event(
                    result.get("status")
                    or result.get("settings", {}).get("last_status")
                    or "同步下载完成",
                    category="同步",
                )
                json_response(self, result)
            elif self.path == "/api/sync/preview":
                json_response(self, get_sync_engine().preview_pull())
            elif self.path == "/api/sync/check":
                json_response(self, get_sync_engine().check_remote())
            else:
                error_response(self, ValueError("未知 API"), 404)
        except (urllib.error.URLError, TimeoutError) as exc:
            error_response(self, RuntimeError(f"订阅拉取失败：{exc}"), 502)
        except Exception as exc:
            error_response(self, exc, 400)

    def serve_static(self, head_only: bool = False) -> None:
        path = urllib.parse.urlsplit(self.path).path
        if path in {"/", ""}:
            file_path = STATIC_ROOT / "index.html"
        else:
            rel = path.removeprefix("/static/")
            file_path = STATIC_ROOT / rel
        try:
            resolved = file_path.resolve()
            if STATIC_ROOT.resolve() not in resolved.parents and resolved != STATIC_ROOT / "index.html":
                raise FileNotFoundError
            data = resolved.read_bytes()
        except Exception:
            self.send_error(404)
            return
        content_type = mimetypes.guess_type(str(resolved))[0] or "application/octet-stream"
        if resolved.suffix == ".html":
            content_type = "text/html; charset=utf-8"
        if resolved.suffix == ".css":
            content_type = "text/css; charset=utf-8"
        # 普通脚本与原生模块使用明确的 JavaScript MIME，兼容各平台的模块加载器。
        if resolved.suffix in {".js", ".mjs"}:
            content_type = "application/javascript; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if not head_only:
            self.wfile.write(data)


class ProxyBridgeHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _shutdown_remote_browser_workers() -> tuple[
    set[str],
    list[str],
    list[tuple[str, BaseException]],
]:
    """Cancel and stop remote workers without holding ``SHUTDOWN_LOCK``."""
    pending_tokens = remote_browser_pending_start_tokens()
    pending_instance_ids = invalidate_all_remote_browser_operations()
    remote_instance_ids = set(pending_instance_ids)
    stopped_remote_ids: list[str] = []
    failures: list[tuple[str, BaseException]] = []
    if not remote_browser_feature_enabled():
        wait_remote_browser_operations(5.0)
        return remote_instance_ids, stopped_remote_ids, failures

    with STATE_LOCK:
        shutdown_state = load_state()
        remote_instance_ids.update(
            str(item.get("id") or "")
            for item in shutdown_state.get("browser_instances", [])
            if item.get("status") in ACTIVE_BROWSER_STATUSES and item.get("id")
        )
    broker_sessions, _broker_error = remote_browser_sessions(force=True, timeout=10.0)
    if isinstance(broker_sessions, dict):
        remote_instance_ids.update(
            instance_id
            for instance_id, session in broker_sessions.items()
            if session.get("status") in REMOTE_BROWSER_LIVE_SESSION_STATUSES
        )

    instance_ids = sorted(instance_id for instance_id in remote_instance_ids if instance_id)
    if instance_ids:
        try:
            broker_client = remote_browser_broker_client()
        except BaseException as error:
            failures.append(("远程浏览器 Broker", error))
        else:
            def stop_worker(instance_id: str) -> None:
                broker_client.stop(
                    instance_id,
                    pending_tokens.get(instance_id, ""),
                    timeout=45.0,
                )

            with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(len(instance_ids), 10)
            ) as executor:
                future_ids = {
                    executor.submit(stop_worker, instance_id): instance_id
                    for instance_id in instance_ids
                }
                for future in concurrent.futures.as_completed(future_ids):
                    instance_id = future_ids[future]
                    try:
                        future.result()
                    except BaseException as error:
                        failures.append((f"远程浏览器 {instance_id}", error))
                    else:
                        stopped_remote_ids.append(instance_id)
    if stopped_remote_ids:
        invalidate_remote_browser_session_cache()
    # stop 会先在 Broker 写入本次 start 的取消 tombstone，再等待容器生命周期锁；
    # 因此先发 stop 比被动等待 readiness 更快。响应结束后只需短暂等待应用侧
    # start/stop 线程完成 finally 登记清理。
    if not wait_remote_browser_operations(10.0):
        failures.append(
            ("远程浏览器生命周期", RuntimeError("等待正在进行的远程浏览器操作超时"))
        )
    return remote_instance_ids, stopped_remote_ids, failures


def _shutdown_once() -> None:
    SHUTTING_DOWN.set()
    notify_status_change()
    SUBSCRIPTION_SCHEDULER_WAKE.set()
    with SPEED_TEST_TASK_LOCK:
        for cancel_event in SPEED_TEST_CANCEL_EVENTS.values():
            cancel_event.set()
    remote_instance_ids, stopped_remote_ids, remote_shutdown_failures = (
        _shutdown_remote_browser_workers()
    )

    with SHUTDOWN_LOCK:
        cancelled_refresh_history_ids: list[str] = []
        with SUBSCRIPTION_REFRESH_TASK_LOCK:
            cancelled_at = now_ts()
            for task_id, cancel_event in SUBSCRIPTION_REFRESH_CANCEL_EVENTS.items():
                cancel_event.set()
                task = SUBSCRIPTION_REFRESH_TASKS.get(task_id)
                if task is None or task.get("status") not in {"queued", "running", "cancelling"}:
                    continue
                subscription_ids = [str(item or "") for item in task.get("subscription_ids") or []]
                cancelled_refresh_history_ids.extend(
                    _cancel_pending_subscription_refresh_items(
                        task_id,
                        subscription_ids,
                        include_active=True,
                        error="程序退出，订阅刷新已取消",
                    )
                )
                task.update(
                    {
                        "status": "cancelled",
                        "completed": sum(1 for item in task.get("items") or [] if item is not None),
                        "updated_at": cancelled_at,
                        "finished_at": cancelled_at,
                    }
                )
        _record_subscription_refresh_cancellations(
            cancelled_refresh_history_ids,
            "程序退出，订阅刷新已取消",
        )
        with SMART_GATEWAY_LOCK:
            gateway = SMART_GATEWAY_TABLE.get(SMART_GATEWAY_ID)
        if gateway is not None:
            try:
                gateway.stop()
            except BaseException as error:
                log_event(f"智能网关停止失败：{error}", level="warn", category="代理")
            finally:
                with SMART_GATEWAY_LOCK:
                    if SMART_GATEWAY_TABLE.get(SMART_GATEWAY_ID) is gateway:
                        SMART_GATEWAY_TABLE.pop(SMART_GATEWAY_ID, None)
        with SPEED_TEST_TASK_LOCK:
            for cancel_event in SPEED_TEST_CANCEL_EVENTS.values():
                cancel_event.set()
        if remote_browser_feature_enabled():
            if stopped_remote_ids:
                with STATE_LOCK:
                    shutdown_state = load_state()
                    stopped_at = now_ts()
                    for instance_id in stopped_remote_ids:
                        try:
                            instance = find_browser_instance(shutdown_state, instance_id)
                        except ValueError:
                            continue
                        instance.update(
                            {
                                "status": "stopped",
                                "pid": None,
                                "remote_url": "",
                                "remote_container_id": "",
                                "remote_host_port": None,
                                "last_stopped_at": stopped_at,
                                "last_used_at": stopped_at,
                            }
                        )
                    save_state(shutdown_state)
            for instance_id in remote_instance_ids:
                prune_remote_browser_lifecycle(instance_id)
        # 只在进程表锁内快照句柄;外部进程的退出等待交给独立工作线程。
        with PROCESS_LOCK:
            browser_procs = list(BROWSER_PROCESS_TABLE.values())
            browser_procs.extend(p for procs in BROWSER_TABLE.values() for p in procs)
            bridge_ids = set(PROCESS_TABLE) | set(LOG_PROXY_TABLE)
            test_processes = list(TEST_PROCESS_TABLE.items())
        with TRAFFIC_LOCK:
            bridge_ids.update(TRAFFIC_TABLE)

        unique_browser_procs: list[tuple[tuple[str, int], subprocess.Popen]] = []
        seen_browser_processes: set[tuple[str, int]] = set()
        for process in browser_procs:
            pid = safe_int(getattr(process, "pid", None), 0)
            identity = ("pid", pid) if pid > 0 else ("object", id(process))
            if identity in seen_browser_processes:
                continue
            seen_browser_processes.add(identity)
            unique_browser_procs.append((identity, process))

        traffic_by_bridge: dict[str, dict[str, int]] = {}
        failures: list[tuple[str, BaseException]] = list(remote_shutdown_failures)
        worker_count = len(unique_browser_procs) + len(bridge_ids) + len(test_processes)
        if worker_count:
            with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as executor:
                browser_futures = [
                    (identity, process, executor.submit(terminate_process, process))
                    for identity, process in unique_browser_procs
                ]
                bridge_futures = {
                    bridge_id: executor.submit(stop_process, bridge_id, pop_traffic=False)
                    for bridge_id in sorted(bridge_ids)
                }
                test_futures = [
                    (test_id, process, executor.submit(terminate_process, process))
                    for test_id, process in test_processes
                ]
                for identity, process, future in browser_futures:
                    try:
                        future.result()
                    except BaseException as error:
                        failures.append((f"浏览器 PID {safe_int(getattr(process, 'pid', None), 0)}", error))
                        continue
                    with PROCESS_LOCK:
                        for instance_id, current in list(BROWSER_PROCESS_TABLE.items()):
                            current_pid = safe_int(getattr(current, "pid", None), 0)
                            current_identity = (
                                ("pid", current_pid) if current_pid > 0 else ("object", id(current))
                            )
                            if current_identity == identity:
                                BROWSER_PROCESS_TABLE.pop(instance_id, None)
                        for node_id, processes in list(BROWSER_TABLE.items()):
                            remaining = []
                            for current in processes:
                                current_pid = safe_int(getattr(current, "pid", None), 0)
                                current_identity = (
                                    ("pid", current_pid) if current_pid > 0 else ("object", id(current))
                                )
                                if current_identity != identity:
                                    remaining.append(current)
                            if remaining:
                                BROWSER_TABLE[node_id] = remaining
                            else:
                                BROWSER_TABLE.pop(node_id, None)
                for bridge_id, future in bridge_futures.items():
                    try:
                        traffic_by_bridge[bridge_id] = future.result()
                    except BaseException as error:
                        failures.append((f"Bridge {bridge_id}", error))
                for test_id, process, future in test_futures:
                    try:
                        future.result()
                    except BaseException as error:
                        failures.append((f"节点测试 PID {safe_int(getattr(process, 'pid', None), 0)}", error))
                        continue
                    with PROCESS_LOCK:
                        if TEST_PROCESS_TABLE.get(test_id) is process:
                            TEST_PROCESS_TABLE.pop(test_id, None)

        if traffic_by_bridge:
            try:
                with STATE_LOCK:
                    state = load_state()
                    stopped_at = now_ts()
                    for bridge in state.get("bridges", []):
                        bridge_id = bridge.get("id")
                        if bridge_id in traffic_by_bridge:
                            bridge.update(
                                {
                                    **traffic_by_bridge[bridge_id],
                                    "status": "stopped",
                                    "pid": None,
                                    "stopped_at": stopped_at,
                                }
                            )
                            bridge.pop("error", None)
                    save_state(state)
            except BaseException as error:
                failures.append(("状态保存", error))
            else:
                with TRAFFIC_LOCK:
                    for bridge_id, traffic in traffic_by_bridge.items():
                        if TRAFFIC_TABLE.get(bridge_id) == traffic:
                            TRAFFIC_TABLE.pop(bridge_id, None)

        if failures:
            details = "; ".join(f"{label}: {error}" for label, error in failures)
            raise RuntimeError(f"关闭部分进程失败（{len(failures)} 项）: {details}") from failures[0][1]


def shutdown() -> None:
    # signal、main.finally 与 atexit 可能依次触发；成功完成后不重复访问 Broker
    # 或再次保存状态。若某项清理失败则保留重试能力，兼容显式二次清理。
    if SHUTDOWN_COMPLETE.is_set() and SHUTTING_DOWN.is_set():
        return
    with SHUTDOWN_RUN_LOCK:
        if SHUTDOWN_COMPLETE.is_set() and SHUTTING_DOWN.is_set():
            return
        if not SHUTTING_DOWN.is_set():
            SHUTDOWN_COMPLETE.clear()
        _shutdown_once()
        SHUTDOWN_COMPLETE.set()


def handle_shutdown_signal(signum: int, _frame: Any) -> None:
    if SHUTDOWN_SIGNAL_CLAIMED.is_set():
        return
    if not SHUTDOWN_SIGNAL_CLAIM_LOCK.acquire(blocking=False):
        return
    try:
        if SHUTDOWN_SIGNAL_CLAIMED.is_set():
            return
        SHUTDOWN_SIGNAL_CLAIMED.set()
    finally:
        SHUTDOWN_SIGNAL_CLAIM_LOCK.release()
    shutdown()
    raise SystemExit(0)


def start_parent_monitor() -> None:
    parent_pid = safe_int(os.environ.get("PROXY_BRIDGE_PARENT_PID"), 0)
    if parent_pid <= 0:
        return

    def monitor() -> None:
        while not SHUTTING_DOWN.is_set():
            if os.getppid() != parent_pid:
                shutdown()
                os._exit(0)
            time.sleep(1.0)

    threading.Thread(target=monitor, name="parent-process-monitor", daemon=True).start()


def parse_cli_args(argv: list[str] | None = None) -> Any:
    import argparse

    parser = argparse.ArgumentParser(
        prog="proxybridge",
        description="ProxyBridge headless console (macOS / Linux / Windows)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help=f"Web console port (default: env PROXY_BRIDGE_PORT or {DEFAULT_SERVER_PORT})",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("PROXY_BRIDGE_HOST", "127.0.0.1"),
        help="Console bind host (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--workdir",
        default=None,
        help="Data directory for state/runtime/cores (default: env PROXY_BRIDGE_WORKDIR or ./work)",
    )
    parser.add_argument(
        "--open",
        action="store_true",
        help="Open the web console in the system browser after startup",
    )
    parser.add_argument(
        "--no-core-auto-install",
        action="store_true",
        help="Do not auto-download sing-box/xray on startup",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_cli_args(argv)
    if args.workdir:
        configure_work_root(args.workdir)
        os.environ["PROXY_BRIDGE_WORKDIR"] = str(WORK_ROOT)
    elif os.environ.get("PROXY_BRIDGE_WORKDIR"):
        configure_work_root(os.environ["PROXY_BRIDGE_WORKDIR"])

    ensure_dirs()
    atexit.register(shutdown)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, handle_shutdown_signal)
    signal.signal(signal.SIGINT, handle_shutdown_signal)
    port = safe_int(args.port if args.port is not None else os.environ.get("PROXY_BRIDGE_PORT"), DEFAULT_SERVER_PORT)
    if port <= 0 or port > 65535:
        raise SystemExit(f"无效端口: {port}")
    host = str(args.host or "127.0.0.1").strip() or "127.0.0.1"
    # Keep derived URLs (proxy pool links, status payloads) aligned with CLI overrides.
    os.environ["PROXY_BRIDGE_PORT"] = str(port)
    os.environ["PROXY_BRIDGE_HOST"] = host
    start_parent_monitor()
    start_subscription_scheduler()
    if not args.no_core_auto_install:
        start_core_auto_install()
    restore_enabled_bridges()
    restore_enabled_smart_gateway()
    start_bridge_health_monitor()
    try:
        get_sync_engine().maybe_check_on_startup()
    except Exception as exc:
        log_event(f"启动同步检查调度失败: {exc}", level="warn", category="同步")
    server = ProxyBridgeHTTPServer((host, port), AppHandler)
    public_host = str(_configured_docker_console_ip() or "127.0.0.1")
    console_url = f"http://{public_host}:{port}"
    authenticated_console_url = console_access_url(console_url)
    log_event(
        f"Proxy Bridge 已启动({host_os_token()}/{host_arch_token()}),监听 {console_url}",
        category="系统",
    )
    managed_by_parent = safe_int(os.environ.get("PROXY_BRIDGE_PARENT_PID"), 0) > 0 and backend_instance_token() is not None
    print(
        f"Proxy Bridge console: {console_url if managed_by_parent else authenticated_console_url}",
        flush=True,
    )
    print(f"Work dir: {WORK_ROOT}", flush=True)
    if args.open and not docker_mode():
        try:
            import webbrowser

            webbrowser.open(authenticated_console_url)
        except Exception as exc:
            log_event(f"自动打开浏览器失败: {exc}", level="warn", category="系统")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        shutdown()


if __name__ == "__main__":
    main()
