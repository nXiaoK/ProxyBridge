from __future__ import annotations

import argparse
import concurrent.futures
import errno
import hmac
import http.client
import ipaddress
import json
import os
import posixpath
import re
import secrets
import signal
import socket
import threading
import time
import urllib.parse
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable


REQUEST_BODY_LIMIT = 128 * 1024
ENGINE_RESPONSE_LIMIT = 16 * 1024 * 1024
MIN_VOLUME_SUBPATH_API = (1, 45)
WORKER_PLATFORM = "linux/amd64"
WORKER_IMAGE_PULL_TIMEOUT = 900.0
BROKER_CONTROL_PORT = 8765
INSTANCE_ID_PATTERN = re.compile(r"[a-f0-9]{16}")
OPERATION_ID_PATTERN = re.compile(r"[a-f0-9]{32}")
USERNAME_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,32}")
PROFILE_PART_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,128}")
IMAGE_PATTERN = re.compile(r"[^\s\x00-\x1f]{1,300}")
PRIVATE_WORKER_NETWORKS = tuple(
    ipaddress.IPv4Network(value)
    for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)
PRIVATE_BIND_NETWORKS = PRIVATE_WORKER_NETWORKS + (ipaddress.IPv4Network("100.64.0.0/10"),)
LABEL_WORKER = "com.proxybridge.browser-worker"
LABEL_INSTANCE = "com.proxybridge.browser-instance"
LABEL_OWNER = "com.proxybridge.owner"
LABEL_PROXY_PORT = "com.proxybridge.browser-proxy-port"
LABEL_PUBLIC_PORT = "com.proxybridge.browser-public-port"
LIVE_WORKER_STATUSES = frozenset({"running", "starting", "failed"})
RELAY_ACTIVE_STATUSES = frozenset({"running", "starting"})
WORKER_HTTPS_PORT = 3001
RELAY_BUFFER_SIZE = 64 * 1024
RELAY_MAX_CONNECTIONS = 16
WORKER_INIT_SCRIPT = """#!/usr/bin/with-contenv bash
set -euo pipefail
test -x /opt/fingerprint-chromium/chrome-wrapper
# 使用启动器保留 chrome-wrapper 自身的真实 $0 路径，避免符号链接让它误判资源目录。
rm -f /usr/bin/chromium
cat > /usr/bin/chromium <<'PROXYBRIDGE_LAUNCHER'
#!/bin/sh
exec /opt/fingerprint-chromium/chrome-wrapper "$@"
PROXYBRIDGE_LAUNCHER
chmod 0755 /usr/bin/chromium
"""


class BrokerError(RuntimeError):
    pass


class EngineTimeoutError(BrokerError):
    pass


class EngineError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class RelayPortInUseError(BrokerError):
    pass


class WorkerTCPRelay:
    """把 Broker 发布端口上的 TLS 字节流转发到 internal worker。"""

    def __init__(self, listen_port: int, target_host: str, target_port: int = WORKER_HTTPS_PORT) -> None:
        self.listen_port = listen_port
        self.target_host = target_host
        self.target_port = target_port
        self.stopping = threading.Event()
        self._connection_lock = threading.Lock()
        self._connections: set[socket.socket] = set()
        self._connection_slots = threading.BoundedSemaphore(RELAY_MAX_CONNECTIONS)
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            # Broker 容器内监听全部接口；真正允许的宿主机地址由 Compose 的
            # PROXY_BRIDGE_BROWSER_HOST 端口映射限制，不能在这里绑定宿主机 IP。
            self._listener.bind(("0.0.0.0", listen_port))
            self._listener.listen(128)
            self._listener.settimeout(0.5)
        except OSError as exc:
            self._listener.close()
            if exc.errno == errno.EADDRINUSE:
                raise RelayPortInUseError(f"远程浏览器发布端口已被占用（{listen_port}）") from exc
            raise BrokerError(f"无法监听远程浏览器发布端口 {listen_port}") from exc
        self._thread = threading.Thread(
            target=self._serve,
            name=f"browser-relay-{listen_port}",
            daemon=True,
        )

    @staticmethod
    def _close_socket(connection: socket.socket) -> None:
        try:
            connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            connection.close()
        except OSError:
            pass

    @staticmethod
    def _pump(
        source: socket.socket,
        destination: socket.socket,
        finished: threading.Event,
        failed: threading.Event,
    ) -> None:
        try:
            while True:
                data = source.recv(RELAY_BUFFER_SIZE)
                if not data:
                    try:
                        destination.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
                    return
                destination.sendall(data)
        except OSError:
            failed.set()
        finally:
            finished.set()

    def _track(self, *connections: socket.socket) -> None:
        with self._connection_lock:
            self._connections.update(connections)

    def _untrack(self, *connections: socket.socket) -> None:
        with self._connection_lock:
            for connection in connections:
                self._connections.discard(connection)

    def _handle_client(self, client: socket.socket) -> None:
        upstream: socket.socket | None = None
        try:
            if self.stopping.is_set():
                return
            upstream = socket.create_connection(
                (self.target_host, self.target_port),
                timeout=5.0,
            )
            upstream.settimeout(None)
            client.settimeout(None)
            if self.stopping.is_set():
                return
            self._track(client, upstream)
            forward_finished = threading.Event()
            reverse_finished = threading.Event()
            failed = threading.Event()
            forward = threading.Thread(
                target=self._pump,
                args=(client, upstream, forward_finished, failed),
                name=f"browser-relay-{self.listen_port}-up",
                daemon=True,
            )
            reverse = threading.Thread(
                target=self._pump,
                args=(upstream, client, reverse_finished, failed),
                name=f"browser-relay-{self.listen_port}-down",
                daemon=True,
            )
            forward.start()
            reverse.start()
            while not self.stopping.is_set() and not failed.is_set():
                if forward_finished.wait(0.05) and reverse_finished.is_set():
                    break
            self._close_socket(client)
            self._close_socket(upstream)
            forward.join(timeout=2.0)
            reverse.join(timeout=2.0)
        except OSError:
            pass
        finally:
            self._close_socket(client)
            if upstream is not None:
                self._close_socket(upstream)
                self._untrack(client, upstream)
            self._connection_slots.release()

    def _serve(self) -> None:
        while not self.stopping.is_set():
            try:
                client, _address = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            if not self._connection_slots.acquire(blocking=False):
                self._close_socket(client)
                continue
            handler = threading.Thread(
                target=self._handle_client,
                args=(client,),
                name=f"browser-relay-{self.listen_port}-connection",
                daemon=True,
            )
            try:
                handler.start()
            except RuntimeError:
                self._close_socket(client)
                self._connection_slots.release()

    def start(self) -> None:
        try:
            # 在返回公开链接前确认 Broker 确实已加入 worker internal 网络；
            # 只依赖 Docker 元数据会让错误的 Compose 覆盖延迟到用户打开链接才暴露。
            upstream_probe = socket.create_connection(
                (self.target_host, self.target_port),
                timeout=5.0,
            )
        except OSError as exc:
            raise BrokerError("Browser Broker 无法连接 internal worker HTTPS 服务") from exc
        self._close_socket(upstream_probe)
        self._thread.start()
        print(
            "ProxyBridge browser relay listening on "
            f"0.0.0.0:{self.listen_port} -> {self.target_host}:{self.target_port}",
            flush=True,
        )

    def stop(self) -> None:
        self.stopping.set()
        self._close_socket(self._listener)
        with self._connection_lock:
            connections = list(self._connections)
        for connection in connections:
            self._close_socket(connection)
        if self._thread.ident is not None:
            self._thread.join(timeout=3.0)


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str, *, timeout: float = 120.0) -> None:
        super().__init__("docker", timeout=timeout)
        self.socket_path = socket_path

    def connect(self) -> None:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(self.timeout)
        connection.connect(self.socket_path)
        self.sock = connection


