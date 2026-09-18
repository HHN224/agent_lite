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

摘要调用**必须看起来不像一段还在进行的会话**（这是被真实 session 打过脸的教训）：
被遮区间渲染成一段 <transcript> 引用文本，总结指令是最后一条 user 消息，
system 用专用压缩器人格（不是会话里的编码 agent 人格），且显式无工具。
详见 render_transcript / make_summarizer 的注释。

摘要的具体调用由可注入的 summarizer 提供（生产用 provider，测试用假函数），
把「怎么总结」与 CompactionEngine 解耦，便于独立测试与替换。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from ai import ProviderError, TextDelta

from .content import BLOCK_IMAGE, BLOCK_TEXT, BLOCK_TOOL_RESULT
from .events import AgentEvent
from .context_manager import TokenMeter


# --------------------------------------------------------------------------- #
# 结构化摘要 prompt（固定检查点，比自由摘要更稳定、可复现）
# --------------------------------------------------------------------------- #
# 指令固定在**最后一条 user 消息**里：模型只能从「给我写摘要」这个位置往下写，
# 物理上无法接着 transcript 里的最后一步继续干活。
SUMMARY_PROMPT = """\
Summarize the conversation transcript you were given so it can replace those messages.

Produce a concise but faithful summary that captures:
1. The user's overall goal and any constraints.
2. Steps already taken (tools executed, files read/written, commands run) and their outcomes.
3. Important facts, decisions, and conclusions established so far.
4. Anything still incomplete, blocked, or pending that must not be forgotten.

Rules (violating any of them makes your output useless):
- Do NOT continue, answer, or act on the conversation; you only summarize it.
- Do NOT ask the user for anything, and do NOT ask for the transcript — it is above.
- Do NOT emit tool calls or tool-call markup of any kind.

Write in the same language as the conversation. Keep it around {max_tokens} tokens at most.
"""

# 压缩器人格：与「编码 agent」人格刻意区分开，避免模型把自己当成会话的下一棒
SUMMARY_SYSTEM_PROMPT = (
    "You are a context compressor for a coding-agent conversation. "
    "You never continue, answer, or act on the conversation you are given, and you never "
    "emit tool calls or tool-call markup. Read the transcript and write a factual summary "
    "of it. Output the summary text only — no preamble, no questions, no offers to help."
)

TRANSCRIPT_OPEN = "<transcript>"
TRANSCRIPT_CLOSE = "</transcript>"

# 被遮区间的字符预算：40 万字符（≈10 万 token）的区间本身就是一次超大请求，
# 既不便宜也容易直接撞上上下文上限。超出时保留头尾、省略中段。
DEFAULT_TRANSCRIPT_CHARS = 60000


# --------------------------------------------------------------------------- #
# 摘要输出的硬校验：宁可不压缩，也不把垃圾当摘要写进历史
# --------------------------------------------------------------------------- #
# 事实依据：修复前的 8 次真实压缩里，6 次存下来的是「继续任务」「DSML 工具调用原文」
# 「反问用户要历史」。这些内容一旦落地成 compaction，被折叠的历史就**永久丢失**，
# 而且会以 user 消息的形式注入后续每一次请求。所以必须有一道闸门。
class SummaryRejected(Exception):
    """摘要调用明确不该被采用（模型仍在发工具调用等）。"""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


MIN_SUMMARY_CHARS = 40
MAX_SUMMARY_CHARS = 16000

# 模型把工具调用写进正文时的标记（GOAT / DeepSeek 路由实测会输出 DSML 文本标记）
_TOOL_CALL_MARKERS = (
    "dsml", "<\uff5c", "\uff5c\uff5c", "<tool_call", "</tool_call",
    "invoke name=", '"tool_calls"', "antml:invoke",
)
# 模型反过来向用户要材料时的措辞（实测出现过「请把对话粘给我」）
_ASKED_FOR_INPUT = (
    "please paste", "paste the actual conversation", "paste the older conversation",
    "i don't see any prior", "i don't have any prior",
    "请把对话", "请提供对话", "看不到任何历史", "没有可总结的对话",
)


