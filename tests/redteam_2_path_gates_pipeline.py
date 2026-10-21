"""RED TEAM PASS 2 (harness): type-confused descriptors, exceptions past the parachute, a signature for the wrong spec, leaky logs, gate bypass shapes, a pipeline that skips the lesson check."""
import json
import os
import pathlib
import tempfile
import unittest

from vega_harness import conductor as c
from vega_harness import predicted_path as pp
from vega_harness import reasoning_pipeline as rp
from vega_harness import use_gate as ug

OK = lambda commitment, action: isinstance(commitment, str) and commitment.startswith("SIGNED")


def registry(**extra):
    r = {"actions": {"one": lambda st: 1, "true": lambda st: True, "boom": lambda st: 1 / 0, "write": lambda st: (st.update(w=1), "wrote")[1],
                     "secretish": lambda st: "sk-" + "a1B2c3D4" * 4, "obj": lambda st: object(), "num": lambda st: float("nan")},
         "checks": {"loose": lambda o: "yes", "crash": lambda o: 1 / 0, "strict": lambda o: o == 1},
         "responses": {"ok": lambda st, o: "fine", "crash": lambda st, o: 1 / 0},
         "recoveries": {"restore": lambda st: (st.pop("w", None), "restored")[1], "crash": lambda st: 1 / 0, "leaky": lambda st: "sk-" + "Z9y8X7w6" * 4}}
    for k, v in extra.items():
        r[k].update(v)
    return r


def spec(*steps, goal="g"):
    return {"goal": goal, "steps": list(steps)}


class Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.log = os.path.join(self.d.name, "path.jsonl")
        self.reg = registry()

    def tearDown(self):
        self.d.cleanup()

    def go(self, sp, n=0, **kw):
        return pp.run(sp, self.reg, self.log, commitment=kw.pop("commitment", "SIGNED-%d" % n), commitment_check=kw.pop("check", OK), **kw)

    def frozen(self, sp):
        pp.freeze(sp, self.reg, self.log)
        return sp


class Descriptors(Base):
    def test_equals_is_type_strict(self):
        for obs_action, want in (("true", 1), ("one", True), ("one", 1.0)):
            self.assertFalse(pp.matches({"equals": want}, self.reg["actions"][obs_action]({}), self.reg), (obs_action, want))
        self.assertTrue(pp.matches({"equals": 1}, 1, self.reg))
        self.assertTrue(pp.matches({"equals": {"a": [1, 2]}}, {"a": [1, 2]}, self.reg))

    def test_in_range_rejects_booleans_nan_and_odd_bounds(self):
        self.assertFalse(pp.matches({"in_range": [0, 5]}, True, self.reg))
        self.assertFalse(pp.matches({"in_range": [0, 5]}, float("nan"), self.reg))
        self.assertFalse(pp.matches({"in_range": [0, float("inf")]}, 3, self.reg))
        self.assertFalse(pp.matches({"in_range": [0]}, 3, self.reg))
        self.assertFalse(pp.matches({"in_range": "05"}, 3, self.reg))
        self.assertTrue(pp.matches({"in_range": [0, 5]}, 3, self.reg))

    def test_is_true_means_the_observation_is_that_boolean(self):
        for obs in ("False", [], "yes", 1, None):
            self.assertFalse(pp.matches({"is_true": True}, obs, self.reg), repr(obs))
        self.assertTrue(pp.matches({"is_true": True}, True, self.reg))
        self.assertTrue(pp.matches({"is_true": False}, False, self.reg))

    def test_a_custom_check_must_return_exactly_true(self):
        self.assertFalse(pp.matches({"custom": "loose"}, 1, self.reg))
        self.assertTrue(pp.matches({"custom": "strict"}, 1, self.reg))

    def test_contains_needs_text_and_never_crashes(self):
        for needle in (5, None, ["a"]):
            self.assertFalse(pp.matches({"contains": needle}, "abc", self.reg))
        self.assertTrue(pp.matches({"contains": "b"}, "abc", self.reg))
        self.assertFalse(pp.matches({"contains": "b"}, object(), self.reg))

    def test_a_descriptor_with_two_tests_or_none_is_refused_at_freeze(self):
        for d in ({"equals": 1, "custom": "strict"}, {"equals": 1, "contains": "x"}, {}, {"type": "something_else"}, "equals", None, 5):
            with self.assertRaises(pp.SpecRefused, msg=repr(d)):
                pp.freeze(spec({"name": "s", "do": "one", "expect": d, "read_only": True}), self.reg, self.log)


