class Agent:
    """仿 pi-agent 的 Agent：负责管理对话状态（messages 与 system prompt）。

    AgentLoop 只负责"接收 messages → 调模型 → 执行工具 → 回填结果"的机械循环，
    而上下文由本类掌控：system prompt 的注入、历史消息的累积，
    以及将来消息的压缩 / 改写 / 持久化，都发生在这里。
    """

    def __init__(self, loop, system_prompt: str = ""):
        self.loop = loop
        self.system_prompt = system_prompt
        self.messages = (
            [{"role": "system", "content": system_prompt}] if system_prompt else []
        )

    def prompt(self, user_input: str) -> str:
        """追加一条用户消息并驱动循环，返回模型最终回复。

        对话历史由本类维护并原样传给 AgentLoop；
        AgentLoop 在运行过程中会把模型回复与工具结果追加回 self.messages。
        """
        self.messages.append({"role": "user", "content": user_input})
        return self.loop.run(self.messages)

    def clear_history(self):
        """清空对话历史，仅保留 system prompt。"""
        self.messages = (
            [{"role": "system", "content": self.system_prompt}] if self.system_prompt else []
        )
