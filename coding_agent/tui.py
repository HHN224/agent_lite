"""字符驱动的终端 TUI：Claude Code / Pico 风格 transcript。

消费 Agent.prompt() 的事件流（agent_start / turn_start / message_update /
thinking_update / tool_execution_* / agent_end），把事件渲染成连续的终端文本流。
Textual 只负责布局、输入和事件循环；可见界面由字符而非应用式控件构成。

运行方式：agent-lite --ui tui（默认）。
"""

from __future__ import annotations

import asyncio
import json
import os
import queue
import threading
import time
from pathlib import Path

from rich.text import Text
from rich.markdown import Markdown as RichMarkdown
from textual import constants, events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Input, Label, Static

from agent_core import Agent, AgentEvent
from agent_core.content import image_block
from agent_core.tool_executor import PermissionPolicy


if os.name == "nt":
    from ctypes import byref, wintypes
    from textual._xterm_parser import XTermParser
    from textual.drivers import win32
    from textual.drivers._writer_thread import WriterThread
    from textual.drivers.windows_driver import WindowsDriver

    class _WindowsInputMonitor(win32.EventMonitor):
        """Keep Escape in synthetic VT replies emitted by Windows Terminal.

        Textual's monitor drops control-modified keys with virtual-key code 0,
        including the ESC of a cursor-position reply. The remaining [row;colR
        is then delivered as typed text. Preserve ESC before parsing VT input.
        """

        def run(self):
            parser = XTermParser(debug=constants.DEBUG)
            handle = win32.GetStdHandle(win32.STD_INPUT_HANDLE)
            records = (win32.INPUT_RECORD * 1024)()
            count = wintypes.DWORD()
            try:
                while not self.exit_event.is_set():
                    for event in parser.tick():
                        self.process_event(event)
                    if win32.wait_for_handles([handle], 100) is None:
                        continue
                    if not win32.KERNEL32.ReadConsoleInputW(handle, byref(records), 1024, byref(count)):
                        raise OSError("ReadConsoleInputW failed")
                    keys = []
                    size = None
                    for record in records[:count.value]:
                        if record.EventType == 1:
                            key = record.Event.KeyEvent
                            char = key.uChar.UnicodeChar
                            if not key.bKeyDown or char == "\0":
                                continue
                            if key.dwControlKeyState and key.wVirtualKeyCode == 0 and char != "\x1b":
                                continue
                            keys.append(char * max(1, key.wRepeatCount))
                        elif record.EventType == 4:
                            dimensions = record.Event.WindowBufferSizeEvent.dwSize
                            size = (dimensions.X, dimensions.Y)
                    if keys:
                        data = "".join(keys).encode("utf-16", "surrogatepass").decode("utf-16")
                        for event in parser.feed(data):
                            self.process_event(event)
                    if size is not None:
                        self.on_size_change(*size)
            except Exception as exc:
                self.app.log.error("Terminal input failed", exc)

    class _WindowsInlineDriver(WindowsDriver):
        """Textual 的 Windows driver，但保留当前终端的 scrollback。"""

        @property
        def is_inline(self) -> bool:
            return True

        def write(self, data: str) -> None:
            # WindowsDriver 会无条件进入备用屏幕。过滤这两个序列后仍复用它的
            # 键盘、resize 和 Unicode 输入处理，界面则由 inline compositor 渲染。
            data = data.replace("\x1b[?1049h", "").replace("\x1b[?1049l", "")
            if data:
                super().write(data)

        def start_application_mode(self) -> None:
            self._restore_console = win32.enable_application_mode()
            self._writer_thread = WriterThread(self._file)
            self._writer_thread.start()
            self._enable_mouse_support()
            self.write("\x1b[?25l\x1b[?1004h")
            self._enable_bracketed_paste()
            self.write("\n" * self._app.INLINE_PADDING)
            self.flush()
            self._event_thread = _WindowsInputMonitor(
                asyncio.get_running_loop(), self._app, self.exit_event, self.process_message
            )
            self._event_thread.start()

        def process_message(self, message):
            if isinstance(message, events.CursorPosition):
                self.cursor_origin = (message.x, message.y)
                return
            super().process_message(message)


class TranscriptScroll(VerticalScroll):
    """Observe scroll intent before the scroll widget consumes the event."""

    def _on_mouse_scroll_up(self, event):
        self.app._follow_tail = False
        super()._on_mouse_scroll_up(event)

    def _on_mouse_scroll_down(self, event):
        super()._on_mouse_scroll_down(event)
        self.app._follow_tail = self.is_vertical_scroll_end

    def watch_scroll_y(self, old_value, new_value):
        super().watch_scroll_y(old_value, new_value)
        if new_value < old_value:
            self.app._follow_tail = False


