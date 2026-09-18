import json
from unittest.mock import patch

import httpx
import pytest
from openai import OpenAI

from ai import CommandCodeProvider, ProviderError, TextDelta, ThinkingDelta, Tool, ToolCall, ToolCallProgress


def make_provider(handler):
    client = OpenAI(
        api_key="offline-test-key", base_url=CommandCodeProvider.DEFAULT_BASE_URL, max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(handler), trust_env=False),
    )
    with patch("ai.providers.OpenAI", return_value=client) as sdk:
        provider = CommandCodeProvider("offline-test-key")
        sdk.assert_called_once_with(api_key="offline-test-key", base_url=provider.DEFAULT_BASE_URL,
                                    max_retries=0, timeout=httpx.Timeout(60.0, connect=10.0))
    return provider


def sse(deltas, reason="stop", usage=None):
    chunks = [{"choices": [{"index": 0, "delta": d, "finish_reason": None}]} for d in deltas]
    chunks.append({"choices": [{"index": 0, "delta": {}, "finish_reason": reason}]})
    if usage:
        chunks.append({"choices": [], "usage": usage})
    return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content="".join(
        "data: " + json.dumps(chunk) + "\n\n" for chunk in chunks
    ) + "data: [DONE]\n\n")


def test_goat_stream_uses_official_endpoint_and_preserves_events():
    requests = []
    def handler(request):
        requests.append(request)
        return sse([
            {"reasoning_content": "inspect"},
            {"content": "answer"},
            {"tool_calls": [{"index": 0, "id": "t1", "type": "function", "function": {"name": "read", "arguments": '{"path":'}}]},
            {"tool_calls": [{"index": 0, "function": {"arguments": '"a.py"}'}}]},
        ], "tool_calls", {"prompt_tokens": 42, "completion_tokens": 7, "total_tokens": 49})
    provider = make_provider(handler)
    try:
        messages = [{"role": "user", "content": [{"type": "text", "text": "look"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}}]}]
        events = list(provider.stream(messages, [Tool("read", "Read a file")], provider.DEFAULT_MODEL))
        assert [e for e in events if not isinstance(e, ToolCallProgress)] == [ThinkingDelta("inspect"), TextDelta("answer"), ToolCall("t1", "read", {"path": "a.py"})]
        request = requests[0]
        assert str(request.url) == "https://api.commandcode.ai/provider/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer offline-test-key"
        body = json.loads(request.content)
        assert body["messages"] == messages
        assert body["model"] == "deepseek/deepseek-v4.1-flash"
        assert body["stream"] is True
        assert body["tools"][0]["function"]["name"] == "read"
        assert provider.last_usage["prompt_tokens"] == 42
    finally:
        provider.client.close()


def test_goat_auth_error_does_not_fall_back_to_deepseek():
    urls = []
    def handler(request):
        urls.append(str(request.url))
        return httpx.Response(401, json={"error": {"message": "Invalid Command Code key"}})
    provider = make_provider(handler)
    try:
        with pytest.raises(ProviderError, match="Invalid Command Code key"):
            list(provider.stream([], [], provider.DEFAULT_MODEL))
        assert urls == ["https://api.commandcode.ai/provider/v1/chat/completions"]
    finally:
        provider.client.close()


@pytest.mark.parametrize("reason", ["length", "content_filter"])
def test_incomplete_output_reports_reason_instead_of_silent_success(reason):
    provider = make_provider(lambda request: sse([{"content": "partial"}], reason))
    try:
        with pytest.raises(ProviderError, match=reason):
            list(provider.stream([], [], provider.DEFAULT_MODEL))
    finally:
        provider.client.close()


