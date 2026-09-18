"""Drive the real TUI and Agent loop without network requests."""
import asyncio
import threading
import pytest

from agent_core import Agent, AgentLoop, Session
from ai import TextDelta
from coding_agent.tui import AgentApp


def visible_text(app):
    from textual.widgets import Markdown, Static
    return "\n".join(
        str(getattr(w, "_markdown", "")) if isinstance(w, Markdown)
        else str(getattr(w.content, "markup", w.render()))
        for w in app.query(Static)
    )


def make_agent(provider):
    return Agent(loop=AgentLoop(provider, "test", []), session=Session("saved"))


def test_restored_history_including_compacted_messages_is_visible():
    async def run():
        agent = make_agent(None)
        agent.session.append_message("user", "original question")
        kept = agent.session.append_message("assistant", "original answer")
        agent.session.append_compaction("summary", kept.id)
        app = AgentApp(agent)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            text = visible_text(app)
            assert "original question" in text
            assert "original answer" in text
            assert "saved" in text
    asyncio.run(run())


def test_slow_initialization_keeps_draft_editable():
    release = threading.Event()
    def factory():
        assert release.wait(3)
        return make_agent(None)
    async def run():
        app = AgentApp(agent_factory=factory)
        async with app.run_test(size=(80, 24)) as pilot:
            try:
                await pilot.press("h", "i", "enter")
                assert app.query_one("#input").value == "hi"
                assert app._initializing
            finally:
                release.set()
            await asyncio.wait_for(app._task, 3)
            assert not app._initializing
            assert app.query_one("#input").value == "hi"
    asyncio.run(run())


def test_initialization_errors_stay_in_tui():
    def factory():
        raise SystemExit("missing session")
    async def run():
        app = AgentApp(agent_factory=factory)
        async with app.run_test() as pilot:
            await asyncio.wait_for(app._task, 3)
            await pilot.pause()
            assert "missing session" in visible_text(app)
            assert app._startup_error
    asyncio.run(run())


def test_lazy_sandbox_is_probed_once_on_first_command(tmp_path):
    from coding_agent.tui import LazyCommandRunner
    from agent_core import ToolResult
    probes, commands = [], []
    class Runner:
        mode = "isolated"
        def run(self, command, timeout=None):
            commands.append((command, timeout))
            return ToolResult("done")
    def factory():
        probes.append(True)
        return Runner()
    runner = LazyCommandRunner(factory, tmp_path, "auto")
    assert not probes
    runner.run("first", timeout=3)
    runner.run("second", timeout=4)
    assert len(probes) == 1
    assert commands == [("first", 3), ("second", 4)]
    assert runner.mode == "isolated"


def test_main_launches_tui_before_building_agent(monkeypatch, tmp_path):
    from coding_agent import __main__ as cli
    args = cli.parse_args(["--ui", "tui", "--workspace", str(tmp_path)])
    launches = []
    monkeypatch.setattr(cli, "parse_args", lambda: args)
    monkeypatch.setattr(cli, "load_dotenv", lambda *a: None)
    monkeypatch.setenv("CMD_API_KEY", "offline-test")
    monkeypatch.setattr(cli, "_HAS_TUI", True)
    monkeypatch.setattr(cli, "build_agent", lambda *a: (_ for _ in ()).throw(AssertionError("eager init")))
    monkeypatch.setattr(cli, "run_tui", lambda **kwargs: launches.append(kwargs))
    cli.main()
    assert callable(launches[0]["agent_factory"])
    assert launches[0]["workspace"] == tmp_path


def test_inline_layout_and_quit_with_input_focused(monkeypatch):
    from textual.drivers.headless_driver import HeadlessDriver
    monkeypatch.setattr(HeadlessDriver, "is_inline", property(lambda self: True))
    async def run():
        agent = make_agent(None)
        agent.session.append_message("assistant", "history\n\n" * 100)
        app = AgentApp(agent)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            assert app.is_inline
            app._handle_command("/help")
            await pilot.pause()
            for size in [(80, 24), (60, 15), (100, 30)]:
                await pilot.resize_terminal(*size)
                await pilot.pause()
                assert app.query_one("#status-row").region.bottom <= size[1]
                assert app.query_one("#input").region.y < size[1]
                assert not app.screen.show_vertical_scrollbar
            assert app.query_one("#input").cursor_blink is False
            await pilot.press("ctrl+d")
            assert app._shutdown_event.is_set()
    asyncio.run(run())


