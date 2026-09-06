import json

import pytest

from agent_core import (
    AgentLoop,
    AgentTool,
    AgentState,
    ToolResult,
    ToolOutputTruncator,
    ToolResultStore,
    truncate_head,
    truncate_tail,
    truncate_head_tail,
    truncate_line,
    truncate_json,
)
from ai import TextDelta, ToolCall

from faux_provider import FauxProvider


# --------------------------------------------------------------------------- #
# truncate_* 纯函数（单元测试）
# --------------------------------------------------------------------------- #
def test_truncate_head_keeps_below_threshold():
    out = truncate_head("short", 100)
    assert out == "short"


def test_truncate_head_truncates_tail():
    out = truncate_head("A" * 200, 100)
    assert out.startswith("A" * 100)
    assert "[... tool result tail truncated ...]" in out
    assert len(out) < 200


def test_truncate_tail_truncates_head():
    out = truncate_tail("A" * 200, 100)
    assert out.endswith("A" * 100)
    assert "[... tool result head truncated ...]" in out


def test_truncate_head_tail_folds_middle():
    out = truncate_head_tail("A" * 200, 100, head_chars=20, tail_chars=10)
    assert out.startswith("A" * 20)
    assert out.endswith("A" * 10)
    assert "[... tool result middle pruned ...]" in out
    assert len(out) < 200


def test_truncate_head_tail_respects_budget():
    out = truncate_head_tail("A" * 200, 100, head_chars=90, tail_chars=90)
    # head+tail 不应超过预算
    assert len(out) <= 100 + 100  # 允许 marker 占一些
    assert out.startswith("A" * 90)


def test_truncate_line_limits_lines_and_length():
    out = truncate_line("a\nb\nc\nd", max_line_chars=10, max_lines=2)
    assert out.startswith("a\nb")
    assert "lines truncated" in out


def test_truncate_line_folds_long_lines():
    out = truncate_line("x" * 100 + "\nok", max_line_chars=10)
    assert out.split("\n")[0].endswith("...")
    assert "ok" in out


def test_truncate_json_preserves_keys():
    data = json.dumps({"alpha": "v" * 200, "beta": 123, "nested": {"gamma": "z" * 50}})
    out = truncate_json(data, 100)
    assert "alpha" in out
    assert "beta" in out
    assert "gamma" in out
    assert "[... tool result middle pruned ...]" in out


def test_truncate_json_handles_non_json():
    out = truncate_json("not json " * 50, 100)
    assert "[... tool result middle pruned ...]" in out


# --------------------------------------------------------------------------- #
# ToolOutputTruncator：策略类 + 可选写盘
# --------------------------------------------------------------------------- #
def test_truncator_keeps_content_below_threshold():
    t = ToolOutputTruncator(threshold_chars=100)
    r = t.truncate("short", tool_name="bash")
    assert r.truncated is False
    assert r.content == "short"
    assert r.full_output_path is None


def test_truncator_folds_long_content():
    t = ToolOutputTruncator(threshold_chars=100, head_chars=20, tail_chars=10)
    r = t.truncate("A" * 200, tool_name="bash")
    assert r.truncated is True
    assert r.content.startswith("A" * 20)
    assert r.content.endswith("A" * 10)


def test_truncator_uses_read_strategy_for_read_tool():
    # read 用 truncate_head（保留开头）
    t = ToolOutputTruncator(threshold_chars=100)
    r = t.truncate("A" * 200, tool_name="read")
    assert r.truncated is True
    assert r.content.startswith("A" * 100)
    assert "[... tool result tail truncated ...]" in r.content


def test_truncator_writes_full_output_when_store_provided(tmp_path):
    store = ToolResultStore(tmp_path)
    t = ToolOutputTruncator(threshold_chars=100, store=store)
    r = t.truncate("A" * 500, tool_name="bash", tool_call_id="call_123", session_id="sess1")
    assert r.truncated is True
    assert r.full_output_path is not None
    assert "full output saved to" in r.content
    # 全文写盘到 <tmp>/tool-results/call_123.txt
    from pathlib import Path
    saved = Path(r.full_output_path)
    assert saved.exists()
    assert saved.read_text(encoding="utf-8") == "A" * 500


def test_truncator_does_not_write_when_no_store():
    t = ToolOutputTruncator(threshold_chars=100)
    r = t.truncate("A" * 500, tool_name="bash", tool_call_id="call_123", session_id="sess1")
    assert r.full_output_path is None
    assert "saved to" not in r.content


