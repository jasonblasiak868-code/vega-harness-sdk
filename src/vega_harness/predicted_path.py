"""predicted_path.py - pre-registered predictions, tripwires and a prepared parachute.

Idea: reasoning happens in a sandbox between two gates where nothing is executed, so no outside check exists yet. The honest substitute is a PRE-REGISTERED PREDICTION:
before the first effect, the expected path is written down and frozen (hash to a ledger). Reality is then compared to the frozen prediction, so a surprise can never be
re-explained as 'it fit all along'. Three outcomes per step:
  ON_PATH        the observation matches the expected descriptor: continue
  PREDICTED_ALT  it matches an alternative that was foreseen and has a prepared response: apply that response, continue
  OFF_PATH       anything else, or an exception anywhere (in the action, a check or a response): HALT (halt is not grant), run the step's prepared recovery, record an
                 incident, keep the state maintained, and queue a lesson. A controlled fall, not a free fall.
Fail closed before the jump: freeze() REFUSES a spec in which a step with side effects has no named recovery that exists in the registry (no parachute, no jump), a
descriptor that is unknown or ambiguous (exactly one test per descriptor), or a malformed spec. Running needs a valid human commitment for exactly THIS frozen spec
(its hash, not just its goal), and the commitment is burned: it cannot run the spec a second time.
calibration() reports how often frozen predictions held (Wilson lower bound). HONEST LIMIT: it counts whatever was frozen and run; easy specs raise it. Pass spec_hash to
look at one spec.

Descriptors (exactly one test each): {"equals": v} (type-strict: 1 is not True) {"contains": s} {"in_range": [lo, hi]} (numbers only, not bool) {"is_true": bool}
(observation must BE that bool) {"type": "dict_has", "keys": [...]} {"custom": "<name in registry['checks']>"}
Callables live in a registry passed to freeze() and run(), referenced by name. They run in this process with this process's rights."""
import hashlib
import json
import math

from vega_harness.records import Duplicate, RecordChain, RecordRefused, burn_commitment, canon

KNOWN = ("equals", "contains", "in_range", "is_true", "type", "custom")


class SpecRefused(ValueError):
    pass


def spec_hash(spec):
    try:
        return hashlib.sha256(canon(spec).encode("utf-8")).hexdigest()
    except RecordRefused as exc:
        raise SpecRefused(f"spec is not plain JSON data: {exc}") from exc


def _is_num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def matches(desc, obs, registry):
    if not isinstance(desc, dict):
        return False
    if "equals" in desc:
        want = desc["equals"]
        try:
            return type(obs) is type(want) and canon(obs) == canon(want)
        except RecordRefused:
            return False
    if "contains" in desc:
        needle = desc["contains"]
        if not isinstance(needle, str):
            return False
        try:
            return needle in (obs if isinstance(obs, str) else canon(obs))
        except RecordRefused:
            return False
    if "in_range" in desc:
        r = desc["in_range"]
        return isinstance(r, (list, tuple)) and len(r) == 2 and _is_num(r[0]) and _is_num(r[1]) and _is_num(obs) and r[0] <= obs <= r[1]
    if "is_true" in desc:
        return isinstance(desc["is_true"], bool) and obs is desc["is_true"]
    if desc.get("type") == "dict_has":
        keys = desc.get("keys", [])
        return isinstance(obs, dict) and isinstance(keys, list) and all(k in obs for k in keys)
    if "custom" in desc:
        return registry["checks"][desc["custom"]](obs) is True
    return False


def _tests_in(desc):
    return [k for k in KNOWN if k in desc and not (k == "type" and desc.get("type") != "dict_has")]


def validate(spec, registry):
    problems = []
    if not isinstance(spec, dict) or not isinstance(spec.get("steps"), list) or not spec["steps"]:
        raise SpecRefused("a spec is an object with a non-empty list of steps")
    if not isinstance(registry, dict) or not isinstance(registry.get("actions"), dict):
        raise SpecRefused("registry needs an 'actions' table")
    names = set()
    for s in spec["steps"]:
        if not isinstance(s, dict) or not isinstance(s.get("name"), str) or not s["name"]:
            problems.append("a step needs a name")
            continue
        if s["name"] in names:
            problems.append("duplicate step name %s" % s["name"])
        names.add(s["name"])
        if s.get("do") not in registry["actions"]:
            problems.append("step %s: action %r not in registry" % (s["name"], s.get("do")))
        alts = s.get("alternatives", [])
        if not isinstance(alts, list) or any(not isinstance(a, dict) or "match" not in a or "response" not in a or "name" not in a for a in alts):
            problems.append("step %s: alternatives need name, match and response" % s["name"])
            alts = []
        for d in [s.get("expect")] + [a["match"] for a in alts]:
            if not isinstance(d, dict) or len(_tests_in(d)) != 1:
                problems.append("step %s: a descriptor must use exactly one known test: %r" % (s["name"], d))
            elif "custom" in d and d["custom"] not in registry.get("checks", {}):
                problems.append("step %s: check %s not in registry" % (s["name"], d["custom"]))
        for a in alts:
            if a["response"] not in registry.get("responses", {}):
                problems.append("step %s: response %s not in registry" % (s["name"], a["response"]))
        if not s.get("read_only", False):
            r = s.get("recovery")
            if not r or r not in registry.get("recoveries", {}):
                problems.append("step %s has side effects and no prepared recovery (no parachute, no jump)" % s["name"])
    if problems:
        raise SpecRefused("; ".join(problems))


