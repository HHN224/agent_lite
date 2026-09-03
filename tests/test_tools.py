import pytest

from coding_agent.sandbox import DockerRunner
from coding_agent.tools import build_tools, safe_path


def test_safe_path_inside_workspace(tmp_path):
    file = safe_path(tmp_path, "a/b.txt")
    assert file == (tmp_path / "a/b.txt").resolve()


def test_safe_path_rejects_parent_traversal(tmp_path):
    with pytest.raises(PermissionError):
        safe_path(tmp_path, "../outside.txt")


def test_safe_path_rejects_absolute_outside(tmp_path):
    with pytest.raises(PermissionError):
        safe_path(tmp_path, str(tmp_path.parent / "other.txt"))


def test_safe_path_accepts_absolute_inside(tmp_path):
    file = safe_path(tmp_path, str(tmp_path / "x.txt"))
    assert file == (tmp_path / "x.txt").resolve()


def test_read_missing_file(tmp_path):
    tools = {t.name: t for t in build_tools(tmp_path)}
    result = tools["read"].execute(path="nope.txt")
    assert result.is_error
    assert "not found" in result.content


def test_write_then_read_round_trip(tmp_path):
    tools = {t.name: t for t in build_tools(tmp_path)}
    result = tools["write"].execute(path="notes.txt", content="hello 世界")
    assert not result.is_error

    # read 现在返回带行号（cat -n 风格）的内容
    result = tools["read"].execute(path="notes.txt")
    assert "hello 世界" in result.content
    assert result.content.startswith("     1\thello 世界")


def test_write_rejects_outside_workspace(tmp_path):
    tools = {t.name: t for t in build_tools(tmp_path)}
    with pytest.raises(PermissionError):
        tools["write"].execute(path="../escape.txt", content="x")


def test_edit_unique_match(tmp_path):
    tools = {t.name: t for t in build_tools(tmp_path)}
    (tmp_path / "f.txt").write_text("a\nb\nc", encoding="utf-8")

    result = tools["edit"].execute(path="f.txt", old_string="b", new_string="B")
    assert not result.is_error
    assert (tmp_path / "f.txt").read_text(encoding="utf-8") == "a\nB\nc"


def test_edit_not_found(tmp_path):
    tools = {t.name: t for t in build_tools(tmp_path)}
    (tmp_path / "f.txt").write_text("abc", encoding="utf-8")

    result = tools["edit"].execute(path="f.txt", old_string="zzz", new_string="x")
    assert result.is_error
    assert "not found" in result.content


def test_edit_ambiguous_match(tmp_path):
    tools = {t.name: t for t in build_tools(tmp_path)}
    (tmp_path / "f.txt").write_text("ab ab ab", encoding="utf-8")

    result = tools["edit"].execute(path="f.txt", old_string="ab", new_string="AB")
    assert result.is_error
    assert "must be unique" in result.content


def test_bash_tool_marks_dangerous(tmp_path):
    tools = {t.name: t for t in build_tools(tmp_path)}
    assert tools["bash"].dangerous is True
    assert tools["write"].dangerous is True
    assert tools["edit"].dangerous is True
    assert tools["read"].dangerous is False


def test_build_tools_has_seven_defaults(tmp_path):
    names = sorted(t.name for t in build_tools(tmp_path))
    assert names == ["bash", "edit", "find", "grep", "ls", "read", "write"]


def test_describe_call_default_uses_repr(tmp_path):
    tools = {t.name: t for t in build_tools(tmp_path)}
    assert "read" in tools["read"].describe_call({"path": "x.txt"})
    assert "'x.txt'" in tools["read"].describe_call({"path": "x.txt"})


def test_describe_call_write_shows_path_and_summary(tmp_path):
    tools = {t.name: t for t in build_tools(tmp_path)}
    desc = tools["write"].describe_call({"path": "notes.md", "content": "line1\nline2"})
    assert "notes.md" in desc
    assert "line1" in desc


def test_describe_call_edit_shows_diff(tmp_path):
    tools = {t.name: t for t in build_tools(tmp_path)}
    desc = tools["edit"].describe_call({"path": "f.txt", "old_string": "a", "new_string": "b"})
    assert "f.txt" in desc
    assert "'a'" in desc and "'b'" in desc


def test_describe_call_bash_shows_command(tmp_path):
    tools = {t.name: t for t in build_tools(tmp_path)}
    desc = tools["bash"].describe_call({"command": "ls -la"})
    assert "ls -la" in desc


