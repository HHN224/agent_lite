import asyncio
import io
import os

import httpx
import pytest
from textual import events
from textual._xterm_parser import XTermParser

from agent_core import Agent, AgentLoop, Session
from ai import ProviderError
from coding_agent import tui
from test_commandcode_provider import make_provider, sse
from test_loop import EchoTool
from test_tui_regressions import make_agent


@pytest.mark.skipif(os.name != "nt", reason="Windows console driver")
def test_render_does_not_inject_fragmented_terminal_replies(monkeypatch):
    # A terminal replies to DSR over stdin. If ESC arrives before the remaining
    # bytes, Textual's escape timeout turns the reply into Esc + literal text.
    output = io.StringIO()
    monkeypatch.setattr(tui.win32, "enable_application_mode", lambda: lambda: None)
    monkeypatch.setattr(tui._WindowsInputMonitor, "start", lambda self: None)
    async def run():
        driver = tui._WindowsInlineDriver(tui.AgentApp(make_agent(None)))
        driver._file = output
        driver.start_application_mode()
        driver.write("frame\r\x1b[6n")
        driver._writer_thread.stop()
    asyncio.run(run())
    parser = XTermParser()
    received = []
    for _ in range(output.getvalue().count("\x1b[6n")):
        received.extend(parser.feed("\x1b"))
        parser._timeout_time = 0
        received.extend(parser.tick())
        received.extend(parser.feed("[2;1R"))
    assert not [e for e in received if isinstance(e, events.Key)]
    # Text that a user actually types must still be accepted verbatim.
    assert "".join(e.character or "" for e in XTermParser().feed("[2;1R")) == "[2;1R"


def test_stream_eof_cannot_be_reported_as_success():
    provider = make_provider(lambda r: httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content='data: {"choices":[{"index":0,"delta":{"content":"partial"}}]}\n\n'))
    try:
        with pytest.raises(ProviderError):
            list(provider.stream([], [], "test"))
    finally:
        provider.client.close()


def test_socket_error_during_stream_is_classified_for_recovery():
    class BrokenStream(httpx.SyncByteStream):
        def __iter__(self):
            yield b'data: {"choices":[{"index":0,"delta":{"content":"partial"}}]}\n\n'
            raise httpx.ReadError("connection reset")
    provider = make_provider(lambda r: httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=BrokenStream()))
    try:
        with pytest.raises(ProviderError) as caught:
            list(provider.stream([], [], "test"))
        assert caught.value.retryable
        assert caught.value.code == "stream_transport"
    finally:
        provider.client.close()


@pytest.mark.parametrize("arguments, reason", [('{"text":', "tool_calls"), ('[]', "tool_calls"), ('{"text":"ok"}', "length")])
def test_invalid_tool_response_is_retried_with_feedback_and_not_executed(arguments, reason):
    replies = [sse([{"tool_calls": [{"index": 0, "id": "t", "function": {"name": "echo", "arguments": arguments}}]}], reason),
               sse([{"content": "done"}])]
    requests = []
    def handler(request):
        import json
        requests.append(json.loads(request.content))
        return replies.pop(0)
    provider = make_provider(handler)
    loop = AgentLoop(provider, "test", [EchoTool()], retry_delay=0)
    try:
        output = list(loop.run([]))
        assert not any(e.type == "tool_execution_start" for e in output)
        assert requests[1]["messages"][-1]["content"].startswith("[Runtime recovery]")
        assert loop.outcome["reason"] == "completed"
    finally:
        provider.client.close()


def test_transient_failure_after_tool_recovers_without_reexecuting_tool():
    replies = [
        sse([{"tool_calls": [{"index": 0, "id": "t1", "function": {"name": "echo", "arguments": '{"text":"ok"}'}}]}], "tool_calls"),
        httpx.Response(503, json={"error": {"message": "temporarily unavailable"}}),
        sse([{"content": "finished"}]),
    ]
    provider = make_provider(lambda r: replies.pop(0))
    agent = Agent(AgentLoop(provider, "test", [EchoTool()]), Session("recovery"))
    try:
        output = list(agent.prompt("work"))
        assert output[-1].data["text"] == "finished"
        assert sum(e.type == "tool_execution_start" for e in output) == 1
        assert any(e.type == "retry" for e in output)
    finally:
        provider.client.close()


@pytest.mark.parametrize("status, attempts", [(401, 1), (400, 1), (429, 3), (503, 3)])
def test_failures_are_bounded_and_reason_survives_reload(tmp_path, status, attempts):
    from agent_core import SessionRepository
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, json={"error": {"message": "service failure"}})
    provider = make_provider(handler)
    repo = SessionRepository(tmp_path)
    agent = Agent(AgentLoop(provider, "test", [], retry_delay=0), Session("failure"), repo)
    try:
        output = list(agent.prompt("work"))
        assert len(calls) == attempts
        assert output[-1].data["reason"] == "error"
        loaded = repo.load("failure")
        assert loaded.runtime_status["code"] == f"http_{status}"
        assert "service failure" in loaded.runtime_status["message"]
        assert "service failure" not in str(loaded.build_llm_payload())
    finally:
        provider.client.close()


def test_cancel_during_retry_does_not_make_another_request():
    from faux_provider import FauxProvider
    provider = FauxProvider([ProviderError("offline", retryable=True)])
    loop = AgentLoop(provider, "test", [], retry_delay=30)
    for event in loop.run([]):
        if event.type == "retry":
            loop.abort()
    assert len(provider.calls) == 1
    assert loop.outcome["reason"] == "aborted"


def test_empty_response_is_retried_and_never_silently_completes():
    from faux_provider import FauxProvider
    provider = FauxProvider([[], [], []])
    loop = AgentLoop(provider, "test", [], retry_delay=0)
    list(loop.run([]))
    assert len(provider.calls) == 3
    assert loop.outcome["code"] == "empty_response"