def test_unavailable_explicit_sandbox_never_executes_on_host(tmp_path):
    import pytest
    from coding_agent.tui import LazyCommandRunner
    from coding_agent.sandbox import SandboxUnavailableError
    def unavailable():
        raise SandboxUnavailableError("docker unavailable")
    runner = LazyCommandRunner(unavailable, tmp_path, "docker")
    with pytest.raises(SandboxUnavailableError):
        runner.run("must not execute")


@pytest.mark.parametrize("decision", ["/allow", "/deny", "/bypass"])
def test_permission_prompt_and_steering_share_tui_without_stdin(monkeypatch, decision):
    from agent_core import AgentTool, ToolResult
    from ai import ToolCall
    from faux_provider import FauxProvider
    calls = []
    class Tool(AgentTool):
        def __init__(self):
            super().__init__(name="danger", description="test", parameters={"type": "object", "properties": {}}, dangerous=True)
        def execute(self):
            calls.append(True)
            return ToolResult("tool worked")
    def forbidden_input(*args):
        raise AssertionError("TUI must never read stdin for permission")
    monkeypatch.setattr("builtins.input", forbidden_input)
    async def run():
        provider = FauxProvider([[ToolCall("t1", "danger", {})], [TextDelta("finished")]])
        agent = make_agent(provider)
        agent.loop = AgentLoop(provider, "test", [Tool()])
        app = AgentApp(agent)
        async with app.run_test() as pilot:
            await asyncio.wait_for(app._task, 3)
            app._start_prompt("use tool")
            for _ in range(100):
                if app._permission is not None:
                    break
                await asyncio.sleep(.01)
            assert app._permission is not None
            assert not calls
            app.query_one("#input").value = "also explain"
            await pilot.press("enter")
            app.query_one("#input").value = decision
            await pilot.press("enter")
            await asyncio.wait_for(app._task, 3)
            assert calls == ([] if decision == "/deny" else [True])
            assert any(m.get("content") == "also explain" for m in provider.calls[-1]["messages"])
            assert app._permission is None
    asyncio.run(run())


def test_bypass_startup_flag(tmp_path):
    from coding_agent.__main__ import parse_args
    args = parse_args(["--bypass", "--sandbox", "wsl", "--workspace", str(tmp_path)])
    assert args.permission_policy == "auto"
    assert args.sandbox == "wsl"
    assert parse_args(["--workspace", str(tmp_path)]).permission_policy == "ask"


@pytest.mark.parametrize("initial,restored", [("ask", "ask"), ("deny", "deny"), ("auto", "ask")])
def test_bypass_status_and_restoring_previous_policy(initial, restored):
    from agent_core.tool_executor import PermissionPolicy
    async def run():
        agent = make_agent(None)
        agent.loop.executor.permission_policy = PermissionPolicy(initial)
        app = AgentApp(agent)
        async with app.run_test() as pilot:
            await asyncio.wait_for(app._task, 3)
            app._handle_command("/bypass")
            app._handle_command("/bypass on")
            assert app._bypass_enabled
            assert "BYPASS" in str(app.query_one("#status").render())
            app._handle_command("/bypass invalid")
            assert app._bypass_enabled
            app._handle_command("/bypass off")
            assert agent.loop.executor.permission_policy == PermissionPolicy(restored)
            assert "BYPASS" not in str(app.query_one("#status").render())
    asyncio.run(run())


