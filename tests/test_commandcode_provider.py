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
    assert credentials == [{"api_key": "goat-key", "base_url": CommandCodeProvider.DEFAULT_BASE_URL}]
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
