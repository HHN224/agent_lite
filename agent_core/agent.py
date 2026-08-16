class Agent:
    """仿 pi-agent 的 Agent：负责管理对话状态（messages 与 system prompt）。

    AgentLoop 只负责"接收 messages → 调模型 → 执行工具 → 回填结果"的机械循环，
    而上下文由本类掌控：system prompt 的注入、历史消息的累积、会话的持久化，
    以及将来消息的压缩 / 改写，都发生在这里。
    """

    def __init__(self, loop, system_prompt: str = "", store=None, resume: bool = True):
        self.loop = loop
        self.system_prompt = system_prompt
        self.store = store
        self.messages = (
            [{"role": "system", "content": system_prompt}] if system_prompt else []
        )

        if store is not None and resume:
            self._restore()

    def prompt(self, user_input: str) -> str:
        """追加一条用户消息并驱动循环，返回模型最终回复；每轮结束后自动存档。

        对话历史由本类维护并原样传给 AgentLoop；
        AgentLoop 在运行过程中会把模型回复与工具结果追加回 self.messages。
        """
        self.messages.append({"role": "user", "content": user_input})
        result = self.loop.run(self.messages)
        self._save()
        return result

    def clear_history(self):
        """清空对话历史，仅保留 system prompt，并同步存档。"""
        self.messages = (
            [{"role": "system", "content": self.system_prompt}] if self.system_prompt else []
        )
        self._save()

    def _restore(self):
        """从存档恢复会话；文件缺失 / 损坏都不影响启动（损坏时放弃存档、从新开始）。"""
        try:
            loaded = self.store.load()
        except Exception as e:
            print(f">>> 会话存档损坏，已忽略并从新会话开始: {e}")
            return

        if loaded is None:
            return

        self.system_prompt, self.messages = loaded
        print(f">>> 已恢复会话（{len(self.messages)} 条历史消息）")

    def _save(self):
        """存档失败只警告，不打断使用。"""
        if self.store is None:
            return
        try:
            self.store.save(self.system_prompt, self.messages)
        except Exception as e:
            print(f">>> 警告：会话存档失败: {e}")