def test_commandcode_startup_uses_cmd_key_and_longer_tool_budget(monkeypatch, tmp_path):
    from coding_agent import __main__ as cli
    from faux_provider import FauxProvider
    from test_loop import EchoTool
    script = [[ToolCall(str(i), "echo", {"text": "ok"})] for i in range(11)] + [[TextDelta("finished")]]
    provider = FauxProvider(script)
    credentials = []
    def factory(**kwargs):
        credentials.append(kwargs)
        return provider
    args = cli.parse_args(["--provider", "commandcode", "--workspace", str(tmp_path), "--new"])
    monkeypatch.setattr(cli, "CommandCodeProvider", factory)
    monkeypatch.setattr(cli, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(cli, "build_tools", lambda *a, **kw: [EchoTool()])
    agent = cli.build_agent(args, "goat-key")
    assert agent.loop.provider is provider
    # 主循环一个 provider 实例，摘要调用另起一个（避免摘要的 usage 覆盖计量锚点）
    assert credentials == [
        {"api_key": "goat-key", "base_url": CommandCodeProvider.DEFAULT_BASE_URL},
        {"api_key": "goat-key", "base_url": CommandCodeProvider.DEFAULT_BASE_URL},
    ]
    output = list(agent.prompt("do the task"))
    assert output[-1].data["text"] == "finished"
    assert len(provider.calls) == 12


def test_main_selects_commandcode_key_without_deepseek_key(monkeypatch, tmp_path):
    from coding_agent import __main__ as cli
    args = cli.parse_args(["--workspace", str(tmp_path)])
    received = []
    monkeypatch.setattr(cli, "parse_args", lambda: args)
    monkeypatch.setattr(cli, "load_dotenv", lambda *a: None)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("CMD_API_KEY", "goat-test")
    monkeypatch.setattr(cli, "_HAS_TUI", True)
    monkeypatch.setattr(cli, "build_agent", lambda args, key: received.append(key))
    monkeypatch.setattr(cli, "run_tui", lambda **kw: kw["agent_factory"]())
    cli.main()
    assert received == ["goat-test"]


# --------------------------------------------------------------------------- #
# 摘要等旁路调用的请求形状：真正无工具 + 可控采样参数
# --------------------------------------------------------------------------- #
def _recording_provider():
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return sse([{"content": "ok"}])

    return make_provider(handler), bodies


def test_empty_tools_are_omitted_from_the_wire_request():
    """摘要调用必须真的无工具：空 tools 不能以 [] 的形式出现（部分路由仍会据此发工具调用）。"""
    provider, bodies = _recording_provider()
    try:
        list(provider.stream([{"role": "user", "content": "x"}], [], provider.DEFAULT_MODEL))
        assert "tools" not in bodies[0]

        # 有工具时照旧发送
        list(provider.stream([{"role": "user", "content": "x"}], [Tool("read", "Read a file")],
                             provider.DEFAULT_MODEL))
        assert bodies[1]["tools"][0]["function"]["name"] == "read"
    finally:
        provider.client.close()


def test_stream_passes_sampling_options_only_when_given():
    provider, bodies = _recording_provider()
    try:
        list(provider.stream([], [], provider.DEFAULT_MODEL, temperature=0, max_tokens=256))
        assert bodies[0]["temperature"] == 0
        assert bodies[0]["max_tokens"] == 256

        # 不传时保持服务端默认：字段完全不出现
        list(provider.stream([], [], provider.DEFAULT_MODEL))
        assert "temperature" not in bodies[1]
        assert "max_tokens" not in bodies[1]
    finally:
        provider.client.close()


# --------------------------------------------------------------------------- #
# 压缩相关的接线与播报
# --------------------------------------------------------------------------- #
def test_summarizer_gets_its_own_provider_instance(monkeypatch, tmp_path):
    """摘要调用不能复用主循环的 provider：否则摘要的 usage 会覆盖计量锚点。"""
    from coding_agent import __main__ as cli
    from faux_provider import FauxProvider
    from test_loop import EchoTool
    made = []

    def factory(**kwargs):
        provider = FauxProvider([])
        made.append(provider)
        return provider

    args = cli.parse_args(["--workspace", str(tmp_path), "--new"])
    monkeypatch.setattr(cli, "CommandCodeProvider", factory)
    monkeypatch.setattr(cli, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(cli, "build_tools", lambda *a, **kw: [EchoTool()])
    agent = cli.build_agent(args, "key")

    assert len(made) == 2
    assert agent.loop.provider is made[0]
    # 真正被摘要器调用的是第二个实例，主循环那个一次都没被摘要碰过
    agent.compaction_engine.summarizer([{"role": "user", "content": "hi"}])
    assert len(made[1].calls) == 1
    assert made[0].calls == []


def test_cli_listener_reports_compaction_failure_and_skip(capsys):
    from agent_core import AgentEvent
    from coding_agent.__main__ import cli_listener

    cli_listener(AgentEvent("compaction_end", {
        "success": False,
        "reason": "summary too short (3 chars < 40)",
        "compacted_count": 12, "folded_messages": 12, "summary_chars": 0,
        "first_kept_entry_id": "e9",
    }))
    out = capsys.readouterr().out
    assert "压缩未生效" in out and "too short" in out

    cli_listener(AgentEvent("compaction_skip", {"reason": "nothing to fold"}))
    assert "跳过压缩" in capsys.readouterr().out

    cli_listener(AgentEvent("compaction_end", {
        "success": True, "reason": "", "compacted_count": 12, "folded_messages": 12,
        "summary_chars": 900, "first_kept_entry_id": "e9",
    }))
    ok = capsys.readouterr().out
    assert "压缩完成" in ok and "12" in ok
