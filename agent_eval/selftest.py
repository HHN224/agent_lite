"""Offline harness validation. These are NOT measured agent results."""
from __future__ import annotations

import tempfile
import unittest
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
