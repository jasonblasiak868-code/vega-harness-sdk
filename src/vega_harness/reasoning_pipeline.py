"""reasoning_pipeline.py - the pre-execution reasoning pipeline on the deterministic Conductor.

Default path = OPTION 2: collect the situation, mine memory/lessons/library for what is already known, build a cache-stable prompt prefix, scan the repo library and the
per-symbol scores for a fit or a combination, challenge EVERY candidate, decide whether a PRE/PEAK is needed, freeze a predicted path, assemble a blueprint, and hand it to
the Conductor. OPTION 1 (brute force) exists only when the caller passes a token cap and allow_brute_force.

HONEST STATUS: stages whose dependency is not supplied return {"status": "NOT_BUILT"} and the blueprint says so; nothing pretends. Stage 18 (execute) is a HUMAN_ONLY step:
the Conductor refuses it without a valid single-use commitment for that step, and running the frozen path needs a SECOND commitment for the exact spec. Autonomy never mints one.
The blueprint is a plan, not a result.

Every stage is a plain callable injected through `deps`, so each real component plugs in without changing the sequencing, and tests run with stubs. Runtime objects (the frozen
spec and its registry of callables) are kept OUT of the hashed context: a hash of a callable's repr would contain a memory address and differ on every run.
The lesson preflight is wired: the risky verbs found in the request are attached to the steps that act on them, so the Conductor consults the lesson ledger before they run."""
import hashlib
import re

from vega_harness import conductor as cd
from vega_harness import predicted_path as pp
from vega_harness import use_gate as ug
from vega_harness.records import canon

NOT_BUILT = {"status": "NOT_BUILT"}
# DESIGN RULE: a match is a candidate, not a stop. Stopping at the first fit can miss a better one and can hide a failure that only shows later; matching is not verifying.
POLICY_PREFIX = ("VEGA pre-execution reasoning. Rules: human commitment before any effect; unbacked is not false; quote, never recall; "
                 "contained use for library code; public builds are clean-room only.")
RISKY = ("delete", "overwrite", "rotate", "push", "publish", "install", "execute", "run", "send", "pay")
_IRREGULAR = {"pay": ("paid",), "send": ("sent",), "run": ("ran",)}


def _forms(v):
    f = {v, v + "s", v + "es", v + "ed", v + "d", v + "ing", v + v[-1] + "ing", v + v[-1] + "ed", *_IRREGULAR.get(v, ())}
    if v.endswith("e"):
        f.add(v[:-1] + "ing")
    return sorted(f, key=len, reverse=True)


_VERB_RX = {v: re.compile(r"\b(?:%s)\b" % "|".join(re.escape(x) for x in _forms(v)), re.I) for v in RISKY}


class RequestRefused(ValueError):
    pass


def risky_verbs(text):
    return [v for v, rx in _VERB_RX.items() if rx.search(text)]


def stable_prefix(goal, node_ids):
    """Same inputs -> byte-identical prefix, so every later step can hit the prompt cache. The variable part always goes AFTER it."""
    body = POLICY_PREFIX + "\nGOAL: " + goal.strip() + "\nANCHORS: " + ",".join(sorted(set(map(str, node_ids))))
    return body, hashlib.sha256(body.encode("utf-8")).hexdigest()


def prompt_layers(layers):
    """APPEND-ONLY prompt assembly for cache hits. layers: ordered list of (name, text). Each layer is appended after the previous ones and never reordered or edited, so the
    byte prefix up to layer k is identical for every later call; prefix_hash (full sha256) proves it. Most stable layers first, most volatile last."""
    out, acc = [], ""
    for name, text in layers:
        acc += "\n## " + str(name) + "\n" + str(text)
        out.append({"name": name, "prefix_hash": hashlib.sha256(acc.encode("utf-8")).hexdigest(), "prefix_chars": len(acc)})
    return {"layers": out, "text": acc}