def test_bypass_runs_multiple_tools_then_off_requires_confirmation():
    from agent_core import AgentTool, ToolResult
    from ai import ToolCall
    from faux_provider import FauxProvider
    executed = []
    class Tool(AgentTool):
        def __init__(self):
            super().__init__(name="danger", description="test", parameters={"type": "object", "properties": {}}, dangerous=True)
        def execute(self):
            executed.append(True)
            return ToolResult("done")
    async def run():
        provider = FauxProvider([
            [ToolCall("t1", "danger", {}), ToolCall("t2", "danger", {})],
            [TextDelta("first done")],
            [ToolCall("t3", "danger", {})],
            [TextDelta("second done")],
        ])
        app = AgentApp(Agent(AgentLoop(provider, "test", [Tool()]), Session("bypass")))
        async with app.run_test() as pilot:
            await asyncio.wait_for(app._task, 3)
            app._handle_command("/bypass")
            app._start_prompt("first")
            await asyncio.wait_for(app._task, 3)
            assert len(executed) == 2
            assert app._permission is None
            app._handle_command("/bypass off")
            app._start_prompt("second")
            for _ in range(100):
                if app._permission is not None:
                    break
                await asyncio.sleep(.01)
            assert app._permission is not None
            assert len(executed) == 2
            app._handle_command("/deny")
            await asyncio.wait_for(app._task, 3)
            assert len(executed) == 2
    asyncio.run(run())


@pytest.mark.parametrize("stopping", [False, True])
def test_bypass_handles_already_queued_permission_but_respects_stop(stopping):
    from agent_core import AgentEvent
    async def run():
        app = AgentApp(make_agent(None))
        async with app.run_test():
            await asyncio.wait_for(app._task, 3)
            request = {"description": "queued", "allowed": False, "done": threading.Event()}
            app._handle_command("/bypass")
            app._stopping = stopping
            app._render_event(AgentEvent("permission_request", request))
            assert request["done"].is_set()
            assert request["allowed"] is (not stopping)
            assert app._permission is None
    asyncio.run(run())


def test_escape_stops_busy_prompt_without_switching_session():
    started, release = threading.Event(), threading.Event()
    class Provider:
        def stream(self, *args):
            started.set()
            assert release.wait(3)
            yield TextDelta("answer")
    async def run():
        app = AgentApp(make_agent(Provider()))
        async with app.run_test() as pilot:
            await asyncio.wait_for(app._task, 3)
            app._start_prompt("first")
            assert await asyncio.to_thread(started.wait, 2)
            try:
                app.query_one("#input").value = "/clear"
                await pilot.press("enter")
                assert app._busy
                await pilot.press("escape")
                assert app._stopping
                assert app.agent.loop._aborted
            finally:
                release.set()
            await asyncio.wait_for(app._task, 3)
            assert not app._busy
            assert app.agent.session.message_count == 1
    asyncio.run(run())


def test_resume_and_older_keep_history_order_and_new_session_separate(tmp_path):
    from agent_core import SessionRepository
    async def run():
        agent = make_agent(None)
        repo = SessionRepository(tmp_path)
        agent.repo = repo
        old = repo.create(name="old")
        for i in range(205):
            old.append_message("user", f"history-{i:03}")
        repo.save(old)
        app = AgentApp(agent)
        async with app.run_test() as pilot:
            app._handle_command(f"/resume {old.session_id}")
            await asyncio.wait_for(app._task, 3)
            await pilot.pause()
            text = visible_text(app)
            assert "history-204" in text and "history-000" not in text
            await app._load_older()
            await app._load_older()
            text = visible_text(app)
            positions = [text.index(f"history-{i:03}") for i in range(205)]
            assert positions == sorted(positions)
            app._handle_command("/new")
            await asyncio.wait_for(app._task, 3)
            await pilot.pause()
            assert "history-204" not in visible_text(app)
            assert app.agent.session_id != old.session_id
            assert app.agent.loop.session_id == app.agent.session_id
    asyncio.run(run())


def test_session_switch_keeps_tool_output_spill_inside_the_workspace(tmp_path):
    """切会话后重新接线 truncator：写盘位置必须仍与文件工具的 workspace 一致。"""
    from pathlib import Path
    from agent_core import SessionRepository
    from coding_agent.tools import ReadTool

    async def run():
        agent = make_agent(None)
        agent.loop.tools = [ReadTool(tmp_path)]
        agent.repo = SessionRepository(tmp_path / "sessions")
        app = AgentApp(agent, workspace=str(tmp_path))
        async with app.run_test() as pilot:
            app._handle_command("/new")
            await asyncio.wait_for(app._task, 3)
            await pilot.pause()
            store = agent.loop.truncator.store
            assert store is not None
            assert Path(store.base_dir).resolve().is_relative_to(tmp_path.resolve())
            assert store.workspace == tmp_path.resolve()
            assert agent.loop.session_id == agent.session.session_id
    asyncio.run(run())


