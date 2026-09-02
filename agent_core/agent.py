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

    上下文管理（阶段 A：计量 + 触发）：
      每轮 build payload 之前，通过 context_manager 量出当前上下文占用并判定是否达到
      压缩阈值，把结果以 AgentEvent("context_check") 发射出来供展示/观测；
      真正的压缩动作属于阶段 B，此处不做。context_window 为当前窗口（如 128000）。
    """

    def __init__(
        self,
        loop,
        session: Session,
        repo: SessionRepository | None = None,
        context_manager=None,
        context_window: int = 0,
    ):
        self.loop = loop
        self.session = session
        self.repo = repo
        self.context_manager = context_manager
        self.context_window = context_window

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

    def _context_check_event(self, pending_messages=None):
        """在 build payload 前量出上下文占用并发射观测事件（阶段 A 只观测，不压缩）。

        计量采用「锚点 + 增量」：
          锚点 = provider 上一次成功调用的真实 usage 总量，并绑定当时的会话 head，
                作为「增量从哪起算」的边界；
          增量 = 锚点之后新 append 进 session 的条目 + 本轮 pending 的 user 消息（启发式估算）。
        这样即便锚点落后于当前会话，也能算出当前真实占用，避免漏检。
        """
        if self.context_manager is None or not self.context_window:
            return None
        # 用 provider 最近一次真实 usage 构造锚点，并记录当时的会话 head 边界
        anchor = None
        usage = getattr(self.loop.provider, "last_usage", None)
        if usage:
            from .context_manager import UsageAnchor, usage_total

            total = usage_total(usage)
            if total is not None:
                anchor = UsageAnchor(total_tokens=total, head_id=self.session.head_id)
        pressure = self.context_manager.measure(
            self.session,
            self.context_window,
            anchor=anchor,
            pending_messages=pending_messages,
        )
        needs_compaction = self.context_manager.requires_compaction(pressure)
        event = AgentEvent("context_check", {
            "total_tokens": pressure.total_tokens,
            "context_window": pressure.context_window,
            "ratio": pressure.ratio,
            "used_anchor": pressure.used_anchor,
            "incremental": pressure.incremental,
            "needs_compaction": needs_compaction,
            "threshold_ratio": self.context_manager.threshold_ratio,
        })
        return pressure, event

    def prompt(self, user_input: str):
        """追加一条用户消息并驱动循环，生成器 yield AgentEvent，结束后自动存档。

        对话历史由 session 维护：每次先用 build_llm_payload() 重建发给模型的列表
        （system → 压缩 summary → 保留消息），再把本轮新消息记录回 session。
        AgentLoop 会在运行过程中把模型回复与工具结果追加到 payload 尾部。

        阶段 A：在 build payload 前发射一个 context_check 事件（把本轮 user 输入
        作为 pending 一起计量），让调用方看到「当前占用 / 是否到阈值」，但不执行压缩（阶段 B）。
        """
        session = self.session

        # 本轮的 user 输入即将发给模型但尚未 record 进 session，作为 pending 参与计量
        pending = [{"role": "user", "content": user_input}]
        check = self._context_check_event(pending_messages=pending)
        if check is not None:
            _pressure, event = check
            yield event

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