def compact(c):
    """Short reference to a candidate. The full record already sits in blueprint['library']; repeating it costs tokens."""
    if not isinstance(c, dict):
        return {"ref": str(c)}
    out = {k: c[k] for k in ("repo", "workflow", "kind", "license_class", "fixes", "score") if k in c}
    if isinstance(c.get("weak_symbol"), dict) and isinstance(c.get("strong_symbol"), dict):
        out.update({"weak": c["weak_symbol"].get("name"), "strong": c["strong_symbol"].get("name")})
    return out


def choose_path(known_precedent, token_cap=None, allow_brute_force=False):
    if allow_brute_force and token_cap and not known_precedent:
        return {"option": 1, "why": "caller allowed brute force under a hard token cap and there is no precedent", "token_cap": token_cap}
    return {"option": 2, "why": "default: research first, spend tokens on a reasoned blueprint" + ("" if known_precedent else " (no precedent found: abstaining on prior knowledge)")}


def _score(v):
    s = v.get("score", 0) if isinstance(v, dict) else 0
    return s if isinstance(s, (int, float)) and not isinstance(s, bool) and s == s else 0


def validate_request(request):
    if not isinstance(request, dict) or not isinstance(request.get("goal"), str) or not request["goal"].strip():
        raise RequestRefused("a request is an object with a non-empty text goal")
    for k in ("prompt", "required_response"):
        if k in request and not isinstance(request[k], str):
            raise RequestRefused(f"{k} must be text")
    tc = request.get("token_cap")
    if tc is not None and (isinstance(tc, bool) or not isinstance(tc, int) or tc <= 0):
        raise RequestRefused("token_cap is a positive whole number or absent")


