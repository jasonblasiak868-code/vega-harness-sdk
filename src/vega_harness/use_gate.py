"""use_gate.py - the use card, the splice gate and the release gate for a library of third-party repos.

  use_card(repo)                    plain-language card shown BEFORE any copy, splice or combine
  splice_gate(sources, build)       build is 'public' or 'personal'. Public: refuse ANY library source (clean-room only). Personal: allowed only inside
                                    quarantine, and the output is marked TAINTED with the strictest class touched. A matrix row whose personal_build says no
                                    (no, never, forbidden, not allowed, refused, false or empty) forbids a personal build with that repo.
  release_gate(provenance)          a public release passes only if its provenance STATES an empty library_sources list, STATES tainted: false (an absent mark is not
                                    a clean one), and carries a clean-room attestation (text or an object; a bare True is not an attestation)
Rules come from a matrix (a list of {repo, license_class, personal_build, public_build, taint_if_used}) given as a path in VEGA_USE_MATRIX or as an argument.
No matrix, an unknown repo, or an unknown class = treated as NO_LICENSE_FILE (the strict side). Not legal advice."""
import json
import os

STRICT = ["NON_COMMERCIAL_OR_RESTRICTED", "STRONG_COPYLEFT", "WEAK_COPYLEFT", "NO_LICENSE_FILE", "UNKNOWN", "PERMISSIVE"]   # strictest first
DEFAULT_CLASS = "NO_LICENSE_FILE"


class GateRefused(ValueError):
    pass


def load_matrix(path=None):
    path = path or os.environ.get("VEGA_USE_MATRIX")
    if not path or not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        rows = json.load(fh)
    if not isinstance(rows, list):
        raise GateRefused("the use matrix is a list of repo rows")
    return {r["repo"]: r for r in rows if isinstance(r, dict) and isinstance(r.get("repo"), str)}


_FORBID = ("no", "never", "forbid", "not allowed", "not permitted", "refus", "prohibit", "denied")


def personal_forbidden(row):
    """True if a matrix row says a personal build may not use this repo. Free text is read conservatively: only an explicit no counts, so rows such as 'read in quarantine only'
    and 'contained use' still allow a personal read inside quarantine."""
    if not isinstance(row, dict) or "personal_build" not in row:
        return False
    v = row["personal_build"]
    if v is False or v is None:
        return True
    if not isinstance(v, str):
        return False
    t = v.strip().casefold()
    return not t or any(t == w or t.startswith(w + " ") or t.startswith(w + ",") or t.startswith(w + ";") or (w.endswith(("bid", "us", "ibit")) and t.startswith(w)) for w in _FORBID)


def klass_of(license_class):
    """A license class as the gates read it: anything unrecognised, or UNKNOWN, is treated as NO_LICENSE_FILE (the strict side)."""
    return DEFAULT_CLASS if not isinstance(license_class, str) or license_class not in STRICT or license_class == "UNKNOWN" else license_class


def klass(repo, matrix=None):
    return klass_of(((matrix if matrix is not None else load_matrix()).get(repo) or {}).get("license_class", "UNKNOWN"))


def use_card(repo, matrix=None):
    m = matrix if matrix is not None else load_matrix()
    r = m.get(repo)
    if not r:
        return "%s is not in the library matrix. Treat it as NO LICENSE: read in quarantine only; never use in a public build." % repo
    return ("USE CARD for %s\n  License class: %s\n  Personal build: %s\n  Public build: %s\n  Tainted if used: %s\n  What taints your result: copying, pasting, translating or lightly rewording its source or text, or running it outside quarantine.\n  Safe path for anything public: write a specification in your own words, and have someone who never opened the source write the code (clean-room)."
            % (repo, r.get("license_class", "UNKNOWN"), r.get("personal_build"), r.get("public_build"), "yes" if r.get("taint_if_used") else "no"))


def splice_gate(sources, build, matrix=None):
    if build not in ("public", "personal"):
        return {"allowed": False, "reason": "build must be 'public' or 'personal'"}
    if isinstance(sources, (str, bytes)) or not isinstance(sources, (list, tuple, set, frozenset)) or any(not isinstance(s, str) for s in sources):
        return {"allowed": False, "reason": "sources must be a list of repo names (a bare string would be read letter by letter)"}
    if not sources:
        return {"allowed": True, "tainted": False, "reason": "no library source"}
    m = matrix if matrix is not None else load_matrix()
    strictest = min((klass(s, m) for s in sources), key=STRICT.index)
    if build == "public":
        return {"allowed": False, "tainted": True, "strictest_class": strictest,
                "reason": "public builds are clean-room only; no library source may be spliced. Write a specification and use the behavioral clean room."}
    for s_ in sources:
        row = m.get(s_)
        if row and personal_forbidden(row):
            return {"allowed": False, "tainted": True, "strictest_class": strictest, "reason": "the use matrix forbids a personal build with %s" % s_}
    return {"allowed": True, "tainted": True, "strictest_class": strictest, "condition": "inside quarantine only; output marked TAINTED; never part of a public build"}


def release_gate(provenance):
    if not isinstance(provenance, dict):
        return {"released": False, "reason": "provenance must be an object"}
    srcs = provenance.get("library_sources")
    if not isinstance(srcs, list):
        return {"released": False, "reason": "provenance must state library_sources as a list (an absent list is not an empty one)"}
    if srcs:
        return {"released": False, "reason": "provenance lists library sources: %s" % ", ".join(str(s) for s in srcs)}
    if provenance.get("tainted") is not False:
        return {"released": False, "reason": "provenance must state tainted: false (it is absent, marked TAINTED, or not a plain false)"}
    att = provenance.get("clean_room_attestation")
    if not ((isinstance(att, str) and att.strip()) or (isinstance(att, dict) and att)):
        return {"released": False, "reason": "no clean-room attestation in the provenance (text or an object; a bare True is not one)"}
    return {"released": True}


def preflight(verbs):
    """Surfaces lesson-ledger lessons for copy/splice verbs (never blocks by itself). Errors are not hidden: if the ledger cannot be read, this raises."""
    from vega_librarian import lesson_ledger as ll
    return ll.preflight(list(verbs))
