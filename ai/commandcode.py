"""Command Code GOAT / Provider API via the existing LLMProvider contract.

Official endpoints and billing: https://commandcode.ai/docs/provider
GOAT keys use the same endpoint and consume the subscriber's GOAT credits.
This adapter uses Chat Completions (OpenAI / open models, not Anthropic).
"""

from .providers import OpenAIProvider, ProviderError


class CommandCodeProvider(OpenAIProvider):
    DEFAULT_BASE_URL = "https://api.commandcode.ai/provider/v1"
    DEFAULT_MODEL = "deepseek/deepseek-v4.1-flash"

    def __init__(self, api_key: str, base_url: str | None = None):
        super().__init__(api_key=api_key, base_url=base_url or self.DEFAULT_BASE_URL)

    def stream(self, messages, tools, model, **options):
        if "claude" in model.lower():
            raise ProviderError(
                "Command Code 的 Claude 模型使用 Anthropic /messages 协议；"
                "当前适配器支持 Chat Completions 模型，请选择 DeepSeek、Qwen 等 GOAT 模型。"
            )
        yield from super().stream(messages, tools, model, **options)
