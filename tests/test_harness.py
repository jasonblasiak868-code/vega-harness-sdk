"""Functional tests: Conductor, predicted path, use gate and the reasoning pipeline (ported from the flat scripts, adapted to the chain-backed API)."""
import os
import tempfile
import unittest

from vega_harness import conductor as c
from vega_harness import predicted_path as pp
from vega_harness import reasoning_pipeline as rp
from vega_harness import use_gate as ug
from vega_harness.records import RecordChain

verify = lambda ctx: ctx.get("answer") == 42
agree = lambda ctx: {"agree": True, "notes": "second opinion ok"}
disagree = lambda ctx: {"agree": False, "notes": "I do not agree"}
OK = lambda commitment, action: isinstance(commitment, str) and commitment.startswith("SIGNED")


def mk_steps(extra=None):
    steps = [c.Step("recall", lambda ctx: {"facts": ["a", "b"]}), c.Step("compute", lambda ctx: {"answer": len(ctx["facts"]) * 21}, verbs=["overwrite"])]
    return steps + (extra or [])


class Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.runs, self.reg, self.log = (os.path.join(self.d.name, n) for n in ("runs.jsonl", "reg.jsonl", "path.jsonl"))

    def tearDown(self):
        self.d.cleanup()

    def conductor(self, **kw):
        return c.Conductor(self.runs, self.reg, preflight=lambda verbs: [{"id": "L1", "title": "t", "protect": "backup first"}] if "overwrite" in verbs else [],
                           commitment_check=OK, **kw)


class ConductorTests(Base):
    def test_success_records_chain_and_registers_candidate(self):
        r = self.conductor().run("t1", {}, mk_steps(), verify, agree)
        self.assertEqual(r["status"], "success")
        self.assertEqual(c.verify_chain(self.runs), (True, None))
        rows = c.read(self.reg)
        self.assertEqual((len(rows), rows[0]["status"]), (1, "candidate"))

    def test_preflight_lessons_are_recorded_and_protect_text_reaches_the_step(self):
        seen = {}
        steps = [c.Step("compute", lambda ctx: seen.update(p=ctx.get("_preflight_compute")) or {"answer": 42}, verbs=["overwrite"])]
        r = self.conductor().run("t2", {}, steps, verify)
        self.assertEqual((r["steps"][0]["preflight"], seen["p"]), (["L1"], ["backup first"]))

    def test_human_only_step_needs_a_valid_commitment(self):
        steps = mk_steps([c.Step("delete_old", lambda ctx: {}, human_only=True)])
        self.assertTrue(self.conductor().run("t3", {}, steps, verify)["status"].startswith("refused"))
        self.assertEqual(self.conductor().run("t3b", {}, steps, verify, commitment="SIGNED-1")["status"], "success")
        self.assertTrue(self.conductor().run("t3c", {}, steps, verify, commitment="forged")["status"].startswith("refused"))

    def test_token_cap_stops_the_run(self):
        r = self.conductor(token_cap=5).run("t4", {"big": "x" * 400}, mk_steps(), verify)
        self.assertTrue(r["status"].startswith("stopped"))
        self.assertEqual(len(c.read(self.reg)), 0)

    def test_second_opinion_disagreement_blocks_success_and_registration(self):
        self.assertEqual(self.conductor().run("t5", {}, mk_steps(), verify, disagree)["status"], "failed: second opinion disagreed")
        self.assertEqual(len(c.read(self.reg)), 0)

    def test_programmatic_verify_failure_blocks_success(self):
        self.assertEqual(self.conductor().run("t6", {}, mk_steps(), lambda ctx: False, agree)["status"], "failed: verification")

    def test_replay_locks_only_when_the_result_reproduces(self):
        cd = self.conductor()
        cd.run("t7", {}, mk_steps(), verify, agree)
        self.assertTrue(cd.replay("t7", {}, mk_steps(), verify, agree))
        self.assertEqual(len(c.locked_workflows(self.reg)), 1)

    def test_step_error_is_recorded_not_raised(self):
        r = self.conductor().run("t9", {}, [c.Step("boom", lambda ctx: 1 / 0)], verify)
        self.assertTrue(r["status"].startswith("failed: step 'boom'"))

    def test_chain_detects_tampering(self):
        self.conductor().run("t10", {}, mk_steps(), verify, agree)
        text = open(self.runs, encoding="utf-8").read().replace('"success"', '"SUCCESS"')
        with open(self.runs, "w", encoding="utf-8") as fh:
            fh.write(text)
        self.assertFalse(c.verify_chain(self.runs)[0])


def registry():
    return {"actions": {"read": lambda st: st.setdefault("data", [1, 2, 3]), "write": lambda st: (st.update(written=True), "wrote")[1],
                        "flaky": lambda st: "weird", "boom": lambda st: 1 / 0, "num": lambda st: 7},
            "checks": {"is_list": lambda o: isinstance(o, list)}, "responses": {"retry_later": lambda st, obs: "queued retry"},
            "recoveries": {"restore": lambda st: (st.pop("written", None), "restored")[1]}}


