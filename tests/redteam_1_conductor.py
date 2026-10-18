"""RED TEAM PASS 1 (harness, Conductor): dodging the human gate, replaying a signature, lying verifiers, a poisoned replay baseline, hostile inputs."""
import os
import pathlib
import tempfile
import threading
import time
import unittest

from vega_custody.ledger.backend_local import LocalBackend
from vega_custody.ledger.ledger import Ledger
from vega_harness import conductor as c
from vega_harness.records import RecordChain, make_ledger_check

OK = lambda commitment, action: isinstance(commitment, str) and commitment.startswith("SIGNED")
answer = lambda ctx: {"answer": 42}
verify = lambda ctx: ctx.get("answer") == 42


class Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.runs, self.reg = (os.path.join(self.d.name, n) for n in ("runs.jsonl", "reg.jsonl"))

    def tearDown(self):
        self.d.cleanup()

    def cond(self, **kw):
        kw.setdefault("commitment_check", OK)
        return c.Conductor(self.runs, self.reg, **kw)


class HumanGate(Base):
    def test_a_step_cannot_dodge_the_gate_by_declaring_itself_ordinary(self):
        """Attack: a step that performs a human-only action but passes human_only=False. Its verbs name the action, so the Conductor gates it anyway."""
        for verb in ("release_public", "Release-Public", " RELEASE PUBLIC ", "spend_funds", "mint_commitment"):
            ran = []
            step = c.Step("publish_it", lambda ctx: ran.append(1) or {"answer": 42}, verbs=[verb], human_only=False)
            self.assertTrue(step.human_only, verb)
            r = self.cond().run("t", {}, [step], verify)
            self.assertTrue(r["status"].startswith("refused"), verb)
            self.assertEqual(ran, [])

    def test_a_step_named_for_a_human_only_action_is_gated_too(self):
        self.assertTrue(c.Step("release_public", answer).human_only)

    def test_one_signature_authorises_one_step_once(self):
        steps = [c.Step("a", answer, human_only=True)]
        self.assertEqual(self.cond().run("t1", {}, steps, verify, commitment="SIGNED-1")["status"], "success")
        again = self.cond().run("t2", {}, steps, verify, commitment="SIGNED-1")
        self.assertTrue(again["status"].startswith("refused"))
        self.assertTrue(any(g.get("commitment_reuse") == "refused" for s in again["steps"] for g in s["gates"]))

    def test_one_signature_cannot_open_two_human_only_steps_in_one_run(self):
        steps = [c.Step("a", lambda ctx: {}, human_only=True), c.Step("b", answer, human_only=True)]
        r = self.cond().run("t", {}, steps, verify, commitment="SIGNED-2")
        self.assertTrue(r["status"].startswith("refused: step 'b'"))

    def test_a_checker_that_says_yes_loosely_or_crashes_does_not_open_the_gate(self):
        steps = [c.Step("a", answer, human_only=True)]
        for chk in (lambda cm, a: "yes", lambda cm, a: 1, lambda cm, a: {"ok": True}, lambda cm, a: None, lambda cm, a: 1 / 0):
            r = self.cond(commitment_check=chk).run("t", {}, steps, verify, commitment="SIGNED-X")
            self.assertTrue(r["status"].startswith("refused"))
        self.assertTrue(self.cond(commitment_check=None).run("t", {}, steps, verify, commitment="SIGNED-X")["status"].startswith("refused"))
        self.assertTrue(self.cond().run("t", {}, steps, verify, commitment=None)["status"].startswith("refused"))

    def test_the_checker_is_asked_about_exactly_this_step(self):
        asked = []
        self.cond(commitment_check=lambda cm, a: asked.append(a) or True).run("t", {}, [c.Step("only_this", answer, human_only=True)], verify, commitment="SIGNED-Q")
        self.assertEqual(asked, ["conductor_step:only_this"])

    def test_a_real_ledger_commitment_for_exactly_this_action(self):
        t = pathlib.Path(self.d.name)
        led = Ledger([LocalBackend(t / "led.jsonl")])
        commit = lambda action, n, exp=None: {"kind": "WCOMMIT", "device": "d", "surface": "s", "action": action, "action_hash": f"{n:064x}", "expiry": exp or int(time.time()) + 300, "nonce_sha256": f"{n + 99:064x}"}
        good, wrong, expired, forged = (commit("conductor_step:a", 1), commit("conductor_step:other", 2), commit("conductor_step:a", 3, exp=1), commit("conductor_step:a", 4))
        for x in (good, wrong, expired):
            led.mint(x)
        chk = make_ledger_check(led)
        steps = [c.Step("a", answer, human_only=True)]
        self.assertEqual(self.cond(commitment_check=chk).run("ok", {}, steps, verify, commitment=good)["status"], "success")
        for bad in (wrong, expired, forged, "text", None, {"kind": "WCOMMIT"}):
            self.assertTrue(self.cond(commitment_check=chk).run("bad", {}, steps, verify, commitment=bad)["status"].startswith("refused"), repr(bad)[:40])


