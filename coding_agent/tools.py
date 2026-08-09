from pathlib import Path
import subprocess

from agent_core import AgentTool


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

    def execute(self, path: str) -> str:
        try:
            file = safe_path(path)

            if not file.exists():
                return f"Error: file not found: {path}"

            if not file.is_file():
                return f"Error: not a file: {path}"

            return file.read_text(encoding="utf-8")

        except PermissionError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error: {e}"


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

    def execute(self, path: str, content: str) -> str:
        try:
            file = safe_path(path)
            file.write_text(content, encoding="utf-8")
            return f"Successfully wrote to {path}"
        except PermissionError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error: {e}"


class BashTool(AgentTool):
    def __init__(self):
        super().__init__(
            name="bash",
            description="Run a shell command and return its stdout/stderr",
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

    def execute(self, command: str) -> str:
        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
            )
            output = result.stdout + result.stderr
            return output.strip() or f"(exit code {result.returncode}, no output)"
        except Exception as e:
            return f"Error: {e}"


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

    def execute(self, path: str, old_string: str, new_string: str) -> str:
        try:
            file = safe_path(path)

            if not file.exists():
                return f"Error: file not found: {path}"

            if not file.is_file():
                return f"Error: not a file: {path}"

            content = file.read_text(encoding="utf-8")

            if content.count(old_string) == 0:
                return f"Error: old_string not found in {path}"

            if content.count(old_string) > 1:
                return f"Error: old_string matches {content.count(old_string)} times in {path}; it must be unique"

            file.write_text(content.replace(old_string, new_string), encoding="utf-8")
            return f"Successfully edited {path}"
        except PermissionError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error: {e}"


tools = [ReadTool(), WriteTool(), BashTool(), EditTool()]

tool_map = {t.name: t.execute for t in tools}