def spec(steps, goal="g"):
    return {"goal": goal, "steps": steps}


class PathTests(Base):
    def run_spec(self, sp, state=None, commitment="SIGNED-A"):
        return pp.run(sp, self.reg_, self.log, state=state, commitment=commitment, commitment_check=OK)

    def setUp(self):
        super().setUp()
        self.reg_ = registry()

    def test_no_parachute_no_jump(self):
        sp = spec([{"name": "w", "do": "write", "expect": {"equals": "wrote"}}])
        with self.assertRaises(pp.SpecRefused) as cm:
            pp.freeze(sp, self.reg_, self.log)
        self.assertIn("no parachute", str(cm.exception))
        sp["steps"][0]["recovery"] = "missing"
        with self.assertRaises(pp.SpecRefused):
            pp.freeze(sp, self.reg_, self.log)

    def test_read_only_on_path_completes(self):
        sp = spec([{"name": "r", "do": "read", "expect": {"custom": "is_list"}, "read_only": True}])
        pp.freeze(sp, self.reg_, self.log)
        out = self.run_spec(sp)
        self.assertEqual((out["status"], out["steps"][0]["outcome"]), ("completed", "ON_PATH"))

    def test_run_refused_when_not_frozen_changed_or_no_commitment(self):
        sp = spec([{"name": "r", "do": "read", "expect": {"custom": "is_list"}, "read_only": True}])
        self.assertTrue(self.run_spec(sp)["status"].startswith("refused"))
        pp.freeze(sp, self.reg_, self.log)
        self.assertTrue(self.run_spec(sp, commitment="forged")["status"].startswith("refused"))
        sp["steps"][0]["expect"] = {"equals": 1}
        self.assertIn("changed after freezing", self.run_spec(sp, commitment="SIGNED-B")["status"])

    def test_foreseen_alternative_applies_its_response_and_continues(self):
        sp = spec([{"name": "f", "do": "flaky", "expect": {"equals": "fine"}, "alternatives": [{"name": "weird_case", "match": {"equals": "weird"}, "response": "retry_later"}], "read_only": True},
                   {"name": "n", "do": "num", "expect": {"in_range": [1, 10]}, "read_only": True}])
        pp.freeze(sp, self.reg_, self.log)
        out = self.run_spec(sp)
        self.assertEqual((out["status"], [e["outcome"] for e in out["steps"]]), ("completed", ["PREDICTED_ALT", "ON_PATH"]))

    def test_unforeseen_result_halts_recovers_and_stops_later_steps(self):
        sp = spec([{"name": "w", "do": "write", "expect": {"equals": "wrote"}, "recovery": "restore"},
                   {"name": "f", "do": "flaky", "expect": {"equals": "fine"}, "recovery": "restore"},
                   {"name": "after", "do": "num", "expect": {"in_range": [1, 10]}, "read_only": True}])
        pp.freeze(sp, self.reg_, self.log)
        st = {}
        out = self.run_spec(sp, state=st)
        self.assertEqual((out["status"], len(out["steps"])), ("halted_off_path", 2))
        self.assertNotIn("written", st)
        self.assertTrue(out["incident"]["lesson_queued"])
        self.assertEqual(c.verify_chain(self.log), (True, None))

    def test_exception_is_off_path_and_contained(self):
        sp = spec([{"name": "b", "do": "boom", "expect": {"equals": 1}, "recovery": "restore"}])
        pp.freeze(sp, self.reg_, self.log)
        out = self.run_spec(sp)
        self.assertEqual((out["status"], out["incident"]["type"]), ("halted_off_path", "exception"))

    def test_calibration_reports_measured_hit_rate_with_a_lower_bound(self):
        sp = spec([{"name": "r", "do": "read", "expect": {"custom": "is_list"}, "read_only": True}])
        pp.freeze(sp, self.reg_, self.log)
        for i in range(3):
            self.run_spec(sp, commitment="SIGNED-%d" % i)
        cal = pp.calibration(self.log)
        self.assertEqual((cal["steps"], cal["held"], cal["off_path"]), (3, 3, 0))
        self.assertTrue(0.4 < cal["wilson_lower"] < 1.0)

    def test_unknown_descriptor_is_refused_at_freeze(self):
        with self.assertRaises(pp.SpecRefused):
            pp.freeze(spec([{"name": "r", "do": "read", "expect": {"nonsense": 1}, "read_only": True}]), self.reg_, self.log)


