import json
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout

from ai import ProviderError, TextDelta, ToolCall

from .agent_tools import ToolResult
from .events import AgentEvent


class AgentLoop:
    """仿 pi-agent 的核心循环：流式调模型 → 若有 tool_calls 则执行 → 循环直到模型直接回复。

    本类只管"接收 messages → 驱动模型 → 追加结果"的机械循环，不关心上下文怎么构建与压缩；
    上下文管理（system prompt、历史累积、消息改写）由 Agent 负责。
    模型访问完全经由 ai 层的 LLMProvider 契约，本层不接触任何具体 API SDK。

    运行过程中只 emit AgentEvent，不做任何 print / UI；
    显示由外部通过 add_listener 注册的监听器负责（CLI、Web、日志等）。
    """

    def __init__(self, provider, model, tools, max_iterations: int = 10):
        self.provider = provider
        self.model = model
        self.tools = tools
        self.tool_map = {t.name: t for t in tools}
        self.max_iterations = max_iterations
        self._listeners: list = []

    def add_listener(self, fn):
        """注册一个事件监听器；emit 事件时会依次调用每个监听器 fn(event)。"""
        self._listeners.append(fn)

    def emit(self, event: AgentEvent):
        """把事件分发给所有已注册的监听器。"""
        for fn in self._listeners:
            fn(event)

    def run(self, messages: list) -> str:
        for _ in range(self.max_iterations):
            text_parts: list[str] = []
            tool_calls: list[ToolCall] = []

            self.emit(AgentEvent("api_start"))
            try:
                for event in self.provider.stream(messages, self.tools, self.model):
                    if isinstance(event, TextDelta):
                        self.emit(AgentEvent("text_delta", {"content": event.content}))
                        text_parts.append(event.content)
                    elif isinstance(event, ToolCall):
                        tool_calls.append(event)
            except ProviderError as e:
                # 模型服务故障：结束本轮而不是让整个 REPL 崩溃
                self.emit(AgentEvent("error", {"message": str(e)}))
                return f"(模型服务出错：{e})"

            if text_parts:
                self.emit(AgentEvent("text_end"))

            # 没有工具调用，说明模型已经回答完了
            if not tool_calls:
                return "".join(text_parts)

            # 把这一轮 assistant 消息以标准 dict 形式记入历史
            messages.append({
                "role": "assistant",
                "content": "".join(text_parts) or None,
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": json.dumps(call.arguments, ensure_ascii=False),
                        },
                    }
                    for call in tool_calls
                ],
            })

            # 依次执行工具并回填结果
            for call in tool_calls:
                self.emit(AgentEvent("tool_start", {"name": call.name, "arguments": call.arguments}))
                result = self._execute(call)
                self.emit(AgentEvent("tool_end", {"content": result.content, "is_error": result.is_error}))

                messages.append({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": result.content,
                })

        return "\n(已达到最大工具调用轮数，停止循环)"

    def _execute(self, call: ToolCall) -> ToolResult:
        """执行单个工具调用；任何错误或超时都转成 ToolResult(is_error=True) 回填给模型，而不是让循环崩溃。"""
        tool = self.tool_map.get(call.name)
        if tool is None:
            return ToolResult(content=f"Error: unknown tool '{call.name}'", is_error=True)

        errors = tool.validate_arguments(call.arguments)
        if errors:
            return ToolResult(content="Error: " + "; ".join(errors), is_error=True)

        # 在独立线程里执行工具，主线程用 future.result(timeout=...) 限时等待。
        # 超时阈值取自工具自身的 timeout metadata；每个工具可以声明不同的时限。
        # 注意：超时后线程无法被强制终止（Python 线程的限制），因此用 shutdown(wait=False)
        # 立即放手——主流程继续，卡死的工具线程在进程退出时随主进程结束。
        executor = ThreadPoolExecutor(max_workers=1)
        try:
            future = executor.submit(tool.execute, **call.arguments)
            return future.result(timeout=tool.timeout)
        except FutureTimeout:
            return ToolResult(content="Tool timeout", is_error=True)
        except Exception as e:
            return ToolResult(content=f"Error: {e}", is_error=True)
        finally:
            executor.shutdown(wait=False)
