import fnmatch
import re
from datetime import datetime
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


# 图片文件扩展名（读取时返回 base64 图片内容，供多模态模型查看）
_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg"}
# 文本读取的默认行数上限（0 表示不限制）
DEFAULT_READ_LIMIT = 2000


class ReadTool(AgentTool):
    """读取文件：支持 offset / limit / 行号，图片文件以 base64 返回。"""

    argument_types = {"path": str}

    def __init__(self, workspace: Path):
        self.workspace = workspace.resolve()

        super().__init__(
            name="read",
            description=(
                "Read a file. Text files are returned with line numbers (cat -n style). "
                "Use offset to skip lines and limit to cap how many lines are returned. "
                "Image files are returned as base64 data for the model to view."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "The file path to read"},
                    "offset": {"type": "integer", "description": "Line number to start reading from (1-indexed)"},
                    "limit": {"type": "integer", "description": "Maximum number of lines to return (default 2000)"},
                },
                "required": ["path"],
            },
            timeout=10,
            dangerous=False,
        )

    def execute(self, path: str, offset: int | None = None, limit: int | None = None) -> ToolResult:
        file = safe_path(self.workspace, path)

        if not file.exists():
            return ToolResult(content=f"Error: file not found: {path}", is_error=True)

        if not file.is_file():
            return ToolResult(content=f"Error: not a file: {path}", is_error=True)

        # 图片文件：返回 base64 图片内容（多模态模型可查看）
        if file.suffix.lower() in _IMAGE_EXTENSIONS:
            try:
                import base64
                import mimetypes
                data = base64.b64encode(file.read_bytes()).decode("ascii")
                mime = mimetypes.guess_type(str(file))[0] or "image/png"
                return ToolResult(content=f"data:{mime};base64,{data}")
            except Exception as e:
                return ToolResult(content=f"Error reading image: {e}", is_error=True)

        # 文本文件
        try:
            text = file.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return ToolResult(content=f"Error reading file: {e}", is_error=True)

        lines = text.split("\n")
        start_line = offset or 1
        if offset is not None and offset > 1:
            lines = lines[offset - 1:]
        if limit is not None:
            lines = lines[:limit]

        # 加行号（cat -n 风格），便于模型定位
        numbered = []
        for i, line in enumerate(lines):
            line_num = start_line + i
            numbered.append(f"{line_num:>6}\t{line}")
        return ToolResult(content="\n".join(numbered))


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
                    "timeout": {
                        "type": "integer",
                        "description": "Optional timeout in seconds to override the default",
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

    def execute(self, command: str, timeout: int | None = None) -> ToolResult:
        return self.runner.run(command, timeout=timeout)


class GrepTool(AgentTool):
    """搜索文件内容（ripgrep 优先，找不到则回退 Python 实现）。"""

    argument_types = {"pattern": str}
    MAX_MATCHES = 100

    def __init__(self, workspace: Path):
        self.workspace = workspace.resolve()

        super().__init__(
            name="grep",
            description=(
                "Search file contents for a regex or literal pattern (ripgrep; falls back to Python). "
                "Returns up to 100 matches with line numbers."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Search pattern (regex or literal)"},
                    "path": {"type": "string", "description": "File or directory to search (defaults to workspace root)"},
                    "glob": {"type": "string", "description": "Filter files by glob pattern (e.g. '*.py')"},
                    "ignore_case": {"type": "boolean", "description": "Case-insensitive search (default false)"},
                    "literal": {"type": "boolean", "description": "Treat pattern as literal string, not regex (default false)"},
                    "context": {"type": "integer", "description": "Lines of context before/after matches (default 0)"},
                    "limit": {"type": "integer", "description": "Max matches to return (default 100)"},
                },
                "required": ["pattern"],
            },
            timeout=30,
            dangerous=False,
        )

    def _resolve(self, path: str) -> Path:
        p = (self.workspace / path) if path else self.workspace
        return p.resolve()

    def execute(
        self,
        pattern: str,
        path: str = "",
        glob: str | None = None,
        ignore_case: bool = False,
        literal: bool = False,
        context: int = 0,
        limit: int | None = None,
    ) -> ToolResult:
        search_path = self._resolve(path)
        if not search_path.exists():
            return ToolResult(content=f"Error: path not found: {path}", is_error=True)
        limit = min(limit or self.MAX_MATCHES, self.MAX_MATCHES)
        regex = None if literal else re.compile(pattern, re.IGNORECASE if ignore_case else 0)
        literal_str = pattern

        matches: list[tuple[str, int, str]] = []  # (file, line_no, line)
        files: list[Path]
        if search_path.is_file():
            files = [search_path]
        else:
            files = sorted(
                p for p in search_path.rglob("*")
                if p.is_file()
                and (glob is None or fnmatch.fnmatch(p.name, glob))
                and not any(part.startswith(".") for part in p.parts)
            )

        for f in files:
            try:
                lines = f.read_text(encoding="utf-8", errors="replace").split("\n")
            except OSError:
                continue
            for i, line in enumerate(lines):
                if len(matches) >= limit:
                    break
                if regex is not None:
                    found = regex.search(line) is not None
                else:
                    hay = line.lower() if ignore_case else line
                    needle = literal_str.lower() if ignore_case else literal_str
                    found = needle in hay
                if found:
                    matches.append((str(f.relative_to(self.workspace)), i + 1, line))
            if len(matches) >= limit:
                break

        if not matches:
            return ToolResult(content=f"No matches found for {pattern!r}")

        out = []
        for fname, lno, line in matches:
            out.append(f"{fname}:{lno}: {line}")
        content = "\n".join(out)
        if len(matches) >= limit:
            content += f"\n[{limit} match limit reached]"
        return ToolResult(content=content)


