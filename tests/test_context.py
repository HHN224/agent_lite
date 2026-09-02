import json

import pytest

from agent_core import (
    Agent,
    AgentLoop,
    ContextManager,
    ContextPressure,
    Session,
    SessionRepository,
    TokenMeter,
)
from ai import TextDelta

from faux_provider import FauxProvider


# --------------------------------------------------------------------------- #
# TokenMeter：计量 / 估算
# --------------------------------------------------------------------------- #
def test_estimate_payload_counts_system_and_messages():
    meter = TokenMeter()
    payload = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi" * 40},  # 120 chars -> 30 tokens
        {"role": "assistant", "content": "hello" * 40},
    ]
    # 每条约 4 个结构性 token；content 估算与每 4 字符 1 token 一致
    total = meter.estimate_payload(payload)
    assert total > 0
    assert meter.estimate_message(payload[0]) == meter.estimate_message(
        {"role": "system", "content": "sys"}
    )


def test_estimate_message_includes_tool_calls():
    meter = TokenMeter()
    msg = {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": "t1", "function": {"name": "read", "arguments": "{}"}}],
    }
    # 只有 tool_calls 时也应有正计数
    assert meter.estimate_message(msg) > 0


def test_measure_uses_usage_anchor_when_available():
    meter = TokenMeter()
    s = Session(session_id="abc", system_prompt="sys")
    s.append_message("user", "你好")

    # 提供真实 usage 时，直接采用 total_tokens 作锚点
    p = meter.measure(s, context_window=10000, usage={"total_tokens": 5000})
    assert p.total_tokens == 5000
    assert p.ratio == 0.5
    assert p.used_anchor is True


def test_measure_falls_back_to_heuristic_without_usage():
    meter = TokenMeter()
    s = Session(session_id="abc", system_prompt="sys")
    s.append_message("user", "你好")

    p = meter.measure(s, context_window=10000)
    assert p.used_anchor is False
    # 启发式应来自 payload（system + user 消息）
    assert p.total_tokens == meter.estimate_payload(s.build_llm_payload())
    assert p.ratio > 0


def test_measure_zero_context_window_returns_zero():
    meter = TokenMeter()
    s = Session(session_id="abc", system_prompt="sys")
    p = meter.measure(s, context_window=0)
    assert p.total_tokens == 0
    assert p.ratio == 0.0


# --------------------------------------------------------------------------- #
# ContextManager：触发判定 + 溢出检测
# --------------------------------------------------------------------------- #
def test_should_compact_true_when_at_threshold():
    mgr = ContextManager(threshold_ratio=0.8)
    s = Session(session_id="abc", system_prompt="sys")
    s.append_message("user", "x" * 100)

    # 用一个小窗口让启发式占比超过阈值
    pressure = mgr.measure(s, context_window=10)
    assert mgr.requires_compaction(pressure) is True


def test_should_compact_false_when_below_threshold():
    mgr = ContextManager(threshold_ratio=0.8)
    s = Session(session_id="abc", system_prompt="sys")
    s.append_message("user", "hi")

    pressure = mgr.measure(s, context_window=100000)
    assert mgr.requires_compaction(pressure) is False


def test_requires_compaction_respects_threshold():
    mgr = ContextManager(threshold_ratio=0.5)
    p_high = ContextPressure(total_tokens=60, context_window=100, ratio=0.6, used_anchor=False)
    p_low = ContextPressure(total_tokens=30, context_window=100, ratio=0.3, used_anchor=False)
    assert mgr.requires_compaction(p_high) is True
    assert mgr.requires_compaction(p_low) is False


def test_should_compact_uses_usage_anchor():
    mgr = ContextManager(threshold_ratio=0.5)
    s = Session(session_id="abc", system_prompt="sys")
    # usage 锚点：6000/10000 = 0.6 > 0.5，应压缩
    assert mgr.should_compact(s, context_window=10000, usage={"total_tokens": 6000}) is True
    # 锚点 3000/10000 = 0.3 < 0.5，不压缩
    assert mgr.should_compact(s, context_window=10000, usage={"total_tokens": 3000}) is False


@pytest.mark.parametrize("text", [
    "Error: context length exceeded for this request",
    "context_window exceeded: reduce messages",
    "maximum context tokens reached",
    "This exceeds the token limit",
    "ContextLimitExceeded",
])
def test_is_context_overflow_matches(text):
    mgr = ContextManager()
    assert mgr.is_context_overflow(text) is True


@pytest.mark.parametrize("text", [
    "模型 API 错误: 500",
    "rate limit exceeded",
    "invalid api key",
    "connection error",
])
def test_is_context_overflow_rejects_unrelated(text):
    mgr = ContextManager()
    assert mgr.is_context_overflow(text) is False


# --------------------------------------------------------------------------- #
# Agent：context_check 事件被发射（阶段 A 只观测，不压缩）
# --------------------------------------------------------------------------- #
def make_agent(script, repo=None, context_manager=None, context_window=0):
    provider = FauxProvider(script)
    loop = AgentLoop(provider=provider, model="faux-model", tools=[])
    session = Session(
        session_id=(repo.new_session_id() if repo else "abc"),
        name="t",
        system_prompt="sys",
    )
    agent = Agent(
        loop=loop,
        session=session,
        repo=repo,
        context_manager=context_manager,
        context_window=context_window,
    )
    return agent, provider


def test_no_context_check_when_window_disabled():
    agent, _ = make_agent([[TextDelta("你好")]], context_window=0)
    events = list(agent.prompt("请打招呼"))
    types = [e.type for e in events]
    assert "context_check" not in types
    assert "agent_start" in types


def test_context_check_emitted_when_window_enabled():
    agent, _ = make_agent(
        [[TextDelta("你好")]],
        context_window=10000,
        context_manager=ContextManager(threshold_ratio=0.8),
    )
    events = list(agent.prompt("请打招呼"))
    types = [e.type for e in events]
    assert "context_check" in types
    check = events[0]
    assert check.type == "context_check"
    assert "total_tokens" in check.data
    assert "context_window" in check.data
    assert check.data["context_window"] == 10000
    assert "used_anchor" in check.data
    # 本轮应正常记录 user + assistant
    assert agent.session.message_count == 2
    # context_check 是首事件（在 agent_start 之前）
    assert events[0].type == "context_check"
    assert events[1].type == "agent_start"


def test_context_check_uses_last_usage_anchor_from_provider():
    # FauxProvider 不采集 usage，因此应回退到启发式（used_anchor=False）
    agent, _ = make_agent(
        [[TextDelta("你好")]],
        context_window=10000,
        context_manager=ContextManager(threshold_ratio=0.8),
    )
    events = list(agent.prompt("请打招呼"))
    check = events[0]
    assert check.data["used_anchor"] is False  # FauxProvider 无 usage


def test_agent_roundtrip_with_context_manager(tmp_path):
    # 带 ContextManager 的 Agent 正常驱动一轮，会话可存档并往返
    repo = SessionRepository(tmp_path)
    agent, _ = make_agent(
        [[TextDelta("ok")]],
        repo=repo,
        context_window=10000,
        context_manager=ContextManager(threshold_ratio=0.8),
    )
    list(agent.prompt("喂"))

    loaded = repo.load(agent.session_id)
    assert loaded is not None
    assert loaded.message_count == 2
    # 重建 payload 应能正常给出 model 输入（无异常）
    assert len(loaded.build_llm_payload()) > 0
