"""textual TUI：用事件流渲染 agent，支持流式显示、运行中 steer、/image 喂图。

消费 Agent.prompt() 产出的事件（agent_start / turn_start / message_update /
thinking_update / tool_execution_* / agent_end），映射到界面。核心复用事件流，
一个核心多种前端（CLI 的 cli_listener、TUI 的 tui 渲染）。

运行方式：agent-lite --ui tui（默认）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from textual.app import App, ComposeResult
from textual.widgets import Footer, Header, Input, Markdown, RichLog

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
    """textual 应用：历史 RichLog + 流式 Markdown + 底部 Input。"""

    TITLE = "agent-lite"
    SUB_TITLE = "textual UI"

    CSS = """
    Screen { layout: vertical; }
    #history { height: 1fr; }
    #answer { height: auto; margin: 0 1; }
    #input { dock: bottom; height: 3; }
    """

    def __init__(self, agent: Agent, **kwargs):
        super().__init__(**kwargs)
        self.agent = agent
        self._busy = False
        self._task: asyncio.Task | None = None
        self._answer_buf = []
        self._thinking_buf = []

    def compose(self) -> ComposeResult:
        yield Header()
        yield RichLog(id="history", markup=True, wrap=True, highlight=True)
        yield Markdown(id="answer")
        yield Input(id="input", placeholder="输入消息，回车发送；运行中输入可 steer")
        yield Footer()

    def on_mount(self):
        self.query_one("#input", Input).focus()
        self._log("欢迎使用 agent-lite（textual UI）。输入 /help 查看命令。")

    def _log(self, text: str):
        self.query_one("#history", RichLog).write(text)

    def _show_answer(self):
        md = self.query_one("#answer", Markdown)
        text = "".join(self._answer_buf)
        if self._thinking_buf:
            text = ("[dim]思考中…[/dim]\n" if not text else "") + text
        md.update(text)

    def _flush_answer(self):
        text = "".join(self._answer_buf)
        thinking = "".join(self._thinking_buf)
        if thinking:
            self._log(f"[dim]{thinking}[/dim]")
        if text:
            self._log(text)
        self._answer_buf = []
        self._thinking_buf = []
        self.query_one("#answer", Markdown).update("")

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
            self._log("\n[bold yellow]【steer】[/bold yellow]" + text)
            return
        self._start_prompt(text)

    def _handle_command(self, text: str):
        if text == "/help":
            self._log("\n".join([
                "/help            命令帮助",
                "/new             新建会话",
                "/mode <name>     切换模式（default/plan/code/review）",
                "/sessions        查看会话列表",
                "/image <path>    喂图",
                "/clear           清空当前会话历史",
                "/compact         手动压缩上下文",
            ]))
        elif text == "/new":
            self._new_session()
        elif text.startswith("/mode "):
            self._change_mode(text[len("/mode "):].strip())
        elif text == "/sessions":
            self._list_sessions()
        elif text == "/clear":
            self.agent.clear_history()
            self._log("\n>>> 已清空对话历史")
        elif text == "/compact":
            self._log(">>> 手动压缩...")
            try:
                for ev in self.agent.compaction_engine.compact_if_needed(
                    self.agent.session, self.agent.context_window
                ):
                    self._render_event(ev)
                self.agent._save()
            except Exception as e:
                self._log(f">>> 压缩失败: {e}")
        elif text.startswith("/image "):
            self._send_image(text[len("/image "):].strip())
        else:
            self._log(f"未知命令: {text}（输入 /help）")

    def _new_session(self):
        from coding_agent.modes import get_mode
        repo = self.agent.repo
        if repo is None:
            self._log(">>> 无会话仓库，无法新建")
            return
        session = repo.create(system_prompt=get_mode("default"))
        repo.save(session)
        self.agent.session = session
        self.agent.steer_queue = []
        self.agent.follow_up_queue = []
        self._log("\n>>> 已新建会话: " + session.session_id)

    def _change_mode(self, name: str):
        from coding_agent.modes import get_mode, mode_names
        if name not in mode_names():
            self._log(f">>> 未知 mode: {name}，可选: {', '.join(mode_names())}")
            return
        self.agent.session.system_prompt = get_mode(name)
        self.agent._save()
        self._log("\n>>> 已切换到 mode: " + name)

    def _list_sessions(self):
        repo = self.agent.repo
        if repo is None:
            self._log(">>> 无会话仓库")
            return
        metas = repo.list()
        if not metas:
            self._log(">>> 暂无会话")
            return
        for m in metas:
            mark = " *" if m.session_id == self.agent.session_id else ""
            self._log(f"   {m.session_id}  {m.name or '<未命名>':<16}  {m.message_count} 条{mark}")

    def _send_image(self, path: str):
        p = Path(path).resolve()
        if not p.is_file():
            self._log(f">>> 图片不存在: {path}")
            return
        try:
            import base64, mimetypes
            data = base64.b64encode(p.read_bytes()).decode("ascii")
            mime = mimetypes.guess_type(str(p))[0] or "image/png"
            img = image_block(data, mime)
        except Exception as e:
            self._log(f">>> 读图失败: {e}")
            return
        self._start_prompt("(图片)", images=[img])

    def _start_prompt(self, text: str, images: list | None = None):
        if self._busy:
            self._log(">>> 正在运行，请稍候")
            return
        self._busy = True
        self._log(f"[bold cyan]你:[/bold cyan] {text}")
        self._answer_buf = []
        self._thinking_buf = []
        self.query_one("#answer", Markdown).update("")
        self._task = asyncio.create_task(self._run_prompt(text, images))

    async def _run_prompt(self, text: str, images: list | None = None):
        agent = self.agent
        try:
            def consume():
                # 运行在子线程，把事件安全地调度到 App 主线程
                for event in agent.prompt(text, images=images):
                    self.call_from_thread(self._on_event, event)
            await asyncio.to_thread(consume)
        except Exception as e:
            # _run_prompt 协程运行在主线程，直接更新 UI
            self._log("\n[red]>>> 出错:[/red] " + str(e))
        finally:
            self._after_prompt()

    def _after_prompt(self):
        self._flush_answer()
        self._busy = False

    def _on_event(self, event: AgentEvent):
        try:
            self._render_event(event)
        except Exception as e:
            self._log("\n[red]渲染出错:[/red] " + str(e))

    def _render_event(self, event: AgentEvent):
        t = event.type
        if t == "agent_start":
            pass
        elif t == "turn_start":
            self._log("[dim]>>> 调用模型...[/dim]")
        elif t == "message_update":
            self._answer_buf.append(_content_text(event.data["content"]))
            self._show_answer()
        elif t == "thinking_update":
            self._thinking_buf.append(_content_text(event.data["content"]))
            self._show_answer()
        elif t == "tool_execution_start":
            self._flush_answer()
            self._log("\n[bold magenta]▶ 工具:[/bold magenta] " + event.data["name"] + " " + str(event.data["arguments"]))
        elif t == "tool_execution_end":
            fp = event.data.get("full_output_path")
            suffix = "（已截断）" if event.data.get("pruned") else ""
            if fp:
                suffix += " —— 全文: " + fp
            self._log("[bold magenta]← 返回:[/bold magenta] " + _content_text(event.data["content"])[:200] + suffix)
        elif t == "steer":
            self._log("[bold yellow]【插话】[/bold yellow] " + event.data.get("content"))
        elif t == "error":
            self._log("[red]>>> 模型出错:[/red] " + event.data.get("message"))
        elif t == "context_check":
            d = event.data
            self._log(f"[dim]上下文: {d['total_tokens']}/{d['context_window']} ({d['ratio']:.0%})[/dim]")
        elif t == "compaction_end":
            d = event.data
            if d.get("success"):
                self._log(f"[dim]压缩完成，折叠 {d['compacted_count']} 条[/dim]")


def run_tui(agent: Agent) -> None:
    """启动 textual TUI。"""
    AgentApp(agent).run()