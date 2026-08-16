"""会话持久化：把 Agent 的对话状态（system prompt + messages）以 JSON 存到磁盘。

只负责「状态 ↔ JSON 文件」的读写与转换，不知道消息从哪来、给谁用。
"""

import json
from pathlib import Path

VERSION = 1


class SessionStore:
    """一个 JSON 文件对应一个会话：{version, system_prompt, messages}。"""

    def __init__(self, path: Path):
        self.path = path

    def save(self, system_prompt: str, messages: list) -> None:
        data = {
            "version": VERSION,
            "system_prompt": system_prompt,
            "messages": messages,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)

        # 先写临时文件再原子替换，避免中途出错写坏已有存档
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(self.path)

    def load(self):
        """读回会话状态；文件不存在返回 None，损坏则抛异常由调用方决定如何兜底。"""
        if not self.path.exists():
            return None

        data = json.loads(self.path.read_text(encoding="utf-8"))
        system_prompt = data.get("system_prompt", "")
        messages = data.get("messages", [])

        if not isinstance(system_prompt, str) or not isinstance(messages, list):
            raise ValueError("存档结构不合法")

        return system_prompt, messages
