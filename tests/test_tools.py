import pytest

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

    result = tools["read"].execute(path="notes.txt")
    assert result.content == "hello 世界"


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
    assert tools["read"].dangerous is False


def test_build_tools_has_four_defaults(tmp_path):
    names = sorted(t.name for t in build_tools(tmp_path))
    assert names == ["bash", "edit", "read", "write"]


class _FakeSubprocessResult:
    """伪造 subprocess.run 的返回值，测试 bash 工具的输出解码路径。"""

    def __init__(self, returncode=0, stdout=b"", stderr=b""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _stub_subprocess(monkeypatch, result):
    monkeypatch.setattr(
        "coding_agent.tools.subprocess.run", lambda *a, **k: result
    )


def test_bash_tool_survives_none_stdout(tmp_path, monkeypatch):
    # 回归：Python 3.12 text=True 解码失败时 stdout 可能为 None，历史上会 None + str 崩溃
    _stub_subprocess(
        monkeypatch,
        _FakeSubprocessResult(stdout=None, stderr=b"some stderr"),
    )
    tools = {t.name: t for t in build_tools(tmp_path)}
    result = tools["bash"].execute(command="whatever")
    assert not result.is_error
    assert "some stderr" in result.content


def test_bash_tool_decodes_utf8_stdout(tmp_path, monkeypatch):
    # docker 容器按 UTF-8 输出中文文件名，宿主应正确解码而不是乱码/崩溃
    _stub_subprocess(
        monkeypatch,
        _FakeSubprocessResult(stdout="实习计划\n".encode("utf-8")),
    )
    tools = {t.name: t for t in build_tools(tmp_path)}
    result = tools["bash"].execute(command="ls")
    assert not result.is_error
    assert "实习计划" in result.content


def test_bash_tool_undecodable_bytes_become_replace_char(tmp_path, monkeypatch):
    # 任何字节都不应让工具崩溃，坏字节用替换符兜底
    _stub_subprocess(
        monkeypatch,
        _FakeSubprocessResult(stdout=b"\xff\xfe\xff abc"),
    )
    tools = {t.name: t for t in build_tools(tmp_path)}
    result = tools["bash"].execute(command="ls")
    assert not result.is_error
    assert "abc" in result.content


def test_bash_tool_no_output_reports_exit_code(tmp_path, monkeypatch):
    _stub_subprocess(monkeypatch, _FakeSubprocessResult(returncode=3))
    tools = {t.name: t for t in build_tools(tmp_path)}
    result = tools["bash"].execute(command="exit 3")
    assert not result.is_error
    assert "exit code 3" in result.content