class DockerEngineClient:
    def __init__(self, socket_path: str, *, timeout: float = 120.0) -> None:
        self.socket_path = socket_path
        self.timeout = timeout
        self._api_version: str | None = None
        self._version_lock = threading.Lock()

    @staticmethod
    def _json_message(data: bytes, fallback: str) -> str:
        try:
            payload = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return fallback
        if isinstance(payload, dict):
            return str(payload.get("message") or payload.get("error") or fallback)[:1000]
        return fallback

    def _raw_request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> tuple[int, bytes]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(body))
        connection = UnixHTTPConnection(self.socket_path, timeout=timeout or self.timeout)
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            data = response.read(ENGINE_RESPONSE_LIMIT + 1)
        except TimeoutError as exc:
            raise EngineTimeoutError("Docker Engine 请求超时") from exc
        except (OSError, http.client.HTTPException) as exc:
            raise BrokerError("无法连接 Docker Engine") from exc
        finally:
            connection.close()
        if len(data) > ENGINE_RESPONSE_LIMIT:
            raise BrokerError("Docker Engine 响应过大")
        return response.status, data

    def api_version(self) -> str:
        if self._api_version:
            return self._api_version
        with self._version_lock:
            if self._api_version:
                return self._api_version
            status, data = self._raw_request("GET", "/version", timeout=10.0)
            if status != 200:
                raise BrokerError("无法读取 Docker Engine 版本")
            try:
                payload = json.loads(data.decode("utf-8"))
                version = str(payload.get("ApiVersion") or "")
            except (UnicodeDecodeError, json.JSONDecodeError, AttributeError) as exc:
                raise BrokerError("Docker Engine 版本响应无效") from exc
            if re.fullmatch(r"[0-9]+\.[0-9]+", version) is None:
                raise BrokerError("Docker Engine API 版本无效")
            self._api_version = version
            return version

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        expected: tuple[int, ...] = (200,),
        timeout: float | None = None,
    ) -> bytes:
        versioned_path = f"/v{self.api_version()}{path}"
        status, data = self._raw_request(method, versioned_path, payload, timeout=timeout)
        if status not in expected:
            raise EngineError(status, self._json_message(data, f"Docker Engine HTTP {status}"))
        return data

    def json_request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        expected: tuple[int, ...] = (200,),
        timeout: float | None = None,
    ) -> dict[str, Any]:
        data = self.request(method, path, payload, expected=expected, timeout=timeout)
        if not data:
            return {}
        try:
            result = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BrokerError("Docker Engine 返回了无效 JSON") from exc
        if not isinstance(result, dict):
            raise BrokerError("Docker Engine JSON 格式无效")
        return result


def _private_or_loopback_ipv4(value: str) -> str:
    try:
        address = ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError as exc:
        raise BrokerError("远程浏览器发布地址必须是明确的 IPv4 地址") from exc
    if not (address.is_loopback or any(address in network for network in PRIVATE_BIND_NETWORKS)):
        raise BrokerError("远程浏览器只能发布到回环、RFC1918 或 Tailscale IPv4 地址")
    return str(address)


def _safe_positive_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        parsed = default
    return min(max(parsed, minimum), maximum)


@dataclass(frozen=True)
class BrokerConfig:
    docker_socket: str
    owner_container: str
    broker_container: str
    worker_network: str
    worker_image: str
    bind_host: str
    public_host: str
    port_start: int
    port_end: int
    max_sessions: int
    memory_bytes: int
    nano_cpus: int
    pids_limit: int
    shm_bytes: int
    proxy_host: str
    control_dir: Path
    token_file: Path

    @classmethod
    def from_environment(cls) -> "BrokerConfig":
        """读取 Broker 配置。

        配置说明：worker 镜像、发布/公开地址、端口范围、并发上限及资源上限均可通过
        ``PROXY_BRIDGE_BROWSER_*`` 环境变量覆盖。内存、共享内存使用字节数，CPU 使用
        Docker NanoCPUs（10 亿表示 1 核）；Docker Socket 与控制卷目录固定在容器内部，
        owner 容器和 worker 网络属于 Compose 信任边界，非自定义部署不要修改。
        """
        image = str(os.environ.get("PROXY_BRIDGE_BROWSER_WORKER_IMAGE") or "lscr.io/linuxserver/chromium:latest")
        if IMAGE_PATTERN.fullmatch(image) is None:
            raise BrokerError("远程浏览器 worker 镜像名称无效")
        bind_host = _private_or_loopback_ipv4(
            str(os.environ.get("PROXY_BRIDGE_BROWSER_HOST") or "127.0.0.1").strip()
        )
        public_host = _private_or_loopback_ipv4(
            str(os.environ.get("PROXY_BRIDGE_BROWSER_PUBLIC_HOST") or bind_host).strip()
        )
        start = _safe_positive_int(os.environ.get("PROXY_BRIDGE_BROWSER_PORT_START"), 23000, 1024, 65535)
        end = _safe_positive_int(os.environ.get("PROXY_BRIDGE_BROWSER_PORT_END"), 23099, 1024, 65535)
        # 端口范围会由 Compose 整段发布到 Broker 容器；不得包含内部控制端口
        # 8765，否则会把 Bearer 控制 API 一并映射到宿主机。
        if end < start or end - start > 999 or start <= BROKER_CONTROL_PORT <= end:
            raise BrokerError("远程浏览器端口范围无效")
        owner = str(os.environ.get("PROXY_BRIDGE_OWNER_CONTAINER") or "proxybridge").strip()
        broker = str(os.environ.get("HOSTNAME") or "proxybridge-browser-broker").strip()
        network = str(os.environ.get("PROXY_BRIDGE_BROWSER_NETWORK") or "proxybridge-browser-workers").strip()
        proxy_host = str(os.environ.get("PROXY_BRIDGE_BROWSER_PROXY_HOST") or "proxybridge").strip()
        for label, value in (("owner", owner), ("broker", broker), ("network", network), ("proxy", proxy_host)):
            if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value) is None:
                raise BrokerError(f"远程浏览器 {label} 配置无效")
        control_dir = Path("/control")
        return cls(
            docker_socket="/var/run/docker.sock",
            owner_container=owner,
            broker_container=broker,
            worker_network=network,
            worker_image=image,
            bind_host=bind_host,
            public_host=public_host,
            port_start=start,
            port_end=end,
            max_sessions=_safe_positive_int(os.environ.get("PROXY_BRIDGE_BROWSER_MAX_SESSIONS"), 10, 1, 100),
            memory_bytes=_safe_positive_int(
                os.environ.get("PROXY_BRIDGE_BROWSER_MEMORY_BYTES"), 2 * 1024**3, 512 * 1024**2, 16 * 1024**3
            ),
            nano_cpus=_safe_positive_int(os.environ.get("PROXY_BRIDGE_BROWSER_NANO_CPUS"), 2_000_000_000, 250_000_000, 16_000_000_000),
            pids_limit=_safe_positive_int(os.environ.get("PROXY_BRIDGE_BROWSER_PIDS_LIMIT"), 512, 64, 4096),
            shm_bytes=_safe_positive_int(
                os.environ.get("PROXY_BRIDGE_BROWSER_SHM_BYTES"), 1024**3, 256 * 1024**2, 8 * 1024**3
            ),
            proxy_host=proxy_host,
            control_dir=control_dir,
            token_file=control_dir / "token",
        )


def _mount_source(inspect: dict[str, Any], destination: str) -> str:
    mount = _container_mount(inspect, destination)
    source = str(mount.get("Source") or "")
    if source.startswith("/"):
        return source.rstrip("/")
    raise BrokerError(f"容器缺少必要挂载：{destination}")


def _container_mount(inspect: dict[str, Any], destination: str) -> dict[str, Any]:
    mounts = inspect.get("Mounts")
    for mount in mounts if isinstance(mounts, list) else []:
        if isinstance(mount, dict) and str(mount.get("Destination") or "") == destination:
            return dict(mount)
    raise BrokerError(f"容器缺少必要挂载：{destination}")


def _named_volume(inspect: dict[str, Any], destination: str) -> str:
    mount = _container_mount(inspect, destination)
    name = str(mount.get("Name") or "")
    if str(mount.get("Type") or "") != "volume" or re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", name
    ) is None:
        raise BrokerError(
            f"远程浏览器要求 {destination} 使用 Docker 命名卷，不能使用宿主机 bind mount"
        )
    return name


def _profile_relative_path(value: Any) -> str:
    raw = str(value or "").replace("\\", "/").strip("/")
    parts = raw.split("/") if raw else []
    if len(parts) not in {2, 3, 4} or parts[0] != "browser_profiles":
        raise BrokerError("浏览器 Profile 相对路径无效")
    if len(parts) == 4 and parts[-1] != "chromium-profile":
        raise BrokerError("浏览器 Profile 兼容布局无效")
    if any(PROFILE_PART_PATTERN.fullmatch(part) is None or part in {".", ".."} for part in parts):
        raise BrokerError("浏览器 Profile 路径包含非法字符")
    return "/".join(parts)


def _fingerprint_relative_path(value: Any) -> str:
    raw = str(value or "").replace("\\", "/").strip("/")
    parts = raw.split("/") if raw else []
    if not 1 <= len(parts) <= 5 or parts[0] != "fingerprint-chromium":
        raise BrokerError("指纹内核相对路径无效")
    if any(PROFILE_PART_PATTERN.fullmatch(part) is None or part in {".", ".."} for part in parts):
        raise BrokerError("指纹内核路径包含非法字符")
    return "/".join(parts)


