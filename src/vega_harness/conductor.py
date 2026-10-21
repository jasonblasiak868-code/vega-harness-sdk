"""conductor.py - the deterministic Conductor: runs a workflow step by step, gates it, records it, and registers it.

NON-GENERATIVE by design: this file contains no model call and no LLM reasoning. Models are only ever used INSIDE a step the caller supplies (a retrieval step, an
arsenal step, a council second opinion). The Conductor only sequences, gates, measures and records.

For every run it:
  1. consults the lesson ledger (preflight) once per risky verb a step declares, and records what it surfaced. A preflight that fails stops the run: a
     skipped lesson check is never silent;
  2. refuses any HUMAN_ONLY step unless a valid commitment for exactly that step is presented. A step is human-only if it says so OR if any verb it declares is a
     HUMAN_ONLY action (a step cannot dodge the gate by declaring itself ordinary). The commitment is burned: the same signature never authorises a second step or run;
  3. measures estimated tokens (chars/4) entering each step and stops if the cap is exceeded. The cap is checked BEFORE a human-only step's commitment is burned, so
     a run stopped by the cap never spends a signature on a step that did not run. The cap is the tighter of the Conductor's own and the one given to run();
  4. runs the steps in order, hashing every step's inputs and outputs (values must be plain JSON data: no object reprs with memory addresses in a hash);
  5. verifies the result with a PROGRAMMATIC check that must return exactly True (a truthy string or dict does not count), and asks for a second opinion when one is
     supplied: it must say agree is exactly True, and a crash or disagreement blocks success;
  6. appends a run record to a custody chain (hash-chained, replica-checked, hashes only);
  7. on success registers the workflow as a CANDIDATE PRE. replay() re-runs it WITHOUT registering a new candidate and LOCKS it only if the result hash equals the FIRST
     candidate's: a non-reproducing replay can never become the thing the next replay is compared against.
A step's identity includes a hash of its code AND of the values it captured from its surroundings and its default arguments (records.code_hash), so a different
function, or the same function closing over a different value, is a different workflow. (Bytecode differs between Python versions, so a lock made on one version does
not match on another; that fails safe. A captured object that is not plain data is identified by its type only.)"""
import time

from vega_harness.records import Duplicate, RecordChain, RecordRefused, burn_commitment, canon, code_hash, h
from vega_jacket import humanauth

MAX_STEPS = 200


class ConductorRefused(ValueError):
    pass


