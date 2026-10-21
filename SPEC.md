# vega-harness-sdk — Specification

Spec v1.0 — 2026-10-19 — describes the published reference implementation as tested; where this document and the source disagree, the source wins.

## 1. Purpose and scope

This specification covers the five modules published in `src/vega_harness/`: `conductor`, `predicted_path`, `use_gate`, `reasoning_pipeline` and `records`. It specifies what each module guarantees, what it refuses, and how a third party can verify both by running the tests in `tests/`.

Out of scope: the behavior of the dependencies themselves (vega-custody-sdk, vega-governance-jacket-sdk, vega-librarian-sdk — each has its own spec), the pipeline stages that report `NOT_BUILT` when their dependency is not supplied (prompt reconstruction, kinetic prediction, history, hybrid — DESIGNED ONLY per STATUS.md; the arsenal stage reports NOT_BUILT until something is indexed), any operating-system-level containment, and any legal interpretation of the license matrix.

## 2. Vocabulary

Definitions simplified from the code's own docstrings.

- **Step** — a named callable plus the verbs it declares. Its identity includes a hash of its code AND of the values it captured and its default arguments, so a different function, or the same function over a different captured value, is a different step (`conductor.Step`, `records.code_hash`). A captured object that is not plain data is identified by its type only: keep what a step depends on in plain data.
- **Verb** — a text label a step declares (e.g. `delete`, `push`, `publish`). Risky verbs trigger the lesson preflight; human-only verbs force the step through the human gate (`reasoning_pipeline.RISKY`, `vega_jacket.humanauth`).
- **Human-only step** — a step that says so, is named for a human-only action, or declares a human-only verb. It cannot dodge the gate by declaring itself ordinary.
- **Commitment** — a small signed note from a human authorising one exact action once, for a short time. In this kit it is a `WCOMMIT` dict checked against a ledger (`records.make_ledger_check`). The signing key is never on the model's side.
- **Burn** — recording a commitment's hash as spent, atomically, so the same signature can never authorise a second step, spec or run (`records.burn_commitment` via `RecordChain.append_unique`).
- **Gate** — a check that refuses by default and fails closed: the human-only gate, the token cap, the verifier, the second opinion, the freeze/execute gates, the splice and release gates.
- **Record chain** — the append-only, hash-chained ledger every harness record lands in, provided by vega-custody-sdk's DAG (`records.RecordChain`).
- **Run record** — one Conductor run: per-step input/output hashes, preflight lesson ids, gate verdicts, token estimate, verify result, final result hash, status. Hashes and verdicts, never values.
- **Candidate PRE** — a successfully run workflow registered in the registry chain as reusable-pending-reproduction (`WORKFLOW_CANDIDATE_PRE`).
- **Locked workflow** — a candidate whose replay reproduced the first candidate's result hash exactly (`WORKFLOW_REPLAY` with status `locked`). A non-reproducing replay can never become the baseline.
- **Spec** — a predicted-path document: a goal and an ordered list of steps, each with an action name, an expectation descriptor, optional foreseen alternatives, and a recovery (`predicted_path.validate`).
- **Freeze** — validating a spec fail-closed and recording its hash as `PATH_FROZEN` before any effect. Execution later requires this exact hash to be present in the log.
- **Descriptor** — exactly one known test per descriptor: `equals` (type-strict), `contains`, `in_range` (numbers, not bool), `is_true`, `type: dict_has`, `custom` (named check in the registry).
- **Outcome** — per step: `ON_PATH` (matches expectation), `PREDICTED_ALT` (matches a foreseen alternative; its prepared response runs), `OFF_PATH` (anything else, or any exception: halt, recover, record an incident, queue a lesson).
- **Parachute** — a step's prepared recovery, named and present in the registry. No parachute, no jump: freeze refuses a side-effecting step without one.
- **Registry** (predicted_path) — tables of callables by name: `actions`, `checks`, `responses`, `recoveries`. They run in this process with this process's rights.
- **Use card** — the plain-language card for a library repo shown before any copy, splice or combine (`use_gate.use_card`).
- **License class** — one of `NON_COMMERCIAL_OR_RESTRICTED`, `STRONG_COPYLEFT`, `WEAK_COPYLEFT`, `NO_LICENSE_FILE`, `UNKNOWN`, `PERMISSIVE`, strictest first. Anything unrecognised is treated as `NO_LICENSE_FILE`.
- **Taint** — the mark on output that touched library source, carrying the strictest class touched. A tainted output cannot pass the release gate.
- **Clean-room attestation** — text or an object in the provenance stating the public build was written from a specification by someone who never opened the source. A bare `True` is not an attestation.
- **Blueprint** — the plan assembled by pipeline stages 1–17: request, priors, library candidates, challenge results, containment plan, meta-score, frozen prediction, and the list of unbuilt stages. A plan, not a result.
- **Deps** — the injected callables the pipeline stages use (`recall`, `precedents`, `associate`, `library_fit`, `verify_candidate`, `kinetic`, ...). A stage without its dep returns `NOT_BUILT`; nothing pretends.
- **Token estimate** — `max(1, len(canonical_json)//4)`; a budget measure, not a model's tokenizer (`conductor.est_tokens`).
- **Calibration** — the measured share of frozen-path steps that held (`ON_PATH` or `PREDICTED_ALT`), with a Wilson lower bound (`predicted_path.calibration`).

