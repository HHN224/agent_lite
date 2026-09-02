"""ai 层的 Provider 抽象：定义「流式调用 LLM」的契约与事件类型。

本层只依赖第三方 SDK 与自身（.tools），不引用 agent_core / coding_agent 等任何上层设施；
上层通过 LLMProvider 接口消费流式事件，无需关心具体是哪家 API。
"""

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Iterator, Union

from openai import OpenAI, OpenAIError

from .tools import Tool


class ProviderError(Exception):
    """Provider 层的统一异常：API / 网络 / 协议解析错误都包装成本类型。

    上层循环只需捕获它即可做到「模型服务故障不崩溃」，无需了解具体 SDK 的异常体系。
    """


@dataclass
class TextDelta:
    """一段文本增量，按到达顺序拼接即为完整回复。"""

    content: str


@dataclass
class ToolCall:
    """一次完整的工具调用（参数已由 provider 拼装并解析为 dict）。"""

    id: str
    name: str
    arguments: dict


StreamEvent = Union[TextDelta, ToolCall]


class LLMProvider(ABC):
    """流式 Provider 契约：把「messages + 工具定义」变成一串流式事件。

    只负责「如何向某个 LLM 提问」，不知道也不关心谁来消费这些事件。
    任何失败都抛 ProviderError。
    """

    @abstractmethod
    def stream(
        self, messages: list[dict], tools: list[Tool], model: str
    ) -> Iterator[StreamEvent]:
        """以流式方式请求模型，依次产出 TextDelta / ToolCall 事件。"""
        raise NotImplementedError


class OpenAIProvider(LLMProvider):
    """OpenAI 兼容 API 的流式实现（DeepSeek 等兼容服务同样适用）。

    内部消化了 OpenAI 流式协议里「工具调用参数分片返回」的细节：
    按 tool_call.index 累积 arguments，流结束后再产出完整的 ToolCall。
    连接类错误由 SDK 自动重试（max_retries），所有失败统一抛 ProviderError。
    """

    def __init__(self, api_key: str, base_url: str | None = None):
        self.client = OpenAI(api_key=api_key, base_url=base_url, max_retries=2)
        # 最近一次调用的真实 usage（anchor），供上下文计量锚定；None 表示尚未拿到
        self.last_usage: dict | None = None

    def stream(
        self, messages: list[dict], tools: list[Tool], model: str
    ) -> Iterator[StreamEvent]:
        # 流式协议下 tool_calls 按 index 分片到达，先累积、流结束后再产出完整事件
        pending: dict[int, dict[str, str]] = {}

        try:
            response = self.client.chat.completions.create(
                model=model,
                messages=messages,
                tools=[t.to_schema() for t in tools],
                stream=True,
                stream_options={"include_usage": True},
            )

            # 收集流末的真实 usage（非流式末 chunk，通常无 choices）
            usage: dict | None = None
            for chunk in response:
                if chunk.usage:
                    usage = {
                        k: v
                        for k, v in chunk.usage.model_dump().items()
                        if v is not None
                    }
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                if delta is None:
                    continue

                if delta.content:
                    yield TextDelta(delta.content)

                for tc in delta.tool_calls or []:
                    acc = pending.setdefault(tc.index, {"id": "", "name": "", "args": ""})
                    if tc.id:
                        acc["id"] = tc.id
                    if tc.function and tc.function.name:
                        acc["name"] = tc.function.name
                    if tc.function and tc.function.arguments:
                        acc["args"] += tc.function.arguments

            # 流成功结束：把最近一次真实 usage 记录为锚点
            self.last_usage = usage
        except OpenAIError as e:
            raise ProviderError(f"模型 API 错误: {e}") from e

        for index in sorted(pending):
            acc = pending[index]
            try:
                arguments = json.loads(acc["args"]) if acc["args"] else {}
            except json.JSONDecodeError as e:
                # 不再静默吞掉：明确告知上层「模型输出了非法 JSON 参数」
                raise ProviderError(
                    f"工具 {acc['name']!r} 的参数不是合法 JSON: {acc['args'][:200]!r}"
                ) from e
            yield ToolCall(id=acc["id"], name=acc["name"], arguments=arguments)
