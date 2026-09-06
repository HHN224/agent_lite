import pytest

from agent_core import (
    AgentLoop,
    AgentTool,
    AgentState,
    Session,
    SessionRepository,
    ToolResult,
    content_to_text,
    is_content_blocks,
    text_block,
)
from ai import TextDelta, ThinkingDelta, ToolCall

from faux_provider import FauxProvider


def make_loop(script, tools=None, **kwargs):
    provider = FauxProvider(script)
    loop = AgentLoop(
        provider=provider,
        model="faux-model",
        tools=tools or [],
        **kwargs,
    )
    return loop, provider


def test_thinking_events_emitted():
    loop, _ = make_loop([
        [ThinkingDelta("思考中"), TextDelta("最终答案")],
    ])
    events = list(loop.run([{"role": "user", "content": "x"}]))
    types = [e.type for e in events]
    assert "thinking_start" in types
    assert "thinking_update" in types
    assert "thinking_end" in types
    assert "message_start" in types


def test_thinking_not_in_final_text():
    loop, _ = make_loop([
        [ThinkingDelta("思考中"), TextDelta("最终答案")],
    ])
    events = list(loop.run([{"role": "user", "content": "x"}]))
    # 事件里的 message_update 只含最终文本（不含 thinking）
    text_contents = [e.data.get("content") for e in events if e.type == "message_update"]
    assert any("最终答案" in c for c in text_contents)
    assert not any("思考中" in c for c in text_contents)


def test_thinking_stored_as_block_in_history():
    loop, _ = make_loop([
        [ThinkingDelta("思考中"), TextDelta("最终答案")],
    ])
    messages = [{"role": "user", "content": "x"}]
    list(loop.run(messages))
    # 用户 + assistant(content 块含 thinking + text)
    assistant = messages[1]
    assert assistant["role"] == "assistant"
    assert is_content_blocks(assistant["content"])
    assert assistant["content"][0]["type"] == "thinking"
    assert assistant["content"][1]["type"] == "text"
    assert assistant["content"][1]["text"] == "最终答案"


def test_thinking_with_tool_call_kept_in_history():
    class EchoTool(AgentTool):
        argument_types = {"text": str}

        def __init__(self):
            super().__init__(
                name="echo",
                description="Echo",
                parameters={"type": "object", "properties": {"text": {"type": "string"}}},
                timeout=5,
                dangerous=False,
            )

        def execute(self, text: str) -> ToolResult:
            return ToolResult(content=f"echo:{text}")

    loop, _ = make_loop(
        [
            [ThinkingDelta("思考中"), ToolCall(id="t1", name="echo", arguments={"text": "hi"})],
            [TextDelta("完成")],
        ],
        tools=[EchoTool()],
    )
    messages = [{"role": "user", "content": "x"}]
    list(loop.run(messages))
    assistant_tool = messages[1]
    assert assistant_tool["role"] == "assistant"
    assert is_content_blocks(assistant_tool["content"])
    assert assistant_tool["content"][0]["type"] == "thinking"


def test_unresolved_tool_call_recovery_synthetic_message():
    # 构造一个会话：assistant 发起 tool_call，但没有对应 tool 结果（模拟中断恢复）
    s = Session(session_id="s1", system_prompt="sys")
    s.record_turn([
        {"role": "user", "content": "run ls"},
    ])
    s.record_turn([
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "bash", "arguments": "{\"command\": \"ls\"}"}}
        ]},
    ])
    payload = s.build_llm_payload()
    # 第一条是 system；第二条应是合成的恢复提示消息
    assert payload[0]["role"] == "system"
    assert payload[1]["role"] == "user"
    text = payload[1]["content"]
    assert "SYSTEM NOTE" in text
    assert "call_1" in text
    assert "unknown" in text