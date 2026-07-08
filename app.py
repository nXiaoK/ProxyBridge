#!/usr/bin/env python3
from __future__ import annotations

import atexit
import base64
import hashlib
import json
import mimetypes
import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


APP_ROOT = Path(__file__).resolve().parent
STATIC_ROOT = APP_ROOT / "static"
WORK_ROOT = Path(os.environ.get("PROXY_BRIDGE_WORKDIR", APP_ROOT / "work")).expanduser()
RUNTIME_ROOT = WORK_ROOT / "runtime"
STATE_PATH = WORK_ROOT / "state.json"
DEFAULT_SERVER_PORT = 8787
DEFAULT_SOCKS_PORT = 21080
REQUEST_LOG_LIMIT = 500
ACTIVE_BRIDGE_STATUSES = {"running", "starting"}
CORE_LOG_VISIBLE_STATUSES = ACTIVE_BRIDGE_STATUSES | {"failed"}
CORE_LOG_TAIL_LIMIT = 12000
CORE_LOG_ENTRY_LIMIT = 30
PROCESS_TABLE: dict[str, subprocess.Popen] = {}
# 记录每个节点已启动的指纹浏览器进程(可能因单例复用产生多个短命进程,故用列表);
# 用于「停止浏览器」时结束进程组,并据此在前端显示停止按钮。
BROWSER_TABLE: dict[str, list[subprocess.Popen]] = {}
LOG_PROXY_TABLE: dict[str, "Socks5LogProxy"] = {}
TRAFFIC_TABLE: dict[str, dict[str, int]] = {}
STATE_LOCK = threading.RLock()
PROCESS_LOCK = threading.RLock()
TRAFFIC_LOCK = threading.RLock()
SHUTTING_DOWN = threading.Event()


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


def now_ts() -> int:
    return int(time.time())


def ensure_dirs() -> None:
    WORK_ROOT.mkdir(parents=True, exist_ok=True)
    RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
    browser_profiles_root().mkdir(parents=True, exist_ok=True)


def browser_profiles_root() -> Path:
    return WORK_ROOT / "browser_profiles"


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


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


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


def node_region(name: str, server: str = "") -> str:
    haystack = f"{name} {server}".lower()
    patterns = [
        ("香港", ("香港", "hong kong", "hongkong", "hk", "hkg")),
        ("日本", ("日本", "japan", "tokyo", "osaka", "jp", "jpn")),
        ("新加坡", ("新加坡", "singapore", "sg", "sin")),
        ("台湾", ("台湾", "taiwan", "tw", "twn")),
        ("韩国", ("韩国", "korea", "seoul", "kr", "kor")),
        ("美国", ("美国", "united states", "america", "los angeles", "san jose", "us", "usa")),
        ("英国", ("英国", "united kingdom", "london", "uk", "gb")),
        ("德国", ("德国", "germany", "frankfurt", "de", "deu")),
        ("法国", ("法国", "france", "paris", "fr", "fra")),
        ("加拿大", ("加拿大", "canada", "ca", "toronto")),
        ("澳大利亚", ("澳大利亚", "australia", "sydney", "au", "aus")),
        ("荷兰", ("荷兰", "netherlands", "amsterdam", "nl")),
    ]
    for region, terms in patterns:
        for term in terms:
            if re.search(rf"(^|[^a-z0-9]){re.escape(term)}([^a-z0-9]|$)", haystack):
                return region
    return "未分类"


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


def region_locale(region: str) -> tuple[str, str, str]:
    # 根据节点地区返回 (时区, 语言, Accept-Language),用于指纹内核的 --timezone/--lang/--accept-lang。
    return REGION_LOCALE_MAP.get(region or "", DEFAULT_LOCALE)


def fingerprint_seed(node_id: str) -> int:
    # 从节点 ID 确定性派生 32 位指纹种子:同一节点每次启动得到完全相同的指纹(可恢复环境),
    # 不同节点得到互不相同的指纹(避免被网站关联为同一设备)。取正数并避开 0(0 在部分构建中表示关闭)。
    digest = hashlib.sha256(f"fp-seed::{node_id}".encode("utf-8")).digest()
    seed = int.from_bytes(digest[:4], "big") & 0x7FFFFFFF
    return seed or 1


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
    if isinstance(reality_opts, dict):
        settings["public_key"] = reality_opts.get("public-key") or reality_opts.get("publicKey") or ""
        settings["short_id"] = reality_opts.get("short-id") or reality_opts.get("shortId") or ""
        settings["fingerprint"] = reality_opts.get("fingerprint") or proxy.get("client-fingerprint") or ""
    if isinstance(ws_opts, dict):
        settings["path"] = ws_opts.get("path") or ""
        headers = ws_opts.get("headers") or {}
        if isinstance(headers, dict):
            settings["host"] = headers.get("Host") or headers.get("host") or ""

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
    return {"subscriptions": [], "nodes": [], "bridges": [], "tests": {}, "request_logs": []}


