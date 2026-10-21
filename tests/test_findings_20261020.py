"""Regression tests for six defects found by reading the source (2026-10-20). Each test states the behavior the docs promise; each failed before its fix."""
import os
import tempfile
import unittest

from vega_harness import conductor as c
from vega_harness import predicted_path as pp
from vega_harness import reasoning_pipeline as rp
from vega_harness import use_gate as ug

OK = lambda commitment, action: isinstance(commitment, str) and commitment.startswith("SIGNED")
answer = lambda ctx: {"answer": 42}
verify = lambda ctx: ctx.get("answer") == 42


class Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.runs, self.reg, self.log = (os.path.join(self.d.name, n) for n in ("runs.jsonl", "reg.jsonl", "path.jsonl"))

    def tearDown(self):
        self.d.cleanup()

    def cond(self, **kw):
        kw.setdefault("commitment_check", OK)
        return c.Conductor(self.runs, self.reg, **kw)


class F1_CapStopMustNotSpendTheSignature(Base):
    def test_a_run_stopped_by_the_cap_leaves_the_commitment_unspent(self):
        steps = [c.Step("a", answer, human_only=True)]
        big = {"pad": "x" * 4000}
        r = self.cond(token_cap=10).run("t1", big, steps, verify, commitment="SIGNED-CAP")
        self.assertTrue(r["status"].startswith("stopped: token cap"), r["status"])
        again = self.cond().run("t2", big, steps, verify, commitment="SIGNED-CAP")
        self.assertEqual(again["status"], "success", "the signature was spent by a step that never ran")

    def test_a_per_run_cap_can_be_given_and_the_tighter_cap_wins(self):
        steps = [c.Step("a", answer)]
        big = {"pad": "x" * 4000}
        self.assertTrue(self.cond().run("t", big, steps, verify, token_cap=10)["status"].startswith("stopped: token cap"))
        self.assertTrue(self.cond(token_cap=10).run("t", big, steps, verify, token_cap=10**9)["status"].startswith("stopped: token cap"))
        self.assertEqual(self.cond(token_cap=10**9).run("t", big, steps, verify, token_cap=10**8)["status"], "success")
        for bad in (0, -1, True, 1.5, "5"):
            with self.assertRaises(c.ConductorRefused):
                self.cond().run("t", {}, steps, verify, token_cap=bad)


class F2_StepIdentityCoversCapturedValues(Base):
    def test_same_code_different_captured_value_is_a_different_workflow(self):
        def make(v):
            return c.Step("s", lambda ctx: {"answer": v})
        self.assertNotEqual(c.workflow_id([make(1)]), c.workflow_id([make(2)]))
        self.assertEqual(c.workflow_id([make(1)]), c.workflow_id([make(1)]))

    def test_a_different_default_argument_is_a_different_workflow(self):
        def make(v):
            def fn(ctx, k=v):
                return {"answer": k}
            return c.Step("s", fn)
        self.assertNotEqual(c.workflow_id([make(1)]), c.workflow_id([make(2)]))

    def test_unhashable_or_exotic_captured_values_do_not_crash_the_identity(self):
        obj = object()
        s = c.Step("s", lambda ctx: {"o": obj})
        self.assertIsInstance(c.workflow_id([s]), str)


