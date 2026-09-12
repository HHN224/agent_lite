"""Textual TUI：Pico 风格的会话流、工具折叠与底部 composer。

消费 Agent.prompt() 的事件流（agent_start / turn_start / message_update /
thinking_update / tool_execution_* / agent_end），把每个消息渲染成独立卡片。
放弃逐字流式，采用"整块/半块刷新"——更贴近 Claude Code / pi。

运行方式：agent-lite --ui tui（默认）。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Collapsible, Input, Label, Markdown, Static

from agent_core import Agent, AgentEvent
from agent_core.content import image_block


def _content_text(content) -> str:
    """把消息 content（str 或结构块列表）提取为可读文本。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
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
            parts.append(b.get("content") or "")
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


class SessionIntro(Vertical):
    """会话开场信息；结构对应 Pico 的轻量 session panel。"""

    def __init__(self, *, model: str, cwd: str, tool_count: int) -> None:
        super().__init__(classes="session-intro")
        self.model = model
        self.cwd = cwd
        self.tool_count = tool_count

    def compose(self) -> ComposeResult:
        with Horizontal(classes="intro-header"):
            yield Label("[b]agent-lite[/b] [#6c6c6c]· coding agent[/]", classes="intro-name")
            yield Label(self.model, classes="intro-model", markup=False)
        yield Label(self.cwd, classes="intro-path", markup=False)
        yield Label(f"▸ Tools ({self.tool_count})", classes="intro-tools", markup=False)
        yield Label("/help  commands", classes="intro-help", markup=False)


