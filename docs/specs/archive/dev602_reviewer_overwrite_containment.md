# Reviewer-Overwrite Containment (DEV-602 dogfood, run 19)

## Context

Target repo: **coding-model-server** (this repository — a self-target run).

Run 17 (spec_74c42cc8) delivered a corrupt branch: the implementer produced a
correct 419-line test suite, the pre-gate build check compiled and ran exactly
that file (34 tests green), and then the reviewer phase wrote its own file at
the **same workspace path** — a 6-line comment stub. Artifact writes are
last-writer-wins on disk, so `_deliver_completed_spec` shipped the stub. Every
gate told the truth about a file that no longer existed when delivery ran;
nothing compared what was tested with what was shipped. Full evidence: DEV-602.

This spec closes that hole with three guards. It does NOT implement
retry-history code retention (DEV-602's fix item 4) — that is recovery
hygiene, deliberately out of scope for this run.

## Required behavior

1. **A same-path artifact collision is an error, not a silent overwrite.**
   Within a single attempt, when a later phase produces an artifact at a
   relative path an earlier phase already produced, the write is refused: the
   earlier artifact's content survives, and an event is recorded naming the
   path and BOTH producing roles. Legitimate cases are unaffected: a retry
   attempt regenerating its own files, and a reviewer writing test files at
   paths the implementer did not produce (DEV-563) both proceed as today.

2. **Delivery ships what was tested, or says why not.** When the pre-gate
   build check passes, record a manifest of the code-artifact set it verified
   (relative path + content hash) — as an event payload and/or a workspace
   file, NOT a DB schema change. At delivery time, recompute hashes over the
   artifact set being shipped; on any divergence, delivery refuses (a loud
   non-`pushed` status) and `delivery_report.md` names each divergent path
   with tested-vs-shipped hashes. A spec whose delivery was refused must not
   read as a clean `done`-and-delivered run.

3. **An emptying overwrite is content destruction and is rejected.** A phase
   output that would replace an existing artifact having one or more code
   declarations (e.g. `def`/`class` in Python, `func`/`struct`/`@Test` in
   Swift) with content having ZERO declarations is refused and recorded as a
   harness-side rejection charged to the producing phase — the same shape as
   the DEV-573 fabricated-stub defect. Comment-only and prose-only
   replacements are exactly the run-17 signature.

## Change surface

The implementation may modify ONLY these files (plus new test files):

| Path | What changes here |
|---|---|
| `src/coding_model_autonomous/executor.py` | `_write_artifact` / artifact recording: collision refusal (behavior 1) and the emptying-overwrite check (behavior 3) |
| `src/coding_model_server/orchestrator_daemon.py` | record the tested-artifact manifest at the passing pre-gate build check; route delivery refusal so the run record shows it (behavior 2) |
| `src/coding_model_autonomous/delivery.py` | hash comparison before push; divergence detail into the delivery report (behavior 2) |
| `tests/` (new files) | `test_artifact_overwrite_guard.py`, `test_deliver_what_was_tested.py` |

## Protected paths — must not be modified

- `src/coding_model_server/config.py` (agent registry / model configs)
- `src/coding_model_server/llama_server.py`, `src/coding_model_server/routes/`
- `src/coding_model_autonomous/schema.sql` and `db.py` schema — persist the
  manifest as an event payload or workspace file, never a schema migration
- `src/coding_model_client/` (client is untouched by this concern)
- All existing tests: this change must not weaken or rewrite any existing
  test; new coverage lives in the new test files.

## Testing

pytest from the repo root; tests run in the bwrap sandbox — hermetic, tmp-dir
Databases and workspaces, no network, no GPU, no model calls (follow the
`tests/test_diff_based_edits.py` construction pattern).

Required coverage, one criterion per test at minimum:

- **A1 (collision):** implementer artifact at path P, then a reviewer artifact
  at the same P in the same attempt → refused; implementer content intact; an
  event names P and both roles. A reviewer artifact at a NEW path is accepted.
- **A2 (manifest):** a passing pre-gate build check records path+hash for the
  verified code artifacts; mutating one artifact afterwards makes delivery
  refuse, with the divergent path and both hashes present in the delivery
  report content. An unmutated set delivers exactly as today.
- **A3 (emptying overwrite):** replacing an artifact containing declarations
  with declaration-free content is refused and recorded; replacing it with
  different-but-real code (declarations present) is NOT caught by this guard.
- **A4 (regression):** the ordinary single-producer flow — implement, test,
  deliver — is byte-identical in behavior to today.

## Constraints

- No new dependencies. Hash with `hashlib` from the standard library.
- Guard failures must be loud in the run record (events / report), never
  exceptions that kill the daemon loop.
- Keep the collision and emptying checks at the artifact-write layer so every
  phase (reviewer, synthesis, repair) passes through them — do not special-case
  the reviewer.

## Test strategy (for the planner — carry these keys through)

- `repo`: coding-model-server (self)
- `framework`: pytest
- `protected_paths`: as listed in “Protected paths” above