def load_state(reset_running: bool = False) -> dict[str, Any]:
    ensure_dirs()
    if not STATE_PATH.exists():
        return empty_state()
    try:
        with STATE_PATH.open("r", encoding="utf-8") as f:
            state = json.load(f)
    except Exception:
        return empty_state()
    state.setdefault("subscriptions", [])
    state.setdefault("nodes", [])
    state.setdefault("bridges", [])
    state.setdefault("tests", {})
    state.setdefault("request_logs", [])
    for bridge in state["bridges"]:
        if "enabled" not in bridge:
            bridge["enabled"] = bridge.get("status") in ACTIVE_BRIDGE_STATUSES
        else:
            bridge["enabled"] = bool(bridge.get("enabled"))
        if bridge.get("port") and not bridge.get("socks_url"):
            bridge["socks_url"] = f"socks5://127.0.0.1:{bridge.get('port')}"
        traffic = traffic_defaults(bridge.get("upload_bytes"), bridge.get("download_bytes"))
        bridge.update(traffic)
    if reset_running:
        for bridge in state["bridges"]:
            if bridge.get("status") in ACTIVE_BRIDGE_STATUSES:
                bridge["status"] = "stopped"
                bridge["pid"] = None
    return state


def save_state(state: dict[str, Any]) -> None:
    ensure_dirs()
    tmp = STATE_PATH.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    tmp.replace(STATE_PATH)


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


def pop_bridge_traffic(bridge_id: str) -> dict[str, int]:
    with TRAFFIC_LOCK:
        return dict(TRAFFIC_TABLE.pop(bridge_id, traffic_defaults()))


def append_request_log(entry: dict[str, Any]) -> None:
    with STATE_LOCK:
        state = load_state(reset_running=False)
        logs = state.setdefault("request_logs", [])
        logs.insert(0, entry)
        del logs[REQUEST_LOG_LIMIT:]
        save_state(state)


def update_request_log(log_id: str, updates: dict[str, Any]) -> None:
    with STATE_LOCK:
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
    return {"ok": True}


def public_node(node: dict[str, Any]) -> dict[str, Any]:
    settings = node.get("settings") or {}
    return {
        "id": node.get("id"),
        "name": node.get("name"),
        "protocol": node.get("protocol"),
        "server": node.get("server"),
        "port": node.get("port"),
        "region": node.get("region"),
        "source": node.get("source"),
        "tags": node.get("tags", []),
        "transport": node.get("transport"),
        "security": node.get("security"),
        "preferred_core": node.get("preferred_core"),
        "has_secret": bool(settings.get("password") or settings.get("uuid")),
    }


def core_paths() -> dict[str, str | None]:
    core_dir = Path(os.environ.get("PROXY_BRIDGE_CORE_DIR", APP_ROOT / "bin")).expanduser()

    def executable(path: Path) -> str | None:
        return str(path) if path.is_file() and os.access(path, os.X_OK) else None

    sing_box = (
        executable(Path(os.environ.get("PROXY_BRIDGE_SING_BOX", "")))
        or executable(core_dir / "sing-box")
        or shutil.which("sing-box")
    )
    xray = (
        executable(Path(os.environ.get("PROXY_BRIDGE_XRAY", "")))
        or executable(core_dir / "xray")
        or executable(core_dir / "xray-core")
        or shutil.which("xray")
        or shutil.which("xray-core")
    )
    return {"sing-box": sing_box, "xray": xray}


def chrome_path() -> str | None:
    env_path = os.environ.get("PROXY_BRIDGE_CHROME")
    candidates: list[Path] = []
    if env_path:
        candidates.append(Path(env_path).expanduser())
    candidates.extend(
        [
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        ]
    )
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return shutil.which("google-chrome") or shutil.which("chrome") or shutil.which("google-chrome-stable")


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


def fingerprint_chromium_path() -> str | None:
    # 定位指纹内核可执行文件。优先级:环境变量 > 工作目录安装位 > 系统 Applications。
    # 指纹内核必须是打过补丁的 Chromium(而非本机 Google Chrome),否则无法在底层伪造指纹。
    env_path = os.environ.get("PROXY_BRIDGE_FP_CHROMIUM")
    if env_path:
        candidate = Path(env_path).expanduser()
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
        if candidate.suffix == ".app" and candidate.is_dir():
            exe = _bundle_executable(candidate)
            if exe:
                return str(exe)

    search_dirs = [fingerprint_chromium_root(), Path("/Applications"), Path.home() / "Applications"]
    for base in search_dirs:
        if not base.is_dir():
            continue
        # 递归查找 Chromium.app,兼容 dmg 解压后可能带有中间目录层级的情况。
        for app_dir in sorted(base.glob("**/Chromium.app")):
            exe = _bundle_executable(app_dir)
            if exe:
                return str(exe)
    return None


def status_payload() -> dict[str, Any]:
    with STATE_LOCK:
        state = load_state()
        sync_process_status(state)
        save_state(state)
        return {
            "cores": core_paths(),
            "chrome": chrome_path(),
            "fingerprint_chromium": fingerprint_chromium_path(),
            "active_browsers": active_browser_node_ids(),
            "subscriptions": state["subscriptions"],
            "nodes": [public_node(node) for node in state["nodes"]],
            "bridges": state["bridges"],
            "tests": state.get("tests", {}),
            "request_logs": state.get("request_logs", [])[:200],
            "version": "0.1.0",
        }


def fetch_subscription(url: str) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "ProxyBridge/0.1 (+https://localhost)",
            "Accept": "*/*",
        },
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        data = response.read()
        charset = response.headers.get_content_charset() or "utf-8"
        return data.decode(charset, errors="replace")


