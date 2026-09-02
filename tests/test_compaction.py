import pytest

from agent_core import (
    Agent,
    AgentLoop,
    CompactionEngine,
    CompactionResult,
    ContextManager,
    Session,
    SessionRepository,
    make_summarizer,
)
from ai import TextDelta

from faux_provider import FauxProvider


# --------------------------------------------------------------------------- #
# 一个确定性的假摘要器：record 被摘的 region，返回固定文本
# --------------------------------------------------------------------------- #
def make_recording_summarizer():
    calls = []

    def summarize(region_messages, system_prompt):
        calls.append({"region_messages": list(region_messages), "system_prompt": system_prompt})
        return "SUMMARY:" + str(len(region_messages))

    return summarize, calls


# --------------------------------------------------------------------------- #
# 切点：工具配对平衡
# --------------------------------------------------------------------------- #
def _make_session_with_tool_usage():
    """构造一段含工具调用的历史：assistant(tool_calls) → tool 结果 → assistant → user。"""
    s = Session(session_id="abc", system_prompt="sys")
    s.append_message("user", "请读取文件")
    s.append_message(
        "assistant",
        content=None,
        tool_calls=[{"id": "t1", "type": "function", "function": {"name": "read", "arguments": "{}"}}],
    )
    s.append_message("tool", content="文件内容：..." * 100, tool_call_id="t1")
    s.append_message("assistant", "好了，内容如下")
    s.append_message("user", "再分析一下")
    return s


def test_find_cut_is_not_on_tool_message():
    s = _make_session_with_tool_usage()
    engine = CompactionEngine(summarizer=lambda r, sp: "x", retain_ratio=0.16)
    cut = engine.find_cut(s, context_window=100000)
    assert cut is not None
    cut_index, first_kept_id = cut
    # 切点绝不能是 tool 消息（否则保留区段以孤立 tool 结果开头，破坏配对）
    path = s._path_to_head()
    assert path[cut_index].role != "tool"


def test_find_cut_returns_none_when_too_short():
    s = Session(session_id="abc", system_prompt="sys")
    s.append_message("user", "hi")
    engine = CompactionEngine(summarizer=lambda r, sp: "x", retain_ratio=0.16)
    assert engine.find_cut(s, context_window=100000) is None


def test_compact_now_lands_compaction_and_renders_user():
    s = _make_session_with_tool_usage()
    summarize, calls = make_recording_summarizer()
    engine = CompactionEngine(summarizer=summarize, retain_ratio=0.16)
    result = engine.compact_now(s, context_window=100000)

    assert result.success is True
    assert result.summary.startswith("SUMMARY:")
    assert result.first_kept_entry_id is not None
    # 落地的 compaction 节点在 entries 里
    assert any(e.type == "compaction" for e in s.entries.values())
    # 压缩后 payload：system → user(摘要) → 保留消息；摘要不是第二条 system
    payload = s.build_llm_payload()
    assert payload[0] == {"role": "system", "content": "sys"}
    assert payload[1]["role"] == "user"
    assert payload[1]["content"].startswith("SUMMARY:")
    assert calls[0]["system_prompt"] == "sys"


def test_compact_now_returns_failure_when_no_cut():
    s = Session(session_id="abc", system_prompt="sys")
    s.append_message("user", "hi")
    engine = CompactionEngine(summarizer=lambda r, sp: "x", retain_ratio=0.16)
    result = engine.compact_now(s, context_window=100000)
    assert result.success is False


def test_summary_input_includes_prior_compaction_summary():
    # 多次压缩：被遮区间里已有一次 compaction，新摘要应前置该摘要（总结的压缩）。
    # 用小窗口 + 精心调配内容长度，让切点落在「先前摘要」之后，使先前摘要进入被遮区间。
    s = Session(session_id="abc", system_prompt="sys")
    e1 = s.append_message("user", "a")
    s.append_compaction("先前摘要", first_kept_entry_id=e1.id)
    s.append_message("user", "B" * 100)  # 大内容，让 acc 累积到 retain 预算
    s.append_message("assistant", "c")  # 小内容

    summarize, calls = make_recording_summarizer()
    engine = CompactionEngine(summarizer=summarize, retain_ratio=0.16)
    # context_window=100 → retain=16 token；assistant "c"(≈5) <16，加上 user "B"*100(≈29) 后 ≥16，
    # 使 cut 落在 index2（user "B"），保留 [B, c]，被遮区间 = [user a, compaction(先前摘要)]
    result = engine.compact_now(s, context_window=100)
    assert result.success is True
    region_texts = [m.get("content", "") for m in calls[0]["region_messages"]]
    assert any("先前摘要" in t for t in region_texts)


# --------------------------------------------------------------------------- #
# 自动触发：compact_if_needed 生成器 + Agent 集成
# --------------------------------------------------------------------------- #
def test_compact_if_needed_yields_events():
    s = _make_session_with_tool_usage()
    summarize, _ = make_recording_summarizer()
    engine = CompactionEngine(summarizer=summarize, retain_ratio=0.16)
    events = list(engine.compact_if_needed(s, context_window=100000))
    types = [e.type for e in events]
    assert "compaction_start" in types
    assert "compaction_end" in types
    assert events[-1].data["success"] is True


def test_compact_if_needed_skips_when_no_cut():
    s = Session(session_id="abc", system_prompt="sys")
    s.append_message("user", "hi")
    engine = CompactionEngine(summarizer=lambda r, sp: "x", retain_ratio=0.16)
    events = list(engine.compact_if_needed(s, context_window=100000))
    assert events[0].type == "compaction_skip"


def make_agent(script, session_kw=None, context_window=100000, threshold=0.8, engine=None):
    provider = FauxProvider(script)
    loop = AgentLoop(provider=provider, model="faux-model", tools=[])
    session = Session(session_id="abc", name="t", system_prompt="sys", **(session_kw or {}))
    cm = ContextManager(threshold_ratio=threshold)
    agent = Agent(
        loop=loop,
        session=session,
        repo=None,
        context_manager=cm,
        context_window=context_window,
        compaction_engine=engine,
    )
    return agent, provider


def test_agent_auto_compacts_on_threshold():
    # usage 已超阈值（0.9），且历史足够长 → Auto 触发压缩
    summarize, calls = make_recording_summarizer()
    engine = CompactionEngine(summarizer=summarize, retain_ratio=0.16)
    s = Session(session_id="abc", name="t", system_prompt="sys", usage=90000, new_usage=0)
    # 造一段足够长的历史，让 find_cut 能找到安全切点
    for i in range(20):
        s.append_message("user", f"第{i}轮用户问题" + "x" * 100)
        s.append_message("assistant", f"第{i}轮回复" + "y" * 100)
    agent, _ = make_agent(
        [[TextDelta("好的")]],
        session_kw={"usage": 90000, "new_usage": 0},
        context_window=100000,
        threshold=0.8,
        engine=engine,
    )
    # 用上面造好的长历史替换 make_agent 里的空 session
    agent.session = s
    events = list(agent.prompt("请继续"))
    types = [e.type for e in events]
    assert "compaction_start" in types
    assert "compaction_end" in types


def test_agent_does_not_compact_when_below_threshold():
    agent, _ = make_agent(
        [[TextDelta("好的")]],
        session_kw={"usage": 30000, "new_usage": 0},
        context_window=100000,
        threshold=0.8,
        engine=CompactionEngine(summarizer=lambda r, sp: "x", retain_ratio=0.16),
    )
    events = list(agent.prompt("请继续"))
    types = [e.type for e in events]
    assert "compaction_start" not in types
