"""工具输出管理：truncate_* 体系 + 全文写盘 + 模型见 preview（阶段 C / Phase 2）。

对齐 pi-mono 的 truncate.py 与 DSH pruner 的思路，把原来单一的工具结果截断逻辑
泛化成一族可组合的截断函数，并支持把超长结果原文写盘、模型只看到 preview + 路径。

关键原则：
  - 所有 truncate_* 只作用于 content 字符串，**不触碰 tool_call_id**，因此模型历史
    中 assistant 的 tool_calls 与 tool 结果的配对关系永远不被破坏。
  - **截断必须自曝**：任何被截断的文本都会带上「截了、从哪边截的、丢了多少」的提示，
    并给出把剩余内容读回来的具体做法。模型不该在毫不知情的情况下拿到残缺输出。
  - 写盘是可选的：store 为 None 时退化为纯截断，不影响上下文结构；但写盘位置必须
    在工作目录内（见 workspace_state_dir），否则模型读不回自己的工具输出。
  - 不做任何"为了兼容旧存档"的防御性代码——开发阶段允许破坏性修改，只要达成目的。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


# --------------------------------------------------------------------------- #
# 截断标记
# --------------------------------------------------------------------------- #
# 设计原则：**截断必须在文本里说出来**。模型上下文里如果只剩「内容突然断掉」，
# 它会以为那就是全部输出，然后基于残缺信息下结论、或者反复重跑同一个工具。
# 所以每个标记都要说清三件事：截了、从哪边截的、大概丢了多少。
def head_truncated_notice(kept: int, total: int) -> str:
    """保留开头（丢弃尾部）时的提示。"""
    return (
        f"\n\n[... OUTPUT TRUNCATED: showing only the FIRST {kept} of {total} characters; "
        f"the last {total - kept} characters were dropped from the end — "
        f"everything below is missing ...]\n\n"
    )


def tail_truncated_notice(kept: int, total: int) -> str:
    """保留结尾（丢弃头部）时的提示。"""
    return (
        f"\n\n[... OUTPUT TRUNCATED: showing only the LAST {kept} of {total} characters; "
        f"the first {total - kept} characters were dropped from the beginning — "
        f"everything above is missing ...]\n\n"
    )


def middle_truncated_notice(head: int, tail: int, total: int) -> str:
    """保留头尾（折叠中段）时的提示。"""
    return (
        f"\n\n[... OUTPUT TRUNCATED: showing only the FIRST {head} and the LAST {tail} "
        f"characters of {total}; {total - head - tail} characters in the middle "
        f"were dropped ...]\n\n"
    )


def json_truncated_notice(kept: int, total: int) -> str:
    """JSON 结构感知截断时的提示（键名保留，长值被切）。

    注意：JSON 截断后不再是合法 JSON，模型必须知道这一点，否则会照原样解析。
    """
    return (
        f"\n\n[... OUTPUT TRUNCATED: this JSON has been structurally shrunk — "
        f"long values were cut, only ~{kept} of {total} characters survive and "
        f"the result is no longer valid JSON ...]\n\n"
    )


# --------------------------------------------------------------------------- #
# 工具全文写盘位置：必须在工作目录内
# --------------------------------------------------------------------------- #
# 关键约束：read / write / edit 的 safe_path 只允许**工作目录内**的路径。
# 若把全文写到工作目录之外（例如项目根的 sessions/<id>/），模型无论如何都读不回
# 自己刚才的工具输出——它拿到一个路径，却没有任何工具能打开它。
# 因此统一写在 <workspace>/.agent-lite/tool-results/<session_id>/ 下：
#   · 位于工作目录内，read（含 offset/limit）可以正常分页读取；
#   · 单独一个隐藏文件夹，不污染用户的项目文件。
WORKSPACE_STATE_DIRNAME = ".agent-lite"
TOOL_RESULTS_DIRNAME = "tool-results"


def workspace_state_dir(workspace) -> Path:
    """工作目录内的 Agent 状态目录：``<workspace>/.agent-lite``。

    作为 ToolResultStore 的 base_dir 使用（全文写盘的根）。
    """
    return Path(workspace).resolve() / WORKSPACE_STATE_DIRNAME


def _safe_segment(value: str, max_len: int = 64) -> str:
    """把一段字符串收敛成安全的路径片段（防越界，保留字母数字与 -_）。"""
    safe = "".join(c for c in str(value) if c.isalnum() or c in "-_")[:max_len]
    return safe or "unknown"


# --------------------------------------------------------------------------- #
# truncate_* 基础函数（都只改 content，返回新的字符串）
# --------------------------------------------------------------------------- #
def truncate_head(text: str, max_chars: int, marker: str | None = None) -> str:
    """保留开头，丢弃尾部。未超阈值原样返回；截断时带上「截了多少」的提示。"""
    if text is None or len(text) <= max_chars:
        return text or ""
    return text[:max_chars] + (
        marker or head_truncated_notice(max_chars, len(text))
    )


def truncate_tail(text: str, max_chars: int, marker: str | None = None) -> str:
    """保留结尾，丢弃头部。未超阈值原样返回；截断时带上「截了多少」的提示。"""
    if text is None or len(text) <= max_chars:
        return text or ""
    return (marker or tail_truncated_notice(max_chars, len(text))) + text[-max_chars:]


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
    head = head_chars or int(max_chars * 0.8)
    tail = tail_chars or int(max_chars * 0.2)
    if head + tail > max_chars:
        tail = max_chars - head
    marker = marker or middle_truncated_notice(head, tail, len(text))
    return text[:head] + marker + text[-tail:]


def truncate_line(text: str, max_line_chars: int, max_lines: int | None = None) -> str:
    """对每行做截断，并可限制行数。长行折叠，不破坏行结构。"""
    if text is None:
        return ""
    lines = text.split("\n")
    if max_lines is not None and len(lines) > max_lines:
        keep = lines[:max_lines]
        dropped = len(lines) - max_lines
        return (
            "\n".join(keep)
            + f"\n[... OUTPUT TRUNCATED: showing only the first {max_lines} of "
              f"{len(lines)} lines; the remaining {dropped} lines were dropped ...]"
        )
    out = []
    for ln in lines:
        if len(ln) > max_line_chars:
            out.append(
                ln[:max_line_chars]
                + f"... [+{len(ln) - max_line_chars} chars truncated from this line]"
            )
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
    return shrunk + json_truncated_notice(len(shrunk), len(text))


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
    模型看到的内容末尾追加 full output path 与「怎么把剩下的读回来」的明确指引。
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

    def _continuation_hint(self, full_path: str | None) -> str:
        """截断之后必须给模型一条**能照着做**的出路，而不是只丢一个路径。

        有全文写盘：给出工作目录内相对写法 + read(offset/limit) 分页指引。
        没有写盘：明确说「剩下的没保存」，并给出缩小查询范围的做法。
        """
        if full_path:
            relative = self.store.relative(full_path) if self.store is not None else None
            where = f"{full_path}"
            if relative:
                where += f" (workspace-relative: {relative})"
            read_path = relative or full_path
            return (
                f"\n\n(full output saved to {where})\n"
                f"[The preview above is incomplete. Read the full text back with "
                f'read(path="{read_path}", offset=<line>, limit=<lines>) — do not guess '
                f"or re-run the same command blindly.]"
            )
        return (
            "\n\n[The preview above is truncated and the rest was NOT saved. "
            "Narrow the query (e.g. grep for the interesting part, head/tail, "
            "sed -n 'start,endp') instead of re-running the same call.]"
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
            preview = preview + self._continuation_hint(full_path)
        return TruncationResult(preview, truncated, full_length, full_path)


class ToolResultStore:
    """把超长工具结果原文写盘，模型只见 preview + 路径。

    base_dir 为写盘根，应取 ``workspace_state_dir(workspace)``（即
    ``<workspace>/.agent-lite``）——**必须位于模型的工作目录内**，否则模型手里的
    read 工具（safe_path 限制在工作目录内）永远打不开这个路径。

    每条结果的落盘位置：
        <base_dir>/tool-results/<session_id>/<tool_call_id>.txt
    没有 session_id 时退化为 <base_dir>/tool-results/<tool_call_id>.txt。

    workspace 可选：提供时额外给出「相对工作目录」的路径写法，便于模型直接喂给 read。
    """

    def __init__(self, base_dir: Path, workspace: Path | None = None):
        self.base_dir = Path(base_dir)
        self.workspace = Path(workspace).resolve() if workspace is not None else None

    def _dir(self, session_id: str | None = None) -> Path:
        d = self.base_dir / TOOL_RESULTS_DIRNAME
        if session_id:
            d = d / _safe_segment(session_id)
        return d

    def _ensure_dir(self, d: Path) -> Path:
        d.mkdir(parents=True, exist_ok=True)
        # 写盘根自带 .gitignore，避免这些中间产物被用户的项目版本控制收进去
        root = self.base_dir / ".gitignore"
        if not root.exists():
            try:
                root.write_text("*\n", encoding="utf-8")
            except OSError:
                pass
        return d

    def write(self, session_id: str, tool_call_id: str, content: str) -> str:
        """全文写盘，返回绝对路径（模型可直接用 read 读取）。"""
        d = self._ensure_dir(self._dir(session_id))
        path = d / f"{_safe_segment(tool_call_id, 80)}.txt"
        path.write_text(content, encoding="utf-8")
        return str(path.resolve())

    def relative(self, path: str | Path) -> str | None:
        """把写盘路径折算成「相对工作目录」的写法；不在工作目录内或未知工作目录时为 None。"""
        if self.workspace is None:
            return None
        try:
            return Path(path).resolve().relative_to(self.workspace).as_posix()
        except ValueError:
            return None
