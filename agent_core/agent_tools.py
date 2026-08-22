from abc import ABC, abstractmethod

from ai import Tool


class ToolResult:
    """工具 execute() 的统一返回类型：内容与元信息分开携带。

    content    要回填给模型的字符串结果。
    is_error   True 表示这是一次失败的结果（如权限拒绝、文件不存在、命令超时）。
    denied     本次调用是否因权限策略被拒绝（未产生副作用）。
    exit_code  Bash 等命令类工具的退出码；非命令工具为 None。
    stdout     Bash 的标准输出（原始文本，未拼接 stderr）。
    stderr     Bash 的标准错误。
    """

    def __init__(
        self,
        content: str,
        is_error: bool = False,
        denied: bool = False,
        exit_code: int | None = None,
        stdout: str = "",
        stderr: str = "",
    ):
        self.content = content
        self.is_error = is_error
        self.denied = denied
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr


class AgentTool(Tool, ABC):
    """接口层：在 ai 层 Tool 三元组之上扩展出 execute()。

    只声明"具体工具必须提供 execute"这一契约，不实现任何功能。
    具体工具由上层继承本类并实现 execute() 来提供。
    """

    argument_types: dict = {}

    def validate_arguments(self, args: dict) -> list[str]:
        """自检参数：返回错误消息列表，空列表表示通过。

        只做最简单的"必填 + 类型"两项检查，不解析 JSON Schema。
        子类通过覆盖 argument_types 声明每个参数期望的 Python 类型，例如：
            argument_types = {"path": str}
        """
        if not isinstance(args, dict):
            return ["arguments should be a JSON object"]
        errors = []
        for key, typ in self.argument_types.items():
            if key not in args:
                errors.append(f"{key} is required")
            elif not isinstance(args[key], typ):
                errors.append(f"{key} should be {typ.__name__}, got {type(args[key]).__name__}")
        return errors

    def describe_call(self, arguments: dict) -> str:
        """把一次调用翻译成给用户看的人类可读描述（权限确认时展示）。

        默认展示工具名与参数；写文件 / 编辑 / 执行命令等工具应覆盖，
        用「路径 / 变更摘要 / 命令」让用户能看懂即将发生什么。
        """
        return f"{self.name}({arguments!r})"

    @abstractmethod
    def execute(self, **kwargs) -> ToolResult:
        """执行工具，返回 ToolResult（content + is_error）。"""