class LyingVerifiers(Base):
    def test_verify_must_return_exactly_true(self):
        for v in ("yes", {"ok": False}, 1, [0], "False"):
            self.assertEqual(self.cond().run("t", {}, [c.Step("s", answer)], lambda ctx, v=v: v)["status"], "failed: verification", repr(v))
        self.assertEqual(self.cond().run("t", {}, [c.Step("s", answer)], lambda ctx: 1 / 0)["status"], "failed: verification")
        self.assertEqual(len(c.read(self.reg)), 0)

    def test_second_opinion_must_say_agree_exactly_true_and_must_not_crash(self):
        for so in (lambda ctx: {"agree": "no"}, lambda ctx: {"agree": 1}, lambda ctx: {}, lambda ctx: None, lambda ctx: 1 / 0, lambda ctx: "agree"):
            r = self.cond().run("t", {}, [c.Step("s", answer)], verify, so)
            self.assertEqual(r["status"], "failed: second opinion disagreed")
        self.assertEqual(len(c.read(self.reg)), 0)


class Preflight(Base):
    def test_a_failing_or_malformed_preflight_stops_the_run_instead_of_skipping_the_check(self):
        for pf in (lambda verbs: 1 / 0, lambda verbs: "lessons", lambda verbs: [{"no_id": 1}], lambda verbs: None):
            ran = []
            r = self.cond(preflight=pf).run("t", {}, [c.Step("s", lambda ctx: ran.append(1) or {"answer": 42}, verbs=["overwrite"])], verify)
            self.assertTrue(r["status"].startswith("stopped: lesson preflight failed"))
            self.assertEqual(ran, [])

    def test_preflight_is_called_once_per_step_not_twice(self):
        calls = []
        self.cond(preflight=lambda verbs: calls.append(verbs) or []).run("t", {}, [c.Step("s", answer, verbs=["overwrite"])], verify)
        self.assertEqual(len(calls), 1)

    def test_lessons_may_arrive_as_ranked_pairs_as_the_ledger_returns_them(self):
        r = self.cond(preflight=lambda verbs: [(0.9, {"id": "L9", "protect": "x"})]).run("t", {}, [c.Step("s", answer, verbs=["overwrite"])], verify)
        self.assertEqual(r["steps"][0]["preflight"], ["L9"])


class HostileInputs(Base):
    def test_non_json_data_is_refused_not_hashed_as_a_memory_address(self):
        with self.assertRaises(c.ConductorRefused):
            self.cond().run("t", {"x": object()}, [c.Step("s", answer)], verify)
        with self.assertRaises(c.ConductorRefused):
            self.cond().run("t", {"x": float("nan")}, [c.Step("s", answer)], verify)
        r = self.cond().run("t", {}, [c.Step("s", lambda ctx: {"x": {1, 2}})], verify)
        self.assertTrue(r["status"].startswith("failed: step 's'"))
        r = self.cond().run("t", {}, [c.Step("s", lambda ctx: {"f": (lambda: 0)})], verify)
        self.assertTrue(r["status"].startswith("failed: step 's'"))

    def test_a_step_that_returns_a_list_fails_cleanly(self):
        r = self.cond().run("t", {}, [c.Step("s", lambda ctx: [1, 2, 3])], verify)
        self.assertTrue(r["status"].startswith("failed: step 's' raised TypeError"))

    def test_token_cap_zero_does_not_mean_unlimited(self):
        for cap in (0, -1, True, "5", 2.5):
            with self.assertRaises(c.ConductorRefused, msg=repr(cap)):
                self.cond(token_cap=cap)
        self.assertIsNotNone(self.cond(token_cap=1000))

    def test_malformed_steps_and_names(self):
        for bad in (None, [], "x", [c], [1], ["s"]):
            with self.assertRaises(c.ConductorRefused, msg=repr(bad)):
                self.cond().run("t", {}, bad, verify)
        with self.assertRaises(c.ConductorRefused):
            self.cond().run("t", {}, [c.Step("same", answer), c.Step("same", answer)], verify)
        for args in (("", answer), (5, answer), ("n", "notcallable"), ("n", answer, "overwrite"), ("n", answer, [5])):
            with self.assertRaises(c.ConductorRefused, msg=repr(args)):
                c.Step(*args)
        for task in ("", None, 5):
            with self.assertRaises(c.ConductorRefused):
                self.cond().run(task, {}, [c.Step("s", answer)], verify)

    def test_a_credential_shaped_task_id_is_refused_by_the_chain(self):
        with self.assertRaises(Exception):
            self.cond().run("deploy with sk-" + "a1B2c3D4" * 4, {}, [c.Step("s", answer)], verify)
        self.assertEqual(c.read(self.runs), [])

    def test_the_run_record_holds_hashes_not_values(self):
        self.cond().run("t", {"note": "private value 12345"}, [c.Step("s", lambda ctx: {"answer": 42, "out": "another private value"})], verify)
        text = pathlib.Path(self.runs).read_text(encoding="utf-8")
        self.assertNotIn("private value", text)
        self.assertNotIn("another private", text)