# 指纹参数白名单：与主服务 browser_fingerprint.py 的取值保持一致。Broker 是持有 Docker
# Socket 的安全边界，这里独立复核，确保 CHROME_CLI 中不会出现空白、引号或额外参数。
WORKER_PLATFORMS = frozenset({"auto", "windows", "macos", "linux"})
WORKER_BRANDS = frozenset({"", "Chrome", "Edge", "Opera", "Vivaldi"})
WORKER_SPOOFING_SWITCHES = ("font", "audio", "canvas", "clientrects", "gpu")
WORKER_HARDWARE_CONCURRENCY = frozenset({0, 2, 4, 6, 8, 10, 12, 16, 20, 24, 32})
WORKER_VERSION_PATTERN = re.compile(r"\d{1,4}(?:\.\d{1,6}){0,3}")
WORKER_WINDOW_SIZE_PATTERN = re.compile(r"(\d{3,4})x(\d{3,4})")
WORKER_EXTENSION_ID_PATTERN = re.compile(r"[a-f0-9]{16}")
WORKER_START_URL_PATTERN = re.compile(r"https?://[A-Za-z0-9._~:/?#\[\]@!$&()*+,;=%-]{1,2040}")
WORKER_PROFILE_MOUNT = "/config/browser-profile"


def _validate_worker_fingerprint(payload: dict[str, Any]) -> dict[str, Any]:
    platform = str(payload.get("platform") or "auto")
    if platform not in WORKER_PLATFORMS:
        raise BrokerError("远程浏览器平台参数无效")
    brand = str(payload.get("brand") or "")
    if brand not in WORKER_BRANDS:
        raise BrokerError("远程浏览器品牌参数无效")
    values: dict[str, Any] = {"platform": "linux" if platform == "auto" else platform, "brand": brand}
    for key in ("platform_version", "brand_version"):
        text = str(payload.get(key) or "")
        if text and WORKER_VERSION_PATTERN.fullmatch(text) is None:
            raise BrokerError(f"远程浏览器 {key} 参数无效")
        values[key] = text
    if not brand:
        values["brand_version"] = ""
    concurrency = payload.get("hardware_concurrency") or 0
    if isinstance(concurrency, bool) or not isinstance(concurrency, int) or concurrency not in WORKER_HARDWARE_CONCURRENCY:
        raise BrokerError("远程浏览器 CPU 核心数参数无效")
    values["hardware_concurrency"] = concurrency
    raw_spoofing = payload.get("disable_spoofing") or []
    if not isinstance(raw_spoofing, list) or any(item not in WORKER_SPOOFING_SWITCHES for item in raw_spoofing):
        raise BrokerError("远程浏览器关闭伪装项参数无效")
    values["disable_spoofing"] = [item for item in WORKER_SPOOFING_SWITCHES if item in raw_spoofing]
    window_size = str(payload.get("window_size") or "")
    match = WORKER_WINDOW_SIZE_PATTERN.fullmatch(window_size) if window_size else None
    if window_size and (
        match is None or not (640 <= int(match.group(1)) <= 7680 and 480 <= int(match.group(2)) <= 4320)
    ):
        raise BrokerError("远程浏览器窗口尺寸参数无效")
    values["window_size"] = window_size
    raw_extensions = payload.get("extension_ids") or []
    if (
        not isinstance(raw_extensions, list)
        or len(raw_extensions) > 20
        or any(not isinstance(item, str) or WORKER_EXTENSION_ID_PATTERN.fullmatch(item) is None for item in raw_extensions)
    ):
        raise BrokerError("远程浏览器扩展参数无效")
    values["extension_ids"] = list(dict.fromkeys(raw_extensions))
    raw_urls = payload.get("start_urls") or []
    if (
        not isinstance(raw_urls, list)
        or len(raw_urls) > 10
        or any(not isinstance(item, str) or WORKER_START_URL_PATTERN.fullmatch(item) is None for item in raw_urls)
    ):
        raise BrokerError("远程浏览器启动网址参数无效")
    values["start_urls"] = list(dict.fromkeys(raw_urls))
    return values


def _worker_fingerprint_switches(values: dict[str, Any]) -> list[str]:
    switches = [
        f"--fingerprint={values['fingerprint_seed']}",
        f"--fingerprint-platform={values.get('platform') or 'linux'}",
    ]
    if values.get("platform_version"):
        switches.append(f"--fingerprint-platform-version={values['platform_version']}")
    if values.get("brand"):
        switches.append(f"--fingerprint-brand={values['brand']}")
        if values.get("brand_version"):
            switches.append(f"--fingerprint-brand-version={values['brand_version']}")
    if values.get("hardware_concurrency"):
        switches.append(f"--fingerprint-hardware-concurrency={values['hardware_concurrency']}")
    if values.get("disable_spoofing"):
        switches.append("--disable-spoofing=" + ",".join(values["disable_spoofing"]))
    switches.extend(
        [
            f"--timezone={values['timezone']}",
            f"--lang={values['language']}",
            f"--accept-lang={values['accept_language']}",
        ]
    )
    if values.get("window_size"):
        width, height = values["window_size"].split("x", 1)
        switches.append(f"--window-size={width},{height}")
    if values.get("extension_ids"):
        # 扩展由主服务复制到 Profile 内 ProxyBridgeExtensions/<ID>/，随 Profile 挂载进 worker。
        switches.append(
            "--load-extension="
            + ",".join(f"{WORKER_PROFILE_MOUNT}/ProxyBridgeExtensions/{item}" for item in values["extension_ids"])
        )
    return switches


def _validate_start_payload(payload: dict[str, Any]) -> dict[str, Any]:
    instance_id = str(payload.get("instance_id") or "")
    if INSTANCE_ID_PATTERN.fullmatch(instance_id) is None:
        raise BrokerError("浏览器实例 ID 无效")
    operation_id = str(payload.get("operation_id") or "")
    if OPERATION_ID_PATTERN.fullmatch(operation_id) is None:
        raise BrokerError("浏览器启动操作 ID 无效")
    username = str(payload.get("username") or "proxybridge")
    if USERNAME_PATTERN.fullmatch(username) is None:
        raise BrokerError("远程浏览器用户名必须为 1–32 位字母、数字、点、下划线或短横线")
    password = str(payload.get("password") or "")
    if not 10 <= len(password) <= 128 or any(ord(char) < 32 or ord(char) == 127 for char in password):
        raise BrokerError("远程浏览器密码必须为 10–128 个可见字符")
    proxy_port = _safe_positive_int(payload.get("proxy_port"), 0, 0, 65535)
    if proxy_port <= 0:
        raise BrokerError("远程浏览器代理端口无效")
    seed = _safe_positive_int(payload.get("fingerprint_seed"), 1, 1, 0x7FFFFFFF)
    language = str(payload.get("language") or "en-US")
    accept_language = str(payload.get("accept_language") or "en-US,en;q=0.9")
    timezone = str(payload.get("timezone") or "UTC")
    for label, value in (("language", language), ("accept_language", accept_language), ("timezone", timezone)):
        if re.fullmatch(r"[A-Za-z0-9,;=._/+:-]{1,100}", value) is None:
            raise BrokerError(f"远程浏览器 {label} 参数无效")
    return {
        "instance_id": instance_id,
        "operation_id": operation_id,
        "profile_rel": _profile_relative_path(payload.get("profile_rel")),
        "fingerprint_rel": _fingerprint_relative_path(payload.get("fingerprint_rel")),
        "username": username,
        "password": password,
        "proxy_port": proxy_port,
        "fingerprint_seed": seed,
        "language": language,
        "accept_language": accept_language,
        "timezone": timezone,
        **_validate_worker_fingerprint(payload),
    }