def validate_summary(text: str) -> tuple[bool, str]:
    """判断一段输出能不能当摘要用。返回 (ok, reason)。

    先查「特征很明确」的垃圾（工具调用标记 / 反问用户），再查长度：
    这样一段很短的 DSML 片段会被报成 tool-call markup，而不是含糊的 too short。
    """
    clean = (text or "").strip()
    lowered = clean.lower()
    for marker in _TOOL_CALL_MARKERS:
        if marker in lowered:
            return False, f"summary contains tool-call markup ({marker!r})"
    for phrase in _ASKED_FOR_INPUT:
        if phrase in lowered:
            return False, "summary asks the user for the transcript instead of summarizing it"
    if len(clean) < MIN_SUMMARY_CHARS:
        return False, f"summary too short ({len(clean)} chars < {MIN_SUMMARY_CHARS})"
    return True, ""


def cap_summary(text: str, max_chars: int = MAX_SUMMARY_CHARS) -> str:
    """摘要过长时按字符截断（长摘要仍然有用，所以截断而不是丢弃）。"""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n\n[... summary truncated by the agent ...]"


def sanitize_summary(text: str) -> tuple[str, bool]:
    """从模型输出里救回可用的摘要正文；返回 (清理后的文本, 是否清理过)。

    为什么需要：这个路由会把工具调用序列化成 DSML 文本混在正文里（我在两次独立探针里
    都撞到过）。以前只要检测到标记就整条丢弃 —— 但实测那种输出往往是「一段真摘要 +
    一段工具调用尾巴」，整条扔掉等于白花一次调用，还让压缩一直失败。现在只砍掉标记
    及其之后的内容：前面那段正文仍然可用。
    """
    clean = (text or "").strip()
    cut = len(clean)
    lowered = clean.lower()
    for marker in _TOOL_CALL_MARKERS:
        index = lowered.find(marker)
        if index != -1:
            cut = min(cut, index)
    if cut >= len(clean):
        return clean, False
    # 截断处可能留下半个标签，一并去掉
    salvaged = re.sub(r"<[^>]*$", "", clean[:cut]).strip()
    return salvaged, True


def mechanical_digest(region: list[dict]) -> str:
    """不依赖模型的机械摘要：模型摘要连续失败时用它兜底，保证上下文一定压得下去。

    只摘「机械就能拿到」的事实：用户目标/要求、触及的文件、跑过的命令、最后的结论。
    它比模型摘要粗，所以明确标注来源；但它永不失败、不花一次 API 调用、也不会把
    任务卡住（真实事故：模型摘要一直超时，压缩 0 次成功，会话涨到 328 条）。
    """
    users: list[str] = []
    assistants: list[str] = []
    files: list[str] = []
    commands: list[str] = []

    for message in region:
        role = message.get("role")
        text = _render_content(message.get("content")).strip()
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            name = function.get("name")
            try:
                args = json.loads(function.get("arguments") or "{}")
            except (ValueError, TypeError):
                args = {}
            if not isinstance(args, dict):
                continue
            if name in ("read", "write", "edit") and args.get("path"):
                files.append(f"{name} {args['path']}")
            elif name == "bash" and args.get("command"):
                commands.append(str(args["command"]).strip().splitlines()[0][:160])
        if role == "user" and text:
            users.append(text.replace("\n", " ")[:240])
        elif role == "assistant" and text:
            assistants.append(text.replace("\n", " ")[:240])

    def unique(items: list[str], limit: int) -> list[str]:
        seen, out = set(), []
        for item in items:
            if item not in seen:
                seen.add(item)
                out.append(item)
        return out[-limit:]

    parts = [
        "[机械压缩摘要] 模型摘要连续失败，以下为 agent 从被折叠区间机械提取的事实"
        f"（共 {len(region)} 条消息）。细节可能不完整，如需原始内容请查看会话存档。",
    ]
    goals = unique(users, 6)
    if goals:
        parts.append("\n## 用户目标与要求\n" + "\n".join(f"- {g}" for g in goals))
    touched = unique(files, 20)
    if touched:
        parts.append("\n## 触及的文件\n" + "\n".join(f"- {f}" for f in touched))
    ran = unique(commands, 12)
    if ran:
        parts.append("\n## 执行过的命令（截断）\n" + "\n".join(f"- {c}" for c in ran))
    conclusions = unique(assistants, 3)
    if conclusions:
        parts.append("\n## 最近的结论/说明\n" + "\n".join(f"- {c}" for c in conclusions))
    return cap_summary("\n".join(parts), max_chars=6000)


