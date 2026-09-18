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

import os
import subprocess
from abc import ABC, abstractmethod
from pathlib import Path

from agent_core import ToolResult

DEFAULT_BASH_IMAGE = "python:3.12-slim"


# --------------------------------------------------------------------------- #
# 沙箱内的秘密掩蔽（内核级，不依赖用户的判断力）
# --------------------------------------------------------------------------- #
# 为什么必须有这一层：工作区里通常躺着密钥（.env）与全部对话存档（sessions/），
# 而沙箱把整个工作区 bind 进 /workspace。只要有任何一个出网路径能被注入的指令利用，
# 这些内容就是第一个被带走的东西。不靠"逐条确认"挡它 —— 用户会条件反射点 yes。
#
# 掩蔽方式（都在挂载层完成，进程内无法绕过）：
#   文件 -> --ro-bind /dev/null <path>
#   目录 -> --tmpfs <path>               看到空目录，写入也不落盘
# 实测（真实 WSL bubblewrap，见 Step 1 冒烟记录）：文件读到的是 **Permission denied**
# 而不是空内容 —— /mnt/c 这类 drvfs 是 nodev 挂载，绑进去的设备节点打不开。
# 「明确拒绝」比「静默读到空内容」更好：既不会泄密，也不会让模型以为配置是空的而继续跑。
SENSITIVE_FILE_PATTERNS = (
    ".env", ".env.*",
    "*.pem", "*.key", "*.p12", "*.pfx", "*.keystore",
    "id_rsa", "id_ed25519", "id_ecdsa",
    ".netrc", ".npmrc", ".pypirc", ".git-credentials", "credentials.json",
)
SENSITIVE_DIR_NAMES = (".ssh", ".aws", ".gnupg", ".docker", "secrets")


def _masked_entries(workspace: Path, extra_paths: list[Path] | None = None):
    """列出工作区内需要掩蔽的条目：[(相对路径, 'file' | 'dir')]。

    只返回**真实存在**的条目（bwrap 不会为一个不存在的挂载点报错，但没必要多写参数）。
    extra_paths 由调用方传入（例如 agent 自己的会话存档目录），只有落在工作区内才生效。
    """
    workspace = Path(workspace).resolve()
    found: dict[str, str] = {}

    for pattern in SENSITIVE_FILE_PATTERNS:
        for path in workspace.glob(pattern):
            if path.is_file():
                found[path.name] = "file"

    for name in SENSITIVE_DIR_NAMES:
        path = workspace / name
        if path.is_dir():
            found[name] = "dir"

    for extra in extra_paths or []:
        try:
            relative = Path(extra).resolve().relative_to(workspace)
        except ValueError:
            continue  # 不在工作区内的路径不需要掩蔽
        if (workspace / relative).is_dir():
            found[relative.as_posix()] = "dir"
        elif (workspace / relative).is_file():
            found[relative.as_posix()] = "file"

    return sorted(found.items())


def _run_command(*args, **kwargs):
    """Unattended tools must not read draft keys or share the TUI console."""
    return subprocess.run(*args, stdin=subprocess.DEVNULL,
                          creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                          **kwargs)


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
    """Decode WSL launcher diagnostics separately from UTF-8 shell output."""
    data = data or b""
    diagnostics = []
    # wsl.exe may prepend a UTF-16LE warning to the Linux process's UTF-8
    # stderr. Decoding the entire pipe with either codec corrupts one half.
    while data.startswith(b"w\x00s\x00l\x00:\x00"):
        end = data.find(b"\n\x00")
        length = len(data) if end < 0 else end + 2
        diagnostics.append(data[:length].decode("utf-16-le", errors="replace"))
        data = data[length:]
    return "".join(diagnostics) + data.decode("utf-8", errors="replace")


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
    # 容器内的工作目录别名（host 档为 None）。
    # 关键：这个别名只对 bash 生效；read / write / edit 认的是宿主真实工作目录，
    # 把别名喂给它们只会被 safe_path 拒绝，所以要说清楚而不是让模型自己猜。
    shell_workspace_alias: str | None = None
    # 掩蔽是否真的生效（host 档只是"文档承诺"，见 describe）
    masks_secrets = False

    def __init__(self, workspace, timeout: int = 60, mask: list | None = None):
        self.workspace = Path(workspace).resolve()
        self.timeout = timeout
        self.mask_paths = list(mask or [])

    def masked_entries(self):
        """本 runner 会在沙箱里掩蔽的条目：[(相对路径, 'file' | 'dir')]。"""
        return _masked_entries(self.workspace, self.mask_paths)

    @abstractmethod
    def run(self, command: str, timeout: int | None = None) -> ToolResult:
        """执行一条命令并返回规范化结果。

        timeout 覆盖默认限时；None 用 self.timeout。由 BashTool 传入（可选 command 超时）。
        """
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
    masks_secrets = False  # 宿主直跑：无法在内核层掩蔽密钥，只能如实告知

    def run(self, command: str, timeout: int | None = None) -> ToolResult:
        t = self.timeout if timeout is None else timeout
        proc = _run_command(
            command,
            cwd=self.workspace,
            capture_output=True,
            shell=True,
            timeout=t,
        )
        return _compose_result(proc)

    def describe(self) -> str:
        return (
            f"宿主直跑（无内核文件系统隔离），cwd = 工作目录 {self.workspace}；"
            "真实边界依赖权限门 + safe_path；"
            "密钥掩蔽在本档**不生效**（.env / sessions/ 对命令仍然可见），"
            "bash 命令请用 WSL/Docker 档以获取真正的隔离"
        )


