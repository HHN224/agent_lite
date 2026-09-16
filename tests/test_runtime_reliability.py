import copy
import io
import json
import os
import subprocess

import pytest

from ai import TextDelta, ToolCall
from agent_core import AgentLoop
from coding_agent import __main__ as cli
from coding_agent.tools import WriteTool
from test_commandcode_provider import make_provider, sse
from test_loop import EchoTool
from faux_provider import FauxProvider


def test_default_provider_is_commandcode():
    assert cli.parse_args([]).provider == "commandcode"


def test_default_loop_finishes_beyond_old_limit():
    provider = FauxProvider([[ToolCall(str(i), "echo", {"text": "ok"})] for i in range(125)]
                            + [[TextDelta("complete")]])
    loop = AgentLoop(provider, "test", [EchoTool()])
    list(loop.run([]))
    assert loop.outcome == {"reason": "completed", "rounds": 126}


def test_image_results_use_user_message_after_entire_tool_batch():
    image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}}
    calls = [{"id": name, "type": "function", "function": {"name": "read", "arguments": "{}"}}
             for name in ("first", "second")]
    messages = [{"role": "assistant", "tool_calls": calls},
                {"role": "tool", "tool_call_id": "first", "content": [image]},
                {"role": "tool", "tool_call_id": "second", "content": [{"type": "text", "text": "caption"}, image]},
                {"role": "user", "content": "continue"}]
    original = copy.deepcopy(messages)
    def handler(request):
        sent = json.loads(request.content)["messages"]
        assert [m["role"] for m in sent] == ["assistant", "tool", "tool", "user", "user"]
        assert all(isinstance(m["content"], str) for m in sent if m["role"] == "tool")
        assert "caption" in sent[2]["content"]
        assert [b for b in sent[3]["content"] if b["type"] == "image_url"] == [image, image]
        assert sent[-1]["content"] == "continue"
        return sse([{"content": "recovered"}])
    provider = make_provider(handler)
    try:
        assert list(provider.stream(messages, [], provider.DEFAULT_MODEL)) == [TextDelta("recovered")]
        assert messages == original
    finally:
        provider.client.close()


def test_write_creates_nested_directory(tmp_path):
    WriteTool(tmp_path).execute("showcase/tools/preview.py", "print('ok')")
    assert (tmp_path / "showcase/tools/preview.py").read_text() == "print('ok')"
    with pytest.raises(PermissionError):
        WriteTool(tmp_path).execute("../outside/file.py", "bad")


@pytest.mark.skipif(os.name != "nt", reason="Windows console output")
def test_cursor_query_split_across_output_writes_never_reaches_terminal():
    from coding_agent.tui import _ConsoleOutput
    for split in range(1, len("\x1b[6n")):
        output = io.StringIO()
        positions = []
        writer = _ConsoleOutput(output, lambda: positions.append(output.getvalue()))
        writer.write("before" + "\x1b[6n"[:split])
        writer.flush()
        writer.write("\x1b[6n"[split:] + "after")
        writer.flush()
        assert output.getvalue() == "beforeafter"
        assert positions == ["before"]


@pytest.mark.skipif(os.name != "nt", reason="Windows terminal replies")
@pytest.mark.parametrize("reply", ["\x1b[>0;10;1c", "\x1b[?1;2c", "\x1b[0n", "\x1b[>5u", "\x1b]10;rgb:ffff/ffff/ffff\x1b\\"])
def test_terminal_capability_replies_do_not_become_draft_text(reply, monkeypatch):
    from coding_agent import tui
    from textual import events
    from textual._xterm_parser import XTermParser
    import textual._parser as parser_module
    clock = [0.0]
    monkeypatch.setattr(parser_module, "get_time", lambda: clock[0])
    monkeypatch.setattr(tui.time, "monotonic", lambda: clock[0])
    parser = getattr(tui, "_WindowsTerminalParser", XTermParser)()
    output = []
    # Deliver the unambiguous sequence prefix, then stall between every byte.
    output.extend(parser.feed(reply[:2]))
    for char in reply[2:]:
        clock[0] += 1
        output.extend(parser.tick())
        output.extend(parser.feed(char))
    clock[0] += 1
    output.extend(parser.tick())
    output.extend(parser.feed("draft"))
    keys = [e for e in output if isinstance(e, events.Key)]
    assert "".join(e.character or "" for e in keys) == "draft"
    assert not any(e.key == "escape" for e in keys)


@pytest.mark.parametrize("backend", ["HostRunner", "WslRunner", "DockerRunner"])
def test_shell_tools_do_not_inherit_tui_console(backend, monkeypatch, tmp_path):
    from coding_agent import sandbox
    captured = {}
    def run(*args, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(args, 0, b"ok", b"")
    monkeypatch.setattr(sandbox.subprocess, "run", run)
    getattr(sandbox, backend)(tmp_path).run("echo ok")
    assert captured["stdin"] == subprocess.DEVNULL
    if os.name == "nt":
        assert captured["creationflags"] & subprocess.CREATE_NO_WINDOW


def test_long_task_compacts_between_tool_rounds_without_losing_history():
    from agent_core import Agent, Session, ContextManager, CompactionEngine
    provider = FauxProvider([[ToolCall(str(i), "echo", {"text": "work " * 100})] for i in range(5)]
                            + [[TextDelta("complete")]])
    session = Session("long-task")
    engine = CompactionEngine(summarizer=lambda *args: "earlier work preserved", retain_ratio=0.25)
    agent = Agent(AgentLoop(provider, "test", [EchoTool()]), session,
                  context_manager=ContextManager(threshold_ratio=0.8), context_window=500,
                  compaction_engine=engine)
    output = list(agent.prompt("finish all five steps"))
    assert any(e.type == "compaction_end" and e.data["success"] for e in output)
    assert "earlier work preserved" in str(provider.calls[-1]["messages"])
    assert agent.loop.outcome["reason"] == "completed"
    entries = list(session.entries.values())
    assert sum(e.role == "user" for e in entries) == 1
    assert sorted(e.tool_call_id for e in entries if e.role == "tool") == [str(i) for i in range(5)]


@pytest.mark.skipif(os.name != "nt", reason="Windows terminal input")
def test_terminal_parser_preserves_keys_literal_paste_and_escape(monkeypatch):
    from coding_agent import tui
    from textual import events
    clock = [0.0]
    monkeypatch.setattr(tui.time, "monotonic", lambda: clock[0])
    parser = tui._WindowsTerminalParser()
    output = []
    for part in ["中文", "\x1b[", "A", "\x1b[6~", "\x1b[200~", "literal [>5u \x1b[0n", "\x1b[201~", "\x1b"]:
        output.extend(parser.feed(part))
    clock[0] = 1
    output.extend(parser.tick())
    keys = [e.key for e in output if isinstance(e, events.Key)]
    assert keys[-3:] == ["up", "pagedown", "escape"]
    assert [e.text for e in output if isinstance(e, events.Paste)] == ["literal [>5u \x1b[0n"]
