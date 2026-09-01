"""可插拔的命令执行后端（sandbox seam）：host / wsl / docker 三档 + 自动探测。

设计对齐 Claude Code / DSH 的「沙箱 best-effort、权限门才是硬边界」：
这里只决定「用什么机制跑命令」（以及是否隔离），不决定「能不能跑」——
能否运行由 ToolExecutor 层的 dangerous 标记 + permission-policy（ask/deny/auto）负责。
两者职责分离：沙箱处理隔离，权限门处理授权。

分层约束：本模块只依赖 agent_core（ToolResult），不引用 agent_core 之外的任何上层设施，
也不被 agent_core 反向依赖（agent_core 永远不知道沙箱的存在）。

CommandRunner 是接缝抽象：把「跑一条命令」与具体后端解耦。
BashTool 注入一个 runner；runner 自己决定怎么隔离（以及是否隔离）。
"""

import subprocess
from abc import ABC, abstractmethod
from pathlib import Path

from agent_core import ToolResult

DEFAULT_BASH_IMAGE = "python:3.12-slim"


class SandboxUnavailableError(Exception):
    """指定了沙箱后端但探测不到可用实现时的失败（fail-closed，不静默降级）。"""


def windows_to_wsl(path) -> str:
    """把 Windows 路径映射成 WSL 的 /mnt/<盘符>/<路径> 形式。

    例如 D:\\project space\\agent lite → /mnt/d/project space/agent lite。
    路径中的空格原样保留（由调用方在命令串里用引号包裹）。
    非盘符前缀（如 / 开头或 UNC）则原样返回，只反转分隔符。
    """
    resolved = str(Path(path).resolve())
    drive, sep, rest = resolved.partition(":")
    if not sep:
        return resolved.replace("\\", "/")
    return "/mnt/{}{}".format(drive.lower(), rest.replace("\\", "/"))


def _decode_bytes(data) -> str:
    """按 UTF-8 容错解码字节；None（历史 text=True 解码线程死亡的产物）也用 b"" 兜底。"""
    return (data or b"").decode("utf-8", errors="replace")


def _compose_result(proc) -> ToolResult:
    """把 subprocess.CompletedProcess 规范化为 ToolResult。

    沿用已验证的约定：按字节捕获 + 显式 UTF-8 解码（中文 Windows 的 GBK 地雷），
    errors="replace" 兜底坏字节；stdout/stderr 分开携带，非零退出码标记失败。
    任何字节都不应让工具崩溃。
    """
    out = _decode_bytes(proc.stdout)
    err = _decode_bytes(proc.stderr)
    output = (out + err).strip()

    if proc.returncode != 0:
        if output:
            content = f"(exit code {proc.returncode})\n{output}"
        else:
            content = f"(exit code {proc.returncode}, no output)"
        return ToolResult(
            content=content,
            is_error=True,
            exit_code=proc.returncode,
            stdout=out,
            stderr=err,
        )

    return ToolResult(
        content=output or "(exit code 0, no output)",
        exit_code=0,
        stdout=out,
        stderr=err,
    )


class CommandRunner(ABC):
    """命令执行接缝：抽象「跑一条命令」，与具体隔离后端解耦。

    子类只须实现 run(command)：在各自的隔离粒度下执行命令，返回规范化的 ToolResult。
    timeout 为单次执行的限时（与 bash 工具的时间一致，默认 60s）。
    """

    mode = "abstract"  # 后端标识，供 BashTool / CLI 展示与描述

    def __init__(self, workspace, timeout: int = 60):
        self.workspace = Path(workspace).resolve()
        self.timeout = timeout

    @abstractmethod
    def run(self, command: str) -> ToolResult:
        """执行一条命令并返回规范化结果。"""
        raise NotImplementedError

    def describe(self) -> str:
        """给用户看的人类可读隔离说明（启动时打印，不夸大边界）。"""
        return "抽象后端"


class HostRunner(CommandRunner):
    """零配置兜底：宿主直跑，cwd 落在工作目录。

    隔离是「程序级」而非「内核级」——由两层构成：
      1. read/write/edit 的 safe_path（把文件操作锁在工作目录内）；
      2. bash 的 dangerous=True + permission-policy（默认 ask）逐条人工确认。
    没有 OS 层文件系统隔离，bash 理论上能碰工作目录外的文件；如实说明，不夸大。
    """

    mode = "host"

    def run(self, command: str) -> ToolResult:
        proc = subprocess.run(
            command,
            cwd=self.workspace,
            capture_output=True,
            shell=True,
            timeout=self.timeout,
        )
        return _compose_result(proc)

    def describe(self) -> str:
        return (
            "宿主直跑（无内核文件系统隔离），真实边界依赖权限门 + safe_path；"
            "bash 命令请用 WSL/Docker 档以获取真正的隔离"
        )


