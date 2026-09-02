"""上下文管理：计量（measure）+ 触发（should_compact）+ 溢出检测。

这是「上下文管理」的策略层入口（**阶段 A：计量 + 触发**）。
数据层（append-only 事件链 + compaction 模型）在 session.py，已就绪；
真正的压缩动作（摘要、边界吸附、保留尾部）属于**阶段 B**，本模块现在只
负责回答「现在要不要管」——即量出当前上下文占用，并判断是否达到压缩阈值。

计量采用**两变量**模型（对齐用户确认的设计）：
  session.usage      = 老历史的真实 token 基准（provider 真实上报 / 校准）
  session.new_usage  = 上次合并后、新增内容的估算 token（过程变量，每轮累积）
  当前占用 total     = usage + new_usage

「校准式」口径（用户选定）：每轮结束后，用 provider 本轮真实上报的 `last_usage`
直接校准 `session.usage`（它 = 模型本轮实际看到的输入，等于最精确的「老历史 + 本轮 input」）；
而 assistant 回复、工具结果这类「输出后新增」仍在下一轮并入 `new_usage`。
拿不到真实 usage（第一次还没调用过）时，`usage` 用整段 payload 启发式兜底。

本模块不触碰具体 LLM SDK；只与 Session（usage / new_usage / build_llm_payload）协作。
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


TOOL_PRUNE_MARKER = "\n\n[... tool result middle pruned ...]\n\n"


class ToolResultPruner:
    """把超长工具结果折叠成「叠加头尾」（head + marker + tail），省上下文。

    阈值与头尾长度都可配置（默认对齐 DSH pruner：threshold 8192 / head 4096 / tail 1024）。
    关键：截断只影响 content，**不触碰与 tool_call 的配对关系**（tool_call_id 不变），
    因此模型历史不会损坏。
    """

    def __init__(
        self,
        threshold_chars: int = 8192,
        head_chars: int = 4096,
        tail_chars: int = 1024,
    ):
        self.threshold_chars = threshold_chars
        self.head_chars = head_chars
        self.tail_chars = tail_chars

    def prune(self, content: str) -> tuple[str, bool]:
        """对一段工具输出做叠加头尾截断。返回 (处理后的内容, 是否被截断)。

        不超阈值则原样返回（pruned=False）；超阈值则 head + marker + tail（pruned=True）。
        """
        if content is None:
            return (content or "", False)
        if len(content) <= self.threshold_chars:
            return content, False
        head = content[: self.head_chars]
        tail = content[-self.tail_chars :]
        pruned = head + TOOL_PRUNE_MARKER + tail
        return pruned, True


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


def usage_input_total(usage) -> int | None:
    """从 provider usage 里取出**输入侧** token 数（prompt_tokens），用于校准 usage 基准。

    校准式口径下，session.usage 代表「模型实际看到的输入上下文」，应取 prompt_tokens
    （system + 历史 + 本轮 input），而不是 total_tokens（它已含 completion，若再用于
    校准会把输出侧内容算进 usage，导致与 new_usage 里的输出侧新增重复计数）。
    """
    if not isinstance(usage, dict):
        return None
    if usage.get("prompt_tokens") is not None:
        return int(usage["prompt_tokens"])
    return None


# --------------------------------------------------------------------------- #
# 计量结果
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ContextPressure:
    """一次计量的结果：当前上下文的 token 占用与占窗口比例。

    usage      老历史真实/校准基准（session.usage）
    new_usage  本轮估算的新增量（session.new_usage）
    """

    total_tokens: int
    context_window: int
    ratio: float
    usage: int = 0
    new_usage: int = 0


class TokenMeter:
    """计量器：可注入估算函数，对消息/一段 payload 做 token 估算。

    estimate_message / estimate_payload 供「新增内容」的估算用；
    measure() 则负责把 session.usage 与 session.new_usage 合成为当前占用。
    """

    def __init__(self, estimate_text=default_estimate_text):
        self._estimate_text = estimate_text

    def estimate_message(self, message: dict) -> int:
        return _estimate_message(message, self._estimate_text)

    def estimate_payload(self, payload: list[dict]) -> int:
        return sum(self.estimate_message(m) for m in payload)

    def measure(self, session, context_window: int) -> ContextPressure:
        """量出当前 session 的上下文占用 = session.usage + session.new_usage。

        若 usage 尚未校准（为 0 且无真实上报），用整段 payload 启发式兜底。
        """
        if not context_window or context_window <= 0:
            return ContextPressure(0, 0, 0.0, usage=session.usage, new_usage=session.new_usage)

        base = session.usage if session.usage > 0 else self.estimate_payload(
            session.build_llm_payload()
        )
        total = base + session.new_usage
        return ContextPressure(
            total_tokens=total,
            context_window=context_window,
            ratio=total / context_window,
            usage=session.usage,
            new_usage=session.new_usage,
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
    def measure(self, session, context_window: int) -> ContextPressure:
        return self.meter.measure(session, context_window)

    def requires_compaction(self, pressure: ContextPressure) -> bool:
        """按阈值判定一次计量结果是否到了该压缩的临界点。"""
        return pressure.ratio >= self.threshold_ratio

    def should_compact(self, session, context_window: int) -> bool:
        """一键判定：当前上下文是否达到压缩阈值（阶段 A 只判定，不执行压缩）。"""
        pressure = self.measure(session, context_window)
        return self.requires_compaction(pressure)

    # --- 溢出检测（provider 报错时强制压缩的依据，阶段 D 用）---
    def is_context_overflow(self, error) -> bool:
        """判断一个 provider 错误是否表示「上下文超限」。"""
        text = str(error).lower()
        return any(re.search(pat, text) for pat in self._overflow_patterns)