class BrowserWorkerBroker:
    def __init__(
        self,
        config: BrokerConfig,
        engine: DockerEngineClient,
        *,
        relay_factory: Callable[[int, str], WorkerTCPRelay] = WorkerTCPRelay,
    ) -> None:
        self.config = config
        self.engine = engine
        self.relay_factory = relay_factory
        self.lock = threading.RLock()
        # 镜像拉取最长可达 15 分钟，但它不会创建公开 worker。与容器生命周期锁
        # 分离后，SIGTERM 可以立即进入 worker 回收，不必等待无副作用的 pull。
        self.image_lock = threading.Lock()
        self.stopping = threading.Event()
        self._owner_inspect: dict[str, Any] | None = None
        self._broker_inspect: dict[str, Any] | None = None
        self._cancelled_operation_lock = threading.Lock()
        self._cancelled_operations: dict[tuple[str, str], float] = {}
        self._relay_lock = threading.RLock()
        self._relays: dict[str, WorkerTCPRelay] = {}

    def _start_relay(self, instance_id: str, port: int, worker_ip: str) -> None:
        try:
            target = ipaddress.IPv4Address(worker_ip)
        except ipaddress.AddressValueError as exc:
            raise BrokerError("远程浏览器 worker 缺少有效的中继目标地址") from exc
        if not any(target in network for network in PRIVATE_WORKER_NETWORKS):
            raise BrokerError("远程浏览器中继目标必须是 RFC1918 worker 地址")
        if not self.config.port_start <= port <= self.config.port_end:
            raise BrokerError("远程浏览器中继端口超出已配置范围")
        self._stop_relay(instance_id)
        relay = self.relay_factory(port, str(target))
        try:
            relay.start()
        except BaseException:
            relay.stop()
            raise
        with self._relay_lock:
            self._relays[instance_id] = relay

    def _stop_relay(self, instance_id: str) -> None:
        with self._relay_lock:
            relay = self._relays.pop(instance_id, None)
        if relay is None:
            return
        try:
            relay.stop()
        except Exception as exc:
            raise BrokerError("无法停止远程浏览器端口中继") from exc

    def _stop_all_relays(self) -> list[Exception]:
        with self._relay_lock:
            relays = list(self._relays.values())
            self._relays.clear()
        failures: list[Exception] = []
        for relay in relays:
            try:
                relay.stop()
            except Exception as exc:
                failures.append(exc)
        return failures

    def _cancel_operation(self, instance_id: str, operation_id: str) -> None:
        if not operation_id:
            return
        if OPERATION_ID_PATTERN.fullmatch(operation_id) is None:
            raise BrokerError("浏览器启动操作 ID 无效")
        cutoff = time.monotonic() - 20 * 60
        with self._cancelled_operation_lock:
            self._cancelled_operations = {
                key: created_at
                for key, created_at in self._cancelled_operations.items()
                if created_at >= cutoff
            }
            self._cancelled_operations[(instance_id, operation_id)] = time.monotonic()

    def _raise_if_operation_cancelled(self, instance_id: str, operation_id: str) -> None:
        with self._cancelled_operation_lock:
            cancelled = self._cancelled_operations.get((instance_id, operation_id))
        if cancelled is not None:
            raise BrokerError("远程浏览器启动已被停止操作取消")

    def ensure_token(self) -> str:
        # 在任何凭证写盘前先确认 /control 确实来自私有命名卷，避免错误部署
        # 把 Broker token 或后续 worker 密码落到宿主机 bind 目录。
        self._control_volume_source()
        self.config.control_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            existing = self.config.token_file.read_text(encoding="utf-8").strip()
        except OSError:
            existing = ""
        if re.fullmatch(r"[A-Za-z0-9._~-]{32,256}", existing):
            self.config.token_file.chmod(0o444)
            return existing
        token = secrets.token_urlsafe(48)
        temporary = self.config.token_file.with_suffix(".tmp")
        temporary.write_text(token, encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(self.config.token_file)
        # ProxyBridge mounts this private volume read-only and runs as UID 10001.
        self.config.token_file.chmod(0o444)
        return token

    def token_matches(self, provided: str) -> bool:
        expected = self.ensure_token()
        return bool(provided and hmac.compare_digest(provided, expected))

    def _inspect_container(
        self,
        name: str,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        return self.engine.json_request(
            "GET",
            f"/containers/{urllib.parse.quote(name, safe='')}/json",
            timeout=timeout,
        )

    def _owner(self) -> dict[str, Any]:
        if self._owner_inspect is None:
            self._owner_inspect = self._inspect_container(self.config.owner_container)
        return self._owner_inspect

    def _broker(self) -> dict[str, Any]:
        if self._broker_inspect is None:
            self._broker_inspect = self._inspect_container(self.config.broker_container)
        return self._broker_inspect

    def _data_volume(self) -> str:
        """返回 owner 的数据命名卷，禁止把主服务可控路径交给 Docker bind mount。"""
        return _named_volume(self._owner(), "/data")

    def _control_volume_source(self) -> str:
        """返回 Broker 私有控制卷的宿主路径，并拒绝宿主机 bind mount。"""
        broker = self._broker()
        _named_volume(broker, str(self.config.control_dir))
        return _mount_source(broker, str(self.config.control_dir))

    def _validated_worker_network(self) -> dict[str, Any]:
        """校验 worker 隔离网络以及 Broker 的中继网络与宿主端口。"""
        network = self.engine.json_request(
            "GET",
            f"/networks/{urllib.parse.quote(self.config.worker_network, safe='')}",
        )
        if (
            network.get("Internal") is not True
            or str(network.get("Driver") or "") != "bridge"
            or str(network.get("Scope") or "") != "local"
        ):
            raise BrokerError("远程浏览器 worker 网络必须是 internal=true 的本地 bridge 网络")

        ipam = network.get("IPAM") if isinstance(network.get("IPAM"), dict) else {}
        ipam_config = ipam.get("Config") if isinstance(ipam.get("Config"), list) else []
        private_subnets: list[ipaddress.IPv4Network] = []
        for item in ipam_config:
            subnet = str(item.get("Subnet") or "") if isinstance(item, dict) else ""
            try:
                parsed = ipaddress.IPv4Network(subnet, strict=False)
            except ValueError:
                raise BrokerError("远程浏览器 worker 网络必须配置私有 IPv4 IPAM") from None
            if not any(parsed.subnet_of(allowed) for allowed in PRIVATE_WORKER_NETWORKS):
                raise BrokerError("远程浏览器 worker 网络 IPAM 只能使用 RFC1918 IPv4 子网")
            private_subnets.append(parsed)
        if not private_subnets:
            raise BrokerError("远程浏览器 worker 网络必须配置私有 IPv4 IPAM")

        owner = self._inspect_container(self.config.owner_container)
        self._owner_inspect = owner
        owner_id = str(owner.get("Id") or "")
        owner_name = str(owner.get("Name") or "").lstrip("/")
        network_id = str(network.get("Id") or "")
        network_settings = (
            owner.get("NetworkSettings")
            if isinstance(owner.get("NetworkSettings"), dict)
            else {}
        )
        owner_networks = (
            network_settings.get("Networks")
            if isinstance(network_settings.get("Networks"), dict)
            else {}
        )
        attachment = (
            owner_networks.get(self.config.worker_network)
            if isinstance(owner_networks.get(self.config.worker_network), dict)
            else {}
        )
        if (
            not owner_id
            or not owner_name
            or not attachment
            or not network_id
            or str(attachment.get("NetworkID") or "") != network_id
        ):
            raise BrokerError("ProxyBridge owner 容器未加入远程浏览器 worker 网络")

        try:
            owner_ip = ipaddress.IPv4Address(str(attachment.get("IPAddress") or ""))
        except ValueError:
            raise BrokerError("ProxyBridge owner 容器缺少有效的 worker 网络 IPv4 地址") from None
        if not any(owner_ip in subnet for subnet in private_subnets):
            raise BrokerError("ProxyBridge owner 容器的 worker 网络地址不属于私有 IPAM")

        containers = (
            network.get("Containers")
            if isinstance(network.get("Containers"), dict)
            else {}
        )
        owner_endpoint = containers.get(owner_id) if isinstance(containers.get(owner_id), dict) else {}
        try:
            endpoint_ip = ipaddress.IPv4Interface(
                str(owner_endpoint.get("IPv4Address") or "")
            ).ip
        except ValueError:
            raise BrokerError("worker 网络未记录 ProxyBridge owner 容器端点") from None
        if endpoint_ip != owner_ip or str(owner_endpoint.get("Name") or "") != owner_name:
            raise BrokerError("worker 网络中的 ProxyBridge owner 容器端点不匹配")

        resolvable_names = {owner_name.casefold()}
        for key in ("Aliases", "DNSNames"):
            values = attachment.get(key)
            for value in values if isinstance(values, list) else []:
                normalized = str(value or "").strip().casefold()
                if normalized:
                    resolvable_names.add(normalized)
        if self.config.proxy_host.casefold() not in resolvable_names:
            raise BrokerError("远程浏览器代理主机名不是 ProxyBridge owner 的网络名称或别名")

        broker = self._broker()
        broker_network_settings = (
            broker.get("NetworkSettings")
            if isinstance(broker.get("NetworkSettings"), dict)
            else {}
        )
        broker_networks = (
            broker_network_settings.get("Networks")
            if isinstance(broker_network_settings.get("Networks"), dict)
            else {}
        )
        broker_attachment = (
            broker_networks.get(self.config.worker_network)
            if isinstance(broker_networks.get(self.config.worker_network), dict)
            else {}
        )
        if (
            not broker_attachment
            or str(broker_attachment.get("NetworkID") or "") != network_id
        ):
            raise BrokerError("Browser Broker 容器未加入远程浏览器 worker 网络")
        try:
            broker_ip = ipaddress.IPv4Address(str(broker_attachment.get("IPAddress") or ""))
        except ValueError:
            raise BrokerError("Browser Broker 缺少有效的 worker 网络 IPv4 地址") from None
        if not any(broker_ip in subnet for subnet in private_subnets):
            raise BrokerError("Browser Broker 的 worker 网络地址不属于私有 IPAM")

        # 只检查 HostConfig 会误把 internal 网络中未实际生效的 PortBindings 当作
        # 可访问入口；NetworkSettings.Ports 才反映 Docker 真正建立的宿主机映射。
        published_ports = (
            broker_network_settings.get("Ports")
            if isinstance(broker_network_settings.get("Ports"), dict)
            else {}
        )
        for port in range(self.config.port_start, self.config.port_end + 1):
            bindings = published_ports.get(f"{port}/tcp")
            if not isinstance(bindings, list) or not any(
                isinstance(binding, dict)
                and str(binding.get("HostIp") or "") == self.config.bind_host
                and _safe_positive_int(binding.get("HostPort"), 0, 0, 65535) == port
                for binding in bindings
            ):
                raise BrokerError(
                    f"Browser Broker 未实际发布 {self.config.bind_host}:{port}；"
                    "请使用最新 compose.yaml 重新创建服务"
                )
        return network

    def _require_volume_subpath_api(self) -> None:
        version = self.engine.api_version()
        try:
            parsed = tuple(int(part) for part in version.split(".", 1))
        except ValueError as exc:
            raise BrokerError("Docker Engine API 版本无效") from exc
        if parsed < MIN_VOLUME_SUBPATH_API:
            required = ".".join(str(part) for part in MIN_VOLUME_SUBPATH_API)
            raise BrokerError(
                f"远程浏览器安全挂载要求 Docker Engine API {required} 或更高，当前为 {version}"
            )

    def _secret_paths(self, instance_id: str) -> tuple[Path, str]:
        session_dir = self.config.control_dir / "sessions"
        try:
            session_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError as exc:
            raise BrokerError("无法创建远程浏览器凭证目录") from exc
        local = session_dir / f"{instance_id}.password"
        control_source = self._control_volume_source()
        host = posixpath.join(control_source, "sessions", f"{instance_id}.password")
        return local, host

    def _write_secret(self, instance_id: str, password: str) -> str:
        local, host = self._secret_paths(instance_id)
        temporary = local.with_suffix(".tmp")
        try:
            temporary.write_text(password, encoding="utf-8")
            temporary.chmod(0o600)
            temporary.replace(local)
        except OSError as exc:
            try:
                temporary.unlink()
            except OSError:
                pass
            raise BrokerError("无法写入远程浏览器临时凭证") from exc
        return host

    def _read_secret(self, instance_id: str) -> str:
        """读取运行 Worker 的临时密码；内容只允许通过认证控制接口按需返回。"""
        local, _host = self._secret_paths(instance_id)
        try:
            with local.open("r", encoding="utf-8") as handle:
                password = handle.read(129)
                overflow = handle.read(1)
        except (OSError, UnicodeError) as exc:
            raise BrokerError("远程浏览器访问凭证已不存在，请重新启动该记录") from exc
        if (
            overflow
            or not 10 <= len(password) <= 128
            or any(ord(char) < 32 or ord(char) == 127 for char in password)
        ):
            raise BrokerError("远程浏览器访问凭证格式无效，请重新启动该记录")
        return password

    def _worker_init_script_path(self) -> str:
        init_dir = self.config.control_dir / "worker-init"
        local = init_dir / "10-proxybridge-fingerprint"
        try:
            init_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            if not local.exists() or local.read_text(encoding="utf-8") != WORKER_INIT_SCRIPT:
                temporary = local.with_suffix(".tmp")
                temporary.write_text(WORKER_INIT_SCRIPT, encoding="utf-8")
                temporary.chmod(0o755)
                temporary.replace(local)
            else:
                local.chmod(0o755)
        except OSError as exc:
            raise BrokerError("无法准备远程浏览器初始化脚本") from exc
        control_source = self._control_volume_source()
        return posixpath.join(control_source, "worker-init", local.name)

    def _remove_secret(self, instance_id: str) -> None:
        local, _host = self._secret_paths(instance_id)
        try:
            local.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise BrokerError("无法清理远程浏览器临时凭证") from exc

    def _list_raw(self, *, timeout: float | None = None) -> list[dict[str, Any]]:
        filters = urllib.parse.quote(json.dumps({"label": [f"{LABEL_WORKER}=1", f"{LABEL_OWNER}={self.config.owner_container}"]}))
        data = self.engine.request(
            "GET",
            f"/containers/json?all=1&filters={filters}",
            timeout=timeout,
        )
        try:
            payload = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BrokerError("Docker worker 列表响应无效") from exc
        return [dict(item) for item in payload if isinstance(item, dict)] if isinstance(payload, list) else []

    def _session_from_inspect(self, inspect: dict[str, Any]) -> dict[str, Any]:
        config = inspect.get("Config") if isinstance(inspect.get("Config"), dict) else {}
        labels = config.get("Labels") if isinstance(config.get("Labels"), dict) else {}
        state = inspect.get("State") if isinstance(inspect.get("State"), dict) else {}
        network = inspect.get("NetworkSettings") if isinstance(inspect.get("NetworkSettings"), dict) else {}
        # internal worker 网络不会真正发布 Docker HostPort。公开端口由 Broker
        # TCP 中继监听，因此以创建时写入且随容器不可变的标签为准。
        host_port = _safe_positive_int(labels.get(LABEL_PUBLIC_PORT), 0, 0, 65535)
        running = bool(state.get("Running"))
        health = state.get("Health") if isinstance(state.get("Health"), dict) else {}
        health_status = str(health.get("Status") or "")
        if not running:
            status = "stopped"
        elif health_status == "healthy" or not health_status:
            status = "running"
        elif health_status == "unhealthy":
            status = "failed"
        else:
            status = "starting"
        instance_id = str(labels.get(LABEL_INSTANCE) or "")
        proxy_port = _safe_positive_int(labels.get(LABEL_PROXY_PORT), 0, 0, 65535)
        networks = network.get("Networks") if isinstance(network.get("Networks"), dict) else {}
        worker_network = (
            networks.get(self.config.worker_network)
            if isinstance(networks.get(self.config.worker_network), dict)
            else {}
        )
        worker_ip = str(worker_network.get("IPAddress") or "")
        try:
            worker_ip = str(ipaddress.IPv4Address(worker_ip))
        except ipaddress.AddressValueError:
            worker_ip = ""
        return {
            "instance_id": instance_id,
            "container_id": str(inspect.get("Id") or "")[:64],
            "container_name": str(inspect.get("Name") or "").lstrip("/"),
            "status": status,
            "host_port": host_port or None,
            "url": f"https://{self.config.public_host}:{host_port}/" if running and host_port else "",
            "worker_ip": worker_ip,
            "proxy_port": proxy_port or None,
            "health": health_status or None,
            "fingerprint_chromium": True,
        }

    def sessions(self, *, timeout: float | None = None) -> list[dict[str, Any]]:
        summaries = self._list_raw(timeout=timeout)

        def inspect_summary(summary: dict[str, Any]) -> dict[str, Any] | None:
            container_id = str(summary.get("Id") or "")
            if not container_id:
                return None
            try:
                inspect = self._inspect_container(container_id, timeout=timeout)
            except EngineError as exc:
                if exc.status == 404:
                    return None
                raise
            session = self._session_from_inspect(inspect)
            if INSTANCE_ID_PATTERN.fullmatch(str(session.get("instance_id") or "")):
                return session
            return None

        result: list[dict[str, Any]] = []
        if summaries:
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(len(summaries), self.config.max_sessions, 100)
            ) as executor:
                futures = [executor.submit(inspect_summary, summary) for summary in summaries]
                for future in concurrent.futures.as_completed(futures):
                    session = future.result()
                    if session is not None:
                        result.append(session)
        relay_instances = {
            str(session.get("instance_id") or "")
            for session in result
            if session.get("status") in RELAY_ACTIVE_STATUSES
        }
        with self._relay_lock:
            stale_relays = [
                instance_id
                for instance_id in self._relays
                if instance_id not in relay_instances
            ]
        for instance_id in stale_relays:
            self._stop_relay(instance_id)
        return result

    def _worker_image_ready(self) -> bool:
        encoded = urllib.parse.quote(self.config.worker_image, safe="")
        try:
            current = self.engine.json_request("GET", f"/images/{encoded}/json")
        except EngineError as exc:
            if exc.status == 404:
                return False
            raise
        return (
            str(current.get("Os") or "").lower() == "linux"
            and str(current.get("Architecture") or "").lower() == "amd64"
        )

    def _ensure_image(self) -> None:
        if self._worker_image_ready():
            return

        image = self.config.worker_image
        encoded = urllib.parse.quote(image, safe="")
        query_values = {"platform": WORKER_PLATFORM}
        if "@" in image:
            query_values["fromImage"] = image
        else:
            slash = image.rfind("/")
            colon = image.rfind(":")
            if colon > slash:
                query_values["fromImage"] = image[:colon]
                query_values["tag"] = image[colon + 1 :]
            else:
                query_values["fromImage"] = image
                query_values["tag"] = "latest"
        query = urllib.parse.urlencode(query_values)
        started = time.monotonic()
        print(
            "ProxyBridge browser broker pulling worker image "
            f"{image} ({WORKER_PLATFORM}), timeout={int(WORKER_IMAGE_PULL_TIMEOUT)}s",
            flush=True,
        )
        try:
            try:
                data = self.engine.request(
                    "POST",
                    f"/images/create?{query}",
                    expected=(200,),
                    timeout=WORKER_IMAGE_PULL_TIMEOUT,
                )
            except EngineTimeoutError as exc:
                raise BrokerError(
                    "拉取远程浏览器 Worker 镜像超过 15 分钟；请检查镜像仓库网络，"
                    "或在 Docker 宿主机预先 pull/load linux/amd64 镜像后重试"
                ) from exc
            for line in data.splitlines():
                try:
                    event = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if isinstance(event, dict) and event.get("error"):
                    raise BrokerError(f"拉取远程浏览器镜像失败：{str(event['error'])[:500]}")
            pulled = self.engine.json_request("GET", f"/images/{encoded}/json")
            if not (
                str(pulled.get("Os") or "").lower() == "linux"
                and str(pulled.get("Architecture") or "").lower() == "amd64"
            ):
                raise BrokerError("远程浏览器镜像不是 linux/amd64，请检查 Docker 平台模拟配置")
        except BaseException as exc:
            elapsed = max(0.0, time.monotonic() - started)
            print(
                f"ProxyBridge browser broker worker image pull failed after {elapsed:.1f}s: {exc}",
                flush=True,
            )
            raise
        elapsed = max(0.0, time.monotonic() - started)
        print(
            f"ProxyBridge browser broker worker image ready after {elapsed:.1f}s: {image}",
            flush=True,
        )

    def _existing_session(
        self,
        instance_id: str,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any] | None:
        for session in self.sessions(timeout=timeout):
            if session.get("instance_id") == instance_id:
                return session
        return None

    def _assert_owned_worker(
        self,
        inspect: dict[str, Any],
        *,
        instance_id: str | None = None,
    ) -> None:
        config = inspect.get("Config") if isinstance(inspect.get("Config"), dict) else {}
        labels = config.get("Labels") if isinstance(config.get("Labels"), dict) else {}
        valid = (
            labels.get(LABEL_WORKER) == "1"
            and labels.get(LABEL_OWNER) == self.config.owner_container
            and (
                instance_id is None
                or labels.get(LABEL_INSTANCE) == instance_id
            )
        )
        if not valid:
            raise BrokerError("同名容器不属于当前 ProxyBridge 浏览器实例，已拒绝操作")

    def _container_name(self, instance_id: str) -> str:
        return f"proxybridge-browser-{instance_id}"

    def _worker_payload(self, values: dict[str, Any], port: int, secret_host_path: str) -> dict[str, Any]:
        data_volume = self._data_volume()
        init_script_host_path = self._worker_init_script_path()
        cli = " ".join(
            (
                f"--proxy-server=socks5://{self.config.proxy_host}:{values['proxy_port']}",
                f"--user-data-dir={WORKER_PROFILE_MOUNT}",
                *_worker_fingerprint_switches(values),
                "--webrtc-ip-handling-policy=disable_non_proxied_udp",
                "--no-first-run",
                "--no-default-browser-check",
                *(values.get("start_urls") or ["https://ip.skk.moe/"]),
            )
        )
        return {
            "Image": self.config.worker_image,
            "Env": [
                "PUID=10001",
                "PGID=10001",
                "TZ=Etc/UTC",
                # false 强制使用 LinuxServer 的 Xvfb/Openbox 稳定路径；true 会
                # 启用 labwc/Wayland，部分 fingerprint-chromium 版本可触发
                # labwc SIGABRT，最终导致 worker HTTPS 初始化超时。
                "PIXELFLUX_WAYLAND=false",
                "HARDEN_DESKTOP=true",
                "RESTART_APP=true",
                "SELKIES_ENABLE_SHARING=false",
                f"CUSTOM_USER={values['username']}",
                "FILE__PASSWORD=/run/secrets/proxybridge-browser-password",
                f"CHROME_CLI={cli}",
            ],
            "Labels": {
                LABEL_WORKER: "1",
                LABEL_INSTANCE: values["instance_id"],
                LABEL_OWNER: self.config.owner_container,
                LABEL_PROXY_PORT: str(values["proxy_port"]),
                LABEL_PUBLIC_PORT: str(port),
            },
            # /config 使用随容器删除的匿名卷，避免把 Selkies TLS/认证运行文件混入
            # ProxyBridge Profile 与 WebDAV；只有 Chromium 用户数据挂入命名卷子路径。
            "Volumes": {"/config": {}},
            "ExposedPorts": {"3001/tcp": {}},
            "Healthcheck": {
                "Test": [
                    "CMD-SHELL",
                    (
                        "bash -c 'command -v curl >/dev/null && "
                        "unauth=\"$(curl --insecure --silent --output /dev/null "
                        "--write-out \"%{http_code}\" https://127.0.0.1:3001/)\" && "
                        "[[ \"$unauth\" == \"401\" ]] && "
                        "curl --insecure --silent --show-error --fail --output /dev/null "
                        "--user \"$CUSTOM_USER:$(< /run/secrets/proxybridge-browser-password)\" "
                        "https://127.0.0.1:3001/'"
                    ),
                ],
                "Interval": 2_000_000_000,
                "Timeout": 2_000_000_000,
                "StartPeriod": 5_000_000_000,
                "Retries": 45,
            },
            "HostConfig": {
                "Binds": [
                    f"{init_script_host_path}:/custom-cont-init.d/10-proxybridge-fingerprint:ro",
                    f"{secret_host_path}:/run/secrets/proxybridge-browser-password:ro",
                ],
                "Mounts": [
                    {
                        "Type": "volume",
                        "Source": data_volume,
                        "Target": "/config/browser-profile",
                        "ReadOnly": False,
                        "VolumeOptions": {"NoCopy": True, "Subpath": values["profile_rel"]},
                    },
                    {
                        "Type": "volume",
                        "Source": data_volume,
                        "Target": "/opt/fingerprint-chromium",
                        "ReadOnly": True,
                        "VolumeOptions": {"NoCopy": True, "Subpath": values["fingerprint_rel"]},
                    },
                ],
                # worker 只加入 internal 网络，Docker 会忽略或拒绝其 HostPort。
                # 宿主机端口由 Broker 容器的固定映射和进程内 TCP 中继提供；这里
                # 不得再增加 PortBindings，否则会产生“配置存在但端口未监听”的假象。
                "NetworkMode": self.config.worker_network,
                "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0},
                "AutoRemove": False,
                "Privileged": False,
                "SecurityOpt": ["no-new-privileges:true"],
                "Memory": self.config.memory_bytes,
                "NanoCpus": self.config.nano_cpus,
                "PidsLimit": self.config.pids_limit,
                "ShmSize": self.config.shm_bytes,
                # LinuxServer Chromium 自带 s6-overlay，必须让镜像的 /init 保持
                # PID 1。Docker Init 会在外层插入 tini，使 s6-overlay-suexec
                # 以 "can only run as pid 1" 和退出码 100 终止。
            },
        }

    def _remove_container(
        self,
        identifier: str,
        *,
        force: bool = True,
        timeout: float = 60.0,
    ) -> None:
        # v=1 同时删除 /config 的匿名运行卷；Profile 所在命名卷不受影响。
        query = urllib.parse.urlencode({"force": "1" if force else "0", "v": "1"})
        try:
            self.engine.request(
                "DELETE",
                f"/containers/{urllib.parse.quote(identifier, safe='')}?{query}",
                expected=(204,),
                timeout=timeout,
            )
        except EngineError as exc:
            if exc.status != 404:
                raise

    def _stop_and_remove_owned(
        self,
        inspect: dict[str, Any],
        *,
        instance_id: str | None = None,
        force_on_failure: bool = False,
        stop_timeout: float = 15.0,
        remove_timeout: float = 60.0,
    ) -> None:
        self._assert_owned_worker(inspect, instance_id=instance_id)
        identifier = str(inspect.get("Id") or "")
        if not identifier:
            raise BrokerError("浏览器 worker 容器 ID 无效")
        state = inspect.get("State") if isinstance(inspect.get("State"), dict) else {}
        try:
            if state.get("Running"):
                self.engine.request(
                    "POST",
                    f"/containers/{urllib.parse.quote(identifier, safe='')}/stop?t=10",
                    expected=(204, 304),
                    timeout=stop_timeout,
                )
            self._remove_container(identifier, force=False, timeout=remove_timeout)
        except (BrokerError, EngineError):
            if not force_on_failure:
                raise
            self._remove_container(identifier, force=True, timeout=remove_timeout)

    def _wait_until_ready(
        self,
        container_id: str,
        *,
        instance_id: str,
        operation_id: str,
        timeout: float = 100.0,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            if self.stopping.is_set():
                raise BrokerError("浏览器 Broker 正在退出")
            self._raise_if_operation_cancelled(instance_id, operation_id)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BrokerError("等待远程浏览器 HTTPS 服务就绪超时")
            inspect = self._inspect_container(
                container_id,
                timeout=max(1.0, min(10.0, remaining)),
            )
            self._assert_owned_worker(inspect)
            state = inspect.get("State") if isinstance(inspect.get("State"), dict) else {}
            if not state.get("Running"):
                detail = str(state.get("Error") or "").strip()
                exit_code = _safe_positive_int(state.get("ExitCode"), 0, 0, 255)
                suffix = f"（退出码 {exit_code}{f'：{detail}' if detail else ''}）"
                raise BrokerError(f"远程浏览器 worker 初始化期间退出{suffix}")
            health = state.get("Health") if isinstance(state.get("Health"), dict) else {}
            health_status = str(health.get("Status") or "")
            session = self._session_from_inspect(inspect)
            if health_status == "healthy" and session.get("url") and session.get("worker_ip"):
                return session
            if health_status == "unhealthy":
                raise BrokerError("远程浏览器 HTTPS 服务健康检查失败")
            time.sleep(0.5)

    @staticmethod
    def _port_conflict(error: BaseException) -> bool:
        message = str(error).casefold()
        return any(
            phrase in message
            for phrase in (
                "port is already allocated",
                "address already in use",
                "port is already in use",
                "端口已被占用",
            )
        )

    def prepare(self) -> dict[str, Any]:
        """在生命周期操作前准备镜像；本阶段不接收或保存远程访问密码。"""
        if self.stopping.is_set():
            raise BrokerError("浏览器 Broker 正在退出")
        self._require_volume_subpath_api()
        self._validated_worker_network()
        self._data_volume()
        with self.image_lock:
            if self.stopping.is_set():
                raise BrokerError("浏览器 Broker 正在退出")
            self._ensure_image()
        if self.stopping.is_set():
            raise BrokerError("浏览器 Broker 正在退出")
        return {"ok": True, "worker_image": self.config.worker_image, "platform": WORKER_PLATFORM}

    def start(self, payload: dict[str, Any]) -> dict[str, Any]:
        values = _validate_start_payload(payload)
        self._raise_if_operation_cancelled(values["instance_id"], values["operation_id"])
        if self.stopping.is_set():
            raise BrokerError("浏览器 Broker 正在退出")
        self._require_volume_subpath_api()
        with self.image_lock:
            if self.stopping.is_set():
                raise BrokerError("浏览器 Broker 正在退出")
            self._raise_if_operation_cancelled(values["instance_id"], values["operation_id"])
            self._ensure_image()
        # 只读 Docker 元数据检查和初始化脚本准备也放在生命周期锁外；即使 Engine
        # 暂时无响应，Broker 退出仍可先回收已公开的 worker。
        self._validated_worker_network()
        self._data_volume()
        self._worker_init_script_path()
        if self.stopping.is_set():
            raise BrokerError("浏览器 Broker 正在退出")
        self._raise_if_operation_cancelled(values["instance_id"], values["operation_id"])
        with self.lock:
            if self.stopping.is_set():
                raise BrokerError("浏览器 Broker 正在退出")
            self._raise_if_operation_cancelled(values["instance_id"], values["operation_id"])
            self._require_volume_subpath_api()
            existing = self._existing_session(values["instance_id"], timeout=5.0)
            if existing:
                # 每次启动都重建 worker，确保本次提交的新用户名/密码真正生效。
                self._stop_relay(values["instance_id"])
                existing_inspect = self._inspect_container(
                    str(existing.get("container_id") or ""),
                    timeout=5.0,
                )
                self._stop_and_remove_owned(
                    existing_inspect,
                    instance_id=values["instance_id"],
                    force_on_failure=True,
                    remove_timeout=15.0,
                )
                self._remove_secret(values["instance_id"])
            sessions = [
                item
                for item in self.sessions(timeout=5.0)
                if item.get("status") in LIVE_WORKER_STATUSES
            ]
            if len(sessions) >= self.config.max_sessions:
                raise BrokerError(f"远程浏览器并发已达到上限（{self.config.max_sessions}）")
            if self.stopping.is_set():
                raise BrokerError("浏览器 Broker 正在退出")
            self._raise_if_operation_cancelled(values["instance_id"], values["operation_id"])
            secret_host_path = self._write_secret(values["instance_id"], values["password"])
            keep_secret = False
            try:
                used_ports = {int(item["host_port"]) for item in sessions if item.get("host_port")}
                last_error: Exception | None = None
                for port in range(self.config.port_start, self.config.port_end + 1):
                    if port in used_ports:
                        continue
                    self._raise_if_operation_cancelled(
                        values["instance_id"],
                        values["operation_id"],
                    )
                    name = self._container_name(values["instance_id"])
                    create_path = (
                        "/containers/create?"
                        + urllib.parse.urlencode({"name": name, "platform": WORKER_PLATFORM})
                    )
                    container_id = ""
                    try:
                        created = self.engine.json_request(
                            "POST",
                            create_path,
                            self._worker_payload(values, port, secret_host_path),
                            expected=(201,),
                            timeout=60.0,
                        )
                        container_id = str(created.get("Id") or "")
                        if not container_id:
                            raise BrokerError("Docker Engine 未返回 worker 容器 ID")
                        if self.stopping.is_set():
                            self._remove_container(container_id, timeout=20.0)
                            raise BrokerError("浏览器 Broker 正在退出")
                        self._raise_if_operation_cancelled(
                            values["instance_id"],
                            values["operation_id"],
                        )
                        try:
                            self.engine.request(
                                "POST",
                                f"/containers/{urllib.parse.quote(container_id, safe='')}/start",
                                expected=(204, 304),
                                timeout=60.0,
                            )
                        except BaseException:
                            self._remove_container(
                                container_id,
                                timeout=20.0 if self.stopping.is_set() else 60.0,
                            )
                            raise
                        session = self._wait_until_ready(
                            container_id,
                            instance_id=values["instance_id"],
                            operation_id=values["operation_id"],
                        )
                        self._start_relay(
                            values["instance_id"],
                            int(session["host_port"]),
                            str(session["worker_ip"]),
                        )
                        self._raise_if_operation_cancelled(
                            values["instance_id"],
                            values["operation_id"],
                        )
                        keep_secret = True
                        return {"ok": True, **session}
                    except (BrokerError, EngineError) as exc:
                        last_error = exc
                        self._stop_relay(values["instance_id"])
                        if container_id:
                            try:
                                failed_inspect = self._inspect_container(
                                    container_id,
                                    timeout=10.0 if self.stopping.is_set() else 30.0,
                                )
                                self._stop_and_remove_owned(
                                    failed_inspect,
                                    instance_id=values["instance_id"],
                                    force_on_failure=True,
                                    remove_timeout=20.0 if self.stopping.is_set() else 60.0,
                                )
                            except EngineError as cleanup_error:
                                if cleanup_error.status != 404:
                                    raise BrokerError("无法回收启动失败的浏览器 worker") from cleanup_error
                        if not self._port_conflict(exc):
                            raise
                raise BrokerError(
                    f"没有可用的远程浏览器端口：{last_error or '端口范围已用完'}"
                )
            finally:
                if not keep_secret:
                    self._remove_secret(values["instance_id"])

    def stop(self, instance_id: str, cancelled_operation_id: str = "") -> dict[str, Any]:
        if INSTANCE_ID_PATTERN.fullmatch(instance_id) is None:
            raise BrokerError("浏览器实例 ID 无效")
        # This tombstone is recorded before taking the Docker lifecycle lock. If stop
        # wins the HTTP race, a late start carrying the same one-time operation ID is
        # rejected instead of creating a worker after stop already returned.
        self._cancel_operation(instance_id, cancelled_operation_id)
        with self.lock:
            existing = self._existing_session(instance_id, timeout=5.0)
            if not existing:
                self._stop_relay(instance_id)
                self._remove_secret(instance_id)
                return {"ok": True, "stopped": 0, "instance_id": instance_id}
            # 先关闭公开入口，再优雅停止 worker，避免清理 Profile 时仍有新连接进入。
            self._stop_relay(instance_id)
            inspect = self._inspect_container(
                str(existing.get("container_id") or ""),
                timeout=5.0,
            )
            self._stop_and_remove_owned(
                inspect,
                instance_id=instance_id,
                remove_timeout=20.0,
            )
            self._remove_secret(instance_id)
            return {"ok": True, "stopped": 1, "instance_id": instance_id}

    def credentials(self, instance_id: str) -> dict[str, Any]:
        """返回运行中 Worker 的当前 Basic Auth 凭证，不写入会话状态。"""
        if INSTANCE_ID_PATTERN.fullmatch(instance_id) is None:
            raise BrokerError("浏览器实例 ID 无效")
        with self.lock:
            session = self._existing_session(instance_id, timeout=5.0)
            if not session or session.get("status") != "running" or not session.get("url"):
                raise BrokerError("仅运行中的远程浏览器可以查看访问凭证")
            inspect = self._inspect_container(
                str(session.get("container_id") or ""),
                timeout=5.0,
            )
            self._assert_owned_worker(inspect, instance_id=instance_id)
            config = inspect.get("Config") if isinstance(inspect.get("Config"), dict) else {}
            environment = config.get("Env") if isinstance(config.get("Env"), list) else []
            username = ""
            for item in environment:
                text = str(item or "")
                if text.startswith("CUSTOM_USER="):
                    username = text.partition("=")[2]
                    break
            if USERNAME_PATTERN.fullmatch(username) is None:
                raise BrokerError("远程浏览器访问用户名格式无效，请重新启动该记录")
            password = self._read_secret(instance_id)
            return {
                "ok": True,
                "instance_id": instance_id,
                "url": str(session["url"]),
                "username": username,
                "password": password,
            }

    def status(self) -> dict[str, Any]:
        version = self.engine.api_version()
        self._require_volume_subpath_api()
        # 数据必须来自命名卷子路径；控制文件则来自仅 Broker 可写的私有卷。
        self._validated_worker_network()
        self._data_volume()
        self._control_volume_source()
        return {
            "ok": True,
            "docker_api_version": version,
            "worker_image": self.config.worker_image,
            "worker_image_ready": self._worker_image_ready(),
            "bind_host": self.config.bind_host,
            "public_host": self.config.public_host,
            "port_range": [self.config.port_start, self.config.port_end],
            "max_sessions": self.config.max_sessions,
            "active_sessions": sum(
                item.get("status") in LIVE_WORKER_STATUSES for item in self.sessions()
            ),
        }

    def cleanup_workers(self, *, mark_stopping: bool = True) -> int:
        """先优雅停止再回收带固定标签的 worker，并清除相应临时凭证。"""
        if mark_stopping:
            self.stopping.set()
        with self.lock:
            removed = 0
            # 关闭外部入口不依赖 Docker Engine，应始终先执行。
            failures: list[Exception] = self._stop_all_relays()
            summaries = self._list_raw(timeout=10.0)

            def cleanup(summary: dict[str, Any]) -> bool:
                identifier = str(summary.get("Id") or "")
                if not identifier:
                    return False
                try:
                    inspect = self._inspect_container(identifier, timeout=10.0)
                except EngineError as exc:
                    if exc.status == 404:
                        return False
                    raise
                self._stop_and_remove_owned(
                    inspect,
                    force_on_failure=True,
                    stop_timeout=15.0,
                    remove_timeout=20.0,
                )
                return True

            if summaries:
                with concurrent.futures.ThreadPoolExecutor(
                    max_workers=min(len(summaries), self.config.max_sessions, 100)
                ) as executor:
                    futures = [executor.submit(cleanup, summary) for summary in summaries]
                    for future in concurrent.futures.as_completed(futures):
                        try:
                            removed += int(future.result())
                        except (BrokerError, EngineError) as exc:
                            failures.append(exc)
            session_dir = self.config.control_dir / "sessions"
            try:
                secret_files = list(session_dir.glob("*.password"))
            except OSError as exc:
                failures.append(exc)
                secret_files = []
            for secret_file in secret_files:
                if INSTANCE_ID_PATTERN.fullmatch(secret_file.stem) is None:
                    continue
                try:
                    secret_file.unlink()
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    failures.append(exc)
            if failures:
                details = "; ".join(str(error) for error in failures[:3])
                raise BrokerError(
                    f"Broker 退出时有 {len(failures)} 项 worker 资源未清理：{details}"
                ) from failures[0]
            return removed

    def reconcile_startup(self) -> int:
        """启动时回收上次异常退出遗留的公开 worker 与凭证。"""
        return self.cleanup_workers(mark_stopping=False)


def _json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


class BrokerHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], broker: BrowserWorkerBroker) -> None:
        self.broker = broker
        super().__init__(address, BrokerHandler)


