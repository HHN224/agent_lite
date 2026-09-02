"""上下文管理：计量（measure）+ 触发（should_compact）+ 溢出检测。

这是「上下文管理」的策略层入口（**阶段 A：计量 + 触发**）。
数据层（append-only 事件链 + compaction 模型）在 session.py，已就绪；
真正的压缩动作（摘要、边界吸附、保留尾部）属于**阶段 B**，本模块现在只
负责回答「现在要不要管」——即量出当前上下文占用，并判断是否达到压缩阈值。

设计对齐研究结论（docs/research/05-synthesis-and-recommendations.md §3 阶段 A）：
  - 计量与压缩引擎解耦（DSH 的 meter / compaction 拆分），可各自独立测试、独立替换。
  - 计量「启发式 + provider usage 锚点」：优先复用 provider 上报的真实 usage 作锚点，
    拿不到（usage=None）则整体回退到 chars/4 启发式。
  - 触发阈值 threshold_ratio 默认 0.8，做成配置项而非硬编码。

本模块不触碰具体 LLM SDK；只与 Session（build_llm_payload）协作。
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


def _usage_total(usage) -> int | None:
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
# 计量结果
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ContextPressure:
    """一次计量的结果：当前上下文的 token 占用与占窗口比例。"""

    total_tokens: int
    context_window: int
    ratio: float
    used_anchor: bool  # True 表示这次计量用了 provider 真实 usage 锚点，否则纯启发式


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

    def measure(
        self,
        session,
        context_window: int,
        usage=None,
    ) -> ContextPressure:
        """量出当前 session 的上下文占用。

        usage  可选：provider 最近一次上报的真实 usage 字典。若其能给出总 token
               数，则作为锚点直接采用（used_anchor=True）；否则整体回退启发式。
        """
        if context_window and context_window > 0:
            if usage is not None:
                total = _usage_total(usage)
                if total is not None:
                    return ContextPressure(
                        total_tokens=total,
                        context_window=context_window,
                        ratio=total / context_window,
                        used_anchor=True,
                    )
            total = self.estimate_payload(session.build_llm_payload())
            return ContextPressure(
                total_tokens=total,
                context_window=context_window,
                ratio=total / context_window,
                used_anchor=False,
            )
        # 未配置 context_window：无从判定比例，返回 0 占用
        return ContextPressure(
            total_tokens=0,
            context_window=0,
            ratio=0.0,
            used_anchor=False,
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
    def measure(self, session, context_window: int, usage=None) -> ContextPressure:
        return self.meter.measure(session, context_window, usage=usage)

    def requires_compaction(self, pressure: ContextPressure) -> bool:
        """按阈值判定一次计量结果是否到了该压缩的临界点。"""
        return pressure.ratio >= self.threshold_ratio

    def should_compact(self, session, context_window: int, usage=None) -> bool:
        """一键判定：当前上下文是否达到压缩阈值（阶段 A 只判定，不执行压缩）。"""
        pressure = self.measure(session, context_window, usage=usage)
        return self.requires_compaction(pressure)

    # --- 溢出检测（provider 报错时强制压缩的依据，阶段 D 用）---
    def is_context_overflow(self, error) -> bool:
        """判断一个 provider 错误是否表示「上下文超限」。"""
        text = str(error).lower()
        return any(re.search(pat, text) for pat in self._overflow_patterns)
