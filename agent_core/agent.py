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

      计量口径为「两变量 + 校准式」：
        session.usage     老历史真实基准；每轮结束后用 provider 本轮真实 last_usage 校准
        session.new_usage 本轮启动后新增内容（本轮 user 输入 + 上轮输出/工具结果）的估算，每轮归零
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

    def _context_check_event(self):
        """在 build payload 前量出上下文占用并发射观测事件（阶段 A 只观测，不压缩）。

        计量口径为「两变量 + 校准式」：total = session.usage + session.new_usage。
        session.new_usage 在每轮开始时由 prompt() 负责累积（本轮 user 输入 + 上轮输出/工具结果）。
        这里只负责量出并判定，返回 (pressure, event)。
        """
        if self.context_manager is None or not self.context_window:
            return None
        pressure = self.context_manager.measure(self.session, self.context_window)
        needs_compaction = self.context_manager.requires_compaction(pressure)
        event = AgentEvent("context_check", {
            "total_tokens": pressure.total_tokens,
            "context_window": pressure.context_window,
            "ratio": pressure.ratio,
            "usage": pressure.usage,
            "new_usage": pressure.new_usage,
            "needs_compaction": needs_compaction,
            "threshold_ratio": self.context_manager.threshold_ratio,
        })
        return pressure, event

    def _accumulate_new_usage(self, messages: list[dict]):
        """把一段消息的估算 token 累加进 session.new_usage（表：还没被 usage 覆盖的新增）。"""
        if self.context_manager is None:
            return
        meter = self.context_manager.meter
        self.session.new_usage += sum(meter.estimate_message(m) for m in messages)

    def _set_new_usage(self, messages: list[dict]):
        """把 session.new_usage 重置为「一段消息」的估算值。

        用于校准式口径的收尾：此时 session.usage 已被 provider 真实 last_usage 校准为
        「模型本轮实际看到的输入」，new_usage 应只保留「输出后新增、下一轮待估」的部分，
        因此整体重置而不是继续累加，避免与已校准进 usage 的内容重复计数。
        """
        if self.context_manager is None:
            return
        meter = self.context_manager.meter
        self.session.new_usage = sum(meter.estimate_message(m) for m in messages)

    def prompt(self, user_input: str):
        """追加一条用户消息并驱动循环，生成器 yield AgentEvent，结束后自动存档。

        对话历史由 session 维护：每次先用 build_llm_payload() 重建发给模型的列表
        （system → 压缩 summary → 保留消息），再把本轮新消息记录回 session。
        AgentLoop 会在运行过程中把模型回复与工具结果追加到 payload 尾部。

        阶段 A 生命周期（校准式 + 两变量）：
          开场：把本轮 user 输入累加进 new_usage，量出占用、发射 context_check 事件；
          收尾：用 provider 本轮真实 last_usage 校准 session.usage（= 模型本轮实际看到的输入），
                再把 new_usage 重置为「本轮输出侧新增」（assistant 回复 / 工具结果的估算），
                这样 usage 代表「真实输入」，new_usage 代表「输出之后又新增的待估部分」。
        """
        session = self.session

        # 开场：本轮 user 输入即将发给模型但尚未 record 进 session，累加进 new_usage
        self._accumulate_new_usage([{"role": "user", "content": user_input}])
        check = self._context_check_event()
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
            # 本轮 loop 追加到 payload 尾部的消息 = 输出侧新增（assistant 回复 / 工具结果）
            produced = payload[prior:]
            # 校准：provider 本轮真实 usage 的**输入侧**（prompt_tokens）等于
            # 「system + 历史 + 本轮 input」这个模型实际看到的上下文，用作 session.usage。
            # （不用 total_tokens，因为它已含 completion，会造成与 new_usage 里的输出侧新增重复计数。）
            usage = getattr(self.loop.provider, "last_usage", None)
            from .context_manager import usage_input_total

            real_input = usage_input_total(usage) if usage else None
            if real_input is not None:
                session.usage = real_input
            # new_usage 重置为输出侧新增（避免与已校准进 usage 的输入重复计数）
            self._set_new_usage(produced)
            # record_turn 会把本轮新消息追加进历史（含 user 输入），并 touch + bump token_count
            session.record_turn(payload[prior - 1:])
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