## 3. Components

### 3.1 records.py — the record chain and commitment burning

**Role.** One place where every harness record lands and every gate burns commitments.

**Public interface.** `RecordRefused`, `Duplicate`, `canon`, `h`, `RecordChain(path, mirrors=())` with `.read()`, `.verify(expected_head=None, min_length=None)`, `.append(kind, payload)`, `.append_unique(kind, field, value, payload)`; `burn_commitment(chain, commitment, purpose)`; `make_ledger_check(ledger, clock=time.time)`.

**Behavior.** `canon` is strict canonical JSON with no `default=` fallback: a value that is not plain JSON data is refused, never rendered as text containing a memory address; NaN is refused. `append` retries when the chain changed underneath (concurrent writers are safe; a stale writer re-reads and retries). `append_unique` is an atomic first-writer-wins: a second record of the same kind and field value raises `Duplicate`. `verify` delegates to the custody chain and returns `(False, problems)` rather than raising on a corrupt chain. `make_ledger_check` returns a checker that accepts only a `WCOMMIT` for exactly the requested action, unexpired, verifying in the ledger; any exception inside the check means "not accepted" (returns `False`).

**Invariants.** Append-only; every entry fingerprints its predecessor (custody property); records hold hashes, names, verdicts and counts, never secrets (credential-shaped values are refused by the custody chain); one commitment id burns once across the shared chain.

### 3.2 conductor.py — the deterministic Conductor

**Role.** Runs a workflow step by step with no model inside: it only sequences, gates, measures and records.

**Public interface.** `ConductorRefused`, `MAX_STEPS` (200), `est_tokens`, `code_hash`, `Step(name, fn, verbs=(), human_only=False)` with `.ident()`, `workflow_id(steps)`, `read(path)`, `verify_chain(path, expected_head=None, min_length=None)`, `locked_workflows(registry_path)`, `Conductor(runs_path, registry_path, preflight=None, commitment_check=None, token_cap=None)` with `.run(task_id, ctx, steps, verify, second_opinion=None, commitment=None, register=True)` and `.replay(task_id, ctx, steps, verify, second_opinion=None, commitment=None)`.

**Behavior on success.** For each step: lesson preflight once per step that declares verbs (surfaced lesson ids recorded, protect-text passed to the step); human-only steps require a commitment that the checker accepts with exactly `True` for `conductor_step:<name>`, then burn it; token estimate accumulates against the cap; the step runs, its input and output are hashed, output merged into context. After all steps: `verify(ctx)` must return exactly `True`; a supplied second opinion must return `{"agree": True, ...}`; the result hash covers the context minus underscore-prefixed keys; a `RUN` record is appended; a success registers a `WORKFLOW_CANDIDATE_PRE`. `replay` re-runs without registering a new candidate and appends `WORKFLOW_REPLAY`: status `locked` only if the run succeeded and its result hash equals the FIRST candidate's; a locked workflow refuses further replays; replaying an unregistered workflow raises `KeyError`.

