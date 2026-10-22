"""ProxyBridge 后端载荷（payload）选择与启动。

桌面发布包分为两层：
- 外壳：macOS 菜单栏 App 或 Windows 托盘 exe，负责启动、托盘菜单与窗口，很少变化；
- 后端载荷：app.py 等 Python 模块、static/ 前端与 vendor/ 依赖，几乎每次发布都会变化。

软件更新只下载新的后端载荷并解压到用户数据目录，外壳下次启动后端时由本模块选择
载荷，避免每次都整包替换 App / exe。本模块同时被外壳（启动器）与载荷（安装、确认）
使用，因此只依赖标准库并保持 Python 3.10 兼容；指针文件格式变更时必须向后兼容。

数据目录（PROXY_BRIDGE_WORKDIR）下的布局::

    app-updates/
      active.json          当前启用的载荷指针（版本、所需外壳修订号、确认状态）
      failed.json          启动失败或被用户回退的版本记录，供界面提示
      payloads/<版本>/      解压后的载荷：app.py、static/、vendor/、VERSION 等

macOS App 通过 ``python3 Contents/Resources/bridge/app_payload.py`` 启动后端；
Windows 托盘 exe 在 ``--backend-only`` 子进程中调用 :func:`prepare_frozen_backend`。
"""

from __future__ import annotations

import importlib.abc
import importlib.util
import json
import os
import re
import runpy
import sys
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, MutableMapping


UPDATES_DIRNAME = "app-updates"
PAYLOADS_DIRNAME = "payloads"
ACTIVE_FILENAME = "active.json"
FAILED_FILENAME = "failed.json"
# 构建时写入外壳（App 的 bridge/ 目录或 exe 包内）的外壳修订号文件，内容为 64 位十六进制 SHA-256。
SHELL_REVISION_FILENAME = "SHELL_REVISION"
POINTER_SCHEMA = 1
# 新载荷首次启动后必须由后端调用 confirm_payload 确认；未确认就再次启动即视为启动失败，
# 自动回退到外壳内置后端。1 表示只给新载荷一次启动机会。
MAX_PENDING_ATTEMPTS = 1
FAILED_HISTORY_LIMIT = 10
REQUIRED_PAYLOAD_FILES = ("app.py", "VERSION", "proxybridge_version.py", "static/index.html")

# 以下环境变量由外壳或本启动器写入，供后端判断运行形态：
# PROXY_BRIDGE_WORKDIR：用户数据目录，载荷与指针都保存在其中的 app-updates/。
ENV_WORKDIR = "PROXY_BRIDGE_WORKDIR"
# PROXY_BRIDGE_SHELL_ROOT：外壳内置后端所在目录（macOS 为 bridge/，Windows 为 exe 解包目录），
# 后端据此查找随外壳分发的 bin/、bundled/ 等资源。
ENV_SHELL_ROOT = "PROXY_BRIDGE_SHELL_ROOT"
# PROXY_BRIDGE_SHELL_REVISION：当前外壳的修订号；与 Release 清单中的修订号一致时可以只热更新载荷。
ENV_SHELL_REVISION = "PROXY_BRIDGE_SHELL_REVISION"
# PROXY_BRIDGE_PAYLOAD_VERSION：正在运行的载荷版本；为空表示运行外壳内置后端。
ENV_PAYLOAD_VERSION = "PROXY_BRIDGE_PAYLOAD_VERSION"
# PROXY_BRIDGE_PAYLOAD_DISABLED：设为 1 时忽略已安装的载荷，强制使用内置后端（排障或启动失败回退时使用）。
ENV_PAYLOAD_DISABLED = "PROXY_BRIDGE_PAYLOAD_DISABLED"

_SEMVER_PATTERN = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
_REVISION_PATTERN = re.compile(r"[0-9a-f]{64}")


def parse_version(value: Any) -> tuple[int, int, int, tuple[str, ...]] | None:
    match = _SEMVER_PATTERN.fullmatch(str(value or "").strip())
    if match is None:
        return None
    prerelease = match.group(4)
    return (
        int(match.group(1)),
        int(match.group(2)),
        int(match.group(3)),
        tuple(prerelease.split(".")) if prerelease else (),
    )


def _compare_identifiers(left: tuple[str, ...], right: tuple[str, ...]) -> int:
    for a, b in zip(left, right):
        if a == b:
            continue
        a_numeric, b_numeric = a.isdigit(), b.isdigit()
        if a_numeric and b_numeric:
            return -1 if int(a) < int(b) else 1
        if a_numeric != b_numeric:
            # SemVer：纯数字标识符优先级低于含字母的标识符。
            return -1 if a_numeric else 1
        return -1 if a < b else 1
    if len(left) == len(right):
        return 0
    return -1 if len(left) < len(right) else 1


