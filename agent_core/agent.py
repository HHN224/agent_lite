from .events import AgentEvent
from .states import AgentState
from .session import Session, SessionRepository


class Agent:
    """仿 pi-agent 的 Agent：负责管理对话状态（messages 与 system prompt）。

    AgentLoop 只负责"接收 messages → 调模型 → 执行工具 → 回填结果"的机械循环，
    而上下文由本类掌控：system prompt 的注入、历史消息的累积、会话的持久化，
    以及将来消息的压缩 / 改写，都发生在这里。

    prompt() 是一个生成器：yield agent_start → 转发 loop 的全部事件 → yield agent_end。
    消费者（CLI / Web）用 for event in agent.prompt(...) 迭代。

    state 与持久化：
      session  一个 Session（对话状态：条目链 + system prompt + head）。
      repo     一个 SessionRepository（把 session 持久化到磁盘）；可空，空则不存档。
    """

    def __init__(self, loop, session: Session, repo: SessionRepository | None = None):
        self.loop = loop
        self.session = session
        self.repo = repo

    @property
    def state(self) -> AgentState:
        """当前运行时状态（委托给 loop，单一真相源）。"""
        return self.loop.state

    @property
    def session_id(self) -> str:
        return self.session.session_id

    def abort(self):
        """请求中止当前运行（委托给 loop）。"""
        self.loop.abort()

    def prompt(self, user_input: str):
        """追加一条用户消息并驱动循环，生成器 yield AgentEvent，结束后自动存档。

        对话历史由 session 维护：每次先用 build_llm_payload() 重建发给模型的列表
        （system → 压缩 summary → 保留消息），再把本轮新消息记录回 session。
        AgentLoop 会在运行过程中把模型回复与工具结果追加到 payload 尾部。
        """
        session = self.session
        payload = session.build_llm_payload()
        payload.append({"role": "user", "content": user_input})
        prior = len(payload)  # payload 已含本轮的 user 消息，位于尾部

        yield AgentEvent("agent_start")
        try:
            final_text = yield from self.loop.run(payload)
        finally:
            # 本轮新产生的消息 = 用户消息 + loop 追加到 payload 尾部的那批
            new_messages = payload[prior - 1:]
            session.record_turn(new_messages)
            self._save()
        yield AgentEvent("agent_end", {"text": final_text})

    def clear_history(self):
        """清空对话历史，仅保留 system prompt，并同步存档。"""
        self.session.clear_history()
        self._save()

    def _save(self):
        """存档失败只警告，不打断使用。"""
        if self.repo is None:
            return
        try:
            self.repo.save(self.session)
        except Exception as e:
            print(f">>> 警告：会话存档失败: {e}")
