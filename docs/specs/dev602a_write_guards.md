# Artifact Write Guards (DEV-602 split A, run 20)

## Context

Target repo: **coding-model-server** (self). First of three right-sized specs
carved from the approved run-19 design (spec_d9024b79's design.md) after the
monolithic change surface exceeded implementer budgets. This spec is ONLY the
write-layer guards in the executor; the tested-manifest recorder and the
delivery verifier ship separately.

Run 17 delivered a corrupt branch because a later phase silently overwrote an
earlier phase's artifact at the same workspace path (last-writer-wins), and
run 17's stub replaced 419 real lines with a comment block. Details: DEV-602.

## Authoritative design — reproduce this in your design document

Extend `_write_artifact` in the executor to enforce two rules, in this order,
before writing to disk:

1. Collision check. Query the existing artifact records for the current
   attempt to find a previous producer of the same relative path. If a
   DIFFERENT role already produced it, refuse the write, keep the existing
   content, and record a `collision_refused` event carrying the path, the
   existing role, and the attempted role. A SAME-role rewrite is permitted
   and proceeds to the next check.
2. Emptying check. If the path exists with content containing one or more
   code declarations and the new content contains zero declarations, refuse
   the write, keep the existing content, and record an `emptying_refused`
   event carrying the path and the role. Otherwise write normally.

Declaration detection is a line-start keyword scan after stripping leading
whitespace, one list for all languages, no AST and no compilation. The
keywords are `def`, `class`, `async def`, `func`, `struct`, and the Swift
`@Test` attribute.

Guard state derives from existing artifact records scoped to the current
attempt — no new in-memory registries, no DB schema changes. A fresh attempt
starts clean. Guards record events and return refusal results; they never
raise exceptions that could kill the daemon loop.

## Change surface

| Path | Action |
|---|---|
| `src/coding_model_autonomous/executor.py` | modify — add the two write-layer guards described above |
| `tests/test_artifact_overwrite_guard.py` | new test file |

## Protected paths — must not be modified

- `src/coding_model_server/` (all of it, including orchestrator_daemon.py)
- `src/coding_model_autonomous/delivery.py`, `db.py`, `schema.sql`
- `src/coding_model_client/`
- All existing tests.

## Acceptance criteria (hermetic pytest, tmp-dir Database, no model calls)

- A1: implementer writes path P; reviewer writes same P in the same attempt →
  refused, implementer content intact, `collision_refused` event names P and
  both roles. A reviewer write to a fresh path Q succeeds.
- A2: role impl writes path Z containing one `def`; the SAME role impl
  rewrites Z with comment-only content → refused, `emptying_refused` event
  recorded, original content of Z intact. A same-role rewrite of Z with
  different real code (declarations present) succeeds.
- A3: the ordinary single-producer flow (each path written once) is
  byte-identical in behavior to today — no refusals, no new events.

## Constraints

- No new dependencies. No DB schema changes. Follow the
  `tests/test_diff_based_edits.py` fixture pattern.

## Test strategy (planner: carry these keys)

- `repo`: coding-model-server
- `framework`: pytest
- `protected_paths`: as listed above