class BrokerHandler(BaseHTTPRequestHandler):
    server: BrokerHTTPServer
    server_version = "ProxyBridgeBrowserBroker/1"

    def log_message(self, fmt: str, *args: Any) -> None:
        print("[%s] %s" % (self.log_date_time_string(), fmt % args), flush=True)

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        data = _json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _authenticated(self) -> bool:
        authorization = str(self.headers.get("Authorization") or "")
        prefix = "Bearer "
        if not authorization.startswith(prefix):
            return False
        return self.server.broker.token_matches(authorization[len(prefix) :])

    def _read_payload(self) -> dict[str, Any]:
        if self.headers.get_content_type() != "application/json":
            raise BrokerError("仅支持 application/json")
        try:
            length = int(str(self.headers.get("Content-Length") or "0"))
        except ValueError as exc:
            raise BrokerError("Content-Length 无效") from exc
        if length < 0 or length > REQUEST_BODY_LIMIT:
            raise BrokerError("请求体过大")
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BrokerError("JSON 请求体无效") from exc
        if not isinstance(payload, dict):
            raise BrokerError("JSON 请求体必须是对象")
        return payload

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._send(200, {"ok": True})
            return
        if not self._authenticated():
            self._send(403, {"ok": False, "error": "Broker 凭证无效"})
            return
        try:
            if self.path == "/v1/status":
                self._send(200, self.server.broker.status())
            elif self.path == "/v1/sessions":
                self._send(200, {"ok": True, "sessions": self.server.broker.sessions()})
            else:
                self._send(404, {"ok": False, "error": "接口不存在"})
        except (BrokerError, EngineError) as exc:
            self._send(503, {"ok": False, "error": str(exc)[:1000]})
        except Exception as exc:
            print(f"Broker GET 未处理异常: {exc}", flush=True)
            self._send(500, {"ok": False, "error": "浏览器 Broker 内部错误"})

    def do_POST(self) -> None:
        if not self._authenticated():
            self._send(403, {"ok": False, "error": "Broker 凭证无效"})
            return
        try:
            payload = self._read_payload()
            if self.path == "/v1/image/prepare":
                self._send(200, self.server.broker.prepare())
            elif self.path == "/v1/sessions/start":
                self._send(200, self.server.broker.start(payload))
            elif self.path == "/v1/sessions/stop":
                self._send(
                    200,
                    self.server.broker.stop(
                        str(payload.get("instance_id") or ""),
                        str(payload.get("cancelled_operation_id") or ""),
                    ),
                )
            elif self.path == "/v1/sessions/credentials":
                self._send(
                    200,
                    self.server.broker.credentials(str(payload.get("instance_id") or "")),
                )
            else:
                self._send(404, {"ok": False, "error": "接口不存在"})
        except BrokerError as exc:
            self._send(400, {"ok": False, "error": str(exc)[:1000]})
        except EngineError as exc:
            self._send(503, {"ok": False, "error": f"Docker worker 操作失败：{str(exc)[:800]}"})
        except Exception as exc:
            print(f"Broker POST 未处理异常: {exc}", flush=True)
            self._send(500, {"ok": False, "error": "浏览器 Broker 内部错误"})


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ProxyBridge 远程浏览器 worker Broker")
    # --host/--port 只配置容器内部 browser-control 网络的监听端点。默认监听容器
    # 全部接口的 8765 端口以供主服务访问，但 Compose 不得把该端口发布到宿主机。
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Broker 内部监听地址（默认 0.0.0.0；仅限 Docker internal 控制网络）",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=BROKER_CONTROL_PORT,
        help="Broker 内部监听端口（默认 8765；禁止发布到宿主机或公网）",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    config = BrokerConfig.from_environment()
    engine = DockerEngineClient(config.docker_socket)
    broker = BrowserWorkerBroker(config, engine)
    broker.ensure_token()
    removed = broker.reconcile_startup()
    if removed:
        print(f"ProxyBridge browser broker reconciled {removed} orphan worker(s)", flush=True)
    server = BrokerHTTPServer((args.host, args.port), broker)

    def stop(_signum: int, _frame: Any) -> None:
        broker.stopping.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    print(f"ProxyBridge browser broker listening on {args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        try:
            removed = broker.cleanup_workers()
            if removed:
                print(f"ProxyBridge browser broker removed {removed} worker(s)", flush=True)
        except (BrokerError, EngineError) as exc:
            print(f"ProxyBridge browser broker cleanup failed: {exc}", flush=True)


if __name__ == "__main__":
    main()
