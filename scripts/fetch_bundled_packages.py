#!/usr/bin/env python3
"""下载「完整版（离线包）」需要内置的官方安装包：sing-box、Xray 与 fingerprint-chromium 指纹内核。

用法：
  python scripts/fetch_bundled_packages.py --os darwin --arch arm64 --out bundled
  python scripts/fetch_bundled_packages.py --os windows --arch x86_64 --out bundled

输出目录结构与程序运行时查找的离线包目录一致：
  <out>/sing-box/<官方文件名>
  <out>/xray/<官方文件名>
  <out>/fingerprint-chromium/<官方文件名>

选包规则直接复用 app.py 中在线下载使用的同一套函数，保证离线包与首次启动在线下载得到的是同一类安装包；
GitHub 提供 SHA-256 摘要时下载后会自动校验。

环境变量：
  GITHUB_TOKEN：可选，查询 GitHub Releases API 时使用的令牌，仅用于提高匿名请求的频率上限（CI 中传入 github.token）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

import app  # noqa: E402  复用选包与带校验的下载逻辑

API_LIMIT = 8 * 1024 * 1024


def github_json(url: str) -> object:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "ProxyBridge-release"}
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=60) as response:
        data = response.read(API_LIMIT + 1)
    if len(data) > API_LIMIT:
        raise RuntimeError(f"GitHub API 响应过大：{url}")
    return json.loads(data)


def pick_assets(host_os: str, host_arch: str) -> dict[str, tuple[dict, str]]:
    """返回 {类型: (资源, Release 标签)}。"""
    picked: dict[str, tuple[dict, str]] = {}
    sing_box = github_json(app.SING_BOX_RELEASES_API)
    asset = app._pick_sing_box_asset(sing_box, host_arch, host_os)
    if not asset:
        raise RuntimeError(f"sing-box 最新版本没有适配 {host_os}/{host_arch} 的安装包")
    picked["sing-box"] = (asset, str(sing_box.get("tag_name") or ""))

    xray = github_json(app.XRAY_RELEASES_API)
    asset = app._pick_xray_asset(xray, host_arch, host_os)
    if not asset:
        raise RuntimeError(f"Xray 最新版本没有适配 {host_os}/{host_arch} 的安装包")
    picked["xray"] = (asset, str(xray.get("tag_name") or ""))

    # 上游并非每个版本都发布全部平台，选包函数会从最近的 Release 中挑选最新的可用包。
    releases = github_json(f"{app.FP_CHROMIUM_RELEASES_API}?per_page=30")
    asset = app._pick_fingerprint_asset(releases if isinstance(releases, list) else [], host_os=host_os, host_arch=host_arch)
    if not asset:
        raise RuntimeError(f"fingerprint-chromium 没有适配 {host_os}/{host_arch} 的安装包")
    picked["fingerprint-chromium"] = (asset, str(asset.get("tag") or ""))
    return picked


def main() -> int:
    # Windows Runner 的控制台默认 cp1252，无法输出中文进度；统一改为 UTF-8，无法编码的字符替换而不中断下载。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="下载完整版需要内置的官方安装包")
    parser.add_argument("--os", dest="host_os", required=True, choices=("darwin", "windows", "linux"))
    parser.add_argument("--arch", dest="host_arch", required=True, choices=("arm64", "x86_64"))
    parser.add_argument("--out", required=True, help="输出目录，会为每种类型创建子目录")
    args = parser.parse_args()

    out = Path(args.out).resolve()
    manifest: dict[str, dict[str, object]] = {}
    for kind, (asset, tag) in pick_assets(args.host_os, args.host_arch).items():
        name = app._safe_asset_name(asset)
        target = out / kind / name
        for stale in (out / kind).glob("*") if (out / kind).is_dir() else []:
            if stale.is_file() and stale != target:
                stale.unlink()
        if not target.is_file():
            print(f"下载 {kind} {tag}: {name}", flush=True)
            app._download_asset(asset, target)
        manifest[kind] = {
            "tag": tag,
            "file": name,
            "size": target.stat().st_size,
            "sha256": app._file_sha256(target),
        }
        print(f"{kind}: {name} ({manifest[kind]['size']} bytes, sha256 {manifest[kind]['sha256']})", flush=True)
    (out / "bundled-packages.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
