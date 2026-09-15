from ai import ThinkingDelta, ToolCallProgress, ToolCall
from test_commandcode_provider import make_provider, sse


def test_commandcode_reasoning_field_reaches_consumer():
    # Field names observed in a real GOAT deepseek-v4.1-flash stream.
    provider = make_provider(lambda request: sse([
        {"reasoning": "checking constraints", "reasoning_details": [{"type": "reasoning.text", "text": "checking constraints", "index": 0}]},
        {"content": "done"},
    ]))
    try:
        output = list(provider.stream([], [], provider.DEFAULT_MODEL))
        assert [e.content for e in output if isinstance(e, ThinkingDelta)] == ["checking constraints"]
    finally:
        provider.client.close()


def test_structured_reasoning_fallback_ignores_encrypted_blocks():
    provider = make_provider(lambda request: sse([
        {"reasoning_details": [{"type": "reasoning.encrypted", "data": "opaque"},
                               {"type": "reasoning.text", "text": "visible", "index": 0}]},
        {"content": "done"},
    ]))
    try:
        output = list(provider.stream([], [], provider.DEFAULT_MODEL))
        assert [e.content for e in output if isinstance(e, ThinkingDelta)] == ["visible"]
    finally:
        provider.client.close()


def test_tool_argument_progress_arrives_before_executable_call():
    provider = make_provider(lambda request: sse([
        {"tool_calls": [{"index": 0, "id": "t", "function": {"name": "write", "arguments": '{"path":'}}]},
        {"tool_calls": [{"index": 0, "function": {"arguments": '"a.txt","content":"test"}'}}]},
    ], "tool_calls"))
    try:
        output = list(provider.stream([], [], provider.DEFAULT_MODEL))
        assert isinstance(output[0], ToolCallProgress)
        assert output[0].name == "write"
        assert output[1].characters > output[0].characters
        assert isinstance(output[-1], ToolCall)
    finally:
        provider.client.close()


def test_abort_does_not_pull_another_argument_chunk():
    from agent_core import AgentLoop
    class Provider:
        closed = False
        def stream(self, *args):
            try:
                yield ToolCallProgress("write", 100)
                raise AssertionError("Abort must be checked before the next blocking read")
            finally:
                self.closed = True
    provider = Provider()
    loop = AgentLoop(provider, "test", [])
    for event in loop.run([]):
        if event.type == "tool_call_progress":
            loop.abort()
    assert loop.outcome["reason"] == "aborted"
    assert provider.closed


def test_steering_interrupts_reasoning_before_stale_tools_execute():
    from agent_core import Agent, AgentLoop, Session
    from ai import TextDelta
    class Provider:
        calls = []
        closed = False
        def stream(self, messages, *args):
            self.calls.append(list(messages))
            if len(self.calls) == 1:
                try:
                    yield ThinkingDelta("starting the old plan")
                    yield ThinkingDelta("more old planning")
                    yield ToolCall("stale", "missing", {})
                finally:
                    self.closed = True
            else:
                yield TextDelta("new task done")
    provider = Provider()
    agent = Agent(AgentLoop(provider, "test", []), Session("steer"))
    output = []
    steered = False
    for event in agent.prompt("old task"):
        output.append(event)
        if event.type == "thinking_update" and not steered:
            agent.steer("new task")
            steered = True
    assert provider.closed
    assert not any(e.type == "tool_execution_start" for e in output)
    assert provider.calls[1][-1] == {"role": "user", "content": "new task"}
    assert sum(e.type == "steer" for e in output) == 1
    assert any(e.content == "new task" for e in agent.session.entries.values())


def test_context_budget_matches_payload_without_display_only_reasoning():
    from agent_core.context_manager import TokenMeter
    from agent_core import AgentLoop
    message = {"role": "assistant", "content": [{"type": "thinking", "text": "planning " * 60000},
                                                 {"type": "text", "text": "done"}]}
    meter = TokenMeter()
    assert meter.estimate_message(message) == meter.estimate_message(AgentLoop._llm_messages([message])[0])


def test_compaction_does_not_resend_stored_reasoning_blocks():
    from agent_core import make_summarizer
    from faux_provider import FauxProvider
    from ai import TextDelta
    provider = FauxProvider([[TextDelta("summary")]])
    summarize = make_summarizer(provider, "test")
    summarize([{"role": "assistant", "content": [{"type": "thinking", "text": "PRIVATE_PLANNING"},
                                                   {"type": "text", "text": "visible answer"}]}], "")
    assert "PRIVATE_PLANNING" not in str(provider.calls[0]["messages"])
    assert "visible answer" in str(provider.calls[0]["messages"])


def test_reopening_recomputes_old_context_cache(monkeypatch, tmp_path):
    from coding_agent import __main__ as cli
    from agent_core import SessionRepository
    from faux_provider import FauxProvider
    repo = SessionRepository(tmp_path / "sessions")
    session = repo.create(name="old", system_prompt="test")
    session.append_message("assistant", [{"type": "thinking", "text": "plan " * 40000},
                                         {"type": "text", "text": "ready"}])
    session.usage = 200000
    session.new_usage = 200000
    repo.save(session)
    args = cli.parse_args(["--provider", "commandcode", "--session", session.session_id,
                           "--workspace", str(tmp_path), "--sandbox", "wsl"])
    monkeypatch.setattr(cli, "SESSIONS_DIR", repo._path(session.session_id).parent)
    monkeypatch.setattr(cli, "CommandCodeProvider", lambda **kwargs: FauxProvider([]))
    agent = cli.build_agent(args, "offline")
    assert not agent.context_manager.requires_compaction(agent.context_manager.measure(agent.session, agent.context_window))


def test_long_stream_progress_is_checkpointed(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from agent_core import Agent, AgentLoop, Session, SessionRepository
    import agent_core.agent as agent_module
    from faux_provider import FauxProvider
    from ai import TextDelta
    ticks = iter(range(0, 200, 6))
    monkeypatch.setattr(agent_module, "time", SimpleNamespace(time=lambda: 100, monotonic=lambda: next(ticks)))
    class Repo(SessionRepository):
        snapshots = []
        def save(self, session):
            self.snapshots.append(dict(session.runtime_status))
            super().save(session)
    repo = Repo(tmp_path)
    agent = Agent(AgentLoop(FauxProvider([[ThinkingDelta("plan"), ToolCallProgress("write", 99), TextDelta("done")]]), "test", []), Session("progress"), repo)
    list(agent.prompt("task"))
    assert any(s.get("thinking_characters") == 4 and s.get("phase") == "thinking_update" for s in repo.snapshots)
    assert any(s.get("tool_argument_characters") == 99 for s in repo.snapshots)
