import json

from agent_core import AgentLoop, AgentState, AgentTool, ToolResult
from ai import ProviderError, TextDelta, ToolCall

from faux_provider import FauxProvider


class EchoTool(AgentTool):
    """测试用最小工具：把 text 参数回显。"""

    argument_types = {"text": str}

    def __init__(self):
        super().__init__(
            name="echo",
            description="Echo the text",
            parameters={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
            timeout=5,
            dangerous=False,
        )

    def execute(self, text: str) -> ToolResult:
        return ToolResult(content=f"echo:{text}")


class BoomTool(AgentTool):
    """测试用故障工具：执行时必然抛异常。"""

    argument_types = {}

    def __init__(self):
        super().__init__(
            name="boom",
            description="Always raises",
            parameters={"type": "object", "properties": {}},
            timeout=5,
            dangerous=False,
        )

    def execute(self) -> ToolResult:
        raise RuntimeError("boom!")


class DangerousTool(AgentTool):
    """测试用危险工具：受权限策略约束。"""

    argument_types = {"text": str}

    def __init__(self):
        super().__init__(
            name="risky",
            description="Dangerous echo",
            parameters={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
            timeout=5,
            dangerous=True,
        )

    def execute(self, text: str) -> ToolResult:
        return ToolResult(content=f"risky:{text}")


def make_loop(script, tools=None, max_iterations=10, **kwargs):
    provider = FauxProvider(script)
    loop = AgentLoop(
        provider=provider,
        model="faux-model",
        tools=tools or [],
        max_iterations=max_iterations,
        **kwargs,
    )
    return loop, provider


def run_all(loop, messages=None):
    messages = messages if messages is not None else []
    events = list(loop.run(messages))
    return events, messages


def event_types(events):
    return [e.type for e in events]


def test_pure_text_reply():
    loop, _ = make_loop([[TextDelta("你好，"), TextDelta("世界")]])
    events, _ = run_all(loop)

    assert event_types(events) == [
        "turn_start",
        "message_start",
        "message_update",
        "message_update",
        "message_end",
        "turn_end",
    ]
    assert loop.state == AgentState.FINISHED


def test_tool_call_then_final_reply():
    tools = [EchoTool()]
    script = [
        [ToolCall(id="t1", name="echo", arguments={"text": "hi"})],
        [TextDelta("完成")],
    ]
    loop, provider = make_loop(script, tools=tools)
    messages = [{"role": "user", "content": "请回显 hi"}]
    events, messages = run_all(loop, messages)

    # 两轮：第一轮工具调用，第二轮纯文本收尾
    assert event_types(events).count("turn_start") == 2
    assert event_types(events).count("tool_execution_start") == 1

    # 历史回填：assistant(tool_calls) → tool 结果 → 最终 assistant
    roles = [m["role"] for m in messages]
    assert roles == ["user", "assistant", "tool", "assistant"]

    assistant_tool_msg = messages[1]
    assert assistant_tool_msg["tool_calls"][0]["function"]["name"] == "echo"
    assert json.loads(assistant_tool_msg["tool_calls"][0]["function"]["arguments"]) == {"text": "hi"}
    assert messages[2]["role"] == "tool"
    assert messages[2]["content"] == "echo:hi"

    assert provider.calls[0]["model"] == "faux-model"


def test_tool_error_backfilled_to_model():
    tools = [BoomTool()]
    script = [
        [ToolCall(id="t1", name="boom", arguments={})],
        [TextDelta("已重试")],
    ]
    loop, _ = make_loop(script, tools=tools)
    messages = [{"role": "user", "content": "触发异常"}]
    _, messages = run_all(loop, messages)

    # 异常被转换成 is_error 结果回填，循环不崩溃
    assert messages[2]["role"] == "tool"
    assert "Error" in messages[2]["content"]


def test_unknown_tool_is_error():
    script = [
        [ToolCall(id="t1", name="nope", arguments={})],
        [TextDelta("收尾")],
    ]
    loop, _ = make_loop(script)
    _, messages = run_all(loop, messages=[{"role": "user", "content": "x"}])

    assert messages[2]["role"] == "tool"
    assert "unknown tool" in messages[2]["content"]


def test_provider_error_finishes_run():
    loop, _ = make_loop([ProviderError("模型 API 错误: 500")])
    events, _ = run_all(loop)

    assert event_types(events) == ["turn_start", "error", "turn_end"]
    assert loop.state == AgentState.ERROR


def test_max_iterations_cap():
    # 模型永远调工具，靠 max_iterations 终止循环
    script = [[ToolCall(id="t1", name="echo", arguments={"text": "x"})]] * 10
    loop, _ = make_loop(script, tools=[EchoTool()], max_iterations=2)
    events, _ = run_all(loop)

    assert event_types(events).count("turn_start") == 2
    assert loop.state == AgentState.FINISHED


def test_deny_policy_blocks_dangerous_tool_and_backfills():
    # deny 策略：危险工具被拒绝，拒绝结果（is_error）回填给模型，不产生副作用
    script = [
        [ToolCall(id="t1", name="risky", arguments={"text": "hi"})],
        [TextDelta("收尾")],
    ]
    loop, _ = make_loop(script, tools=[DangerousTool()], permission_policy="deny")
    _, messages = run_all(loop, messages=[{"role": "user", "content": "x"}])

    assert messages[2]["role"] == "tool"
    assert "permission denied" in messages[2]["content"]
    assert loop.executor.audit_log[0]["denied"] is True


def test_ask_policy_uses_confirm_and_auto_skips():
    # ask：confirm 返回 False → 拒绝；auto：跳过 confirm 直接执行
    script = [
        [ToolCall(id="t1", name="risky", arguments={"text": "hi"})],
        [TextDelta("收尾")],
    ]
    loop, _ = make_loop(
        script,
        tools=[DangerousTool()],
        permission_policy="ask",
        confirm=lambda desc: False,
    )
    _, messages = run_all(loop, messages=[{"role": "user", "content": "x"}])
    assert "permission denied" in messages[2]["content"]

    loop, _ = make_loop(
        script,
        tools=[DangerousTool()],
        permission_policy="auto",
        confirm=lambda desc: False,  # auto 下不应被调用
    )
    _, messages = run_all(loop, messages=[{"role": "user", "content": "x"}])
    assert messages[2]["content"] == "risky:hi"