# Delivery Manifest Verification (DEV-602 split C, run 22)

## Context

Target repo: **coding-model-server** (self). Third of three specs carved from
the approved run-19 design (spec_d9024b79). This spec ONLY adds the
delivery-side verification; it reads the `tested_manifest.json` that split B
records, and an ABSENT manifest means delivery proceeds exactly as today —
so this spec is safe to ship before, after, or without split B.

Run 17 delivered files that were not the files the build check had tested;
delivery compared nothing. Details: DEV-602.

## Authoritative design — reproduce this in your design document

Extend the delivery module:

1. Before pushing, read `tested_manifest.json` from the spec workspace. If
   the file is absent, proceed exactly as today (backward compatible).
2. If present, recompute the SHA-256 of every artifact in the shipping set.
   On ANY divergence — a hash mismatch, a manifest path missing from the
   shipping set, or a shipped file absent from the manifest — refuse the
   delivery with a loud non-pushed status, and write `delivery_report.md`
   listing every divergent path with its tested and shipped hashes (or the
   word "missing" for an absent side).
3. On an exact match, deliver identically to today and note the verified
   file count in the delivery report.

A refused delivery must be visibly refused in the run record (the existing
delivery event path already records status and detail — a refusal must never
read as a clean pushed run). Failures never raise out of the daemon loop.

## Change surface

| Path | Action |
|---|---|
| `src/coding_model_autonomous/delivery.py` | modify — manifest verification before push, report detail |
| `tests/test_delivery_manifest_verify.py` | new test file |

## Protected paths — must not be modified

- `src/coding_model_server/` (all of it, including orchestrator_daemon.py)
- `src/coding_model_autonomous/executor.py`, `db.py`, `schema.sql`
- `src/coding_model_client/`
- All existing tests.

## Acceptance criteria (hermetic pytest, tmp workspace, no model calls, no git remotes)

- C1: with a manifest present and one listed file mutated, delivery refuses
  with a non-pushed status and `delivery_report.md` names the divergent path
  with both hashes.
- C2: with a manifest present and hashes matching exactly, delivery proceeds
  identically to baseline behavior.
- C3: with no manifest file, delivery behavior is byte-identical to today.
- C4: a manifest path absent from the shipping set (or vice versa) is a
  divergence per criterion C1.

## Constraints

- No new dependencies (`hashlib` only). No DB schema changes. Verification
  logic must be testable without a reachable git remote — factor it so tests
  exercise the compare-and-report step directly.

## test_strategy

```yaml
repo: coding-model-server
framework: pytest
required: true
protected_paths:
  - src/coding_model_server/
  - src/coding_model_autonomous/executor.py
  - src/coding_model_autonomous/db.py
  - src/coding_model_autonomous/schema.sql
  - src/coding_model_client/
```
