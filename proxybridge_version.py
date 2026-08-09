"""ProxyBridge 运行时版本解析。

仓库根目录的 VERSION 是基础版本唯一来源；发布构建可通过环境变量注入带
build 序号的完整版本，并由打包脚本把该值写回产物中的 VERSION 文件。
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path


VERSION_ENV = "PROXY_BRIDGE_VERSION"
DEVELOPMENT_VERSION = "0.0.0-dev"
VERSION_PATTERN = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?"
    r"(?:\+[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?$"
)


def validate_version(value: str) -> str:
    """校验并返回可用于界面、Release 标签和平台元数据的版本号。"""
    normalized = str(value).strip()
    if not VERSION_PATTERN.fullmatch(normalized):
        raise ValueError(
            "ProxyBridge 版本号必须是 SemVer，例如 0.3.0 或 0.3.0-build.12"
        )
    return normalized


def resolve_version(
    root: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> str:
    """优先读取构建注入值，其次读取产物或仓库中的 VERSION。"""
    environment = os.environ if environ is None else environ
    override = environment.get(VERSION_ENV, "").strip()
    if override:
        return validate_version(override)

    version_root = Path(__file__).resolve().parent if root is None else Path(root)
    version_file = version_root / "VERSION"
    try:
        value = version_file.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        # 单独复制源码文件进行开发时保持可启动；正式构建会验证 VERSION 已打包。
        return DEVELOPMENT_VERSION
    return validate_version(value)


VERSION = resolve_version()
