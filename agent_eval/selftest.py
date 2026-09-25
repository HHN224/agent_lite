"""Offline harness validation. These are NOT measured agent results."""
from __future__ import annotations

import tempfile
import unittest
import copy
import json
import shutil
import sys
import types
from unittest.mock import patch
from pathlib import Path

import eval as ev
from tasks import (CATALOG, LEDGER_REFERENCE, QUEUE_REFERENCE, dump, inventory_step,
                   ledger_oracle, queue_step, reconcile, write)


class HarnessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agent-eval-selftest-")
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def run_for(self, task, seed=17, scale="smoke", name=None):
        run = ev.prepare(task, seed, scale, self.root / (name or task))
        ev.start(run, "oracle-selftest", "none", "harness-only", 1, 3600, "normal")
        return run

    def end(self, run, status="completed", help_count=0):
        dump(run / "telemetry.json", {"human_interventions": help_count})
        ev.finish(run, status, run / "telemetry.json")

    def solve(self, run):
        _, meta, spec = ev.load_run(run)
        if meta["task"] in {"C01", "C02"}:
            code = LEDGER_REFERENCE if meta["task"] == "C01" else QUEUE_REFERENCE
            write(run / "workspace/src/main.py", code)
        elif "phases" in spec:
            if meta["task"].startswith("R"):
                shutil.copyfile(Path(__file__).with_name("long_tasks.py"), run / "workspace/src/reference.py")
                write(run / "workspace/src/main.py", "import json,sys\nfrom reference import replay\nd=json.load(sys.stdin)\nprint(json.dumps(replay(" + repr(spec["base_machine"]) + ",d['initial'],d['batches'])))\n")
            for phase in spec["phases"]:
                dump(run / "workspace" / phase["output"], spec["outputs"][phase["output"]])
                ev.checkpoint(run)
        else:
            for rel, value in spec["outputs"].items():
                # Do not rewrite protected already-migrated configs.
                if rel not in spec["immutable"]:
                    dump(run / "workspace" / rel, value)

    def test_all_reference_solutions_pass(self):
        for task in CATALOG:
            with self.subTest(task=task):
                run = self.run_for(task)
                self.solve(run)
                self.end(run)
                result = ev.grade(run, execute=True)
                self.assertTrue(result["autonomous_success"], [c for c in result["checks"] if not c["passed"]])

    def test_determinism_and_new_seed(self):
        for task in CATALOG:
            a = ev.prepare(task, 17, "smoke", self.root / (task + "a"))
            b = ev.prepare(task, 17, "smoke", self.root / (task + "b"))
            c = ev.prepare(task, 18, "smoke", self.root / (task + "c"))
            self.assertEqual(ev.read(a / "private/spec.json"), ev.read(b / "private/spec.json"))
            self.assertNotEqual(ev.read(a / "private/spec.json"), ev.read(c / "private/spec.json"))

    def test_empty_and_starter_submissions_fail(self):
        for task in CATALOG:
            with self.subTest(task=task):
                run = self.run_for(task)
                self.end(run)
                self.assertFalse(ev.grade(run, execute=True)["artifact_pass"])

    def test_wrong_cents_fail(self):
        run = self.run_for("A01")
        self.solve(run)
        path = run / "workspace/output/reconciliation.json"
        data = ev.read(path)
        data["balances"][0]["net_cents"] += 1
        dump(path, data)
        self.end(run)
        self.assertFalse(ev.grade(run)["autonomous_success"])

    def test_correct_output_does_not_hide_changed_input(self):
        run = self.run_for("A01")
        self.solve(run)
        write(run / "workspace/input/transactions/part-000.jsonl", "")
        self.end(run)
        result = ev.grade(run)
        self.assertFalse(result["integrity_pass"])
        self.assertFalse(result["artifact_pass"])

    def test_no_extra_config(self):
        run = self.run_for("A02")
        self.solve(run)
        dump(run / "workspace/configs/extra.json", {})
        self.end(run)
        self.assertFalse(ev.grade(run)["integrity_pass"])

    def test_unknown_evidence_not_zero(self):
        run = self.run_for("A03")
        self.solve(run)
        path = run / "workspace/output/incidents.json"
        data = ev.read(path)
        data[0]["lost_records"] = 0
        dump(path, data)
        self.end(run)
        self.assertFalse(ev.grade(run)["artifact_pass"])

    def test_snapshot_prevents_late_repair(self):
        run = self.run_for("L01")
        spec = ev.read(run / "private/spec.json")
        for i, phase in enumerate(spec["phases"]):
            answer = spec["outputs"][phase["output"]]
            if i == 0:
                dump(run / "workspace" / phase["output"], {})
            else:
                dump(run / "workspace" / phase["output"], answer)
            ev.checkpoint(run)
        first = spec["phases"][0]["output"]
        dump(run / "workspace" / first, spec["outputs"][first])
        self.end(run)
        result = ev.grade(run)
        self.assertFalse(result["artifact_pass"])
        self.assertFalse(result["integrity_pass"])

    def test_future_phases_hidden(self):
        run = self.run_for("L01")
        self.assertFalse((run / "workspace/input/batch-02.json").exists())
        self.assertNotIn("单位 2 件", (run / "workspace/PROMPT.md").read_text(encoding="utf-8"))

    def test_early_grading_and_regrading_rejected(self):
        run = self.run_for("A01")
        with self.assertRaises(ValueError):
            ev.grade(run)
        self.end(run)
        ev.grade(run)
        with self.assertRaises(ValueError):
            ev.grade(run)

    def test_timeouts_human_help_and_unknown_help_fail(self):
        for i, (status, help_count) in enumerate([("timeout", 0), ("completed", 1), ("completed", None)]):
            run = self.run_for("A01", name=f"r{i}")
            self.solve(run)
            self.end(run, status, help_count)
            result = ev.grade(run)
            self.assertTrue(result["artifact_pass"])
            self.assertFalse(result["autonomous_success"])

    def test_compaction_label_needs_event(self):
        run = self.run_for("A01")
        meta = ev.read(run / "run.json")
        meta["protocol"] = "compaction"
        dump(run / "run.json", meta)
        self.solve(run)
        self.end(run)
        self.assertFalse(ev.grade(run)["autonomous_success"])

    def test_strict_types_and_order(self):
        self.assertFalse(ev.same(True, 1))
        self.assertFalse(ev.same(1.0, 1))
        self.assertFalse(ev.same([1, 2], [2, 1]))
        self.assertFalse(ev.same({"a": 1, "extra": 2}, {"a": 1}))
        self.assertTrue(ev.same({"b": 2, "a": 1}, {"a": 1, "b": 2}))

    def test_oracles_against_hand_calculated_cases(self):
        base = dict(event_id="e", revision=1, account="a", currency="CNY", kind="credit", status="posted", amount_cents=10)
        self.assertEqual(reconcile([base, base, dict(base, revision=2, status="void")]), {"balances": [], "accepted_ids": []})
        data = {"initial": {"a": 10}, "events": [
            dict(id="z", seq=1, **{"from": "a"}, to="b", amount=7),
            dict(id="a", seq=1, **{"from": "a"}, to="c", amount=6),
            dict(id="s", seq=2, **{"from": "ghost"}, to="ghost", amount=999)]}
        self.assertEqual(ledger_oracle(data), {"balances": {"a": 4, "c": 6}, "applied": ["a", "s"], "rejected": ["z"]})
        state = {}
        self.assertEqual(queue_step(state, [dict(op="enqueue", id="a", payload=7, ready_at=1), dict(op="claim", now=1, lease=2)]),
                         [True, dict(id="a", payload=7, attempt=1, lease_until=3)])
        self.assertEqual(queue_step(state, [dict(op="ack", id="a", attempt=1, now=3)]), [False])
        stock, seen = {"x": 2}, set()
        batch = [dict(id="a", sku="x", delta=-2), dict(id="b", sku="x", delta=2), dict(id="a", sku="x", delta=-2)]
        self.assertEqual(inventory_step(stock, seen, batch, 2), (["b"], ["a"]))
        self.assertEqual(stock, {"x": 6})
        self.assertEqual(len(seen), 2)

    def test_summary_no_missing_cost_as_zero(self):
        run = self.run_for("A01")
        self.solve(run)
        self.end(run)
        ev.grade(run)
        result = ev.summarize(self.root)["groups"][0]
        self.assertEqual(result["success_rate"], 1)
        self.assertIsNone(result["cost_usd"])
        self.assertIsNone(result["cost_per_success_usd"])

    def test_standard_reference_data_tasks_and_long_stages(self):
        for task, scale in [("A01", "standard"), ("A02", "long"), ("A03", "long"), ("L01", "long")]:
            run = self.run_for(task, 29, scale)
            self.solve(run)
            self.end(run)
            self.assertTrue(ev.grade(run)["artifact_pass"])

    def test_memory_packet_is_not_a_workspace_history_file(self):
        run = self.run_for("M01")
        self.assertNotIn("memo-001-00000", (run / "workspace/PROMPT.md").read_text(encoding="utf-8"))
        self.assertIn("memo-001-00000", (run / "next_prompt.txt").read_text(encoding="utf-8"))
        self.assertNotIn("memo-002-00000", (run / "next_prompt.txt").read_text(encoding="utf-8"))

    def test_custom_pressure_size_and_stage_bounds(self):
        run = ev.prepare("M01", 17, "smoke", self.root / "custom", 5, 2500)
        spec = ev.read(run / "private/spec.json")
        self.assertEqual(len(spec["phases"]), 5)
        self.assertGreater(spec["pressure_design"]["total_prompt_chars"], 5 * 2500)
        for task, stages, size in [("A01", 3, None), ("W01", 3, 1000), ("M01", 129, 1000)]:
            with self.assertRaises(ValueError):
                ev.prepare(task, 17, "smoke", self.root / "invalid", stages, size)

    def test_source_snapshots_prevent_last_minute_code_repair(self):
        run = self.run_for("R01")
        spec = ev.read(run / "private/spec.json")
        first = spec["phases"][0]
        dump(run / "workspace" / first["output"], spec["outputs"][first["output"]])
        ev.checkpoint(run)  # freeze broken starter, even though checkpoint JSON is perfect
        shutil.copyfile(Path(__file__).with_name("long_tasks.py"), run / "workspace/src/reference.py")
        write(run / "workspace/src/main.py", "import json,sys\nfrom reference import replay\nd=json.load(sys.stdin)\nprint(json.dumps(replay('W01',d['initial'],d['batches'])))\n")
        for phase in spec["phases"][1:]:
            dump(run / "workspace" / phase["output"], spec["outputs"][phase["output"]])
            ev.checkpoint(run)
        self.end(run)
        result = ev.grade(run, True)
        self.assertFalse(result["artifact_pass"])
        failed = [c for c in result["checks"] if c["kind"] == "behavior" and not c["passed"]]
        self.assertEqual(len(failed), 2)
        self.assertTrue(all(c["name"].startswith("phase-001-") for c in failed))

    def test_pressure_peak_is_not_last_ratio_or_summary_usage(self):
        from pressure import assess_pressure
        run = self.run_for("M01")
        meta = ev.read(run / "run.json")
        meta["pressure_requirements"] = {"min_peak_ratio": 0.8, "min_compactions": 1}
        events = [
            {"type": "model_usage", "purpose": "main", "input_tokens": 820, "context_window": 1000, "phase": 1},
            {"type": "compaction_end", "success": True, "phase": 1},
            {"type": "model_usage", "purpose": "compaction", "input_tokens": 990, "context_window": 1000, "phase": 1},
            {"type": "model_usage", "purpose": "main", "input_tokens": 160, "context_window": 1000, "phase": 2}]
        write(run / "events.jsonl", "".join(json.dumps(dict(e, run_id=meta["run_id"])) + "\n" for e in events))
        result = assess_pressure(run, meta, [{"name": "artifact:output/checkpoint-002.json", "kind": "artifact", "passed": True}])
        self.assertTrue(result["qualified"])
        self.assertEqual(result["peak_measured_ratio"], 0.82)
        self.assertEqual(result["last_measured_ratio"], 0.16)
        self.assertEqual(result["post_last_compaction_stage_pass_rate"], 1)
        meta["pressure_requirements"]["min_compactions"] = 2
        self.assertFalse(assess_pressure(run, meta, [])["qualified"])

    def test_pressure_missing_data_not_a_zero_or_success(self):
        from pressure import assess_pressure
        run = self.run_for("M01")
        meta = ev.read(run / "run.json")
        meta["pressure_requirements"] = {"min_peak_ratio": 0.8, "min_compactions": 1}
        result = assess_pressure(run, meta, [])
        self.assertIsNone(result["qualified"])
        self.assertIsNone(result["peak_measured_ratio"])
        self.assertIsNone(result["successful_compactions"])

    def test_independent_memory_boundaries(self):
        from long_tasks import resolve_memory
        q = {"key": "a", "at": 2, "known_at": 2}
        a = dict(id="a1", key="a", revision=1, status="confirmed", value=10,
                 effective=1, recorded=1, authority="board", action="set", clause="x", min=1, max=9,
                 start=0, end=3, priority=1)
        b = dict(a, id="a2", revision=2, value=20, effective=3, recorded=3, authority="ops", action="withdraw",
                 start=2, end=5, priority=2)
        self.assertEqual(resolve_memory("M01", [a, b, dict(b, revision=3, status="draft")], q)["value"], 20)
        self.assertEqual(resolve_memory("M02", [a, b], q)["value"], 10)
        self.assertEqual(resolve_memory("M03", [a, b], q), {"value": None, "sources": ["a2"]})
        self.assertEqual(resolve_memory("M04", [a, b], q)["value"], 10)
        c = dict(a, id="a3", clause="y", min=10, max=20)
        self.assertEqual(resolve_memory("M05", [a, c], q)["value"], {"min": 10, "max": 9, "feasible": False})
        self.assertEqual(resolve_memory("M06", [dict(a, action="link", target="a")], q), {"value": None, "sources": ["a1"]})
        self.assertEqual(resolve_memory("M07", [a, dict(b, effective=1)], q)["value"], 10)
        self.assertEqual(resolve_memory("M08", [a, b], q)["value"], 20)
        self.assertEqual(resolve_memory("M08", [a, b], dict(q, at=5))["sources"], [])

    def test_independent_workflow_boundaries(self):
        from long_tasks import step
        stock = {"stock": {"a": 5}, "holds": {}}
        self.assertTrue(step("W01", stock, dict(op="reserve", sku="a", hold="h", qty=5)))
        self.assertTrue(step("W01", stock, dict(op="release", hold="h")))
        self.assertEqual(stock, {"stock": {"a": 5}, "holds": {}})
        transaction = {"balances": {"a": 5, "b": 0}, "pending": {"t": [dict(src="a", dst="b", amount=3), dict(src="a", dst="b", amount=3)]}}
        before = copy.deepcopy(transaction)
        self.assertFalse(step("W02", transaction, dict(op="commit", tx="t")))
        self.assertEqual(transaction, before)
        deploy = {"versions": {"a": 1, "b": 1}, "deps": {"a": [], "b": ["a"]}, "frozen": []}
        self.assertFalse(step("W03", deploy, dict(op="rollback", service="a")))
        auth = {"roles": {"a": {"allow": ["r"], "deny": [], "parents": ["b"]}, "b": {"allow": [], "deny": ["r"], "parents": ["a"]}}, "users": {"u": ["a"]}}
        self.assertFalse(step("W04", auth, dict(op="check", user="u", resource="r")))
        migration = {"fields": ["a"], "rows": {"r": {"revision": 5, "data": {"a": 10}}}}
        self.assertTrue(step("W05", migration, dict(op="rename", old="a", new="b")))
        self.assertEqual(migration["rows"]["r"], {"revision": 5, "data": {"b": 10}})
        self.assertFalse(step("W05", migration, dict(op="drop", field="b")))
        graph = {"nodes": ["a", "b", "c", "d"], "edges": [["a", "b"], ["b", "a"], ["b", "c"]]}
        self.assertEqual(step("W06", graph, dict(op="order")), {"order": ["d"], "blocked": ["a", "b", "c"]})
        replicas = {"values": {}}
        self.assertTrue(step("W07", replicas, dict(op="merge", key="a", clock=2, source="x", id="e", value=1, deleted=True)))
        self.assertFalse(step("W07", replicas, dict(op="merge", key="a", clock=1, source="z", id="f", value=2, deleted=False)))
        self.assertIsNone(step("W07", replicas, dict(op="read", key="a")))
        quota = {"limits": {"a": 10}, "used": {"a": 10}, "period": 1}
        self.assertFalse(step("W08", quota, dict(op="consume", tenant="a", qty=1)))
        self.assertTrue(step("W08", quota, dict(op="reset", period=2)))
        self.assertEqual(quota["used"], {"a": 0})
        build = {"deps": {"a": [], "b": ["a"], "c": ["b"]}, "dirty": [], "built": {"a": 1, "b": 1, "c": 1}}
        self.assertTrue(step("W09", build, dict(op="change", item="a")))
        self.assertEqual(build["dirty"], ["a", "b", "c"])
        self.assertFalse(step("W09", build, dict(op="build", item="c")))
        sla = {"now": 0, "tickets": {"a": {"status": "active", "remaining": 2}, "b": {"status": "paused", "remaining": 2}}}
        step("W10", sla, dict(op="tick", now=3))
        self.assertEqual(step("W10", sla, dict(op="report")), ["a"])
        self.assertEqual(sla["tickets"]["b"]["remaining"], 2)

    def test_adapter_totals_include_summarizer_and_unknown_usage(self):
        from run_agent_lite import telemetry_from_events
        run = self.run_for("A01")
        meta = ev.read(run / "run.json")
        events = [dict(type="model_usage", phase=1, purpose="main", input_tokens=100, output_tokens=20),
                  dict(type="model_usage", phase=1, purpose="compaction", input_tokens=50, output_tokens=10)]
        write(run / "events.jsonl", "".join(json.dumps(dict(e, run_id=meta["run_id"])) + "\n" for e in events))
        result = telemetry_from_events(run, meta["run_id"])
        self.assertEqual((result["input_tokens"], result["output_tokens"]), (150, 30))

    def test_adapter_worker_same_session_inline_delivery_without_network(self):
        import run_agent_lite as adapter
        run = self.run_for("M01")
        spec = ev.read(run / "private/spec.json")
        repo = self.root / "fake_repo"
        write(repo / "coding_agent/__main__.py", "# Fake module; no network\n")
        app = types.ModuleType("coding_agent.__main__")
        parent = types.ModuleType("coding_agent")
        parent.__main__ = app
        dotenv = types.ModuleType("dotenv")
        dotenv.load_dotenv = lambda *a, **k: None
        prompts, instances = [], []
        class FakeProvider:
            def __init__(self):
                self.client = types.SimpleNamespace(close=lambda: None)
                self.last_usage = None
            def stream(self, messages, tools, model, **options):
                self.last_usage = {"prompt_tokens": 100 if tools else 50, "completion_tokens": 10}
                yield "mock-delta"
        class FakeAgent:
            session_id = "fake-single-session"
            def __init__(self):
                self.provider, self.summary = app.OpenAIProvider(), app.OpenAIProvider()
            def prompt(self, prompt):
                prompts.append(prompt)
                i = len(prompts) - 1
                list(self.provider.stream([], ["tool"], "mock"))
                list(self.summary.stream([], [], "mock"))
                phase = spec["phases"][i]
                dump(run / "workspace" / phase["output"], spec["outputs"][phase["output"]])
                yield types.SimpleNamespace(type="agent_end", data={"reason": "completed"})
        app.OpenAIProvider = app.CommandCodeProvider = FakeProvider
        app.parse_args = lambda argv: types.SimpleNamespace(session="")
        def build(*_):
            agent = FakeAgent()
            instances.append(agent)
            return agent
        app.build_agent = build
        args = adapter.parse_args(["--run", str(run), "--repo-root", str(repo), "--model", "mock", "--variant", "selftest", "--sandbox", "host"])
        modules = {"coding_agent": parent, "coding_agent.__main__": app, "dotenv": dotenv}
        original_path = sys.path.copy()
        try:
            with patch.dict(sys.modules, modules), patch.dict("os.environ", {"CMD_API_KEY": "fake-not-a-real-key"}):
                adapter.worker(args)
        finally:
            sys.path[:] = original_path
        self.assertEqual(len(instances), 1)
        self.assertEqual(len(prompts), 4)
        self.assertIn("memo-001-00000", prompts[0])
        self.assertNotIn("memo-001-00000", prompts[3])
        self.assertIn("memo-004-00000", prompts[3])
        meta = ev.read(run / "run.json")
        self.assertEqual(meta["current_phase"], 5)
        self.assertEqual(adapter.telemetry_from_events(run, meta["run_id"])["input_tokens"], 600)


if __name__ == "__main__":
    unittest.main(verbosity=2)