**Behavior on failure (fail-closed).** A preflight that raises or returns a malformed list stops the run (`stopped: lesson preflight failed`). A missing, loose, crashing or already-burned commitment refuses the step (`refused: ... needs a human-signed commitment`; reuse recorded as `commitment_reuse: refused`). Exceeding the token cap stops the run. A step that raises, or returns a non-dict, fails the run cleanly — recorded, not raised past the caller. A verifier that crashes has verified nothing; a second opinion that crashes or disagrees blocks success and registration. Every terminal path still writes its run record.

**Invariants.** No model call anywhere in the module. A step's identity includes its code hash, its captured values and its default arguments: a different function, or the same function over a different value, is a different workflow. The token cap is the tighter of the Conductor's own and the one passed to `run()`; it is checked before a human-only step's commitment is burned, so a run stopped by the cap does not spend a signature. `token_cap=0` is refused at construction, never read as "unlimited". Values entering hashes must be plain JSON data. Bytecode differs between Python versions, so a lock made on one version does not match on another; that fails safe.

### 3.3 predicted_path.py — pre-registered predictions, tripwires, prepared parachute

**Role.** Freezes the expected path before any effect and compares reality to it, so a surprise can never be re-explained as "it fit all along".

**Public interface.** `SpecRefused`, `KNOWN` descriptor keys, `spec_hash(spec)`, `matches(desc, obs, registry)`, `validate(spec, registry)`, `freeze(spec, registry, log_path)`, `run(spec, registry, log_path, state=None, commitment=None, commitment_check=None, burn=None)`, `wilson_lower(pos, n, z=1.96)`, `calibration(log_path, spec_hash=None)`.

**Behavior on success.** `freeze` validates fail-closed and appends `PATH_FROZEN` (spec hash, goal, step names); returns the hash. `run` requires: this exact spec hash already frozen in this log; a commitment the checker accepts with exactly `True` for purpose `execute:<spec hash>`; a successful burn of that commitment (a `Duplicate` refuses). Then per step it calls the action, tests the observation against the descriptor, applies a foreseen alternative's response on `PREDICTED_ALT`, appends a `STEP` record, and on full completion appends `RUN_DONE` with counts. Observations and response results enter the log as hashes, never as text.

**Behavior on failure (fail-closed).** `validate` refuses: a spec that is not an object with a non-empty step list; a registry without an `actions` table; missing or duplicate step names; actions, custom checks or responses not in the registry; descriptors with zero or two known tests; a side-effecting step (not `read_only`) with no named recovery present in the registry ("no parachute, no jump"). `freeze` also records a registry hash (the code identity of every named action, check, response and recovery); `run` refuses if the registry it is given no longer hashes the same, or if the spec was frozen without that binding. `run` refuses when the spec was not frozen first or changed after freezing, when the commitment is absent/loose/crashing-checker, or when the commitment was already used. Any exception while acting, checking or responding is `OFF_PATH`: halt (halt is not grant), run the prepared recovery, record an `INCIDENT` (a failed recovery is recorded as failed; the run halts either way), keep the state maintained, queue a lesson, and stop later steps.

**Invariants.** Exactly one test per descriptor; `equals` is type-strict (1 is not True); `in_range` is numbers-only and finite (not bool); `is_true` requires the observation to BE that bool; a custom check must return exactly `True`. One signature runs one spec once (given a shared burn chain — every gate in a deployment must use the SAME burn chain, or one signature could be spent once per chain).

### 3.4 use_gate.py — use card, splice gate, release gate

**Role.** Gates third-party library use: informed before, contained during, clean at release.