def compare_versions(left: Any, right: Any) -> int:
    """按 SemVer 优先级比较版本，返回 -1 / 0 / 1；构建元数据（+ 之后）不参与比较。"""
    parsed_left = parse_version(left)
    parsed_right = parse_version(right)
    if parsed_left is None or parsed_right is None:
        raise ValueError(f"无法比较的版本号：{left!r} / {right!r}")
    if parsed_left[:3] != parsed_right[:3]:
        return -1 if parsed_left[:3] < parsed_right[:3] else 1
    left_pre, right_pre = parsed_left[3], parsed_right[3]
    if left_pre == right_pre:
        return 0
    # 没有预发布标识的正式版本优先级更高，例如 0.3.0 > 0.3.0-build.12。
    if not left_pre:
        return 1
    if not right_pre:
        return -1
    return _compare_identifiers(left_pre, right_pre)


@dataclass(frozen=True)
class PayloadSelection:
    root: Path
    version: str


def updates_root(work_root: Path | str) -> Path:
    return Path(work_root) / UPDATES_DIRNAME


def payloads_root(work_root: Path | str) -> Path:
    return updates_root(work_root) / PAYLOADS_DIRNAME


def payload_dir(work_root: Path | str, version: str) -> Path:
    if parse_version(version) is None:
        raise ValueError(f"载荷版本号无效：{version!r}")
    return payloads_root(work_root) / version


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return ""


def read_shell_revision(shell_root: Path | str) -> str:
    value = _read_text(Path(shell_root) / SHELL_REVISION_FILENAME).lower()
    return value if _REVISION_PATTERN.fullmatch(value) else ""


def read_bundled_version(shell_root: Path | str) -> str:
    value = _read_text(Path(shell_root) / "VERSION")
    return value if parse_version(value) is not None else ""


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None


def write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temp_path.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            temp_path.chmod(0o600)
        except OSError:
            pass
        os.replace(temp_path, path)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def load_active(work_root: Path | str) -> dict[str, Any] | None:
    data = read_json(updates_root(work_root) / ACTIVE_FILENAME)
    if not isinstance(data, dict) or data.get("schema") != POINTER_SCHEMA:
        return None
    return data


def save_active(work_root: Path | str, data: dict[str, Any]) -> None:
    write_json_atomic(updates_root(work_root) / ACTIVE_FILENAME, {**data, "schema": POINTER_SCHEMA})


def clear_active(work_root: Path | str) -> None:
    try:
        (updates_root(work_root) / ACTIVE_FILENAME).unlink()
    except FileNotFoundError:
        pass


def activate_payload(work_root: Path | str, version: str, shell_revision: str) -> dict[str, Any]:
    """把已校验并解压的载荷设为下次启动使用的版本；首次启动成功后由后端确认。"""
    if parse_version(version) is None:
        raise ValueError(f"载荷版本号无效：{version!r}")
    if _REVISION_PATTERN.fullmatch(str(shell_revision or "")) is None:
        raise ValueError("外壳修订号无效")
    pointer = {
        "version": version,
        "shell_revision": shell_revision,
        "state": "pending",
        "attempts": 0,
        "installed_at": int(time.time()),
    }
    save_active(work_root, pointer)
    return pointer


def confirm_payload(work_root: Path | str, version: str) -> bool:
    """后端在载荷中成功启动（HTTP 服务已监听）后调用，标记该载荷可继续使用。"""
    active = load_active(work_root)
    if not active or active.get("version") != version:
        return False
    if active.get("state") == "confirmed":
        return True
    active.update({"state": "confirmed", "attempts": 0, "confirmed_at": int(time.time())})
    save_active(work_root, active)
    return True


def payload_is_pending(work_root: Path | str, version: str) -> bool:
    active = load_active(work_root)
    return bool(active and active.get("version") == version and active.get("state") == "pending")


def load_failures(work_root: Path | str) -> list[dict[str, Any]]:
    data = read_json(updates_root(work_root) / FAILED_FILENAME)
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict) and parse_version(item.get("version"))]


def record_failure(work_root: Path | str, version: str, reason: str, *, kind: str = "startup") -> None:
    items = [item for item in load_failures(work_root) if item.get("version") != version]
    items.append({
        "version": version,
        "reason": str(reason or "")[:500],
        "kind": kind,
        "at": int(time.time()),
    })
    write_json_atomic(updates_root(work_root) / FAILED_FILENAME, {"items": items[-FAILED_HISTORY_LIMIT:]})