def import_subscription(payload: dict[str, Any]) -> dict[str, Any]:
    name = clean_name(str(payload.get("name") or ""), "未命名订阅")
    url = str(payload.get("url") or "").strip()
    raw = str(payload.get("raw") or "").strip()
    if url and not raw:
        raw = fetch_subscription(url)
    if not raw:
        raise ValueError("需要订阅 URL 或原始订阅内容")

    nodes = parse_subscription_payload(raw, name)
    subscription_id = stable_id(name, url, str(now_ts()))
    imported_at = now_ts()
    node_dicts = [asdict(node) for node in nodes]
    node_ids = [node.id for node in nodes]
    subscription = {
        "id": subscription_id,
        "name": name,
        "url": url,
        "last_update": imported_at,
        "node_count": len(nodes),
        "node_ids": node_ids,
    }
    with STATE_LOCK:
        state = load_state()
        state["subscriptions"] = [s for s in state["subscriptions"] if s.get("url") != url or not url]
        state["subscriptions"].append(subscription)
        existing = {node["id"]: node for node in state["nodes"]}
        for node in node_dicts:
            existing[node["id"]] = node
        state["nodes"] = list(existing.values())
        save_state(state)
    return {"subscription": subscription, "nodes": [public_node(node) for node in node_dicts]}


def refresh_subscription(subscription_id: str) -> dict[str, Any]:
    with STATE_LOCK:
        state = load_state()
        subscription = next((s for s in state["subscriptions"] if s.get("id") == subscription_id), None)
    if not subscription:
        raise ValueError("订阅不存在")
    url = subscription.get("url")
    if not url:
        raise ValueError("这个订阅没有 URL，无法刷新")
    return import_subscription({"name": subscription.get("name"), "url": url})


def delete_subscription(subscription_id: str) -> dict[str, Any]:
    with STATE_LOCK:
        state = load_state()
        subscription = next((s for s in state["subscriptions"] if s.get("id") == subscription_id), None)
        if not subscription:
            raise ValueError("订阅不存在")
        remove_ids = set(subscription.get("node_ids") or [])
        state["subscriptions"] = [s for s in state["subscriptions"] if s.get("id") != subscription_id]
        state["nodes"] = [n for n in state["nodes"] if n.get("id") not in remove_ids]
        save_state(state)
    return {"ok": True}


def find_node(state: dict[str, Any], node_id: str) -> dict[str, Any]:
    node = next((n for n in state["nodes"] if n.get("id") == node_id), None)
    if not node:
        raise ValueError("节点不存在")
    return node


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


def is_port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def next_free_port(preferred: int | None = None) -> int:
    start = preferred or DEFAULT_SOCKS_PORT
    for port in range(start, start + 1000):
        if is_port_free(port):
            return port
    raise RuntimeError("没有可用的本地端口")


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


def xray_config(node: dict[str, Any], local_port: int) -> dict[str, Any]:
    settings = node.get("settings") or {}
    if node["protocol"] != "vless":
        raise ValueError("第一版只用 xray 处理 VLESS XHTTP fallback")
    user: dict[str, Any] = {"id": settings.get("uuid", ""), "encryption": "none"}
    if settings.get("flow"):
        user["flow"] = settings["flow"]
    stream: dict[str, Any] = {"network": "xhttp", "security": node.get("security") or "reality"}
    if stream["security"] == "reality":
        reality: dict[str, Any] = {}
        if settings.get("sni"):
            reality["serverName"] = settings["sni"]
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
    xhttp: dict[str, Any] = {}
    if settings.get("path"):
        xhttp["path"] = settings["path"]
    if settings.get("host"):
        xhttp["host"] = settings["host"]
    if settings.get("mode"):
        xhttp["mode"] = settings["mode"]
    stream["xhttpSettings"] = xhttp
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
        "outbounds": [
            {
                "protocol": "vless",
                "settings": {
                    "vnext": [
                        {
                            "address": node["server"],
                            "port": int(node["port"]),
                            "users": [user],
                        }
                    ]
                },
                "streamSettings": stream,
            }
        ],
    }


def generate_config(core: str, node: dict[str, Any], local_port: int) -> dict[str, Any]:
    if core == "xray":
        return xray_config(node, local_port)
    return sing_box_config(node, local_port)


def core_command(core: str, config_path: Path) -> list[str]:
    paths = core_paths()
    binary = paths.get(core)
    if not binary:
        raise FileNotFoundError(f"找不到 {core}，请先安装对应核心")
    if core == "xray":
        return [binary, "run", "-config", str(config_path)]
    return [binary, "run", "-c", str(config_path)]


def terminate_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
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