def _content_text(content) -> str:
    """把消息 content（str 或结构块列表）提取为可读文本。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        content = [content]
    parts = []
    for b in content:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t == "text":
            parts.append(b.get("text") or "")
        elif t == "image_url":
            url = b.get("image_url", {}).get("url", "")
            parts.append(f"[image:{url[:30]}]")
        elif t == "tool_result":
            parts.append(_content_text(b.get("content")))
    return "".join(parts)


def _compact_tool_arguments(arguments, limit: int = 96) -> str:
    """把工具参数压成适合单行标题的预览。"""
    try:
        value = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        value = str(arguments)
    value = " ".join(value.split())
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _fmt_count(value: int) -> str:
    """以 Pico 状态栏使用的紧凑形式显示 token 数。"""
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}m"
    if value >= 1_000:
        return f"{value / 1_000:.1f}k"
    return str(value)


class LazyCommandRunner:
    """Resolve the configured sandbox on first command, outside the UI thread.

    Detection and fail-closed behavior remain owned by detect_backend; opening
    a chat no longer pays the Docker / WSL probe cost.
    """

    def __init__(self, factory, workspace, mode):
        self._factory = factory
        self.workspace = Path(workspace)
        self.timeout = 60
        self._mode = mode
        self._runner = None
        self._lock = threading.Lock()

    @property
    def mode(self):
        return self._runner.mode if self._runner is not None else f"{self._mode}（待检测）"

    def describe(self):
        return self._runner.describe() if self._runner is not None else "首次执行命令前检测沙箱"

    def run(self, command, timeout=None):
        with self._lock:
            if self._runner is None:
                self._runner = self._factory()
        return self._runner.run(command, timeout=timeout)


class SessionIntro(Vertical):
    """由普通字符组成的会话开场信息。"""

    def __init__(self, *, model: str, cwd: str, session_id: str, tool_count: int) -> None:
        super().__init__(classes="session-intro")
        self.model = model
        self.cwd = cwd
        self.session_id = session_id
        self.tool_count = tool_count

    def compose(self) -> ComposeResult:
        brand = Text()
        brand.append("◆ ", style="bold #f0c75e")
        brand.append("agent-lite", style="bold #e6e6e6")
        brand.append("  coding agent", style="#666666")

        meta = Text("  ")
        meta.append(self.model, style="#b8b8b8")
        meta.append(f" · {self.tool_count} tools · {self.session_id}", style="#666666")

        help_line = Text("  ")
        help_line.append("/help", style="#7aa2f7")
        help_line.append(" commands", style="#666666")

        yield Static(brand, classes="intro-brand")
        yield Static(Text(f"  {self.cwd}", style="#666666"), classes="intro-path")
        yield Static(meta, classes="intro-meta")
        yield Static(help_line, classes="intro-help")


class AgentApp(App):
    """Pico 风格：原生终端 transcript + 字符 composer。"""

    TITLE = "agent-lite"
    SUB_TITLE = "coding agent"
    BINDINGS = [
        Binding("escape", "stop", "停止", priority=True),
        Binding("ctrl+c", "interrupt", "停止 / 退出", priority=True),
        Binding("ctrl+d", "quit", "退出", priority=True),
        ("pageup", "history_up", "向上翻页"),
        ("pagedown", "history_down", "向下翻页"),
        ("ctrl+end", "latest", "回到底部"),
    ]

    CSS = """
    Screen {
        layout: vertical;
        background: #0d0d0d;
        color: #e6e6e6;
        overflow: hidden;
    }
    Screen:inline { border: none; }

    #scroll {
        height: auto;
        padding: 0 2;
        background: #0d0d0d;
        scrollbar-size: 1 0;
    }

    .session-intro {
        height: auto;
        margin: 1 0 2 0;
        background: transparent;
    }

    .intro-brand, .intro-path, .intro-meta, .intro-help {
        width: 1fr;
        height: 1;
        background: transparent;
        text-overflow: ellipsis;
    }

    .user-line {
        width: 1fr;
        height: auto;
        margin: 1 0;
        padding: 0;
        background: transparent;
        color: #e6e6e6;
    }

    .assistant-card {
        width: 1fr;
        height: auto;
        margin: 0 0 1 2;
        padding: 0;
        border: none;
        background: transparent;
        color: #e6e6e6;
    }

    .assistant-card.thinking {
        color: #bb9af7;
    }

    .tool-card {
        width: 1fr;
        height: auto;
        margin: 0 0 1 4;
        padding: 0;
        border: none;
        background: transparent;
    }

    .tool-title, .tool-result {
        width: 1fr;
        height: auto;
        background: transparent;
        color: #6c6c6c;
    }

    .notice {
        width: 1fr;
        height: auto;
        margin: 0 0 1 2;
        color: #6c6c6c;
        background: transparent;
    }
    .notice.info { color: #7aa2f7; }
    .notice.warn { color: #e0af68; }
    .notice.error { color: #f7768e; }

    .command-panel {
        width: 1fr;
        height: auto;
        margin: 0 0 1 2;
        padding: 0;
        color: #b8b8b8;
        background: transparent;
    }

    #composer-shell {
        height: 3;
        margin: 0 2;
        background: transparent;
    }

    #composer-top, #composer-bottom {
        height: 1;
        width: 1fr;
        color: #505058;
        background: transparent;
        text-overflow: clip;
    }

    #composer-row {
        height: 1;
        width: 1fr;
        background: transparent;
    }

    #composer-left, #composer-right {
        width: 2;
        height: 1;
        color: #505058;
        background: transparent;
    }

    #prompt {
        width: 2;
        height: 1;
        color: #d8d8d8;
        text-style: bold;
        background: transparent;
    }

    #input {
        width: 1fr;
        height: 1;
        padding: 0;
        border: none;
        background: transparent;
        color: #e6e6e6;
    }
    #input:focus { border: none; background-tint: transparent; }
    #input > .input--placeholder { color: #6c6c6c; }
    #input > .input--cursor { background: #d8d8d8; color: #0d0d0d; }
    #input > .input--selection { background: #363636; color: #e6e6e6; }

    #status-row {
        height: 1;
        margin: 0 2;
        background: transparent;
        color: #6c6c6c;
    }
    #status { width: 1fr; color: #6c6c6c; overflow: hidden; text-overflow: ellipsis; }
    #hint { width: auto; color: #6c6c6c; text-align: right; }
    """

    def __init__(self, agent: Agent | None = None, *, agent_factory=None, workspace=None, **kwargs):
        super().__init__(**kwargs)
        self.agent = agent
        self._agent_factory = agent_factory
        self._workspace = str(workspace or Path.cwd())
        self._initializing = True
        self._operation_busy = False
        self._prompt_running = False
        self._startup_error = False
        self._shutdown_event = threading.Event()
        self._events = queue.Queue(maxsize=4096)
        self._permission = None
        self._original_confirm = None
        self._normal_permission_policy = PermissionPolicy.ASK
        self._stopping = False
        self._dirty = False
        self._follow_tail = True
        self._history = []
        self._history_start = 0
        self._history_loading = False
        self._pending_cards = []
        self._mount_task = None
        self._busy = False
        self._task: asyncio.Task | None = None
        self._assistant_widget = None
        self._assistant_buf = []
        self._thinking_buf = []
        self._thinking_widget = None
        self._thinking_tail = ""
        self._phase = "等待模型"
        self._round = 0
        self._started_at = 0.0
        self._tool_widget = None
        self._tool_title_widget = None
        self._tool_content_widget = None
        self._tool_title = ""

    def _build_driver(
        self,
        headless: bool,
        inline: bool,
        mouse: bool,
        size: tuple[int, int] | None,
    ):
        """补上 Textual 在 Windows 上缺失的 inline driver 选择。"""
        if os.name == "nt" and inline and not headless:
            driver = _WindowsInlineDriver(
                self,
                debug=constants.DEBUG,
                mouse=mouse,
                size=size,
            )
            self._driver = driver
            return driver
        return super()._build_driver(headless, inline, mouse, size)

    def compose(self) -> ComposeResult:
        yield TranscriptScroll(id="scroll")
        with Vertical(id="composer-shell"):
            yield Static("", id="composer-top", markup=False)
            with Horizontal(id="composer-row"):
                yield Label("│ ", id="composer-left", markup=False)
                yield Label("❯", id="prompt", markup=False)
                yield Input(id="input", placeholder="Ask anything…", compact=True)
                yield Label(" │", id="composer-right", markup=False)
            yield Static("", id="composer-bottom", markup=False)
        with Horizontal(id="status-row"):
            yield Label(id="status")
            yield Label("/help", id="hint", markup=False)

    def on_mount(self):
        self.query_one("#input", Input).focus()
        # Inline Textual repaints the entire viewport on a cursor blink.
        self.query_one("#input", Input).cursor_blink = False
        self._fit_transcript()
        self.set_interval(1 / 30, self._drain_events)
        self.set_interval(0.25, self._heartbeat)
        self._update_status()
        self.call_after_refresh(self._draw_composer_frame)
        if self.agent is None:
            self._add_notice("◆ agent-lite · 正在准备会话和运行环境…", "info")
        self._task = asyncio.create_task(self._initialize())

    async def _initialize(self):
        try:
            if self.agent is None:
                result = []
                def initialize():
                    try:
                        result.append(self._agent_factory())
                    except SystemExit as exc:
                        raise RuntimeError(str(exc)) from exc
                await self._run_worker(initialize)
                self.agent = result[0]
            self._attach_agent()
            await self._show_session()
        except Exception as exc:
            self._startup_error = True
            self._add_notice(f"启动失败: {exc} · Ctrl+D 退出", "error")
        finally:
            self._initializing = False
            self._update_status()

    def _attach_agent(self):
        self._original_confirm = self.agent.loop.executor.confirm
        if not self._bypass_enabled:
            self._normal_permission_policy = self.agent.loop.executor.permission_policy
        self.agent.loop.executor.confirm = self._confirm_tool

    @property
    def _bypass_enabled(self):
        return self.agent is not None and self.agent.loop.executor.permission_policy == PermissionPolicy.AUTO

    def _set_bypass(self, enabled):
        executor = self.agent.loop.executor
        if enabled:
            if not self._bypass_enabled:
                self._normal_permission_policy = executor.permission_policy
            executor.permission_policy = PermissionPolicy.AUTO
            if self._permission is not None:
                self._answer_permission(not (self._stopping or self._shutdown_event.is_set()))
            self._add_notice("◆ BYPASS 已开启 · 工具请求自动通过 · /bypass off 关闭", "warn")
        else:
            if self._bypass_enabled:
                executor.permission_policy = self._normal_permission_policy
            self._add_notice(f"◆ BYPASS 已关闭 · 权限策略: {executor.permission_policy.value}", "info")
        self._update_status()

    async def _show_session(self):
        # Use the complete active branch, not the compacted LLM payload.
        self._history = await asyncio.to_thread(self.agent.session._path_to_head)
        self._history_start = max(0, len(self._history) - 100)
        await self._flush_cards()
        scroll = self.query_one("#scroll", VerticalScroll)
        await scroll.remove_children()
        self._add_card(SessionIntro(
            model=str(getattr(self.agent.loop, "model", "—")),
            cwd=self._workspace,
            session_id=str(getattr(self.agent.session, "session_id", "—")),
            tool_count=len(getattr(self.agent.loop, "tools", ()) or ()),
        ))
        count = self.agent.session.message_count
        name = self.agent.session.name or "未命名"
        self._add_notice(
            f"已恢复会话 · {name} · {count} 条消息" if count else f"新会话 · {name}", "info"
        )
        if self._history_start:
            self._add_card(Static(Text(f"还有 {self._history_start} 条较早记录，/older 加载"), classes="notice info", id="history-marker"))
        for entry in self._history[self._history_start:]:
            self._add_card(self._history_card(entry))
        self._follow_tail = True
        await self._flush_cards()
        self._update_status()

    def _history_card(self, entry):
        content = _content_text(entry.content)
        if entry.type == "compaction":
            return Static(Text("◆ 上下文已压缩 · 原始对话仍保留"), classes="notice")
        if entry.role == "user":
            return Static(Text("❯ " + content), classes="user-line")
        if entry.role == "assistant":
            thinking = "\n".join(
                block.get("text", "") for block in (entry.content if isinstance(entry.content, list) else [])
                if isinstance(block, dict) and block.get("type") == "thinking"
            )
            calls = []
            for call in entry.tool_calls or []:
                fn = call.get("function", {})
                calls.append("✦ " + fn.get("name", "tool") + "  " + _compact_tool_arguments(fn.get("arguments", {})))
            body = "\n\n".join(part for part in [content, *calls] if part)
            if thinking:
                return Vertical(
                    Static(Text("✦ 思考\n" + thinking, style="#bb9af7"), classes="assistant-card thinking"),
                    Static(RichMarkdown(body), classes="assistant-card") if body else Static("", classes="notice"),
                    classes="tool-card",
                )
            return Static(RichMarkdown(body or "（无文本回复）"), classes="assistant-card")
        return Static(Text("╰─ " + (content[:1200] or "（空结果）")), classes="tool-card")

    async def _load_older(self):
        if self._history_loading:
            return
        if not self._history_start:
            self._add_notice("已显示全部历史")
            return
        self._history_loading = True
        try:
            end = self._history_start
            self._history_start = max(0, end - 100)
            scroll = self.query_one("#scroll", VerticalScroll)
            self._follow_tail = False
            await self._flush_cards()
            await scroll.mount(*(self._history_card(e) for e in self._history[self._history_start:end]), before=3)
            self.query_one("#history-marker", Static).update(Text(
                f"还有 {self._history_start} 条较早记录，/older 加载" if self._history_start else "已显示全部历史"
            ))
            scroll.scroll_home(animate=False)
        finally:
            self._history_loading = False

    def _fit_transcript(self, height=None):
        # Inline screens need auto height, but an unbounded transcript pushes
        # the composer below the terminal. Reserve its four rows on every resize.
        self.query_one("#scroll").styles.max_height = max(1, (self.size.height if height is None else height) - 5)

    def on_resize(self, event: events.Resize) -> None:
        self._fit_transcript(event.size.height)
        self.call_after_refresh(self._draw_composer_frame)

    def _draw_composer_frame(self) -> None:
        """按当前终端宽度绘制真正的字符边框，而不是 Widget 边框。"""
        shell = self.query_one("#composer-shell", Vertical)
        width = max(2, shell.size.width)
        self.query_one("#composer-top", Static).update("╭" + "─" * (width - 2) + "╮")
        self.query_one("#composer-bottom", Static).update("╰" + "─" * (width - 2) + "╯")

    def _add_card(self, widget):
        self._pending_cards.append(widget)
        if self._mount_task is None or self._mount_task.done():
            self._mount_task = asyncio.create_task(self._mount_cards())

    async def _mount_cards(self):
        scroll = self.query_one("#scroll", VerticalScroll)
        while self._pending_cards:
            cards, self._pending_cards = self._pending_cards, []
            await scroll.mount(*cards)
        if self._follow_tail:
            scroll.scroll_end(animate=False)

    async def _flush_cards(self):
        if self._mount_task is not None:
            await self._mount_task

    def _update_status(self):
        if self._initializing or self.agent is None:
            self.query_one("#status", Label).update("启动失败" if self._startup_error else "◌ 正在初始化 · 可先输入草稿")
            self.query_one("#hint", Label).update("Ctrl+D 退出")
            return
        s = self.agent.session
        model = str(getattr(self.agent.loop, "model", "—"))
        busy = "stopping…" if self._stopping else ("working" if self._busy else "ready")
        if self._busy and not self._stopping:
            elapsed = max(0, int(time.monotonic() - self._started_at))
            spinner = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"[int(time.monotonic() * 4) % 10]
            busy = f"{spinner} {self._phase} {elapsed}s · {self._round}/{self.agent.loop.max_iterations}轮"
        if self._permission is not None:
            busy = "等待授权 · /allow 或 /deny"
        msgs = s.message_count
        usage = int(getattr(s, "usage", 0) or 0)
        window = int(getattr(self.agent, "context_window", 0) or 0)

        status = Text()
        status.append("◆ ", style="#bb9af7" if self._busy else "#9ece6a")
        status.append(busy, style="#bb9af7" if self._busy else "#9ece6a")
        if self._bypass_enabled:
            status.append(" · BYPASS", style="bold #e0af68")
        status.append(f" · {model} · ", style="#6c6c6c")
        if window:
            status.append(f"{_fmt_count(usage)}/{_fmt_count(window)} ctx", style="#6c6c6c")
        else:
            status.append(f"{_fmt_count(usage)} tok", style="#6c6c6c")
        status.append(f" · {s.session_id[:8]} · {msgs} msg", style="#6c6c6c")
        self.query_one("#status", Label).update(status)
        self.query_one("#hint", Label).update("Enter 插话 · Esc 停止" if self._busy else "/help · PgUp 历史")

    def _heartbeat(self):
        if self._busy:
            self._update_status()

    def _add_user(self, text: str):
        body = Text()
        body.append("❯ ", style="bold #d8d8d8")
        body.append(text, style="#e6e6e6")
        self._add_card(Static(body, classes="user-line"))

    def _add_notice(self, text: str, tone: str = "muted"):
        classes = "notice" if tone == "muted" else f"notice {tone}"
        self._add_card(Static(Text(text), classes=classes, markup=False))

    def _start_assistant(self):
        self._assistant_buf = []
        self._assistant_widget = Static("", markup=False)
        self._assistant_widget.classes = "assistant-card"
        self._add_card(self._assistant_widget)

    def _update_assistant(self):
        if self._assistant_widget is None:
            return
        text = "".join(self._assistant_buf)
        self._assistant_widget.update(Text(("● " + text) if text else ""))
        self._dirty = False

    def _update_thinking(self):
        if not self._thinking_buf:
            return
        remaining = self._thinking_tail + "".join(self._thinking_buf)
        self._thinking_buf.clear()
        # Freeze completed chunks, so long reasoning never reparses/repaints
        # every preceding character for each token.
        while remaining:
            if self._thinking_widget is None:
                self._thinking_widget = Static("", markup=False, classes="assistant-card thinking")
                self._add_card(self._thinking_widget)
            chunk, remaining = remaining[:4000], remaining[4000:]
            self._thinking_widget.update(Text("✦ " + chunk, style="#bb9af7"))
            self._thinking_tail = chunk
            if remaining:
                self._thinking_widget = None
                self._thinking_tail = ""

    def _end_thinking(self):
        self._update_thinking()
        self._thinking_widget = None
        self._thinking_tail = ""

    def _end_assistant(self):
        if self._assistant_widget is not None:
            self._update_assistant()
            text = "".join(self._assistant_buf)
            if text:
                self._assistant_widget.update(RichMarkdown(text))
        self._assistant_widget = None

    def _start_tool(self, name: str, arguments):
        preview = _compact_tool_arguments(arguments)
        title = name + (f"  {preview}" if preview and preview != "{}" else "")
        title_text = Text("├─ ", style="#505058")
        title_text.append("✦ ", style="#bb9af7")
        title_text.append(title, style="#b8b8b8")
        title_widget = Static(title_text, classes="tool-title", markup=False)
        content_text = Text("╰─ running…", style="#666666")
        content = Static(content_text, classes="tool-result", markup=False)
        self._tool_title = title
        self._tool_title_widget = title_widget
        self._tool_content_widget = content
        self._tool_widget = Vertical(title_widget, content, classes="tool-card")
        self._add_card(self._tool_widget)

    def _end_tool(self, content_text, pruned, full_path, is_error=False):
        if self._tool_widget is None:
            return
        suffix = "（已截断）" if pruned else ""
        if full_path:
            suffix += f" —— 全文: {full_path}"
        body = _content_text(content_text)[:500] + suffix
        if self._tool_title_widget is not None:
            title = Text("├─ ", style="#505058")
            title.append("× " if is_error else "✓ ", style="#f7768e" if is_error else "#9ece6a")
            title.append(self._tool_title, style="#b8b8b8")
            self._tool_title_widget.update(title)
        if self._tool_content_widget is not None:
            lines = (body or "(empty result)").replace("\r\n", "\n").splitlines()
            shown = lines[:4]
            result = Text()
            for index, line in enumerate(shown):
                result.append("╰─ " if index == 0 else "   ", style="#505058")
                result.append(line, style="#666666")
                if index < len(shown) - 1:
                    result.append("\n")
            if len(lines) > len(shown):
                result.append("\n   …", style="#505058")
            self._tool_content_widget.update(result)
        self._tool_widget = None
        self._tool_title_widget = None
        self._tool_content_widget = None
        self._tool_title = ""

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text:
            return
        if self._initializing or self.agent is None:
            self._add_notice("运行环境尚未准备好，草稿已保留。", "warn")
            return
        if self._operation_busy and not self._prompt_running and not text.startswith("/"):
            self._add_notice("正在处理会话操作，草稿已保留。", "warn")
            return
        event.input.value = ""
        if text.startswith("/"):
            self._handle_command(text)
            return
        if self._busy:
            self.agent.steer(text)
            self._add_notice(f"↳ 已排队插话，下个安全检查点处理: {text}", "warn")
            return
        self._start_prompt(text)

    def _handle_command(self, text: str):
        command = text.split(maxsplit=1)[0]
        if command == "/bypass":
            args = text.split()[1:]
            if args not in ([], ["on"], ["off"]):
                self._add_notice("用法: /bypass [on|off]", "warn")
            else:
                self._set_bypass(args != ["off"])
            return
        if command in ("/allow", "/deny"):
            self._answer_permission(command == "/allow")
            return
        if command in ("/stop", "/exit", "/quit"):
            self.action_stop() if command == "/stop" else self.action_quit()
            return
        if self._busy and command in ("/new", "/resume", "/clear", "/compact", "/mode", "/image"):
            self._add_notice("任务运行中，请先 Esc 停止并等待本轮结束。", "warn")
            return
        if text == "/help":
            self._add_card(Static("\n".join([
                "Commands",
                "  /help            命令帮助",
                "  /new             新建会话",
                "  /mode <name>     切换模式（default/plan/code/review）",
                "  /sessions        查看会话列表",
                "  /resume <id>     打开旧会话并显示历史",
                "  /older           加载更早的 100 条记录",
                "  /stop            停止当前任务（Esc）",
                "  /allow /deny     允许 / 拒绝当前工具请求",
                "  /bypass [on|off] 自动通过工具请求 / 恢复原权限策略",
                "  /image <path>    喂图",
                "  /clear           清空当前会话历史",
                "  /compact         手动压缩上下文",
                "  /exit            退出（Ctrl+D）",
                "  PgUp / PgDn      翻阅对话 · Ctrl+End 回到底部",
                "  运行中 Enter     插话；当前回复结束时自动接续",
            ]), classes="command-panel", markup=False))
        elif text == "/new":
            self._start_operation(self._new_session)
        elif text.startswith("/mode "):
            self._start_operation(self._change_mode, text[len("/mode "):].strip())
        elif text == "/sessions":
            asyncio.create_task(self._list_sessions())
        elif text.startswith("/resume "):
            self._start_operation(self._resume_session, text.split(maxsplit=1)[1].strip())
        elif text == "/older":
            asyncio.create_task(self._load_older())
        elif text == "/clear":
            self._start_operation(self._clear_session)
        elif text == "/compact":
            self._start_operation(self._compact)
        elif text.startswith("/image "):
            self._start_operation(self._send_image, text[len("/image "):].strip())
        else:
            self._add_notice(f"未知命令: {text}（输入 /help）", "warn")

    def _start_operation(self, function, *args):
        self._busy = True
        self._started_at = time.monotonic()
        self._phase = "处理会话操作"
        self._operation_busy = True
        self._update_status()
        async def run():
            try:
                await function(*args)
            except Exception as exc:
                self._add_notice(f"操作失败: {exc}", "error")
            finally:
                self._operation_busy = False
                if not self._shutdown_event.is_set():
                    self._after_prompt()
        self._task = asyncio.create_task(run())

    async def _new_session(self):
        repo = self.agent.repo
        if repo is None:
            self._add_notice("无会话仓库，无法新建", "warn")
            return
        session = repo.create(system_prompt=self.agent.session.system_prompt)
        await asyncio.to_thread(repo.save, session)
        await self._select_session(session)

    async def _select_session(self, session):
        from agent_core import ToolOutputTruncator, ToolResultStore
        self.agent.session = session
        self.agent.clear_queues()
        self.agent.loop.session_id = session.session_id
        if self.agent.repo is not None:
            folder = self.agent.repo._path(session.session_id).parent / session.session_id
            self.agent.loop.truncator = ToolOutputTruncator(store=ToolResultStore(folder))
        await self._show_session()

    async def _resume_session(self, session_id):
        if self.agent.repo is None:
            self._add_notice("无会话仓库", "warn")
            return
        session = await asyncio.to_thread(self.agent.repo.load, session_id)
        if session is None:
            self._add_notice(f"找不到会话: {session_id}", "error")
            return
        await self._select_session(session)

    async def _clear_session(self):
        await asyncio.to_thread(self.agent.clear_history)
        await self._show_session()

    async def _compact(self):
        if self.agent.compaction_engine is None:
            self._add_notice("未配置上下文压缩", "warn")
            return
        self._add_notice("✦ 正在压缩上下文…")
        def compact():
            for event in self.agent.compaction_engine.compact_if_needed(self.agent.session, self.agent.context_window):
                self._put_event(event)
            self.agent._save()
        await self._run_worker(compact)

    async def _change_mode(self, name: str):
        from coding_agent.modes import get_mode, mode_names
        if name not in mode_names():
            self._add_notice(f"未知 mode: {name}，可选: {', '.join(mode_names())}", "warn")
            return
        self.agent.session.system_prompt = get_mode(name)
        await asyncio.to_thread(self.agent._save)
        self._add_notice(f"◆ 已切换到 mode: {name}", "info")
        self._update_status()

    async def _list_sessions(self):
        repo = self.agent.repo
        if repo is None:
            self._add_notice("无会话仓库", "warn")
            return
        try:
            metas = await asyncio.to_thread(repo.list)
        except Exception as exc:
            self._add_notice(f"无法读取会话列表: {exc}", "error")
            return
        if not metas:
            self._add_notice("暂无会话")
            return
        self._add_notice("使用 /resume <id> 打开会话", "info")
        for m in metas:
            mark = " *" if m.session_id == self.agent.session_id else ""
            self._add_notice(f"{m.session_id}  {m.name or '<未命名>':<16}  {m.message_count} 条{mark}")

    async def _send_image(self, path: str):
        p = Path(path.strip('"')).resolve()
        if not p.is_file():
            self._add_notice(f"图片不存在: {path}", "error")
            return
        try:
            import base64, mimetypes
            data = base64.b64encode(await asyncio.to_thread(p.read_bytes)).decode("ascii")
            mime = mimetypes.guess_type(str(p))[0] or "image/png"
            img = image_block(data, mime)
        except Exception as e:
            self._add_notice(f"读图失败: {e}", "error")
            return
        self._add_user(f"(图片) {p.name}")
        await self._run_prompt("(图片)", images=[img], finish=False)

    def _start_prompt(self, text: str, images: list | None = None):
        if self._busy:
            self._add_notice("正在运行，请稍候", "warn")
            return
        self._busy = True
        self._stopping = False
        self._follow_tail = True
        self._started_at = time.monotonic()
        self._round = 0
        self._phase = "等待模型"
        self._add_user(text)
        self._update_status()
        self._task = asyncio.create_task(self._run_prompt(text, images))

    async def _run_prompt(self, text: str, images: list | None = None, *, finish=True):
        agent = self.agent
        self._prompt_running = True
        try:
            def consume():
                prompts = [(text, images)]
                while prompts and not self._shutdown_event.is_set():
                    message, attachments = prompts.pop(0)
                    for event in agent.prompt(message, images=attachments):
                        self._put_event(event)
                    if self._stopping:
                        break
                    # The core only polls steering between tool turns. A final
                    # text response has no next turn: explicitly continue it.
                    pending = agent._dequeue_steer()
                    for message in pending:
                        self._put_event(AgentEvent("steer", {"content": message["content"]}))
                        prompts.append((message["content"], None))
            await self._run_worker(consume)
        except Exception as e:
            self._add_notice(f"出错: {e}", "error")
        finally:
            self._prompt_running = False
            if finish and not self._shutdown_event.is_set():
                self._after_prompt()

    def _after_prompt(self):
        self._end_thinking()
        self._end_assistant()
        self._busy = False
        stopped = self._stopping
        self._stopping = False
        if stopped:
            self.agent.clear_queues()
            self._add_notice("◆ 本轮已停止", "warn")
        elif self.agent.steer_queue and not self._shutdown_event.is_set():
            # Input may arrive after the worker finished, before UI completion.
            pending = self.agent._dequeue_steer()
            for message in pending[1:]:
                self.agent.steer(message["content"])
            self._start_prompt(pending[0]["content"])
        self._update_status()

    def _put_event(self, event):
        while not self._shutdown_event.is_set():
            try:
                self._events.put(event, timeout=0.1)
                return
            except queue.Full:
                continue

    async def _run_worker(self, function):
        done = threading.Event()
        errors = []
        def run():
            try:
                function()
            except Exception as exc:
                errors.append(exc)
            finally:
                done.set()
        # A provider's blocking socket read must not trap terminal shutdown.
        threading.Thread(target=run, daemon=True, name="tui-agent").start()
        while not done.is_set() or not self._events.empty():
            self._drain_events()
            await asyncio.sleep(1 / 30)
        self._drain_events()
        if errors:
            raise errors[0]

    def _drain_events(self):
        start = time.monotonic()
        for _ in range(1000):
            try:
                event = self._events.get_nowait()
            except queue.Empty:
                break
            self._on_event(event)
            if time.monotonic() - start > 0.008:
                break
        changed = self._dirty or bool(self._thinking_buf)
        self._update_thinking()
        if self._dirty:
            self._update_assistant()
        if changed and self._follow_tail:
            self.query_one("#scroll", VerticalScroll).scroll_end(animate=False)

    def _confirm_tool(self, description):
        if self._shutdown_event.is_set() or self._stopping:
            return False
        if self._bypass_enabled:
            return True
        request = {"description": description, "done": threading.Event(), "allowed": False}
        self._put_event(AgentEvent("permission_request", request))
        while not request["done"].wait(0.1):
            if self._shutdown_event.is_set() or self._stopping:
                return False
        return request["allowed"]

    def _answer_permission(self, allowed):
        if self._permission is None:
            self._add_notice("当前没有待授权工具", "warn")
            return
        request, self._permission = self._permission, None
        request["allowed"] = allowed
        request["done"].set()
        self._add_notice("✓ 已允许执行" if allowed else "× 已拒绝执行", "info")
        self._update_status()

    def action_stop(self):
        if self.agent is not None and self._busy:
            self._stopping = True
            self.agent.abort()
            if self._permission is not None:
                self._answer_permission(False)
            self._add_notice("正在停止，等待当前请求或工具到达安全检查点…", "warn")
            self._update_status()

    def action_interrupt(self):
        if self._busy:
            self.action_stop()
        elif self.query_one("#input", Input).value:
            self.query_one("#input", Input).value = ""
        else:
            self.action_quit()

    def action_quit(self):
        self._shutdown_event.set()
        self.action_stop()
        self.exit()

    def on_unmount(self):
        self._shutdown_event.set()
        if self.agent is not None:
            self.agent.abort()
            if self._original_confirm is not None:
                self.agent.loop.executor.confirm = self._original_confirm
        if self._permission is not None:
            self._permission["done"].set()
        if self._task is not None:
            self._task.cancel()

    def action_history_up(self):
        self._follow_tail = False
        self.query_one("#scroll", VerticalScroll).scroll_page_up(animate=False)

    def action_history_down(self):
        scroll = self.query_one("#scroll", VerticalScroll)
        scroll.scroll_page_down(animate=False)
        self._follow_tail = scroll.is_vertical_scroll_end

    def action_latest(self):
        self._follow_tail = True
        self.query_one("#scroll", VerticalScroll).scroll_end(animate=False)

    def on_mouse_scroll_up(self):
        self._follow_tail = False

    def _on_event(self, event: AgentEvent):
        try:
            self._render_event(event)
        except Exception as e:
            self._add_notice(f"渲染出错: {e}", "error")

    def _render_event(self, event: AgentEvent):
        t = event.type
        if t == "agent_start":
            self._round = 0
        elif t == "turn_start":
            self._round += 1
            self._phase = "等待模型"
        elif t == "message_update":
            self._end_thinking()
            self._phase = "正在回复"
            if self._assistant_widget is None:
                self._start_assistant()
            self._assistant_buf.append(_content_text(event.data["content"]))
            self._dirty = True
        elif t == "thinking_update":
            self._phase = "思考中"
            self._thinking_buf.append(_content_text(event.data["content"]))
        elif t == "thinking_end":
            self._end_thinking()
        elif t == "message_end":
            self._end_assistant()
        elif t == "tool_execution_start":
            self._end_thinking()
            self._phase = "工具: " + event.data["name"]
            self._end_assistant()
            self._start_tool(event.data["name"], event.data["arguments"])
        elif t == "tool_execution_end":
            self._end_tool(event.data["content"], event.data.get("pruned"), event.data.get("full_output_path"), event.data.get("is_error", False))
            self._phase = "等待下一轮"
        elif t == "agent_end":
            self._end_thinking()
            result = event.data.get("text", "") or ""
            if "已达到最大工具调用轮数" in result:
                self._add_notice(f"已达到最大工具调用轮数（{self.agent.loop.max_iterations}轮），任务可能尚未完成。输入“继续”接续，或用 --max-iterations 增大轮数。", "warn")
            elif "已中止" in result and not self._stopping:
                self._add_notice(result.strip(), "warn")
        elif t == "steer":
            self._add_notice(f"↳ 正在处理插话: {event.data.get('content')}", "info")
        elif t == "permission_request":
            if self._stopping or self._shutdown_event.is_set():
                event.data["done"].set()
                return
            # A request may already be queued when the user enables bypass.
            if self._bypass_enabled:
                event.data["allowed"] = True
                event.data["done"].set()
                return
            self._permission = event.data
            self._add_notice(f"需要授权: {event.data['description']}\n输入 /allow 允许，/deny 拒绝 · 仍可输入插话", "warn")
            self._update_status()
        elif t == "error":
            self._add_notice(f"模型出错: {event.data.get('message')}", "error")
        elif t == "context_check":
            self._update_status()
        elif t == "compaction_end":
            d = event.data
            if d.get("success"):
                self._add_notice(f"◆ 压缩完成，折叠 {d['compacted_count']} 条")


def run_tui(agent: Agent | None = None, *, agent_factory=None, workspace=None) -> None:
    """在当前终端缓冲区内启动 TUI，并在退出后保留 transcript。"""
    AgentApp(agent, agent_factory=agent_factory, workspace=workspace).run(inline=True, inline_no_clear=True)
