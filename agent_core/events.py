class AgentEvent:
    """Agent 运行时事件：把"发生了什么"和"怎么显示"彻底解耦。

    type    事件类型（字符串）。
    data    与该事件相关的数据（dict），具体字段由事件类型决定。

    事件由 AgentLoop.run() 和 Agent.prompt() 以 yield 方式产出；
    消费者（CLI / Web / 日志）用 for event in ... 迭代，一个核心多种前端。

    事件层级（外层包内层）：

        agent_start                         一次 prompt() 的开始
          turn_start                        一轮 API 调用 + 工具执行
            message_start                   模型文本输出开始
              message_update                流式 token（data: content）
            message_end                     模型文本输出结束
            tool_execution_start            工具开始执行（data: name, arguments）
              tool_execution_update         工具执行进度（暂不发射，未来 hook）
            tool_execution_end              工具执行结束（data: content, is_error）
          turn_end                          一轮结束
        agent_end                           一次 prompt() 的结束（data: text）

    error                                 模型服务故障（data: message）
    """

    def __init__(self, type: str, data: dict | None = None):
        self.type = type
        self.data = data or {}
