import json

from ai import ProviderError, TextDelta, ToolCall

from .agent_tools import ToolResult


class AgentLoop:
    """仿 pi-agent 的核心循环：流式调模型 → 若有 tool_calls 则执行 → 循环直到模型直接回复。

    本类只管"接收 messages → 驱动模型 → 追加结果"的机械循环，不关心上下文怎么构建与压缩；
    上下文管理（system prompt、历史累积、消息改写）由 Agent 负责。
    模型访问完全经由 ai 层的 LLMProvider 契约，本层不接触任何具体 API SDK。
    """

    def __init__(self, provider, model, tools, max_iterations: int = 10):
        self.provider = provider
        self.model = model
        self.tools = tools
        self.tool_map = {t.name: t for t in tools}
        self.max_iterations = max_iterations

    def run(self, messages: list) -> str:
        for _ in range(self.max_iterations):
            text_parts: list[str] = []
            tool_calls: list[ToolCall] = []

            print("\n>>> 调用 API ...")
            try:
                for event in self.provider.stream(messages, self.tools, self.model):
                    if isinstance(event, TextDelta):
                        print(event.content, end="", flush=True)
                        text_parts.append(event.content)
                    elif isinstance(event, ToolCall):
                        tool_calls.append(event)
            except ProviderError as e:
                # 模型服务故障：结束本轮而不是让整个 REPL 崩溃
                print(f"\n>>> 模型服务出错: {e}")
                return f"(模型服务出错：{e})"

            if text_parts:
                print()  # 结束流式输出所在的行

            # 没有工具调用，说明模型已经回答完了
            if not tool_calls:
                return "".join(text_parts)

            # 把这一轮 assistant 消息以标准 dict 形式记入历史
            messages.append({
                "role": "assistant",
                "content": "".join(text_parts) or None,
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": json.dumps(call.arguments, ensure_ascii=False),
                        },
                    }
                    for call in tool_calls
                ],
            })

            # 依次执行工具并回填结果
            for call in tool_calls:
                print(f">>> 正在使用工具: {call.name} | 参数: {call.arguments}")
                result = self._execute(call)
                print(f">>> 工具返回: {result.content[:200]}")

                messages.append({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": result.content,
                })

        return "\n(已达到最大工具调用轮数，停止循环)"

    def _execute(self, call: ToolCall) -> ToolResult:
        """执行单个工具调用；任何错误都转成 ToolResult(is_error=True) 回填给模型，而不是让循环崩溃。"""
        tool = self.tool_map.get(call.name)
        if tool is None:
            return ToolResult(content=f"Error: unknown tool '{call.name}'", is_error=True)

        try:
            return tool.execute(**call.arguments)
        except Exception as e:
            return ToolResult(content=f"Error: {e}", is_error=True)
