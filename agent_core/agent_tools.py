from abc import ABC, abstractmethod

from ai import Tool


class AgentTool(Tool, ABC):
    """接口层：在 ai 层 Tool 三元组之上扩展出 execute()。

    只声明"具体工具必须提供 execute"这一契约，不实现任何功能。
    具体工具由上层 coding agent 层继承本类并实现 execute() 来提供。
    """

    @abstractmethod
    def execute(self, **kwargs) -> str:
        """执行工具，返回给模型的字符串结果。"""
