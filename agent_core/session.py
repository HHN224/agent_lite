"""会话持久化：把 Agent 的对话状态（system prompt + 消息）以 JSON 存到磁盘。

设计目标：
  - 会话是可寻址的「条目（entry）列表」，每条消息有稳定的 id 与 parent_id，
    为将来的「压缩（compaction）」与「分支 / 回溯」留好接缝。
  - 追加式、不可变：每轮只追加新条目，不改写旧条目。
  - 存储按「会话 id」组织：sessions/<session_id>.json（文件名是 id，人类可读名在文件内）。

向下兼容：本模块仍保留旧的 SessionStore（单文件 flat JSON，
结构为 {version, system_prompt, messages}），因为 Agent / CLI 目前仍在用它；
新的 Session + SessionRepository 供 Phase 2 起接入。二者并存，互不干扰。
"""

import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

VERSION = 2
LEGACY_VERSION = 1

# 会话名白名单长度限制（校验用，防止 session_id 越界）
MAX_ID_LEN = 64


@dataclass
class SessionEntry:
    """一条会话条目：一条消息，或一次压缩记录。

    type:
      "message"   一条对话消息（role/content/tool_call_id/tool_calls）
      "compaction" 一次上下文压缩记录（summary / first_kept_entry_id）

    parent_id 指向它在会话链中的上一跳；head 是从 root 一路能走到的叶子。
    线性对话里每个新条目的 parent_id 就是当前的 head。
    """

    id: str
    parent_id: str | None
    type: str  # "message" | "compaction"
    timestamp: float = field(default_factory=time.time)

    # --- type == "message" 时使用 ---
    role: str | None = None
    content: str | None = None
    tool_call_id: str | None = None
    tool_calls: list | None = None

    # --- type == "compaction" 时使用 ---
    summary: str | None = None
    first_kept_entry_id: str | None = None

    def to_llm(self) -> dict:
        """转成发给模型的消息 dict（仅对 message 有效）。"""
        msg: dict = {"role": self.role}
        if self.content is not None:
            msg["content"] = self.content
        if self.tool_call_id is not None:
            msg["tool_call_id"] = self.tool_call_id
        if self.tool_calls:
            msg["tool_calls"] = self.tool_calls
        return msg

    def to_dict(self) -> dict:
        d = {
            "id": self.id,
            "parent_id": self.parent_id,
            "type": self.type,
            "timestamp": self.timestamp,
        }
        if self.type == "message":
            d["role"] = self.role
            if self.content is not None:
                d["content"] = self.content
            if self.tool_call_id is not None:
                d["tool_call_id"] = self.tool_call_id
            if self.tool_calls:
                d["tool_calls"] = self.tool_calls
        else:  # compaction
            d["summary"] = self.summary
            d["first_kept_entry_id"] = self.first_kept_entry_id
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "SessionEntry":
        return cls(
            id=d["id"],
            parent_id=d.get("parent_id"),
            type=d["type"],
            timestamp=d.get("timestamp", 0.0),
            role=d.get("role"),
            content=d.get("content"),
            tool_call_id=d.get("tool_call_id"),
            tool_calls=d.get("tool_calls"),
            summary=d.get("summary"),
            first_kept_entry_id=d.get("first_kept_entry_id"),
        )


@dataclass
class SessionMeta:
    """会话列表里的一行元数据（不加载全部条目）。"""

    session_id: str
    name: str
    created_at: float
    updated_at: float
    message_count: int


