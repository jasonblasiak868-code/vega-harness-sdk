# vega-harness-sdk

![vega-harness-sdk](assets/tile.svg)

The harness core: a deterministic Conductor, pre-registered predicted paths, library use gates and the pre-execution reasoning pipeline.

> **Licensing.** Source-available, not open source. Free for personal use; free for registered small businesses (under US$1M revenue and valuation); everyone else needs a commercial license (see LICENSE; for a commercial license write to jasonblasiak.ai.mail@gmail.com). Patent pending. Draft licence text, not yet reviewed by an attorney.

Package: `vega_harness` (src layout). Depends on: vega-custody-sdk, vega-governance-jacket-sdk, vega-librarian-sdk.

## Modules

| Module | What it does |
|---|---|
| `conductor` | Runs a workflow step by step with no model inside: lesson preflight, human-only gate with single-use commitments, token cap, strict verification, second opinion, chain-recorded runs, replay that locks a workflow only if it reproduces. |
| `predicted_path` | Freeze the expected path before any effect; compare reality to it; halt and recover on a surprise. No recovery prepared, no jump. |
| `use_gate` | Use card, splice gate and release gate for third-party repos: public builds are clean-room only, personal builds are contained and tainted. |
| `reasoning_pipeline` | Stages 1 to 17 build a blueprint (every candidate challenged, none accepted on first match); stage 18 executes only under two commitments. |
| `records` | The record chain and single-use commitment burning shared by every gate. |

## What it does NOT do (read this)

- The Conductor sequences and gates; it does not reason.
- Registry callables run in this process with this process's rights: containment is not an operating-system sandbox.
- The license matrix is policy, not legal advice.

## Status

See STATUS.md. Tests: 78 tests in 3 files, all passing.  See SECURITY.md for the threat model.

## Run the tests

Put `src` of this repo and of the repos it depends on on PYTHONPATH, then run each file in `tests/`.
