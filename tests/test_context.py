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
from agent_core.context_manager import usage_input_total, usage_total
from ai import TextDelta

from faux_provider import FauxProvider


# --------------------------------------------------------------------------- #
# usage_total / usage_input_total
# --------------------------------------------------------------------------- #
def test_usage_total_from_total_tokens():
    assert usage_total({"total_tokens": 5000}) == 5000


def test_usage_total_falls_back_to_prompt_plus_completion():
    assert usage_total({"prompt_tokens": 4000, "completion_tokens": 200}) == 4200


def test_usage_total_rejects_non_dict_and_missing():
    assert usage_total("nope") is None
    assert usage_total({}) is None


def test_usage_input_total_uses_prompt_tokens():
    # 校准式用输入侧 prompt_tokens，而不是 total_tokens（避免把输出侧算进 usage）
    assert usage_input_total({"prompt_tokens": 4000, "completion_tokens": 200, "total_tokens": 4200}) == 4000


def test_usage_input_total_rejects_missing_prompt():
    assert usage_input_total({"completion_tokens": 200}) is None
    assert usage_input_total({}) is None


# --------------------------------------------------------------------------- #
# TokenMeter：估算 / 合成（两变量）
# --------------------------------------------------------------------------- #
def test_estimate_message_includes_tool_calls():
    meter = TokenMeter()
    msg = {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": "t1", "function": {"name": "read", "arguments": "{}"}}],
    }
    assert meter.estimate_message(msg) > 0


def test_measure_zero_context_window_returns_zero():
    meter = TokenMeter()
    s = Session(session_id="abc", system_prompt="sys")
    p = meter.measure(s, context_window=0)
    assert p.total_tokens == 0
    assert p.ratio == 0.0


def test_measure_is_usage_plus_new_usage():
    # total = usage + new_usage，正是两变量模型
    meter = TokenMeter()
    s = Session(session_id="abc", system_prompt="sys", usage=70000, new_usage=15000)
    p = meter.measure(s, context_window=100000)
    assert p.total_tokens == 85000
    assert p.usage == 70000
    assert p.new_usage == 15000
    assert p.ratio == 0.85


def test_measure_falls_back_to_payload_heuristic_when_usage_zero():
    # usage 为 0（尚未校准）时，用整段 payload 启发式兜底
    meter = TokenMeter()
    s = Session(session_id="abc", system_prompt="sys")
    s.append_message("user", "hi")
    p = meter.measure(s, context_window=100000)
    # usage 是 0，new_usage 是 0 → 启发式兜底 = 整个 payload 估算
    assert p.total_tokens == meter.estimate_payload(s.build_llm_payload())
    assert p.usage == 0
    assert p.new_usage == 0


# --------------------------------------------------------------------------- #
# ContextManager：触发判定 + 溢出检测
# --------------------------------------------------------------------------- #
def test_should_compact_true_when_at_threshold():
    mgr = ContextManager(threshold_ratio=0.8)
    s = Session(session_id="abc", system_prompt="sys", usage=85000, new_usage=0)
    assert mgr.should_compact(s, context_window=100000) is True


def test_should_compact_false_when_below_threshold():
    mgr = ContextManager(threshold_ratio=0.8)
    s = Session(session_id="abc", system_prompt="sys", usage=30000, new_usage=0)
    assert mgr.should_compact(s, context_window=100000) is False


def test_should_compact_crosses_threshold_via_new_usage():
    # 核心场景：usage 自身低于阈值，但 new_usage 把实际占用推过阈值 → 应压缩
    mgr = ContextManager(threshold_ratio=0.8)
    s = Session(session_id="abc", system_prompt="sys", usage=70000, new_usage=15000)
    # total 85000 / 100000 = 0.85 ≥ 0.8
    assert mgr.should_compact(s, context_window=100000) is True


def test_requires_compaction_respects_threshold():
    mgr = ContextManager(threshold_ratio=0.5)
    p_high = ContextPressure(total_tokens=60, context_window=100, ratio=0.6, usage=50, new_usage=10)
    p_low = ContextPressure(total_tokens=30, context_window=100, ratio=0.3, usage=30, new_usage=0)
    assert mgr.requires_compaction(p_high) is True
    assert mgr.requires_compaction(p_low) is False


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
# Session：usage / new_usage 持久化往返
# --------------------------------------------------------------------------- #
def test_session_roundtrips_usage_and_new_usage(tmp_path):
    repo = SessionRepository(tmp_path)
    s = repo.create(name="x", system_prompt="sys")
    s.append_message("user", "你好")
    s.usage = 12345
    s.new_usage = 678
    repo.save(s)

    loaded = repo.load(s.session_id)
    assert loaded.usage == 12345
    assert loaded.new_usage == 678


def test_session_clear_history_resets_usage(tmp_path):
    repo = SessionRepository(tmp_path)
    s = repo.create(name="x", system_prompt="sys")
    s.usage = 100
    s.new_usage = 50
    s.clear_history()
    assert s.usage == 0
    assert s.new_usage == 0


# --------------------------------------------------------------------------- #
# Agent：两变量生命周期（校准式）
# --------------------------------------------------------------------------- #
def make_agent(script, repo=None, context_manager=None, context_window=0, **session_kw):
    provider = FauxProvider(script)
    loop = AgentLoop(provider=provider, model="faux-model", tools=[])
    session = Session(
        session_id=(repo.new_session_id() if repo else "abc"),
        name="t",
        system_prompt="sys",
        **session_kw,
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
    assert check.data["context_window"] == 10000
    assert "usage" in check.data
    assert "new_usage" in check.data
    assert agent.session.message_count == 2
    assert events[0].type == "context_check"
    assert events[1].type == "agent_start"


def test_context_check_counts_user_input_as_new_usage():
    # 开场：本轮 user 输入应累加进 new_usage（FauxProvider 无 usage，usage 保持 0）
    agent, _ = make_agent(
        [[TextDelta("你好")]],
        context_window=10000,
        context_manager=ContextManager(threshold_ratio=0.8),
    )
    events = list(agent.prompt("请打招呼"))
    check = events[0]
    meter = agent.context_manager.meter
    expected = meter.estimate_message({"role": "user", "content": "请打招呼"})
    assert check.data["new_usage"] == expected
    assert check.data["usage"] == 0


def test_agent_calibrates_usage_from_provider_hint():
    # 模拟 provider 在调用后上报真实 usage 的「输入侧」，校准 session.usage。
    # FauxProvider 不采集 usage，直接手动设 last_usage，验证 Agent 收尾时用它校准。
    agent, provider = make_agent(
        [[TextDelta("你好")]],
        context_window=10000,
        context_manager=ContextManager(threshold_ratio=0.8),
    )
    provider.last_usage = {"prompt_tokens": 4321, "completion_tokens": 50, "total_tokens": 4371}
    list(agent.prompt("请打招呼"))
    # 收尾用输入侧 prompt_tokens 校准 usage
    assert agent.session.usage == 4321


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
