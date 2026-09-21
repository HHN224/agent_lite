"""Portable offline evaluation controller; Python standard library only.

Run `python eval.py --help`. No model/network calls are made by this controller.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import datetime as dt
import json
import math
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

from tasks import CATALOG, SCALES, VERSION, digest, dump, generate, write


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def same(actual, expected):
    """JSON equality with strict bool/int/null types (True must not pass as 1)."""
    if type(actual) is not type(expected):
        return False
    if isinstance(expected, dict):
        return actual.keys() == expected.keys() and all(same(actual[k], v) for k, v in expected.items())
    if isinstance(expected, list):
        return len(actual) == len(expected) and all(same(a, b) for a, b in zip(actual, expected))
    return actual == expected


def within(root, rel):
    path = (root / rel).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError(f"path escapes root: {rel}")
    return path


def load_run(run):
    run = Path(run).resolve()
    return run, read(run / "run.json"), read(run / "private/spec.json")


def prepare(task, seed, scale, out):
    out = Path(out).resolve()
    if out.exists():
        raise ValueError(f"output already exists; use a fresh directory: {out}")
    out.mkdir(parents=True)
    workspace = out / "workspace"
    workspace.mkdir()
    spec = generate(workspace, task, seed, scale)
    dump(out / "private/spec.json", spec)
    identity = {"benchmark_version": VERSION, "task": task, "seed": seed, "scale": scale}
    identity["fixture_sha256"] = digest(out / "private/spec.json")
    meta = {**identity, "run_id": uuid.uuid4().hex, "created_at": now(), "status": "prepared",
            "current_phase": 1, "agent": None, "model": None, "variant": None, "repeat": None,
            "budget_seconds": None, "started_at": None, "finished_at": None,
            "elapsed_seconds": None, "telemetry": {}, "restarts": []}
    dump(out / "run.json", meta)
    return out


def start(run, agent, model, variant, repeat, budget_seconds, protocol):
    run, meta, _ = load_run(run)
    if meta["status"] != "prepared":
        raise ValueError("start is allowed only once, on a prepared run")
    if budget_seconds <= 0 or repeat < 1:
        raise ValueError("positive budget and repeat >= 1 required")
    meta.update(agent=agent, model=model, variant=variant, repeat=repeat, budget_seconds=budget_seconds,
                protocol=protocol, started_at=now(), status="running")
    dump(run / "run.json", meta)


def checkpoint(run):
    """Freeze current submission before exposing the next user message; no correctness feedback."""
    run, meta, spec = load_run(run)
    if meta["status"] != "running":
        raise ValueError("run must be running")
    phases = spec.get("phases")
    if not phases:
        raise ValueError("this task has no stages")
    index = meta["current_phase"] - 1
    if index >= len(phases):
        raise ValueError("all stages already frozen")
    rel = phases[index]["output"]
    path = within(run / "workspace", rel)
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"missing regular checkpoint file: {rel}")
    snapshot = run / "private/snapshots" / Path(rel).name
    if snapshot.exists():
        raise ValueError("checkpoint already frozen")
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(path, snapshot)
    # Do not validate correctness here: stage advancement cannot leak a hidden score.
    spec["immutable"][rel] = digest(snapshot)
    meta.setdefault("checkpoints", []).append({"phase": index + 1, "frozen_at": now(), "sha256": digest(snapshot)})
    meta["current_phase"] += 1
    if index + 1 < len(phases):
        nxt = phases[index + 1]
        for name, data in nxt["files"].items():
            target = within(run / "workspace", name)
            if target.exists():
                raise ValueError(f"next stage input already exists: {name}")
            dump(target, data)
            spec["immutable"][name] = digest(target)
        write(run / "next_prompt.txt", nxt["prompt"] + "\n")
        result = nxt["prompt"]
    else:
        write(run / "next_prompt.txt", "全部阶段已冻结，结束测评。\n")
        result = "All stages frozen. Run finish, then grade."
    dump(run / "private/spec.json", spec)
    dump(run / "run.json", meta)
    return result


def finish(run, status, telemetry=None):
    run, meta, _ = load_run(run)
    if meta["status"] != "running":
        raise ValueError("finish requires a running run")
    elapsed = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(meta["started_at"])).total_seconds()
    if status == "completed" and elapsed > meta["budget_seconds"]:
        status = "timeout"
    meta.update(status=status, finished_at=now(), elapsed_seconds=round(elapsed, 3))
    if telemetry is not None:
        values = read(telemetry)
        if not isinstance(values, dict):
            raise ValueError("telemetry must be an object")
        for key in ["input_tokens", "output_tokens", "cost_usd", "tool_calls", "compactions", "human_interventions"]:
            value = values.get(key)
            if value is not None and (type(value) not in (int, float) or value < 0 or not math.isfinite(value)):
                raise ValueError(f"invalid telemetry number: {key}")
        meta["telemetry"] = values
    dump(run / "run.json", meta)


def integrity_checks(run, spec):
    w = run / "workspace"
    checks = []
    for rel, sha in sorted(spec["immutable"].items()):
        try:
            path = within(w, rel)
            ok = path.is_file() and not path.is_symlink() and digest(path) == sha
        except (OSError, ValueError):
            ok = False
        checks.append({"name": f"immutable:{rel}", "passed": ok, "kind": "integrity"})
    originals = set(spec["original_paths"])
    for path in w.rglob("*"):
        if not path.is_file() and not path.is_symlink():
            continue
        rel = path.relative_to(w).as_posix()
        # Runtime caches are recorded/exempted, not considered solution files.
        allowed = (rel in originals or rel in spec["immutable"] or
                   rel.startswith(("scratch/", "output/", ".agent-lite/")) or
                   any(rel.startswith(p) for p in spec.get("mutable_prefixes", [])) or
                   "__pycache__" in path.parts)
        checks.append({"name": f"allowed_path:{rel}", "passed": allowed and not path.is_symlink(), "kind": "integrity"})
    return checks


def code_checks(run, meta, spec, execute):
    """Run submitted code in disposable copies. This is NOT an OS security sandbox."""
    if not execute:
        raise ValueError("coding grade executes candidate Python: use --execute-submission in an isolated evaluator environment")
    results = []
    for case in spec["code_cases"]:
        with tempfile.TemporaryDirectory(prefix="agent-eval-") as temp:
            root = Path(temp)
            src = run / "workspace/src"
            if (not src.is_dir() or src.is_symlink() or not src.resolve().is_relative_to((run / "workspace").resolve())
                    or any(p.is_symlink() for p in src.rglob("*"))):
                results.append({"name": case["name"], "passed": False, "kind": "behavior", "error": "missing src or symlink"})
                continue
            shutil.copytree(src, root / "src")
            batches = case.get("batches", [case])
            for index, batch in enumerate(batches):
                # -I excludes script directory; insert only the submitted src for legitimate helper imports.
                cmd = [sys.executable, "-I", "-c",
                       "import sys,runpy; p=sys.argv.pop(1); sys.path.insert(0,p); runpy.run_path(p+'/main.py',run_name='__main__')",
                       str(root / "src")]
                if meta["task"] == "C02":
                    cmd.append(str(root / "queue.db"))
                error = None
                try:
                    # No environment secrets needed by an offline submitted program.
                    env = {k: v for k, v in os.environ.items() if k.upper() in
                           {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "LANG", "LC_ALL"}}
                    env["PYTHONIOENCODING"] = "utf-8"
                    # File-backed output prevents unbounded capture_output memory use.
                    with (root / "stdout.txt").open("wb") as stdout, (root / "stderr.txt").open("wb") as stderr:
                        proc = subprocess.run(cmd, input=json.dumps(batch["input"], ensure_ascii=True).encode("ascii"),
                                              stdout=stdout, stderr=stderr, cwd=root, env=env, timeout=5)
                    if (root / "stdout.txt").stat().st_size > 2_000_000:
                        raise ValueError("stdout exceeds 2 MB")
                    answer = read(root / "stdout.txt")
                    ok = proc.returncode == 0 and same(answer, batch["expected"])
                    if meta["task"] == "C02":
                        database = root / "queue.db"
                        if database.is_symlink() or not database.is_file():
                            raise ValueError("missing regular SQLite database")
                        # No schema assumptions: any valid SQLite implementation is accepted.
                        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
                            ok = ok and db.execute("PRAGMA quick_check").fetchall() == [("ok",)]
                    if not ok:
                        error = f"wrong output or exit code {proc.returncode}"
                except (ValueError, OSError, subprocess.TimeoutExpired, UnicodeError, sqlite3.Error) as exc:
                    ok = False
                    error = type(exc).__name__ + ": " + str(exc)[:180]
                results.append({"name": f"{case['name']}/process-{index + 1}", "passed": ok, "kind": "behavior", "error": error})
    return results


def output_checks(run, spec):
    checks = []
    phase_paths = {p["output"] for p in spec.get("phases", [])}
    for rel, expected in sorted(spec.get("outputs", {}).items()):
        try:
            path = within(run / "workspace", rel)
            if rel in phase_paths:
                path = run / "private/snapshots" / Path(rel).name
            actual = read(path)
            ok = same(actual, expected)
        except (OSError, ValueError, UnicodeError):
            actual, ok = None, False
        checks.append({"name": f"artifact:{rel}", "passed": ok, "kind": "artifact"})
        # Diagnostic field-level checks do not inflate the primary all-or-nothing pass.
        if isinstance(expected, dict):
            for key, value in expected.items():
                checks.append({"name": f"field:{rel}:{key}", "passed": isinstance(actual, dict) and key in actual and same(actual[key], value), "kind": "field"})
        elif isinstance(expected, list):
            for i, value in enumerate(expected):
                checks.append({"name": f"record:{rel}:{i}", "passed": isinstance(actual, list) and len(actual) > i and same(actual[i], value), "kind": "field"})
    return checks


def grade(run, execute=False):
    run, meta, spec = load_run(run)
    if meta["status"] in {"prepared", "running"}:
        raise ValueError("freeze artifacts with finish before grading; do not reveal hidden feedback mid-run")
    result_path = run / "results/grade.json"
    if result_path.exists():
        raise ValueError("already graded; a new attempt requires a fresh run")
    checks = integrity_checks(run, spec) + output_checks(run, spec)
    if "code_cases" in spec:
        checks += code_checks(run, meta, spec, execute)
    core = [c for c in checks if c["kind"] in {"artifact", "behavior"}]
    integrity = all(c["passed"] for c in checks if c["kind"] == "integrity")
    complete_phases = not spec.get("phases") or meta["current_phase"] > len(spec["phases"])
    artifact_pass = bool(core) and all(c["passed"] for c in core) and integrity and complete_phases
    no_help = meta.get("telemetry", {}).get("human_interventions") == 0
    protocol = meta.get("protocol", "normal")
    protocol_ok = (protocol == "normal" or
                   (protocol == "resume" and bool(meta.get("restarts"))) or
                   (protocol == "compaction" and meta.get("telemetry", {}).get("compactions", 0) is not None
                    and meta.get("telemetry", {}).get("compactions", 0) > 0))
    measured_completion = (meta["status"] == "completed" and meta["elapsed_seconds"] is not None
                           and meta["elapsed_seconds"] <= meta["budget_seconds"])
    result = {"run_id": meta["run_id"], "benchmark_version": VERSION, "graded_at": now(),
              "artifact_pass": artifact_pass, "autonomous_success": artifact_pass and measured_completion and no_help and protocol_ok,
              "protocol_evidence_present": protocol_ok,
              "integrity_pass": integrity, "all_stages_frozen": complete_phases,
              "partial_score": round(sum(c["passed"] for c in core) / len(core), 6) if core else 0,
              "checks": checks, "failure_reason": []}
    if not no_help:
        result["failure_reason"].append("human interventions unknown or nonzero: no autonomous success claim")
    if not measured_completion:
        result["failure_reason"].append("not completed within recorded budget")
    if not integrity:
        result["failure_reason"].append("protected files changed or disallowed paths")
    if not complete_phases:
        result["failure_reason"].append("unfrozen stages")
    if not protocol_ok:
        result["failure_reason"].append("no recorded restart or successful compaction for the selected protocol")
    dump(result_path, result)
    return result


def wilson(wins, n):
    if n == 0:
        return None
    z = 1.95996398454
    p = wins / n
    center = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return [max(0, center - half), min(1, center + half)]


def summarize(root):
    groups = {}
    seen = set()
    for path in sorted(Path(root).resolve().rglob("run.json")):
        # Only our run roots (not a random run.json produced inside a submission).
        if not (path.parent / "private/spec.json").is_file() or "workspace" in path.relative_to(Path(root).resolve()).parts:
            continue
        m = read(path)
        if m["run_id"] in seen:
            raise ValueError(f"duplicate run_id (copied result would double count): {m['run_id']}")
        seen.add(m["run_id"])
        key = tuple(m.get(k) for k in ["benchmark_version", "agent", "model", "variant", "scale", "budget_seconds", "protocol"])
        result = read(path.parent / "results/grade.json") if (path.parent / "results/grade.json").exists() else None
        groups.setdefault(key, []).append((m, result))
    output = []
    for key, rows in groups.items():
        labels = ["benchmark_version", "agent", "model", "variant", "scale", "budget_seconds", "protocol"]
        item = dict(zip(labels, key))
        n = len(rows)
        wins = sum(bool(r and r["autonomous_success"]) for _, r in rows)
        tasks = {}
        pairs = set()
        for m, r in rows:
            pair = (m["task"], m["seed"], m["repeat"])
            if pair in pairs:
                raise ValueError(f"duplicate task/seed/repeat in group: {pair}")
            pairs.add(pair)
            tasks.setdefault(m["task"], []).append(bool(r and r["autonomous_success"]))
        elapsed = [m["elapsed_seconds"] for m, _ in rows if m["elapsed_seconds"] is not None]
        costs = [m.get("telemetry", {}).get("cost_usd") for m, _ in rows]
        token_inputs = [m.get("telemetry", {}).get("input_tokens") for m, _ in rows]
        token_outputs = [m.get("telemetry", {}).get("output_tokens") for m, _ in rows]
        item.update(planned_runs=n, graded_runs=sum(r is not None for _, r in rows), successes=wins,
                    success_rate=wins / n, macro_task_rate=sum(sum(v) / len(v) for v in tasks.values()) / len(tasks),
                    by_task={k: {"n": len(v), "wins": sum(v), "rate": sum(v) / len(v)} for k, v in sorted(tasks.items())},
                    descriptive_wilson95=wilson(wins, n),
                    incomplete=sum(m["status"] in {"prepared", "running"} or r is None for m, r in rows),
                    total_elapsed_seconds=sum(elapsed) if len(elapsed) == n else None,
                    cost_usd=sum(costs) if all(x is not None for x in costs) else None,
                    input_tokens=sum(token_inputs) if all(x is not None for x in token_inputs) else None,
                    output_tokens=sum(token_outputs) if all(x is not None for x in token_outputs) else None)
        item["cost_per_success_usd"] = item["cost_usd"] / wins if item["cost_usd"] is not None and wins else None
        item["warning"] = "Wilson assumes independent trials; repeated seeds/families are correlated. Use paired, clustered analysis for claims. Missing/ungraded runs count as non-success; incomplete groups are preliminary."
        output.append(item)
    return {"generated_at": now(), "groups": output}


def main():
    # Consistent Chinese output when redirected by Windows terminals and automation.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    p = sub.add_parser("prepare")
    p.add_argument("--task", required=True, choices=CATALOG)
    p.add_argument("--seed", type=int, default=17)
    p.add_argument("--scale", choices=SCALES, default="standard")
    p.add_argument("--out", required=True)
    p = sub.add_parser("start")
    p.add_argument("--run", required=True)
    for name in ["agent", "model", "variant"]:
        p.add_argument("--" + name, required=True)
    p.add_argument("--repeat", type=int, default=1)
    p.add_argument("--budget-seconds", type=int, required=True)
    p.add_argument("--protocol", choices=["normal", "resume", "compaction"], default="normal")
    for command in ["checkpoint", "restart", "finish", "grade"]:
        p = sub.add_parser(command)
        p.add_argument("--run", required=True)
        if command == "restart":
            p.add_argument("--note", required=True, help="session ID/log path and planned interruption point")
        if command == "finish":
            p.add_argument("--status", choices=["completed", "timeout", "agent_error", "infra_error", "aborted"], required=True)
            p.add_argument("--telemetry", help="controller-provided JSON, never agent self-report")
        if command == "grade":
            p.add_argument("--execute-submission", action="store_true")
    p = sub.add_parser("summarize")
    p.add_argument("--root", required=True)
    p.add_argument("--out", required=True)
    args = parser.parse_args()
    try:
        if args.cmd == "list":
            for task, (title, dimension) in CATALOG.items():
                print(f"{task}  {dimension:14} {title}")
        elif args.cmd == "prepare":
            out = prepare(args.task, args.seed, args.scale, args.out)
            print(f"Prepared {out}\nCandidate receives ONLY: {out / 'workspace'}")
        elif args.cmd == "start":
            start(args.run, args.agent, args.model, args.variant, args.repeat, args.budget_seconds, args.protocol)
            print("Started. Wall clock is recorded; the operator must enforce the budget.")
        elif args.cmd == "checkpoint":
            print(checkpoint(args.run))
        elif args.cmd == "restart":
            run, meta, _ = load_run(args.run)
            if meta["status"] != "running":
                raise ValueError("restart record requires a running run")
            meta["restarts"].append({"at": now(), "phase": meta["current_phase"], "note": args.note})
            dump(run / "run.json", meta)
            print("Restart evidence recorded; this command does not control the agent process.")
        elif args.cmd == "finish":
            finish(args.run, args.status, args.telemetry)
            print("Finished. Stop the candidate and revoke workspace writes before grading.")
        elif args.cmd == "grade":
            result = grade(args.run, args.execute_submission)
            print(json.dumps({k: v for k, v in result.items() if k != "checks"}, ensure_ascii=False, indent=2))
        elif args.cmd == "summarize":
            result = summarize(args.root)
            dump(Path(args.out), result)
            print(f"Wrote {len(result['groups'])} groups to {args.out}")
    except (ValueError, OSError, KeyError) as exc:
        parser.exit(2, f"Error: {exc}\n")


if __name__ == "__main__":
    main()