class AgentApp(App):
    """Pico 风格：低噪音消息流 + 圆角 composer + 紧凑状态信息。"""

    TITLE = "agent-lite"
    SUB_TITLE = "coding agent"

    CSS = """
    Screen {
        layout: vertical;
        background: #141414;
        color: #e1e1e1;
    }

    #topbar {
        height: 1;
        padding: 0 2;
        background: #141414;
        color: #6c6c6c;
    }

    #workspace {
        width: 1fr;
        color: #6c6c6c;
        text-overflow: ellipsis;
    }

    #top-meta {
        width: auto;
        color: #c8c8c8;
        text-align: right;
    }

    #scroll {
        height: 1fr;
        padding: 0 2;
        background: #141414;
        scrollbar-background: #141414;
        scrollbar-color: #505058;
        scrollbar-color-hover: #7aa2f7;
        scrollbar-color-active: #c8c8c8;
    }

    .session-intro {
        height: auto;
        margin: 1 0;
    }

    .intro-header {
        height: 1;
        padding: 0 1;
        background: #242424;
    }

    .intro-name { width: 1fr; }
    .intro-model { width: auto; color: #6c6c6c; text-align: right; }
    .intro-path { height: 1; padding: 0 1; color: #6c6c6c; text-overflow: ellipsis; }
    .intro-tools { height: 1; padding: 0 1; color: #7aa2f7; text-style: bold; }
    .intro-help { height: 1; padding: 0 1; color: #6c6c6c; }

    .user-card {
        width: 1fr;
        height: auto;
        margin: 1 0;
        padding: 0 1;
        background: #242424;
        color: #e1e1e1;
    }

    .assistant-card {
        width: 1fr;
        height: auto;
        margin: 0 0 1 3;
        padding: 0 1;
        border-left: solid #323237;
        background: #141414;
        color: #e1e1e1;
    }

    .assistant-card.thinking {
        color: #bb9af7;
        border-left: solid #bb9af7;
    }

    .tool-card {
        width: 1fr;
        height: auto;
        margin: 0 0 1 3;
        padding: 0;
        border: none;
        background: #141414;
    }

    .tool-card:focus-within { background-tint: transparent; }
    .tool-card CollapsibleTitle {
        padding: 0;
        color: #6c6c6c;
        background: #141414;
    }
    .tool-card CollapsibleTitle:hover { color: #7aa2f7; background: #141414; }
    .tool-card CollapsibleTitle:focus { color: #7aa2f7; background: #242424; }
    .tool-card Contents { padding: 0 0 0 2; color: #6c6c6c; }

    .notice {
        width: 1fr;
        height: auto;
        margin: 0 0 1 3;
        color: #6c6c6c;
    }
    .notice.info { color: #7aa2f7; }
    .notice.warn { color: #e0af68; }
    .notice.error { color: #f7768e; }

    #status-row {
        height: 1;
        margin: 0 2;
        background: #141414;
        color: #6c6c6c;
    }

    #status { width: 1fr; color: #6c6c6c; }
    #status-cwd {
        width: auto;
        max-width: 40%;
        color: #c8c8c8;
        text-align: right;
        text-overflow: ellipsis;
    }

    #composer {
        height: 3;
        margin: 0 2;
        padding: 0 1;
        border: round #505058;
        background: #141414;
    }

    #composer:focus-within { border: round #7aa2f7; }
    #prompt {
        width: 2;
        height: 1;
        color: #c8c8c8;
        text-style: bold;
    }

    #input {
        width: 1fr;
        height: 1;
        padding: 0;
        border: none;
        background: #141414;
        color: #e1e1e1;
    }
    #input:focus { border: none; background-tint: transparent; }
    #input > .input--placeholder { color: #6c6c6c; }
    #input > .input--cursor { background: #c8c8c8; color: #141414; }
    #input > .input--selection { background: #363636; color: #e1e1e1; }

    #hints {
        height: 1;
        margin: 0 2;
        color: #6c6c6c;
    }
    #hint-left { width: 1fr; }
    #hint-right { width: auto; color: #6c6c6c; text-align: right; }
    """

    def __init__(self, agent: Agent, **kwargs):
        super().__init__(**kwargs)
        self.agent = agent
        self._busy = False
        self._task: asyncio.Task | None = None
        self._assistant_widget = None
        self._assistant_buf = []
        self._thinking_buf = []
        self._tool_widget = None
        self._tool_content_widget = None
        self._tool_title = ""

    def compose(self) -> ComposeResult:
        with Horizontal(id="topbar"):
            yield Label(str(Path.cwd()), id="workspace", markup=False)
            yield Label("", id="top-meta", markup=False)
        yield VerticalScroll(id="scroll")
        with Horizontal(id="status-row"):
            yield Label(id="status")
            yield Label(Path.cwd().name or str(Path.cwd()), id="status-cwd", markup=False)
        with Horizontal(id="composer"):
            yield Label("❯", id="prompt", markup=False)
            yield Input(id="input", placeholder="输入消息或 /help", compact=True)
        with Horizontal(id="hints"):
            yield Label("enter 发送  │  /help 命令", id="hint-left", markup=False)
            yield Label("", id="hint-right", markup=False)

    def on_mount(self):
        self.query_one("#input", Input).focus()
        self._update_status()
        self._add_card(SessionIntro(
            model=str(getattr(self.agent.loop, "model", "—")),
            cwd=str(Path.cwd()),
            tool_count=len(getattr(self.agent.loop, "tools", ()) or ()),
        ))

    def _add_card(self, widget):
        async def _m():
            scroll = self.query_one("#scroll")
            await scroll.mount(widget)
            scroll.scroll_end(animate=False)
        asyncio.create_task(_m())

    def _update_status(self):
        s = self.agent.session
        model = str(getattr(self.agent.loop, "model", "—"))
        busy = "working" if self._busy else "ready"
        msgs = s.message_count
        usage = int(getattr(s, "usage", 0) or 0)
        window = int(getattr(self.agent, "context_window", 0) or 0)

        status = Text()
        status.append("◆ ", style="#bb9af7" if self._busy else "#9ece6a")
        status.append(busy, style="#bb9af7" if self._busy else "#9ece6a")
        status.append(f" · {model} · ", style="#6c6c6c")
        if window:
            status.append(f"{_fmt_count(usage)}/{_fmt_count(window)} ctx", style="#6c6c6c")
        else:
            status.append(f"{_fmt_count(usage)} tok", style="#6c6c6c")
        status.append(f" · {msgs} msg", style="#6c6c6c")
        self.query_one("#status", Label).update(status)
        self.query_one("#top-meta", Label).update(
            f"{_fmt_count(usage)} / {_fmt_count(window)}" if window else f"{_fmt_count(usage)} tokens"
        )
        self.query_one("#hint-right", Label).update("输入可 steer" if self._busy else "")

    def _add_user(self, text: str):
        body = Text()
        body.append("❯ ", style="bold #c8c8c8")
        body.append(text, style="#e1e1e1")
        self._add_card(Static(body, classes="user-card"))

    def _add_notice(self, text: str, tone: str = "muted"):
        classes = "notice" if tone == "muted" else f"notice {tone}"
        self._add_card(Static(Text(text), classes=classes, markup=False))

    def _start_assistant(self):
        self._assistant_buf = []
        self._thinking_buf = []
        self._assistant_widget = Markdown("")
        self._assistant_widget.classes = "assistant-card"
        self._add_card(self._assistant_widget)

    def _update_assistant(self):
        if self._assistant_widget is None:
            return
        text = "".join(self._assistant_buf)
        if self._thinking_buf and not text:
            self._assistant_widget.add_class("thinking")
            self._assistant_widget.update("◇ thinking…")
        else:
            self._assistant_widget.remove_class("thinking")
            self._assistant_widget.update(text)

    def _end_assistant(self):
        self._update_assistant()
        self._assistant_widget = None

    def _start_tool(self, name: str, arguments):
        preview = _compact_tool_arguments(arguments)
        title = f"⚡ {name}" + (f"  {preview}" if preview and preview != "{}" else "")
        content = Static(Text("running…", style="#bb9af7"), markup=False)
        self._tool_title = title
        self._tool_content_widget = content
        self._tool_widget = Collapsible(
            content,
            title=title,
            collapsed=True,
            collapsed_symbol="▸",
            expanded_symbol="▾",
            classes="tool-card",
        )
        self._add_card(self._tool_widget)

    def _end_tool(self, content_text, pruned, full_path):
        if self._tool_widget is None:
            return
        suffix = "（已截断）" if pruned else ""
        if full_path:
            suffix += f" —— 全文: {full_path}"
        body = _content_text(content_text)[:500] + suffix
        if self._tool_content_widget is not None:
            self._tool_content_widget.update(Text(body or "(empty result)", style="#6c6c6c"))
        self._tool_widget.title = self._tool_title.replace("⚡", "✓", 1)
        self._tool_widget = None
        self._tool_content_widget = None
        self._tool_title = ""

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        if not text:
            return
        if text.startswith("/"):
            self._handle_command(text)
            return
        if self._busy:
            self.agent.steer(text)
            self._add_notice(f"↳ steer  {text}", "warn")
            return
        self._start_prompt(text)

    def _handle_command(self, text: str):
        if text == "/help":
            self._add_card(Static("\n".join([
                "/help            命令帮助",
                "/new             新建会话",
                "/mode <name>     切换模式（default/plan/code/review）",
                "/sessions        查看会话列表",
                "/image <path>    喂图",
                "/clear           清空当前会话历史",
                "/compact         手动压缩上下文",
            ]), markup=True))
        elif text == "/new":
            self._new_session()
        elif text.startswith("/mode "):
            self._change_mode(text[len("/mode "):].strip())
        elif text == "/sessions":
            self._list_sessions()
        elif text == "/clear":
            self.agent.clear_history()
            self._add_card(Static(">>> 已清空对话历史", markup=True))
        elif text == "/compact":
            self._add_card(Static(">>> 手动压缩...", markup=True))
            try:
                for ev in self.agent.compaction_engine.compact_if_needed(
                    self.agent.session, self.agent.context_window
                ):
                    self._render_event(ev)
                self.agent._save()
            except Exception as e:
                self._add_card(Static(f">>> 压缩失败: {e}", markup=True))
        elif text.startswith("/image "):
            self._send_image(text[len("/image "):].strip())
        else:
            self._add_notice(f"未知命令: {text}（输入 /help）", "warn")

    def _new_session(self):
        from coding_agent.modes import get_mode
        repo = self.agent.repo
        if repo is None:
            self._add_notice("无会话仓库，无法新建", "warn")
            return
        session = repo.create(system_prompt=get_mode("default"))
        repo.save(session)
        self.agent.session = session
        self.agent.steer_queue = []
        self.agent.follow_up_queue = []
        self._add_notice(f"◆ 已新建会话: {session.session_id}", "info")
        self._update_status()

    def _change_mode(self, name: str):
        from coding_agent.modes import get_mode, mode_names
        if name not in mode_names():
            self._add_notice(f"未知 mode: {name}，可选: {', '.join(mode_names())}", "warn")
            return
        self.agent.session.system_prompt = get_mode(name)
        self.agent._save()
        self._add_notice(f"◆ 已切换到 mode: {name}", "info")
        self._update_status()

    def _list_sessions(self):
        repo = self.agent.repo
        if repo is None:
            self._add_notice("无会话仓库", "warn")
            return
        metas = repo.list()
        if not metas:
            self._add_notice("暂无会话")
            return
        for m in metas:
            mark = " *" if m.session_id == self.agent.session_id else ""
            self._add_notice(f"{m.session_id}  {m.name or '<未命名>':<16}  {m.message_count} 条{mark}")

    def _send_image(self, path: str):
        p = Path(path).resolve()
        if not p.is_file():
            self._add_notice(f"图片不存在: {path}", "error")
            return
        try:
            import base64, mimetypes
            data = base64.b64encode(p.read_bytes()).decode("ascii")
            mime = mimetypes.guess_type(str(p))[0] or "image/png"
            img = image_block(data, mime)
        except Exception as e:
            self._add_notice(f"读图失败: {e}", "error")
            return
        self._start_prompt("(图片)", images=[img])

    def _start_prompt(self, text: str, images: list | None = None):
        if self._busy:
            self._add_notice("正在运行，请稍候", "warn")
            return
        self._busy = True
        self._add_user(text)
        self._start_assistant()
        self._update_status()
        self._task = asyncio.create_task(self._run_prompt(text, images))

    async def _run_prompt(self, text: str, images: list | None = None):
        agent = self.agent
        try:
            def consume():
                for event in agent.prompt(text, images=images):
                    self.call_from_thread(self._on_event, event)
            await asyncio.to_thread(consume)
        except Exception as e:
            self._add_notice(f"出错: {e}", "error")
        finally:
            self._after_prompt()

    def _after_prompt(self):
        self._end_assistant()
        self._busy = False
        self._update_status()

    def _on_event(self, event: AgentEvent):
        try:
            self._render_event(event)
        except Exception as e:
            self._add_notice(f"渲染出错: {e}", "error")

    def _render_event(self, event: AgentEvent):
        t = event.type
        if t == "agent_start":
            return
        elif t == "turn_start":
            return
        elif t == "message_update":
            if self._assistant_widget is None:
                self._start_assistant()
            self._assistant_buf.append(_content_text(event.data["content"]))
            self._update_assistant()
        elif t == "thinking_update":
            if self._assistant_widget is None:
                self._start_assistant()
            self._thinking_buf.append(_content_text(event.data["content"]))
            self._update_assistant()
        elif t == "tool_execution_start":
            self._end_assistant()
            self._start_tool(event.data["name"], event.data["arguments"])
        elif t == "tool_execution_end":
            self._end_tool(event.data["content"], event.data.get("pruned"), event.data.get("full_output_path"))
        elif t == "steer":
            self._add_notice(f"↳ steer  {event.data.get('content')}", "warn")
        elif t == "error":
            self._add_notice(f"模型出错: {event.data.get('message')}", "error")
        elif t == "context_check":
            self._update_status()
        elif t == "compaction_end":
            d = event.data
            if d.get("success"):
                self._add_notice(f"◆ 压缩完成，折叠 {d['compacted_count']} 条")


def run_tui(agent: Agent) -> None:
    """启动 textual TUI。"""
    AgentApp(agent).run()
