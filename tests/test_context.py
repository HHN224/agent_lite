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
from agent_core.context_manager import UsageAnchor, usage_total
from ai import TextDelta

from faux_provider import FauxProvider


# --------------------------------------------------------------------------- #
# usage_total / UsageAnchor
# --------------------------------------------------------------------------- #
def test_usage_total_from_total_tokens():
    assert usage_total({"total_tokens": 5000}) == 5000


def test_usage_total_falls_back_to_prompt_plus_completion():
    assert usage_total({"prompt_tokens": 4000, "completion_tokens": 200}) == 4200


def test_usage_total_rejects_non_dict_and_missing():
    assert usage_total("nope") is None
    assert usage_total({}) is None


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
    assert meter.estimate_message(msg) > 0


def test_measure_no_anchor_falls_back_to_heuristic():
    meter = TokenMeter()
    s = Session(session_id="abc", system_prompt="sys")
    s.append_message("user", "你好")

    p = meter.measure(s, context_window=10000)
    assert p.used_anchor is False
    assert p.anchor_total is None
    assert p.total_tokens == meter.estimate_payload(s.build_llm_payload())
    assert p.ratio > 0


def test_measure_zero_context_window_returns_zero():
    meter = TokenMeter()
    s = Session(session_id="abc", system_prompt="sys")
    p = meter.measure(s, context_window=0)
    assert p.total_tokens == 0
    assert p.ratio == 0.0


# --------------------------------------------------------------------------- #
# 锚点 + 增量：核心逻辑
# --------------------------------------------------------------------------- #
def test_measure_anchor_with_no_growth_equals_anchor():
    # 锚点会话 head 即当前 head，无新增 → 总占用 = 锚点本身
    meter = TokenMeter()
    s = Session(session_id="abc", system_prompt="sys")
    e1 = s.append_message("user", "你好")

    anchor = UsageAnchor(total_tokens=5000, head_id=e1.id)
    p = meter.measure(s, context_window=10000, anchor=anchor)
    assert p.used_anchor is True
    assert p.anchor_total == 5000
    assert p.incremental == 0
    assert p.total_tokens == 5000
    assert p.ratio == 0.5


def test_measure_anchor_plus_growth_after_head():
    # 锚点后新增了内容 → 总占用 = 锚点 + 之后新增的启发式
    meter = TokenMeter()
    s = Session(session_id="abc", system_prompt="sys")
    e_anchor = s.append_message("user", "第一段")  # 锚点边界
    s.append_message("assistant", "回复一")
    s.append_message("user", "第二段")  # 锚点之后新增

    anchor = UsageAnchor(total_tokens=5000, head_id=e_anchor.id)
    p = meter.measure(s, context_window=10000, anchor=anchor)
    assert p.used_anchor is True
    assert p.anchor_total == 5000
    # 增量 = 锚点之后新 append 的 assistant + user 两条消息
    assert p.incremental == meter.estimate_message(
        {"role": "assistant", "content": "回复一"}
    ) + meter.estimate_message({"role": "user", "content": "第二段"})
    assert p.total_tokens == 5000 + p.incremental
    assert p.ratio == p.total_tokens / 10000


def test_measure_anchor_plus_pending_messages():
    # 锚点之后无新 entry，但本轮有 pending（尚未 record 的 user 输入）也要计入
    meter = TokenMeter()
    s = Session(session_id="abc", system_prompt="sys")
    e1 = s.append_message("user", "你好")

    anchor = UsageAnchor(total_tokens=5000, head_id=e1.id)
    pending = [{"role": "user", "content": "这轮的新输入"}]
    p = meter.measure(s, context_window=10000, anchor=anchor, pending_messages=pending)
    assert p.incremental == meter.estimate_message(pending[0])
    assert p.total_tokens == 5000 + p.incremental


def test_measure_anchor_head_not_in_path_counts_zero_growth():
    # 锚点边界不在当前链上（如同锚点后发生了压缩） → 保守增量记 0，避免重复计数
    meter = TokenMeter()
    s = Session(session_id="abc", system_prompt="sys")
    s.append_message("user", "你好")
    # head_id 指向一个不存在的条目
    anchor = UsageAnchor(total_tokens=5000, head_id="nonexistent")
    p = meter.measure(s, context_window=10000, anchor=anchor)
    assert p.total_tokens == 5000
    assert p.incremental == 0