@dataclass
class CompactionResult:
    """一次压缩的结果。success=False 表示未找到安全切点或摘要失败，未落地任何东西。"""

    success: bool
    summary: str = ""
    first_kept_entry_id: str | None = None
    entry_id: str | None = None
    compacted_count: int = 0
    reason: str = ""
    folded_messages: int = 0
    summary_chars: int = 0
    fallback: bool = False   # True = 这次用的是机械摘要，不是模型摘要


# --------------------------------------------------------------------------- #
# 被遮区间 -> 引用文本（transcript）
# --------------------------------------------------------------------------- #
def _render_content(content) -> str:
    """把一条消息的 content 渲染成纯文本。

    图片只留占位符（绝不把 base64 塞进摘要请求），展示用的 thinking 块不进摘要材料。
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind in (BLOCK_TEXT, BLOCK_TOOL_RESULT):
            parts.append(block.get("text") or block.get("content") or "")
        elif kind == BLOCK_IMAGE:
            parts.append("[image omitted]")
    return "".join(parts)


def _render_message(message: dict) -> str:
    """把一条消息渲染成带角色标签的文本行。

    工具调用**渲染成文本**而不是保留 tool_calls 字段：发出去的摘要请求里因此不存在
    任何 assistant.tool_calls / role=tool 消息，模型看不到「刚调用完工具、等着下一步」
    的形状（那正是它接着说下去的诱因）。
    """
    role = message.get("role") or "?"
    text = _render_content(message.get("content")).strip()
    if role == "tool":
        head = f"[tool result id={message.get('tool_call_id') or '?'}]"
    else:
        head = f"[{role}]"
    lines = [f"{head} {text}".rstrip()]
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        lines.append(f"[tool call] {function.get('name', '?')}({function.get('arguments', '')})")
    return "\n".join(lines)


def render_transcript(
    messages: list[dict], max_chars: int | None = DEFAULT_TRANSCRIPT_CHARS
) -> str:
    """把被遮区间渲染成一段引用文本；超预算时保留头尾、省略中段（按消息切，不切半条）。

    max_chars 默认就是预算（而不是无限）：真实存档里出现过 40 万字符的被遮区间，
    忘了传预算的调用方会直接把整段历史塞进一次请求。
    显式传 None 表示「不限长度」。
    """
    blocks = [_render_message(m) for m in messages]
    blocks = [b for b in blocks if b.strip()]
    if max_chars is None or sum(len(b) for b in blocks) <= max_chars:
        return "\n\n".join(blocks)

    head_budget = int(max_chars * 0.6)
    head: list[str] = []
    used = 0
    for block in blocks:
        if head and used + len(block) > head_budget:
            break
        head.append(block)
        used += len(block) + 2

    tail: list[str] = []
    used = 0
    for block in reversed(blocks[len(head):]):
        if tail and used + len(block) > max_chars - head_budget:
            break
        tail.append(block)
        used += len(block) + 2
    tail.reverse()

    omitted = len(blocks) - len(head) - len(tail)
    if omitted <= 0:
        return "\n\n".join([*head, *tail])
    marker = (
        f"[... TRANSCRIPT TRUNCATED: {omitted} messages from the middle were omitted "
        f"to stay within the summary budget ...]"
    )
    return "\n\n".join([*head, marker, *tail])


# --------------------------------------------------------------------------- #
# 摘要器工厂：生产用 provider 的独立、无工具、确定性调用
# --------------------------------------------------------------------------- #
# 输出额度与「摘要该多长」必须分开，这是实测踩出来的坑：
# 这个路由在写正文前会先花掉几千 token 的 reasoning。如果把 API 的 max_tokens 直接设成
# 「摘要目标长度」（默认 2000），模型会把额度全烧在思考上、正文一个字都写不出来，
# 服务端返回 finish_reason=length —— 用工信事故 session 的真实区间实测 4/4 次失败，
# 而只把额度提到 8000 就立刻成功（13.2s 写出 3237 字摘要）。
MIN_SUMMARY_OUTPUT_TOKENS = 6000
SUMMARY_OUTPUT_MULTIPLIER = 4


def make_summarizer(
    provider,
    model: str,
    max_tokens: int = 2000,
    max_transcript_chars: int = DEFAULT_TRANSCRIPT_CHARS,
    output_tokens: int | None = None,
):
    """构造一个「独立的 LLM 摘要调用」的 summarizer。

    返回的 summarizer(region_messages) -> str，请求形状固定为三条消息：

        [system] 专用压缩器人格（**不使用会话里的编码 agent 人格**）
        [user]   <transcript> 被遮区间的文本渲染 </transcript>
        [user]   总结指令（放在最后：模型只能接着「写摘要」，接不了任务）

    max_tokens 只是**提示词里的目标**（"keep it around N tokens"）；
    真正发给 API 的输出上限是 output_tokens，默认 max(max_tokens*4, 6000) ——
    必须给 reasoning 留出空间，否则正文永远写不出来（见上面的实测注释）。
    另外显式 temperature=0、工具列表为空（provider 层会因此整条不发 tools 字段）。
    """
    from ai import ProviderError, ToolCall, ToolCallProgress

    api_output_tokens = output_tokens or max(
        max_tokens * SUMMARY_OUTPUT_MULTIPLIER, MIN_SUMMARY_OUTPUT_TOKENS
    )

    def summarize(region_messages: list[dict]) -> str:
        transcript = render_transcript(region_messages, max_transcript_chars)
        msgs: list[dict] = [
            {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
            {"role": "user", "content": f"{TRANSCRIPT_OPEN}\n{transcript}\n{TRANSCRIPT_CLOSE}"},
            {"role": "user", "content": SUMMARY_PROMPT.format(max_tokens=max_tokens)},
        ]
        parts: list[str] = []
        try:
            for event in provider.stream(msgs, [], model, temperature=0.0,
                                         max_tokens=api_output_tokens):
                if isinstance(event, (ToolCall, ToolCallProgress)):
                    # 到这一步还发工具调用，说明模型仍在「接着干活」而不是总结：
                    # 直接判定这次摘要不可用，交给上层拒绝落地（绝不静默丢弃后当真摘要用）。
                    raise SummaryRejected(
                        "summarizer still tried to call a tool instead of summarizing"
                    )
                if isinstance(event, TextDelta):
                    parts.append(event.content)
        except ProviderError as exc:
            # finish_reason=length：正文其实已经流出来了，只是被截断。
            # 截断的摘要仍然有用（后面还有校验把关），没必要时整条丢掉。
            if getattr(exc, "code", "") != "length" or not "".join(parts).strip():
                raise
        return "".join(parts).strip()

    summarize.output_tokens = api_output_tokens
    return summarize


# --------------------------------------------------------------------------- #
# 压缩引擎
# --------------------------------------------------------------------------- #
class CompactionEngine:
    """真正的压缩动作：选切点 + 保留尾部 + 独立摘要 + 落地 compaction。

    summarizer   可注入的摘要函数：callable(region_messages) -> str
    retain_ratio 保留尾部的窗口占比（默认 0.16，DSH）
    meter        用于估算切点 token 的量计器（复用 TokenMeter）

    失败退避（真实事故的直接教训，见 sessions/3dcba68b1cfb）：
    压缩失败时计量**必须保留**（否则该压的不再压），但这会带来一个新问题 ——
    needs_compaction 一直为真，于是每个工具轮次都再发一次摘要请求。实测一次任务里
    摘要调用被重试了 7 次，而每次最长要等 60s 超时，用户看到的就是「任务被卡住」。
    所以失败之后必须有退避：上下文再增长若干条消息前不重试；连续失败到上限就**彻底停手**
    —— 压不动就别压了，绝不能让压缩把主任务拖死。
    """

    def __init__(
        self,
        summarizer,
        retain_ratio: float = 0.16,
        min_keep_entries: int = 2,
        meter: TokenMeter | None = None,
        failure_backoff_messages: int = 20,
        max_consecutive_failures: int = 3,
        fallback: str = "digest",
    ):
        self.summarizer = summarizer
        self.retain_ratio = retain_ratio
        self.min_keep_entries = min_keep_entries
        self.meter = meter or TokenMeter()
        self.failure_backoff_messages = failure_backoff_messages
        self.max_consecutive_failures = max_consecutive_failures
        self.fallback = fallback  # "digest"：模型摘要彻底失败后机械兜底；"none"：只拒绝
        self._reset_failure_state()

    # --- 失败退避状态 ---
    def _reset_failure_state(self):
        self._consecutive_failures = 0
        self._last_failure_reason = ""
        self._retry_after_messages = 0
        self._disabled_reason = ""
        self._pause_announced = False
        self._session_id = None

    def _sync_session(self, session):
        """换会话就重置退避状态：上一个会话的连续失败不该污染新会话。"""
        if getattr(session, "session_id", None) != self._session_id:
            self._reset_failure_state()
            self._session_id = getattr(session, "session_id", None)

    def _pause_reason(self, session) -> str:
        """现在该不该「先别试」；返回原因，空串表示可以尝试。

        fallback="digest" 时，连续失败到上限**不算停手**：改成走机械摘要继续压，
        所以不返回停手原因（否则上下文会一直涨下去）。
        """
        self._sync_session(session)
        if self._disabled_reason and self.fallback != "digest":
            return self._disabled_reason
        if self._consecutive_failures and session.message_count < self._retry_after_messages:
            remaining = self._retry_after_messages - session.message_count
            return (f"上次压缩失败（{self._last_failure_reason}），"
                    f"退避中：再增长约 {remaining} 条消息后重试")
        return ""

    def paused_reason(self, session) -> str:
        """对外可见的暂停原因（供测试与 UI 使用）。"""
        return self._pause_reason(session)

    def _note_failure(self, session, reason: str):
        self._consecutive_failures += 1
        self._last_failure_reason = reason
        self._retry_after_messages = session.message_count + self.failure_backoff_messages
        self._pause_announced = False  # 新的退避窗口，允许再播报一次
        if self._consecutive_failures >= self.max_consecutive_failures:
            self._disabled_reason = (
                f"压缩连续失败 {self._consecutive_failures} 次（最后原因：{reason}），"
                "已停止自动压缩"
            )
        session.runtime_status["compaction"] = {
            "consecutive_failures": self._consecutive_failures,
            "last_failure": reason,
            "retry_after_messages": self._retry_after_messages,
            "disabled": bool(self._disabled_reason),
        }

    def _note_success(self, session, via_fallback: bool = False):
        """记录一次成功。

        via_fallback=True 时**不清空「模型摘要不可用」的状态** —— 否则下一轮又会去试模型、
        再超时三次、再兜底，白白烧掉三次调用（实测 12 轮里因此多花了 6 次）。
        机械摘要成功只是说明上下文压下去了，不代表模型摘要恢复了；要退出兜底模式，
        得靠一次真正的模型摘要成功（例如用户手动 /compact）。
        """
        if not via_fallback:
            self._consecutive_failures = 0
            self._last_failure_reason = ""
            self._disabled_reason = ""
        self._retry_after_messages = 0
        self._pause_announced = False
        session.runtime_status["compaction"] = {
            "consecutive_failures": self._consecutive_failures,
            "last_failure": self._last_failure_reason,
            "disabled": bool(self._disabled_reason),
            "fallback_mode": bool(self._disabled_reason) and self.fallback == "digest",
        }

    # --- 切点：工具配对平衡 + 保留尾部 ---
    def find_cut(self, session, context_window: int) -> tuple[int, str] | None:
        """找到安全切点，返回 (切点索引, first_kept_entry_id)；无安全切点返回 None。

        从当前 head 沿链往回累积 token，直到达到保留尾部的 token 预算；
        然后把切点吸附到最近的「安全边界」——绝不落在 tool 消息上
        （否则保留区段会以孤立的 tool 结果开头，破坏工具配对）。

        关键约束：**没有任何消息需要折叠时必须返回 None**。
        历史本身还没超过保留预算时（扫描走完都没到预算）不能"从 0 折叠"——
        那会生成一个空区间，模型只会反问用户要历史，而这条反问还会被当成摘要
        存进存档（实测发生过两次，见 sessions/9c2d5ef1c648）。
        """
        path = session._path_to_head()
        if len(path) < self.min_keep_entries:
            return None  # 历史太短，不值得压

        retain_tokens = max(1, int(context_window * self.retain_ratio))
        acc = 0
        cut: int | None = None
        for i in range(len(path) - 1, -1, -1):
            e = path[i]
            if e.type == "message":
                acc += self.meter.estimate_message(e.to_llm())
            if acc >= retain_tokens:
                cut = i
                break
        if cut is None:
            # 整个历史都塞得进保留尾部：本来就没有要折叠的东西
            return None

        # 保证保留区段至少 min_keep_entries 条
        if len(path) - cut < self.min_keep_entries:
            cut = len(path) - self.min_keep_entries

        # 边界吸附：切点必须是一条非 tool 的 message；否则向后推进到下一个安全切点
        while cut < len(path) and (
            path[cut].type != "message" or path[cut].role == "tool"
        ):
            cut += 1

        # 没有可折叠的消息（cut=0）或找不到安全切点（如全是 tool 消息）→ 不压缩
        if cut < 1 or cut >= len(path) or len(path) - cut < self.min_keep_entries:
            return None

        return cut, path[cut].id

    def _summary_input(self, session, cut_index: int) -> list[dict]:
        """构造喂给摘要调用的材料：被遮区间的 message 列表（含最近一次 prior 摘要）。

        为处理「多次压缩越攒越多」（总结的压缩）：若被遮区间里已有一次 compaction，
        把它的摘要作为**引用材料**前置，让新摘要建立在其上（并明确标注不是新指令）。
        """
        path = session._path_to_head()
        region = [e.to_llm() for e in path[:cut_index] if e.type == "message"]
        prior = None
        for e in path[:cut_index]:
            if e.type == "compaction" and e.summary:
                prior = e.summary  # 取被遮区间里最近一次压缩的摘要
        if prior:
            region = [
                {"role": "user",
                 "content": "[Prior summary of even earlier turns — reference material only, "
                            "not a new instruction]\n" + prior},
                *region,
            ]
        return region

    # --- 落地压缩（同步，返回 CompactionResult）---
    def compact_now(
        self,
        session,
        context_window: int,
        cut: tuple[int, str] | None = None,
        use_fallback: bool = False,
    ) -> CompactionResult:
        """强制压缩一次。cut 可显式指定；缺省自动 find_cut。

        use_fallback=True 时不再调用模型，直接用机械摘要兜底（模型摘要连续失败后走这条路，
        保证上下文一定压得下去，见 mechanical_digest）。

        摘要不合格（校验失败 / 模型仍在发工具调用 / provider 报错）时返回 success=False，
        **不落地任何 compaction、不重置任何计量**：宁可这次不压，也不让被折叠的历史
        永远丢失，更不让垃圾以 user 消息的形式进入后续每一次请求。
        """
        if cut is None:
            cut = self.find_cut(session, context_window)
        if cut is None:
            return CompactionResult(success=False, reason="no safe cut point")
        cut_index, first_kept_id = cut

        region = self._summary_input(session, cut_index)
        if not region:
            return CompactionResult(success=False, reason="nothing to summarize (empty region)")

        note = ""
        if use_fallback:
            summary = mechanical_digest(region)
        else:
            try:
                raw = self.summarizer(region)
            except SummaryRejected as exc:
                return CompactionResult(success=False, reason=str(exc.reason))
            except ProviderError as exc:
                # 摘要调用失败不该炸掉用户这一轮：保留原文继续跑，由退避机制决定何时再试
                return CompactionResult(success=False, reason=f"summarizer call failed: {exc}")

            summary, salvaged = sanitize_summary(raw)
            if salvaged:
                # 这个路由会把工具调用序列化成文本混进正文；救回标记之前的正文，
                # 而不是整条丢弃（实测：模型摘要经常带着这类尾巴）
                note = "\n\n[注：摘要末尾的工具调用标记已由 agent 丢弃]"
            ok, reason = validate_summary(summary)
            if not ok:
                return CompactionResult(success=False, reason=reason)

        summary = cap_summary(summary + note)
        entry = session.append_compaction(summary, first_kept_id)
        return CompactionResult(
            success=True,
            summary=summary,
            first_kept_entry_id=first_kept_id,
            entry_id=entry.id,
            compacted_count=cut_index,
            folded_messages=sum(
                1 for e in session._path_to_head()[:cut_index] if e.type == "message"
            ),
            summary_chars=len(summary),
            fallback=use_fallback,
        )

    # --- 自动触发生成器（供 Agent 的生成器协作调用）---
    def compact_if_needed(self, session, context_window: int, force: bool = False):
        """若到阈值则压缩，yield 事件，return CompactionResult；否则 return None。

        这是生成器：把「正在压缩 / 压缩完成（含失败原因）/ 退避中」以事件形式发射给消费者，
        压缩动作本身由 compact_now 同步完成（MVP 不流式播报摘要）。
        失败时 result.success=False 且 reason 会随事件带给 UI，绝不静默。

        退避：上次失败后、上下文还没长够之前不重试（否则每个工具轮次都发一次摘要请求，
        任务会被拖死）；连续失败到上限后，fallback="digest" 改为机械摘要继续压，
        fallback="none" 则彻底停手。退避只在**状态刚变化时**播报一次，避免刷屏。

        force=True（用户手动 /compact）会忽略退避与停手，强制再试一次模型摘要。
        """
        self._sync_session(session)
        if not force:
            paused = self._pause_reason(session)
            if paused:
                if not self._pause_announced:
                    self._pause_announced = True
                    yield AgentEvent("compaction_paused", {
                        "reason": paused,
                        "consecutive_failures": self._consecutive_failures,
                        "disabled": bool(self._disabled_reason),
                        "fallback": self.fallback,
                    })
                return None

        cut = self.find_cut(session, context_window)
        if cut is None:
            yield AgentEvent("compaction_skip", {
                "reason": "nothing to fold (history fits in the retained tail) "
                          "or no safe tool-pairing boundary",
            })
            return None

        use_fallback = (
            self.fallback == "digest" and bool(self._disabled_reason) and not force
        )
        yield AgentEvent("compaction_start", {
            "compacted_count": cut[0],
            "fallback": use_fallback,
        })
        result = self.compact_now(session, context_window, cut=cut, use_fallback=use_fallback)
        if result.success:
            self._note_success(session, via_fallback=result.fallback)
        else:
            self._note_failure(session, result.reason or "unknown")
        yield AgentEvent("compaction_end", {
            "success": result.success,
            "reason": result.reason,
            "fallback": use_fallback,
            "compacted_count": result.compacted_count,
            "folded_messages": result.folded_messages,
            "summary_chars": result.summary_chars,
            "first_kept_entry_id": result.first_kept_entry_id,
            "consecutive_failures": self._consecutive_failures,
            "disabled": bool(self._disabled_reason),
        })
        return result
