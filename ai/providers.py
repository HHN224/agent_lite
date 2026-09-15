"""ai 层的 Provider 抽象：定义「流式调用 LLM」的契约与事件类型。

本层只依赖第三方 SDK 与自身（.tools），不引用 agent_core / coding_agent 等任何上层设施；
上层通过 LLMProvider 接口消费流式事件，无需关心具体是哪家 API。
"""

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Iterator, Union

import httpx
from openai import APIConnectionError, APIStatusError, OpenAI, OpenAIError

from .tools import Tool


class ProviderError(Exception):
    """Provider 层的统一异常：API / 网络 / 协议解析错误都包装成本类型。

    上层循环只需捕获它即可做到「模型服务故障不崩溃」，无需了解具体 SDK 的异常体系。
    """

    def __init__(self, message: str, *, retryable: bool = False, code: str = "provider_error", recovery_hint: str = ""):
        super().__init__(message)
        self.retryable = retryable
        self.code = code
        self.recovery_hint = recovery_hint


@dataclass
class TextDelta:
    """一段文本增量，按到达顺序拼接即为完整回复。"""

    content: str


@dataclass
class ThinkingDelta:
    """一段模型推理（thinking / reasoning）增量。

    DeepSeek 等带 reasoning 的模型会在内容之前产出 thinking 片段；
    它与最终答案 TextDelta 分开，进历史时用 ThinkingBlock，不进入最终回复。
    """

    content: str


@dataclass
class ToolCall:
    """一次完整的工具调用（参数已由 provider 拼装并解析为 dict）。"""

    id: str
    name: str
    arguments: dict


StreamEvent = Union[TextDelta, ThinkingDelta, ToolCall]


class LLMProvider(ABC):
    """流式 Provider 契约：把「messages + 工具定义」变成一串流式事件。

    只负责「如何向某个 LLM 提问」，不知道也不关心谁来消费这些事件。
    任何失败都抛 ProviderError。
    """

    @abstractmethod
    def stream(
        self, messages: list[dict], tools: list[Tool], model: str
    ) -> Iterator[StreamEvent]:
        """以流式方式请求模型，依次产出 TextDelta / ThinkingDelta / ToolCall 事件。"""
        raise NotImplementedError


class OpenAIProvider(LLMProvider):
    """OpenAI 兼容 API 的流式实现（DeepSeek 等兼容服务同样适用）。

    内部消化了 OpenAI 流式协议里「工具调用参数分片返回」的细节：
    按 tool_call.index 累积 arguments，流结束后再产出完整的 ToolCall。
    所有失败统一抛 ProviderError；可恢复错误交由消费方显式重试和展示状态。
    """

    def __init__(self, api_key: str, base_url: str | None = None):
        # The SDK's hidden retries multiply the loop's retry budget and leave
        # the UI silent. Bound idle reads and let the loop report each retry.
        self.client = OpenAI(api_key=api_key, base_url=base_url, max_retries=0,
                             timeout=httpx.Timeout(60.0, connect=10.0))
        # 最近一次调用的真实 usage（anchor），供上下文计量锚定；None 表示尚未拿到
        self.last_usage: dict | None = None

    def stream(
        self, messages: list[dict], tools: list[Tool], model: str
    ) -> Iterator[StreamEvent]:
        # 流式协议下 tool_calls 按 index 分片到达，先累积、流结束后再产出完整事件
        pending: dict[int, dict[str, str]] = {}
        self.last_usage = None
        response = None
        finish_reason = None

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
                if chunk.choices[0].finish_reason is not None:
                    finish_reason = chunk.choices[0].finish_reason
                delta = chunk.choices[0].delta
                if delta is None:
                    continue

                # thinking / reasoning 增量（DeepSeek 等）
                thinking = getattr(delta, "reasoning_content", None) or getattr(delta, "thinking", None)
                if thinking:
                    yield ThinkingDelta(thinking)

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
            status = e.status_code if isinstance(e, APIStatusError) else None
            retryable = isinstance(e, APIConnectionError) or status in (408, 409, 429) or (status is not None and status >= 500)
            raise ProviderError(f"模型 API 错误: {e}", retryable=retryable,
                                code=f"http_{status}" if status else type(e).__name__) from e
        except httpx.TransportError as e:
            # Streaming reads can escape the SDK's normal exception wrapping.
            raise ProviderError(f"模型流连接中断: {e}", retryable=True, code="stream_transport") from e
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise ProviderError("模型流包含损坏的数据。", retryable=True, code="invalid_stream") from e
        finally:
            if response is not None:
                response.close()

        if finish_reason is None:
            raise ProviderError("模型流提前结束，未收到完成标记。", retryable=True, code="incomplete_stream")
        if finish_reason in ("length", "content_filter"):
            reason = "达到单次输出长度上限" if finish_reason == "length" else "被服务端内容过滤终止"
            raise ProviderError(f"模型输出未完成：{reason}（finish_reason={finish_reason}）。",
                                retryable=finish_reason == "length", code=finish_reason,
                                recovery_hint="Previous response exceeded the output limit and was discarded; no tools from it ran. Retry using smaller tool calls and split large file writes into chunks.")

        for index in sorted(pending):
            acc = pending[index]
            try:
                arguments = json.loads(acc["args"]) if acc["args"] else {}
                if not isinstance(arguments, dict) or not acc["name"] or not acc["id"]:
                    raise ValueError("tool call requires id, name and an arguments object")
            except (json.JSONDecodeError, ValueError) as e:
                # 不再静默吞掉：明确告知上层「模型输出了非法 JSON 参数」
                raise ProviderError(
                    f"工具 {acc['name']!r} 的参数不是合法 JSON 对象或调用标识缺失。",
                    retryable=True, code="invalid_tool_call",
                    recovery_hint="Previous response contained an invalid tool call and was discarded; no tools from it ran. Retry with complete tool identifiers and a valid JSON object for arguments."
                ) from e
            yield ToolCall(id=acc["id"], name=acc["name"], arguments=arguments)
