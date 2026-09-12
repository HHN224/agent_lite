import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent_core import Agent, AgentLoop, Session
from ai import TextDelta

from coding_agent.tui import AgentApp, run_tui

from faux_provider import FauxProvider


def _make_agent(script):
    provider = FauxProvider(script)
    loop = AgentLoop(provider=provider, model="m", tools=[])
    session = Session(session_id="s1", system_prompt="sys")
    return Agent(loop=loop, session=session)


def test_tui_sends_message_and_resets_busy():
    async def run():
        agent = _make_agent([[TextDelta("回复1")]])
        app = AgentApp(agent)
        async with app.run_test(size=(100, 30)) as pilot:
            app.query_one("#input").value = "问题一"
            await pilot.press("enter")
            await asyncio.sleep(0.3)
            assert app._busy is False
            assert agent.session.message_count >= 2
            from textual.widgets import Markdown, Static
            from rich.console import Console
            scroll = app.query_one("#scroll")
            parts = []
            for w in scroll.children:
                if isinstance(w, Markdown):
                    parts.append(str(getattr(w, "_markdown", "")))
                elif isinstance(w, Static):
                    parts.append(str(w.render()))
            joined = "".join(parts)
            assert "问题一" in joined
            assert "回复1" in joined
    asyncio.run(run())


def test_run_tui_stays_in_terminal_buffer(monkeypatch):
    """TUI 必须内联运行，并在退出后保留终端 transcript。"""
    run_options = {}

    def fake_run(self, **kwargs):
        run_options.update(kwargs)

    monkeypatch.setattr(AgentApp, "run", fake_run)
    run_tui(object())

    assert run_options == {"inline": True, "inline_no_clear": True}


def test_windows_inline_mode_uses_inline_driver():
    """Textual 默认忽略 Windows 的 inline=True；TUI 必须补上该 adapter。"""
    if os.name != "nt":
        return

    async def build_driver():
        app = AgentApp(object())
        driver = app._build_driver(
            headless=False,
            inline=True,
            mouse=False,
            size=(80, 24),
        )
        try:
            assert driver.is_inline is True
            writes = []

            class Writer:
                def write(self, data):
                    writes.append(data)

                def stop(self):
                    pass

            driver._writer_thread = Writer()
            driver.write("before\x1b[?1049hinside\x1b[?1049lafter")
            assert writes == ["beforeinsideafter"]
        finally:
            driver.close()

    asyncio.run(build_driver())


def test_transcript_height_is_content_driven_for_inline_mode():
    """内联 Screen 高度由内容决定，transcript 不能使用会坍缩的 fr 高度。"""
    async def check_layout():
        app = AgentApp(_make_agent([]))
        async with app.run_test(size=(100, 30)):
            height = app.query_one("#scroll").styles.height
            assert height is None or height.is_auto

    asyncio.run(check_layout())
