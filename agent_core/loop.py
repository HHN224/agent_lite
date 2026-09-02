import json

from ai import ProviderError, TextDelta, ToolCall

from .context_manager import ToolResultPruner
from .events import AgentEvent
from .states import AgentState
from .tool_executor import ToolExecutor


class StepResult:
    """单步 step() 的返回值：标记这一轮是否结束，并携带最终回复（若已结束）。"""

    def __init__(self, finished: bool, text: str = ""):
        self.finished = finished
        self.text = text


class AgentLoop:
    """仿 pi-agent 的核心循环：流式调模型 → 若有 tool_calls 则执行 → 循环直到模型直接回复。

    本类只管"接收 messages → 驱动模型 → 追加结果"的机械循环，不关心上下文怎么构建与压缩；
    上下文管理（system prompt、历史累积、消息改写）由 Agent 负责。
    模型访问完全经由 ai 层的 LLMProvider 契约，本层不接触任何具体 API SDK。

    工具输出管理（阶段 C）：大工具结果在回填给模型前，经 ToolResultPruner 做「叠加头尾」截断，
    只保留 head + marker + tail 进上下文，且 tool_call_id 配对不变。
    ToolResultPruner 可注入；缺省用默认阈值（8192 / 4096 / 1024）。

    run() 是生成器薄壳：反复调 step() 直到某轮 finished；单轮逻辑在 step()。
    两者都逐个 yield AgentEvent，消费者（CLI / Web）用 for 迭代。
    最终回复通过生成器 return 值返回（yield from 可捕获）。
    """

    def __init__(
        self,
        provider,
        model,
        tools,
        max_iterations: int = 10,
        permission_policy: str = "ask",
        confirm=None,
        tool_pruner: ToolResultPruner | None = None,
    ):
        self.provider = provider
        self.model = model
        self.tools = tools
        # 工具执行统一交给 ToolExecutor：参数校验、权限判断、超时、审计、结果规范化都在它里面
        self.executor = ToolExecutor(
            tools,
            permission_policy=permission_policy,
            confirm=confirm,
        )
        self.tool_map = self.executor.tool_map
        self.tool_pruner = tool_pruner or ToolResultPruner()
        self.max_iterations = max_iterations
        self.state = AgentState.IDLE
        self._aborted = False

    def abort(self):
        """请求中止当前运行；在下个安全检查点（轮间 / 流式 token 间 / 工具间）安全退出。"""
        self._aborted = True

    def run(self, messages: list):
        """生成器：反复 step() 直到某轮 finished，return 最终回复字符串。

        本方法只管"循环 + 判断是否结束"，单轮逻辑在 step() 里；
        所有事件由 step() 产出，这里用 yield from 透传给消费者。
        """
        self._aborted = False
        for _ in range(self.max_iterations):
            if self._aborted:
                self.state = AgentState.FINISHED
                return "\n(已中止)"
            result = yield from self.step(messages)
            if result.finished:
                return result.text

        self.state = AgentState.FINISHED
        return "\n(已达到最大工具调用轮数，停止循环)"

    def step(self, messages: list):
        """单步生成器：跑一轮（调模型 → 可能执行工具），yield AgentEvent，return StepResult。

        三种结束路径：
          - ProviderError  → StepResult(finished=True, text=错误信息)
          - 模型直接回复    → StepResult(finished=True, text=最终回复)
          - 执行了工具      → StepResult(finished=False)（还需下一轮让模型看到结果）
        """
        self.state = AgentState.THINKING
        yield AgentEvent("turn_start")

        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        message_started = False

        try:
            for event in self.provider.stream(messages, self.tools, self.model):
                if self._aborted:
                    break
                if isinstance(event, TextDelta):
                    if not message_started:
                        yield AgentEvent("message_start")
                        message_started = True
                    yield AgentEvent("message_update", {"content": event.content})
                    text_parts.append(event.content)
                elif isinstance(event, ToolCall):
                    tool_calls.append(event)
        except ProviderError as e:
            # 模型服务故障：结束本轮而不是让整个 REPL 崩溃
            self.state = AgentState.ERROR
            yield AgentEvent("error", {"message": str(e)})
            yield AgentEvent("turn_end")
            return StepResult(finished=True, text=f"(模型服务出错：{e})")

        if message_started:
            yield AgentEvent("message_end")

        # 用户中止：在模型回复后安全退出
        if self._aborted:
            self.state = AgentState.FINISHED
            yield AgentEvent("turn_end")
            return StepResult(finished=True, text="\n(已中止)")

        # 没有工具调用，说明模型已经回答完了
        if not tool_calls:
            self.state = AgentState.FINISHED
            yield AgentEvent("turn_end")
            final_text = "".join(text_parts)
            # 最终回复也要记入历史，否则下一轮与持久化存档都缺这条 assistant 消息
            if final_text:
                messages.append({"role": "assistant", "content": final_text})
            return StepResult(finished=True, text=final_text)

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
        self.state = AgentState.CALLING_TOOL
        for call in tool_calls:
            if self._aborted:
                break
            yield AgentEvent("tool_execution_start", {"name": call.name, "arguments": call.arguments})
            result = self.executor.execute(call)
            # 阶段 C：大工具结果做「叠加头尾」截断（只改 content，tool_call_id 配对不变）
            pruned_content, was_pruned = self.tool_pruner.prune(result.content)
            yield AgentEvent("tool_execution_end", {
                "content": pruned_content,
                "is_error": result.is_error,
                "denied": result.denied,
                "pruned": was_pruned,
            })

            messages.append({
                "role": "tool",
                "tool_call_id": call.id,
                "content": pruned_content,
            })

        # 用户中止：在工具执行后安全退出
        if self._aborted:
            self.state = AgentState.FINISHED
            yield AgentEvent("turn_end")
            return StepResult(finished=True, text="\n(已中止)")

        yield AgentEvent("turn_end")
        return StepResult(finished=False)
