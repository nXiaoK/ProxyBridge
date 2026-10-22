#!/usr/bin/env python3
"""发布流水线使用的软件更新工具：计算外壳修订号、打包后端载荷、生成并签名更新清单。

子命令：
- shell-revision --shell macos-app|windows-exe
    输出外壳修订号（外壳源文件的 SHA-256 汇总）。修订号与已安装外壳一致时，客户端只需热更新
    后端载荷；不一致时需要整包替换。
- payload --version 0.3.0-build.12 --out release-assets
    生成 ProxyBridge-payload-<版本>.zip：后端模块、static/、vendor/（PyYAML 纯 Python 版本）与 VERSION。
- manifest --version ... --tag ... --commit ... --assets-dir release-assets \
           --macos-revision <修订号> --windows-revision <修订号>
    生成 update-manifest.json；设置了 PROXY_BRIDGE_UPDATE_SIGNING_KEY 时同时生成 update-manifest.json.sig。

环境变量：
- PROXY_BRIDGE_UPDATE_SIGNING_KEY：更新清单签名私钥，64 位十六进制 Ed25519 种子，只应配置在
  GitHub 仓库 Secret 中。未设置时只生成未签名清单，客户端会提示手动下载而不会自动安装。
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app_payload  # noqa: E402
import app_update  # noqa: E402

# 后端载荷中的顶层 Python 模块；新增被 app.py 导入的同级模块时必须同步加入，
# 并同时加入 macOS / Windows / Linux 构建脚本与 Dockerfile。
PAYLOAD_MODULES = (
    "app.py",
    "app_payload.py",
    "app_update.py",
    "browser_fingerprint.py",
    "proxybridge_version.py",
    "remote_browser.py",
    "webdav_sync.py",
)
# 外壳修订号覆盖的源文件：这些文件决定外壳与后端之间的启动约定、托盘或菜单栏行为。
# 改动其中任意文件都会让下一个版本走整包替换，而不是只热更新后端载荷。
SHELL_SOURCES = {
    app_update.SHELL_KIND_MACOS: (
        "app_payload.py",
        "macos/ProxyBridgeMenuBar/Info.plist",
        "macos/ProxyBridgeMenuBar/main.swift",
        "scripts/build_menubar_app.sh",
        "scripts/generate_app_icon.swift",
    ),
    app_update.SHELL_KIND_WINDOWS: (
        "app_payload.py",
        "desktop/__init__.py",
        "desktop/__main__.py",
        "desktop/autostart.py",
        "desktop/requirements.txt",
        "desktop/tray_host.py",
        "scripts/build_windows_exe.ps1",
        "static/assets/app-icon.png",
    ),
}
SIGNING_KEY_ENV = "PROXY_BRIDGE_UPDATE_SIGNING_KEY"
# 固定 zip 内文件时间戳，同一提交重复构建得到相同载荷。
ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
MACOS_ASSET_TEMPLATE = "ProxyBridge-macOS-arm64-{version}.zip"
WINDOWS_ASSET_TEMPLATE = "ProxyBridge-Windows-x64-{version}.exe"


def shell_revision(kind: str, root: Path = ROOT) -> str:
    """按相对路径排序后对每个文件内容求 SHA-256；统一 CRLF，避免 Windows 检出换行差异。"""
    digest = hashlib.sha256()
    digest.update(f"proxybridge-shell-revision-v1\0{kind}\0".encode("utf-8"))
    for relative in sorted(SHELL_SOURCES[kind]):
        content = (root / relative).read_bytes().replace(b"\r\n", b"\n")
        digest.update(relative.encode("utf-8") + b"\0")
        digest.update(hashlib.sha256(content).digest())
    return digest.hexdigest()


def vendor_dependencies(target: Path, python: str, wheelhouse: str = "") -> None:
    """与 macOS App 相同：安装锁定的 PyYAML 轮子后删除编译扩展，只保留纯 Python 模块。"""
    command = [
        python, "-m", "pip", "install",
        "--disable-pip-version-check", "--no-deps", "--no-compile", "--require-hashes",
        "--only-binary=:all:", "--platform", "macosx_11_0_arm64", "--python-version", "39",
        "--implementation", "cp", "--abi", "cp39",
        "--target", str(target), "-r", str(ROOT / "scripts" / "requirements.lock"),
    ]
    if wheelhouse:
        command += ["--no-index", "--find-links", wheelhouse]
    subprocess.run(command, check=True)
    for path in sorted(target.rglob("*"), reverse=True):
        if path.is_file() and path.suffix in {".so", ".dylib", ".pyd"}:
            path.unlink()
        elif path.is_dir() and path.name == "__pycache__":
            shutil.rmtree(path)
    check = subprocess.run(
        [python, "-I", "-c", "import sys; sys.path.insert(0, sys.argv[1]); import yaml; "
         "assert yaml.safe_load('ok: true') == {'ok': True}", str(target)],
        check=False,
    )
    if check.returncode != 0:
        raise SystemExit("vendor/ 中的 PyYAML 无法导入")


def _payload_files(staging: Path) -> list[tuple[str, Path]]:
    entries: list[tuple[str, Path]] = []
    for path in sorted(staging.rglob("*")):
        if not path.is_file() or path.name == ".DS_Store" or "__pycache__" in path.parts:
            continue
        if path.suffix in {".pyc", ".pyo"}:
            continue
        entries.append((path.relative_to(staging).as_posix(), path))
    return entries


def build_payload(version: str, out_dir: Path, *, python: str, wheelhouse: str = "", vendor_from: str = "") -> Path:
    if app_payload.parse_version(version) is None:
        raise SystemExit(f"版本号无效：{version}")
    out_dir.mkdir(parents=True, exist_ok=True)
    archive = out_dir / app_update.payload_asset_name(version)
    with tempfile.TemporaryDirectory(prefix="proxybridge-payload-") as temp:
        staging = Path(temp) / "payload"
        staging.mkdir()
        for name in PAYLOAD_MODULES:
            shutil.copy2(ROOT / name, staging / name)
        shutil.copytree(ROOT / "static", staging / "static", ignore=shutil.ignore_patterns(".DS_Store"))
        (staging / "VERSION").write_text(f"{version}\n", encoding="utf-8")
        if vendor_from:
            shutil.copytree(vendor_from, staging / "vendor")
        else:
            vendor_dependencies(staging / "vendor", python, wheelhouse)
        problem = app_payload.validate_payload_dir(staging, version)
        if problem:
            raise SystemExit(problem)
        temp_archive = archive.with_name(f".{archive.name}.tmp")
        with zipfile.ZipFile(temp_archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as bundle:
            for arcname, path in _payload_files(staging):
                info = zipfile.ZipInfo(arcname, date_time=ZIP_TIMESTAMP)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                bundle.writestr(info, path.read_bytes())
        os.replace(temp_archive, archive)
    return archive


def _asset_entry(path: Path) -> dict[str, object]:
    if not path.is_file():
        raise SystemExit(f"缺少发布文件：{path}")
    return {"name": path.name, "size": path.stat().st_size, "sha256": app_update.file_sha256(path)}


def _signing_seed() -> bytes | None:
    value = str(os.environ.get(SIGNING_KEY_ENV) or "").strip().lower()
    if not value:
        return None
    try:
        seed = bytes.fromhex(value)
    except ValueError as error:
        raise SystemExit(f"{SIGNING_KEY_ENV} 必须是 64 位十六进制") from error
    if len(seed) != 32:
        raise SystemExit(f"{SIGNING_KEY_ENV} 必须是 64 位十六进制")
    return seed


def write_manifest(
    *,
    version: str,
    tag: str,
    commit: str,
    assets_dir: Path,
    macos_revision: str,
    windows_revision: str,
    require_signature: bool = False,
) -> tuple[Path, Path | None]:
    payload = _asset_entry(assets_dir / app_update.payload_asset_name(version))
    shells = {
        app_update.SHELL_KIND_MACOS: {
            "revision": macos_revision,
            "asset": _asset_entry(assets_dir / MACOS_ASSET_TEMPLATE.format(version=version)),
        },
        app_update.SHELL_KIND_WINDOWS: {
            "revision": windows_revision,
            "asset": _asset_entry(assets_dir / WINDOWS_ASSET_TEMPLATE.format(version=version)),
        },
    }
    data = app_update.build_manifest(version=version, tag=tag, commit=commit, payload=payload, shells=shells)
    manifest_path = assets_dir / app_update.MANIFEST_ASSET_NAME
    manifest_path.write_bytes(data)
    seed = _signing_seed()
    if seed is None:
        if require_signature:
            raise SystemExit(f"未设置 {SIGNING_KEY_ENV}，无法签名更新清单")
        print(f"警告：未设置 {SIGNING_KEY_ENV}，更新清单未签名，客户端只会提示手动下载。", file=sys.stderr)
        return manifest_path, None
    signature = app_update.ed25519_sign(seed, data)
    # 私钥必须对应客户端内置的公钥，否则已发布的客户端会拒绝该清单。
    if not app_update.verify_manifest_signature(data, signature):
        raise SystemExit(
            f"{SIGNING_KEY_ENV} 对应的公钥 {app_update.ed25519_public_key(seed).hex()} "
            "不在 app_update.TRUSTED_SIGNING_KEYS 中"
        )
    signature_path = assets_dir / app_update.SIGNATURE_ASSET_NAME
    signature_path.write_text(signature.hex() + "\n", encoding="ascii")
    return manifest_path, signature_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)

    revision = commands.add_parser("shell-revision", help="输出外壳修订号")
    revision.add_argument("--shell", required=True, choices=sorted(SHELL_SOURCES))

    payload = commands.add_parser("payload", help="打包后端载荷")
    payload.add_argument("--version", required=True)
    payload.add_argument("--out", required=True, type=Path)
    payload.add_argument("--python", default=sys.executable, help="用于 pip 安装 vendor 依赖的 Python")
    payload.add_argument("--wheelhouse", default="", help="离线 wheelhouse 目录（可选）")
    payload.add_argument("--vendor-from", default="", help="直接复制已有 vendor/ 目录，跳过 pip（可选）")

    manifest = commands.add_parser("manifest", help="生成并签名更新清单")
    manifest.add_argument("--version", required=True)
    manifest.add_argument("--tag", required=True)
    manifest.add_argument("--commit", required=True)
    manifest.add_argument("--assets-dir", required=True, type=Path)
    manifest.add_argument("--macos-revision", required=True)
    manifest.add_argument("--windows-revision", required=True)
    manifest.add_argument("--require-signature", action="store_true", help="缺少签名私钥时失败")

    args = parser.parse_args(argv)
    if args.command == "shell-revision":
        print(shell_revision(args.shell))
    elif args.command == "payload":
        print(build_payload(args.version, args.out, python=args.python, wheelhouse=args.wheelhouse, vendor_from=args.vendor_from))
    else:
        manifest_path, signature_path = write_manifest(
            version=args.version,
            tag=args.tag,
            commit=args.commit,
            assets_dir=args.assets_dir,
            macos_revision=args.macos_revision,
            windows_revision=args.windows_revision,
            require_signature=args.require_signature,
        )
        print(manifest_path)
        if signature_path is not None:
            print(signature_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