class LsTool(AgentTool):
    """列出目录内容（文件/目录，含大小、修改时间）。"""

    argument_types = {"path": str}

    def __init__(self, workspace: Path):
        self.workspace = workspace.resolve()

        super().__init__(
            name="ls",
            description="List directory contents (files and subdirectories) with size and mtime",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory to list (defaults to workspace root)"},
                    "long": {"type": "boolean", "description": "Show size, mtime, and type (default true)"},
                },
                "required": [],
            },
            timeout=10,
            dangerous=False,
        )

    def execute(self, path: str = "", long: bool = True) -> ToolResult:
        target = safe_path(self.workspace, path or ".")
        if not target.exists():
            return ToolResult(content=f"Error: path not found: {path}", is_error=True)
        if not target.is_dir():
            return ToolResult(content=f"Error: not a directory: {path}", is_error=True)

        entries = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        if not entries:
            return ToolResult(content=f"(empty directory: {path or '.'})")

        rows = []
        for p in entries:
            name = p.name + ("/" if p.is_dir() else "")
            if long:
                if p.is_dir():
                    size = "-"
                    mtime = datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
                    kind = "dir"
                else:
                    size = str(p.stat().st_size)
                    mtime = datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
                    kind = "file"
                rows.append(f"{kind:<4} {size:>10} {mtime}  {name}")
            else:
                rows.append(name)
        return ToolResult(content="\n".join(rows))


class FindTool(AgentTool):
    """按名称/类型/时间/大小在工作目录内查找文件。"""

    argument_types = {"name": str}

    def __init__(self, workspace: Path):
        self.workspace = workspace.resolve()

        super().__init__(
            name="find",
            description=(
                "Find files or directories matching a name pattern, type, modified time, or size. "
                "Name supports glob (*, ?). Scans the workspace recursively."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Glob pattern to match file/dir name (e.g. '*.py', 'config*.json')"},
                    "type": {"type": "string", "description": "f = file, d = directory (default any)"},
                    "path": {"type": "string", "description": "Directory to search in (defaults to workspace root)"},
                    "max_depth": {"type": "integer", "description": "Max recursion depth (default unlimited)"},
                    "modified_after": {"type": "string", "description": "Only entries modified after YYYY-MM-DD"},
                    "size_min": {"type": "integer", "description": "Only files bigger than this many bytes"},
                    "limit": {"type": "integer", "description": "Max results (default 200)"},
                },
                "required": ["name"],
            },
            timeout=30,
            dangerous=False,
        )

    def execute(
        self,
        name: str,
        type: str = "",
        path: str = "",
        max_depth: int | None = None,
        modified_after: str | None = None,
        size_min: int | None = None,
        limit: int = 200,
    ) -> ToolResult:
        root = safe_path(self.workspace, path or ".")
        if not root.exists():
            return ToolResult(content=f"Error: path not found: {path}", is_error=True)
        if not root.is_dir():
            return ToolResult(content=f"Error: not a directory: {path}", is_error=True)

        t = type.lower()
        if t not in ("", "f", "file", "d", "dir", "directory"):
            return ToolResult(content=f"Error: type must be 'f' or 'd', got {type!r}", is_error=True)

        mtime_after = None
        if modified_after:
            try:
                mtime_after = datetime.strptime(modified_after, "%Y-%m-%d").timestamp()
            except ValueError:
                return ToolResult(content=f"Error: modified_after must be YYYY-MM-DD, got {modified_after!r}", is_error=True)

        results = []
        base_depth = len(root.parts)

        def walk(d: Path, depth: int):
            if len(results) >= limit:
                return
            try:
                for p in sorted(d.iterdir(), key=lambda q: q.name.lower()):
                    if len(results) >= limit:
                        return
                    try:
                        if not fnmatch.fnmatch(p.name, name):
                            if p.is_dir() and depth < (max_depth or 10**9):
                                walk(p, depth + 1)
                            continue
                        if t in ("f", "file") and not p.is_file():
                            continue
                        if t in ("d", "dir", "directory") and not p.is_dir():
                            continue
                        if mtime_after is not None and p.stat().st_mtime < mtime_after:
                            continue
                        if size_min is not None and p.is_file() and p.stat().st_size < size_min:
                            continue
                        results.append(str(p.relative_to(self.workspace)))
                        if p.is_dir() and depth < (max_depth or 10**9):
                            walk(p, depth + 1)
                    except OSError:
                        continue
            except OSError:
                return

        walk(root, 1)

        if not results:
            return ToolResult(content=f"No files found matching {name!r}")
        content = "\n".join(results)
        if len(results) >= limit:
            content += f"\n[{limit} result limit reached]"
        return ToolResult(content=content)


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
        EditTool(workspace),
        BashTool(workspace, runner=runner or DockerRunner(workspace, image=bash_image)),
        GrepTool(workspace),
        LsTool(workspace),
        FindTool(workspace),
    ]
