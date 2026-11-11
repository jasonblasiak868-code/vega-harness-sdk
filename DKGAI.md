# DKGAI - the universal any-AI interface to the VEGA DKG

Owner rule (2026-11-10): the DKG record, DAG commitments, and queries must be
usable by EVERY AI, not only the Hermes agent. If you are an AI reading this
repo, you already have everything you need: a shell and Python. No plugins,
no API keys, no network.

## Commands (all pure stdlib)

```
python dkgai.py resume                  # one-query session boot (latest handoff capsule)
python dkgai.py boot                    # compact capsule boot context
python dkgai.py recall "<keywords>"     # ranked DKG recall (JSON)
python dkgai.py commit --done "..." --next "..."   # append a handoff/record capsule
python dkgai.py record KIND payload.json --dag-file PATH.jsonl   # hash-chained DAG commitment
python dkgai.py verify PATH.jsonl       # verify a chain from disk (tamper/truncation -> exit 3)
python dkgai.py corpus "<text>"         # grep the graphify corpus (code graph content)
python dkgai.py graphinfo [dir]         # corpus/index status
```

## Rules every AI must follow

- Read side is always allowed. `resume` + `recall` + `corpus` replace re-reading
  files or transcripts - query the record instead of holding context.
- `record` appends to a hash-chained DAG: chains hold hashes only. Credential-shaped
  or bulk values are REFUSED by the chain itself (exit 3). Never put secrets in payloads.
- The human commitment rule is unchanged: the model never self-authorizes governance
  changes. `commit` writes to the handoff capsule lane under standing owner directives;
  anything beyond that waits for a human commitment.
- Fail closed: exit code 3 = chain broken or record refused. Never ignore it.

## Where the surfaces live

- CLI (this file): `dkgai.py` - works from any shell, any AI, zero dependencies.
- MCP (read-only, for MCP-capable agents): `harness_core/w3_mcp_server.py`
  (dkg_search, dkg_traverse, dkg_deep_verify, ...). Writes require a human
  TPM-signed commitment.
- Code graph: corpus at `C:\Users\jjb20\VEGA_CORPUS_SDK_20261110`, indexed by
  graphify; `dkgai.py corpus` searches the same content without the graph.