def test_truncator_handles_none_content():
    t = ToolOutputTruncator(threshold_chars=100)
    r = t.truncate(None)
    assert r.truncated is False
    assert r.content == ""


# --------------------------------------------------------------------------- #
# AgentLoop 接线：工具结果经 ToolOutputTruncator（Phase 2）
# --------------------------------------------------------------------------- #
class BigTool(AgentTool):
    """返回超大内容，触发截断。"""

    argument_types = {"size": int}

    def __init__(self):
        super().__init__(
            name="big",
            description="Returns a big content",
            parameters={"type": "object", "properties": {"size": {"type": "integer"}}},
            timeout=5,
            dangerous=False,
        )

    def execute(self, size: int = 500) -> ToolResult:
        return ToolResult(content="X" * size)


def make_loop(script, tools=None, truncator=None, **kwargs):
    provider = FauxProvider(script)
    loop = AgentLoop(
        provider=provider,
        model="faux-model",
        tools=tools or [],
        truncator=truncator,
        **kwargs,
    )
    return loop, provider


def test_tool_result_is_truncated_before_backfill():
    truncator = ToolOutputTruncator(threshold_chars=100, head_chars=20, tail_chars=10)
    tools = [BigTool()]
    script = [
        [ToolCall(id="t1", name="big", arguments={"size": 500})],
        [TextDelta("完成")],
    ]
    loop, _ = make_loop(script, tools=tools, truncator=truncator)
    messages = [{"role": "user", "content": "x"}]
    list(loop.run(messages))

    tool_msg = messages[2]  # user, assistant(tool_calls), tool
    assert tool_msg["role"] == "tool"
    assert tool_msg["tool_call_id"] == "t1"  # 配对保持不变
    assert "[... tool result middle pruned ...]" in tool_msg["content"]
    assert len(tool_msg["content"]) < 500


def test_tool_execution_end_carries_pruned_flag():
    truncator = ToolOutputTruncator(threshold_chars=100, head_chars=20, tail_chars=10)
    tools = [BigTool()]
    script = [
        [ToolCall(id="t1", name="big", arguments={"size": 500})],
        [TextDelta("完成")],
    ]
    loop, _ = make_loop(script, tools=tools, truncator=truncator)
    events = list(loop.run([{"role": "user", "content": "x"}]))

    exec_end = [e for e in events if e.type == "tool_execution_end"][0]
    assert exec_end.data["pruned"] is True
    assert "full_length" in exec_end.data
    assert "full_output_path" in exec_end.data  # 默认无 store，为 None


def test_no_truncate_when_content_below_threshold():
    loop, _ = make_loop(
        [[ToolCall(id="t1", name="big", arguments={"size": 50})], [TextDelta("完成")]],
        tools=[BigTool()],
    )
    messages = [{"role": "user", "content": "x"}]
    list(loop.run(messages))
    assert messages[2]["content"] == "X" * 50
    assert "middle pruned" not in messages[2]["content"]


def test_default_truncator_constructed():
    loop, _ = make_loop([[TextDelta("ok")]])
    assert loop.truncator is not None


def test_loop_writes_full_output_and_preserves_pairing(tmp_path):
    # Phase 2 端到端：loop + store + session_id -> 全文写盘 + preview 含路径 + tool_call_id 配对不变
    store = ToolResultStore(tmp_path)
    truncator = ToolOutputTruncator(threshold_chars=100, head_chars=20, tail_chars=10, store=store)
    tools = [BigTool()]
    script = [
        [ToolCall(id="t1", name="big", arguments={"size": 500})],
        [TextDelta("完成")],
    ]
    loop, _ = make_loop(script, tools=tools, truncator=truncator, session_id="sess1")
    messages = [{"role": "user", "content": "x"}]
    events = list(loop.run(messages))

    # 回填的 tool 消息：截断 + 配对保留 + preview 含路径
    tool_msg = messages[2]
    assert tool_msg["role"] == "tool"
    assert tool_msg["tool_call_id"] == "t1"
    assert "full output saved to" in tool_msg["content"]

    # 全文写盘到 <tmp>/tool-results/t1.txt
    saved = tmp_path / "tool-results" / "t1.txt"
    assert saved.exists()
    assert saved.read_text(encoding="utf-8") == "X" * 500

    # tool_execution_end 事件带 full_output_path
    exec_end = [e for e in events if e.type == "tool_execution_end"][0]
    assert exec_end.data.get("full_output_path") is not None