class WslRunner(CommandRunner):
    """免 daemon 中间档：通过 WSL2 + Bubblewrap 隔离命令。

    WSL 负责提供 Linux 内核，Bubblewrap 只暴露可写的 /workspace，隐藏 Windows
    挂载目录和 Linux 用户目录，并为每条命令创建无网络的临时运行环境。
    注意：wsl 包装进程可能把容器输出转成宿主编码（潜在的地雷），这里统一按 UTF-8 容错解码。
    """

    mode = "wsl"
    shell_workspace_alias = "/workspace"
    masks_secrets = True

    def run(self, command: str, timeout: int | None = None) -> ToolResult:
        t = self.timeout if timeout is None else timeout
        mapped = windows_to_wsl(self.workspace)
        args = [
            "wsl", "-e", "bwrap",
            "--unshare-all",
            "--new-session",
            "--die-with-parent",
            "--ro-bind", "/", "/",
            "--bind", mapped, "/workspace",
            "--tmpfs", "/mnt",
            "--tmpfs", "/home",
            "--tmpfs", "/root",
            "--tmpfs", "/run",
            "--tmpfs", "/tmp",
            "--proc", "/proc",
            "--dev", "/dev",
            "--clearenv",
            "--setenv", "HOME", "/tmp",
            "--setenv", "PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            # 依赖目录（受控取件的落地点）暴露给命令；目录不存在也无害
            "--setenv", "PYTHONPATH", "/workspace/.agent-lite/deps/python",
            "--chdir", "/workspace",
        ]
        args.extend(self._mask_args())
        args.extend(["sh", "-lc", command])
        proc = _run_command(
            args,
            capture_output=True,
            timeout=t,
        )
        return _compose_result(proc)

    def _mask_args(self) -> list[str]:
        """把密钥/存档掩蔽掉：文件用 /dev/null 覆盖（读会被拒绝），目录用空 tmpfs。"""
        args: list[str] = []
        for relative, kind in self.masked_entries():
            target = f"/workspace/{relative}"
            if kind == "dir":
                args.extend(["--tmpfs", target])
            else:
                args.extend(["--ro-bind", "/dev/null", target])
        return args

    def describe(self) -> str:
        masked = [name for name, _ in self.masked_entries()]
        mask_note = (
            f"沙箱内已掩蔽敏感内容（{', '.join(masked)}）：读这些路径会是 "
            "Permission denied 或空目录，这是**故意**的，不要尝试绕过或改写它们；"
            if masked else ""
        )
        return (
            "WSL2 + Bubblewrap（免 Docker daemon）：**无网络出网**、系统只读，"
            f"仅工作目录（宿主真实路径 {self.workspace}）映射为可写的 /workspace"
            "（也是命令的当前目录）。"
            + mask_note +
            "宿主 /mnt、/home、/root 不可见；/tmp 等临时目录在每次命令结束后丢弃。"
            "要装库请用 install 工具（域名白名单 + 审计，落 .agent-lite/deps 并自动进 PYTHONPATH），"
            "要取网页/文件请用 fetch 工具；沙箱本身无法联网，不要反复尝试 curl/pip install。"
        )


