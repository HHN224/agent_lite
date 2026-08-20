from abc import ABC, abstractmethod

from ai import Tool


class ToolResult:
    """工具 execute() 的统一返回类型：把"内容"与"是否出错"分开携带。

    content    要回填给模型的字符串结果。
    is_error   True 表示这是一次失败的结果（如权限拒绝、文件不存在、命令超时）。
    """

    def __init__(
        self,
        content: str,
        is_error: bool = False,
    ):
        self.content = content
        self.is_error = is_error


class AgentTool(Tool, ABC):
    """接口层：在 ai 层 Tool 三元组之上扩展出 execute()。

    只声明"具体工具必须提供 execute"这一契约，不实现任何功能。
    具体工具由上层继承本类并实现 execute() 来提供。
    """

    @abstractmethod
    def execute(self, **kwargs) -> ToolResult:
        """执行工具，返回 ToolResult（content + is_error）。"""