def freeze(spec, registry, log_path):
    """Validate (fail closed) and record the frozen prediction. Returns the spec hash."""
    validate(spec, registry)
    hh = spec_hash(spec)
    RecordChain(log_path).append("PATH_FROZEN", {"spec_hash": hh, "goal": str(spec.get("goal", "")), "steps": [s["name"] for s in spec["steps"]]})
    return hh


def _frozen(log, hh):
    return any(r.get("kind") == "PATH_FROZEN" and r.get("spec_hash") == hh for r in log.read())


def _digest(x):
    try:
        return hashlib.sha256(canon(x).encode()).hexdigest()
    except RecordRefused:
        return hashlib.sha256(repr(type(x)).encode()).hexdigest()


def run(spec, registry, log_path, state=None, commitment=None, commitment_check=None, burn=None):
    """Execute under the frozen prediction. Returns the outcome per step, the final status, and the incident if any.
    burn: the RecordChain where spent commitments are written. Give every gate in one deployment the SAME burn chain, or one signature could be spent once per chain."""
    log = RecordChain(log_path)
    hh = spec_hash(spec)
    if not _frozen(log, hh):
        return {"status": "refused: spec was not frozen first, or changed after freezing", "spec_hash": hh}
    validate(spec, registry)
    purpose = "execute:" + hh
    ok = False
    if commitment_check is not None and commitment is not None:
        try:
            ok = commitment_check(commitment, purpose) is True
        except Exception:  # reviewed: a checker that crashes does not accept the commitment
            ok = False
    if not ok:
        return {"status": "refused: execution needs a human-signed commitment for this exact spec", "spec_hash": hh}
    try:
        burn_commitment(burn or log, commitment, purpose)
    except Duplicate:
        return {"status": "refused: this commitment was already used", "spec_hash": hh}
    state = {} if state is None else state
    out = []
    for s in spec["steps"]:
        entry = {"step": s["name"]}
        obs, outcome = None, "OFF_PATH"
        try:
            obs = registry["actions"][s["do"]](state)
            if matches(s["expect"], obs, registry):
                outcome = "ON_PATH"
            else:
                for a in s.get("alternatives", []):
                    if matches(a["match"], obs, registry):
                        outcome = "PREDICTED_ALT"
                        entry["alternative"] = a["name"]
                        resp = registry["responses"][a["response"]](state, obs)
                        entry["response_result_hash"] = _digest(resp)
                        break
        except Exception as e:  # reviewed: ANY failure while acting, checking or responding is OFF_PATH: halt and recover, never crash past the parachute
            outcome, entry["exception"] = "OFF_PATH", type(e).__name__
        entry["outcome"] = outcome
        entry["obs_hash"] = _digest(obs)
        out.append(entry)
        log.append("STEP", {"spec_hash": hh, **entry})
        if outcome == "OFF_PATH":
            rec = None
            if s.get("recovery"):
                try:
                    rec = _digest(registry["recoveries"][s["recovery"]](state))
                except Exception as e:  # reviewed: a failed recovery is recorded as failed, the run is halted either way
                    rec = "RECOVERY_FAILED " + type(e).__name__
            inc = {"spec_hash": hh, "step": s["name"], "type": "unforeseen" if "exception" not in entry else "exception",
                   "recovery": s.get("recovery"), "recovery_result_hash": rec, "state": "contained: halted, state maintained", "lesson_queued": True}
            log.append("INCIDENT", inc)
            return {"status": "halted_off_path", "spec_hash": hh, "steps": out, "incident": inc}
    log.append("RUN_DONE", {"spec_hash": hh, "on_path": sum(1 for e in out if e["outcome"] == "ON_PATH"), "alt": sum(1 for e in out if e["outcome"] == "PREDICTED_ALT")})
    return {"status": "completed", "spec_hash": hh, "steps": out}


def wilson_lower(pos, n, z=1.96):
    if n == 0:
        return None
    p = pos / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return round((c - m) / d, 4)


def calibration(log_path, spec_hash=None):
    """Measured confidence in the frozen predictions: share of steps that were ON_PATH or a foreseen alternative."""
    rows = [r for r in RecordChain(log_path).read() if r.get("kind") == "STEP" and (spec_hash is None or r.get("spec_hash") == spec_hash)]
    hit = sum(1 for r in rows if r["outcome"] in ("ON_PATH", "PREDICTED_ALT"))
    return {"steps": len(rows), "held": hit, "off_path": len(rows) - hit, "wilson_lower": wilson_lower(hit, len(rows))}