def test_failed_stream_closes_ui_blocks_and_discards_unexecuted_calls():
    from ai import TextDelta, ThinkingDelta, ToolCall
    class BrokenProvider:
        calls = 0
        def stream(self, *args):
            self.calls += 1
            if self.calls == 1:
                yield ThinkingDelta("planning")
                yield TextDelta("partial")
                yield ToolCall("bad", "echo", {"text": "never run"})
                raise ProviderError("stream interrupted", retryable=True)
            yield TextDelta("done")
    loop = AgentLoop(BrokenProvider(), "test", [EchoTool()], retry_delay=0)
    messages = []
    output = list(loop.run(messages))
    types = [e.type for e in output]
    assert types.index("thinking_end") < types.index("retry")
    assert types.index("message_end") < types.index("retry")
    assert "tool_execution_start" not in types
    assert messages == [{"role": "assistant", "content": "done"}]


def test_cancel_between_tools_keeps_history_valid_for_continue():
    from ai import ToolCall, TextDelta
    from faux_provider import FauxProvider
    provider = FauxProvider([[ToolCall("a", "echo", {"text": "a"}), ToolCall("b", "echo", {"text": "b"})], [TextDelta("done")]])
    loop = AgentLoop(provider, "test", [EchoTool()])
    messages = []
    for event in loop.run(messages):
        if event.type == "tool_execution_end":
            loop.abort()
    results = [m for m in messages if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in results] == ["a", "b"]
    assert "did not run" in results[1]["content"]
    list(loop.run(messages))
    assert loop.outcome["reason"] == "completed"


def test_completed_round_is_saved_before_next_model_request(tmp_path):
    from agent_core import SessionRepository
    from ai import TextDelta, ToolCall
    from faux_provider import FauxProvider
    repo = SessionRepository(tmp_path)
    provider = FauxProvider([[ToolCall("t", "echo", {"text": "ok"})], [TextDelta("done")]])
    agent = Agent(AgentLoop(provider, "test", [EchoTool()]), Session("checkpoint"), repo)
    for event in agent.prompt("work"):
        if event.type == "turn_end" and len(provider.calls) == 1:
            saved = repo.load("checkpoint")
            assert saved.build_llm_payload()[-1]["role"] == "tool"
            assert saved.runtime_status["reason"] == "running"
    assert repo.load("checkpoint").message_count == 4


def test_preparation_failure_is_saved_for_reopening(tmp_path, monkeypatch):
    from agent_core import SessionRepository
    from faux_provider import FauxProvider
    repo = SessionRepository(tmp_path)
    agent = Agent(AgentLoop(FauxProvider([]), "test", []), Session("preparation"), repo)
    def fail():
        raise RuntimeError("context preparation failed")
    monkeypatch.setattr(agent, "_context_check_event", fail)
    with pytest.raises(RuntimeError):
        list(agent.prompt("work"))
    saved = repo.load("preparation")
    assert saved.runtime_status["reason"] == "error"
    assert saved.runtime_status["message"] == "context preparation failed"


def test_wsl_environment_is_available_in_tool_schema_without_starting_sandbox(tmp_path):
    from coding_agent.tools import BashTool
    def unexpected_probe():
        raise AssertionError("schema generation must not launch a subprocess")
    runner = tui.LazyCommandRunner(unexpected_probe, tmp_path, "wsl")
    description = BashTool(tmp_path, runner).to_schema()["function"]["description"]
    assert "无网络" in description and "/workspace" in description
    assert "Node" in description and "/mnt" in description
    # 必须给模型真实工作目录，而不是只给一个容器内别名（问题 3）
    assert str(tmp_path.resolve()) in description
    assert "container-side alias only" in description


def test_file_tools_advertise_the_real_workspace_root(tmp_path):
    from coding_agent.tools import build_tools
    root = str(tmp_path.resolve())
    for name in ("read", "write", "edit", "grep", "ls", "find"):
        schema = {t.name: t for t in build_tools(tmp_path)}[name].to_schema()
        description = schema["function"]["description"]
        assert root in description, name
        assert "/workspace" not in description, name


def test_bash_schema_tells_the_model_it_is_offline_and_how_to_get_deps(tmp_path):
    """模型必须知道沙箱没网、并且知道正确的取件入口 —— 否则它会一直 curl / pip install 试。"""
    from coding_agent.sandbox import WslRunner
    from coding_agent.tools import BashTool

    description = BashTool(tmp_path, WslRunner(tmp_path)).to_schema()["function"]["description"]
    assert "无网络出网" in description
    assert "install 工具" in description and "fetch 工具" in description
    assert "不要反复尝试 curl/pip install" in description


def test_install_and_fetch_descriptions_list_the_allowlist(tmp_path):
    from coding_agent.fetch import allowed_hosts
    from coding_agent.tools import build_tools

    tools = {t.name: t for t in build_tools(tmp_path, allowed_hosts=["example.com"])}
    for name in ("install", "fetch"):
        description = tools[name].to_schema()["function"]["description"]
        assert "example.com" in description
        for host in allowed_hosts(["example.com"]):
            assert host in description, (name, host)


def test_resumed_session_displays_last_failure():
    from test_tui_regressions import visible_text
    async def run():
        agent = make_agent(None)
        agent.session.runtime_status = {"reason": "error", "code": "http_503", "message": "service unavailable"}
        app = tui.AgentApp(agent)
        async with app.run_test() as pilot:
            await app._task
            await pilot.pause()
            assert "http_503" in visible_text(app)
            assert "service unavailable" in visible_text(app)
    asyncio.run(run())
