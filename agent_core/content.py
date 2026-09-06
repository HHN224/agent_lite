"""消息内容块（ContentBlock）：让 content 从裸字符串升级为结构化块列表。

Phase 3 的目标是让消息具备多模态 / thinking / 结构化工具结果的表达能力，
同时尽量不破坏现有工具与压缩逻辑（它们大多依赖"content 是 str"）。

关键设计：
  - ContentBlock 用 **dict** 兼容 OpenAI，这样 to_llm() 只需转发。
  - content 允许 `str | list[dict]`：纯文本消息仍可用字符串（零改动）。
  - 提供 content_to_llm / content_to_text / content_length 等辅助。
  - 不做任何"为兼容旧存档的防御性代码"。

块类型（type 值对齐 OpenAI / pi 语义）：
  text / image_url / thinking / tool_result
"""

from __future__ import annotations

from typing import Any

BLOCK_TEXT = "text"
BLOCK_IMAGE = "image_url"
BLOCK_THINKING = "thinking"
BLOCK_TOOL_RESULT = "tool_result"

IMAGE_CHAR_EQUIVALENT = 4800
_TEXT_BLOCK_TYPES = (BLOCK_TEXT, BLOCK_TOOL_RESULT)


def text_block(text: str) -> dict:
    return {"type": BLOCK_TEXT, "text": text}


def image_block(data: str, mime_type: str = "image/png") -> dict:
    """构造图片块。data 可为 base64 data URI 或 http(s) URL。"""
    if data.startswith("data:") or data.startswith("http") or data.startswith("https"):
        url = data
    else:
        url = "data:" + mime_type + ";base64," + data
    return {"type": BLOCK_IMAGE, "image_url": {"url": url}}


def thinking_block(text: str) -> dict:
    return {"type": BLOCK_THINKING, "text": text}


def tool_result_block(
    content: str,
    *,
    is_error: bool = False,
    exit_code: int | None = None,
    stdout: str = "",
    stderr: str = "",
    full_output_path: str | None = None,
) -> dict:
    """构造工具结果块：保留结构化元信息，同时提供可读文本。"""
    return {
        "type": BLOCK_TOOL_RESULT,
        "content": content,
        "is_error": is_error,
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "full_output_path": full_output_path,
    }


def is_content_blocks(content: Any) -> bool:
    """判断 content 是否为结构块列表。"""
    return isinstance(content, list) and all(isinstance(b, dict) and "type" in b for b in content)


def content_to_llm(content: Any) -> Any:
    """把 content 转成发给 OpenAI 的格式（str 或结构块列表都原样）。"""
    return content


def content_to_text(content: Any) -> str:
    """把 content 提取为纯文本（供估算 / 截断 / UI 回退）。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for b in content:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t in _TEXT_BLOCK_TYPES:
            parts.append(b.get("text") or b.get("content") or "")
    return "".join(parts)


def content_length(content: Any) -> int:
    """估算 content 的"字符长度"（供阈值 / 截断判定）。"""
    if content is None:
        return 0
    if isinstance(content, str):
        return len(content)
    total = 0
    for b in content:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t in _TEXT_BLOCK_TYPES:
            total += len(b.get("text") or b.get("content") or "")
        elif t == BLOCK_IMAGE:
            total += IMAGE_CHAR_EQUIVALENT
        elif t == BLOCK_THINKING:
            total += len(b.get("text") or "")
    return total