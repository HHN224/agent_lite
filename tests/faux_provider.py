"""剧本式假 Provider：按预设脚本返回流式事件，让 AgentLoop 可确定性测试。

对齐 pi 的 fauxProvider 设计——不碰真实 API，用脚本驱动循环，
把「循环行为」与「具体模型行为」解耦。
"""

from ai import LLMProvider, Tool, TextDelta, ToolCall


class FauxProvider(LLMProvider):
    """script 是「每次 LLM 调用返回的一批事件」的列表，按调用顺序逐个弹出。

    元素可以是 TextDelta / ToolCall 组成的 list，或一个异常（stream 时抛出，
    用于模拟 ProviderError 故障路径）。
    """

    def __init__(self, script: list):
        self.script = list(script)
        self.calls: list = []

    def stream(self, messages: list[dict], tools: list[Tool], model: str):
        self.calls.append({"messages": list(messages), "tools": list(tools), "model": model})

        if not self.script:
            return iter(())

        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return iter(step)