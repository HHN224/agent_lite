"""受控取件（controlled fetch）：把「联网」收敛成两个**不执行代码**的窄通道。

为什么不是「给沙箱开网络」——两条实测事实：
  1. 沙箱里根本没有 pip/npm（`python3 -m pip` -> No module named pip），而且
     /tmp、/home、/root 是每条命令丢弃的 tmpfs：就算开了网、装了包，下一条命令也没了。
     所以「用不了某个库」的病根是**取件 + 落地**两个，不是网络一个。
  2. 逐条人工确认挡不住供应链与提示注入（用户会条件反射点 yes）。安全必须落在机制上。

因此本模块把联网收敛为：
  · 域名白名单（内置注册表，可用 --allow-host 扩展）
  · 拒绝私网/回环/链路本地目标（解析后校验 IP，阻断 SSRF 与内网横向）
  · 只做「下载」与「解包」，**不执行任何被下载代码**（pip download 不跑 setup.py，
    且强制 --only-binary=:all:，拿到的都是 wheel = zip，解包即可用）
  · 拒绝遮蔽标准库的顶层模块（PYTHONPATH 先于标准库，恶意 wheel 可用 json.py 顶替）
  · 全程写 .agent-lite/network.log（事后可审计，不需要人盯着）
产物落在工作区内的 .agent-lite/ 下，沙箱保持零出网能力。
"""

from __future__ import annotations

import ipaddress
import json
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

# --------------------------------------------------------------------------- #
# 白名单
# --------------------------------------------------------------------------- #
# 默认只放行「取依赖」必需的注册表域名；要取别的站点用 --allow-host 显式加。
DEFAULT_ALLOWED_HOSTS = (
    "pypi.org",
    "files.pythonhosted.org",
    "registry.npmjs.org",
    "github.com",
    "codeload.github.com",
    "raw.githubusercontent.com",
    "objects.githubusercontent.com",
)

MAX_FETCH_BYTES = 64 * 1024 * 1024          # 单次 URL 取件上限
MAX_WHEELHOUSE_BYTES = 512 * 1024 * 1024    # 依赖目录总量上限
FETCH_TIMEOUT_SECONDS = 60

# Linux x86_64 的 wheel 标签：Windows 上装了也用不了，必须跨平台下载
TARGET_PLATFORMS = ("manylinux2014_x86_64", "manylinux_2_17_x86_64", "manylinux1_x86_64")
TARGET_PYTHON_VERSION = "312"
TARGET_ABI = "cp312"

# 包名/版本约束：只允许 pip 能理解的字符，杜绝把参数当 shell 或 pip 选项注入
# （不允许空格、分号、$、引号；也不允许以 - 开头 —— 那会变成 pip 的选项）
_SPECIFIER = r"(==|>=|<=|~=|!=|>|<)\s*[A-Za-z0-9._*+!-]{1,32}"
_PACKAGE_SPEC_RE = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]{0,63})"
    r"(?P<extras>\[[A-Za-z0-9._,-]{1,128}\])?"
    r"(?P<version>\s*" + _SPECIFIER + r"(\s*,\s*" + _SPECIFIER + r"){0,3})?$"
)
_MODULE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class FetchError(Exception):
    """取件被拒绝或失败：message 会原样回给模型，必须是人话。"""


# --------------------------------------------------------------------------- #
# 目录布局（全部在工作区内，沙箱可见且已 gitignore）
# --------------------------------------------------------------------------- #
def deps_root(workspace: Path) -> Path:
    return Path(workspace) / ".agent-lite" / "deps"


def wheelhouse_dir(workspace: Path) -> Path:
    return deps_root(workspace) / "wheelhouse"


def deps_python_dir(workspace: Path) -> Path:
    """解包后的依赖目录：沙箱里的 PYTHONPATH 指向它。"""
    return deps_root(workspace) / "python"


def downloads_dir(workspace: Path) -> Path:
    return Path(workspace) / ".agent-lite" / "downloads"


def audit_log_path(workspace: Path) -> Path:
    return Path(workspace) / ".agent-lite" / "network.log"


def append_audit(workspace: Path, entry: dict) -> None:
    """把一次取件追加进审计日志（append-only，出错也不影响主流程）。"""
    path = audit_log_path(workspace)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {"ts": time.time(), **entry}
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# 校验
# --------------------------------------------------------------------------- #
def allowed_hosts(extra: list[str] | None = None) -> tuple[str, ...]:
    hosts = list(DEFAULT_ALLOWED_HOSTS)
    for host in extra or []:
        host = (host or "").strip().lower().lstrip(".")
        if host and host not in hosts:
            hosts.append(host)
    return tuple(hosts)