class Containment(Base):
    def test_a_crashing_check_response_or_recovery_halts_and_is_recorded_not_raised(self):
        sp = self.frozen(spec({"name": "s", "do": "one", "expect": {"custom": "crash"}, "recovery": "restore"}))
        out = self.go(sp, 1)
        self.assertEqual((out["status"], out["incident"]["type"]), ("halted_off_path", "exception"))
        sp2 = self.frozen(spec({"name": "s", "do": "one", "expect": {"equals": 9}, "alternatives": [{"name": "a", "match": {"equals": 1}, "response": "crash"}], "recovery": "restore"}, goal="g2"))
        self.assertEqual(self.go(sp2, 2)["status"], "halted_off_path")
        sp3 = self.frozen(spec({"name": "s", "do": "boom", "expect": {"equals": 1}, "recovery": "crash"}, goal="g3"))
        out3 = self.go(sp3, 3)
        self.assertEqual(out3["status"], "halted_off_path")
        self.assertTrue(out3["incident"]["recovery_result_hash"].startswith("RECOVERY_FAILED"))
        self.assertEqual(c.verify_chain(self.log), (True, None))

    def test_a_loose_check_does_not_turn_a_surprise_into_on_path(self):
        sp = self.frozen(spec({"name": "s", "do": "one", "expect": {"custom": "loose"}, "recovery": "restore"}))
        self.assertEqual(self.go(sp, 4)["status"], "halted_off_path")

    def test_outputs_that_are_not_json_do_not_crash_the_log(self):
        sp = self.frozen(spec({"name": "s", "do": "obj", "expect": {"equals": 1}, "recovery": "restore"}))
        self.assertEqual(self.go(sp, 5)["status"], "halted_off_path")
        sp2 = self.frozen(spec({"name": "s", "do": "num", "expect": {"equals": 1}, "recovery": "restore"}, goal="g6"))
        self.assertEqual(self.go(sp2, 6)["status"], "halted_off_path")

    def test_results_enter_the_log_as_hashes_never_as_text(self):
        sp = self.frozen(spec({"name": "s", "do": "secretish", "expect": {"equals": 1}, "recovery": "leaky"}))
        out = self.go(sp, 7)
        self.assertEqual(out["status"], "halted_off_path")
        text = pathlib.Path(self.log).read_text(encoding="utf-8")
        self.assertNotIn("a1B2c3D4", text)
        self.assertNotIn("Z9y8X7w6", text)
        self.assertEqual(len(out["incident"]["recovery_result_hash"]), 64)


class Commitments(Base):
    def test_a_signature_for_one_spec_does_not_run_another(self):
        a = self.frozen(spec({"name": "a", "do": "one", "expect": {"equals": 1}, "read_only": True}, goal="same goal"))
        b = self.frozen(spec({"name": "b", "do": "one", "expect": {"equals": 1}, "read_only": True}, goal="same goal"))
        wanted = "execute:" + pp.spec_hash(a)
        check = lambda cm, action: cm == "SIGNED-A" and action == wanted
        self.assertEqual(self.go(a, check=check, commitment="SIGNED-A")["status"], "completed")
        self.assertTrue(self.go(b, check=check, commitment="SIGNED-A")["status"].startswith("refused"))

    def test_one_signature_runs_a_spec_once(self):
        a = self.frozen(spec({"name": "a", "do": "one", "expect": {"equals": 1}, "read_only": True}))
        self.assertEqual(self.go(a, commitment="SIGNED-ONCE")["status"], "completed")
        self.assertIn("already used", self.go(a, commitment="SIGNED-ONCE")["status"])

    def test_a_loose_or_crashing_checker_does_not_run_anything(self):
        a = self.frozen(spec({"name": "a", "do": "one", "expect": {"equals": 1}, "read_only": True}))
        for chk in (lambda cm, ac: "yes", lambda cm, ac: 1, lambda cm, ac: 1 / 0, None):
            self.assertTrue(pp.run(a, self.reg, self.log, commitment="SIGNED-X", commitment_check=chk)["status"].startswith("refused"))

    def test_a_spec_frozen_in_another_log_is_not_frozen_here(self):
        a = self.frozen(spec({"name": "a", "do": "one", "expect": {"equals": 1}, "read_only": True}))
        other = os.path.join(self.d.name, "other.jsonl")
        self.assertIn("not frozen", pp.run(a, self.reg, other, commitment="SIGNED-Y", commitment_check=OK)["status"])