def mark_payload_failed(work_root: Path | str, version: str, reason: str) -> None:
    """记录载荷启动失败并清除指针，下次启动直接使用外壳内置后端。"""
    record_failure(work_root, version, reason)
    active = load_active(work_root)
    if active and active.get("version") == version:
        clear_active(work_root)


def validate_payload_dir(root: Path, version: str) -> str:
    """返回载荷目录的问题描述；为空表示目录完整且版本一致。"""
    for relative in REQUIRED_PAYLOAD_FILES:
        if not (root / relative).is_file():
            return f"载荷缺少 {relative}"
    actual = _read_text(root / "VERSION")
    if actual != version:
        return f"载荷 VERSION 不一致：期望 {version}，实际 {actual or '空'}"
    return ""


def select_payload(
    work_root: Path | str,
    shell_root: Path | str,
    *,
    environ: MutableMapping[str, str] | None = None,
) -> PayloadSelection | None:
    """选择本次启动要运行的载荷；返回 None 表示使用外壳内置后端。

    只有同时满足以下条件才会使用载荷：未被禁用、版本高于外壳内置版本、要求的外壳
    修订号与当前外壳一致、目录完整，以及（尚未确认时）启动次数未超过上限。
    """
    environment = os.environ if environ is None else environ
    if str(environment.get(ENV_PAYLOAD_DISABLED) or "").strip() == "1":
        return None
    active = load_active(work_root)
    if not active:
        return None
    version = str(active.get("version") or "")
    if parse_version(version) is None:
        clear_active(work_root)
        return None
    bundled_version = read_bundled_version(shell_root)
    if bundled_version and compare_versions(version, bundled_version) <= 0:
        # 外壳本身已经升级到相同或更新的版本，旧载荷不再需要。
        clear_active(work_root)
        return None
    shell_revision = read_shell_revision(shell_root)
    if not shell_revision or active.get("shell_revision") != shell_revision:
        return None
    root = payload_dir(work_root, version)
    problem = validate_payload_dir(root, version)
    if problem:
        mark_payload_failed(work_root, version, problem)
        return None
    state = active.get("state")
    if state == "pending":
        attempts = active.get("attempts") if isinstance(active.get("attempts"), int) else 0
        if attempts >= MAX_PENDING_ATTEMPTS:
            mark_payload_failed(work_root, version, "新版本上次启动未完成，已自动回退到内置版本")
            return None
        active["attempts"] = attempts + 1
        save_active(work_root, active)
    elif state != "confirmed":
        clear_active(work_root)
        return None
    return PayloadSelection(root=root, version=version)


def export_shell_environment(
    shell_root: Path | str,
    selection: PayloadSelection | None,
    *,
    environ: MutableMapping[str, str] | None = None,
) -> None:
    environment = os.environ if environ is None else environ
    environment[ENV_SHELL_ROOT] = str(Path(shell_root).resolve())
    revision = read_shell_revision(shell_root)
    if revision:
        environment[ENV_SHELL_REVISION] = revision
    else:
        environment.pop(ENV_SHELL_REVISION, None)
    if selection is None:
        environment.pop(ENV_PAYLOAD_VERSION, None)
    else:
        environment[ENV_PAYLOAD_VERSION] = selection.version


def _work_root_from_environment(shell_root: Path) -> Path:
    configured = str(os.environ.get(ENV_WORKDIR) or "").strip()
    return Path(configured).expanduser() if configured else shell_root / "work"


def _log(message: str) -> None:
    print(f"ProxyBridge payload: {message}", file=sys.stderr, flush=True)


def _is_within(path: str | None, root: Path) -> bool:
    if not path:
        return False
    try:
        Path(path).resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def launch(argv: list[str] | None = None) -> None:
    """macOS App 的后端入口：选择载荷并以 __main__ 身份运行其 app.py。"""
    arguments = list(sys.argv[1:] if argv is None else argv)
    launcher_path = Path(__file__).resolve()
    shell_root = launcher_path.parent
    work_root = _work_root_from_environment(shell_root)
    selection: PayloadSelection | None = None
    try:
        selection = select_payload(work_root, shell_root)
    except Exception as error:  # noqa: BLE001 - 选择失败不能阻止内置后端启动
        _log(f"载荷选择失败，使用内置后端：{error}")
    export_shell_environment(shell_root, selection)
    target_root = selection.root if selection is not None else shell_root
    if sys.path and Path(sys.path[0] or ".").resolve() == shell_root:
        sys.path[0] = str(target_root)
    else:
        sys.path.insert(0, str(target_root))
    sys.argv = [str(target_root / "app.py"), *arguments]
    if selection is not None:
        _log(f"使用已安装的后端载荷 {selection.version}")
    try:
        runpy.run_path(str(target_root / "app.py"), run_name="__main__")
    except (SystemExit, KeyboardInterrupt):
        raise
    except BaseException as error:
        if selection is None or not payload_is_pending(work_root, selection.version):
            raise
        traceback.print_exc()
        mark_payload_failed(work_root, selection.version, f"新版本启动失败：{type(error).__name__}: {error}")
        _log("新版本启动失败，改用内置后端重新启动")
        # 用全新进程映像启动内置后端，避免失败载荷遗留的线程与模块；PID 不变，外壳无感知。
        os.environ[ENV_PAYLOAD_DISABLED] = "1"
        os.execv(sys.executable, [sys.executable, str(launcher_path), *arguments])