def test_tui_reports_compaction_failure_and_skip():
    """压缩失败/跳过必须在 TUI 里可见：以前只显示成功，用户以为压缩成功了。"""
    from agent_core import AgentEvent

    def notice_text(app):
        parts = []
        for widget in app.query(".notice"):
            content = getattr(widget, "content", None)
            parts.append(getattr(content, "plain", None) or str(widget.render()))
        return "\n".join(parts)

    async def run():
        app = AgentApp(make_agent(None))
        async with app.run_test() as pilot:
            app._put_event(AgentEvent("compaction_end", {
                "success": False, "reason": "summary too short (3 chars < 40)",
                "compacted_count": 0, "folded_messages": 0, "summary_chars": 0,
            }))
            app._put_event(AgentEvent("compaction_skip", {"reason": "nothing to fold"}))
            app._put_event(AgentEvent("compaction_paused", {
                "reason": "压缩连续失败 3 次（最后原因：Request timed out.），已停止自动压缩",
                "consecutive_failures": 3, "disabled": True,
            }))
            app._put_event(AgentEvent("compaction_end", {
                "success": True, "reason": "", "compacted_count": 12,
                "folded_messages": 12, "summary_chars": 900, "first_kept_entry_id": "e9",
            }))
            app._drain_events()
            await app._flush_cards()
            await pilot.pause()
            text = notice_text(app)
            assert "压缩未生效" in text and "too short" in text
            assert "跳过压缩" in text and "nothing to fold" in text
            # 压缩彻底失败时要有明确的停手提示，而不是每轮刷屏
            assert "压缩已停手" in text and "Request timed out." in text
            assert "压缩完成，折叠 12 条消息" in text
    asyncio.run(run())


def test_long_reply_keeps_composer_inside_terminal():
    class Provider:
        def stream(self, *args):
            yield TextDelta("long reply\n" * 150)
    async def run():
        app = AgentApp(make_agent(Provider()))
        async with app.run_test(size=(80, 24)) as pilot:
            await asyncio.wait_for(app._task, 3)
            app._start_prompt("hello")
            await asyncio.wait_for(app._task, 4)
            await pilot.pause()
            region = app.query_one("#input").region
            assert 0 <= region.y < app.size.height
            assert region.height == 1
    asyncio.run(run())


def test_steer_during_final_response_is_not_stranded():
    started, release = threading.Event(), threading.Event()
    class Provider:
        calls = []
        def stream(self, messages, *args):
            self.calls.append(list(messages))
            if len(self.calls) == 1:
                started.set()
                assert release.wait(3)
            yield TextDelta("answer")
    async def run():
        provider = Provider()
        app = AgentApp(make_agent(provider))
        async with app.run_test() as pilot:
            await asyncio.wait_for(app._task, 3)
            app._start_prompt("first")
            assert await asyncio.to_thread(started.wait, 2)
            try:
                app.query_one("#input").value = "change direction"
                await pilot.press("enter")
            finally:
                release.set()
            await asyncio.wait_for(app._task, 4)
            assert len(provider.calls) == 2
            assert any(m.get("content") == "change direction" for m in provider.calls[-1])
            assert not app.agent.steer_queue
    asyncio.run(run())


def test_token_burst_does_not_block_producer_on_each_paint():
    finished = threading.Event()
    class Provider:
        def stream(self, *args):
            for _ in range(2000):
                yield TextDelta("x")
            finished.set()
    async def run():
        app = AgentApp(make_agent(Provider()))
        async with app.run_test() as pilot:
            await asyncio.wait_for(app._task, 3)
            app._start_prompt("burst")
            try:
                assert await asyncio.to_thread(finished.wait, 1), "UI throttles every token"
            finally:
                await asyncio.wait_for(app._task, 15)
            await pilot.pause()
            assert "x" * 2000 in visible_text(app)
    asyncio.run(run())
