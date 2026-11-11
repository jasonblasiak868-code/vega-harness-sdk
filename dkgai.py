"""
dkgai.py - THE UNIVERSAL AI ADAPTER for the VEGA DKG. Any AI, any agent, any shell.

RULE (owner directive 2026-11-10): the DKG record, DAG commitments, and queries
must be usable by EVERY AI - not only the Hermes harness. No Hermes tools are
required here: this file is pure Python stdlib plus the existing harness_core /
vega_custody modules, invoked as a plain CLI. If you can run a shell command,
you can read the record, commit to it, record DAG commitments, and verify.

USAGE (any AI, any terminal, no plugins, no API keys):

    python C:/Users/jjb20/AI_Harness_Workspace/harness_core/dkgai.py <command> [args]

COMMANDS
  resume                       One-query session boot: latest handoff capsule briefing.
  recall <query>               Keyword recall over the DKG capsule store (gated, ranked).
  boot                         Compact capsule boot context (top capsules, ~1.4k chars).
  commit                       Append a handoff/record capsule. Flags:
                                 --done TEXT  --in-progress TEXT  --next TEXT
                                 --decision TEXT  --guard TEXT   (repeatable)
  record <KIND> <json-file>    Append a hash-chained DAG commitment (vega_custody).
                                 KIND e.g. SHU_HOLD, WARDEN_VIOLATION, GATE_ENTRY...
                                 json-file: {"payload": {...}} - chains hold hashes only,
                                 credentials/bulk data are REFUSED by the chain itself.
  verify [dag-file]            Verify a DAG chain from disk (tamper/truncation check).
  corpus <query> [dir]         Grep the graphify corpus (default VEGA_CORPUS_SDK_20261110)
                                 for code/doc text - what the code graph indexes.
  graphinfo [dir]              Corpus + graph index status for a directory.

EXIT CODES: 0 ok, 2 usage error, 3 chain broken/refused (fail closed).

DESIGN CONSTRAINTS: no third-party deps, no network, no env assumptions beyond
the harness_core directory existing next to this file. Every command prints
plain text/JSON a foreign AI can parse.
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

DEFAULT_CORPUS = r"C:\Users\jjb20\VEGA_CORPUS_SDK_20261110"
DEFAULT_DAG = r"C:\Users\jjb20\AI_Harness_Workspace\dkgai_ledger.jsonl"


def _print(text):
    print(text)


# ---------------------------------------------------------------- read side
def cmd_resume(_a):
    import dkg_handoff
    _print(dkg_handoff.resume_from_dkg()["briefing"])


def cmd_boot(_a):
    from dkg_bridge_recall import capsule_boot_context
    _print(capsule_boot_context())


def cmd_recall(a):
    from dkg_bridge_recall import gated_query
    result = gated_query(a.query, kind=a.kind, limit=a.limit)
    _print(json.dumps(result, ensure_ascii=False))


# ---------------------------------------------------------------- write side
def cmd_commit(a):
    import dkg_handoff
    done = " ".join(a.done) or ""
    prog = " ".join(a.in_progress) or ""
    nxt = " ".join(a.next) or ""
    if not (done or prog or nxt or a.decision or a.guard):
        _print("nothing to commit: pass --done/--in-progress/--next/--decision/--guard")
        return 2
    dkg_handoff.commit_handoff(
        done=done, in_progress=prog, next_actions=nxt,
        guards=a.guard or None, decisions=a.decision or None)
    _print("COMMIT OK")
    return 0


def _dag_paths(path):
    return [path]


def cmd_record(a):
    try:
        from vega_custody.dag.dag import Dag
    except ImportError:
        sys.path.insert(0, r"C:\Users\jjb20\vega_runtime\PUBLIC_STAGING\vega-custody-sdk\src")
        from vega_custody.dag.dag import Dag
    with open(a.payload_json, "r", encoding="utf-8") as fh:
        doc = json.load(fh)
    payload = doc.get("payload", doc) if isinstance(doc, dict) else None
    if not isinstance(payload, dict):
        _print("payload must be a JSON object: {\"payload\": {...}}")
        return 2
    base = os.path.splitext(a.dag_file or DEFAULT_DAG)[0]
    dag = Dag([base + ".content.jsonl"], [base + ".locations.jsonl"])
    try:
        entry = dag.record(a.kind, payload)
    except Exception as exc:                                    # RecordRefused / ChainBroken: fail closed
        _print(f"RECORD REFUSED: {type(exc).__name__}: {exc}")
        return 3
    _print("RECORD OK\n" + json.dumps({k: entry[k] for k in ("seq", "kind", "entry_hash", "utc")},
                                      indent=2, ensure_ascii=False))
    return 0


def cmd_verify(a):
    try:
        from vega_custody.dag.dag import Dag
    except ImportError:
        sys.path.insert(0, r"C:\Users\jjb20\vega_runtime\PUBLIC_STAGING\vega-custody-sdk\src")
        from vega_custody.dag.dag import Dag
    if not a.dag_file:
        _print("verify needs --dag-file <path>.content.jsonl (or the base path)")
        return 2
    path = a.dag_file
    if path.endswith(".content.jsonl"):
        base = path[: -len(".content.jsonl")]
    elif os.path.exists(path):
        base = path
    else:
        base = os.path.splitext(path)[0]
    dag = Dag([base + ".content.jsonl"], [base + ".locations.jsonl"])
    ok, problems = dag.verify()
    _print("CHAIN VERIFIED" if ok else "CHAIN BROKEN")
    for p in problems:
        _print(f"  problem: {p}")
    return 0 if ok else 3


# ------------------------------------------------------------ corpus side
def cmd_corpus(a):
    import re
    root = a.dir or DEFAULT_CORPUS
    pat = re.compile(re.escape(a.query), re.IGNORECASE)
    n_hits = n_files = 0
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            if not name.lower().endswith((".md", ".txt", ".json", ".jsonl", ".csv")):
                continue
            fp = os.path.join(dirpath, name)
            try:
                with open(fp, "r", encoding="utf-8", errors="replace") as fh:
                    for i, line in enumerate(fh, 1):
                        if pat.search(line):
                            n_hits += 1
                            if n_hits <= a.limit:
                                _print(f"{fp}:{i}: {line.strip()[:300]}")
            except OSError:
                continue
            n_files += 1
            if n_hits >= a.limit:
                break
        if n_hits >= a.limit:
            break
    _print(f"[corpus] files_scanned={n_files} hits={n_hits} (limit={a.limit})")


def cmd_graphinfo(a):
    root = a.dir or DEFAULT_CORPUS
    total = indexed = 0
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            total += 1
            if name.lower().endswith((".md", ".txt", ".json", ".jsonl", ".csv")):
                indexed += 1
    _print(f"corpus={root} files={total} text_indexable={indexed}")


def main(argv=None):
    p = argparse.ArgumentParser(prog="dkgai", description="Universal AI adapter for the VEGA DKG")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("resume").set_defaults(func=cmd_resume)
    sub.add_parser("boot").set_defaults(func=cmd_boot)

    r = sub.add_parser("recall")
    r.add_argument("query")
    r.add_argument("--kind", default=None)
    r.add_argument("--limit", type=int, default=10)
    r.set_defaults(func=cmd_recall)

    c = sub.add_parser("commit")
    c.add_argument("--done", nargs="+", default=[])
    c.add_argument("--in-progress", nargs="+", default=[])
    c.add_argument("--next", nargs="+", default=[])
    c.add_argument("--decision", action="append", default=[])
    c.add_argument("--guard", action="append", default=[])
    c.set_defaults(func=cmd_commit)

    rc = sub.add_parser("record")
    rc.add_argument("kind")
    rc.add_argument("payload_json")
    rc.add_argument("--dag-file", default=None)
    rc.set_defaults(func=cmd_record)

    v = sub.add_parser("verify")
    v.add_argument("dag_file", nargs="?", default=None)
    v.set_defaults(func=cmd_verify)

    cp = sub.add_parser("corpus")
    cp.add_argument("query")
    cp.add_argument("dir", nargs="?", default=None)
    cp.add_argument("--limit", type=int, default=20)
    cp.set_defaults(func=cmd_corpus)

    gi = sub.add_parser("graphinfo")
    gi.add_argument("dir", nargs="?", default=None)
    gi.set_defaults(func=cmd_graphinfo)

    a = p.parse_args(argv)
    return a.func(a) or 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        sys.exit(0)
