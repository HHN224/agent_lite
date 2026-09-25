"""Pressure evidence, kept separate from functional task success.

Only model_usage/main events provide measured peak input. Session anchors do not.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def read_events(path, run_id):
    if not path.is_file():
        return [], "missing_events"
    events = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict) or row.get("run_id") != run_id:
                return [], "event_run_id_mismatch"
            if type(row.get("phase")) is not int or row["phase"] < 1:
                return [], "invalid_event_phase"
            events.append(row)
    except (ValueError, OSError):
        return [], "invalid_events"
    return events, None


def assess_pressure(run, meta, checks):
    events, error = read_events(Path(run) / "events.jsonl", meta["run_id"])
    observed, estimated, successful, incomplete_usage = [], [], [], 0
    for index, event in enumerate(events):
        if event.get("type") == "model_usage" and event.get("purpose") == "main":
            tokens, window = event.get("input_tokens"), event.get("context_window")
            if type(tokens) is int and tokens >= 0 and type(window) is int and window > 0:
                observed.append({"index": index, "phase": event["phase"], "input_tokens": tokens,
                                 "context_window": window, "ratio": tokens / window})
            else:
                incomplete_usage += 1
        elif event.get("type") == "context_check":
            if type(event.get("ratio")) in (int, float):
                estimated.append(event["ratio"])
        elif event.get("type") == "compaction_end" and event.get("success") is True:
            successful.append({"index": index, "phase": event["phase"], "fallback": event.get("fallback")})
    peak = max(observed, key=lambda e: e["ratio"]) if observed else None
    req = meta.get("pressure_requirements", {})
    target, min_compactions = req.get("min_peak_ratio"), req.get("min_compactions", 0)
    requested = target is not None or min_compactions > 0
    qualified = None
    reason = "not_requested"
    if requested:
        if error:
            reason = error
        elif target is not None and not observed:
            reason = "no_provider_input_usage"
        else:
            qualified = (target is None or peak["ratio"] >= target) and len(successful) >= min_compactions
            reason = "met" if qualified else "insufficient_observed_pressure"
    phase_results = {}
    for check in checks:
        if check["kind"] != "artifact" or "checkpoint-" not in check["name"]:
            continue
        phase = int(check["name"].rsplit("checkpoint-", 1)[1].split(".")[0])
        phase_results[phase] = check["passed"]
    # Conservative: only LATER completed phases count as post-compaction work.
    last_compaction_phase = max((e["phase"] for e in successful), default=None)
    later = [v for p, v in phase_results.items() if last_compaction_phase is not None and p > last_compaction_phase]
    if qualified is True and min_compactions > 0 and not later:
        qualified, reason = False, "no_graded_stage_after_last_compaction"
    prefix = 0
    for phase in sorted(phase_results):
        if phase != prefix + 1 or not phase_results[phase]:
            break
        prefix += 1
    return {"qualified": qualified, "reason": reason, "requirements": req,
            "peak_measured_input_tokens": max((e["input_tokens"] for e in observed), default=None),
            "peak_measured_ratio": peak["ratio"] if peak else None,
            "peak_phase": peak["phase"] if peak else None,
            "last_measured_ratio": observed[-1]["ratio"] if observed else None,
            "max_estimated_ratio": max(estimated, default=None),
            "successful_compactions": len(successful) if events else None,
            "fallback_compactions": sum(e["fallback"] is True for e in successful) if events else None,
            "missing_main_usage_events": incomplete_usage,
            "measured_requests": len(observed),
            "post_last_compaction_stages": len(later),
            "post_last_compaction_stage_pass_rate": sum(later) / len(later) if later else None,
            "longest_correct_stage_prefix": prefix,
            "note": "Service-reported input tokens and declared local window. Provider model capacity must be independently fixed. Sum of billed inputs is not peak context."}


def inspect_session(path, window):
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    entries = data.get("entries", [])
    usage, new = data.get("usage", 0), data.get("new_usage", 0)
    return {"session_id": data.get("session_id"), "messages": sum(e.get("type") == "message" for e in entries),
            "persisted_compactions": sum(e.get("type") == "compaction" for e in entries),
            "final_anchor_plus_estimate": usage + new,
            "final_ratio": (usage + new) / window if window else None,
            "peak_ratio": None,
            "warning": "Final session anchor cannot reconstruct historical peak or all billable calls. A 16% final ratio can follow compaction; inspect event logs."}


def main():
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description=__doc__)
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--run")
    group.add_argument("--session")
    p.add_argument("--context-window", type=int, default=128000)
    args = p.parse_args()
    if args.session:
        result = inspect_session(args.session, args.context_window)
    else:
        root = Path(args.run)
        meta = json.loads((root / "run.json").read_text(encoding="utf-8"))
        grade_path = root / "results/grade.json"
        checks = json.loads(grade_path.read_text(encoding="utf-8"))["checks"] if grade_path.is_file() else []
        result = assess_pressure(root, meta, checks)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
