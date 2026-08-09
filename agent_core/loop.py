import json


class AgentLoop:
    """仿 pi-agent 的核心循环：调模型 → 若有 tool_calls 则执行 → 循环直到模型直接回复。"""

    def __init__(self, client, model, system_prompt, tools, tool_map):
        self.client = client
        self.model = model
        self.system_prompt = system_prompt
        self.tools = tools
        self.tool_map = tool_map

    def run(self, prompt: str) -> str:
        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": prompt},
        ]

        while True:
            print("\n>>> 调用 API ...")
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=self.tools,
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
                result = self.tool_map[name](**args)
                print(f">>> 工具返回: {str(result)[:200]}")

                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": result,
                })
