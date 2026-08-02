from __future__ import annotations

import argparse
import concurrent.futures
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
from typing import Any


REQUEST_BODY_LIMIT = 128 * 1024
ENGINE_RESPONSE_LIMIT = 16 * 1024 * 1024
MIN_VOLUME_SUBPATH_API = (1, 45)
WORKER_PLATFORM = "linux/amd64"
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
LIVE_WORKER_STATUSES = frozenset({"running", "starting", "failed"})
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


class EngineError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


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
        if end < start or end - start > 999:
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
    }


class BrowserWorkerBroker:
    def __init__(self, config: BrokerConfig, engine: DockerEngineClient) -> None:
        self.config = config
        self.engine = engine
        self.lock = threading.RLock()
        # 镜像拉取最长可达 15 分钟，但它不会创建公开 worker。与容器生命周期锁
        # 分离后，SIGTERM 可以立即进入 worker 回收，不必等待无副作用的 pull。
        self.image_lock = threading.Lock()
        self.stopping = threading.Event()
        self._owner_inspect: dict[str, Any] | None = None
        self._broker_inspect: dict[str, Any] | None = None
        self._cancelled_operation_lock = threading.Lock()
        self._cancelled_operations: dict[tuple[str, str], float] = {}

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
        """校验 worker 网络是仅连接当前 owner 的本地私有隔离网络。"""
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
        ports = network.get("Ports") if isinstance(network.get("Ports"), dict) else {}
        bindings = ports.get("3001/tcp") if isinstance(ports, dict) else None
        host_port = 0
        if isinstance(bindings, list) and bindings and isinstance(bindings[0], dict):
            host_port = _safe_positive_int(bindings[0].get("HostPort"), 0, 0, 65535)
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

        if not summaries:
            return []
        result: list[dict[str, Any]] = []
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(len(summaries), self.config.max_sessions, 100)
        ) as executor:
            futures = [executor.submit(inspect_summary, summary) for summary in summaries]
            for future in concurrent.futures.as_completed(futures):
                session = future.result()
                if session is not None:
                    result.append(session)
        return result

    def _ensure_image(self) -> None:
        encoded = urllib.parse.quote(self.config.worker_image, safe="")
        try:
            current = self.engine.json_request("GET", f"/images/{encoded}/json")
            if (
                str(current.get("Os") or "").lower() == "linux"
                and str(current.get("Architecture") or "").lower() == "amd64"
            ):
                return
        except EngineError as exc:
            if exc.status != 404:
                raise

        image = self.config.worker_image
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
        data = self.engine.request("POST", f"/images/create?{query}", expected=(200,), timeout=900.0)
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
                "--user-data-dir=/config/browser-profile",
                f"--fingerprint={values['fingerprint_seed']}",
                "--fingerprint-platform=linux",
                f"--timezone={values['timezone']}",
                f"--lang={values['language']}",
                f"--accept-lang={values['accept_language']}",
                "--webrtc-ip-handling-policy=disable_non_proxied_udp",
                "--no-first-run",
                "--no-default-browser-check",
                "https://ip.skk.moe/",
            )
        )
        return {
            "Image": self.config.worker_image,
            "Env": [
                "PUID=10001",
                "PGID=10001",
                "TZ=Etc/UTC",
                "PIXELFLUX_WAYLAND=true",
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
                "PortBindings": {
                    "3001/tcp": [{"HostIp": self.config.bind_host, "HostPort": str(port)}]
                },
                "NetworkMode": self.config.worker_network,
                "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0},
                "AutoRemove": False,
                "Privileged": False,
                "SecurityOpt": ["no-new-privileges:true"],
                "Memory": self.config.memory_bytes,
                "NanoCpus": self.config.nano_cpus,
                "PidsLimit": self.config.pids_limit,
                "ShmSize": self.config.shm_bytes,
                "Init": True,
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
                        self._raise_if_operation_cancelled(
                            values["instance_id"],
                            values["operation_id"],
                        )
                        keep_secret = True
                        return {"ok": True, **session}
                    except (BrokerError, EngineError) as exc:
                        last_error = exc
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
                self._remove_secret(instance_id)
                return {"ok": True, "stopped": 0, "instance_id": instance_id}
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
            failures: list[Exception] = []
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
        default=8765,
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
