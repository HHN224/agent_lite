"""上下文压缩引擎（阶段 B）：把 `/compact` 与自动触发变成「真的压缩」。

在阶段 A（计量 + 触发）之上，本模块实现真正的压缩动作：
  1. 选一个安全的切点（工具配对平衡，绝不切在未闭合工具区间上）。
  2. 保留尾部（默认按 context_window 的 retain_ratio，DSH 0.16 原文）。
  3. 用独立 provider 调用（结构化摘要 prompt）把切点之前的历史总结成一条摘要。
  4. 落地成一个 compaction 节点（append-only、非破坏），由 build_llm_payload 渲染。

设计对齐研究结论（docs/research/05-synthesis-and-recommendations.md §3 阶段 B）：
  - 非破坏（append-only）：被遮区间仍在 entries 里，只是不再发给模型。
  - 边界吸附：切点必须是工具配对平衡的安全边界。
  - 摘要走独立 LLM 调用（不走 AgentLoop 的 turn）。
  - 用「总结的压缩」解决多次压缩越攒越多：build_llm_payload 只取最新的 compaction，
    旧的在 entries 里被遮蔽，且其摘要会作为上下文喂给新的摘要调用。

摘要的具体调用由可注入的 summarizer 提供（生产用 provider，测试用假函数），
把「怎么总结」与 CompactionEngine 解耦，便于独立测试与替换。
"""

from __future__ import annotations

from dataclasses import dataclass

from ai import TextDelta

from .events import AgentEvent
from .context_manager import TokenMeter


# --------------------------------------------------------------------------- #
# 结构化摘要 prompt（固定检查点，比自由摘要更稳定、可复现）
# --------------------------------------------------------------------------- #
SUMMARY_PROMPT = """\
You are summarizing an older part of a conversation so it can be replaced by this summary.

Produce a concise but faithful summary that captures:
1. The user's overall goal and any constraints.
2. Steps already taken (tools executed, files read/written, commands run) and their outcomes.
3. Important facts, decisions, and conclusions established so far.
4. Anything still incomplete, blocked, or pending that must not be forgotten.

Write in the same language as the conversation. Keep it around {max_tokens} tokens at most.
"""


@dataclass
class CompactionResult:
    """一次压缩的结果。success=False 表示未找到安全切点或摘要失败，未落地任何东西。"""

    success: bool
    summary: str = ""
    first_kept_entry_id: str | None = None
    entry_id: str | None = None
    compacted_count: int = 0
    reason: str = ""


# --------------------------------------------------------------------------- #
# 摘要器工厂：生产用 provider 的独立流式调用
# --------------------------------------------------------------------------- #
def make_summarizer(provider, model: str, max_tokens: int = 2000):
    """构造一个「独立的 LLM 摘要调用」的 summarizer。

    返回的 summarizer(region_messages, system_prompt) -> str：
      用固定结构化 prompt + 重放被遮区间的消息，调 provider 收集纯文本摘要。
      不给工具（纯文本任务），忽略任何 ToolCall 事件。
    """

    def summarize(region_messages: list[dict], system_prompt: str) -> str:
        instruction = SUMMARY_PROMPT.format(max_tokens=max_tokens)
        msgs: list[dict] = []
        if system_prompt:
            msgs.append({"role": "system", "content": system_prompt})
        msgs.append({"role": "user", "content": instruction})
        msgs.extend(region_messages)
        parts: list[str] = []
        for event in provider.stream(msgs, [], model):
            if isinstance(event, TextDelta):
                parts.append(event.content)
        text = "".join(parts).strip()
        return text or "(无摘要)"

    return summarize


