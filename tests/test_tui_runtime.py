import asyncio
import os
import threading

import pytest
from textual import events
from agent_core import AgentEvent
from coding_agent import tui
from test_tui_regressions import make_agent, visible_text


def test_mouse_scroll_while_streaming_does_not_snap_back():
    async def run():
        app = tui.AgentApp(make_agent(None))
        async with app.run_test(size=(80, 24)) as pilot:
            await app._task
            app._busy = True
            app._render_event(AgentEvent("message_update", {"content": "line\n" * 100}))
            app._drain_events()
            await pilot.pause()
            scroll = app.query_one("#scroll")
            scroll.scroll_end(animate=False, immediate=True)
            await pilot.pause()
            scroll.post_message(events.MouseScrollUp(scroll, 2, 2, 0, -1, 0, False, False, False))
            await pilot.pause()
            position = scroll.scroll_y
            assert position < scroll.max_scroll_y
            app._render_event(AgentEvent("message_update", {"content": "more\n" * 5}))
            app._drain_events()
            await pilot.pause()
            assert scroll.scroll_y == position
    asyncio.run(run())


def test_thinking_is_visible_and_survives_answer_and_history_reload():
    async def run():
        agent = make_agent(None)
        app = tui.AgentApp(agent)
        async with app.run_test() as pilot:
            await app._task
            app._render_event(AgentEvent("thinking_update", {"content": "checking the code"}))
            app._drain_events()
            await pilot.pause()
            assert "checking the code" in visible_text(app)
            app._render_event(AgentEvent("thinking_end"))
            app._render_event(AgentEvent("message_update", {"content": "the answer"}))
            app._render_event(AgentEvent("message_end"))
            await pilot.pause()
            assert "checking the code" in visible_text(app)
            agent.session.append_message("assistant", [{"type": "thinking", "text": "checking the code"}, {"type": "text", "text": "the answer"}])
            await app._show_session()
            assert "checking the code" in visible_text(app)
    asyncio.run(run())


def test_running_status_shows_round_without_limit():
    async def run():
        app = tui.AgentApp(make_agent(None))
        async with app.run_test() as pilot:
            await app._task
            app._busy = True
            app._round = 125
            app._update_status()
            await pilot.pause()
            assert "第125轮" in visible_text(app)
    asyncio.run(run())


def test_long_reasoning_keeps_completed_chunks_in_order():
    async def run():
        app = tui.AgentApp(make_agent(None))
        async with app.run_test() as pilot:
            await app._task
            reasoning = "reasoning text\n" * 900
            for start in range(0, len(reasoning), 57):
                app._render_event(AgentEvent("thinking_update", {"content": reasoning[start:start + 57]}))
                app._drain_events()
            app._render_event(AgentEvent("thinking_end"))
            await pilot.pause()
            chunks = [widget.content.plain.removeprefix("✦ ") for widget in app.query(".thinking")]
            assert "".join(chunks) == reasoning
            assert all(len(chunk) <= 4000 for chunk in chunks)
    asyncio.run(run())


@pytest.mark.skipif(os.name != "nt", reason="Windows console records")
def test_windows_cpr_escape_is_preserved_in_console_records(monkeypatch):
    from textual.drivers import win32
    stop = threading.Event()
    received = []
    def read(handle, records, size, count):
        records = records._obj
        for i, char in enumerate("\x1b[2;1R"):
            records[i].EventType = 1
            key = records[i].Event.KeyEvent
            key.bKeyDown = 1
            key.wRepeatCount = 1
            key.uChar.UnicodeChar = char
            key.dwControlKeyState = 8 if char == "\x1b" else 0
        count._obj.value = 6
        stop.set()
        return 1
    monkeypatch.setattr(win32, "GetStdHandle", lambda *a: 1)
    monkeypatch.setattr(win32, "wait_for_handles", lambda *a: 1)
    monkeypatch.setattr(win32.KERNEL32, "ReadConsoleInputW", read)
    monitor_type = getattr(tui, "_WindowsInputMonitor", win32.EventMonitor)
    monitor_type(None, tui.AgentApp(make_agent(None)), stop, received.append).run()
    assert any(isinstance(event, events.CursorPosition) for event in received)
    assert not any(isinstance(event, events.Key) for event in received)
