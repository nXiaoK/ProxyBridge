"""指纹浏览器环境配置、启动参数、Cookie 格式与本机 CDP 客户端。

本模块只放纯逻辑，不读写 ProxyBridge 状态文件，便于主服务、测试和打包脚本复用。
所有可配置项都必须先经过这里的校验，再拼接为 fingerprint-chromium 命令行参数。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import socket
import struct
import time
import urllib.parse
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# 指纹配置
# ---------------------------------------------------------------------------

# 上报的操作系统：auto 表示跟随运行内核的系统（本机为宿主系统，Docker worker 为 linux）。
# 选择与真实系统不同的平台时，字体和图形特征仍来自真实系统，可能出现不一致。
FINGERPRINT_PLATFORMS = ("auto", "windows", "macos", "linux")
# 上报的浏览器品牌：空字符串表示内核默认值（Chromium）。Chrome 是真实用户最常见的品牌。
FINGERPRINT_BRANDS = ("", "Chrome", "Edge", "Opera", "Vivaldi")
# --disable-spoofing 可关闭的伪装项（内核 144+ 支持），用于排查网站兼容问题。
SPOOFING_SWITCHES = ("font", "audio", "canvas", "clientrects", "gpu")
# 时区/语言来源：region 按节点地区表，exit_ip 按启动前查询到的出口 IP，custom 为手动填写。
LOCALE_MODES = ("region", "exit_ip", "custom")
# 语言另有 system：不传 --lang/--accept-lang，界面语言、navigator.language 与 Intl 区域都跟随系统。
# macOS 版 Chromium 的 Intl 区域只认系统语言，改成其他语言会出现可被检测的不一致。
LANGUAGE_MODES = ("system", *LOCALE_MODES)
# macOS 上 Canvas 噪声与字体伪装会被 Pixelscan 等检测站判定为 masking，推荐关闭。
MACOS_DETECTION_FRIENDLY_DISABLED = ("canvas", "font")
# 地理位置默认权限：block 写入 Profile 默认禁止；ask 保持 Chromium 默认询问。
GEOLOCATION_MODES = ("ask", "block")
# CPU 核心数可选值；0 表示由指纹种子派生。
HARDWARE_CONCURRENCY_CHOICES = (0, 2, 4, 6, 8, 10, 12, 16, 20, 24, 32)

PLATFORM_VERSION_PATTERN = re.compile(r"\d{1,3}(?:\.\d{1,5}){0,3}")
BRAND_VERSION_PATTERN = re.compile(r"\d{1,4}(?:\.\d{1,6}){0,3}")
TIMEZONE_PATTERN = re.compile(r"(?:UTC|[A-Za-z]{2,32}(?:/[A-Za-z0-9_+\-]{1,32}){1,2})")
LANGUAGE_PATTERN = re.compile(r"[a-z]{2,3}(?:-[A-Za-z0-9]{2,8}){0,2}")
ACCEPT_LANGUAGE_PATTERN = re.compile(r"[A-Za-z0-9,;=.*\-]{1,100}")
WINDOW_SIZE_PATTERN = re.compile(r"(\d{3,4})x(\d{3,4})")
KERNEL_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
EXTENSION_ID_PATTERN = re.compile(r"[a-f0-9]{16}")

GROUP_MAX_LENGTH = 40
NAME_MAX_LENGTH = 40
START_URL_LIMIT = 10
START_URL_MAX_LENGTH = 2048
EXTENSION_LIMIT = 20

FINGERPRINT_FIELDS = (
    "platform",
    "platform_version",
    "brand",
    "brand_version",
    "hardware_concurrency",
    "disable_spoofing",
    "timezone_mode",
    "timezone",
    "language_mode",
    "language",
    "accept_language",
    "geolocation",
    "window_size",
)


def legacy_fingerprint_config() -> dict[str, Any]:
    """旧记录的等效配置：与升级前的启动参数完全一致，避免已登录账号看到指纹变化。"""
    return {
        "platform": "auto",
        "platform_version": "",
        "brand": "",
        "brand_version": "",
        "hardware_concurrency": 0,
        "disable_spoofing": [],
        "timezone_mode": "region",
        "timezone": "",
        "language_mode": "region",
        "language": "",
        "accept_language": "",
        "geolocation": "ask",
        "window_size": "",
    }


def recommended_fingerprint_config(host_platform: str = "") -> dict[str, Any]:
    """新记录默认值：Chrome 品牌、按出口 IP 匹配时区与语言、默认禁止地理定位。

    host_platform 为 macos 时关闭 Canvas 与字体伪装：实测 fingerprint-chromium 在 macOS 上这两项
    会让 Pixelscan 判定为 masking。语言在所有平台都默认按出口 IP；macOS 上 Intl 区域只跟随系统语言，
    需要规避这类不一致时可在配置里改为「跟随系统」或使用「一键应用检测友好设置」。
    """
    config = legacy_fingerprint_config()
    config.update(
        {
            "brand": "Chrome",
            "timezone_mode": "exit_ip",
            "language_mode": "exit_ip",
            "geolocation": "block",
        }
    )
    if host_platform == "macos":
        config["disable_spoofing"] = list(MACOS_DETECTION_FRIENDLY_DISABLED)
    return config


def _field_value(name: str, value: Any) -> Any:
    """校验单个字段，返回规范值；非法时抛出带中文说明的 ValueError。"""
    if name == "platform":
        text = str(value or "auto").strip().lower()
        if text not in FINGERPRINT_PLATFORMS:
            raise ValueError("平台只能是 auto、windows、macos 或 linux")
        return text
    if name == "platform_version":
        text = str(value or "").strip()
        if text and PLATFORM_VERSION_PATTERN.fullmatch(text) is None:
            raise ValueError("平台版本只能是数字和点，例如 10.0.0 或 14.5")
        return text
    if name == "brand":
        text = str(value or "").strip()
        lookup = {brand.lower(): brand for brand in FINGERPRINT_BRANDS}
        if text.lower() not in lookup:
            raise ValueError("品牌只能是 Chrome、Edge、Opera、Vivaldi 或留空使用内核默认值")
        return lookup[text.lower()]
    if name == "brand_version":
        text = str(value or "").strip()
        if text and BRAND_VERSION_PATTERN.fullmatch(text) is None:
            raise ValueError("品牌版本只能是数字和点，例如 150.0.7871.186")
        return text
    if name == "hardware_concurrency":
        if isinstance(value, bool):
            raise ValueError("CPU 核心数必须是整数")
        try:
            number = int(value or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError("CPU 核心数必须是整数") from exc
        if number not in HARDWARE_CONCURRENCY_CHOICES:
            raise ValueError("CPU 核心数只能是 0（自动）或 2–32 中的常见值")
        return number
    if name == "disable_spoofing":
        if value in (None, ""):
            return []
        items = value.split(",") if isinstance(value, str) else value
        if not isinstance(items, (list, tuple)):
            raise ValueError("关闭伪装项必须是列表")
        result = set()
        for item in items:
            text = str(item or "").strip().lower()
            if text not in SPOOFING_SWITCHES:
                raise ValueError("关闭伪装项只能是 font、audio、canvas、clientrects、gpu")
            result.add(text)
        return [item for item in SPOOFING_SWITCHES if item in result]
    if name == "timezone_mode":
        text = str(value or "region").strip().lower()
        if text not in LOCALE_MODES:
            raise ValueError("时区来源只能是 region、exit_ip 或 custom")
        return text
    if name == "language_mode":
        text = str(value or "region").strip().lower()
        if text not in LANGUAGE_MODES:
            raise ValueError("语言来源只能是 system、region、exit_ip 或 custom")
        return text
    if name == "timezone":
        text = str(value or "").strip()
        if text and TIMEZONE_PATTERN.fullmatch(text) is None:
            raise ValueError("时区必须是 IANA 名称，例如 America/New_York")
        return text
    if name == "language":
        text = str(value or "").strip()
        if text and LANGUAGE_PATTERN.fullmatch(text) is None:
            raise ValueError("语言必须是 BCP 47 格式，例如 en-US")
        return text
    if name == "accept_language":
        text = re.sub(r"\s+", "", str(value or ""))
        if text and ACCEPT_LANGUAGE_PATTERN.fullmatch(text) is None:
            raise ValueError("Accept-Language 格式无效，例如 en-US,en;q=0.9")
        return text
    if name == "geolocation":
        text = str(value or "ask").strip().lower()
        if text not in GEOLOCATION_MODES:
            raise ValueError("地理位置只能是 ask 或 block")
        return text
    if name == "window_size":
        text = str(value or "").strip().lower().replace("×", "x").replace("*", "x")
        if not text:
            return ""
        match = WINDOW_SIZE_PATTERN.fullmatch(text)
        if match is None:
            raise ValueError("窗口尺寸格式为 宽x高，例如 1920x1080")
        width, height = int(match.group(1)), int(match.group(2))
        if not (640 <= width <= 7680 and 480 <= height <= 4320):
            raise ValueError("窗口尺寸需在 640x480 到 7680x4320 之间")
        return f"{width}x{height}"
    raise ValueError(f"未知指纹字段：{name}")


def validate_fingerprint_patch(patch: Any, base: dict[str, Any] | None = None) -> dict[str, Any]:
    """严格校验 API 提交的部分指纹配置，并合并到 base 上返回完整配置。"""
    if not isinstance(patch, dict):
        raise ValueError("指纹配置必须是对象")
    unknown = sorted(set(patch) - set(FINGERPRINT_FIELDS))
    if unknown:
        raise ValueError(f"未知指纹字段：{unknown[0]}")
    result = dict(base or legacy_fingerprint_config())
    for name, value in patch.items():
        result[name] = _field_value(name, value)
    if result["timezone_mode"] == "custom" and not result["timezone"]:
        raise ValueError("自定义时区时必须填写时区")
    if result["language_mode"] == "custom" and not result["language"]:
        raise ValueError("自定义语言时必须填写语言")
    if not result["brand"]:
        # 内核默认品牌没有独立版本号；保留会让 UI 误以为已生效。
        result["brand_version"] = ""
    return result


def normalize_fingerprint_config(value: Any, fallback: dict[str, Any]) -> dict[str, Any]:
    """宽松规范化状态文件或同步来的配置：单个非法字段回退到 fallback，不整体丢弃。"""
    source = value if isinstance(value, dict) else {}
    result: dict[str, Any] = {}
    for name in FINGERPRINT_FIELDS:
        try:
            result[name] = _field_value(name, source.get(name, fallback.get(name)))
        except ValueError:
            result[name] = _field_value(name, fallback.get(name))
    if result["timezone_mode"] == "custom" and not result["timezone"]:
        result["timezone_mode"] = fallback.get("timezone_mode") or "region"
        if result["timezone_mode"] == "custom":
            result["timezone_mode"] = "region"
    if result["language_mode"] == "custom" and not result["language"]:
        result["language_mode"] = fallback.get("language_mode") or "region"
        if result["language_mode"] == "custom":
            result["language_mode"] = "region"
    if not result["brand"]:
        result["brand_version"] = ""
    return result


def normalize_group(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    return text[:GROUP_MAX_LENGTH].rstrip()


def normalize_name(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise ValueError("名称不能包含控制字符")
    return text[:NAME_MAX_LENGTH].rstrip()


def validate_start_url(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError("启动网址不能为空")
    if len(text) > START_URL_MAX_LENGTH:
        raise ValueError("启动网址过长")
    if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in text) or any(
        char in text for char in "\"'\\<>`"
    ):
        raise ValueError(f"启动网址包含非法字符：{text[:80]}")
    parsed = urllib.parse.urlsplit(text)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"启动网址只能是 http 或 https 地址：{text[:80]}")
    return text


def normalize_start_urls(value: Any, *, strict: bool) -> list[str]:
    if value in (None, ""):
        return []
    items = value.splitlines() if isinstance(value, str) else value
    if not isinstance(items, (list, tuple)):
        if strict:
            raise ValueError("启动网址必须是列表")
        return []
    result: list[str] = []
    for item in items:
        if not str(item or "").strip():
            continue
        try:
            url = validate_start_url(item)
        except ValueError:
            if strict:
                raise
            continue
        if url not in result:
            result.append(url)
    if len(result) > START_URL_LIMIT:
        if strict:
            raise ValueError(f"启动网址最多 {START_URL_LIMIT} 个")
        result = result[:START_URL_LIMIT]
    return result


def normalize_extension_ids(value: Any, *, strict: bool) -> list[str]:
    if value in (None, ""):
        return []
    if not isinstance(value, (list, tuple)):
        if strict:
            raise ValueError("扩展列表必须是数组")
        return []
    result: list[str] = []
    for item in value:
        text = str(item or "").strip().lower()
        if EXTENSION_ID_PATTERN.fullmatch(text) is None:
            if strict:
                raise ValueError("扩展 ID 无效")
            continue
        if text not in result:
            result.append(text)
    if len(result) > EXTENSION_LIMIT:
        if strict:
            raise ValueError(f"每个浏览器最多启用 {EXTENSION_LIMIT} 个扩展")
        result = result[:EXTENSION_LIMIT]
    return result


def normalize_kernel_id(value: Any, *, strict: bool) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if KERNEL_ID_PATTERN.fullmatch(text) is None:
        if strict:
            raise ValueError("内核版本标识无效")
        return ""
    return text


def resolve_platform(config: dict[str, Any], host_platform: str) -> str:
    platform = str(config.get("platform") or "auto")
    return host_platform if platform == "auto" else platform


def resolve_locale(
    config: dict[str, Any],
    region_locale: tuple[str, str, str],
    exit_locale: tuple[str, str, str] | None,
) -> dict[str, str]:
    """按配置选择最终时区与语言，返回值包含来源，便于在界面上解释生效结果。"""
    region_timezone, region_language, region_accept = region_locale
    timezone, timezone_source = region_timezone, "region"
    language, accept_language, language_source = region_language, region_accept, "region"
    if config.get("timezone_mode") == "custom" and config.get("timezone"):
        timezone, timezone_source = str(config["timezone"]), "custom"
    elif config.get("timezone_mode") == "exit_ip":
        if exit_locale and exit_locale[0]:
            timezone, timezone_source = exit_locale[0], "exit_ip"
        else:
            timezone_source = "region_fallback"
    if config.get("language_mode") == "system":
        # 空语言表示不传语言参数，由浏览器使用系统语言。
        language, accept_language, language_source = "", "", "system"
    elif config.get("language_mode") == "custom" and config.get("language"):
        language = str(config["language"])
        accept_language = str(config.get("accept_language") or default_accept_language(language))
        language_source = "custom"
    elif config.get("language_mode") == "exit_ip":
        if exit_locale and exit_locale[1]:
            language, accept_language = exit_locale[1], exit_locale[2] or default_accept_language(exit_locale[1])
            language_source = "exit_ip"
        else:
            language_source = "region_fallback"
    return {
        "timezone": timezone,
        "language": language,
        "accept_language": accept_language,
        "timezone_source": timezone_source,
        "language_source": language_source,
    }


def default_accept_language(language: str) -> str:
    base = language.split("-", 1)[0]
    if base == language:
        return language if base == "en" else f"{language},en"
    if base == "en":
        return f"{language},en"
    return f"{language},{base},en"


def chromium_accept_languages(value: str, language: str = "") -> str:
    """把 Accept-Language 转成 --accept-lang 需要的纯语言列表。

    Chromium 会自行生成带 q 权重的请求头；参数里带 ;q= 会让 navigator.languages 出现
    「en;q=0.9」这类真实浏览器不可能有的条目。
    """
    items: list[str] = []
    for part in str(value or "").split(","):
        tag = part.split(";", 1)[0].strip()
        if tag and tag not in items:
            items.append(tag)
    if language and (not items or items[0] != language):
        items = [language, *[item for item in items if item != language]]
    return ",".join(items)


def fingerprint_switches(
    config: dict[str, Any],
    *,
    seed: int,
    host_platform: str,
    locale: dict[str, str],
) -> list[str]:
    """把规范化后的配置转换为 fingerprint-chromium 命令行参数。"""
    switches = [
        # --fingerprint 是指纹伪造的总开关：同种子得到同指纹，不同种子得到不同指纹。
        f"--fingerprint={seed}",
        f"--fingerprint-platform={resolve_platform(config, host_platform)}",
    ]
    if config.get("platform_version"):
        switches.append(f"--fingerprint-platform-version={config['platform_version']}")
    if config.get("brand"):
        switches.append(f"--fingerprint-brand={config['brand']}")
        if config.get("brand_version"):
            switches.append(f"--fingerprint-brand-version={config['brand_version']}")
    if config.get("hardware_concurrency"):
        switches.append(f"--fingerprint-hardware-concurrency={int(config['hardware_concurrency'])}")
    if config.get("disable_spoofing"):
        switches.append("--disable-spoofing=" + ",".join(config["disable_spoofing"]))
    switches.append(f"--timezone={locale['timezone']}")
    if locale.get("language"):
        switches.extend(
            [
                f"--lang={locale['language']}",
                f"--accept-lang={chromium_accept_languages(locale.get('accept_language') or '', locale['language'])}",
            ]
        )
    if config.get("window_size"):
        width, height = str(config["window_size"]).split("x", 1)
        switches.append(f"--window-size={width},{height}")
    return switches


def apply_geolocation_preference(prefs: dict[str, Any], mode: str) -> None:
    """把地理位置默认权限写入 Chromium Preferences；2 表示禁止。"""
    profile = prefs.get("profile")
    if not isinstance(profile, dict):
        profile = {}
        prefs["profile"] = profile
    defaults = profile.get("default_content_setting_values")
    if not isinstance(defaults, dict):
        defaults = {}
        profile["default_content_setting_values"] = defaults
    if mode == "block":
        defaults["geolocation"] = 2
    elif defaults.get("geolocation") == 2:
        defaults.pop("geolocation", None)


def fingerprint_version_from_name(name: str) -> str:
    """从内核安装包文件名或 Release tag 中提取 Chromium 版本号。"""
    match = re.search(r"(?<![\d.])(\d{2,4}\.\d+\.\d+\.\d+)(?![\d.])", str(name or ""))
    if match:
        return match.group(1)
    match = re.search(r"(?<![\d.])(\d{2,4}\.\d+\.\d+)(?![\d.])", str(name or ""))
    return match.group(1) if match else ""


def version_sort_key(value: str) -> tuple[int, ...]:
    parts = re.findall(r"\d+", str(value or ""))
    return tuple(int(part) for part in parts[:4]) or (0,)


# ---------------------------------------------------------------------------
# Cookie 导入导出
# ---------------------------------------------------------------------------

COOKIE_LIMIT = 5000
COOKIE_NAME_MAX = 4096
COOKIE_VALUE_MAX = 16384
COOKIE_SAME_SITE_TO_CDP = {
    "strict": "Strict",
    "lax": "Lax",
    "none": "None",
    "no_restriction": "None",
}
COOKIE_SAME_SITE_FROM_CDP = {"Strict": "strict", "Lax": "lax", "None": "no_restriction"}


def _cookie_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes"}
    return value is True or value == 1


def _cookie_expires(item: dict[str, Any]) -> float | None:
    for key in ("expirationDate", "expires", "expiry"):
        if key not in item or item[key] in (None, ""):
            continue
        try:
            number = float(item[key])
        except (TypeError, ValueError):
            continue
        if number > 1e12:
            # 部分工具以毫秒导出。
            number /= 1000
        return number if number > 0 else None
    return None


def _cookie_param(item: dict[str, Any]) -> dict[str, Any]:
    name = str(item.get("name") or "")
    value = str(item.get("value") if item.get("value") is not None else "")
    domain = str(item.get("domain") or item.get("host") or "").strip()
    path = str(item.get("path") or "/") or "/"
    if not name or len(name) > COOKIE_NAME_MAX or len(value) > COOKIE_VALUE_MAX:
        raise ValueError("Cookie 名称为空或长度超限")
    if not domain or any(char.isspace() for char in domain) or len(domain) > 253:
        raise ValueError(f"Cookie {name[:40]} 缺少有效域名")
    if not path.startswith("/"):
        path = "/"
    secure = _cookie_bool(item.get("secure"))
    http_only = _cookie_bool(item.get("httpOnly") if "httpOnly" in item else item.get("http_only"))
    host_only_value = item.get("hostOnly")
    host_only = _cookie_bool(host_only_value) if host_only_value is not None else not domain.startswith(".")
    param: dict[str, Any] = {
        "name": name,
        "value": value,
        "path": path,
        "secure": secure,
        "httpOnly": http_only,
    }
    bare_domain = domain.lstrip(".")
    if host_only:
        # CDP 只有通过 url 且不传 domain 才会创建 host-only Cookie。
        param["url"] = f"{'https' if secure else 'http'}://{bare_domain}{path}"
    else:
        param["domain"] = f".{bare_domain}"
    same_site = str(item.get("sameSite") or item.get("same_site") or "").strip().lower()
    if same_site in COOKIE_SAME_SITE_TO_CDP:
        param["sameSite"] = COOKIE_SAME_SITE_TO_CDP[same_site]
        if param["sameSite"] == "None" and not secure:
            # Chromium 会拒绝非 secure 的 SameSite=None；降级为默认策略而不是整体失败。
            param.pop("sameSite")
    session_value = item.get("session")
    expires = None if _cookie_bool(session_value) else _cookie_expires(item)
    if expires is not None:
        param["expires"] = expires
    return param


def _parse_netscape(text: str) -> list[dict[str, Any]]:
    cookies: list[dict[str, Any]] = []
    for raw_line in text.splitlines():
        line = raw_line.rstrip("\r\n")
        http_only = False
        if line.startswith("#HttpOnly_"):
            http_only = True
            line = line[len("#HttpOnly_"):]
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            raise ValueError("Netscape Cookie 行必须包含 7 个 Tab 分隔字段")
        domain, include_subdomains, path, secure, expires, name = parts[:6]
        value = "\t".join(parts[6:])
        cookies.append(
            {
                "domain": domain,
                "hostOnly": include_subdomains.strip().upper() != "TRUE",
                "path": path,
                "secure": secure.strip().upper() == "TRUE",
                "expirationDate": expires.strip() or 0,
                "name": name,
                "value": value,
                "httpOnly": http_only,
                "session": (expires.strip() in {"", "0"}),
            }
        )
    return cookies


def parse_cookie_text(text: str, fmt: str = "auto") -> list[dict[str, Any]]:
    """解析 JSON（Cookie-Editor/EditThisCookie/CDP 风格）或 Netscape cookies.txt，返回 CDP CookieParam。"""
    content = str(text or "").lstrip("﻿").strip()
    if not content:
        raise ValueError("Cookie 内容为空")
    selected = str(fmt or "auto").lower()
    if selected not in {"auto", "json", "netscape"}:
        raise ValueError("Cookie 格式只能是 auto、json 或 netscape")
    if selected == "json" or (selected == "auto" and content[:1] in "[{"):
        try:
            loaded = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Cookie JSON 解析失败：{exc.msg}") from exc
        if isinstance(loaded, dict):
            loaded = loaded.get("cookies")
        if not isinstance(loaded, list):
            raise ValueError("Cookie JSON 必须是数组或包含 cookies 数组的对象")
        items = loaded
    else:
        items = _parse_netscape(content)
    if len(items) > COOKIE_LIMIT:
        raise ValueError(f"单次最多导入 {COOKIE_LIMIT} 条 Cookie")
    params: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(f"第 {index + 1} 条 Cookie 不是对象")
        try:
            params.append(_cookie_param(item))
        except ValueError as exc:
            raise ValueError(f"第 {index + 1} 条 Cookie 无效：{exc}") from exc
    return params


def format_cookies_json(cookies: list[dict[str, Any]]) -> str:
    """导出为 Cookie-Editor/EditThisCookie 兼容 JSON，商业指纹浏览器通常也能直接导入。"""
    result = []
    for cookie in cookies:
        domain = str(cookie.get("domain") or "")
        session = bool(cookie.get("session")) or float(cookie.get("expires") or -1) <= 0
        item: dict[str, Any] = {
            "domain": domain,
            "hostOnly": not domain.startswith("."),
            "httpOnly": bool(cookie.get("httpOnly")),
            "name": str(cookie.get("name") or ""),
            "path": str(cookie.get("path") or "/"),
            "sameSite": COOKIE_SAME_SITE_FROM_CDP.get(str(cookie.get("sameSite") or ""), "unspecified"),
            "secure": bool(cookie.get("secure")),
            "session": session,
            "value": str(cookie.get("value") or ""),
        }
        if not session:
            item["expirationDate"] = float(cookie.get("expires") or 0)
        result.append(item)
    return json.dumps(result, ensure_ascii=False, indent=2)


def format_cookies_netscape(cookies: list[dict[str, Any]]) -> str:
    lines = ["# Netscape HTTP Cookie File", "# Exported by ProxyBridge", ""]
    for cookie in cookies:
        domain = str(cookie.get("domain") or "")
        name = str(cookie.get("name") or "")
        value = str(cookie.get("value") or "")
        if any(char in name + value + domain for char in "\r\n"):
            continue
        expires = float(cookie.get("expires") or 0)
        prefix = "#HttpOnly_" if cookie.get("httpOnly") else ""
        lines.append(
            "\t".join(
                [
                    f"{prefix}{domain}",
                    "TRUE" if domain.startswith(".") else "FALSE",
                    str(cookie.get("path") or "/"),
                    "TRUE" if cookie.get("secure") else "FALSE",
                    str(int(expires)) if expires > 0 and not cookie.get("session") else "0",
                    name,
                    value,
                ]
            )
        )
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# 扩展包
# ---------------------------------------------------------------------------


def crx_zip_offset(header: bytes) -> int:
    """返回 CRX 包内 zip 数据的起始偏移；普通 zip 返回 0。"""
    if header[:4] != b"Cr24":
        return 0
    if len(header) < 12:
        raise ValueError("CRX 文件头不完整")
    version = struct.unpack("<I", header[4:8])[0]
    if version == 3:
        header_size = struct.unpack("<I", header[8:12])[0]
        return 12 + header_size
    if version == 2:
        if len(header) < 16:
            raise ValueError("CRX 文件头不完整")
        key_size, signature_size = struct.unpack("<II", header[8:16])
        return 16 + key_size + signature_size
    raise ValueError(f"不支持的 CRX 版本：{version}")


def read_extension_manifest(root: Path) -> dict[str, Any]:
    manifest_path = root / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("扩展包缺少有效的 manifest.json") from exc
    if not isinstance(manifest, dict) or manifest.get("manifest_version") not in (2, 3):
        raise ValueError("manifest.json 的 manifest_version 只能是 2 或 3")
    name = str(manifest.get("name") or "").strip()
    version = str(manifest.get("version") or "").strip()
    if not name or not version or len(name) > 200 or len(version) > 64:
        raise ValueError("manifest.json 缺少有效的 name 或 version")
    display_name = name
    message = re.fullmatch(r"__MSG_([A-Za-z0-9_@]+)__", name)
    if message:
        locale = str(manifest.get("default_locale") or "en")
        for candidate in (locale, locale.replace("-", "_"), "en", "en_US"):
            messages_path = root / "_locales" / candidate / "messages.json"
            try:
                messages = json.loads(messages_path.read_text(encoding="utf-8-sig"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            entry = next(
                (value for key, value in messages.items() if key.lower() == message.group(1).lower()),
                None,
            ) if isinstance(messages, dict) else None
            if isinstance(entry, dict) and str(entry.get("message") or "").strip():
                display_name = str(entry["message"]).strip()[:200]
                break
    return {"name": display_name, "version": version, "manifest_version": manifest["manifest_version"]}


# ---------------------------------------------------------------------------
# 本机 CDP 客户端（仅连接 127.0.0.1）
# ---------------------------------------------------------------------------

DEVTOOLS_ACTIVE_PORT_FILE = "DevToolsActivePort"
CDP_MESSAGE_LIMIT = 64 * 1024 * 1024
_WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class CdpError(RuntimeError):
    pass


def read_devtools_active_port(profile_path: Path) -> tuple[int, str] | None:
    """读取 Chromium 在 --remote-debugging-port=0 时写入 Profile 的实际端口与浏览器 ws 路径。"""
    try:
        lines = (profile_path / DEVTOOLS_ACTIVE_PORT_FILE).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return None
    if len(lines) < 2:
        return None
    try:
        port = int(lines[0].strip())
    except ValueError:
        return None
    path = lines[1].strip()
    if not 1024 <= port <= 65535 or re.fullmatch(r"/devtools/browser/[A-Za-z0-9-]{8,64}", path) is None:
        return None
    return port, path


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = sock.recv(size - len(chunks))
        if not chunk:
            raise CdpError("CDP 连接已关闭")
        chunks.extend(chunk)
    return bytes(chunks)


def encode_websocket_frame(payload: bytes, opcode: int = 0x1, mask: bytes | None = None) -> bytes:
    mask_key = mask if mask is not None else os.urandom(4)
    header = bytearray([0x80 | opcode])
    length = len(payload)
    if length < 126:
        header.append(0x80 | length)
    elif length < 65536:
        header.append(0x80 | 126)
        header.extend(struct.pack(">H", length))
    else:
        header.append(0x80 | 127)
        header.extend(struct.pack(">Q", length))
    header.extend(mask_key)
    masked = bytes(byte ^ mask_key[index % 4] for index, byte in enumerate(payload))
    return bytes(header) + masked


def _read_frame(sock: socket.socket) -> tuple[bool, int, bytes]:
    first, second = _recv_exact(sock, 2)
    fin = bool(first & 0x80)
    opcode = first & 0x0F
    length = second & 0x7F
    if length == 126:
        length = struct.unpack(">H", _recv_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack(">Q", _recv_exact(sock, 8))[0]
    if length > CDP_MESSAGE_LIMIT:
        raise CdpError("CDP 消息过大")
    mask = _recv_exact(sock, 4) if second & 0x80 else b""
    payload = _recv_exact(sock, length) if length else b""
    if mask:
        payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    return fin, opcode, payload


def _read_message(sock: socket.socket) -> bytes | None:
    message = bytearray()
    while True:
        fin, opcode, payload = _read_frame(sock)
        if opcode == 0x8:
            return None
        if opcode == 0x9:
            sock.sendall(encode_websocket_frame(payload, opcode=0xA))
            continue
        if opcode == 0xA:
            continue
        message.extend(payload)
        if len(message) > CDP_MESSAGE_LIMIT:
            raise CdpError("CDP 消息过大")
        if fin:
            return bytes(message)


class CdpSession:
    """浏览器级 CDP WebSocket 会话。只连接 127.0.0.1，避免被状态文件诱导访问外部主机。"""

    def __init__(self, port: int, ws_path: str, *, timeout: float = 15.0) -> None:
        if not 1024 <= int(port) <= 65535 or re.fullmatch(r"/devtools/browser/[A-Za-z0-9-]{8,64}", ws_path) is None:
            raise CdpError("CDP 端点无效")
        self.port = int(port)
        self.ws_path = ws_path
        self.timeout = timeout
        self.sock: socket.socket | None = None
        self.next_id = 1

    def __enter__(self) -> "CdpSession":
        try:
            sock = socket.create_connection(("127.0.0.1", self.port), timeout=self.timeout)
        except OSError as exc:
            raise CdpError(f"无法连接浏览器调试端口：{exc}") from exc
        self.sock = sock
        try:
            self._handshake()
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    def _handshake(self) -> None:
        assert self.sock is not None
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {self.ws_path} HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{self.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        try:
            self.sock.sendall(request.encode("ascii"))
            response = bytearray()
            while b"\r\n\r\n" not in response:
                chunk = self.sock.recv(4096)
                if not chunk:
                    raise CdpError("CDP 握手失败")
                response.extend(chunk)
                if len(response) > 16384:
                    raise CdpError("CDP 握手响应过大")
        except OSError as exc:
            raise CdpError(f"CDP 握手失败：{exc}") from exc
        head, _, rest = bytes(response).partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        if not lines or " 101 " not in f"{lines[0]} ":
            raise CdpError(f"CDP 握手失败：{lines[0] if lines else ''}")
        headers = {
            name.strip().lower(): value.strip()
            for name, _, value in (line.partition(":") for line in lines[1:])
        }
        expected = base64.b64encode(hashlib.sha1((key + _WEBSOCKET_GUID).encode("ascii")).digest()).decode("ascii")
        if headers.get("sec-websocket-accept") != expected:
            raise CdpError("CDP 握手校验失败")
        if rest:
            raise CdpError("CDP 握手后收到意外数据")

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if self.sock is None:
            raise CdpError("CDP 会话未连接")
        request_id = self.next_id
        self.next_id += 1
        deadline = time.monotonic() + self.timeout
        try:
            self.sock.settimeout(self.timeout)
            self.sock.sendall(
                encode_websocket_frame(
                    json.dumps({"id": request_id, "method": method, "params": params or {}}).encode("utf-8")
                )
            )
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CdpError(f"CDP 调用超时：{method}")
                self.sock.settimeout(remaining)
                raw = _read_message(self.sock)
                if raw is None:
                    raise CdpError("CDP 连接被浏览器关闭")
                try:
                    message = json.loads(raw.decode("utf-8"))
                except (UnicodeError, json.JSONDecodeError):
                    continue
                if not isinstance(message, dict) or message.get("id") != request_id:
                    continue
                if isinstance(message.get("error"), dict):
                    raise CdpError(str(message["error"].get("message") or "CDP 调用失败")[:300])
                result = message.get("result")
                return result if isinstance(result, dict) else {}
        except socket.timeout as exc:
            raise CdpError(f"CDP 调用超时：{method}") from exc
        except OSError as exc:
            raise CdpError(f"CDP 通信失败：{exc}") from exc

    def close(self) -> None:
        if self.sock is None:
            return
        try:
            self.sock.sendall(encode_websocket_frame(b"", opcode=0x8))
        except OSError:
            pass
        self.sock.close()
        self.sock = None


def cdp_call(
    port: int,
    ws_path: str,
    method: str,
    params: dict[str, Any] | None = None,
    *,
    timeout: float = 15.0,
) -> dict[str, Any]:
    """对浏览器级 CDP 端点执行一次调用。"""
    with CdpSession(port, ws_path, timeout=timeout) as session:
        return session.call(method, params)


def set_cookies(session: CdpSession, params: list[dict[str, Any]], batch_size: int = 200) -> tuple[int, list[str]]:
    """批量写入 Cookie；某一批被拒绝时逐条重试，返回 (成功数, 失败说明)。"""
    imported = 0
    failures: list[str] = []
    for start in range(0, len(params), batch_size):
        chunk = params[start:start + batch_size]
        try:
            session.call("Storage.setCookies", {"cookies": chunk})
            imported += len(chunk)
            continue
        except CdpError:
            pass
        for item in chunk:
            try:
                session.call("Storage.setCookies", {"cookies": [item]})
                imported += 1
            except CdpError as exc:
                failures.append(f"{item.get('name', '')[:40]}@{(item.get('domain') or item.get('url') or '')[:60]}: {exc}")
    return imported, failures