**Public interface.** `GateRefused`, `STRICT` (class order, strictest first), `DEFAULT_CLASS` (`NO_LICENSE_FILE`), `load_matrix(path=None)`, `klass_of(license_class)`, `klass(repo, matrix=None)`, `use_card(repo, matrix=None)`, `splice_gate(sources, build, matrix=None)`, `release_gate(provenance)`, `preflight(verbs)`.

**Behavior.** The matrix is a list of rows `{repo, license_class, personal_build, public_build, taint_if_used}`, loaded from a path argument or the `VEGA_USE_MATRIX` environment variable. `use_card` renders the plain-language card; a repo not in the matrix is reported as "treat as NO LICENSE". `splice_gate` refuses a bare string of sources (it would be read letter by letter) and any build value other than `public`/`personal`; empty sources are allowed untainted; a public build is refused for ANY non-empty library sources (clean-room only); a personal build is refused for any source whose matrix row's `personal_build` says no (no, never, forbidden, not allowed, refused, false or empty), and otherwise allowed, tainted, with the strictest class touched and the condition "inside quarantine only; never part of a public build". `release_gate` passes only if the provenance is an object that STATES `library_sources` as a list (absent is not empty), the list is empty, `tainted` is STATED and is plain `false` (an absent mark is refused, like an absent list), and a real clean-room attestation (non-empty text or non-empty object) is carried. `preflight(verbs)` surfaces lesson-ledger lessons for copy/splice verbs; it never blocks by itself, and it raises rather than hides a ledger it cannot read.

**Invariants.** Fail to the strict side: no matrix, unknown repo, or unknown/unrecognised class is treated as `NO_LICENSE_FILE`. The matrix is policy, not legal advice.

### 3.5 reasoning_pipeline.py — the pre-execution reasoning pipeline

**Role.** Stages 1–17 build a blueprint on the Conductor; stage 18 executes only under two commitments.

**Public interface.** `NOT_BUILT`, `POLICY_PREFIX`, `RISKY` verbs, `RequestRefused`, `risky_verbs(text)`, `stable_prefix(goal, node_ids)`, `prompt_layers(layers)`, `compact(c)`, `choose_path(known_precedent, token_cap=None, allow_brute_force=False)`, `validate_request(request)`, `build_steps(deps, request, sink=None, runtime=None, path_log=None, spec_commitment=None, burn=None)`, `plan(request, conductor, deps=None, commitment=None, spec_commitment=None, execute=False, path_log=None)`.

**Behavior on success.** `plan` validates the request, builds the 18 steps, and runs stages 1–17 on the Conductor (stage 18 only when `execute=True`): intake with risky-verb detection (word boundaries and inflections); prior memory/precedents; association; prompt reconstruction (dep-injected); a cache-stable prompt prefix (same inputs, byte-identical prefix, hashed); history (dep); kinetic/lattice prediction (dep); routing; library scan producing fits, combination hypotheses ("hypothesis only; needs a contained run") and optional use cards; **challenge of EVERY candidate** — a match is a candidate, never a stop: each is verified through `deps['verify_candidate']`, ranked, rejected ones kept with reasons, downstream risk asked per candidate; containment decision (risky verbs or any non-permissive source demand the PRE/PEAK plan); arsenal (dep); hybrid; meta-score (completeness of reasoning inputs, explicitly NOT a verification the plan is right); what-can-go-wrong; roundtable; freeze of the predicted path via `predicted_path.freeze` (the spec and its callables stay OUT of the hashed context — a callable's repr contains a memory address); blueprint assembly with its hash, the chosen path (option 2 by default; option 1 brute force only when the caller passes a token cap AND `allow_brute_force` AND no precedent), the unbuilt list, and `requires_commitment: True`. Stage 18 is a HUMAN_ONLY step: the Conductor refuses it without a valid single-use commitment for that step, and `predicted_path.run` then needs a SECOND commitment for the exact frozen spec. Both gates burn into one shared chain (`conductor.runs`).

**Behavior on failure (fail-closed).** A malformed request (no text goal, non-text prompt, `token_cap` that is not a positive whole number) is refused. A request's `token_cap` caps the run (the tighter of it and the Conductor's own cap). The blueprint's `cache_prefix` also lists the append-only layers (policy, goal, anchors) with their prefix hashes. A dep that returns non-JSON fails the run closed through the Conductor's canonicalisation. A freeze refusal (no parachute or bad spec) is recorded in the blueprint as `REFUSED_NO_PARACHUTE_OR_BAD_SPEC`. Without a path log, nothing is frozen and the blueprint says so; without a frozen spec, stage 18 executes nothing ("no frozen path: nothing executed"). A dep-less stage returns `NOT_BUILT` and stage 14 lists it; the blueprint's `unbuilt` repeats it.

