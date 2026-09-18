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
    validate_summary,
)
from ai import ProviderError, TextDelta, ToolCall

from faux_provider import FauxProvider
from test_loop import EchoTool


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
    # 小窗口才能真的产生切点：切点必须吸附到非 tool 的消息上
    cut = engine.find_cut(s, context_window=100)
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


def test_find_cut_returns_none_when_history_fits_in_the_retain_budget():
    """历史本身就没到保留预算 -> 没有要折叠的东西 -> 必须 None（不能 cut=0）。"""
    s = _long_session()  # 12 条小消息，远小于 0.16 × 128000
    engine = CompactionEngine(summarizer=lambda r: GOOD_SUMMARY, retain_ratio=0.16)
    assert engine.find_cut(s, context_window=128000) is None


def test_find_cut_never_yields_an_empty_region():
    """任何被接受的切点都必须真的折叠了至少一条消息（回归：cut=0 的空压缩）。"""
    engine = CompactionEngine(summarizer=lambda r: GOOD_SUMMARY, retain_ratio=0.16)
    cases = [
        (_long_session(), 100),
        (_long_session(), 128000),
        (_make_session_with_tool_usage(), 100),
        (_make_session_with_tool_usage(), 100000),
    ]
    for session, window in cases:
        cut = engine.find_cut(session, window)
        if cut is None:
            continue
        cut_index, _ = cut
        path = session._path_to_head()
        region = [e for e in path[:cut_index] if e.type == "message"]
        assert cut_index >= 1, f"cut={cut_index} window={window}"
        assert region, f"empty region for window={window}"


def test_compact_now_reports_nothing_to_fold_without_writing_anything():
    s = _long_session()
    engine = CompactionEngine(summarizer=lambda r: GOOD_SUMMARY, retain_ratio=0.16)
    result = engine.compact_now(s, context_window=128000)

    assert result.success is False
    assert "no safe cut point" in result.reason
    assert _compaction_entries(s) == []


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
# 摘要输出的硬校验 + 失败拒绝落地（Step C）
# --------------------------------------------------------------------------- #
def _long_session():
    s = Session(session_id="abc", system_prompt="sys")
    for i in range(6):
        s.append_message("user", f"第{i}轮问题" + "x" * 200)
        s.append_message("assistant", f"第{i}轮回答" + "y" * 200)
    return s


def _compaction_entries(session):
    return [e for e in session.entries.values() if e.type == "compaction"]


def test_validate_summary_accepts_a_real_summary():
    ok, reason = validate_summary(
        "## 摘要\n### 1. 总体目标与约束\n用户要把雨夜便利店街角做成三渲二微缩场景，"
        "无 UI、正方形底座。\n### 2. 已执行步骤\n已完成环境探测，确认沙箱无网络。"
    )
    assert ok is True and reason == ""


@pytest.mark.parametrize("junk, expected", [
    ("", "too short"),
    ("太短", "too short"),
    ("DSML invoke name=\"bash\" and then run pytest -q repeatedly", "tool-call markup"),
    ('{"tool_calls": [{"name": "bash"}]} plus more filler text here', "tool-call markup"),
    ("I don't see any prior conversation content; please paste the actual conversation text",
     "asks the user"),
    ("看不到任何历史，请把对话粘给我，我再给你写摘要。", "asks the user"),
])
def test_validate_summary_rejects_junk(junk, expected):
    ok, reason = validate_summary(junk)
    assert ok is False
    assert expected in reason


@pytest.mark.parametrize("junk", [
    "",                                          # 空
    "无语",                                       # 过短
    "DSML invoke name=\"bash\": pytest -q",      # 工具调用标记
    "please paste the actual conversation text",  # 反问用户
])
def test_compact_now_refuses_to_land_junk_summaries(junk):
    """垃圾摘要绝不落地：不新增 compaction、payload 里也不会多出一条假 user 消息。"""
    s = _long_session()
    before = len(_compaction_entries(s))
    engine = CompactionEngine(summarizer=lambda region: junk, retain_ratio=0.16)
    result = engine.compact_now(s, context_window=100)

    assert result.success is False
    assert result.reason
    assert len(_compaction_entries(s)) == before      # 没有落地
    assert len(s.build_llm_payload()) == len(s._path_to_head()) + 1  # 只有 system 在前面