def test_measure_anchor_ignores_compaction_entries_in_delta():
    # 增量只统计 message 条目，compaction 条目不算 token 文本的一部分
    meter = TokenMeter()
    s = Session(session_id="abc", system_prompt="sys")
    e_anchor = s.append_message("user", "a")
    s.append_compaction("一段总结", first_kept_entry_id=e_anchor.id)
    s.append_message("user", "b")

    anchor = UsageAnchor(total_tokens=5000, head_id=e_anchor.id)
    p = meter.measure(s, context_window=10000, anchor=anchor)
    # 增量只含 message "b"，不含 compaction 条目
    assert p.incremental == meter.estimate_message({"role": "user", "content": "b"})


# --------------------------------------------------------------------------- #
# ContextManager：触发判定 + 溢出检测
# --------------------------------------------------------------------------- #
def test_should_compact_true_when_at_threshold():
    mgr = ContextManager(threshold_ratio=0.8)
    s = Session(session_id="abc", system_prompt="sys")
    s.append_message("user", "x" * 100)

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


def test_should_compact_anchor_crosses_threshold_with_growth():
    # 关键场景：锚点自身低于阈值，但锚点后新增的内容把实际占用推过阈值 → 应压缩
    mgr = ContextManager(threshold_ratio=0.5)
    s = Session(session_id="abc", system_prompt="sys")
    e_anchor = s.append_message("user", "起点")
    s.append_message("assistant", "x" * 4000)  # 锚点后新增大量内容

    anchor = UsageAnchor(total_tokens=6000, head_id=e_anchor.id)
    # 锚点 6000/10000 = 0.6 本身已超…… 调低锚点以复现"靠增量才越线"：
    anchor_low = UsageAnchor(total_tokens=4000, head_id=e_anchor.id)  # 0.4 < 0.5
    assert mgr.should_compact(s, context_window=10000, anchor=anchor_low) is True


def test_should_compact_anchor_below_threshold_no_growth():
    mgr = ContextManager(threshold_ratio=0.5)
    s = Session(session_id="abc", system_prompt="sys")
    e1 = s.append_message("user", "hi")
    anchor = UsageAnchor(total_tokens=3000, head_id=e1.id)  # 0.3 < 0.5，无新增
    assert mgr.should_compact(s, context_window=10000, anchor=anchor) is False


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
    assert "incremental" in check.data
    assert agent.session.message_count == 2
    # context_check 是首事件（在 agent_start 之前）
    assert events[0].type == "context_check"
    assert events[1].type == "agent_start"


def test_context_check_heuristic_when_provider_has_no_usage():
    # FauxProvider 不采集 usage → 锚点为 None → 回退启发式（used_anchor=False）
    agent, _ = make_agent(
        [[TextDelta("你好")]],
        context_window=10000,
        context_manager=ContextManager(threshold_ratio=0.8),
    )
    events = list(agent.prompt("请打招呼"))
    check = events[0]
    assert check.data["used_anchor"] is False


def test_context_check_counts_pending_user_input():
    # 本轮 user 输入应计入 pending，使 total = 现有 payload + 本轮 user 输入。
    # 注意：context_check 发生在 build payload / record_turn 之前，此时 session 只有 system。
    agent, _ = make_agent(
        [[TextDelta("你好")]],
        context_window=10000,
        context_manager=ContextManager(threshold_ratio=0.8),
    )
    events = list(agent.prompt("请打招呼"))
    check = events[0]
    meter = agent.context_manager.meter
    # 无锚点（FauxProvider）→ total = 目前 payload（仅 system）+ 本轮 pending user 输入
    expected = meter.estimate_payload([{"role": "system", "content": "sys"}]) + meter.estimate_message(
        {"role": "user", "content": "请打招呼"}
    )
    assert check.data["total_tokens"] == expected


def test_agent_roundtrip_with_context_manager(tmp_path):
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
    assert len(loaded.build_llm_payload()) > 0
