from .events import AgentEvent
from .states import AgentState


class Agent:
    """仿 pi-agent 的 Agent：负责管理对话状态（messages 与 system prompt）。

    AgentLoop 只负责"接收 messages → 调模型 → 执行工具 → 回填结果"的机械循环，
    而上下文由本类掌控：system prompt 的注入、历史消息的累积、会话的持久化，
    以及将来消息的压缩 / 改写，都发生在这里。

    prompt() 是一个生成器：yield agent_start → 转发 loop 的全部事件 → yield agent_end。
    消费者（CLI / Web）用 for event in agent.prompt(...) 迭代。
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

    @property
    def state(self) -> AgentState:
        """当前运行时状态（委托给 loop，单一真相源）。"""
        return self.loop.state

    def abort(self):
        """请求中止当前运行（委托给 loop）。"""
        self.loop.abort()

    def prompt(self, user_input: str):
        """追加一条用户消息并驱动循环，生成器 yield AgentEvent，结束后自动存档。

        对话历史由本类维护并原样传给 AgentLoop；
        AgentLoop 在运行过程中会把模型回复与工具结果追加回 self.messages。
        """
        self.messages.append({"role": "user", "content": user_input})

        yield AgentEvent("agent_start")
        final_text = yield from self.loop.run(self.messages)
        yield AgentEvent("agent_end", {"text": final_text})

        self._save()

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