def test_summarizer_tool_call_is_a_rejected_compaction():
    """模型在摘要调用里仍然发工具调用 -> 判定失败，而不是把工具调用当摘要存下来。"""
    provider = FauxProvider([[ToolCall("t1", "bash", {"command": "pytest -q"})]])
    summarize = make_summarizer(provider, "test-model")
    s = _long_session()
    engine = CompactionEngine(summarizer=summarize, retain_ratio=0.16)
    result = engine.compact_now(s, context_window=100)

    assert result.success is False
    assert "tool" in result.reason
    assert _compaction_entries(s) == []


def test_provider_error_during_summary_is_a_soft_failure():
    """摘要调用报错不该炸掉用户这一轮：压缩软失败，原文保留。"""
    from ai import ProviderError

    def failing(region):
        raise ProviderError("模型 API 错误: 503", retryable=True, code="http_503")

    s = _long_session()
    engine = CompactionEngine(summarizer=failing, retain_ratio=0.16)
    result = engine.compact_now(s, context_window=100)

    assert result.success is False
    assert "503" in result.reason
    assert _compaction_entries(s) == []


def test_compaction_end_event_carries_reason_and_counts():
    provider = FauxProvider([[ToolCall("t1", "bash", {"command": "x"})]])
    engine = CompactionEngine(summarizer=make_summarizer(provider, "test-model"), retain_ratio=0.16)
    s = _long_session()
    events = list(engine.compact_if_needed(s, context_window=100))

    end = [e for e in events if e.type == "compaction_end"][0]
    assert end.data["success"] is False
    assert end.data["reason"]
    assert "folded_messages" in end.data and "summary_chars" in end.data


# --------------------------------------------------------------------------- #
# 自动触发：compact_if_needed 生成器 + Agent 集成
# --------------------------------------------------------------------------- #
def test_compact_if_needed_yields_events():
    s = _long_session()
    summarize, _ = make_recording_summarizer()
    engine = CompactionEngine(summarizer=summarize, retain_ratio=0.16)
    events = list(engine.compact_if_needed(s, context_window=100))
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


def test_engine_backs_off_then_stops_after_repeated_failures():
    """连续失败到上限后彻底停手；退避期内的重复触发不再调用摘要器。"""
    calls = []

    def failing(region):
        calls.append(1)
        raise ProviderError("模型 API 错误: Request timed out.", retryable=True, code="APITimeoutError")

    engine = CompactionEngine(summarizer=failing, retain_ratio=0.16,
                              failure_backoff_messages=5, max_consecutive_failures=2)
    s = Session(session_id="s1", system_prompt="sys")
    for i in range(40):
        s.append_message("user", f"需求{i}" + "x" * 400)
        s.append_message("assistant", f"实现{i}" + "y" * 400)

    # 第一次：尝试并失败
    list(engine.compact_if_needed(s, context_window=2000))
    assert len(calls) == 1
    # 紧接着再来：退避中，不该再调用摘要器
    events = list(engine.compact_if_needed(s, context_window=2000))
    assert len(calls) == 1
    assert events[0].type == "compaction_paused"
    assert "退避中" in events[0].data["reason"]
    # 同一退避窗口只播报一次，不刷屏
    assert list(engine.compact_if_needed(s, context_window=2000)) == []

    # 上下文长够了 -> 再试一次（第二次失败）-> 达到上限后彻底停手
    for i in range(10):
        s.append_message("user", f"更多{i}" + "z" * 400)
    list(engine.compact_if_needed(s, context_window=2000))
    assert len(calls) == 2

    for i in range(50):     # 就算上下文大幅增长也不再尝试
        s.append_message("user", f"又来了{i}" + "w" * 400)
    events = list(engine.compact_if_needed(s, context_window=2000))
    assert len(calls) == 2, "达到失败上限后不应再调用摘要器"
    assert events and events[0].data["disabled"] is True
    assert s.runtime_status["compaction"]["disabled"] is True