class _FakeSubprocessResult:
    """伪造 subprocess.run 的返回值，测试 bash 工具的输出解码路径。"""

    def __init__(self, returncode=0, stdout=b"", stderr=b""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _stub_subprocess(monkeypatch, result):
    monkeypatch.setattr(
        "coding_agent.sandbox.subprocess.run", lambda *a, **k: result
    )


def _bash_tool(tmp_path, monkeypatch, result):
    """构造一个注入 DockerRunner 的 BashTool，并把 subprocess.run 打桩成返回 result。"""
    _stub_subprocess(monkeypatch, result)
    from coding_agent.tools import BashTool
    return BashTool(tmp_path, runner=DockerRunner(tmp_path))


def test_bash_tool_survives_none_stdout(tmp_path, monkeypatch):
    # 回归：Python 3.12 text=True 解码失败时 stdout 可能为 None，历史上会 None + str 崩溃
    tool = _bash_tool(tmp_path, monkeypatch, _FakeSubprocessResult(stdout=None, stderr=b"some stderr"))
    result = tool.execute(command="whatever")
    assert not result.is_error
    assert "some stderr" in result.content


def test_bash_tool_decodes_utf8_stdout(tmp_path, monkeypatch):
    # docker 容器按 UTF-8 输出中文文件名，宿主应正确解码而不是乱码/崩溃
    tool = _bash_tool(tmp_path, monkeypatch, _FakeSubprocessResult(stdout="实习计划\n".encode("utf-8")))
    result = tool.execute(command="ls")
    assert not result.is_error
    assert "实习计划" in result.content


def test_bash_tool_undecodable_bytes_become_replace_char(tmp_path, monkeypatch):
    # 任何字节都不应让工具崩溃，坏字节用替换符兜底
    tool = _bash_tool(tmp_path, monkeypatch, _FakeSubprocessResult(stdout=b"\xff\xfe\xff abc"))
    result = tool.execute(command="ls")
    assert not result.is_error
    assert "abc" in result.content


def test_bash_tool_no_output_reports_exit_code(tmp_path, monkeypatch):
    # 非零退出码：无输出时也要让模型看到退出码，且标记为失败
    tool = _bash_tool(tmp_path, monkeypatch, _FakeSubprocessResult(returncode=3))
    result = tool.execute(command="exit 3")
    assert result.is_error
    assert result.exit_code == 3
    assert "exit code 3" in result.content


def test_bash_tool_structured_fields(tmp_path, monkeypatch):
    # 结构化返回：exit_code / stdout / stderr 分开携带，非零退出码标记失败
    tool = _bash_tool(tmp_path, monkeypatch, _FakeSubprocessResult(returncode=1, stdout=b"out line", stderr=b"err line"))
    result = tool.execute(command="false")
    assert result.is_error
    assert result.exit_code == 1
    assert result.stdout == "out line"
    assert result.stderr == "err line"
    assert "out line" in result.content and "err line" in result.content


def test_bash_tool_success_zero_exit(tmp_path, monkeypatch):
    tool = _bash_tool(tmp_path, monkeypatch, _FakeSubprocessResult(returncode=0, stdout=b"ok"))
    result = tool.execute(command="echo ok")
    assert not result.is_error
    assert result.exit_code == 0
    assert result.stdout == "ok"


def test_bash_image_configurable(tmp_path, monkeypatch):
    # 运行镜像可配置：注入的 DockerRunner 携带镜像，build_tools 透传 bash_image
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        return _FakeSubprocessResult()

    monkeypatch.setattr("coding_agent.sandbox.subprocess.run", fake_run)
    tools = {t.name: t for t in build_tools(tmp_path, bash_image="custom:1.0")}
    tools["bash"].execute(command="true")
    assert "custom:1.0" in captured["args"]
    assert tools["bash"].image == "custom:1.0"


# --------------------------------------------------------------------------- #
# Phase 1: grep / ls / find + read offset+limit / bash timeout
# --------------------------------------------------------------------------- #


def test_read_offset_and_limit(tmp_path):
    tools = {t.name: t for t in build_tools(tmp_path)}
    (tmp_path / "f.txt").write_text("a\nb\nc\nd\ne", encoding="utf-8")

    result = tools["read"].execute(path="f.txt", offset=2, limit=3)
    assert not result.is_error
    # 行号为 2,3,4（cat -n 风格）
    assert result.content == "     2\tb\n     3\tc\n     4\td"


def test_read_line_numbers_cat_n(tmp_path):
    tools = {t.name: t for t in build_tools(tmp_path)}
    (tmp_path / "f.txt").write_text("x\ny", encoding="utf-8")
    result = tools["read"].execute(path="f.txt")
    assert result.content == "     1\tx\n     2\ty"


def test_read_image_returns_data_uri(tmp_path):
    tools = {t.name: t for t in build_tools(tmp_path)}
    (tmp_path / "img.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8)
    result = tools["read"].execute(path="img.png")
    assert not result.is_error
    assert result.content.startswith("data:image/png;base64,")


def test_bash_tool_timeout_param(tmp_path, monkeypatch):
    captured = {}

    def fake_run(args, **kwargs):
        captured["timeout"] = kwargs.get("timeout")
        return _FakeSubprocessResult()

    monkeypatch.setattr("coding_agent.sandbox.subprocess.run", fake_run)
    tools = {t.name: t for t in build_tools(tmp_path)}
    tools["bash"].execute(command="sleep 10", timeout=5)
    assert captured["timeout"] == 5


def test_bash_tool_default_timeout_uses_runner(tmp_path, monkeypatch):
    captured = {}

    def fake_run(args, **kwargs):
        captured["timeout"] = kwargs.get("timeout")
        return _FakeSubprocessResult()

    monkeypatch.setattr("coding_agent.sandbox.subprocess.run", fake_run)
    tools = {t.name: t for t in build_tools(tmp_path)}
    tools["bash"].execute(command="true")
    # 未传 timeout 时应使用 runner 默认（DockerRunner 默认 60）
    assert captured["timeout"] == 60


def test_grep_literal_finds_match(tmp_path):
    tools = {t.name: t for t in build_tools(tmp_path)}
    (tmp_path / "a.py").write_text("import os\nprint('hello')\n", encoding="utf-8")
    result = tools["grep"].execute(pattern="print", glob="*.py")
    assert not result.is_error
    assert "a.py:2:" in result.content
    assert "print" in result.content


def test_grep_no_match(tmp_path):
    tools = {t.name: t for t in build_tools(tmp_path)}
    (tmp_path / "a.txt").write_text("nothing here", encoding="utf-8")
    result = tools["grep"].execute(pattern="zzz")
    assert not result.is_error
    assert "No matches found" in result.content


def test_grep_limit(tmp_path):
    tools = {t.name: t for t in build_tools(tmp_path)}
    (tmp_path / "a.txt").write_text("x\nx\nx\nx", encoding="utf-8")
    result = tools["grep"].execute(pattern="x", limit=2)
    assert not result.is_error
    assert "match limit reached" in result.content
    assert result.content.count("a.txt:") == 2


def test_ls_lists_entries(tmp_path):
    tools = {t.name: t for t in build_tools(tmp_path)}
    (tmp_path / "file1.txt").write_text("hi", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    result = tools["ls"].execute(path="")
    assert not result.is_error
    assert "file1.txt" in result.content
    assert "sub/" in result.content


def test_ls_empty_dir(tmp_path):
    tools = {t.name: t for t in build_tools(tmp_path)}
    result = tools["ls"].execute(path="")
    assert not result.is_error
    assert "empty directory" in result.content


def test_find_by_name_glob(tmp_path):
    tools = {t.name: t for t in build_tools(tmp_path)}
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    (tmp_path / "data.txt").write_text("x", encoding="utf-8")
    result = tools["find"].execute(name="*.json")
    assert not result.is_error
    assert "config.json" in result.content
    assert "data.txt" not in result.content


def test_find_by_type_dir(tmp_path):
    tools = {t.name: t for t in build_tools(tmp_path)}
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("x", encoding="utf-8")
    result = tools["find"].execute(name="*", type="d")
    assert not result.is_error
    assert "src" in result.content
    assert "main.py" not in result.content


def test_find_invalid_type(tmp_path):
    tools = {t.name: t for t in build_tools(tmp_path)}
    result = tools["find"].execute(name="*", type="x")
    assert result.is_error
    assert "type must be" in result.content


def test_find_size_min(tmp_path):
    tools = {t.name: t for t in build_tools(tmp_path)}
    (tmp_path / "big.bin").write_bytes(b"\x00" * 100)
    (tmp_path / "small.txt").write_text("hi", encoding="utf-8")
    result = tools["find"].execute(name="*", type="f", size_min=50)
    assert not result.is_error
    assert "big.bin" in result.content
    assert "small.txt" not in result.content