class ReplayBaseline(Base):
    def steps(self, noise=None):
        return [c.Step("compute", (lambda ctx: {"answer": 42, "noise": noise}) if noise is not None else answer)]

    def test_a_replay_that_does_not_reproduce_never_becomes_the_baseline(self):
        """The old code registered a new candidate on EVERY successful run, replays included, then compared the next replay with the LAST candidate. Two drifted replays in a
        row therefore locked a workflow that never reproduced. A replay does not register, and the baseline is the FIRST candidate."""
        cd = self.cond()
        cd.run("t", {}, self.steps(), verify)
        drifted = [c.Step("compute", lambda ctx: {"answer": 42, "noise": 1})]
        # same name/verbs but a different function: a different workflow id, so a drifted replay is refused outright
        with self.assertRaises(KeyError):
            cd.replay("t", {}, drifted, verify)
        # the same function with a value that changes between runs is the real drift case
        counter = {"n": 0}

        def flaky(ctx):
            counter["n"] += 1
            return {"answer": 42, "noise": counter["n"]}
        flaky_steps = [c.Step("compute", flaky)]
        cd2 = self.cond()
        cd2.run("f", {}, flaky_steps, verify)                                   # baseline noise=1
        self.assertFalse(cd2.replay("f", {}, flaky_steps, verify))              # noise=2: differs
        self.assertFalse(cd2.replay("f", {}, flaky_steps, verify))              # noise=3: still differs from the FIRST baseline
        self.assertEqual(c.locked_workflows(self.reg), [])
        self.assertEqual(len([r for r in c.read(self.reg) if r["kind"] == "WORKFLOW_CANDIDATE_PRE" and r["workflow_id"] == c.workflow_id(flaky_steps)]), 1)

    def test_a_workflow_whose_own_runs_disagree_is_not_locked_by_matching_the_later_one(self):
        results = iter([1, 2, 2])
        steps = [c.Step("compute", lambda ctx: {"answer": 42, "v": next(results)})]
        cd = self.cond()
        cd.run("a", {}, steps, verify)                                          # candidate with v=1
        cd.run("b", {}, steps, verify)                                          # a second candidate with v=2: the workflow is not deterministic
        self.assertFalse(cd.replay("c", {}, steps, verify))                     # v=2 matches the LAST candidate, not the first
        self.assertEqual(c.locked_workflows(self.reg), [])

    def test_a_reproducing_replay_locks_and_a_locked_workflow_cannot_be_replayed_again(self):
        cd = self.cond()
        cd.run("t", {}, self.steps(), verify)
        self.assertTrue(cd.replay("t", {}, self.steps(), verify))
        self.assertEqual(len(c.locked_workflows(self.reg)), 1)
        with self.assertRaises(c.ConductorRefused):
            cd.replay("t", {}, self.steps(), verify)

    def test_a_different_function_under_the_same_name_is_a_different_workflow(self):
        a = [c.Step("compute", lambda ctx: {"answer": 42})]
        b = [c.Step("compute", lambda ctx: {"answer": 42, "extra": "same output, other code"})]
        self.assertNotEqual(c.workflow_id(a), c.workflow_id(b))
        self.cond().run("t", {}, a, verify)
        with self.assertRaises(KeyError):
            self.cond().replay("t", {}, b, verify)

    def test_replaying_an_unregistered_workflow_is_refused(self):
        with self.assertRaises(KeyError):
            self.cond().replay("t", {}, self.steps(), verify)


class ChainProperties(Base):
    def test_a_cut_off_tail_is_caught_with_a_remembered_head(self):
        cd = self.cond()
        cd.run("a", {}, self.steps(), verify)
        last = cd.run("b", {}, self.steps(), verify)
        lines = pathlib.Path(self.runs).read_text(encoding="utf-8").splitlines()
        pathlib.Path(self.runs).write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
        self.assertEqual(c.verify_chain(self.runs), (True, None))
        self.assertFalse(c.verify_chain(self.runs, expected_head=last["hash"])[0])

    def steps(self):
        return [c.Step("s", answer)]

    def test_eight_conductors_running_at_once_all_record_and_the_chain_stays_valid(self):
        errs = []

        def go(n):
            for i in range(3):
                try:
                    self.cond().run(f"t{n}-{i}", {}, self.steps(), verify)
                except Exception as exc:
                    errs.append(repr(exc))
        ts = [threading.Thread(target=go, args=(n,)) for n in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(errs, [])
        self.assertEqual(len([r for r in c.read(self.runs) if r["kind"] == "RUN"]), 24)
        self.assertEqual(c.verify_chain(self.runs), (True, None))

    def test_a_commitment_burned_by_one_of_two_racing_runs_is_refused_to_the_other(self):
        results = []
        steps = [c.Step("a", answer, human_only=True)]

        def go():
            results.append(self.cond().run("race", {}, steps, verify, commitment="SIGNED-RACE")["status"])
        ts = [threading.Thread(target=go) for _ in range(4)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(results.count("success"), 1)
        self.assertEqual(sum(1 for r in results if r.startswith("refused")), 3)


if __name__ == "__main__":
    unittest.main()
