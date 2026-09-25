"""Opt-in adapter for the local agent-lite checkout. Running this CALLS YOUR MODEL.

Parent supervises one worker; phases go into the SAME session automatically.
No grade or reference answers are passed to the model.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import eval as controller
from tasks import dump


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run", required=True)
    p.add_argument("--repo-root", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--provider", choices=["commandcode", "deepseek"], default="commandcode")
    p.add_argument("--sandbox", choices=["wsl", "docker", "host"], required=True)
    p.add_argument("--variant", required=True)
    p.add_argument("--repeat", type=int, default=1)
    p.add_argument("--budget-seconds", type=int, default=3600)
    p.add_argument("--context-window", type=int, default=128000)
    p.add_argument("--compact-threshold", type=float, default=0.8)
    p.add_argument("--compact-retain", type=float, default=0.16)
    p.add_argument("--min-peak-ratio", type=float)
    p.add_argument("--min-compactions", type=int, default=0)
    p.add_argument("--protocol", choices=["normal", "compaction"], default="normal")
    p.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    return p.parse_args(argv)


def worker(args):
    repo = Path(args.repo_root).resolve()
    if not (repo / "coding_agent/__main__.py").is_file():
        raise ValueError("--repo-root must be an agent-lite source checkout")
    sys.path.insert(0, str(repo))
    from dotenv import load_dotenv
    import coding_agent.__main__ as app
    run, meta, spec = controller.load_run(args.run)
    load_dotenv(repo / ".env")
    key = os.environ.get("CMD_API_KEY" if args.provider == "commandcode" else "DEEPSEEK_API_KEY")
    if not key:
        raise ValueError("model API key not configured")
    phase = 1
    providers = []
    with (run / "events.jsonl").open("a", encoding="utf-8") as log:
        def emit(kind, **data):
            event = {"run_id": meta["run_id"], "phase": phase, "at": controller.now(), "type": kind, **data}
            log.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
            log.flush()

        def observed(base):
            class Observed(base):
                def __init__(self, *a, **kw):
                    super().__init__(*a, **kw)
                    providers.append(self)

                def stream(self, messages, tools, model, **options):
                    self.last_usage = None
                    request_id = uuid.uuid4().hex
                    purpose = "main" if tools else "compaction"
                    started = time.monotonic()
                    try:
                        yield from super().stream(messages, tools, model, **options)
                    finally:
                        usage = self.last_usage or {}
                        emit("model_usage", purpose=purpose, request_id=request_id, model=model,
                             context_window=args.context_window, input_tokens=usage.get("prompt_tokens"),
                             output_tokens=usage.get("completion_tokens"), raw_usage=usage,
                             elapsed_seconds=round(time.monotonic() - started, 3))
            return Observed

        # Instrument both provider instances created by build_agent, including the summarizer.
        app.CommandCodeProvider = observed(app.CommandCodeProvider)
        app.OpenAIProvider = observed(app.OpenAIProvider)
        app.SESSIONS_DIR = run / "private/agent_sessions"
        cli = app.parse_args(["--ui", "cli", "--new", "--workspace", str(run / "workspace"),
                              "--provider", args.provider, "--model", args.model, "--sandbox", args.sandbox,
                              "--permission-policy", "auto", "--context-window", str(args.context_window),
                              "--compact-threshold", str(args.compact_threshold), "--compact-retain", str(args.compact_retain)])
        # AGENT_SESSION environment variable must not cause a previous experiment to be restored.
        cli.session = ""
        try:
            agent = app.build_agent(cli, key)
            dump(run / "adapter.json", {"session_id": agent.session_id, "repo_root": str(repo),
                 "context_window": args.context_window, "compact_threshold": args.compact_threshold,
                 "compact_retain": args.compact_retain, "sandbox": args.sandbox, "provider": args.provider,
                 "model": args.model, "instrumentation": "provider stream usage + AgentEvent"})
            total = len(spec.get("phases", [])) or 1
            for phase in range(1, total + 1):
                prompt = (run / "next_prompt.txt").read_text(encoding="utf-8")
                emit("phase_start", prompt_chars=len(prompt))
                last = None
                for event in agent.prompt(prompt):
                    last = event
                    if event.type in {"context_check", "compaction_end", "compaction_start", "retry", "error", "agent_end"}:
                        data = {k: v for k, v in event.data.items() if k not in {"text", "content", "summary"}}
                        emit(event.type, **data)
                    elif event.type == "tool_execution_end":
                        emit("tool_end", name=event.data.get("name"), is_error=event.data.get("is_error"),
                             truncated=event.data.get("pruned", False))
                    elif event.type == "tool_execution_start":
                        emit("tool_start", name=event.data.get("name"))
                if last is None or last.type != "agent_end" or last.data.get("reason") != "completed":
                    raise RuntimeError("agent did not complete the phase")
                if spec.get("phases"):
                    controller.checkpoint(run)
                emit("phase_frozen")
                print(f"phase {phase}/{total} finished", flush=True)
        finally:
            for provider in providers:
                provider.client.close()


def telemetry_from_events(run, run_id):
    from pressure import read_events
    events, error = read_events(run / "events.jsonl", run_id)
    usage = [e for e in events if e["type"] == "model_usage"]
    def total(field):
        values = [e.get(field) for e in usage]
        return sum(values) if values and all(type(v) is int and v >= 0 for v in values) else None
    return {"human_interventions": 0, "input_tokens": total("input_tokens"), "output_tokens": total("output_tokens"),
            "cost_usd": None, "tool_calls": sum(e["type"] == "tool_start" for e in events),
            "compactions": sum(e["type"] == "compaction_end" and e.get("success") is True for e in events),
            "compaction_failures": sum(e["type"] == "compaction_end" and e.get("success") is False for e in events),
            "retry_count": sum(e["type"] == "retry" for e in events),
            "evidence": {"raw_log_path": "events.jsonl", "usage_source": "provider per-call usage; includes summarizer", "parse_error": error}}


def main():
    args = parse_args()
    if args.worker:
        worker(args)
        return
    run, meta, _ = controller.load_run(args.run)
    repo = Path(args.repo_root).resolve()
    if not (repo / "coding_agent/__main__.py").is_file():
        raise ValueError("invalid agent-lite source path")
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True)
    revision = commit.stdout.strip() if commit.returncode == 0 else "unknown"
    controller.start(run, f"agent-lite@{revision}", args.model, args.variant, args.repeat,
                     args.budget_seconds, args.protocol, args.min_peak_ratio, args.min_compactions)
    command = [sys.executable, str(Path(__file__).resolve()), *sys.argv[1:], "--worker"]
    status = "agent_error"
    try:
        with (run / "runner.stdout.txt").open("wb") as out, (run / "runner.stderr.txt").open("wb") as err:
            result = subprocess.run(command, stdout=out, stderr=err, timeout=args.budget_seconds,
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        status = "completed" if result.returncode == 0 else "agent_error"
    except subprocess.TimeoutExpired:
        status = "timeout"
    except KeyboardInterrupt:
        status = "aborted"
    finally:
        telemetry = telemetry_from_events(run, meta["run_id"])
        # An interrupted request may have incurred charges without a final usage event.
        if status != "completed":
            telemetry["input_tokens"] = telemetry["output_tokens"] = None
        dump(run / "telemetry.json", telemetry)
        controller.finish(run, status, run / "telemetry.json")
    print(f"status={status}; events={run / 'events.jsonl'}; run grade separately after inspecting isolation")
    if status != "completed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
