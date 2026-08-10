import json


class AgentLoop:
    """仿 pi-agent 的核心循环：调模型 → 若有 tool_calls 则执行 → 循环直到模型直接回复。

    本类只管"接收 messages → 驱动模型 → 追加结果"，不关心上下文怎么构建与压缩；
    上下文管理（system prompt、历史累积、消息改写）由 Agent 负责。
    """

    def __init__(self, client, model, tools):
        self.client = client
        self.model = model
        self.tools = tools
        self.tool_map = {t.name: t for t in tools}

    def run(self, messages: list) -> str:
        while True:
            print("\n>>> 调用 API ...")
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=[t.to_schema() for t in self.tools],
            )

            message = response.choices[0].message
            messages.append(message)

            # 没有工具调用，说明模型已经回答完了
            if not message.tool_calls:
                print(f">>> 最终回复: {message.content}")
                return message.content

            # 执行工具
            for tool_call in message.tool_calls:
                name = tool_call.function.name
                args = json.loads(tool_call.function.arguments)

                print(f">>> 正在使用工具: {name} | 参数: {args}")
                result = self.tool_map[name].execute(**args)
                print(f">>> 工具返回: {str(result)[:200]}")

                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": result,
                })
