"""textual TUI：用事件流渲染 agent，支持流式显示、运行中 steer、/image 喂图。

消费 Agent.prompt() 产出的事件（agent_start / turn_start / message_update /
thinking_update / tool_execution_* / agent_end），映射到界面。核心复用事件流，
一个核心多种前端（CLI 的 cli_listener、TUI 的 tui 渲染）。

运行方式：agent-lite --ui tui（默认）。也可 python -m coding_agent.tui（需传入组装好的 agent）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from textual.app import App, ComposeResult
from textual.widgets import Footer, Header, Input, RichLog

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
    """textual 应用：消息 RichLog + 底部 Input。"""

    TITLE = "agent-lite"
    SUB_TITLE = "textual UI"

    CSS = """
    Screen { layout: vertical; }
    #log { height: 1fr; }
    #input { dock: bottom; height: 3; }
    """

    def __init__(self, agent: Agent, **kwargs):
        super().__init__(**kwargs)
        self.agent = agent
        self._running = False
        self._task: asyncio.Task | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield RichLog(id="log", markup=True, wrap=True, highlight=True)
        yield Input(id="input", placeholder="输入消息，回车发送；运行中输入可 steer")
        yield Footer()

    def on_mount(self):
        self.query_one("#input", Input).focus()
        self._log("欢迎使用 agent-lite（textual UI）。输入 /help 查看命令。")

    def _log(self, text: str, *args):
        self.query_one("#log", RichLog).write(text, *args)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        if not text:
            return
        if text.startswith("/"):
            self._handle_command(text)
            return
        if self._running:
            self.agent.steer(text)
            self._log(f"\n[bold yellow]【steer】[/bold yellow]{text}")
            return
        self._start_prompt(text)

    def _handle_command(self, text: str):
        if text == "/help":
            self._log("/image <path> 喂图\n/clear 清空历史\n/compact 手动压缩")
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
        if self._running:
            self._log(">>> 正在运行，请稍候")
            return
        self._running = True
        self._log(f"[bold cyan]你:[/bold cyan] {text}")
        self._task = asyncio.create_task(self._run_prompt(text, images))

    async def _run_prompt(self, text: str, images: list | None = None):
        agent = self.agent
        loop = asyncio.get_running_loop()
        try:
            def consume():
                for event in agent.prompt(text, images=images):
                    loop.call_soon_threadsafe(self._render_event_safe, event)
            await asyncio.to_thread(consume)
        except Exception as e:
            self.call_from_thread(self._log, f"\n[red]>>> 出错:[/red] {e}")
        finally:
            self._running = False

    def _render_event_safe(self, event: AgentEvent):
        self._render_event(event)

    def _render_event(self, event: AgentEvent):
        t = event.type
        if t == "agent_start":
            self._log("")
        elif t == "turn_start":
            self._log("[dim]>>> 调用模型...[/dim]")
        elif t == "message_update":
            self._log(_content_text(event.data["content"]), end="")
        elif t == "thinking_update":
            self._log(f"[dim]{_content_text(event.data['content'])}[/dim]", end="")
        elif t == "tool_execution_start":
            self._log(f"\n[bold magenta]▶ 工具:[/bold magenta] {event.data['name']} {event.data['arguments']}")
        elif t == "tool_execution_end":
            suffix = "（已截断）" if event.data.get("pruned") else ""
            fp = event.data.get("full_output_path")
            if fp:
                suffix += f" —— 全文: {fp}"
            self._log(f"[bold magenta]← 返回:[/bold magenta] {_content_text(event.data['content'])[:200]}{suffix}")
        elif t == "steer":
            self._log(f"[bold yellow]【插话】[/bold yellow] {event.data.get('content')}")
        elif t == "error":
            self._log(f"[red]>>> 模型出错:[/red] {event.data.get('message')}")
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