def start_bridge(payload: dict[str, Any]) -> dict[str, Any]:
    if SHUTTING_DOWN.is_set():
        raise RuntimeError("程序正在退出，不能启动新的 SOCKS5")
    node_id = str(payload.get("node_id") or "")
    requested_port = safe_int(payload.get("port"), 0) or None
    requested_core = str(payload.get("core") or "auto")
    preserve_traffic = bool(payload.get("restore"))
    initial_upload = safe_int(payload.get("upload_bytes"), 0) if preserve_traffic else 0
    initial_download = safe_int(payload.get("download_bytes"), 0) if preserve_traffic else 0
    with STATE_LOCK:
        state = load_state()
        node = find_node(state, node_id)
        core = node.get("preferred_core") if requested_core == "auto" else requested_core
        if core not in {"sing-box", "xray"}:
            raise ValueError("核心只能是 sing-box、xray 或 auto")
        local_port = next_free_port(requested_port)
        core_port = next_free_port(max(local_port + 1000, 23080))
        bridge_id = stable_id(node_id, str(local_port), core)
        config = generate_config(core, node, core_port)
        config_path = RUNTIME_ROOT / f"{bridge_id}.json"
        log_path = RUNTIME_ROOT / f"{bridge_id}.log"
        with config_path.open("w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
        command = core_command(core, config_path)
        log_file = log_path.open("ab")
        try:
            process = subprocess.Popen(
                command,
                cwd=str(APP_ROOT),
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        finally:
            log_file.close()
        with PROCESS_LOCK:
            PROCESS_TABLE[bridge_id] = process
        core_started = wait_for_port(core_port, timeout=2.5)
        if process.poll() is not None:
            error = read_tail(log_path)
            stop_process(bridge_id)
            raise RuntimeError(error or f"{core} 启动失败")
        if not core_started:
            stop_process(bridge_id)
            raise RuntimeError("核心内部端口启动超时")
        proxy = Socks5LogProxy(
            bridge_id=bridge_id,
            node_id=node_id,
            node_name=str(node.get("name") or ""),
            listen_port=local_port,
            upstream_port=core_port,
        )
        try:
            proxy.start()
        except Exception:
            stop_process(bridge_id)
            raise
        with PROCESS_LOCK:
            LOG_PROXY_TABLE[bridge_id] = proxy
        started = wait_for_port(local_port, timeout=1.0)
        traffic = init_bridge_traffic(bridge_id, initial_upload, initial_download)
        bridge = {
            "id": bridge_id,
            "node_id": node_id,
            "node_name": node.get("name"),
            "protocol": node.get("protocol"),
            "region": node.get("region"),
            "core": core,
            "port": local_port,
            "core_port": core_port,
            "socks_url": f"socks5://127.0.0.1:{local_port}",
            "status": "running" if started else "starting",
            "enabled": True,
            "pid": process.pid,
            "started_at": now_ts(),
            "config_path": str(config_path),
            "log_path": str(log_path),
            **traffic,
        }
        state["bridges"] = [b for b in state["bridges"] if b.get("id") != bridge_id]
        state["bridges"].append(bridge)
        save_state(state)
        return bridge


def launch_browser(payload: dict[str, Any]) -> dict[str, Any]:
    node_id = str(payload.get("node_id") or "")
    if not node_id:
        raise ValueError("缺少节点 ID")
    # 方案 B:使用打过补丁的指纹内核(fingerprint-chromium),从底层伪造 Canvas/WebGL/Audio/
    # 字体/屏幕/硬件并发/设备内存/Client Hints 等全部指纹维度;若内核缺失则明确提示去下载。
    fp_chromium = fingerprint_chromium_path()
    if not fp_chromium:
        raise RuntimeError(
            "未找到指纹内核 fingerprint-chromium。请点击「下载指纹内核」自动安装,"
            "或手动下载后设置 PROXY_BRIDGE_FP_CHROMIUM 指向 Chromium 可执行文件。"
        )

    with STATE_LOCK:
        state = load_state()
        node = find_node(state, node_id)
        sync_process_status(state)
        save_state(state)
        bridge = active_bridge_for_node(state, node_id)

    if not bridge:
        bridge = start_bridge({"node_id": node_id, "core": "auto"})

    local_port = safe_int(bridge.get("port"), 0)
    if local_port <= 0:
        raise RuntimeError("当前节点没有可用的 SOCKS5 端口")

    profile_path = browser_profiles_root() / stable_id(node_id)
    profile_path.mkdir(parents=True, exist_ok=True)
    proxy = f"socks5://127.0.0.1:{local_port}"

    # 每个节点派生固定指纹种子,并按地区匹配时区/语言,使指纹与代理出口地区一致。
    seed = fingerprint_seed(node_id)
    region = node.get("region") or node_region(node.get("name") or "", node.get("server") or "")
    timezone, language, accept_language = region_locale(region)

    command = [
        fp_chromium,
        f"--user-data-dir={profile_path}",
        # --fingerprint 是指纹伪造的总开关:同种子必得同指纹,不同种子必得不同指纹。
        f"--fingerprint={seed}",
        "--fingerprint-platform=macos",
        f"--timezone={timezone}",
        f"--lang={language}",
        f"--accept-lang={accept_language}",
        # 使用代理时强制 WebRTC 只走代理,禁止非代理 UDP,防止通过 STUN 泄漏真实 IP。
        # 用标准 Chromium 开关 --webrtc-ip-handling-policy + 枚举值(经内核二进制验证存在),
        # 而非独立布尔开关 --disable-non-proxied-udp(该 build 中不存在,会被静默忽略)。
        "--webrtc-ip-handling-policy=disable_non_proxied_udp",
        "--no-first-run",
        "--no-default-browser-check",
        f"--proxy-server={proxy}",
        "--new-window",
        "about:blank",
    ]
    process = subprocess.Popen(
        command,
        cwd=str(APP_ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    # 记录浏览器进程,便于后续「停止浏览器」结束整个进程组;同时清理已退出的旧句柄。
    with PROCESS_LOCK:
        procs = [p for p in BROWSER_TABLE.get(node_id, []) if p.poll() is None]
        procs.append(process)
        BROWSER_TABLE[node_id] = procs
    return {
        "ok": True,
        "pid": process.pid,
        "node_id": node_id,
        "node_name": node.get("name") or node_id,
        "proxy": proxy,
        "bridge_id": bridge.get("id"),
        "profile_path": str(profile_path),
        "chromium_path": fp_chromium,
        "fingerprint_seed": seed,
        "region": region,
        "timezone": timezone,
        "language": language,
        "isolation": "fingerprint-chromium 底层指纹伪造 + 独立 Profile + 节点 SOCKS5 代理",
        "fingerprint_note": (
            "已按节点派生独立指纹(Canvas/WebGL/Audio/字体/屏幕/硬件并发/设备内存/Client Hints),"
            "时区与语言按节点地区匹配,并已禁止 WebRTC 非代理 UDP。"
        ),
    }


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
    # 返回当前仍有存活浏览器进程的节点 ID 列表,供前端决定是否显示「停止浏览器」按钮。
    with PROCESS_LOCK:
        node_ids = list(BROWSER_TABLE.keys())
    return [node_id for node_id in node_ids if prune_browser_processes(node_id)]


def stop_browser(payload: dict[str, Any]) -> dict[str, Any]:
    # 停止某节点的指纹浏览器:先结束全部浏览器进程组,再停止该节点对应的 SOCKS5 桥接。
    node_id = str(payload.get("node_id") or "")
    if not node_id:
        raise ValueError("缺少节点 ID")

    with PROCESS_LOCK:
        procs = BROWSER_TABLE.pop(node_id, [])
    killed = 0
    for process in procs:
        if process.poll() is None:
            # 浏览器以 start_new_session 启动,终结整个进程组以带走全部子进程。
            terminate_process(process)
            killed += 1

    # 同步停止该节点当前活跃的 SOCKS5 桥接(浏览器与代理成对出现,一起关闭)。
    stopped_bridges: list[str] = []
    with STATE_LOCK:
        state = load_state()
        sync_process_status(state)
        save_state(state)
        bridge = active_bridge_for_node(state, node_id)
    if bridge and bridge.get("id"):
        stop_bridge(str(bridge["id"]))
        stopped_bridges.append(str(bridge["id"]))

    return {
        "ok": True,
        "node_id": node_id,
        "killed_browsers": killed,
        "stopped_bridges": stopped_bridges,
    }


FP_CHROMIUM_RELEASES_API = "https://api.github.com/repos/adryfish/fingerprint-chromium/releases"


def _macos_arch_token() -> str:
    # 返回本机 CPU 架构标记,用于安装后校验内核二进制与主机是否兼容。
    machine = platform.machine().lower()
    if machine in {"arm64", "aarch64"}:
        return "arm64"
    return "x86_64"


def _pick_macos_asset(releases: list[dict[str, Any]]) -> dict[str, str] | None:
    # 从 GitHub releases 列表中挑选第一个 macOS dmg 资源。
    # 注意:fingerprint-chromium 的 macOS 包命名为 ..._macos.dmg,文件名不含架构标记
    # (目前只发布单一 arm64 包),因此这里按 "_macos.dmg" 结尾识别,不按架构 token 过滤。
    # 并非每个版本都发布 macOS 包,所以需要遍历多个 release 兜底取最新可用者。
    for release in releases:
        if release.get("draft") or release.get("prerelease"):
            continue
        for asset in release.get("assets") or []:
            name = str(asset.get("name") or "")
            lowered = name.lower()
            if lowered.endswith(".dmg") and "macos" in lowered:
                url = asset.get("browser_download_url")
                if url:
                    return {"name": name, "url": str(url), "tag": str(release.get("tag_name") or "")}
    return None


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


def _install_from_dmg(dmg_path: Path, dest_root: Path) -> Path:
    # 挂载 dmg,复制其中的 Chromium.app 到目标目录,再卸载。全程使用 macOS 自带 hdiutil。
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
        if target_app.exists():
            shutil.rmtree(target_app, ignore_errors=True)
        shutil.copytree(source_app, target_app, symlinks=True)
    finally:
        subprocess.run(["hdiutil", "detach", str(mount_point), "-force"], capture_output=True)
        shutil.rmtree(mount_point, ignore_errors=True)
    # 去除 Gatekeeper 隔离属性,避免首次启动被 macOS 拦截("已损坏/无法验证开发者")。
    subprocess.run(["xattr", "-dr", "com.apple.quarantine", str(target_app)], capture_output=True)
    return target_app


def download_fingerprint_chromium(payload: dict[str, Any]) -> dict[str, Any]:
    # 一键下载指纹内核:查询 GitHub Releases,取最新的 macOS dmg,下载并安装到工作目录。
    existing = fingerprint_chromium_path()
    if existing and not truthy(payload.get("force")):
        return {"ok": True, "already_installed": True, "path": existing}

    request = urllib.request.Request(
        f"{FP_CHROMIUM_RELEASES_API}?per_page=20",
        headers={"User-Agent": "ProxyBridge/0.1", "Accept": "application/vnd.github+json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            releases = json.loads(response.read().decode("utf-8", errors="replace"))
    except Exception as error:
        raise RuntimeError(f"获取指纹内核版本列表失败:{error}") from error

    asset = _pick_macos_asset(releases if isinstance(releases, list) else [])
    if not asset:
        raise RuntimeError(
            "未找到 macOS 指纹内核包(..._macos.dmg)。"
            "该项目并非每个版本都发布 macOS 包,请稍后重试或手动下载。"
        )

    host_arch = _macos_arch_token()
    dest_root = fingerprint_chromium_root()
    dest_root.mkdir(parents=True, exist_ok=True)
    dmg_path = dest_root / asset["name"]
    try:
        # 流式下载 dmg 到工作目录,再从中安装。
        with urllib.request.urlopen(
            urllib.request.Request(asset["url"], headers={"User-Agent": "ProxyBridge/0.1"}),
            timeout=600,
        ) as response, dmg_path.open("wb") as out:
            shutil.copyfileobj(response, out)
    except Exception as error:
        dmg_path.unlink(missing_ok=True)
        raise RuntimeError(f"下载指纹内核失败:{error}") from error

    try:
        target_app = _install_from_dmg(dmg_path, dest_root)
    finally:
        dmg_path.unlink(missing_ok=True)

    # 安装后校验二进制架构:若与主机不一致,给出明确提示(arm64 内核在 Intel Mac 无法运行;
    # 反之 Apple Silicon 可通过 Rosetta 兼容 x86_64,但仍提醒用户以免困惑)。
    binary_arch = _verify_macos_binary_arch(target_app)
    arch_warning = ""
    if binary_arch and binary_arch != host_arch:
        if host_arch == "x86_64" and binary_arch == "arm64":
            arch_warning = "警告:下载到的是 arm64 内核,但本机是 Intel(x86_64),无法运行。"
        elif host_arch == "arm64" and binary_arch == "x86_64":
            arch_warning = "提示:下载到的是 x86_64 内核,将通过 Rosetta 在 Apple Silicon 上运行。"

    resolved = fingerprint_chromium_path()
    return {
        "ok": True,
        "already_installed": False,
        "path": resolved or str(target_app),
        "version": asset.get("tag"),
        "host_arch": host_arch,
        "binary_arch": binary_arch,
        "arch_warning": arch_warning,
        "asset": asset["name"],
    }


def read_tail(path: Path, limit: int = 6000) -> str:
    try:
        data = path.read_bytes()[-limit:]
        return data.decode("utf-8", errors="replace").strip()
    except Exception:
        return ""


def core_log_payload(core: str) -> dict[str, Any]:
    core = core.strip()
    if core not in {"sing-box", "xray"}:
        raise ValueError("核心只能是 sing-box 或 xray")

    with STATE_LOCK:
        state = load_state()
        sync_process_status(state)
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


def stop_process(bridge_id: str) -> dict[str, int]:
    with PROCESS_LOCK:
        proxy = LOG_PROXY_TABLE.pop(bridge_id, None)
        process = PROCESS_TABLE.pop(bridge_id, None)
    if proxy:
        proxy.stop()
    if process:
        terminate_process(process)
    return pop_bridge_traffic(bridge_id)


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
            print(f"Restored SOCKS5 {restored.get('socks_url')} for {restored.get('node_name')}", flush=True)
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
            print(f"Failed to restore SOCKS5 for {previous.get('node_name') or node_id}: {exc}", flush=True)


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
    ) -> None:
        self.bridge_id = bridge_id
        self.node_id = node_id
        self.node_name = node_name
        self.listen_port = listen_port
        self.upstream_port = upstream_port
        self._stop = threading.Event()
        self._server: socket.socket | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", self.listen_port))
        server.listen(128)
        server.settimeout(0.5)
        self._server = server
        self._thread = threading.Thread(target=self._serve, name=f"request-log-proxy-{self.bridge_id}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._server:
            try:
                self._server.close()
            except OSError:
                pass
        if self._thread and self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(timeout=1.0)
        self._server = None
        self._thread = None

    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and not self._stop.is_set())

    def _serve(self) -> None:
        assert self._server is not None
        while not self._stop.is_set():
            try:
                client, address = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle_client, args=(client, address), daemon=True).start()

    def _handle_client(self, client: socket.socket, address: tuple[str, int]) -> None:
        log_id = ""
        started = time.monotonic()
        upstream: socket.socket | None = None
        counters = {"upload": 0, "download": 0}
        client_label = f"{address[0]}:{address[1]}"
        try:
            client.settimeout(2.0)
            header = read_exact(client, 2)
            if header[0] != 5:
                return
            methods = read_exact(client, header[1])
            if 0 not in methods:
                client.sendall(b"\x05\xff")
                return
            client.sendall(b"\x05\x00")

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

            upstream = socket.create_connection(("127.0.0.1", self.upstream_port), timeout=12.0)
            upstream.settimeout(12.0)
            upstream.sendall(b"\x05\x01\x00")
            upstream_greeting = read_exact(upstream, 2)
            if upstream_greeting != b"\x05\x00":
                raise RuntimeError("核心 SOCKS5 握手失败")
            upstream.sendall(request_head + request_tail)
            reply = self._read_socks_reply(upstream)
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
            for sock in (client, upstream):
                if sock:
                    try:
                        sock.close()
                    except OSError:
                        pass

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

    def _relay(self, client: socket.socket, upstream: socket.socket) -> dict[str, int]:
        counters = {"upload": 0, "download": 0}

        def pump(src: socket.socket, dst: socket.socket, key: str) -> None:
            try:
                while True:
                    data = src.recv(65536)
                    if not data:
                        break
                    dst.sendall(data)
                    byte_count = len(data)
                    counters[key] += byte_count
                    traffic_key = "upload_bytes" if key == "upload" else "download_bytes"
                    add_bridge_traffic(self.bridge_id, traffic_key, byte_count)
            except OSError:
                pass
            finally:
                try:
                    dst.shutdown(socket.SHUT_WR)
                except OSError:
                    pass

        upload = threading.Thread(target=pump, args=(client, upstream, "upload"), daemon=True)
        download = threading.Thread(target=pump, args=(upstream, client, "download"), daemon=True)
        upload.start()
        download.start()
        upload.join()
        download.join()
        return counters


def socks5_http_probe(
    local_port: int,
    target_host: str = "cp.cloudflare.com",
    target_port: int = 80,
    path: str = "/generate_204",
    timeout: float = 10.0,
) -> dict[str, Any]:
    started = time.monotonic()
    with socket.create_connection(("127.0.0.1", local_port), timeout=timeout) as sock:
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

        http_request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {target_host}\r\n"
            "Connection: close\r\n"
            "User-Agent: ProxyBridge/0.2\r\n"
            "\r\n"
        ).encode("ascii")
        sock.sendall(http_request)
        response = b""
        while b"\r\n\r\n" not in response and len(response) < 4096:
            chunk = sock.recv(1024)
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
            "target": f"{target_host}:{target_port}{path}",
            "status_code": status_code,
            "message": "通信正常" if ok else f"HTTP {status_code}",
        }


def test_node(payload: dict[str, Any]) -> dict[str, Any]:
    node_id = str(payload.get("node_id") or "")
    requested_core = str(payload.get("core") or "auto")
    process: subprocess.Popen | None = None
    log_path: Path | None = None
    result: dict[str, Any]
    with STATE_LOCK:
        state = load_state()
        node = find_node(state, node_id)
        core = node.get("preferred_core") if requested_core == "auto" else requested_core
        if core not in {"sing-box", "xray"}:
            raise ValueError("核心只能是 sing-box、xray 或 auto")
        local_port = next_free_port(22080)
        test_id = stable_id("test", node_id, str(now_ts()), str(local_port))
        config = generate_config(core, node, local_port)
        config_path = RUNTIME_ROOT / f"{test_id}.json"
        log_path = RUNTIME_ROOT / f"{test_id}.log"
        with config_path.open("w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
        command = core_command(core, config_path)
        log_file = log_path.open("ab")
        try:
            process = subprocess.Popen(
                command,
                cwd=str(APP_ROOT),
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        finally:
            log_file.close()

    try:
        if not wait_for_port(local_port, timeout=3.0):
            if process.poll() is not None:
                raise RuntimeError(read_tail(log_path) or f"{core} 启动失败")
            raise RuntimeError("本地 SOCKS5 测试端口启动超时")
        probe = socks5_http_probe(local_port)
        result = {
            **probe,
            "node_id": node_id,
            "core": core,
            "tested_at": now_ts(),
        }
    except Exception as exc:
        result = {
            "ok": False,
            "node_id": node_id,
            "core": core,
            "latency_ms": None,
            "status_code": None,
            "target": "cp.cloudflare.com:80/generate_204",
            "message": str(exc),
            "tested_at": now_ts(),
        }
    finally:
        if process is not None:
            terminate_process(process)

    with STATE_LOCK:
        state = load_state()
        state.setdefault("tests", {})[node_id] = result
        save_state(state)
    return result


def stop_bridge(bridge_id: str) -> dict[str, Any]:
    with STATE_LOCK:
        traffic = stop_process(bridge_id)
        state = load_state()
        for bridge in state["bridges"]:
            if bridge.get("id") == bridge_id:
                bridge["status"] = "stopped"
                bridge["enabled"] = False
                bridge["pid"] = None
                bridge["stopped_at"] = now_ts()
                bridge.update(traffic)
                bridge.pop("error", None)
        save_state(state)
    return {"ok": True}


def stop_all_bridges() -> dict[str, Any]:
    with STATE_LOCK:
        with PROCESS_LOCK:
            bridge_ids = list(set(PROCESS_TABLE) | set(LOG_PROXY_TABLE))
        traffic_by_bridge = {}
        for bridge_id in bridge_ids:
            traffic_by_bridge[bridge_id] = stop_process(bridge_id)
        state = load_state()
        for bridge in state["bridges"]:
            bridge["status"] = "stopped"
            bridge["enabled"] = False
            bridge["pid"] = None
            bridge["stopped_at"] = now_ts()
            if bridge.get("id") in traffic_by_bridge:
                bridge.update(traffic_by_bridge[bridge.get("id")])
            bridge.pop("error", None)
        save_state(state)
    return {"ok": True}


def sync_process_status(state: dict[str, Any]) -> None:
    for bridge in state.get("bridges", []):
        bridge_id = bridge.get("id")
        with PROCESS_LOCK:
            process = PROCESS_TABLE.get(bridge_id)
            proxy = LOG_PROXY_TABLE.get(bridge_id)
        if process and process.poll() is None and proxy and proxy.is_alive():
            if wait_for_port(int(bridge.get("port")), timeout=0.05):
                bridge["status"] = "running"
            else:
                bridge["status"] = "starting"
            bridge["enabled"] = True
            bridge["pid"] = process.pid
            bridge.update(bridge_traffic_snapshot(str(bridge_id)))
            bridge.pop("error", None)
        elif bridge.get("status") in ACTIVE_BRIDGE_STATUSES:
            traffic = traffic_defaults(bridge.get("upload_bytes"), bridge.get("download_bytes"))
            if bridge_id:
                traffic = stop_process(str(bridge_id))
            bridge["status"] = "stopped"
            bridge["pid"] = None
            bridge.update(traffic)


def copy_to_clipboard(text: str) -> dict[str, Any]:
    if sys.platform == "darwin" and shutil.which("pbcopy"):
        proc = subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=False)
        if proc.returncode == 0:
            return {"ok": True}
    return {"ok": False, "text": text}


def json_response(handler: BaseHTTPRequestHandler, payload: Any, status: int = 200) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def error_response(handler: BaseHTTPRequestHandler, exc: Exception, status: int = 400) -> None:
    json_response(handler, {"error": str(exc)}, status)


class AppHandler(BaseHTTPRequestHandler):
    server_version = "ProxyBridge/0.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stdout.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or "0")
        if not length:
            return {}
        data = self.rfile.read(length)
        return json.loads(data.decode("utf-8"))

    def do_GET(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == "/api/status":
            json_response(self, status_payload())
            return
        if parsed.path == "/api/core/logs":
            params = urllib.parse.parse_qs(parsed.query)
            core = first(params, "core", default="")
            try:
                json_response(self, core_log_payload(core))
            except Exception as exc:
                error_response(self, exc, 400)
            return
        if parsed.path.startswith("/api/log/"):
            bridge_id = parsed.path.rsplit("/", 1)[-1]
            path = RUNTIME_ROOT / f"{bridge_id}.log"
            json_response(self, {"log": read_tail(path)})
            return
        if parsed.path.startswith("/api/config/"):
            bridge_id = parsed.path.rsplit("/", 1)[-1]
            path = RUNTIME_ROOT / f"{bridge_id}.json"
            try:
                with path.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                json_response(self, data)
            except Exception as exc:
                error_response(self, exc, 404)
            return
        self.serve_static()

    def do_HEAD(self) -> None:
        if self.path == "/api/status":
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.serve_static(head_only=True)

    def do_POST(self) -> None:
        try:
            payload = self.read_json()
            if self.path == "/api/subscriptions/import":
                json_response(self, import_subscription(payload))
            elif self.path == "/api/subscriptions/refresh":
                json_response(self, refresh_subscription(str(payload.get("id") or "")))
            elif self.path == "/api/subscriptions/delete":
                json_response(self, delete_subscription(str(payload.get("id") or "")))
            elif self.path == "/api/bridges/start":
                json_response(self, start_bridge(payload))
            elif self.path == "/api/bridges/stop":
                json_response(self, stop_bridge(str(payload.get("id") or "")))
            elif self.path == "/api/bridges/stop-all":
                json_response(self, stop_all_bridges())
            elif self.path == "/api/nodes/test":
                json_response(self, test_node(payload))
            elif self.path == "/api/browser/launch":
                json_response(self, launch_browser(payload))
            elif self.path == "/api/browser/stop":
                json_response(self, stop_browser(payload))
            elif self.path == "/api/browser/download-kernel":
                json_response(self, download_fingerprint_chromium(payload))
            elif self.path == "/api/clipboard":
                json_response(self, copy_to_clipboard(str(payload.get("text") or "")))
            elif self.path == "/api/logs/clear":
                json_response(self, clear_request_logs())
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
        if resolved.suffix == ".js":
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


def shutdown() -> None:
    SHUTTING_DOWN.set()
    # 退出前一并结束所有指纹浏览器进程,避免残留窗口与孤儿进程。
    with PROCESS_LOCK:
        browser_procs = [p for procs in BROWSER_TABLE.values() for p in procs]
        BROWSER_TABLE.clear()
    for process in browser_procs:
        if process.poll() is None:
            terminate_process(process)
    with PROCESS_LOCK:
        bridge_ids = list(set(PROCESS_TABLE) | set(LOG_PROXY_TABLE))
    traffic_by_bridge = {}
    for bridge_id in bridge_ids:
        traffic_by_bridge[bridge_id] = stop_process(bridge_id)
    if traffic_by_bridge:
        with STATE_LOCK:
            state = load_state()
            for bridge in state.get("bridges", []):
                bridge_id = bridge.get("id")
                if bridge_id in traffic_by_bridge:
                    bridge.update(traffic_by_bridge[bridge_id])
            save_state(state)


def handle_shutdown_signal(signum: int, _frame: Any) -> None:
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


def main() -> None:
    ensure_dirs()
    atexit.register(shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown_signal)
    signal.signal(signal.SIGINT, handle_shutdown_signal)
    port = safe_int(os.environ.get("PROXY_BRIDGE_PORT"), DEFAULT_SERVER_PORT)
    start_parent_monitor()
    restore_enabled_bridges()
    server = ProxyBridgeHTTPServer(("127.0.0.1", port), AppHandler)
    print(f"Proxy Bridge running at http://127.0.0.1:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        shutdown()


if __name__ == "__main__":
    main()