**Invariants.** No stage is skipped silently; unbuilt is stated, never hidden. Verification status `NOT_BUILT` means nothing was verified — the challenge note says so explicitly ("do not trust a pick"). The blueprint is a plan, not a result. Autonomy never mints a commitment.

### 3.6 arsenal.py — the war chest index

**Role.** One index of everything the harness can reach for, so a model asks instead of remembering. Entry kinds: tool, repo, symbol, prompt, solution, skill. Each entry has a name, a summary, a path, tags, a score from 0 to 1 and an optional licence class.

**Public interface.** `Arsenal(path=None, embed=None, use_matrix=None)` with `add`, `remove`, `save`, `search(query, n, kind, show_use_cards)`, `manifest(per_kind)`, `index_hash()` and `as_dependency(n, kind, threshold, show_use_cards)` (the callable pipeline stage 12 takes); `KINDS`, `ArsenalError`.

**Behavior.** Entries are checked on the way in (kind, non-empty text, score a real number from 0 to 1, tags a list) and stored whole, never truncated. Searching ranks by similarity, then by the stored score, then by name; a hit for a repo carries its licence class (a missing class is the strict side) and, on request, the use card. The manifest and the index hash do not depend on insertion order. An empty arsenal reports `NOT_BUILT` to the pipeline.

**Not guaranteed.** A close match is a candidate, not an answer. The score is what the caller entered. Default matching is on words, not meaning.

## 4. Guarantees and non-guarantees

### Guarantees (module — test that demonstrates)