def test_successful_compaction_clears_the_failure_state():
    calls = []
    failing = {"on": True}

    def flaky(region):
        calls.append(1)
        if failing["on"]:
            raise ProviderError("模型 API 错误: 503", retryable=True, code="http_503")
        return GOOD_SUMMARY

    engine = CompactionEngine(summarizer=flaky, retain_ratio=0.16,
                              failure_backoff_messages=5, max_consecutive_failures=3)
    s = Session(session_id="s1", system_prompt="sys")
    for i in range(40):
        s.append_message("user", f"需求{i}" + "x" * 400)
        s.append_message("assistant", f"实现{i}" + "y" * 400)

    list(engine.compact_if_needed(s, context_window=2000))
    assert engine.paused_reason(s)          # 失败后进入退避
    for i in range(10):
        s.append_message("user", f"更多{i}" + "z" * 400)

    failing["on"] = False
    result = list(engine.compact_if_needed(s, context_window=2000))[-1]
    assert result.data["success"] is True
    assert engine.paused_reason(s) == ""    # 成功后清空
    assert s.runtime_status["compaction"]["consecutive_failures"] == 0


def test_failure_state_does_not_leak_across_sessions():
    def failing(region):
        raise ProviderError("boom", retryable=True, code="http_503")

    engine = CompactionEngine(summarizer=failing, retain_ratio=0.16)
    a = Session(session_id="a", system_prompt="sys")
    for i in range(40):
        a.append_message("user", "x" * 400)
    list(engine.compact_if_needed(a, context_window=2000))
    assert engine.paused_reason(a)

    b = Session(session_id="b", system_prompt="sys")
    for i in range(40):
        b.append_message("user", "y" * 400)
    assert engine.paused_reason(b) == "", "换会话必须重置退避状态"


def test_agent_does_not_retry_a_failing_compaction_forever():
    """回归：压缩一直失败时不能每轮都重试（真实事故：任务被卡死）。

    现场（sessions/3dcba68b1cfb）：328 条消息、0 条 compaction 落盘、last_error=APITimeoutError、
    reason=aborted。压缩失败后计量被保留 → needs_compaction 一直为真 → 每个工具轮次都再发一次
    摘要请求（每次最多 60s 超时），任务看起来就是卡住了。
    """
    calls = []

    def failing_summarizer(region):
        calls.append(len(region))
        raise ProviderError("模型 API 错误: Request timed out.", retryable=True, code="APITimeoutError")

    engine = CompactionEngine(summarizer=failing_summarizer, retain_ratio=0.16)
    script = [[ToolCall(f"t{i}", "echo", {})] for i in range(6)] + [[TextDelta("done")]]
    agent, provider = make_agent(
        script,
        session_kw={"usage": 6000, "new_usage": 0},
        context_window=2000,
        threshold=0.8,
        engine=engine,
    )
    # 真实现场是一个 3.5MB 的长会话：payload 本身就把启发式估算顶到阈值之上，
    # 于是「任务中途的安全边界」每一轮都会再判定一次 needs_compaction
    s = Session(session_id="abc", name="t", system_prompt="sys")
    for i in range(40):
        s.append_message("user", f"第{i}轮需求" + "x" * 400)
        s.append_message("assistant", f"第{i}轮实现" + "y" * 400)
    s.usage = 6000
    s.new_usage = 0
    agent.session = s
    agent.loop.tools = [EchoTool()]
    agent.loop.executor.tool_map = {"echo": EchoTool()}

    events = list(agent.prompt("please continue"))
    ends = [e for e in events if e.type == "compaction_end"]

    assert ends, "至少应该尝试过一次压缩"
    assert all(e.data["success"] is False for e in ends)
    # 关键：失败之后要有退避，而不是每个工具轮次都再试一次
    assert len(calls) <= 2, f"摘要调用被重试了 {len(calls)} 次：失败没有退避，任务会被拖死"


