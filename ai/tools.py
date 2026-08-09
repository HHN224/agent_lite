from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Tool:
    """最简洁的工具基类：仅描述一个工具由什么构成，不关心具体造了什么工具。"""

    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {}})
