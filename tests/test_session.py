import json

import pytest

from agent_core import (
    Agent,
    AgentLoop,
    Session,
    SessionEntry,
    SessionRepository,
)
from ai import TextDelta

from faux_provider import FauxProvider


# --------------------------------------------------------------------------- #
# Session：追加与 payload 重建
# --------------------------------------------------------------------------- #
def test_append_message_chains_parent_and_head():
    s = Session(session_id="abc", system_prompt="sys")
    u = s.append_message("user", "你好")
    a = s.append_message("assistant", "世界")

    assert u.parent_id is None
    assert a.parent_id == u.id
    assert s.head_id == a.id
    assert s.message_count == 2


def test_append_does_not_mutate_old_entries():
    # 追加式、不可变：旧条目不因后续追加而改变
    s = Session(session_id="abc")
    a = s.append_message("user", "你好")
    b = s.append_message("assistant", "世界")
    c = s.append_message("user", "继续")

    assert s.entries[a.id].content == "你好"
    assert s.entries[b.id].content == "世界"
    assert b.parent_id == a.id
    assert c.parent_id == b.id


def test_clear_history_keeps_system_prompt():
    s = Session(session_id="abc", system_prompt="sys")
    s.append_message("user", "hi")
    s.append_message("assistant", "hello")
    s.clear_history()

    assert s.message_count == 0
    assert s.head_id is None
    assert s.build_llm_payload() == [{"role": "system", "content": "sys"}]


def test_build_payload_system_plus_messages():
    s = Session(session_id="abc", system_prompt="sys")
    s.append_message("user", "hi")
    s.append_message("assistant", "hello")

    payload = s.build_llm_payload()
    assert payload == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]


def test_build_payload_tool_message_includes_tool_calls():
    m = SessionEntry(
        id="e1", parent_id=None, type="message", role="assistant",
        content=None,
        tool_calls=[{"id": "t1", "type": "function", "function": {"name": "read", "arguments": "{}"}}],
    )
    payload = m.to_llm()
    assert payload["role"] == "assistant"
    assert payload["tool_calls"][0]["function"]["name"] == "read"


def test_build_payload_with_compaction_uses_kept_start():
    # 先有三条消息，中间一次压缩，cut point(first_kept) 指向第二条消息
    s = Session(session_id="abc", system_prompt="sys")
    e1 = s.append_message("user", "第一轮起点")
    e2 = s.append_message("assistant", "第二段")
    e3 = s.append_message("user", "第三段")
    s.append_compaction("这是总结", first_kept_entry_id=e2.id)

    payload = s.build_llm_payload()
    assert payload[0] == {"role": "system", "content": "sys"}
    assert payload[1] == {"role": "system", "content": "这是总结"}
    # 只保留 first_kept(e2) 之后的 message，e1 被打包进 summary 不再出现
    roles = [p["role"] for p in payload[2:]]
    assert roles == ["assistant", "user"]
    assert payload[2]["content"] == "第二段"
    assert payload[3]["content"] == "第三段"


def test_build_payload_no_system_prompt():
    s = Session(session_id="abc")
    s.append_message("user", "hi")
    payload = s.build_llm_payload()
    assert payload == [{"role": "user", "content": "hi"}]


# --------------------------------------------------------------------------- #
# SessionRepository：save / load / list / delete / rename
# --------------------------------------------------------------------------- #
def test_repo_save_load_round_trip(tmp_path):
    repo = SessionRepository(tmp_path)
    s = repo.create(name="我的会话", system_prompt="sys")
    s.append_message("user", "你好")
    repo.save(s)

    loaded = repo.load(s.session_id)
    assert loaded is not None
    assert loaded.session_id == s.session_id
    assert loaded.name == "我的会话"
    assert loaded.system_prompt == "sys"
    assert loaded.message_count == 1


def test_repo_load_missing_returns_none(tmp_path):
    repo = SessionRepository(tmp_path)
    assert repo.load("nope") is None


