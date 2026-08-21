from pathlib import Path

from coding_agent.tools import ReadTool


def make_tool():
    return ReadTool(Path("."))


def test_missing_required():
    assert make_tool().validate_arguments({}) == ["path is required"]


def test_wrong_type():
    errors = make_tool().validate_arguments({"path": 123})
    assert errors == ["path should be str, got int"]


def test_valid_passes():
    assert make_tool().validate_arguments({"path": "x.txt"}) == []


def test_extra_keys_ignored():
    assert make_tool().validate_arguments({"path": "x", "extra": 1}) == []


def test_non_dict_arguments():
    errors = make_tool().validate_arguments(["not", "a", "dict"])
    assert errors == ["arguments should be a JSON object"]