class Session:
    """一个会话的内存态：追加条目、重建发给模型的 payload、记录新一轮。

    它不知道消息从哪来、给谁用；只负责「维护条目链 + 生成 LLM 输入」。
    """

    def __init__(
        self,
        session_id: str,
        name: str = "",
        system_prompt: str = "",
        entries: dict[str, SessionEntry] | None = None,
        head_id: str | None = None,
        next_id: int = 1,
        created_at: float | None = None,
        updated_at: float | None = None,
        token_count: int = 0,
    ):
        self.session_id = session_id
        self.name = name
        self.system_prompt = system_prompt
        self.entries: dict[str, SessionEntry] = entries or {}
        self.head_id = head_id
        self.next_id = next_id
        self.created_at = created_at if created_at is not None else time.time()
        self.updated_at = updated_at if updated_at is not None else self.created_at
        self.token_count = token_count
        self._token_estimate = None  # Phase 4 注入的 tokenizer：Callable[[SessionEntry], int]

    # ------------------------------------------------------------------ #
    # 追加
    # ------------------------------------------------------------------ #
    def _new_id(self) -> str:
        i = self.next_id
        self.next_id += 1
        return f"e{i}"

    def _touch(self):
        self.updated_at = time.time()

    def append_message(
        self,
        role: str,
        content: str | None = None,
        *,
        tool_call_id: str | None = None,
        tool_calls: list | None = None,
        timestamp: float | None = None,
    ) -> SessionEntry:
        """追加一条消息，parent 自动接到当前 head。"""
        entry = SessionEntry(
            id=self._new_id(),
            parent_id=self.head_id,
            type="message",
            timestamp=timestamp if timestamp is not None else time.time(),
            role=role,
            content=content,
            tool_call_id=tool_call_id,
            tool_calls=tool_calls,
        )
        self.entries[entry.id] = entry
        self.head_id = entry.id
        self._touch()
        self._bump_token_estimate(entry)
        return entry

    def append_compaction(
        self,
        summary: str,
        first_kept_entry_id: str,
        *,
        timestamp: float | None = None,
    ) -> SessionEntry:
        """追加一次压缩记录。今天只负责数据层；真正的总结在 Phase 4 接入。"""
        entry = SessionEntry(
            id=self._new_id(),
            parent_id=self.head_id,
            type="compaction",
            timestamp=timestamp if timestamp is not None else time.time(),
            summary=summary,
            first_kept_entry_id=first_kept_entry_id,
        )
        self.entries[entry.id] = entry
        self.head_id = entry.id
        self._touch()
        return entry

    def record_turn(self, messages: list[dict]) -> list[SessionEntry]:
        """把「本轮新产生的消息」（含开头的 user 消息）追加成条目。

        messages 是按顺序排列的 dict 列表（role/content/tool_call_id/tool_calls），
        Phase 2 里 Agent 会把 loop 吐回的增量消息交给这里。
        """
        appended: list[SessionEntry] = []
        for m in messages:
            e = self.append_message(
                m.get("role"),
                m.get("content"),
                tool_call_id=m.get("tool_call_id"),
                tool_calls=m.get("tool_calls"),
            )
            appended.append(e)
        return appended

    # ------------------------------------------------------------------ #
    # 重建发给模型的 payload
    # ------------------------------------------------------------------ #
    def _path_to_head(self) -> list[SessionEntry]:
        """从 head 沿 parent_id 走回 root（线性链；对分支也稳健）。"""
        path: list[SessionEntry] = []
        cur = self.head_id
        seen: set[str] = set()
        while cur and cur not in seen:
            seen.add(cur)
            e = self.entries.get(cur)
            if e is None:
                break
            path.append(e)
            cur = e.parent_id
        path.reverse()
        return path

    def build_llm_payload(self) -> list[dict]:
        """重建发给模型的消息列表：system → （最新的压缩 summary）→ 保留的消息。

        保留的消息从最新的 compression 的 first_kept_entry_id 开始；
        若没有压缩，则返回从 root 到 head 的全部消息。
        """
        path = self._path_to_head()

        latest_cmp: SessionEntry | None = None
        for e in path:
            if e.type == "compaction":
                latest_cmp = e

        kept_start_index = 0
        if latest_cmp and latest_cmp.first_kept_entry_id:
            for i, e in enumerate(path):
                if e.id == latest_cmp.first_kept_entry_id:
                    kept_start_index = i
                    break

        payload: list[dict] = []
        if self.system_prompt:
            payload.append({"role": "system", "content": self.system_prompt})
        if latest_cmp and latest_cmp.summary:
            payload.append({"role": "system", "content": latest_cmp.summary})
        for e in path[kept_start_index:]:
            if e.type == "message":
                payload.append(e.to_llm())
        return payload

    # ------------------------------------------------------------------ #
    # 统计 / 估算
    # ------------------------------------------------------------------ #
    def _bump_token_estimate(self, entry: SessionEntry):
        self.token_count += self.estimate_tokens(entry)

    def estimate_tokens(self, entry: SessionEntry) -> int:
        """粗略 token 估算：用于元数据与压缩阈值判断（Phase 4 的 tokenizer 会替换它）。"""
        if self._token_estimate:
            return self._token_estimate(entry)
        text = ""
        if entry.content:
            text += entry.content
        if entry.tool_calls:
            text += str(entry.tool_calls)
        if entry.summary:
            text += entry.summary
        return max(1, len(text) // 4)

    @property
    def message_count(self) -> int:
        return sum(1 for e in self.entries.values() if e.type == "message")

    # ------------------------------------------------------------------ #
    # 序列化
    # ------------------------------------------------------------------ #
    def to_dict(self) -> dict:
        return {
            "version": VERSION,
            "session_id": self.session_id,
            "name": self.name,
            "system_prompt": self.system_prompt,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "token_count": self.token_count,
            "head_id": self.head_id,
            "next_id": self.next_id,
            "entries": [e.to_dict() for e in self.entries.values()],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Session":
        entries = {
            e["id"]: SessionEntry.from_dict(e)
            for e in d.get("entries", [])
        }
        return cls(
            session_id=d["session_id"],
            name=d.get("name", ""),
            system_prompt=d.get("system_prompt", ""),
            entries=entries,
            head_id=d.get("head_id"),
            next_id=d.get("next_id", 1),
            created_at=d.get("created_at"),
            updated_at=d.get("updated_at"),
            token_count=d.get("token_count", 0),
        )


class SessionRepository:
    """sessions/<session_id>.json 的读写与管理：create / load / list / delete / rename。

    文件名是会话 id；人类可读的「会话名」存在文件内的 name 字段。
    读旧版 v1 flat 存档（{version, system_prompt, messages}）时自动迁移到 v2。
    """

    def __init__(self, sessions_dir: Path):
        self.dir = sessions_dir

    @staticmethod
    def new_session_id() -> str:
        return uuid.uuid4().hex[:12]

    def _safe_id(self, session_id: str) -> bool:
        return (
            isinstance(session_id, str)
            and 0 < len(session_id) <= MAX_ID_LEN
            and session_id.isalnum()
        )

    def _path(self, session_id: str) -> Path:
        return self.dir / f"{session_id}.json"

    def create(self, *, name: str = "", system_prompt: str = "") -> Session:
        session_id = self.new_session_id()
        return Session(session_id=session_id, name=name, system_prompt=system_prompt)

    def save(self, session: Session) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self._path(session.session_id)
        # 先写临时文件再原子替换，避免中途出错写坏已有存档
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(_json_dumps(session.to_dict()), encoding="utf-8")
        tmp.replace(path)

    def load(self, session_id: str) -> Session | None:
        """读回会话；文件不存在返回 None，损坏则抛异常由调用方决定如何兜底。"""
        path = self._path(session_id)
        if not path.exists():
            return None
        data = _json_loads(path.read_text(encoding="utf-8"))
        session = self._migrate(data, session_id)
        return session

    def _migrate(self, data: dict, session_id: str) -> Session:
        """把（可能是 v1 的）存档 dict 归一成 Session。

        v1: {version:1, system_prompt, messages}
        v2: {version:2, session_id, name, entries, head_id, next_id, ...}
        迁移 v1 → v2：把 flat messages 逐条转成线性 entry 链。
        """
        version = data.get("version", LEGACY_VERSION)
        if version == VERSION:
            session = Session.from_dict(data)
            # 文件名优先作为真 id（外部凭证），文件内 id 不一致时以文件名为主
            session.session_id = session_id
            return session

        # v1 迁移：flat {messages} → entry 链
        sys_prompt = data.get("system_prompt", "")
        messages = data.get("messages", [])
        if not isinstance(messages, list):
            raise ValueError("存档结构不合法")

        session = Session(
            session_id=session_id,
            name=data.get("name", ""),
            system_prompt=sys_prompt,
        )
        for m in messages:
            if not isinstance(m, dict):
                raise ValueError("存档结构不合法")
            session.append_message(
                m.get("role"),
                m.get("content"),
                tool_call_id=m.get("tool_call_id"),
                tool_calls=m.get("tool_calls"),
            )
        return session

    def list(self) -> list[SessionMeta]:
        """列出会话目录下所有会话的元数据（不加载条目）。"""
        if not self.dir.exists():
            return []
        metas: list[SessionMeta] = []
        for path in sorted(self.dir.glob("*.json")):
            if path.name.endswith(".tmp"):
                continue
            try:
                data = _json_loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue  # 损坏文件直接跳过，不阻塞列表
            session_id = path.stem
            if data.get("version") == VERSION:
                metas.append(SessionMeta(
                    session_id=session_id,
                    name=data.get("name", ""),
                    created_at=data.get("created_at", 0.0),
                    updated_at=data.get("updated_at", 0.0),
                    message_count=sum(
                        1 for e in data.get("entries", []) if e.get("type") == "message"
                    ),
                ))
            else:
                # v1 文件：name 取文件名，消息数取 messages 长度
                metas.append(SessionMeta(
                    session_id=session_id,
                    name=session_id,
                    created_at=0.0,
                    updated_at=0.0,
                    message_count=len(data.get("messages", [])),
                ))
        metas.sort(key=lambda m: m.updated_at, reverse=True)
        return metas

    def delete(self, session_id: str) -> bool:
        path = self._path(session_id)
        if not path.exists():
            return False
        path.unlink()
        return True

    def rename(self, session_id: str, new_name: str) -> Session:
        session = self.load(session_id)
        if session is None:
            raise FileNotFoundError(f"会话不存在: {session_id}")
        session.name = new_name
        self.save(session)
        return session


class SessionStore:
    """（旧版，保留兼容）一个 JSON 文件对应一个会话：{version, system_prompt, messages}。

    仍被 Agent / CLI 使用；Phase 2 会把它切换成 Session + SessionRepository。
    """

    def __init__(self, path: Path):
        self.path = path

    def save(self, system_prompt: str, messages: list) -> None:
        data = {
            "version": LEGACY_VERSION,
            "system_prompt": system_prompt,
            "messages": messages,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(
            _json_dumps(data), encoding="utf-8"
        )
        tmp.replace(self.path)

    def load(self):
        """读回会话状态；文件不存在返回 None，损坏则抛异常由调用方决定如何兜底。"""
        if not self.path.exists():
            return None
        data = _json_loads(self.path.read_text(encoding="utf-8"))
        system_prompt = data.get("system_prompt", "")
        messages = data.get("messages", [])
        if not isinstance(system_prompt, str) or not isinstance(messages, list):
            raise ValueError("存档结构不合法")
        return system_prompt, messages


def _json_dumps(data: dict) -> str:
    import json

    return json.dumps(data, ensure_ascii=False, indent=2)


def _json_loads(text: str) -> dict:
    import json

    return json.loads(text)