| Guarantee | Module | Test |
|---|---|---|
| A human-only step never runs without a valid commitment for exactly that step | conductor | `redteam_1_conductor.py::HumanGate` (7 tests) |
| A step cannot dodge the gate by declaring itself ordinary | conductor | `test_a_step_cannot_dodge_the_gate_by_declaring_itself_ordinary` |
| One signature authorises one step once; reuse is refused and recorded | conductor + records | `test_one_signature_authorises_one_step_once`, `test_a_commitment_burned_by_one_of_two_racing_runs_is_refused_to_the_other` |
| A failing lesson preflight stops the run; it is never skipped silently | conductor | `redteam_1_conductor.py::Preflight` |
| Only exactly `True` verifies; a crashing verifier or second opinion blocks success | conductor | `redteam_1_conductor.py::LyingVerifiers` |
| Token cap stops the run; 0 is refused, not "unlimited" | conductor | `test_token_cap_stops_the_run`, `test_token_cap_zero_does_not_mean_unlimited` |
| A replay locks only on exact reproduction of the first candidate; a non-reproducing replay never becomes the baseline | conductor | `redteam_1_conductor.py::ReplayBaseline` |
| A different function under the same name is a different workflow | conductor | `test_a_different_function_under_the_same_name_is_a_different_workflow` |
| Run records hold hashes, not values; credential-shaped input is refused | conductor + records | `test_the_run_record_holds_hashes_not_values`, `test_a_credential_shaped_task_id_is_refused_by_the_chain` |
| Chain tampering (cut-off tail) is caught with a remembered head; 8 concurrent writers keep the chain valid | records | `redteam_1_conductor.py::ChainProperties` |
| No freeze, no run; a changed spec is a different spec | predicted_path | `test_run_refused_when_not_frozen_changed_or_no_commitment`, `test_a_spec_frozen_in_another_log_is_not_frozen_here` |
| No parachute, no jump | predicted_path | `test_no_parachute_no_jump` |
| Any exception anywhere is OFF_PATH: halt, recover, record, queue lesson | predicted_path | `test_exception_is_off_path_and_contained`, `test_a_crashing_check_response_or_recovery_halts_and_is_recorded_not_raised` |
| Descriptors are strict: type-strict equals, no-bool ranges, exactly-one-test | predicted_path | `redteam_2_path_gates_pipeline.py::Descriptors` |
| One signature runs one spec once; a signature for one spec does not run another | predicted_path + records | `redteam_2_path_gates_pipeline.py::Commitments` |
| Results enter the log as hashes, never as text | predicted_path | `test_results_enter_the_log_as_hashes_never_as_text` |
| Public builds refuse ANY library source; personal builds are tainted with the strictest class | use_gate | `test_public_refuses_any_library_source_and_personal_is_tainted_with_the_strictest_class` |
| Unknown repo/class/missing matrix fails to the strict side; a damaged matrix is a clear error | use_gate | `redteam_2_path_gates_pipeline.py::Gates` |
| Release needs everything STATED, not just nothing wrong | use_gate | `test_release_gate_wants_everything_stated_not_just_nothing_wrong` |
| First match is never a stop; every candidate is challenged | reasoning_pipeline | `test_first_match_is_never_a_stop` |
| Stage 18 needs two different commitments | reasoning_pipeline | `test_stage_18_needs_two_different_commitments` |
| Unbuilt stages say NOT_BUILT; the blueprint lists them | reasoning_pipeline | `test_plan_without_deps_succeeds_and_says_what_is_not_built` |
| The frozen spec and its callables stay out of the hashed context | reasoning_pipeline | `test_the_frozen_spec_and_its_callables_stay_out_of_the_hashed_context` |
| Prompt layers are append-only with prefix hashes | reasoning_pipeline | `test_prompt_layers_are_append_only` |

### Non-guarantees

| Not guaranteed | Why |
|---|---|
| OS-level containment of registry callables, steps or recoveries | They run in this process with this process's rights (module docstrings; SECURITY.md threat model) |
| Correct legal classification of any license | The matrix is policy, not legal advice (`use_gate` docstring) |
| That a verified/challenged blueprint is a GOOD plan | The meta-score is completeness of inputs, "NOT a verification that the plan is right" (stage 14) |
| That high calibration means hard predictions | It counts whatever was frozen and run; easy specs raise it (`predicted_path` docstring) |
| Single-use across SEPARATE burn chains | Every gate in a deployment must share one burn chain, or a signature could be spent once per chain (`run` docstring) |
| Locks portable across Python versions | Bytecode differs between versions; a lock fails safe rather than matching (`conductor` docstring) |
| That the dep-injected stages exist | Without deps they return NOT_BUILT (STATUS.md: DESIGNED ONLY) |
| Security against a compromised OS, a stolen human key, or a path never wired through a gate | SECURITY.md threat model |

## 5. Failure modes

