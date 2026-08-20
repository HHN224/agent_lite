from pathlib import Path
import subprocess

from agent_core import AgentTool, ToolResult


WORKSPACE = Path(__file__).resolve().parent.parent


def safe_path(path: str) -> Path:
    """把路径解析到工作目录内，越界则抛 PermissionError。"""
    file = (WORKSPACE / path).resolve()

    if file != WORKSPACE and WORKSPACE not in file.parents:
        raise PermissionError(f"不能访问工作目录之外的文件: {path}")

    return file


class ReadTool(AgentTool):
    def __init__(self):
        super().__init__(
            name="read",
            description="Read a text file",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        )

    def execute(self, path: str) -> ToolResult:
        file = safe_path(path)

        if not file.exists():
            return ToolResult(content=f"Error: file not found: {path}", is_error=True)

        if not file.is_file():
            return ToolResult(content=f"Error: not a file: {path}", is_error=True)

        return ToolResult(content=file.read_text(encoding="utf-8"))


class WriteTool(AgentTool):
    def __init__(self):
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
        )

    def execute(self, path: str, content: str) -> ToolResult:
        file = safe_path(path)
        file.write_text(content, encoding="utf-8")
        return ToolResult(content=f"Successfully wrote to {path}")


class BashTool(AgentTool):
    """在一次性 Docker 容器中执行命令：无网络、只读根文件系统、资源限额，
    仅通过 bind mount 把工作目录暴露给容器。"""

    IMAGE = "python:3.12-slim"
    TIMEOUT = 30

    def __init__(self, workspace: Path):
        self.workspace = workspace.resolve()

        super().__init__(
            name="bash",
            description="Run a shell command in a Docker sandbox (no network); the project is mounted at /workspace",
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
        )

    def execute(self, command: str) -> ToolResult:
        result = subprocess.run(
            [
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

                self.IMAGE,
                "sh", "-lc", command,
            ],
            capture_output=True,
            text=True,
            timeout=self.TIMEOUT,
        )
        output = result.stdout + result.stderr
        return ToolResult(content=output.strip() or f"(exit code {result.returncode}, no output)")


class EditTool(AgentTool):
    def __init__(self):
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
        )

    def execute(self, path: str, old_string: str, new_string: str) -> ToolResult:
        file = safe_path(path)

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


tools = [ReadTool(), WriteTool(), BashTool(WORKSPACE), EditTool()]