def test_repo_list_sorted_by_updated(tmp_path):
    repo = SessionRepository(tmp_path)
    a = repo.create(name="A")
    repo.save(a)
    b = repo.create(name="B")
    repo.save(b)

    metas = repo.list()
    assert [m.session_id for m in metas] == [b.session_id, a.session_id]
    assert metas[0].name == "B"
    assert metas[0].message_count == 0


def test_repo_delete(tmp_path):
    repo = SessionRepository(tmp_path)
    s = repo.create(name="x")
    repo.save(s)

    assert repo.delete(s.session_id) is True
    assert repo.load(s.session_id) is None
    assert repo.delete(s.session_id) is False  # 再删返回 False


def test_repo_rename(tmp_path):
    repo = SessionRepository(tmp_path)
    s = repo.create(name="old")
    repo.save(s)

    updated = repo.rename(s.session_id, "新名字")
    assert updated.name == "新名字"
    assert repo.load(s.session_id).name == "新名字"


def test_repo_rename_missing_raises(tmp_path):
    repo = SessionRepository(tmp_path)
    with pytest.raises(FileNotFoundError):
        repo.rename("nope", "x")


def test_repo_list_skips_tmp_and_corrupt(tmp_path):
    repo = SessionRepository(tmp_path)
    s = repo.create(name="ok")
    repo.save(s)
    (tmp_path / "broken.json").write_text("{ not json", encoding="utf-8")

    metas = repo.list()
    assert all(m.session_id != "broken" for m in metas)
    assert any(m.session_id == s.session_id for m in metas)


def test_repo_load_wrong_version_raises(tmp_path):
    repo = SessionRepository(tmp_path)
    path = tmp_path / "legacy.json"
    path.write_text(json.dumps({"version": 1, "messages": []}), encoding="utf-8")
    with pytest.raises(ValueError):
        repo.load("legacy")


# --------------------------------------------------------------------------- #
# Session 序列化往返
# --------------------------------------------------------------------------- #
def test_session_roundtrip_with_compaction(tmp_path):
    repo = SessionRepository(tmp_path)
    s = repo.create(name="x", system_prompt="sys")
    e1 = s.append_message("user", "a")
    s.append_compaction("sum", first_kept_entry_id=e1.id)
    repo.save(s)

    loaded = repo.load(s.session_id)
    assert loaded.token_count == s.token_count
    assert loaded.head_id == s.head_id
    assert loaded.build_llm_payload() == s.build_llm_payload()


def test_session_id_is_dir_safe():
    assert SessionRepository.new_session_id().isalnum()


# --------------------------------------------------------------------------- #
# Agent：用 Session + SessionRepository 驱动一轮，并正确记录本轮消息
# --------------------------------------------------------------------------- #
def make_agent(script, repo=None):
    provider = FauxProvider(script)
    loop = AgentLoop(provider=provider, model="faux-model", tools=[])
    session = Session(
        session_id=(repo.new_session_id() if repo else "abc"),
        name="t",
        system_prompt="sys",
    )
    agent = Agent(loop=loop, session=session, repo=repo)
    return agent, provider


def test_agent_prompt_records_turn_into_session():
    agent, _ = make_agent([[TextDelta("你好")]])
    events = list(agent.prompt("请打招呼"))

    assert events[0].type == "agent_start"
    assert events[-1].type == "agent_end"

    s = agent.session
    # 本轮应记录 user + assistant 两条消息
    assert s.message_count == 2
    assert s.head_id is not None
    assert s.build_llm_payload() == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "请打招呼"},
        {"role": "assistant", "content": "你好"},
    ]


def test_agent_prompt_saves_to_repo(tmp_path):
    repo = SessionRepository(tmp_path)
    agent, _ = make_agent([[TextDelta("ok")]], repo=repo)
    list(agent.prompt("喂"))

    loaded = repo.load(agent.session_id)
    assert loaded is not None
    assert loaded.message_count == 2


def test_agent_clear_history_syncs(tmp_path):
    repo = SessionRepository(tmp_path)
    agent, _ = make_agent([[TextDelta("ok")]], repo=repo)
    list(agent.prompt("喂"))
    agent.clear_history()

    assert agent.session.message_count == 0
    assert repo.load(agent.session_id).message_count == 0