class _PayloadModuleFinder(importlib.abc.MetaPathFinder):
    """让冻结（PyInstaller）进程优先从载荷目录导入后端顶层模块。"""

    def __init__(self, root: Path, names: frozenset[str]) -> None:
        self.root = root
        self.names = names

    def find_spec(self, fullname: str, path: Any = None, target: Any = None) -> Any:
        del target
        if path is not None or fullname not in self.names:
            return None
        return importlib.util.spec_from_file_location(fullname, self.root / f"{fullname}.py")


def payload_module_names(root: Path) -> frozenset[str]:
    return frozenset(
        child.stem
        for child in root.iterdir()
        if child.is_file() and child.suffix == ".py" and child.stem.isidentifier()
    )


def _purge_modules(names: frozenset[str], root: Path | None = None) -> None:
    for name in list(sys.modules):
        module = sys.modules.get(name)
        top_level = name.split(".", 1)[0]
        if top_level in names or (root is not None and _is_within(getattr(module, "__file__", None), root)):
            sys.modules.pop(name, None)


def prepare_frozen_backend(work_root: Path | str, shell_root: Path | str) -> PayloadSelection | None:
    """Windows 托盘 exe 的后端子进程在 ``import app`` 之前调用，按需切换到载荷模块。"""
    selection: PayloadSelection | None = None
    try:
        selection = select_payload(work_root, shell_root)
    except Exception as error:  # noqa: BLE001 - 选择失败不能阻止内置后端启动
        _log(f"载荷选择失败，使用内置后端：{error}")
    export_shell_environment(shell_root, selection)
    if selection is None:
        return None
    names = payload_module_names(selection.root)
    # 托盘入口已导入内置 proxybridge_version 等模块；先移除，确保后端使用载荷中的同名模块。
    _purge_modules(names)
    sys.meta_path.insert(0, _PayloadModuleFinder(selection.root, names))
    sys.path.insert(0, str(selection.root))
    _log(f"使用已安装的后端载荷 {selection.version}")
    return selection


def abandon_frozen_payload(work_root: Path | str, selection: PayloadSelection, reason: str) -> None:
    """载荷导入失败时撤销 prepare_frozen_backend 的改动，之后 ``import app`` 会回到内置模块。"""
    sys.meta_path[:] = [
        finder for finder in sys.meta_path
        if not (isinstance(finder, _PayloadModuleFinder) and finder.root == selection.root)
    ]
    sys.path[:] = [item for item in sys.path if not _is_within(item, selection.root)]
    _purge_modules(payload_module_names(selection.root), selection.root)
    os.environ.pop(ENV_PAYLOAD_VERSION, None)
    mark_payload_failed(work_root, selection.version, reason)
    _log(f"{reason}；改用内置后端")


def independent_process_environment(environ: MutableMapping[str, str] | None = None) -> dict[str, str]:
    """启动另一个独立的 PyInstaller 程序（新版 exe）时使用的环境。

    PyInstaller onefile 子进程会继承 _PYI_* / _MEIPASS2 变量，新进程可能误用旧进程即将删除的
    解包目录；PYINSTALLER_RESET_ENVIRONMENT=1 让新进程按全新实例启动。
    """
    env = dict(os.environ if environ is None else environ)
    for key in list(env):
        if key.startswith("_PYI_") or key == "_MEIPASS2":
            env.pop(key, None)
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return env


def payload_failed_since(work_root: Path | str, since: float) -> bool:
    """外壳启动后端失败后判断是否由新载荷引起：是则可以立即重试一次（会回退到内置后端）。"""
    active = load_active(work_root)
    if active and active.get("state") == "pending" and int(active.get("attempts") or 0) >= MAX_PENDING_ATTEMPTS:
        return True
    return any(
        item.get("kind") == "startup" and float(item.get("at") or 0) >= int(since)
        for item in load_failures(work_root)
    )


if __name__ == "__main__":
    launch()