def est_tokens(o):
    return max(1, len(canon(o)) // 4)


class Step:
    def __init__(self, name, fn, verbs=(), human_only=False):
        if not isinstance(name, str) or not name.strip() or not callable(fn):
            raise ConductorRefused("a step needs a name and a callable")
        if isinstance(verbs, (str, bytes)) or any(not isinstance(v, str) for v in verbs):
            raise ConductorRefused("verbs must be a list of text")
        self.name, self.fn, self.verbs = name, fn, tuple(verbs)
        self.human_only = bool(human_only) or humanauth.is_human_only(name) or any(humanauth.is_human_only(v) for v in self.verbs)

    def ident(self):
        return {"name": self.name, "verbs": list(self.verbs), "human_only": self.human_only, "code": code_hash(self.fn)}


def workflow_id(steps):
    return h([s.ident() for s in steps])


def read(path):
    return RecordChain(path).read()


def verify_chain(path, expected_head=None, min_length=None):
    return RecordChain(path).verify(expected_head=expected_head, min_length=min_length)


def locked_workflows(registry_path):
    return sorted({r["workflow_id"] for r in read(registry_path) if r.get("status") == "locked"})


class Conductor:
    def __init__(self, runs_path, registry_path, preflight=None, commitment_check=None, token_cap=None):
        """preflight: fn(verbs) -> [{"id","title","protect"}] (a lesson ledger). commitment_check: fn(commitment, action) -> bool, where action is
        'conductor_step:<step name>' (see records.make_ledger_check). token_cap: None for no cap, else a positive whole number."""
        if token_cap is not None and (isinstance(token_cap, bool) or not isinstance(token_cap, int) or token_cap <= 0):
            raise ConductorRefused("token_cap is None (no cap) or a positive whole number; 0 would silently mean 'unlimited'")
        self.runs, self.registry = RecordChain(runs_path), RecordChain(registry_path)
        self.preflight, self.commitment_check, self.token_cap = preflight, commitment_check, token_cap

    def _validate(self, task_id, ctx, steps):
        if not isinstance(task_id, str) or not task_id:
            raise ConductorRefused("task_id must be text")
        if not isinstance(steps, (list, tuple)) or not steps or len(steps) > MAX_STEPS or any(not isinstance(s, Step) for s in steps):
            raise ConductorRefused(f"steps must be 1..{MAX_STEPS} Step objects")
        names = [s.name for s in steps]
        if len(set(names)) != len(names):
            raise ConductorRefused("step names must be unique")
        if not isinstance(ctx, dict):
            raise ConductorRefused("ctx must be an object")
        try:
            canon(ctx)
        except RecordRefused as exc:
            raise ConductorRefused(f"ctx must be plain JSON data: {exc}") from exc

    def run(self, task_id, ctx, steps, verify, second_opinion=None, commitment=None, register=True, token_cap=None):
        self._validate(task_id, ctx, steps)
        if token_cap is not None and (isinstance(token_cap, bool) or not isinstance(token_cap, int) or token_cap <= 0):
            raise ConductorRefused("token_cap is None (no cap) or a positive whole number; 0 would silently mean 'unlimited'")
        caps = [x for x in (self.token_cap, token_cap) if x is not None]
        cap = min(caps) if caps else None
        ctx = dict(ctx)
        rec = {"task": task_id, "workflow_id": workflow_id(steps), "steps": [], "tokens_est": 0, "status": None}
        for s in steps:
            entry = {"step": s.name, "in_hash": h(ctx), "tokens_in": est_tokens(ctx), "preflight": [], "gates": []}
            if s.verbs and self.preflight:
                try:
                    lessons = self.preflight(s.verbs)
                    if not isinstance(lessons, (list, tuple)):
                        raise TypeError("preflight must return a list")
                    entry["preflight"] = [str(x[1]["id"] if isinstance(x, tuple) else x["id"]) for x in lessons]
                    ctx["_preflight_" + s.name] = [(x[1] if isinstance(x, tuple) else x).get("protect") for x in lessons]
                except Exception as exc:  # reviewed: a lesson check that fails stops the run (fail closed); it is never skipped silently
                    entry["result"] = "STOPPED_PREFLIGHT_FAILED " + type(exc).__name__
                    rec["steps"].append(entry)
                    rec["status"] = "stopped: lesson preflight failed for step '%s'" % s.name
                    return self._close(rec)
            if cap is not None and rec["tokens_est"] + entry["tokens_in"] > cap:       # checked BEFORE any commitment is burned
                entry["gates"].append({"token_cap": False})
                entry["result"] = "STOPPED_TOKEN_CAP"
                rec["steps"].append(entry)
                rec["status"] = "stopped: token cap %d exceeded" % cap
                return self._close(rec)
            if s.human_only:
                ok, cid = False, None
                if self.commitment_check and commitment is not None:
                    try:
                        ok = self.commitment_check(commitment, "conductor_step:" + s.name) is True
                    except Exception:  # reviewed: a checker that crashes does not accept the commitment
                        ok = False
                    if ok:
                        try:
                            cid = burn_commitment(self.runs, commitment, "conductor_step:" + s.name)
                        except Duplicate:
                            ok = False
                            entry["gates"].append({"commitment_reuse": "refused"})
                        except RecordRefused:
                            ok = False
                entry["gates"].append({"commitment": ok, "commitment_id": cid})
                if not ok:
                    entry["result"] = "REFUSED_NEEDS_HUMAN_COMMITMENT"
                    rec["steps"].append(entry)
                    rec["status"] = "refused: step '%s' needs a human-signed commitment" % s.name
                    return self._close(rec)
            rec["tokens_est"] += entry["tokens_in"]
            try:
                out = s.fn(ctx)
                out = {} if out is None else out
                if not isinstance(out, dict):
                    raise TypeError("a step returns an object (or nothing)")
                entry["out_hash"] = h(out)
            except Exception as e:
                entry["result"] = "ERROR " + type(e).__name__
                rec["steps"].append(entry)
                rec["status"] = "failed: step '%s' raised %s" % (s.name, type(e).__name__)
                return self._close(rec)
            ctx.update(out)
            entry["result"] = "ok"
            rec["steps"].append(entry)
        try:
            ok_v = verify(ctx) is True
        except Exception:  # reviewed: a verifier that crashes has not verified anything
            ok_v = False
        rec["verify"] = ok_v
        agree = True
        if second_opinion is not None:
            try:
                so = second_opinion(ctx) or {}
                agree = so.get("agree") is True
                rec["second_opinion"] = {"agree": agree, "notes_hash": h(so.get("notes", ""))}
            except Exception as exc:  # reviewed: a council that crashes or returns junk blocks success
                agree = False
                rec["second_opinion"] = {"agree": False, "error": type(exc).__name__}
        rec["result_hash"] = h({k: v for k, v in ctx.items() if not k.startswith("_")})
        rec["status"] = "success" if ok_v and agree else ("failed: verification" if not ok_v else "failed: second opinion disagreed")
        out = self._close(rec)
        if rec["status"] == "success" and register:
            self._register(rec)
        return out

    def _close(self, rec):
        return self.runs.append("RUN", {**rec, "t": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})

    def _register(self, rec):
        return self.registry.append("WORKFLOW_CANDIDATE_PRE", {"workflow_id": rec["workflow_id"], "task": rec["task"], "result_hash": rec["result_hash"],
                                                              "tokens_est": rec["tokens_est"], "status": "candidate"})

    def replay(self, task_id, ctx, steps, verify, second_opinion=None, commitment=None, token_cap=None):
        """Re-run a registered workflow. It is LOCKED (a reusable PRE) only if the result hash equals the FIRST registered candidate's, exactly. A replay never
        registers a new candidate, so a run that does not reproduce cannot become the baseline for the next attempt."""
        wid = workflow_id(steps)
        rows = read(self.registry.paths[0])
        if any(r.get("kind") == "WORKFLOW_REPLAY" and r["workflow_id"] == wid and r.get("status") == "locked" for r in rows):
            raise ConductorRefused("this workflow is already locked")
        prior = [r for r in rows if r.get("kind") == "WORKFLOW_CANDIDATE_PRE" and r["workflow_id"] == wid]
        if not prior:
            raise KeyError("no candidate workflow registered for these steps")
        want = prior[0]["result_hash"]
        run = self.run(task_id + "#replay", ctx, steps, verify, second_opinion, commitment, register=False, token_cap=token_cap)
        reproduced = run["status"] == "success" and run["result_hash"] == want
        self.registry.append("WORKFLOW_REPLAY", {"workflow_id": wid, "reproduced": reproduced, "status": "locked" if reproduced else "failed_replay",
                                                 "result_hash": run.get("result_hash"), "baseline_hash": want})
        return reproduced