class SpecShape(Base):
    def test_malformed_specs_and_registries_are_refusals(self):
        for sp in (None, [], {}, {"steps": []}, {"steps": "x"}, {"steps": [None]}, {"steps": [{"name": ""}]}, {"steps": [{"name": "a", "do": "nope", "expect": {"equals": 1}, "read_only": True}]},
                   {"steps": [{"name": "a", "do": "one", "expect": {"equals": float("nan")}, "read_only": True}]},
                   {"steps": [{"name": "a", "do": "one", "expect": {"equals": 1}, "read_only": True}, {"name": "a", "do": "one", "expect": {"equals": 1}, "read_only": True}]},
                   {"steps": [{"name": "a", "do": "one", "expect": {"equals": 1}, "read_only": True, "alternatives": [{"name": "x"}]}]}):
            with self.assertRaises(pp.SpecRefused, msg=repr(sp)[:60]):
                pp.freeze(sp, self.reg, self.log)
        with self.assertRaises(pp.SpecRefused):
            pp.freeze(spec({"name": "a", "do": "one", "expect": {"equals": 1}, "read_only": True}), {}, self.log)
        self.assertEqual(c.read(self.log), [])

    def test_calibration_can_be_read_for_one_spec(self):
        a = self.frozen(spec({"name": "a", "do": "one", "expect": {"equals": 1}, "read_only": True}))
        b = self.frozen(spec({"name": "b", "do": "one", "expect": {"equals": 2}, "recovery": "restore"}, goal="other"))
        self.go(a, 1)
        self.go(b, 2)
        self.assertEqual(pp.calibration(self.log, pp.spec_hash(b))["held"], 0)
        self.assertEqual(pp.calibration(self.log, pp.spec_hash(a))["held"], 1)


class Gates(unittest.TestCase):
    M = {"perm": {"repo": "perm", "license_class": "PERMISSIVE"}, "weird": {"repo": "weird", "license_class": "MIT-ish"}, "strong": {"repo": "strong", "license_class": "STRONG_COPYLEFT"}}

    def test_a_bare_string_is_not_a_list_of_repos(self):
        for srcs in ("perm", b"perm", None, 5, [5], ["perm", None]):
            self.assertFalse(ug.splice_gate(srcs, "personal", self.M)["allowed"], repr(srcs))

    def test_unknown_class_missing_matrix_and_odd_build_values_fail_strict(self):
        self.assertEqual(ug.klass("weird", self.M), "NO_LICENSE_FILE")
        self.assertEqual(ug.klass("nowhere", self.M), "NO_LICENSE_FILE")
        old = os.environ.pop("VEGA_USE_MATRIX", None)
        try:
            self.assertEqual(ug.klass("perm"), "NO_LICENSE_FILE")                         # no matrix at all: everything is unknown
            self.assertEqual(ug.splice_gate(["perm"], "personal")["strictest_class"], "NO_LICENSE_FILE")
        finally:
            if old:
                os.environ["VEGA_USE_MATRIX"] = old
        for build in (None, "Public", "", ["public"], 5):
            self.assertFalse(ug.splice_gate(["perm"], build, self.M)["allowed"])
        self.assertFalse(ug.splice_gate(["perm"], "public", self.M)["allowed"])

    def test_release_gate_wants_everything_stated_not_just_nothing_wrong(self):
        good = {"library_sources": [], "tainted": False, "clean_room_attestation": "spec by A, code by B"}
        self.assertTrue(ug.release_gate(good)["released"])
        for bad in (None, [], "x", {}, {"library_sources": [], "clean_room_attestation": "spec by A, code by B"}, {"clean_room_attestation": "x"}, {"library_sources": "none", "clean_room_attestation": "x"}, {"library_sources": None, "clean_room_attestation": "x"},
                    {"library_sources": [], "clean_room_attestation": True}, {"library_sources": [], "clean_room_attestation": ""}, {"library_sources": [], "clean_room_attestation": "  "},
                    {"library_sources": [], "clean_room_attestation": {}}, {**good, "tainted": True}, {**good, "tainted": "false"}, {**good, "tainted": None}, {**good, "library_sources": ["x"]}):
            self.assertFalse(ug.release_gate(bad)["released"], repr(bad)[:70])
        self.assertTrue(ug.release_gate({**good, "tainted": False})["released"])
        self.assertTrue(ug.release_gate({"library_sources": [], "tainted": False, "clean_room_attestation": {"spec": "sha", "implementer": "B"}})["released"])

    def test_a_damaged_matrix_file_is_a_clear_error_not_a_permissive_default(self):
        d = tempfile.TemporaryDirectory()
        try:
            p = os.path.join(d.name, "m.json")
            pathlib.Path(p).write_text('{"not": "a list"}', encoding="utf-8")
            with self.assertRaises(ug.GateRefused):
                ug.load_matrix(p)
            pathlib.Path(p).write_text("{broken", encoding="utf-8")
            with self.assertRaises(ValueError):
                ug.load_matrix(p)
        finally:
            d.cleanup()


