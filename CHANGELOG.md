# Changelog

## 2026-10-20: fixes found by a source read

These were found by reading the source against its own documentation after release, not by the earlier test and review passes. Each fix has a regression test in
`tests/test_findings_20261020.py` that failed before the fix, and each fix was checked by undoing it and confirming a test fails. Behaviour that changed is listed under "Changed".

### Fixed
- **A run stopped by the token cap could spend a human commitment.** The cap was checked after a human-only step's commitment was burned. It is now checked first.
- **A request's `token_cap` did nothing.** `plan()` validated it and ignored it. `Conductor.run()` now takes an optional `token_cap` and `plan()` passes the request's; the tighter cap wins.
- **A step's identity ignored values it captured and its default arguments**, so two steps with the same body but different captured values had the same workflow id. Both are now part of the identity (`records.code_hash`). A captured object that is not plain data is identified by its type only.
- **A frozen path was not bound to the code it would run.** A registry callable swapped after freezing kept the same spec hash. `freeze` now records a registry hash and `run` refuses a registry that differs (or a spec frozen without the binding).
- **The release gate accepted a record with no taint mark.** `tainted: false` must now be stated, like `library_sources`.
- **`prompt_layers` was never used by the pipeline.** The cache prefix is now also reported as append-only layers (policy, goal, anchors) with prefix hashes.
- **The library gate never read the matrix row's `personal_build` field.** A row that says no (no, never, forbidden, not allowed, refused, false or empty) now forbids a personal build with that repo; rows such as "read in quarantine only" still allow a quarantined read.
- Stage numbering skips 8: documented as retired (renumbering would change every stage's identity).

### Changed (read before upgrading)
- `release_gate` callers must include `"tainted": false`.
- A spec frozen by an earlier version has no registry binding and must be frozen again before it can run.
- Workflows whose steps capture values have new ids, so earlier replay locks do not match them (this fails safe).