class GateTests(unittest.TestCase):
    M = {"a": {"repo": "a", "license_class": "PERMISSIVE", "personal_build": "ok", "public_build": "clean-room only", "taint_if_used": True},
         "b": {"repo": "b", "license_class": "STRONG_COPYLEFT", "personal_build": "quarantine; TAINTED", "public_build": "clean-room only", "taint_if_used": True},
         "c": {"repo": "c", "license_class": "UNKNOWN", "personal_build": "quarantine", "public_build": "clean-room only", "taint_if_used": True}}

    def test_public_refuses_any_library_source_and_personal_is_tainted_with_the_strictest_class(self):
        self.assertFalse(ug.splice_gate(["a"], "public", self.M)["allowed"])
        r = ug.splice_gate(["a", "b"], "personal", self.M)
        self.assertEqual((r["allowed"], r["tainted"], r["strictest_class"]), (True, True, "STRONG_COPYLEFT"))
        self.assertEqual(ug.klass("c", self.M), "NO_LICENSE_FILE")

    def test_release_gate(self):
        self.assertFalse(ug.release_gate({"library_sources": ["a"], "clean_room_attestation": "x"})["released"])
        self.assertFalse(ug.release_gate({"library_sources": [], "tainted": True, "clean_room_attestation": "x"})["released"])
        self.assertFalse(ug.release_gate({"library_sources": []})["released"])
        self.assertTrue(ug.release_gate({"library_sources": [], "clean_room_attestation": "spec by A, code by B"})["released"])

    def test_use_card_names_the_class(self):
        self.assertIn("STRONG_COPYLEFT", ug.use_card("b", self.M))
        self.assertIn("not in the library matrix", ug.use_card("zzz", self.M))


class PipelineTests(Base):
    def cond(self):
        return c.Conductor(self.runs, self.reg, commitment_check=OK)

    def test_plan_without_deps_succeeds_and_says_what_is_not_built(self):
        r = rp.plan({"goal": "summarize the handoff", "prompt": "p"}, self.cond())
        self.assertEqual(r["run"]["status"], "success")
        bp = r["blueprint"]
        self.assertTrue(bp["requires_commitment"])
        for k in ("prompt_reconstruction", "lattice_prediction", "history", "arsenal", "hybrid"):
            self.assertIn(k, bp["unbuilt"])
        self.assertEqual(bp["path"]["option"], 2)
        self.assertEqual(c.verify_chain(self.runs), (True, None))

    def test_execute_needs_a_valid_commitment(self):
        self.assertTrue(rp.plan({"goal": "summarize"}, self.cond(), execute=True)["run"]["status"].startswith("refused"))
        self.assertTrue(rp.plan({"goal": "summarize"}, self.cond(), execute=True, commitment="forged")["run"]["status"].startswith("refused"))
        self.assertEqual(rp.plan({"goal": "summarize"}, self.cond(), execute=True, commitment="SIGNED-P")["run"]["status"], "success")

    def test_risky_verbs_and_non_permissive_sources_demand_pre_peak(self):
        bp = rp.plan({"goal": "reuse the planner"}, self.cond(), {"library_fit": lambda g: [{"repo": "x", "license_class": "STRONG_COPYLEFT"}]})["blueprint"]
        self.assertTrue(bp["containment"]["need_pre_peak"])
        bp2 = rp.plan({"goal": "reuse the planner"}, self.cond(), {"library_fit": lambda g: [{"repo": "x", "license_class": "PERMISSIVE"}]})["blueprint"]
        self.assertFalse(bp2["containment"]["need_pre_peak"])
        self.assertTrue(rp.plan({"goal": "delete the cache"}, self.cond())["blueprint"]["containment"]["need_pre_peak"])

    def test_first_match_is_never_a_stop(self):
        fits = [{"repo": "first", "license_class": "PERMISSIVE"}, {"repo": "second", "license_class": "PERMISSIVE"}, {"repo": "third", "license_class": "PERMISSIVE"}]
        scores = {"first": {"passes": False, "score": 0.4}, "second": {"passes": True, "score": 0.9}, "third": {"passes": True, "score": 0.7}}
        deps = {"library_fit": lambda g: fits, "verify_candidate": lambda cand: scores[cand["repo"]], "downstream_risk": lambda cand: {"status": "checked"}}
        ch = rp.plan({"goal": "reuse a cache"}, self.cond(), deps)["blueprint"]["challenge"]
        self.assertEqual((ch["considered"], ch["stopped_at_first_match"], ch["chosen"]["candidate"]["repo"]), (3, False, "second"))
        self.assertEqual([r["candidate"]["repo"] for r in ch["rejected"]], ["first"])

    def test_prompt_layers_are_append_only(self):
        a = rp.prompt_layers([("policy", "p"), ("goal", "g")])
        b = rp.prompt_layers([("policy", "p"), ("goal", "g"), ("volatile", "v")])
        self.assertEqual(a["layers"][1]["prefix_hash"], b["layers"][1]["prefix_hash"])
        self.assertTrue(b["text"].startswith(a["text"]))


if __name__ == "__main__":
    unittest.main()