# --------------------------------------------------------------------------- #
# 压缩引擎
# --------------------------------------------------------------------------- #
class CompactionEngine:
    """真正的压缩动作：选切点 + 保留尾部 + 独立摘要 + 落地 compaction。

    summarizer   可注入的摘要函数：callable(region_messages, system_prompt) -> str
    retain_ratio 保留尾部的窗口占比（默认 0.16，DSH）
    meter        用于估算切点 token 的量计器（复用 TokenMeter）
    """

    def __init__(
        self,
        summarizer,
        retain_ratio: float = 0.16,
        min_keep_entries: int = 2,
        meter: TokenMeter | None = None,
    ):
        self.summarizer = summarizer
        self.retain_ratio = retain_ratio
        self.min_keep_entries = min_keep_entries
        self.meter = meter or TokenMeter()

    # --- 切点：工具配对平衡 + 保留尾部 ---
    def find_cut(self, session, context_window: int) -> tuple[int, str] | None:
        """找到安全切点，返回 (切点索引, first_kept_entry_id)；无安全切点返回 None。

        从当前 head 沿链往回累积 token，直到达到保留尾部的 token 预算；
        然后把切点吸附到最近的「安全边界」——绝不落在 tool 消息上
        （否则保留区段会以孤立的 tool 结果开头，破坏工具配对）。
        """
        path = session._path_to_head()
        if len(path) < self.min_keep_entries:
            return None  # 历史太短，不值得压

        retain_tokens = max(1, int(context_window * self.retain_ratio))
        acc = 0
        cut = 0
        for i in range(len(path) - 1, -1, -1):
            e = path[i]
            if e.type == "message":
                acc += self.meter.estimate_message(e.to_llm())
            if acc >= retain_tokens:
                cut = i
                break

        # 保证保留区段至少 min_keep_entries 条
        if len(path) - cut < self.min_keep_entries:
            cut = len(path) - self.min_keep_entries

        # 边界吸附：切点必须是一条非 tool 的 message；否则向前推进到下一个安全切点
        while cut < len(path) and (
            path[cut].type != "message" or path[cut].role == "tool"
        ):
            cut += 1

        if cut >= len(path) or len(path) - cut < self.min_keep_entries:
            return None  # 找不到安全切点（如全是 tool 消息）

        return cut, path[cut].id

    def _summary_input(self, session, cut_index: int) -> list[dict]:
        """构造喂给摘要调用的消息：被遮区间的 message 列表（含最近一次 prior 摘要）。

        为处理「多次压缩越攒越多」（总结的压缩）：若被遮区间里已有一次 compaction，
        把它的摘要作为前置上下文，让新摘要建立在其上。
        """
        path = session._path_to_head()
        region = [e.to_llm() for e in path[:cut_index] if e.type == "message"]
        prior = None
        for e in path[:cut_index]:
            if e.type == "compaction" and e.summary:
                prior = e.summary  # 取被遮区间里最近一次压缩的摘要
        if prior:
            region = [{"role": "user", "content": "此前已有一次压缩，其摘要为：" + prior}, *region]
        return region

    # --- 落地压缩（同步，返回 CompactionResult）---
    def compact_now(
        self,
        session,
        context_window: int,
        cut: tuple[int, str] | None = None,
    ) -> CompactionResult:
        """强制压缩一次。cut 可显式指定；缺省自动 find_cut。"""
        if cut is None:
            cut = self.find_cut(session, context_window)
        if cut is None:
            return CompactionResult(success=False, reason="no safe cut point")
        cut_index, first_kept_id = cut

        region = self._summary_input(session, cut_index)
        summary = self.summarizer(region, session.system_prompt)
        entry = session.append_compaction(summary, first_kept_id)
        return CompactionResult(
            success=True,
            summary=summary,
            first_kept_entry_id=first_kept_id,
            entry_id=entry.id,
            compacted_count=cut_index,
        )

    # --- 自动触发生成器（供 Agent 的生成器协作调用）---
    def compact_if_needed(self, session, context_window: int):
        """若到阈值则压缩，yield 事件，return CompactionResult；否则 return None。

        这是生成器：把「正在压缩 / 压缩完成」以事件形式发射给消费者，
        压缩动作本身由 compact_now 同步完成（MVP 不流式播报摘要）。
        """
        cut = self.find_cut(session, context_window)
        if cut is None:
            yield AgentEvent("compaction_skip", {"reason": "no safe cut point"})
            return None
        yield AgentEvent("compaction_start", {"compacted_count": cut[0]})
        result = self.compact_now(session, context_window, cut=cut)
        yield AgentEvent("compaction_end", {
            "success": result.success,
            "compacted_count": result.compacted_count,
            "first_kept_entry_id": result.first_kept_entry_id,
        })
        return result