class F3_FreezeBindsTheRegistryCode(Base):
    def reg_with(self, value):
        return {"actions": {"one": lambda st: value}, "recoveries": {"r": lambda st: None}}

    def sp(self):
        return {"goal": "g", "steps": [{"name": "s", "do": "one", "expect": {"equals": 1}, "read_only": True}]}

    def test_swapping_an_action_after_freezing_is_refused(self):
        sp = self.sp()
        pp.freeze(sp, self.reg_with(1), self.log)
        swapped = self.reg_with(2)
        out = pp.run(sp, swapped, self.log, commitment="SIGNED-1", commitment_check=OK)
        self.assertTrue(out["status"].startswith("refused"), out)
        self.assertIn("registry", out["status"])

    def test_the_registry_that_was_frozen_still_runs(self):
        sp = self.sp()
        reg = self.reg_with(1)
        pp.freeze(sp, reg, self.log)
        self.assertEqual(pp.run(sp, reg, self.log, commitment="SIGNED-2", commitment_check=OK)["status"], "completed")

    def test_swapping_a_recovery_or_check_is_refused_too(self):
        sp = {"goal": "g", "steps": [{"name": "s", "do": "one", "expect": {"custom": "c"}, "recovery": "r"}]}
        mk = lambda rv, cv: {"actions": {"one": lambda st: 1}, "checks": {"c": lambda o: cv}, "recoveries": {"r": lambda st: rv}}
        pp.freeze(sp, mk("a", True), self.log)
        self.assertTrue(pp.run(sp, mk("b", True), self.log, commitment="SIGNED-3", commitment_check=OK)["status"].startswith("refused"))
        self.assertTrue(pp.run(sp, mk("a", False), self.log, commitment="SIGNED-4", commitment_check=OK)["status"].startswith("refused"))

    def test_a_frozen_record_without_a_registry_binding_is_not_accepted(self):
        from vega_harness.records import RecordChain
        sp = self.sp()
        RecordChain(self.log).append("PATH_FROZEN", {"spec_hash": pp.spec_hash(sp), "goal": "g", "steps": ["s"]})   # an old-style record
        out = pp.run(sp, self.reg_with(1), self.log, commitment="SIGNED-5", commitment_check=OK)
        self.assertTrue(out["status"].startswith("refused"), out)


class F4_ReleaseMustStateTheTaintMark(Base):
    def test_a_release_record_with_no_taint_mark_is_refused(self):
        self.assertFalse(ug.release_gate({"library_sources": [], "clean_room_attestation": "spec by A, code by B"})["released"])

    def test_an_explicit_false_still_releases(self):
        self.assertTrue(ug.release_gate({"library_sources": [], "tainted": False, "clean_room_attestation": "spec by A, code by B"})["released"])


class F5_RequestTokenCapCapsTheRun(Base):
    def test_a_request_token_cap_stops_the_run(self):
        out = rp.plan({"goal": "plan something harmless", "token_cap": 5}, self.cond())
        self.assertTrue(out["run"]["status"].startswith("stopped: token cap"), out["run"]["status"])

    def test_without_a_cap_the_plan_completes(self):
        self.assertEqual(rp.plan({"goal": "plan something harmless"}, self.cond())["run"]["status"], "success")


class F6_PipelineUsesAppendOnlyPromptLayers(Base):
    def prefix(self, goal, **extra):
        out = rp.plan({"goal": goal}, self.cond(), deps=extra)
        return out["blueprint"]["cache_prefix"]

    def test_layers_are_recorded_in_the_blueprint(self):
        p = self.prefix("alpha goal")
        self.assertEqual([l["name"] for l in p["layers"]], ["policy", "goal", "anchors"])

    def test_the_policy_layer_prefix_is_identical_across_different_goals(self):
        a, b = self.prefix("first goal here"), self.prefix("a completely different goal")
        self.assertEqual(a["layers"][0]["prefix_hash"], b["layers"][0]["prefix_hash"])
        self.assertNotEqual(a["layers"][1]["prefix_hash"], b["layers"][1]["prefix_hash"])


class F7_MatrixRowCanForbidAPersonalBuild(Base):
    """The library docstrings say a personal read needs the matrix row to allow it; the gate never looked at the row's personal_build field."""

    def matrix(self, personal):
        return {"r": {"repo": "r", "license_class": "NO_LICENSE_FILE", "personal_build": personal, "public_build": "no", "taint_if_used": True}}

    def test_a_row_that_says_no_forbids_a_personal_build(self):
        for personal in ("no", "No personal use", "forbidden", "never", "not allowed", "refused", False, ""):
            out = ug.splice_gate(["r"], "personal", self.matrix(personal))
            self.assertFalse(out["allowed"], repr(personal))
            self.assertIn("forbids", out["reason"])

    def test_rows_that_allow_reading_in_quarantine_still_allow_it(self):
        for personal in ("read in quarantine only (no license = no permission)", "contained use, keep the license notice", "read in quarantine; output is TAINTED", "treated as NO_LICENSE_FILE until resolved"):
            self.assertTrue(ug.splice_gate(["r"], "personal", self.matrix(personal))["allowed"], personal)

    def test_a_repo_with_no_row_is_still_readable_in_quarantine(self):
        self.assertTrue(ug.splice_gate(["unknown"], "personal", {})["allowed"])


if __name__ == "__main__":
    unittest.main()
