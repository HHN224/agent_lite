"""工具输出管理：truncate_* 体系 + 全文写盘 + 模型见 preview（阶段 C / Phase 2）。

对齐 pi-mono 的 truncate.py 与 DSH pruner 的思路，把原来单一的工具结果截断逻辑
泛化成一族可组合的截断函数，并支持把超长结果原文写盘、模型只看到 preview + 路径。

关键原则：
  - 所有 truncate_* 只作用于 content 字符串，**不触碰 tool_call_id**，因此模型历史
    中 assistant 的 tool_calls 与 tool 结果的配对关系永远不被破坏。
  - 写盘是可选的：store 为 None 时退化为纯截断，不影响上下文结构。
  - 不做任何"为了兼容旧存档"的防御性代码——开发阶段允许破坏性修改，只要达成目的。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


# --------------------------------------------------------------------------- #
# 截断标记
# --------------------------------------------------------------------------- #
TOOL_TRUNCATE_MARKER = "\n\n[... tool result middle pruned ...]\n\n"


# --------------------------------------------------------------------------- #
# truncate_* 基础函数（都只改 content，返回新的字符串）
# --------------------------------------------------------------------------- #
def truncate_head(text: str, max_chars: int, marker: str | None = None) -> str:
    """保留开头，丢弃尾部。未超阈值原样返回。"""
    if text is None or len(text) <= max_chars:
        return text or ""
    marker = marker or "\n\n[... tool result tail truncated ...]\n\n"
    return text[:max_chars] + marker


def truncate_tail(text: str, max_chars: int, marker: str | None = None) -> str:
    """保留结尾，丢弃头部。未超阈值原样返回。"""
    if text is None or len(text) <= max_chars:
        return text or ""
    marker = marker or "\n\n[... tool result head truncated ...]\n\n"
    return marker + text[-max_chars:]


def truncate_head_tail(
    text: str,
    max_chars: int,
    head_chars: int | None = None,
    tail_chars: int | None = None,
    marker: str | None = None,
) -> str:
    """叠加头尾：保留 head + marker + tail。head/tail 各自不超过 max_chars 的一部分。

    max_chars 为总预算；head_chars / tail_chars 缺省取 DSH 默认（4096 / 1024），
    但会保证两者之和不超过 max_chars。
    """
    if text is None or len(text) <= max_chars:
        return text or ""
    marker = marker or TOOL_TRUNCATE_MARKER
    head = head_chars or int(max_chars * 0.8)
    tail = tail_chars or int(max_chars * 0.2)
    if head + tail > max_chars:
        tail = max_chars - head
    return text[:head] + marker + text[-tail:]


def truncate_line(text: str, max_line_chars: int, max_lines: int | None = None) -> str:
    """对每行做截断，并可限制行数。长行折叠，不破坏行结构。"""
    if text is None:
        return ""
    lines = text.split("\n")
    if max_lines is not None and len(lines) > max_lines:
        keep = lines[:max_lines]
        return "\n".join(keep) + f"\n[... {len(lines) - max_lines} lines truncated ...]"
    out = []
    for ln in lines:
        if len(ln) > max_line_chars:
            out.append(ln[: max_line_chars] + "...")
        else:
            out.append(ln)
    return "\n".join(out)


def truncate_json(text: str, max_chars: int, max_key_len: int = 32) -> str:
    """对 JSON 做结构感知截断：保留键名全名，值截断，避免破坏 JSON 语法。

    若无法解析为 JSON，退化为 truncate_head_tail。
    """
    if text is None or len(text) <= max_chars:
        return text or ""
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return truncate_head_tail(text, max_chars)

    def shrink(v, budget: int):
        # 预算按顶层条目分摊，避免单个长值占满导致其余键被丢弃
        if isinstance(v, dict):
            if not v:
                return {}
            per = max(1, budget // len(v))
            out = {}
            for k, val in v.items():
                if len(str(k)) > max_key_len:
                    k = k[:max_key_len] + "..."
                out[k] = shrink(val, per)
            return out
        if isinstance(v, list):
            if not v:
                return []
            per = max(1, budget // len(v))
            return [shrink(item, per) for item in v[:max(1, budget // max(1, per))]]
        s = json.dumps(v, ensure_ascii=False)
        if len(s) > budget:
            return s[:budget] + "..."
        return v

    shrunk = json.dumps(shrink(data, int(max_chars * 0.9)), ensure_ascii=False)
    return shrunk + TOOL_TRUNCATE_MARKER


# --------------------------------------------------------------------------- #
# 截断策略类：按工具名选择截断函数
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class TruncationResult:
    """一次截断的结果。content 为要回填给模型的预览文本。"""

    content: str
    truncated: bool
    full_length: int
    full_output_path: str | None = None


class ToolOutputTruncator:
    """把超长工具结果在回填前截断（可叠全文写盘）。

    truncator(工具名) -> 截断函数，缺省按工具名选择：
      - bash          -> truncate_head_tail
      - read          -> truncate_head（文本） / truncate_json（JSON）
      - grep / ls / find -> truncate_head
      其他             -> truncate_head_tail

    store 为可选的 ToolResultStore；提供时，超阈值结果会全文写盘，
    模型看到的内容末尾追加 full output path。
    """

    def __init__(
        self,
        threshold_chars: int = 8192,
        head_chars: int = 4096,
        tail_chars: int = 1024,
        store: ToolResultStore | None = None,
    ):
        self.threshold_chars = threshold_chars
        self.head_chars = head_chars
        self.tail_chars = tail_chars
        self.store = store

    def _truncate_for(self, tool_name: str):
        if tool_name == "bash":
            return lambda t: truncate_head_tail(
                t, self.threshold_chars, self.head_chars, self.tail_chars
            )
        if tool_name in ("read", "grep", "ls", "find"):
            return lambda t: truncate_head(t, self.threshold_chars)
        return lambda t: truncate_head_tail(
            t, self.threshold_chars, self.head_chars, self.tail_chars
        )

    def truncate(
        self,
        content: str,
        tool_name: str = "",
        tool_call_id: str | None = None,
        session_id: str | None = None,
    ) -> TruncationResult:
        if content is None:
            return TruncationResult("", False, 0)

        full_length = len(content)
        fn = self._truncate_for(tool_name)
        truncated = full_length > self.threshold_chars

        preview = content
        full_path = None
        if truncated:
            preview = fn(content)
            if self.store is not None and session_id and tool_call_id:
                full_path = self.store.write(session_id, tool_call_id, content)
                preview = preview + f"\n\n(full output saved to {full_path})"
        return TruncationResult(preview, truncated, full_length, full_path)


class ToolResultStore:
    """把超长工具结果原文写盘，模型只见 preview + 路径。

    base_dir 为 **session 粒度**目录（如 sessions/<session_id>）；
    写盘路径为 base_dir/tool-results/<tool_call_id>.txt。
    """

    def __init__(self, base_dir: Path):
        self.base_dir = Path(base_dir)

    def _dir(self) -> Path:
        return self.base_dir / "tool-results"

    def write(self, session_id: str, tool_call_id: str, content: str) -> str:
        d = self._dir()
        d.mkdir(parents=True, exist_ok=True)
        safe_name = "".join(c for c in tool_call_id if c.isalnum() or c in "-_")[:80] or "unknown"
        path = d / f"{safe_name}.txt"
        path.write_text(content, encoding="utf-8")
        return str(path)
