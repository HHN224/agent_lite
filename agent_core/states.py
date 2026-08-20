from enum import Enum


class AgentState(Enum):
    """Agent 运行时的状态机：外部可查询 agent.state 得知当前在干嘛。

    IDLE          空闲，未在运行
    THINKING      正在调用模型（provider.stream）
    CALLING_TOOL  正在执行工具
    FINISHED      一次 run 已正常结束
    ERROR         一次 run 因故障结束

    状态在 AgentLoop.step() 的各阶段切换；生成器停在 yield 时，
    state 反映的是最近一次设置的阶段，外部可在事件间隙查询。
    """

    IDLE = "idle"
    THINKING = "thinking"
    CALLING_TOOL = "calling_tool"
    FINISHED = "finished"
    ERROR = "error"
