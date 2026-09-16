"""Opt-in paid API validation: python tests/live_commandcode_validation.py --scenario scene.

Runs the real provider, WSL tools and TUI event consumer in an isolated workspace.
Not collected by pytest. Reports and generated files stay under ignored sessions/.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import time
import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv
from textual.widgets import Input
from coding_agent import __main__ as cli
from coding_agent.tui import AgentApp

SCENE_PROMPT = (
    "请制作一个雨夜便利店街角小场景，以第三视角观察整个场景，整体像一个可以自由拖拽、旋转、"
    "缩放观看的微缩三维模型 / 小型景观，没有任何UI界面元素。地面为一个完整的正方形底座，"
    "所有元素都搭建在这个底座上，构图紧凑、层次清晰，整体呈现出小巧、精致、可收藏的模型感。"
    "画面风格为三渲二，强调浓厚的日式二次元动画氛围，材质表现干净，轮廓明确，色彩柔和"
    "但富有夜景霓虹的层次感。"
)


async def validate(options):
    load_dotenv(ROOT / ".env")
    key = os.environ.get("CMD_API_KEY")
    if not key:
        raise RuntimeError("CMD_API_KEY is required; no provider fallback")
    base = Path(options.resume_dir).resolve() if options.resume_dir else ROOT / "sessions" / f"validation-{options.scenario}-{time.time_ns()}"
    workspace = base / "workspace"
    workspace.mkdir(parents=True, exist_ok=bool(options.resume_dir))
    cli.SESSIONS_DIR = base / "sessions"
    args = cli.parse_args(["--workspace", str(workspace),
                           "--sandbox", "wsl", "--bypass"] + ([] if options.resume_dir else ["--new"]))
    agent = cli.build_agent(args, key)
    if options.history_file:
        from agent_core import Session
        snapshot = Session.from_dict(json.loads(Path(options.history_file).read_text(encoding="utf-8")))
        snapshot.session_id = agent.session.session_id
        snapshot.usage = snapshot.new_usage = 0
        agent.session = snapshot
    if options.scenario == "recovery":
        create = agent.loop.provider.client.chat.completions.create
        injected = False
        class DisconnectOnce:
            def __init__(self, response): self.response = response
            def __iter__(self):
                next(iter(self.response))
                raise httpx.ReadError("live validation: injected stream disconnect")
                yield  # generator, like the SDK response
            def close(self): self.response.close()
        def create_with_disconnect(**kwargs):
            nonlocal injected
            response = create(**kwargs)
            if not injected:
                injected = True
                return DisconnectOnce(response)
            return response
        agent.loop.provider.client.chat.completions.create = create_with_disconnect
    report = {"scenario": options.scenario, "workspace": str(workspace), "events": {},
              "thinking_characters": 0, "max_argument_characters": 0, "max_ui_lag": 0.0}
    start = time.monotonic()
    suffix = "-resume" if options.resume_dir else ""
    trace = (base / f"events{suffix}.jsonl").open("w", encoding="utf-8")
    def log(kind, **data):
        line = json.dumps({"seconds": round(time.monotonic()-start, 2), "kind": kind, **data}, ensure_ascii=False)
        trace.write(line + "\n")
        trace.flush()
        if kind != "event" or data.get("event") not in ("message_update", "thinking_update", "tool_call_progress"):
            print(line, flush=True)
    app = AgentApp(agent, workspace=workspace)
    render = app._render_event
    def observe(event):
        report["events"][event.type] = report["events"].get(event.type, 0) + 1
        if event.type == "thinking_update":
            report["thinking_characters"] += len(event.data.get("content", ""))
        elif event.type == "tool_call_progress":
            report["max_argument_characters"] = max(report["max_argument_characters"], event.data["characters"])
        log("event", event=event.type, **{k: event.data[k] for k in ("name", "code", "reason", "is_error") if k in event.data})
        render(event)
    app._render_event = observe
    prompt = ('创建 sales.csv：item,quantity,price 表头，apple,3,2.5 和 pear,4,1.25 两行。'
              '创建 summarize.py 生成 result.json，total 必须为 12.5。实际运行验证，'
              '再用 edit 工具添加中文用途注释，重新执行确认。完成后回复结果。')
    if options.scenario in ("scene", "cancel", "interrupt"):
        prompt = SCENE_PROMPT
    if options.scenario == "web":
        prompt = ('做一个好看的待办事项网页，单个 index.html，不依赖网络资源。'
                  '支持输入任务、添加、勾选完成、删除，刷新后保留数据。'
                  '使用语义化输入框和按钮，实际生成文件并检查后交付。')
    if options.scenario == "image":
        # Make an image with stdlib only; the model must read it through the
        # actual tool and make another API request before finishing the task.
        import struct
        import zlib
        def chunk(kind, data):
            return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 32, 32, 8, 2, 0, 0, 0))
        png += chunk(b"IDAT", zlib.compress((b"\0" + b"\xff\0\0" * 32) * 32)) + chunk(b"IEND", b"")
        (workspace / "sample.png").write_bytes(png)
        prompt = ('用 read 工具查看 sample.png，识别图片的主要颜色。创建 nested/results/color.json，'
                  '字段 color 使用英文小写颜色名称。实际用 Python 读取 JSON 验证格式。完成后回复。')
    if options.resume_dir:
        prompt = '继续完成之前的雨夜便利店场景任务。已有分块代码请优先组装为可运行的 HTML 并验证，避免重新规划整个实现。完成后说明文件路径。'
    if options.prompt_file:
        prompt = Path(options.prompt_file).read_text(encoding="utf-8")
    try:
        async with app.run_test(size=(110, 32)) as pilot:
            await app._task
            app._start_prompt(prompt)
            steered = False
            cancelled = False
            next_log = 0
            while app._busy:
                before = time.monotonic()
                await asyncio.sleep(0.05)
                elapsed = time.monotonic() - start
                report["max_ui_lag"] = max(report["max_ui_lag"], time.monotonic()-before-0.05)
                assert elapsed < options.timeout, f"Task exceeded {options.timeout}s"
                if elapsed >= next_log:
                    log("heartbeat", phase=app._phase, thinking=report["thinking_characters"], arguments=report["max_argument_characters"])
                    next_log = elapsed + 10
                if options.scenario == "steer" and not steered and elapsed > 2:
                    # Enter goes through the real TUI submission/steering path.
                    composer = app.query_one("#input", Input)
                    composer.value = '额外要求：完成后创建 marker.txt，内容严格为 STEERING_OK。'
                    await pilot.press("enter")
                    steered = True
                if options.scenario == "cancel" and not cancelled and report["thinking_characters"] > 1000:
                    cancel_start = time.monotonic()
                    await pilot.press("escape")
                    cancelled = True
                if options.scenario == "interrupt" and not steered and report["thinking_characters"] > 2000:
                    interrupt_start = time.monotonic()
                    app.query_one("#input", Input).value = '停止原来的场景任务。改为创建 marker.txt，内容严格为 STEERING_OK。实际写入后回复完成。'
                    await pilot.press("enter")
                    steered = True
                if options.scenario == "interrupt" and report["events"].get("response_interrupted") and "interrupt_seconds" not in report:
                    report["interrupt_seconds"] = round(time.monotonic()-interrupt_start, 2)
                # Editing the draft must remain responsive while streaming.
                if elapsed > 3 and not report.get("draft_checked"):
                    composer = app.query_one("#input", Input)
                    await pilot.press("x", "backspace")
                    assert composer.value == "", composer.value
                    report["draft_checked"] = True
                scroll = app.query_one("#scroll")
                if elapsed > 3 and scroll.max_scroll_y > 10 and not report.get("scroll_checked"):
                    await pilot.press("pageup")
                    await pilot.pause()
                    position = scroll.scroll_y
                    assert position < scroll.max_scroll_y
                    updates = sum(report["events"].values())
                    await asyncio.sleep(0.3)
                    assert scroll.scroll_y == position, "Streaming snapped history back to the bottom"
                    if sum(report["events"].values()) > updates:
                        report["scroll_checked"] = True
                        await pilot.press("pagedown")
                        await pilot.pause()
                        assert scroll.scroll_y > position
            await app._task
            await app._flush_cards()
            report["outcome"] = dict(agent.session.runtime_status)
            report["seconds"] = round(time.monotonic()-start, 2)
            if options.scenario == "cancel":
                assert cancelled
                report["cancel_seconds"] = round(time.monotonic()-cancel_start, 2)
                assert report["outcome"]["reason"] == "aborted"
                assert report["cancel_seconds"] < 3
            else:
                assert report["outcome"]["reason"] == "completed", report["outcome"]
                if options.scenario in ("smoke", "steer", "recovery"):
                    assert json.loads((workspace / "result.json").read_text(encoding="utf-8"))["total"] == 12.5
                if options.scenario == "image":
                    assert json.loads((workspace / "nested/results/color.json").read_text(encoding="utf-8"))["color"] == "red"
                if options.scenario == "web":
                    assert (workspace / "index.html").stat().st_size > 500
                if options.scenario in ("steer", "interrupt"):
                    assert (workspace / "marker.txt").read_text(encoding="utf-8").strip() == "STEERING_OK"
                if options.scenario == "interrupt":
                    assert report["interrupt_seconds"] < 3
                if options.scenario in ("scene", "resume"):
                    assert any(p.stat().st_size > 3000 for p in workspace.rglob("*.html")), "No scene artifact"
                if options.scenario == "resume":
                    assert report.get("scroll_checked"), "No live scrolling validation completed"
                if options.scenario == "recovery":
                    assert report["events"].get("retry") == 1
                # Send a real follow-up on the same history and verify a new file.
                app._start_prompt('请创建 followup.txt，内容为 FOLLOWUP_OK。使用工具实际写入后回复完成。')
                await asyncio.wait_for(app._task, timeout=120)
                assert (workspace / "followup.txt").read_text(encoding="utf-8").strip() == "FOLLOWUP_OK"
                report["followup"] = agent.session.runtime_status["reason"]
                assert report["followup"] == "completed"
            assert app.query_one("#input", Input).value == ""
            assert report["max_ui_lag"] < 1, report["max_ui_lag"]
            report["passed"] = True
    finally:
        (base / f"report{suffix}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        log("report", path=str(base / f"report{suffix}.json"), passed=report.get("passed", False))
        trace.close()
        agent.loop.provider.client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", choices=["smoke", "steer", "scene", "cancel", "interrupt", "recovery", "resume", "image", "web"], default="smoke")
    parser.add_argument("--resume-dir")
    parser.add_argument("--history-file", help="Replay an archive copy in the isolated test session")
    parser.add_argument("--prompt-file")
    parser.add_argument("--timeout", type=int, default=1800)
    options = parser.parse_args()
    if options.scenario == "resume" and not options.resume_dir:
        parser.error("--scenario resume requires --resume-dir")
    asyncio.run(validate(options))