- **Tamper with the record chain** → `verify` / a remembered head catches a cut-off tail; concurrent writers never corrupt it (`redteam_1_conductor.py::ChainProperties`: `test_a_cut_off_tail_is_caught_with_a_remembered_head`, `test_eight_conductors_running_at_once_all_record_and_the_chain_stays_valid`).
- **Reuse a burned commitment** → `Duplicate` from `append_unique`; the run refuses and records `commitment_reuse: refused` (`test_one_signature_cannot_open_two_human_only_steps_in_one_run`, `test_a_commitment_burned_by_one_of_two_racing_runs_is_refused_to_the_other`, `test_one_signature_runs_a_spec_once`).
- **Expired or wrong-action commitment** → `make_ledger_check` returns `False` (expiry vs clock, exact action string); the gate refuses (`test_a_real_ledger_commitment_for_exactly_this_action`, `test_a_signature_for_one_spec_does_not_run_another`).
- **Loose or crashing checker** → treated as refusal, never as acceptance (`test_a_checker_that_says_yes_loosely_or_crashes_does_not_open_the_gate`, `test_a_loose_or_crashing_checker_does_not_run_anything`).
- **Replay attack on a workflow** → a non-reproducing replay is recorded `failed_replay` and never becomes the baseline; a locked workflow refuses further replays (`test_a_replay_that_does_not_reproduce_never_becomes_the_baseline`, `test_a_reproducing_replay_locks_and_a_locked_workflow_cannot_be_replayed_again`).
- **Corrupt or hostile record payload** → non-JSON data is refused, never hashed as a memory address; credential-shaped values are refused by the chain (`test_non_json_data_is_refused_not_hashed_as_a_memory_address`, `test_a_credential_shaped_task_id_is_refused_by_the_chain`, `test_a_dependency_that_returns_non_json_fails_the_run_closed`).
- **Unforeseen observation or exception mid-path** → OFF_PATH: halt, prepared recovery, INCIDENT recorded, lesson queued, later steps never run (`test_unforeseen_result_halts_recovers_and_stops_later_steps`, `test_exception_is_off_path_and_contained`).
- **Spec edited after freezing** → the hash differs; run refuses ("not frozen first, or changed after freezing") (`test_run_refused_when_not_frozen_changed_or_no_commitment`).
- **Malformed inputs** → malformed steps/names, bad requests, malformed specs/registries, bare-string source lists, odd build values are refusals, not crashes (`test_malformed_steps_and_names`, `test_bad_requests_are_refused`, `test_malformed_specs_and_registries_are_refusals`, `test_a_bare_string_is_not_a_list_of_repos`).
- **Damaged license matrix** → a clear error, not a permissive default (`test_a_damaged_matrix_file_is_a_clear_error_not_a_permissive_default`).
- **Failing lesson preflight** → run stops with the failure recorded; the check is never silently skipped (`test_a_failing_or_malformed_preflight_stops_the_run_instead_of_skipping_the_check`).

## 6. Dependencies and layer rules

Registry entry (verbatim): vega-harness-sdk — layer 6 conduct — owns "deterministic workflow conduct, pre-registered paths and the library use gate" — requires [vega-custody-sdk, vega-governance-jacket-sdk, vega-librarian-sdk] — status BUILT.

What each dependency provides, as actually imported in `src/`:

- **vega-custody-sdk** — `from vega_custody.dag import dag as dagmod` (records.py): the append-only, hash-chained, replica-checked chain, credential-shape refusal and cross-process safety.
- **vega-governance-jacket-sdk** — `from vega_jacket import humanauth` (conductor.py): the human-only classification of step names and verbs; the WCOMMIT ledger is consumed via the injected `commitment_check` (`records.make_ledger_check` wraps a jacket ledger).
- **vega-librarian-sdk** — `from vega_librarian import lesson_ledger as ll` (use_gate.py, inside `preflight`): the lesson ledger the preflight consults; it is also the conventional source of the Conductor's injected `preflight` callable.

Dependencies point down layers only, never up or sideways: custody (0), jacket (1) and librarian (5) all sit below conduct (6). Nothing in this kit imports a layer above it. Kits above that depend on this one (train, council) import it, not the reverse.

## 7. Conformance

A third party verifies this spec by running the tests: put `src` of this repo and of vega-custody-sdk, vega-governance-jacket-sdk and vega-librarian-sdk on `PYTHONPATH`, then run each file in `tests/` (`test_harness.py`, `redteam_1_conductor.py`, `redteam_2_path_gates_pipeline.py`). STATUS.md and REVIEW.md record 115 tests in 5 files, all passing.

**What passing means:** the published reference implementation behaves as specified here — gates refuse what they must, records chain and verify, failures are recorded rather than raised past the caller.

**What passing does NOT mean:** that the design is secure in your deployment (no independent security review; no expert review of the cryptography), that your integration wires every effect through a gate (an unwired path is outside the threat model), that the dep-injected stages exist in your build (unsupplied stages report NOT_BUILT), or that any legal conclusion follows from the license matrix.
