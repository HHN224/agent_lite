"""统一的工具执行器：参数校验 → 权限判断 → 限时执行 → 审计 → 结果规范化。

对齐 pi-agent 的 ToolExecutor 设计，从 AgentLoop 抽出：
  - 参数校验（AgentTool.validate_arguments）
  - 权限策略（ask / deny / auto，只约束危险工具）
  - 超时控制（独立线程 + future.result(timeout)）
  - 审计日志（audit_log 记录每次调用的参数与结果）
  - 结果规范化（拒绝 / 超时 / 异常都转成 ToolResult(is_error=True)，不让循环崩溃）
"""

import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from enum import Enum

from ai import ToolCall

from .agent_tools import AgentTool, ToolResult


class PermissionPolicy(str, Enum):
    """危险工具（dangerous=True）的权限策略：

    ask   每次调用都询问用户确认（默认）
    deny  直接拒绝所有危险工具调用
    auto  自动放行，不再打扰用户

    安全工具（如 read）不受策略约束，始终直接执行。
    """

    ASK = "ask"
    DENY = "deny"
    AUTO = "auto"


def default_confirm(description: str) -> bool:
    """默认确认函数：交互式终端里询问 y/N；非交互环境（EOF）视为拒绝。"""
    try:
        answer = input(f">>> {description}\n>>> 是否允许执行？[y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer in ("y", "yes")


class ToolExecutor:
    """在 AgentLoop 与具体工具之间插入的统一执行器。

    execute(call) 依次完成：
      1. 查找工具（未知工具 → 错误结果）
      2. 参数校验（不合法 → 错误结果）
      3. 权限判断（危险工具按 ask / deny / auto 裁决；拒绝 → 错误结果，无副作用）
      4. 限时执行（超时 → 错误结果）
      5. 审计记录，返回规范化结果

    任何一步失败都以 ToolResult(is_error=True) 回传模型，而不是抛异常中断循环。
    """

    def __init__(
        self,
        tools: list[AgentTool],
        permission_policy: str | PermissionPolicy = PermissionPolicy.ASK,
        confirm=None,
    ):
        self.tool_map = {t.name: t for t in tools}
        self.permission_policy = PermissionPolicy(permission_policy)
        self.confirm = confirm or default_confirm
        self.audit_log: list[dict] = []

    def execute(self, call: ToolCall) -> ToolResult:
        tool = self.tool_map.get(call.name)
        if tool is None:
            result = ToolResult(content=f"Error: unknown tool '{call.name}'", is_error=True)
        else:
            errors = tool.validate_arguments(call.arguments)
            if errors:
                result = ToolResult(content="Error: " + "; ".join(errors), is_error=True)
            elif not self._ensure_permission(tool, call):
                result = ToolResult(
                    content=(
                        f"Error: permission denied for tool '{call.name}'. "
                        "Use `--permission-policy=auto` to allow automatic execution."
                    ),
                    is_error=True,
                    denied=True,
                )
            else:
                result = self._run(tool, call.arguments)

        self.audit_log.append({
            "timestamp": time.time(),
            "tool": call.name,
            "arguments": call.arguments,
            "content": result.content,
            "is_error": result.is_error,
            "denied": result.denied,
        })
        return result

    def _ensure_permission(self, tool: AgentTool, call: ToolCall) -> bool:
        """危险工具按策略裁决；安全工具不打扰用户直接放行。"""
        if not tool.dangerous:
            return True
        if self.permission_policy == PermissionPolicy.DENY:
            return False
        if self.permission_policy == PermissionPolicy.ASK:
            return self.confirm(tool.describe_call(call.arguments))
        return True  # AUTO

    def _run(self, tool: AgentTool, arguments: dict) -> ToolResult:
        """在独立线程里限时执行工具；超时 / 异常都转成错误结果，不让循环崩溃。

        注意：超时后线程无法被强制终止（Python 线程的限制），因此用 shutdown(wait=False)
        立即放手——主流程继续，卡死的工具线程在进程退出时随主进程结束。
        """
        executor = ThreadPoolExecutor(max_workers=1)
        try:
            future = executor.submit(tool.execute, **arguments)
            return future.result(timeout=tool.timeout)
        except FutureTimeout:
            return ToolResult(content="Tool timeout", is_error=True)
        except Exception as e:
            return ToolResult(content=f"Error: {e}", is_error=True)
        finally:
            executor.shutdown(wait=False)