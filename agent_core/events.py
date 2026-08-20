class AgentEvent:
    """AgentLoop 向外发出的运行时事件：把"发生了什么"和"怎么显示"解耦。

    type    事件类型，如 "api_start" / "text_delta" / "tool_start" / "tool_end" / "error"。
    data    与该事件相关的数据（dict），具体字段由事件类型决定。

    AgentLoop 只负责 emit 事件，不关心谁来显示；
    CLI、Web 或日志都通过注册监听器来消费同一份事件流。
    """

    def __init__(self, type: str, data: dict | None = None):
        self.type = type
        self.data = data or {}
