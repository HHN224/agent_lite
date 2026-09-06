"""textual TUI（Claude Code / pi 风格）：Markdown 卡片 + 工具折叠 + 底部状态栏。

消费 Agent.prompt() 的事件流（agent_start / turn_start / message_update /
thinking_update / tool_execution_* / agent_end），把每个消息渲染成独立卡片。
放弃逐字流式，采用"整块/半块刷新"——更贴近 Claude Code / pi。

运行方式：agent-lite --ui tui（默认）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Collapsible, Footer, Header, Input, Label, Markdown, Static

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


class AgentApp(App):
    """Claude Code / pi 风格：消息卡片流 + 底部状态栏 + 输入框。"""

    TITLE = "agent-lite"
    SUB_TITLE = "textual UI"

    CSS = """
    Screen { layout: vertical; }
    #scroll { height: 1fr; padding: 0 1; }
    #status { dock: bottom; height: 1; padding: 0 1; background: $surface; color: $text-muted; }
    #input { dock: bottom; height: 3; }
    .user-card { background: $primary; color: $text; margin: 1 0; padding: 0 1; }
    .assistant-card { margin: 0 0 0 2; padding: 0 1; }
    .tool-card { margin: 0 0 0 2; }
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

    def compose(self) -> ComposeResult:
        yield Header()
        yield VerticalScroll(id="scroll")
        yield Label(id="status")
        yield Input(id="input", placeholder="输入消息，回车发送；运行中输入可 steer")
        yield Footer()

    def on_mount(self):
        self.query_one("#input", Input).focus()
        self._update_status()
        self._add_card(Static("欢迎使用 agent-lite（textual UI）。输入 /help 查看命令。", markup=True))

    def _add_card(self, widget):
        async def _m():
            scroll = self.query_one("#scroll")
            await scroll.mount(widget)
            scroll.scroll_end(animate=False)
        asyncio.create_task(_m())

    def _update_status(self):
        s = self.agent.session
        model = getattr(self.agent.loop, "model", "?")
        busy = "运行中" if self._busy else "就绪"
        msgs = s.message_count
        usage = getattr(s, "usage", 0) or 0
        window = getattr(self.agent, "context_window", 0) or 0
        self.query_one("#status", Label).update(f"[bold]{model}[/bold]  |  msg {msgs}  |  ctx {usage}/{window}  |  {busy}")

    def _add_user(self, text: str):
        self._add_card(Static(f"[bold]你:[/bold] {text}", classes="user-card", markup=True))

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
        if self._thinking_buf:
            text = ("[i][dim]思考中…[/dim][/i]\n" if not text else "") + text
        self._assistant_widget.update(text)

    def _end_assistant(self):
        self._update_assistant()
        self._assistant_widget = None

    def _start_tool(self, name: str, arguments):
        title = f"⚙ {name}  {arguments}"
        content = Static("(运行中...)", markup=True)
        self._tool_widget = Collapsible(content, title=title, classes="tool-card")
        self._add_card(self._tool_widget)

    def _end_tool(self, content_text, pruned, full_path):
        if self._tool_widget is None:
            return
        suffix = "（已截断）" if pruned else ""
        if full_path:
            suffix += f" —— 全文: {full_path}"
        body = _content_text(content_text)[:500] + suffix
        child = self._tool_widget.children[0] if self._tool_widget.children else None
        if child is not None and isinstance(child, Static):
            child.update(body)
        self._tool_widget = None

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
            self._add_card(Static(f"[bold yellow]【steer】[/bold yellow] {text}", markup=True))
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
            self._add_card(Static(f"未知命令: {text}（输入 /help）", markup=True))

    def _new_session(self):
        from coding_agent.modes import get_mode
        repo = self.agent.repo
        if repo is None:
            self._add_card(Static(">>> 无会话仓库，无法新建", markup=True))
            return
        session = repo.create(system_prompt=get_mode("default"))
        repo.save(session)
        self.agent.session = session
        self.agent.steer_queue = []
        self.agent.follow_up_queue = []
        self._add_card(Static(f">>> 已新建会话: {session.session_id}", markup=True))
        self._update_status()

    def _change_mode(self, name: str):
        from coding_agent.modes import get_mode, mode_names
        if name not in mode_names():
            self._add_card(Static(f">>> 未知 mode: {name}，可选: {", ".join(mode_names())}", markup=True))
            return
        self.agent.session.system_prompt = get_mode(name)
        self.agent._save()
        self._add_card(Static(f">>> 已切换到 mode: {name}", markup=True))
        self._update_status()

    def _list_sessions(self):
        repo = self.agent.repo
        if repo is None:
            self._add_card(Static(">>> 无会话仓库", markup=True))
            return
        metas = repo.list()
        if not metas:
            self._add_card(Static(">>> 暂无会话", markup=True))
            return
        for m in metas:
            mark = " *" if m.session_id == self.agent.session_id else ""
            self._add_card(Static(f"   {m.session_id}  {m.name or '<未命名>':<16}  {m.message_count} 条{mark}", markup=True))

    def _send_image(self, path: str):
        p = Path(path).resolve()
        if not p.is_file():
            self._add_card(Static(f">>> 图片不存在: {path}", markup=True))
            return
        try:
            import base64, mimetypes
            data = base64.b64encode(p.read_bytes()).decode("ascii")
            mime = mimetypes.guess_type(str(p))[0] or "image/png"
            img = image_block(data, mime)
        except Exception as e:
            self._add_card(Static(f">>> 读图失败: {e}", markup=True))
            return
        self._start_prompt("(图片)", images=[img])

    def _start_prompt(self, text: str, images: list | None = None):
        if self._busy:
            self._add_card(Static(">>> 正在运行，请稍候", markup=True))
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
            self._add_card(Static(f"[red]>>> 出错:[/red] {e}", markup=True))
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
            self._add_card(Static(f"[red]渲染出错:[/red] {e}", markup=True))

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
            self._add_card(Static(f"[bold yellow]【插话】[/bold yellow] {event.data.get('content')}", markup=True))
        elif t == "error":
            self._add_card(Static(f"[red]>>> 模型出错:[/red] {event.data.get('message')}", markup=True))
        elif t == "context_check":
            self._update_status()
        elif t == "compaction_end":
            d = event.data
            if d.get("success"):
                self._add_card(Static(f"[dim]压缩完成，折叠 {d['compacted_count']} 条[/dim]", markup=True))


def run_tui(agent: Agent) -> None:
    """启动 textual TUI。"""
    AgentApp(agent).run()