def build_steps(deps, request, sink=None, runtime=None, path_log=None, spec_commitment=None, burn=None):
    d = deps
    sink = sink if sink is not None else []
    runtime = runtime if runtime is not None else {}
    verbs = risky_verbs(request["goal"] + " " + request.get("prompt", ""))

    def s01(ctx):
        return {"request": {"goal": request["goal"], "prompt": request.get("prompt", ""), "required_response": request.get("required_response", ""), "verbs": verbs}}

    def s02_prior(ctx):
        r = ctx["request"]
        mem = d["recall"](r["goal"] + " " + r.get("prompt", "")) if "recall" in d else []
        prec = d["precedents"](r["goal"]) if "precedents" in d else []
        return {"prior": {"memory": mem, "precedents": prec}, "known_precedent": bool(mem or prec)}

    def s03_assoc(ctx):
        return {"associated": d["associate"](ctx["request"]["goal"]) if "associate" in d else []}

    def s04_reconstruct(ctx):
        return {"prompt_reconstruction": d["reconstruct"](ctx) if "reconstruct" in d else dict(NOT_BUILT)}

    def s05_prefix(ctx):
        ids = [a.get("id", a.get("repo", "")) if isinstance(a, dict) else str(a) for a in ctx.get("associated", [])]
        text, hh = stable_prefix(ctx["request"]["goal"], ids)
        return {"cache_prefix": {"hash": hh, "chars": len(text)}}

    def s06_history(ctx):
        return {"history": d["history"](ctx["request"]["goal"]) if "history" in d else dict(NOT_BUILT)}

    def s07_kinetic(ctx):
        return {"lattice_prediction": d["kinetic"](ctx) if "kinetic" in d else dict(NOT_BUILT)}

    def s09_route(ctx):
        return {"route": d["route"](ctx) if "route" in d else {"status": "partial", "stations": ["retrieval", "library_scan", "containment_check", "blueprint"]}}

    def s10_library(ctx):
        fits = d["library_fit"](ctx["request"]["goal"]) if "library_fit" in d else []
        combos = [] if fits else (d["combine"](ctx["request"]["goal"]) if "combine" in d else [])
        cards = [ug.use_card(f["repo"], d.get("use_matrix")) for f in fits[:3] if isinstance(f, dict) and "repo" in f] if fits and d.get("show_use_cards") else []
        return {"library": {"fits": fits, "combination_hypotheses": combos, "combination_status": "hypothesis only; needs a contained run" if combos else "none", "use_cards": cards}}

    def s10b_challenge(ctx):
        """A match is a CANDIDATE, never a stop. Collect every candidate (library fits, combination hypotheses, a locked workflow that matches), verify EACH through
        deps['verify_candidate'], rank by the verification, keep the rejected with reasons, and ask what could break downstream through deps['downstream_risk']."""
        cands = list(ctx["library"]["fits"]) + list(ctx["library"]["combination_hypotheses"])
        if "locked_match" in d:
            lm_ = d["locked_match"](ctx["request"]["goal"])
            if lm_:
                cands.append({"workflow": lm_, "kind": "locked_workflow"})
        extra = d["more_candidates"](ctx["request"]["goal"]) if "more_candidates" in d else []
        cands += [c for c in extra if c not in cands]
        ver = d.get("verify_candidate")
        rows = []
        for c in cands:
            v = ver(c) if ver else {"status": "NOT_BUILT"}
            risk = d["downstream_risk"](c) if "downstream_risk" in d else {"status": "NOT_BUILT"}
            rows.append({"candidate": compact(c), "verification": v, "downstream_risk": risk, "score": _score(v)})
        rows.sort(key=lambda r: -r["score"])
        ok = [r for r in rows if isinstance(r["verification"], dict) and r["verification"].get("passes") is True]
        return {"challenge": {"considered": len(rows), "stopped_at_first_match": False, "ranked": rows[:5], "rejected": [r for r in rows if r not in ok][:5],
                              "chosen": ok[0] if ok else None,
                              "note": "verification status NOT_BUILT means nothing was verified: do not trust a pick" if rows and not ver else ""}}

    def s11_containment(ctx):
        classes = [f.get("license_class", "UNKNOWN") for f in ctx["library"]["fits"] if isinstance(f, dict)]
        classes += [c.get("strictest_license_class", "UNKNOWN") for c in ctx["library"]["combination_hypotheses"] if isinstance(c, dict)]
        need = bool(ctx["request"]["verbs"]) or any(c != "PERMISSIVE" for c in classes)
        return {"containment": {"need_pre_peak": need, "reason": "risky verbs or non-permissive source" if need else "none",
                                "plan": ["eval_sandbox PRE (disposable, no network)", "behavior_probe PEAK after success"] if need else []}}

    def s12_arsenal(ctx):
        return {"arsenal": d["arsenal"](ctx["request"]["goal"]) if "arsenal" in d else dict(NOT_BUILT)}

    def s13_hybrid(ctx):
        if "hybrid" in d:
            return {"hybrid": d["hybrid"](ctx)}
        return {"hybrid": {"status": "NOT_BUILT", "inputs_available": {"library_path": bool(ctx["library"]["fits"] or ctx["library"]["combination_hypotheses"]),
                                                                     "arsenal_path": ctx["arsenal"].get("status") != "NOT_BUILT"}}}

    def s14_meta(ctx):
        keys = ["prompt_reconstruction", "lattice_prediction", "history", "arsenal", "hybrid"]
        nb = [k for k in keys if isinstance(ctx.get(k), dict) and ctx[k].get("status") == "NOT_BUILT"]
        return {"meta_score": {"stages_not_built": nb, "completeness": round(1 - len(nb) / len(keys), 2),
                               "note": "completeness of the reasoning inputs, NOT a verification that the plan is right"}}

    def s15_triforce(ctx):
        return {"what_can_go_wrong": d["triforce"](ctx) if "triforce" in d else {"status": "partial", "see": "verification layer; runtime step not wired"}}

    def s16_roundtable(ctx):
        return {"second_opinions": d["roundtable"](ctx) if "roundtable" in d else {"status": "NOT_RUN", "why": "council not run"}}

    def s16b_freeze_prediction(ctx):
        """Pre-register the expected path BEFORE any effect (frozen to the ledger). No parachute (prepared recovery) = freeze refuses = no jump."""
        if "predict_path" not in d or not path_log:
            return {"prediction": {"status": "NOT_BUILT", "why": "no predicted path or no path log supplied"}}
        spec, reg = d["predict_path"](ctx)
        try:
            hh = pp.freeze(spec, reg, path_log)
        except pp.SpecRefused as e:
            return {"prediction": {"status": "REFUSED_NO_PARACHUTE_OR_BAD_SPEC", "why": str(e)}}
        runtime["spec"], runtime["registry"] = spec, reg                  # callables stay out of the hashed context
        return {"prediction": {"status": "frozen", "spec_hash": hh, "steps": [x["name"] for x in spec["steps"]]}}

    def s17_blueprint(ctx):
        keep = ["request", "prior", "associated", "prompt_reconstruction", "cache_prefix", "history", "lattice_prediction", "route", "library",
                "challenge", "containment", "arsenal", "hybrid", "meta_score", "what_can_go_wrong", "second_opinions", "prediction"]
        bp = {k: ctx[k] for k in keep if k in ctx}
        bp["path"] = choose_path(ctx.get("known_precedent"), request.get("token_cap"), request.get("allow_brute_force", False))
        bp["unbuilt"] = [k for k, v in bp.items() if isinstance(v, dict) and v.get("status") == "NOT_BUILT"]
        bp["requires_commitment"] = True
        sink.append(bp)
        return {"blueprint": bp, "blueprint_hash": hashlib.sha256(canon(bp).encode()).hexdigest()}

    steps = [("s01_intake", s01, ()), ("s02_prior", s02_prior, tuple(verbs)), ("s03_associate", s03_assoc, ()), ("s04_reconstruct_prompt", s04_reconstruct, ()),
             ("s05_cache_prefix", s05_prefix, ()), ("s06_history", s06_history, ()), ("s07_kinetic", s07_kinetic, ()), ("s09_route", s09_route, ()),
             ("s10_library_scan", s10_library, ()), ("s10b_challenge_every_candidate", s10b_challenge, ()), ("s11_containment", s11_containment, tuple(verbs)),
             ("s12_arsenal", s12_arsenal, ()), ("s13_hybrid", s13_hybrid, ()), ("s14_meta_score", s14_meta, ()), ("s15_triforce", s15_triforce, ()),
             ("s16_roundtable", s16_roundtable, ()), ("s16b_freeze_prediction", s16b_freeze_prediction, ()), ("s17_blueprint", s17_blueprint, ())]
    out = [cd.Step(n, f, verbs=v) for n, f, v in steps]

    def s18(ctx):
        if "spec" in runtime:
            runtime["execution"] = pp.run(runtime["spec"], runtime["registry"], path_log, commitment=spec_commitment, commitment_check=d.get("commitment_check"), burn=burn)
            return {"execution": runtime["execution"]}
        return {"execution": {"status": "no frozen path: nothing executed"}}
    out.append(cd.Step("s18_execute_under_commitment", s18, human_only=True))
    return out


def plan(request, conductor, deps=None, commitment=None, spec_commitment=None, execute=False, path_log=None):
    """Run stages 1-17 (always) and stage 18 only when execute=True AND the Conductor accepts the step commitment; the frozen path then needs spec_commitment, a second
    commitment for the exact spec. Returns {"run": Conductor record, "blueprint": the plan or None, "execution": the frozen-path result or None}."""
    validate_request(request)
    deps = deps or {}
    sink, runtime = [], {}
    steps = build_steps(deps, request, sink, runtime, path_log, spec_commitment, burn=conductor.runs)   # one burn chain for both gates
    if not execute:
        steps = steps[:-1]
    verify = lambda ctx: bool(ctx.get("blueprint")) and bool(ctx.get("blueprint_hash"))
    rec = conductor.run("preexec:" + hashlib.sha256(request["goal"].encode()).hexdigest()[:12], {}, steps, verify, commitment=commitment)
    return {"run": rec, "blueprint": sink[0] if sink else None, "execution": runtime.get("execution")}