class Pipeline(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.runs, self.reg, self.log = (os.path.join(self.d.name, n) for n in ("runs.jsonl", "reg.jsonl", "path.jsonl"))

    def tearDown(self):
        self.d.cleanup()

    def cond(self, **kw):
        kw.setdefault("commitment_check", OK)
        return c.Conductor(self.runs, self.reg, **kw)

    def test_the_lesson_preflight_is_actually_consulted_for_risky_requests(self):
        asked = []
        r = rp.plan({"goal": "delete the old backups and push the result"}, self.cond(preflight=lambda verbs: asked.append(list(verbs)) or [{"id": "L1", "protect": "back up first"}]))
        self.assertEqual(r["run"]["status"], "success")
        self.assertTrue(asked, "the pipeline never asked the lesson ledger")
        self.assertTrue(all("delete" in v and "push" in v for v in asked))
        by_step = {s["step"]: s["preflight"] for s in r["run"]["steps"]}
        self.assertEqual((by_step["s02_prior"], by_step["s11_containment"]), (["L1"], ["L1"]))

    def test_verb_detection_uses_word_boundaries_and_inflections(self):
        self.assertEqual(rp.risky_verbs("we are deleting old files"), ["delete"])
        self.assertEqual(rp.risky_verbs("sends the report; paid yesterday"), ["send", "pay"])
        self.assertEqual(rp.risky_verbs("was running and pushing"), ["push", "run"])
        self.assertEqual(rp.risky_verbs("a brunch menu and a truncated view of the descent"), [])
        self.assertEqual(rp.risky_verbs("RUN the thing and Install it"), ["install", "run"])

    def test_bad_requests_are_refused(self):
        for bad in (None, [], {}, {"goal": ""}, {"goal": "  "}, {"goal": 5}, {"goal": "x", "prompt": 5}, {"goal": "x", "token_cap": True}, {"goal": "x", "token_cap": 0}, {"goal": "x", "token_cap": "5"}):
            with self.assertRaises(rp.RequestRefused, msg=repr(bad)):
                rp.plan(bad, self.cond())

    def test_a_dependency_that_returns_non_json_fails_the_run_closed(self):
        r = rp.plan({"goal": "summarize"}, self.cond(), {"recall": lambda q: {1, 2, 3}})
        self.assertTrue(r["run"]["status"].startswith("failed: step 's02_prior'"))
        self.assertIsNone(r["blueprint"])

    def test_the_frozen_spec_and_its_callables_stay_out_of_the_hashed_context(self):
        """Old behavior: the registry of callables sat in the context, so its repr (with memory addresses) went into the step hash and every run differed."""
        def deps():
            reg = registry()
            sp = spec({"name": "a", "do": "one", "expect": {"equals": 1}, "read_only": True}, goal="summarize")
            return {"predict_path": lambda ctx: (sp, reg)}
        r1 = rp.plan({"goal": "summarize"}, self.cond(), deps(), path_log=self.log)
        r2 = rp.plan({"goal": "summarize"}, self.cond(), deps(), path_log=self.log)
        self.assertEqual(r1["run"]["status"], "success")
        self.assertEqual(r1["run"]["result_hash"], r2["run"]["result_hash"])
        self.assertEqual([s["out_hash"] for s in r1["run"]["steps"]], [s["out_hash"] for s in r2["run"]["steps"]])
        self.assertEqual(r1["blueprint"]["prediction"]["status"], "frozen")

    def test_stage_18_needs_two_different_commitments(self):
        sp = spec({"name": "a", "do": "one", "expect": {"equals": 1}, "read_only": True}, goal="summarize")
        deps = lambda: {"predict_path": lambda ctx: (sp, registry()), "commitment_check": OK}
        step_only = rp.plan({"goal": "summarize"}, self.cond(), deps(), commitment="SIGNED-STEP", execute=True, path_log=self.log)
        self.assertEqual(step_only["run"]["status"], "success")
        self.assertTrue(step_only["execution"]["status"].startswith("refused: execution needs"))
        both = rp.plan({"goal": "summarize"}, self.cond(), deps(), commitment="SIGNED-STEP-2", spec_commitment="SIGNED-SPEC-2", execute=True, path_log=self.log)
        self.assertEqual(both["execution"]["status"], "completed")
        same = rp.plan({"goal": "summarize"}, self.cond(), deps(), commitment="SIGNED-ONE", spec_commitment="SIGNED-ONE", execute=True, path_log=self.log)
        self.assertIn("already used", same["execution"]["status"])
        neither = rp.plan({"goal": "summarize"}, self.cond(), deps(), execute=True, path_log=self.log)
        self.assertTrue(neither["run"]["status"].startswith("refused"))
        self.assertIsNone(neither["execution"])

    def test_without_a_path_log_nothing_is_frozen_and_the_blueprint_says_so(self):
        r = rp.plan({"goal": "summarize"}, self.cond(), {"predict_path": lambda ctx: ({}, {})})
        self.assertEqual(r["blueprint"]["prediction"]["status"], "NOT_BUILT")
        self.assertIn("prediction", r["blueprint"]["unbuilt"])


if __name__ == "__main__":
    unittest.main()
