import pytest

from agent_core import SessionStore


def test_save_load_round_trip(tmp_path):
    store = SessionStore(tmp_path / "s.json")
    messages = [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "世界"},
    ]
    store.save("sys", messages)

    assert store.load() == ("sys", messages)


def test_save_creates_parent_dirs(tmp_path):
    store = SessionStore(tmp_path / "a" / "b" / "s.json")
    store.save("sys", [])

    assert store.load() == ("sys", [])


def test_no_tmp_file_left_after_save(tmp_path):
    path = tmp_path / "s.json"
    store = SessionStore(path)
    store.save("sys", [])

    assert path.exists()
    assert not (tmp_path / "s.json.tmp").exists()


def test_load_missing_returns_none(tmp_path):
    store = SessionStore(tmp_path / "missing.json")
    assert store.load() is None


def test_load_corrupt_raises(tmp_path):
    path = tmp_path / "s.json"
    path.write_text("{ 这不是合法 JSON", encoding="utf-8")
    store = SessionStore(path)

    with pytest.raises(Exception):
        store.load()


def test_load_missing_optional_keys_returns_defaults(tmp_path):
    path = tmp_path / "s.json"
    path.write_text('{"version": 1}', encoding="utf-8")
    store = SessionStore(path)

    assert store.load() == ("", [])


def test_load_wrong_structure_raises(tmp_path):
    path = tmp_path / "s.json"
    path.write_text('{"system_prompt": 123, "messages": []}', encoding="utf-8")
    store = SessionStore(path)

    with pytest.raises(ValueError):
        store.load()