def _host_allowed(host: str, allowed: tuple[str, ...]) -> bool:
    host = (host or "").lower()
    return any(host == domain or host.endswith("." + domain) for domain in allowed)


def _reject_private_addresses(host: str) -> None:
    """解析域名并拒绝私网/回环/链路本地等目标（阻断 SSRF 与内网横向）。

    诚实边界：解析与连接之间存在 TOCTOU 窗口，这里挡的是「明显指向内网」的请求，
    不是完整的 DNS rebinding 防御。
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as exc:
        raise FetchError(f"无法解析域名 {host!r}: {exc}") from exc
    for info in infos:
        address = info[4][0]
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            continue
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified):
            raise FetchError(
                f"拒绝访问内网/保留地址（{host} -> {address}）；取件只允许公网白名单域名"
            )


def validate_url(url: str, allowed: tuple[str, ...] | None = None) -> str:
    """校验一个待取 URL；返回规范化的 URL，不合法则抛 FetchError。"""
    allowed = allowed or allowed_hosts()
    parsed = urllib.parse.urlsplit((url or "").strip())
    if parsed.scheme not in ("http", "https"):
        raise FetchError("只支持 http/https URL")
    if parsed.username or parsed.password:
        raise FetchError("URL 不允许携带用户名/密码")
    host = parsed.hostname or ""
    if not host:
        raise FetchError("URL 缺少主机名")
    if not _host_allowed(host, allowed):
        raise FetchError(
            f"域名 {host!r} 不在取件白名单内；允许的域名: {', '.join(allowed)}"
        )
    _reject_private_addresses(host)
    return urllib.parse.urlunsplit(parsed)


def package_name(spec: str) -> str:
    """取出包名（用于遮蔽检查与审计）。"""
    match = _PACKAGE_SPEC_RE.match((spec or "").strip())
    if not match:
        raise FetchError(
            f"包名/版本约束不合法: {spec!r}（只允许字母数字与 . _ -，例如 requests==2.31.0）"
        )
    return match.group("name")


def validate_package_specs(specs: list[str]) -> list[str]:
    if not specs:
        raise FetchError("至少要给出一个包名")
    if len(specs) > 20:
        raise FetchError("一次最多安装 20 个包")
    validated = []
    for spec in specs:
        package_name(spec)  # 只做校验；不合法会抛 FetchError
        validated.append(spec.strip())
    return validated


# --------------------------------------------------------------------------- #
# pip 下载（下载 ≠ 执行：不跑 setup.py，且只接受 wheel）
# --------------------------------------------------------------------------- #
def download_pip_wheels(
    workspace: Path,
    specs: list[str],
    run_command=None,
) -> dict:
    """把 packages 的 Linux wheel 下到 wheelhouse（跨平台下载，不执行任何包代码）。

    返回 {"wheelhouse": Path, "wheels": int, "bytes": int, "output": str}。
    """
    specs = validate_package_specs(specs)
    dest = wheelhouse_dir(workspace)
    dest.mkdir(parents=True, exist_ok=True)
    before = {p.name for p in dest.glob("*.whl")}

    command = [
        sys.executable, "-m", "pip", "download",
        "--only-binary=:all:",           # 绝不下载 sdist：sdist 的 setup.py 会被执行
        "--progress-bar", "off",
        "--dest", str(dest),
        "--python-version", TARGET_PYTHON_VERSION,
        "--implementation", "cp",
        "--abi", TARGET_ABI,
    ]
    for platform_tag in TARGET_PLATFORMS:
        command.extend(["--platform", platform_tag])
    command.extend(specs)

    run = run_command or _default_run
    completed = run(command, workspace)
    new_wheels = sorted({p.name for p in dest.glob("*.whl")} - before)
    total_bytes = sum((dest / name).stat().st_size for name in new_wheels)

    if completed.returncode != 0:
        append_audit(workspace, {
            "kind": "pip-download", "ok": False, "packages": specs,
            "detail": (completed.stderr or completed.stdout or "")[-400:],
        })
        detail = (completed.stderr or completed.stdout or "").strip()
        raise FetchError(
            "下载失败（已尝试仅 wheel 的跨平台下载）。"
            + ("常见原因：该包没有 Linux wheel（只有源码包），离线沙箱里无法编译。"
               if "No matching distribution" in detail or "only-binary" in detail else "")
            + f"\n{detail[-600:]}"
        )

    if total_bytes > MAX_WHEELHOUSE_BYTES:
        append_audit(workspace, {
            "kind": "pip-download", "ok": False, "packages": specs,
            "detail": f"size limit exceeded: {total_bytes}",
        })
        raise FetchError(
            f"下载体积 {total_bytes} 字节超过上限 {MAX_WHEELHOUSE_BYTES}，已拒绝"
        )

    append_audit(workspace, {
        "kind": "pip-download", "ok": True, "packages": specs,
        "hosts": list(allowed_hosts()), "wheels": len(new_wheels), "bytes": total_bytes,
    })
    return {
        "wheelhouse": dest,
        "wheels": len(new_wheels),
        "bytes": total_bytes,
        "output": (completed.stdout or "").strip()[-400:],
    }


def _default_run(command: list[str], workspace: Path):
    """执行下载命令：argv 直传、不用 shell、关闭 stdin（与沙箱命令同一套纪律）。"""
    creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    return subprocess.run(
        command,
        cwd=str(workspace),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdin=subprocess.DEVNULL,
        timeout=600,
        creationflags=creationflags,
    )


# --------------------------------------------------------------------------- #
# 解包（wheel 就是 zip；不执行代码，但要挡住归档攻击）
# --------------------------------------------------------------------------- #
def _safe_members(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    """过滤出可安全解包的成员：挡 zip-slip、绝对路径与软链接。"""
    members = []
    for info in archive.infolist():
        name = info.filename.replace("\\", "/")
        if name.startswith("/") or ".." in Path(name).parts:
            raise FetchError(f"wheel 内含越界路径 {info.filename!r}，已拒绝解包")
        if (info.external_attr >> 16) & 0o170000 == 0o120000:  # symlink
            raise FetchError(f"wheel 内含软链接 {info.filename!r}，已拒绝解包")
        if ".data/" in name:  # 脚本/数据段在 PYTHONPATH 场景用不上，跳过更安全
            continue
        members.append(info)
    return members


def _top_level_names(members: list[zipfile.ZipInfo]) -> set[str]:
    names = set()
    for info in members:
        parts = [p for p in info.filename.split("/") if p]
        if not parts:
            continue
        head = parts[0]
        if head.endswith(".dist-info") or head.endswith(".data"):
            continue
        if len(parts) == 1 and head.endswith(".py"):
            head = head[:-3]
        names.add(head)
    return names


def unpack_wheels(workspace: Path, wheelhouse: Path | None = None) -> dict:
    """把 wheelhouse 里的 wheel 解包到 deps/python（沙箱 PYTHONPATH 指向它）。

    解包前先做「标准库遮蔽」检查：PYTHONPATH 排在标准库之前，一个叫 json.py 的
    顶层模块就能顶替标准库 —— 这是安装不可信依赖最容易被忽略的一类风险。
    """
    wheelhouse = wheelhouse or wheelhouse_dir(workspace)
    target = deps_python_dir(workspace)
    target.mkdir(parents=True, exist_ok=True)
    stdlib = set(getattr(sys, "stdlib_module_names", ()))

    unpacked, shadowing, modules = 0, [], set()
    for wheel in sorted(wheelhouse.glob("*.whl")):
        try:
            with zipfile.ZipFile(wheel) as archive:
                members = _safe_members(archive)
                names = _top_level_names(members)
                collision = sorted(n for n in names if n in stdlib)
                if collision:
                    shadowing.append(f"{wheel.name} -> {', '.join(collision)}")
                    continue
                for info in members:
                    archive.extract(info, target)
                modules |= names
                unpacked += 1
        except zipfile.BadZipFile as exc:
            raise FetchError(f"{wheel.name} 不是合法 wheel: {exc}") from exc

    if shadowing:
        raise FetchError(
            "拒绝解包会遮蔽 Python 标准库的 wheel："
            + "; ".join(shadowing)
            + "（PYTHONPATH 先于标准库，装上会顶替标准模块）"
        )
    return {"target": target, "unpacked": unpacked, "modules": sorted(modules)}


# --------------------------------------------------------------------------- #
# 通用 URL 取件（读文档、拉测试夹具）
# --------------------------------------------------------------------------- #
class _AllowlistedRedirects(urllib.request.HTTPRedirectHandler):
    """重定向的每一跳都要重新过白名单与内网检查，否则白名单形同虚设。"""

    def __init__(self, allowed: tuple[str, ...]):
        super().__init__()
        self.allowed = allowed

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_url(newurl, self.allowed)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_url(
    workspace: Path,
    url: str,
    filename: str | None = None,
    allowed: tuple[str, ...] | None = None,
) -> dict:
    """下载一个白名单 URL 到 .agent-lite/downloads/；返回落盘信息。"""
    allowed = allowed or allowed_hosts()
    safe_url = validate_url(url, allowed)
    parsed = urllib.parse.urlsplit(safe_url)

    name = (filename or Path(parsed.path).name or "download").replace("\\", "/").split("/")[-1]
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name)[:120] or "download"
    target = downloads_dir(workspace) / name
    target.parent.mkdir(parents=True, exist_ok=True)

    opener = urllib.request.build_opener(_AllowlistedRedirects(allowed))
    request = urllib.request.Request(safe_url, headers={"User-Agent": "agent-lite/0.1"})
    try:
        with opener.open(request, timeout=FETCH_TIMEOUT_SECONDS) as response:
            data = response.read(MAX_FETCH_BYTES + 1)
    except urllib.error.HTTPError as exc:
        append_audit(workspace, {"kind": "url-fetch", "ok": False, "url": safe_url,
                                 "detail": f"HTTP {exc.code}"})
        raise FetchError(f"取件失败：HTTP {exc.code} {exc.reason}") from exc
    except (urllib.error.URLError, OSError) as exc:
        append_audit(workspace, {"kind": "url-fetch", "ok": False, "url": safe_url,
                                 "detail": str(exc)[:200]})
        raise FetchError(f"取件失败：{exc}") from exc

    if len(data) > MAX_FETCH_BYTES:
        raise FetchError(f"响应超过 {MAX_FETCH_BYTES} 字节上限，已中止")
    target.write_bytes(data)
    append_audit(workspace, {"kind": "url-fetch", "ok": True, "url": safe_url,
                             "host": parsed.hostname, "bytes": len(data),
                             "path": str(target)})
    return {"path": target, "bytes": len(data), "url": safe_url}


def clear_deps(workspace: Path) -> None:
    """/deps clear：删掉取件产物，回到干净状态（可逆性）。"""
    for directory in (wheelhouse_dir(workspace), deps_python_dir(workspace)):
        shutil.rmtree(directory, ignore_errors=True)


def deps_summary(workspace: Path, tail: int = 8) -> str:
    """/deps：把「取过什么」摊开给用户看（透明性靠可查，而不是靠逐条确认）。"""
    workspace = Path(workspace)
    python_dir = deps_python_dir(workspace)
    modules = sorted(
        p.name for p in python_dir.glob("*")
        if p.is_dir() or p.suffix == ".py"
    ) if python_dir.is_dir() else []
    wheels = sorted(p.name for p in wheelhouse_dir(workspace).glob("*.whl"))
    total = sum(p.stat().st_size for p in python_dir.rglob("*") if p.is_file()) \
        if python_dir.is_dir() else 0
    downloads = sorted(p.name for p in downloads_dir(workspace).glob("*")) \
        if downloads_dir(workspace).is_dir() else []

    lines = [
        f"依赖目录: {python_dir}",
        f"  已安装模块 {len(modules)} 个，占用 {total // 1024} KB"
        + (f": {', '.join(modules[:12])}" + (" …" if len(modules) > 12 else "") if modules else ""),
        f"  本地 wheel 缓存: {len(wheels)} 个",
        f"已取文件: {len(downloads)} 个"
        + (f" ({', '.join(downloads[:6])})" if downloads else ""),
        f"白名单域名: {', '.join(allowed_hosts())}",
    ]

    log = audit_log_path(workspace)
    if log.is_file():
        recent = log.read_text(encoding="utf-8").strip().splitlines()[-tail:]
        lines.append(f"审计日志(最近 {len(recent)} 条): {log}")
        for line in recent:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            target = record.get("packages") or record.get("url") or record.get("detail") or ""
            mark = "OK " if record.get("ok") else "拒绝"
            lines.append(f"  [{mark}] {record.get('kind', '?')}: {target}")
    else:
        lines.append("审计日志: 还没有任何取件记录")
    return "\n".join(lines)
