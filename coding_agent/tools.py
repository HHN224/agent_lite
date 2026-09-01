from pathlib import Path

from agent_core import AgentTool, ToolResult

from .sandbox import CommandRunner, DockerRunner, DEFAULT_BASH_IMAGE


def safe_path(workspace: Path, path: str) -> Path:
    """把路径解析到工作目录内，越界则抛 PermissionError。"""
    workspace = workspace.resolve()
    file = (workspace / path).resolve()

    if file != workspace and workspace not in file.parents:
        raise PermissionError(f"不能访问工作目录之外的文件: {path}")

    return file


class ReadTool(AgentTool):
    argument_types = {"path": str}

    def __init__(self, workspace: Path):
        self.workspace = workspace.resolve()

        super().__init__(
            name="read",
            description="Read a text file",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
            timeout=10,
            dangerous=False,
        )

    def execute(self, path: str) -> ToolResult:
        file = safe_path(self.workspace, path)

        if not file.exists():
            return ToolResult(content=f"Error: file not found: {path}", is_error=True)

        if not file.is_file():
            return ToolResult(content=f"Error: not a file: {path}", is_error=True)

        return ToolResult(content=file.read_text(encoding="utf-8"))


class WriteTool(AgentTool):
    argument_types = {"path": str, "content": str}

    def __init__(self, workspace: Path):
        self.workspace = workspace.resolve()

        super().__init__(
            name="write",
            description="Write content to a text file",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
            timeout=10,
            dangerous=True,
        )

    def describe_call(self, arguments: dict) -> str:
        content = arguments.get("content", "")
        summary = content[:80].replace("\n", "\\n")
        if len(content) > 80:
            summary += "..."
        return f"写入文件 {arguments.get('path')}（共 {len(content)} 字符，内容摘要: {summary!r}）"

    def execute(self, path: str, content: str) -> ToolResult:
        file = safe_path(self.workspace, path)
        file.write_text(content, encoding="utf-8")
        return ToolResult(content=f"Successfully wrote to {path}")


class BashTool(AgentTool):
    """执行 shell 命令，隔离机制由注入的 CommandRunner 决定（host / wsl / docker）。

    命令实际怎么跑（以及是否隔离）由 runner 负责；本工具只负责：
      · 定义 bash 的工具元数据（dangerous=True，受权限门约束）；
      · 把命令行转发给 runner.run(command)。
    运行镜像可配置（仅当 runner 是 DockerRunner 时通过 image 指定），默认 python:3.12-slim。
    返回结构化结果：exit_code / stdout / stderr 分开携带，非零退出码标记为失败。
    """

    argument_types = {"command": str}

    def __init__(self, workspace: Path, runner: CommandRunner | None = None):
        self.workspace = workspace.resolve()
        # 未显式注入时，默认用 Docker 档（保持向后兼容；普通路径仍是"宿主 docker"语义）。
        # 注意：这里不调用 detect_backend，避免在工具构造期执行探测（探测是启动期的事，由 __main__ 做）。
        self.runner = runner or DockerRunner(self.workspace)
        self.command_timeout = self.runner.timeout

        super().__init__(
            name="bash",
            description="Run a shell command (isolation depends on the configured sandbox backend)",
            parameters={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The shell command to execute",
                    },
                },
                "required": ["command"],
            },
            timeout=60,
            dangerous=True,
        )

    @property
    def image(self):
        """向后兼容：bash 工具的镜像（仅为 DockerRunner 存在，其他后端为 None）。"""
        return getattr(self.runner, "image", None)

    def describe_call(self, arguments: dict) -> str:
        return f"执行命令: {arguments.get('command')}（沙箱后端: {self.runner.mode}）"

    def execute(self, command: str) -> ToolResult:
        return self.runner.run(command)


class EditTool(AgentTool):
    argument_types = {"path": str, "old_string": str, "new_string": str}

    def __init__(self, workspace: Path):
        self.workspace = workspace.resolve()

        super().__init__(
            name="edit",
            description="Replace an exact substring in a text file with new content",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_string": {"type": "string"},
                    "new_string": {"type": "string"},
                },
                "required": ["path", "old_string", "new_string"],
            },
            timeout=10,
            dangerous=True,
        )

    def describe_call(self, arguments: dict) -> str:
        return (
            f"编辑文件 {arguments.get('path')}: "
            f"{arguments.get('old_string')!r} → {arguments.get('new_string')!r}"
        )

    def execute(self, path: str, old_string: str, new_string: str) -> ToolResult:
        file = safe_path(self.workspace, path)

        if not file.exists():
            return ToolResult(content=f"Error: file not found: {path}", is_error=True)

        if not file.is_file():
            return ToolResult(content=f"Error: not a file: {path}", is_error=True)

        content = file.read_text(encoding="utf-8")

        if content.count(old_string) == 0:
            return ToolResult(content=f"Error: old_string not found in {path}", is_error=True)

        if content.count(old_string) > 1:
            return ToolResult(content=f"Error: old_string matches {content.count(old_string)} times in {path}; it must be unique", is_error=True)

        file.write_text(content.replace(old_string, new_string), encoding="utf-8")
        return ToolResult(content=f"Successfully edited {path}")


def build_tools(
    workspace: Path,
    bash_image: str = DEFAULT_BASH_IMAGE,
    runner: CommandRunner | None = None,
) -> list[AgentTool]:
    """按工作目录组装全部工具（read / write / bash / edit）。

    bash 的隔离后端通过 runner 注入（host / wsl / docker）；
    未注入时默认用 DockerRunner（image 由 bash_image 决定），保持向后兼容。
    """
    return [
        ReadTool(workspace),
        WriteTool(workspace),
        BashTool(workspace, runner=runner or DockerRunner(workspace, image=bash_image)),
        EditTool(workspace),
    ]
