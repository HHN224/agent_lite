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
from agent_core.compaction import (
    SUMMARY_SYSTEM_PROMPT,
    TRANSCRIPT_CLOSE,
    TRANSCRIPT_OPEN,
    render_transcript,
)
from ai import TextDelta

from faux_provider import FauxProvider


# 校验要求摘要不能过短（Step C），所以假的「合法摘要」必须够长且像摘要
GOOD_SUMMARY = "## 摘要\n### 目标\n把会话目标、已执行步骤、结论与待办都记录下来。" * 3


# --------------------------------------------------------------------------- #
# 一个确定性的假摘要器：record 被摘的 region，返回固定文本
# --------------------------------------------------------------------------- #
def make_recording_summarizer(text: str = GOOD_SUMMARY):
    calls = []

    def summarize(region_messages):
        calls.append({"region_messages": list(region_messages)})
        return text

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
    engine = CompactionEngine(summarizer=lambda r: GOOD_SUMMARY, retain_ratio=0.16)
    cut = engine.find_cut(s, context_window=100000)
    assert cut is not None
    cut_index, first_kept_id = cut
    # 切点绝不能是 tool 消息（否则保留区段以孤立 tool 结果开头，破坏配对）
    path = s._path_to_head()
    assert path[cut_index].role != "tool"


def test_find_cut_returns_none_when_too_short():
    s = Session(session_id="abc", system_prompt="sys")
    s.append_message("user", "hi")
    engine = CompactionEngine(summarizer=lambda r: GOOD_SUMMARY, retain_ratio=0.16)
    assert engine.find_cut(s, context_window=100000) is None


def test_compact_now_lands_compaction_and_renders_user():
    # 小窗口 + 一段真实长度的历史：切点落在中间，被遮区间非空（否则压缩没有意义）
    s = Session(session_id="abc", system_prompt="sys")
    for i in range(6):
        s.append_message("user", f"第{i}轮问题" + "x" * 200)
        s.append_message("assistant", f"第{i}轮回答" + "y" * 200)

    summarize, calls = make_recording_summarizer()
    engine = CompactionEngine(summarizer=summarize, retain_ratio=0.16)
    result = engine.compact_now(s, context_window=100)

    assert result.success is True
    assert result.summary == GOOD_SUMMARY
    assert result.first_kept_entry_id is not None
    assert result.folded_messages > 0
    # 落地的 compaction 节点在 entries 里
    assert any(e.type == "compaction" for e in s.entries.values())
    # 摘要器只拿到被遮区间的消息，且不再接收会话的编码 agent 人格
    assert calls[0]["region_messages"]
    # 压缩后 payload：system → user(摘要) → 保留消息；摘要不是第二条 system
    payload = s.build_llm_payload()
    assert payload[0] == {"role": "system", "content": "sys"}
    assert payload[1]["role"] == "user"
    assert GOOD_SUMMARY in payload[1]["content"]


def test_compact_now_returns_failure_when_no_cut():
    s = Session(session_id="abc", system_prompt="sys")
    s.append_message("user", "hi")
    engine = CompactionEngine(summarizer=lambda r: GOOD_SUMMARY, retain_ratio=0.16)
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
    # 先前摘要必须以「引用材料」的措辞出现，不能被当成新指令
    assert any("reference material only" in t for t in region_texts)


# --------------------------------------------------------------------------- #
# 摘要请求的形状（Step B 的核心）：transcript 引用块 + 指令在最后 + 绝无工具
# --------------------------------------------------------------------------- #
def test_summarizer_request_shape_puts_instruction_last_and_uses_compressor_persona():
    provider = FauxProvider([[TextDelta(GOOD_SUMMARY)]])
    summarize = make_summarizer(provider, "test-model", max_tokens=1234)
    summarize([{"role": "user", "content": "把雨夜便利店做成三渲二场景"},
               {"role": "assistant", "content": "我先看仓库结构"}])

    call = provider.calls[0]
    msgs = call["messages"]
    # 三段式：压缩器人格 -> transcript 引用块 -> 指令（最后）
    assert msgs[0] == {"role": "system", "content": SUMMARY_SYSTEM_PROMPT}
    assert msgs[1]["content"].startswith(TRANSCRIPT_OPEN)
    assert msgs[1]["content"].rstrip().endswith(TRANSCRIPT_CLOSE)
    assert msgs[-1]["role"] == "user"
    assert "Do NOT continue" in msgs[-1]["content"]
    # 会话里的编码人格不再冒充摘要 system prompt
    assert "coding-focused AI agent" not in str(msgs)
    # 采样参数真的传给了 provider，且无工具
    assert call["tools"] == []
    assert call["temperature"] == 0.0
    assert call["max_tokens"] == 1234


def test_summarizer_request_contains_no_tool_call_shaped_messages():
    """发出去的摘要请求里不能出现 assistant.tool_calls / role=tool 消息（那是『接着说』的诱因）。"""
    provider = FauxProvider([[TextDelta(GOOD_SUMMARY)]])
    summarize = make_summarizer(provider, "test-model")
    summarize([
        {"role": "user", "content": "跑一下测试"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "t1", "type": "function",
                         "function": {"name": "bash", "arguments": '{"command": "pytest -q"}'}}]},
        {"role": "tool", "tool_call_id": "t1", "content": "3 failed"},
    ])

    msgs = provider.calls[0]["messages"]
    assert all("tool_calls" not in m for m in msgs)
    assert all(m["role"] != "tool" for m in msgs)
    # 工具调用与结果必须以文本形式出现在 transcript 里，信息不丢
    assert "[tool call] bash(" in msgs[1]["content"]
    assert "[tool result id=t1]" in msgs[1]["content"]


def test_render_transcript_budget_keeps_head_and_tail():
    messages = [{"role": "user", "content": f"消息{i}-" + "x" * 200} for i in range(40)]
    out = render_transcript(messages, max_chars=2000)

    assert len(out) < 4000
    assert "TRANSCRIPT TRUNCATED" in out
    assert "消息0" in out          # 开头保留（目标/约束通常在这里）
    assert "消息39" in out         # 结尾保留（最新状态）


def test_render_transcript_replaces_images_with_placeholder():
    out = render_transcript([
        {"role": "tool", "tool_call_id": "i1",
         "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]},
    ])
    assert "[image omitted]" in out
    assert "base64" not in out


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
    engine = CompactionEngine(summarizer=lambda r: GOOD_SUMMARY, retain_ratio=0.16)
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
        engine=CompactionEngine(summarizer=lambda r: GOOD_SUMMARY, retain_ratio=0.16),
    )
    events = list(agent.prompt("请继续"))
    types = [e.type for e in events]
    assert "compaction_start" not in types
