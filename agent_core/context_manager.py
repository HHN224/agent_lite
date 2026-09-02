"""上下文管理：计量（measure）+ 触发（should_compact）+ 溢出检测。

这是「上下文管理」的策略层入口（**阶段 A：计量 + 触发**）。
数据层（append-only 事件链 + compaction 模型）在 session.py，已就绪；
真正的压缩动作（摘要、边界吸附、保留尾部）属于**阶段 B**，本模块现在只
负责回答「现在要不要管」——即量出当前上下文占用，并判断是否达到压缩阈值。

设计对齐研究结论（docs/research/05-synthesis-and-recommendations.md §3 阶段 A）：
  - 计量与压缩引擎解耦（DSH 的 meter / compaction 拆分），可各自独立测试、独立替换。
  - 计量「启发式 + provider usage 锚点」：优先复用 provider 上报的真实 usage 作**锚点**，
    再对「锚点之后新增的内容」用启发式估算出**增量**，两者相加得到当前真实占用；
    拿不到 usage（usage=None）则整体回退到 chars/4 启发式。
  - 触发阈值 threshold_ratio 默认 0.8，做成配置项而非硬编码。

「锚点 + 增量」的含义：
  usage 锚点 = 上一次成功调用模型时，模型实际看到的上下文 token 数（真实、准）。
  增量       = 锚点之后会话又新增的内容（新条目 + 即将发送但这轮还没入历史的 pending 消息），
              用启发式估算。这样即便锚点早已落后于当前会话，也能算出「当前真实占用」，
              避免「锚点一直等于会话末尾、从不增长」导致的漏检。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass


# --------------------------------------------------------------------------- #
# 启发式估算（与 session.estimate_tokens 的 chars/4 思路一致，但作用于 message dict）
# --------------------------------------------------------------------------- #
def default_estimate_text(text: str) -> int:
    """字符数 / 4 的粗略 token 估算；空串返回 0，非空至少 1。"""
    if not text:
        return 0
    return max(1, len(text) // 4)


def _estimate_message(message: dict, estimate_text) -> int:
    """估算一条发给模型的消息 dict 的 token 数：content + tool_calls + 结构开销。"""
    total = estimate_text(message.get("content") or "")
    if message.get("tool_calls"):
        total += estimate_text(json.dumps(message["tool_calls"], ensure_ascii=False))
    # 每个消息的角色标签 / 字段分隔等结构性 token，给一个小的固定开销
    total += 4
    return max(0, total)


def usage_total(usage) -> int | None:
    """从 provider usage（OpenAI 风格 dict）里取出总 token 数；取不到返回 None。"""
    if not isinstance(usage, dict):
        return None
    if usage.get("total_tokens") is not None:
        return int(usage["total_tokens"])
    p = usage.get("prompt_tokens")
    c = usage.get("completion_tokens")
    if p is not None and c is not None:
        return int(p) + int(c)
    return None


# --------------------------------------------------------------------------- #
# 计量锚点：真实 usage 总量 + 它覆盖到的会话条目边界
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class UsageAnchor:
    """一次「计量锚点」：真实 usage 总量，以及当时会话推进到的 head 条目。

    head_id 用来计算增量：从该条目之后到当前 head 的内容是「锚点之后新增的」，
    需要用启发式估算补上，才是当前真实占用。
    """

    total_tokens: int
    head_id: str | None


# --------------------------------------------------------------------------- #
# 计量结果
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ContextPressure:
    """一次计量的结果：当前上下文的 token 占用与占窗口比例。"""

    total_tokens: int
    context_window: int
    ratio: float
    used_anchor: bool  # True 表示这次计量以真实 usage 锚点为基准，否则纯启发式
    anchor_total: int | None = None  # 用了锚点时的锚点基准值（否则 None）
    incremental: int = 0  # 锚点之上的启发式增量（新条目 + pending）


class TokenMeter:
    """计量器：可注入估算函数，对「要发给模型的 payload」做 token 计量。

    计量对象是 session.build_llm_payload()（system → 压缩 summary → 保留消息），
    即模型实际看到的 surface；因此包含 system prompt，比 session.token_count
    元数据（只累计追加条目）更贴近真实占用。
    """

    def __init__(self, estimate_text=default_estimate_text):
        self._estimate_text = estimate_text

    def estimate_message(self, message: dict) -> int:
        return _estimate_message(message, self._estimate_text)

    def estimate_payload(self, payload: list[dict]) -> int:
        return sum(self.estimate_message(m) for m in payload)

    def _estimate_entries_after(self, session, head_id: str | None) -> int:
        """估算从锚点 head_id 之后到当前 head 的条目 token 数（不包含 pending）。"""
        if not head_id:
            return 0
        path = session._path_to_head()  # root → head 的线性链
        start = None
        for i, e in enumerate(path):
            if e.id == head_id:
                start = i + 1
                break
        if start is None:
            # 锚点边界不在当前链上（如已被压缩），保守起见增量记 0，避免重复计数
            return 0
        return sum(
            self.estimate_message(e.to_llm())
            for e in path[start:]
            if e.type == "message"
        )

    def measure(
        self,
        session,
        context_window: int,
        anchor: UsageAnchor | None = None,
        pending_messages: list[dict] | None = None,
    ) -> ContextPressure:
        """量出当前 session 的上下文占用 = 锚点（真实 usage）+ 增量（启发式）。

        anchor            可选：provider 最近一次成功调用的真实 usage 锚点。
                         若有，则以其 total_tokens 为基准，再补「锚点之后的新增内容」；
                         若没有（usage=None / 第一次还没调用过），则整体回退启发式。
        pending_messages  可选：即将发送、但尚未 record 进 session 的消息（如本轮 user 输入）。
        """
        if not context_window or context_window <= 0:
            return ContextPressure(0, 0, 0.0, False, anchor_total=None, incremental=0)

        pending = pending_messages or []

        if anchor is not None and anchor.total_tokens is not None:
            base = anchor.total_tokens
            delta = self._estimate_entries_after(session, anchor.head_id)
            used_anchor = True
            anchor_total = base
        else:
            base = self.estimate_payload(session.build_llm_payload())
            delta = 0
            used_anchor = False
            anchor_total = None

        pending_total = sum(self.estimate_message(m) for m in pending)
        total = base + delta + pending_total
        return ContextPressure(
            total_tokens=total,
            context_window=context_window,
            ratio=total / context_window,
            used_anchor=used_anchor,
            anchor_total=anchor_total,
            incremental=delta + pending_total,
        )


# --------------------------------------------------------------------------- #
# 溢出检测（阶段 A：识别 provider 报「上下文超限」的错误串）
# --------------------------------------------------------------------------- #
_OVERFLOW_PATTERNS = [
    r"context\s*[_ ]?\s*length\s*[_ ]?\s*exceeded",
    r"context\s*[_ ]?\s*window",
    r"maximum\s*[_ ]?\s*context",
    r"too\s*[_ ]?\s*many\s*[_ ]?\s*tokens",
    r"token\s*[_ ]?\s*limit",
    r"context\s*[_ ]?\s*limit\s*[_ ]?\s*exceeded",
    r"context\s*[_ ]?\s*exceeded",
]


# --------------------------------------------------------------------------- #
# 策略层：触发判定 + 溢出检测（压缩动作留待阶段 B）
# --------------------------------------------------------------------------- #
class ContextManager:
    """上下文管理的策略入口：回答「现在要不要压缩」。

    持有阈值配置，把「量出占用」委托给 TokenMeter（计量）与「判定」分离。
    压缩动作（compact_now / compact_if_needed）在阶段 B 接入；在此只暴露
    should_compact / requires_compaction，供 Agent 在每轮 build payload 前调用。
    """

    def __init__(
        self,
        threshold_ratio: float = 0.8,
        meter: TokenMeter | None = None,
        overflow_patterns: list[str] | None = None,
    ):
        self.threshold_ratio = threshold_ratio
        self.meter = meter or TokenMeter()
        self._overflow_patterns = overflow_patterns or list(_OVERFLOW_PATTERNS)

    # --- 计量 / 触发 ---
    def measure(
        self,
        session,
        context_window: int,
        anchor: UsageAnchor | None = None,
        pending_messages: list[dict] | None = None,
    ) -> ContextPressure:
        return self.meter.measure(
            session, context_window, anchor=anchor, pending_messages=pending_messages
        )

    def requires_compaction(self, pressure: ContextPressure) -> bool:
        """按阈值判定一次计量结果是否到了该压缩的临界点。"""
        return pressure.ratio >= self.threshold_ratio

    def should_compact(
        self,
        session,
        context_window: int,
        anchor: UsageAnchor | None = None,
        pending_messages: list[dict] | None = None,
    ) -> bool:
        """一键判定：当前上下文是否达到压缩阈值（阶段 A 只判定，不执行压缩）。"""
        pressure = self.measure(
            session, context_window, anchor=anchor, pending_messages=pending_messages
        )
        return self.requires_compaction(pressure)

    # --- 溢出检测（provider 报错时强制压缩的依据，阶段 D 用）---
    def is_context_overflow(self, error) -> bool:
        """判断一个 provider 错误是否表示「上下文超限」。"""
        text = str(error).lower()
        return any(re.search(pat, text) for pat in self._overflow_patterns)
