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