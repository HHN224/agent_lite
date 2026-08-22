import pytest

from agent_core import PermissionPolicy, ToolExecutor, ToolResult
from agent_core.agent_tools import AgentTool
from ai import ToolCall


class DangerousWriteTool(AgentTool):
    """测试用危险工具：dangerous=True，执行会留下副作用（创建文件）。"""

    argument_types = {"path": str, "content": str}

    def __init__(self, root):
        self.root = root
        super().__init__(
            name="danger_write",
            description="Write a file (dangerous)",
            parameters={"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}},
            timeout=5,
            dangerous=True,
        )

    def describe_call(self, arguments: dict) -> str:
        return f"写入 {arguments.get('path')}"

    def execute(self, path: str, content: str) -> ToolResult:
        (self.root / path).write_text(content, encoding="utf-8")
        return ToolResult(content=f"wrote {path}")


class SafeTool(AgentTool):
    """测试用安全工具：不受权限策略约束。"""

    argument_types = {"text": str}

    def __init__(self):
        super().__init__(
            name="safe_echo",
            description="Echo (safe)",
            parameters={"type": "object", "properties": {"text": {"type": "string"}}},
            timeout=5,
            dangerous=False,
        )

    def execute(self, text: str) -> ToolResult:
        return ToolResult(content=f"echo:{text}")


def make_call(name="danger_write", arguments=None):
    return ToolCall(id="t1", name=name, arguments=arguments or {"path": "f.txt", "content": "x"})


def test_default_policy_is_ask():
    executor = ToolExecutor(tools=[])
    assert executor.permission_policy == PermissionPolicy.ASK


def test_unknown_tool_is_error():
    executor = ToolExecutor(tools=[], permission_policy="auto")
    result = executor.execute(ToolCall(id="t1", name="nope", arguments={}))
    assert result.is_error
    assert "unknown tool" in result.content


def test_argument_validation_is_error():
    executor = ToolExecutor(tools=[DangerousWriteTool(__import__("tempfile").mkdtemp())], permission_policy="auto")
    result = executor.execute(ToolCall(id="t1", name="danger_write", arguments={"path": "x"}))
    assert result.is_error
    assert "content is required" in result.content


def test_deny_policy_blocks_dangerous_no_side_effect(tmp_path):
    executor = ToolExecutor(tools=[DangerousWriteTool(tmp_path)], permission_policy="deny")
    result = executor.execute(make_call())
    assert result.is_error
    assert result.denied is True
    assert "permission denied" in result.content
    # 拒绝不产生副作用：文件没有被创建
    assert not (tmp_path / "f.txt").exists()


def test_deny_policy_allows_safe_tools(tmp_path):
    executor = ToolExecutor(tools=[SafeTool(), DangerousWriteTool(tmp_path)], permission_policy="deny")
    result = executor.execute(make_call("safe_echo", {"text": "hi"}))
    assert not result.is_error
    assert result.content == "echo:hi"


def test_ask_policy_confirmed_executes(tmp_path):
    executor = ToolExecutor(
        tools=[DangerousWriteTool(tmp_path)],
        permission_policy="ask",
        confirm=lambda desc: True,
    )
    result = executor.execute(make_call())
    assert not result.is_error
    assert (tmp_path / "f.txt").read_text(encoding="utf-8") == "x"


def test_ask_policy_rejected_denies(tmp_path):
    executor = ToolExecutor(
        tools=[DangerousWriteTool(tmp_path)],
        permission_policy="ask",
        confirm=lambda desc: False,
    )
    result = executor.execute(make_call())
    assert result.is_error
    assert result.denied is True
    assert not (tmp_path / "f.txt").exists()


def test_ask_policy_shows_description(tmp_path):
    seen = []

    def confirm(desc):
        seen.append(desc)
        return True

    executor = ToolExecutor(tools=[DangerousWriteTool(tmp_path)], permission_policy="ask", confirm=confirm)
    executor.execute(make_call(arguments={"path": "g.txt", "content": "y"}))
    assert seen == ["写入 g.txt"]


def test_auto_policy_skips_confirm(tmp_path):
    called = {"confirm": False}

    def confirm(desc):
        called["confirm"] = True
        return True

    executor = ToolExecutor(
        tools=[DangerousWriteTool(tmp_path)],
        permission_policy="auto",
        confirm=confirm,
    )
    result = executor.execute(make_call())
    assert not result.is_error
    assert not called["confirm"]


def test_safe_tool_skips_confirm(tmp_path):
    called = {"confirm": False}

    def confirm(desc):
        called["confirm"] = True
        return True

    executor = ToolExecutor(
        tools=[SafeTool()],
        permission_policy="ask",
        confirm=confirm,
    )
    result = executor.execute(make_call("safe_echo", {"text": "hi"}))
    assert not result.is_error
    assert not called["confirm"]


def test_audit_log_records_calls(tmp_path):
    executor = ToolExecutor(
        tools=[DangerousWriteTool(tmp_path)],
        permission_policy="deny",
    )
    executor.execute(make_call())
    assert len(executor.audit_log) == 1
    entry = executor.audit_log[0]
    assert entry["tool"] == "danger_write"
    assert entry["denied"] is True
    assert entry["is_error"] is True


def test_audit_log_records_unknown_tool():
    executor = ToolExecutor(tools=[], permission_policy="auto")
    executor.execute(ToolCall(id="t1", name="nope", arguments={}))
    assert executor.audit_log[-1]["tool"] == "nope"


def test_invalid_policy_raises():
    with pytest.raises(ValueError):
        ToolExecutor(tools=[], permission_policy="whatever")


def test_default_confirm_denies_on_eof(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda *a, **k: (_ for _ in ()).throw(EOFError()))
    from agent_core.tool_executor import default_confirm
    assert default_confirm("desc") is False


def test_default_confirm_accepts_y(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda *a, **k: "y")
    from agent_core.tool_executor import default_confirm
    assert default_confirm("desc") is True


def test_default_confirm_accepts_yes_upper(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda *a, **k: "YES")
    from agent_core.tool_executor import default_confirm
    assert default_confirm("desc") is True


def test_default_confirm_denies_other(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda *a, **k: "nope")
    from agent_core.tool_executor import default_confirm
    assert default_confirm("desc") is False