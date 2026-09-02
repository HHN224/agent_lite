import pytest

from agent_core import AgentLoop, AgentTool, AgentState, ToolResult, ToolResultPruner
from ai import TextDelta, ToolCall

from faux_provider import FauxProvider


# --------------------------------------------------------------------------- #
# ToolResultPruner（单元测试）
# --------------------------------------------------------------------------- #
def test_prune_keeps_content_below_threshold():
    p = ToolResultPruner(threshold_chars=100)
    content = "short" * 10  # 50 字符 < 100
    out, pruned = p.prune(content)
    assert out == content
    assert pruned is False


def test_prune_folds_long_content_as_head_marker_tail():
    p = ToolResultPruner(threshold_chars=100, head_chars=20, tail_chars=10)
    content = "A" * 200
    out, pruned = p.prune(content)
    assert pruned is True
    assert out.startswith("A" * 20)
    assert out.endswith("A" * 10)
    assert "[... tool result middle pruned ...]" in out
    # 截断后长度必然小于原文
    assert len(out) < len(content)


def test_prune_handles_none_content():
    p = ToolResultPruner(threshold_chars=100)
    out, pruned = p.prune(None)
    assert out == ""
    assert pruned is False


# --------------------------------------------------------------------------- #
# AgentLoop 接线：工具结果经 pruner（阶段 C）
# --------------------------------------------------------------------------- #
class BigTool(AgentTool):
    """返回超大内容，触发叠加头尾。"""

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


def make_loop(script, tools=None, tool_pruner=None, **kwargs):
    provider = FauxProvider(script)
    loop = AgentLoop(
        provider=provider,
        model="faux-model",
        tools=tools or [],
        tool_pruner=tool_pruner,
        **kwargs,
    )
    return loop, provider


def test_tool_result_is_pruned_before_backfill():
    # 小阈值让 big 工具输出被折叠；回填的历史里 content 已截断
    pruner = ToolResultPruner(threshold_chars=100, head_chars=20, tail_chars=10)
    tools = [BigTool()]
    script = [
        [ToolCall(id="t1", name="big", arguments={"size": 500})],
        [TextDelta("完成")],
    ]
    loop, _ = make_loop(script, tools=tools, tool_pruner=pruner)
    messages = [{"role": "user", "content": "x"}]
    list(loop.run(messages))

    # 历史里 tool 结果的 content 应是截断后的 head+marker+tail
    tool_msg = messages[2]  # user, assistant(tool_calls), tool
    assert tool_msg["role"] == "tool"
    assert tool_msg["tool_call_id"] == "t1"  # 配对保持不变
    assert "[... tool result middle pruned ...]" in tool_msg["content"]
    assert len(tool_msg["content"]) < 500


def test_tool_execution_end_carries_pruned_flag():
    pruner = ToolResultPruner(threshold_chars=100, head_chars=20, tail_chars=10)
    tools = [BigTool()]
    script = [
        [ToolCall(id="t1", name="big", arguments={"size": 500})],
        [TextDelta("完成")],
    ]
    loop, _ = make_loop(script, tools=tools, tool_pruner=pruner)
    events = list(loop.run([{"role": "user", "content": "x"}]))

    exec_end = [e for e in events if e.type == "tool_execution_end"][0]
    assert exec_end.data["pruned"] is True


def test_no_prune_when_content_below_threshold():
    # 默认阈值大，小内容不截断
    loop, _ = make_loop(
        [[ToolCall(id="t1", name="big", arguments={"size": 50})], [TextDelta("完成")]],
        tools=[BigTool()],
    )
    messages = [{"role": "user", "content": "x"}]
    list(loop.run(messages))
    assert messages[2]["content"] == "X" * 50
    assert "middle pruned" not in messages[2]["content"]


def test_default_pruner_constructed():
    # 未显式传入 tool_pruner 时，AgentLoop 用默认 ToolResultPruner
    loop, _ = make_loop([[TextDelta("ok")]])
    assert loop.tool_pruner is not None
