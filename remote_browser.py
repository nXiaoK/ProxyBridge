from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


BROKER_RESPONSE_LIMIT = 2 * 1024 * 1024
BROKER_IMAGE_PREPARE_TIMEOUT = 930.0
BROKER_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9._~-]{32,256}")
BROKER_INSTANCE_ID_PATTERN = re.compile(r"[a-f0-9]{16}")
BROKER_USERNAME_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,32}")
BROKER_PASSWORD_MIN_LENGTH = 10
BROKER_PASSWORD_MAX_LENGTH = 128


class RemoteBrowserError(RuntimeError):
    """A safe, user-facing error returned by the browser worker broker."""


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Broker 控制请求不能携带凭证跳转到另一个地址。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


class RemoteBrowserBrokerClient:
    """Small authenticated client for the internal browser-worker broker.

    The broker is intentionally kept behind an internal Docker network.  A
    random token shared through a private volume protects against accidental
    calls from unrelated containers on that network.
    """

    def __init__(self, base_url: str, token_file: Path, *, timeout: float = 120.0) -> None:
        parsed = urllib.parse.urlsplit(str(base_url or "").strip())
        if parsed.scheme != "http" or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("浏览器 Broker 地址必须是内部 HTTP 地址")
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise ValueError("浏览器 Broker 地址不能包含路径、查询参数或 fragment")
        self.base_url = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", "")).rstrip("/")
        self.token_file = Path(token_file)
        self.timeout = max(1.0, float(timeout))
        # Broker 位于 Docker internal 网络。必须强制直连且拒绝重定向，避免继承
        # HTTP_PROXY 后把 Bearer token 或远程浏览器密码发送给外部明文代理。
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            _NoRedirectHandler(),
        )

    def _open(self, request: urllib.request.Request, *, timeout: float):
        return self._opener.open(request, timeout=timeout)

    def _token(self) -> str:
        try:
            token = self.token_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise RemoteBrowserError("浏览器 Broker 尚未就绪，请稍后重试") from exc
        if BROKER_TOKEN_PATTERN.fullmatch(token) is None:
            raise RemoteBrowserError("浏览器 Broker 凭证无效，请重新创建 Docker 服务")
        return token

    @staticmethod
    def _decode_response(data: bytes) -> dict[str, Any]:
        if len(data) > BROKER_RESPONSE_LIMIT:
            raise RemoteBrowserError("浏览器 Broker 响应过大")
        try:
            payload = json.loads(data.decode("utf-8")) if data else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RemoteBrowserError("浏览器 Broker 返回了无效响应") from exc
        if not isinstance(payload, dict):
            raise RemoteBrowserError("浏览器 Broker 返回格式无效")
        return payload

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
        timeout_error: str = "",
    ) -> dict[str, Any]:
        if not path.startswith("/") or "?" in path or "#" in path:
            raise ValueError("Broker 请求路径无效")
        data = None
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._token()}",
        }
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with self._open(request, timeout=timeout or self.timeout) as response:
                body = response.read(BROKER_RESPONSE_LIMIT + 1)
        except urllib.error.HTTPError as exc:
            body = exc.read(BROKER_RESPONSE_LIMIT + 1)
            try:
                error_payload = self._decode_response(body)
                message = str(error_payload.get("error") or "")
            except RemoteBrowserError:
                message = ""
            raise RemoteBrowserError(message or f"浏览器 Broker 请求失败（HTTP {exc.code}）") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
            reason_text = str(reason).casefold()
            if isinstance(reason, TimeoutError) or "timed out" in reason_text or "超时" in reason_text:
                raise RemoteBrowserError(
                    timeout_error or "等待浏览器 Broker 响应超时，请检查 Broker 日志"
                ) from exc
            raise RemoteBrowserError("无法连接浏览器 Broker，请检查 Docker 服务状态") from exc
        result = self._decode_response(body)
        if result.get("ok") is not True:
            raise RemoteBrowserError(str(result.get("error") or "浏览器 Broker 操作失败"))
        return result

    def status(self) -> dict[str, Any]:
        return self._request("GET", "/v1/status", timeout=10.0)

    def sessions(self, *, timeout: float | None = None) -> list[dict[str, Any]]:
        request_timeout = 30.0 if timeout is None else max(1.0, float(timeout))
        payload = self._request("GET", "/v1/sessions", timeout=request_timeout)
        sessions = payload.get("sessions")
        if not isinstance(sessions, list) or any(not isinstance(item, dict) for item in sessions):
            raise RemoteBrowserError("浏览器 Broker 会话响应格式无效")
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in sessions:
            instance_id = str(item.get("instance_id") or "")
            if BROKER_INSTANCE_ID_PATTERN.fullmatch(instance_id) is None or instance_id in seen:
                raise RemoteBrowserError("浏览器 Broker 会话响应格式无效")
            seen.add(instance_id)
            normalized.append(dict(item))
        return normalized

    def prepare(self) -> dict[str, Any]:
        """拉取并校验固定平台镜像，但不创建容器或处理用户密码。"""
        # Broker 内部 Docker pull 最多等待 900 秒；客户端额外保留 30 秒，确保
        # 服务端能先返回“镜像拉取超时”的真实原因，而不是被误报为 Broker 断连。
        return self._request(
            "POST",
            "/v1/image/prepare",
            {},
            timeout=max(self.timeout, BROKER_IMAGE_PREPARE_TIMEOUT),
            timeout_error=(
                "准备远程浏览器 Worker 镜像超时；Broker 可能仍在拉取 "
                "Worker 镜像，请检查 browser-broker 日志，或在宿主机预先 docker pull/load"
            ),
        )

    def start(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request(
            "POST",
            "/v1/sessions/start",
            payload,
            timeout=max(self.timeout, 600.0),
            timeout_error="创建远程浏览器 Worker 超时，请检查 browser-broker 与 Worker 容器日志",
        )

    def credentials(self, instance_id: str) -> dict[str, Any]:
        """按需读取运行中 Worker 的访问凭证，不进入会话列表或持久状态。"""
        if BROKER_INSTANCE_ID_PATTERN.fullmatch(instance_id) is None:
            raise ValueError("浏览器实例 ID 无效")
        result = self._request(
            "POST",
            "/v1/sessions/credentials",
            {"instance_id": instance_id},
            timeout=10.0,
        )
        username = str(result.get("username") or "")
        password = str(result.get("password") or "")
        if (
            result.get("instance_id") != instance_id
            or BROKER_USERNAME_PATTERN.fullmatch(username) is None
            or not BROKER_PASSWORD_MIN_LENGTH <= len(password) <= BROKER_PASSWORD_MAX_LENGTH
            or any(ord(char) < 32 or ord(char) == 127 for char in password)
        ):
            raise RemoteBrowserError("浏览器 Broker 凭证响应格式无效")
        return dict(result)

    def stop(
        self,
        instance_id: str,
        cancelled_operation_id: str = "",
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        payload = {"instance_id": instance_id}
        if cancelled_operation_id:
            payload["cancelled_operation_id"] = cancelled_operation_id
        request_timeout = max(self.timeout, 90.0) if timeout is None else max(1.0, float(timeout))
        return self._request(
            "POST",
            "/v1/sessions/stop",
            payload,
            timeout=request_timeout,
        )