class DockerRunner(CommandRunner):
    """最强档：一次性 Docker 沙箱容器内执行（沿用原先 bash 工具的隔离参数）。

    无网络、只读根文件系统、资源限额（512MB 内存 / 100 进程）、
    仅通过 bind mount 把工作目录暴露为 /workspace。
    """

    mode = "docker"
    shell_workspace_alias = "/workspace"
    masks_secrets = True

    def __init__(self, workspace, image: str = DEFAULT_BASH_IMAGE, timeout: int = 60, mask=None):
        super().__init__(workspace, timeout, mask)
        self.image = image

    def run(self, command: str, timeout: int | None = None) -> ToolResult:
        t = self.timeout if timeout is None else timeout
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
            "--env", "PYTHONPATH=/workspace/.agent-lite/deps/python",
        ]
        for relative, kind in self.masked_entries():
            if kind == "dir":
                args.extend(["--tmpfs", f"/workspace/{relative}"])
            else:
                args.extend(["--mount",
                             f"type=bind,source=/dev/null,target=/workspace/{relative},readonly"])
        args.extend([self.image, "sh", "-lc", command])
        proc = _run_command(
            args,
            capture_output=True,
            timeout=t,
        )
        return _compose_result(proc)

    def describe(self) -> str:
        masked = [name for name, _ in self.masked_entries()]
        mask_note = f"已掩蔽敏感内容（{', '.join(masked)}）；" if masked else ""
        return (
            f"Docker 沙箱（无网络/只读根/资源限额），镜像 {self.image}；"
            f"工作目录（宿主真实路径 {self.workspace}）通过 bind mount 暴露为 /workspace；"
            + mask_note +
            "要装库请用 install 工具（域名白名单 + 审计），要取网页/文件请用 fetch 工具"
        )


def _docker_available(timeout: int = 5) -> bool:
    """Docker daemon 是否可用（`docker version` 的 Server 段需要 daemon 响应）。"""
    try:
        proc = _run_command(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            timeout=timeout,
        )
        return proc.returncode == 0
    except Exception:
        return False


def _wsl_available(timeout: int = 10) -> bool:
    """默认 WSL 发行版是否可用且已经安装 Bubblewrap。"""
    try:
        proc = _run_command(
            ["wsl", "-e", "sh", "-lc", "command -v bwrap >/dev/null 2>&1"],
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
    mask: list | None = None,
) -> CommandRunner:
    """按运行环境选择后端，返回一个 runner 实例。

    模式：
      auto   自动探测，优先最强可用档：docker → wsl → host（host 无条件兜底）。
      host   强制宿主直跑（零配置兜底）。
      wsl    强制 WSL2 + Bubblewrap；不可用则抛 SandboxUnavailableError（fail-closed，不静默降级）。
      docker 强制 Docker；不可用则抛 SandboxUnavailableError。

    mask 为额外需要掩蔽的路径（如 agent 自己的会话存档目录），
    只有落在工作区内才生效；内置的密钥文件模式始终掩蔽。

    探测行为：docker/wsl 各执行一次轻量探测命令；host 永远可用。
    """
    workspace = Path(workspace) if workspace is not None else Path.cwd()

    if sandbox == "host":
        return HostRunner(workspace, mask=mask)
    if sandbox == "wsl":
        if not _wsl_available():
            raise SandboxUnavailableError(
                "--sandbox=wsl 已指定，但默认 WSL 发行版不可用或未安装 bubblewrap"
            )
        return WslRunner(workspace, mask=mask)
    if sandbox == "docker":
        if not _docker_available():
            raise SandboxUnavailableError(
                "--sandbox=docker 已指定，但 Docker daemon 不可用（请先启动 Docker Desktop）"
            )
        return DockerRunner(workspace, image=bash_image, mask=mask)

    # auto：优先最强可用档，host 无条件兜底
    if _docker_available():
        return DockerRunner(workspace, image=bash_image, mask=mask)
    if _wsl_available():
        return WslRunner(workspace, mask=mask)
    return HostRunner(workspace, mask=mask)
