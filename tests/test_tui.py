import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent_core import Agent, AgentLoop, Session
from ai import TextDelta

from coding_agent.tui import AgentApp

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
            hist = app.query_one("#history")
            text = "".join("".join(seg.text for seg in line) for line in hist.lines)
            assert "问题一" in text
            assert "回复1" in text
    asyncio.run(run())