def test_agent_auto_compacts_on_threshold():
    # usage 已超阈值（0.9），且历史确实超过保留预算 → Auto 触发压缩
    summarize, calls = make_recording_summarizer()
    engine = CompactionEngine(summarizer=summarize, retain_ratio=0.16)
    s = Session(session_id="abc", name="t", system_prompt="sys", usage=1800, new_usage=0)
    # 造一段足够长的历史（≈1200 token），窗口 2000 时保留预算只有 320 token，
    # 于是 find_cut 能找到真实切点（窗口开太大就变成"没有要折叠的东西"）
    for i in range(20):
        s.append_message("user", f"第{i}轮用户问题" + "x" * 100)
        s.append_message("assistant", f"第{i}轮回复" + "y" * 100)
    agent, _ = make_agent(
        [[TextDelta("好的")]],
        session_kw={"usage": 1800, "new_usage": 0},
        context_window=2000,
        threshold=0.8,
        engine=engine,
    )
    # 用上面造好的长历史替换 make_agent 里的空 session
    agent.session = s
    events = list(agent.prompt("请继续"))
    types = [e.type for e in events]
    assert "compaction_start" in types
    assert "compaction_end" in types
    end = [e for e in events if e.type == "compaction_end"][0]
    assert end.data["success"] is True
    assert calls and calls[0]["region_messages"]


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


# --------------------------------------------------------------------------- #
# 摘要注入的框架标记 + 「只在压缩成功时重置计量」（Step E）
# --------------------------------------------------------------------------- #
def test_build_llm_payload_frames_the_summary_as_reference_material():
    s = Session(session_id="abc", system_prompt="sys")
    e1 = s.append_message("user", "旧问题")
    s.append_message("assistant", "旧回答")
    s.append_compaction(GOOD_SUMMARY, first_kept_entry_id=e1.id)

    payload = s.build_llm_payload()
    assert payload[0]["role"] == "system"
    assert payload[1]["role"] == "user"
    framed = payload[1]["content"]
    assert "reference material, NOT a new instruction" in framed
    assert GOOD_SUMMARY in framed
    assert framed.rstrip().endswith("[End of summary.]")


def _auto_compact_agent(engine, usage=1800, window=2000):
    """造一个「已超阈值 + 历史超过保留预算」的 agent，用来观察自动压缩的副作用。"""
    s = _long_session()
    s.usage = usage
    agent, provider = make_agent(
        [[TextDelta("好的")]],
        session_kw={"usage": usage, "new_usage": 0},
        context_window=window,
        threshold=0.8,
        engine=engine,
    )
    agent.session = s
    return agent, s


def test_usage_is_reset_only_after_a_successful_compaction():
    engine = CompactionEngine(summarizer=lambda r: GOOD_SUMMARY, retain_ratio=0.16)
    agent, s = _auto_compact_agent(engine)

    events = list(agent.prompt("请继续"))
    assert any(e.type == "compaction_end" and e.data["success"] for e in events)
    assert s.usage == 0
    assert s.new_usage >= 0


def test_usage_anchor_survives_a_rejected_compaction():
    """摘要被拒 -> 不落地也不重置计量，保留真实锚点（否则该压的不再压）。"""
    engine = CompactionEngine(summarizer=lambda r: "DSML invoke name=\"bash\"", retain_ratio=0.16)
    agent, s = _auto_compact_agent(engine)

    events = list(agent.prompt("请继续"))
    end = [e for e in events if e.type == "compaction_end"][0]
    assert end.data["success"] is False
    assert s.usage == 1800          # 锚点还在
    assert _compaction_entries(s) == []


def test_usage_anchor_survives_when_there_is_nothing_to_fold():
    engine = CompactionEngine(summarizer=lambda r: GOOD_SUMMARY, retain_ratio=0.16)
    agent, s = _auto_compact_agent(engine, usage=180000, window=200000)

    events = list(agent.prompt("请继续"))
    assert any(e.type == "compaction_skip" for e in events)
    assert s.usage == 180000