class WslRunner(CommandRunner):
    """免 daemon 中间档：命令丢进 WSL2 的 Linux 发行版执行。

    wsl -e 会自动拉起 VM，无需用户先启动服务；工作目录映射为 /mnt/<盘符>/...。
    注意：wsl 包装进程可能把容器输出转成宿主编码（潜在的地雷），这里统一按 UTF-8 容错解码。
    """

    mode = "wsl"

    def run(self, command: str) -> ToolResult:
        mapped = windows_to_wsl(self.workspace)
        # 先 cd 到映射的工作目录再执行；路径含空格时用双引号包裹
        full = f'cd "{mapped}" && {command}'
        proc = subprocess.run(
            ["wsl", "-e", "sh", "-lc", full],
            capture_output=True,
            timeout=self.timeout,
        )
        return _compose_result(proc)

    def describe(self) -> str:
        return (
            f"WSL2（免 Docker daemon）：命令在 Linux 发行版内执行；"
            f"工作目录映射为 {windows_to_wsl(self.workspace)}"
        )


class DockerRunner(CommandRunner):
    """最强档：一次性 Docker 沙箱容器内执行（沿用原先 bash 工具的隔离参数）。

    无网络、只读根文件系统、资源限额（512MB 内存 / 100 进程）、
    仅通过 bind mount 把工作目录暴露为 /workspace。
    """

    mode = "docker"

    def __init__(self, workspace, image: str = DEFAULT_BASH_IMAGE, timeout: int = 60):
        super().__init__(workspace, timeout)
        self.image = image

    def run(self, command: str) -> ToolResult:
        args = [
            "docker", "run",
            "--rm",
            "--network", "none",
            "--read-only",
            "--tmpfs", "/tmp",
            "--pids-limit", "100",
            "--memory", "512m",
            "--mount",
            f"type=bind,source={self.workspace},target=/workspace",
            "--workdir", "/workspace",
            self.image,
            "sh", "-lc", command,
        ]
        proc = subprocess.run(
            args,
            capture_output=True,
            timeout=self.timeout,
        )
        return _compose_result(proc)

    def describe(self) -> str:
        return (
            f"Docker 沙箱（无网络/只读根/资源限额），镜像 {self.image}；"
            "工作目录通过 bind mount 暴露为 /workspace"
        )


def _docker_available(timeout: int = 5) -> bool:
    """Docker daemon 是否可用（`docker version` 的 Server 段需要 daemon 响应）。"""
    try:
        proc = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            timeout=timeout,
        )
        return proc.returncode == 0
    except Exception:
        return False


def _wsl_available(timeout: int = 10) -> bool:
    """WSL2 是否安装且有发行版（`wsl -l -q` 列出已安装的发行版）。"""
    try:
        proc = subprocess.run(
            ["wsl", "-l", "-q"],
            capture_output=True,
            timeout=timeout,
        )
        return proc.returncode == 0
    except Exception:
        return False


def detect_backend(
    sandbox: str = "auto",
    workspace=None,
    bash_image: str = DEFAULT_BASH_IMAGE,
) -> CommandRunner:
    """按运行环境选择后端，返回一个 runner 实例。

    模式：
      auto   自动探测，优先最强可用档：docker → wsl → host（host 无条件兜底）。
      host   强制宿主直跑（零配置兜底）。
      wsl    强制 WSL2；不可用则抛 SandboxUnavailableError（fail-closed，不静默降级）。
      docker 强制 Docker；不可用则抛 SandboxUnavailableError。

    探测行为：docker/wsl 各执行一次轻量探测命令；host 永远可用。
    """
    workspace = Path(workspace) if workspace is not None else Path.cwd()

    if sandbox == "host":
        return HostRunner(workspace)
    if sandbox == "wsl":
        if not _wsl_available():
            raise SandboxUnavailableError(
                "--sandbox=wsl 已指定，但检测不到可用的 WSL2 后端（请安装 WSL 并初始化一个发行版）"
            )
        return WslRunner(workspace)
    if sandbox == "docker":
        if not _docker_available():
            raise SandboxUnavailableError(
                "--sandbox=docker 已指定，但 Docker daemon 不可用（请先启动 Docker Desktop）"
            )
        return DockerRunner(workspace, image=bash_image)

    # auto：优先最强可用档，host 无条件兜底
    if _docker_available():
        return DockerRunner(workspace, image=bash_image)
    if _wsl_available():
        return WslRunner(workspace)
    return HostRunner(workspace)
