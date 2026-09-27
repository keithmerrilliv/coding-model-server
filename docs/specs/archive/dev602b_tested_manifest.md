# Tested-Artifact Manifest Recording (DEV-602 split B, run 21)

## Context

Target repo: **coding-model-server** (self). Second of three specs carved
from the approved run-19 design (spec_d9024b79). This spec ONLY records the
manifest of what the pre-gate build check verified; the delivery-side
verification against it ships separately (split C) and treats an absent
manifest as backward-compatible pass-through, so the two specs are decoupled.

Run 17's corrupt delivery shipped files that were not the files the build
check had tested; nothing recorded what WAS tested. Details: DEV-602.

## Authoritative design — reproduce this in your design document

Hook the pre-gate build check success path in the orchestrator daemon:

1. When the pre-gate build check PASSES, compute the SHA-256 hex digest of
   every verified code artifact and write `tested_manifest.json` into the
   spec workspace directory — a JSON object mapping each relative artifact
   path to its digest. This file is the operative copy a later delivery
   verifier reads.
2. Mirror the same path-and-hash list into the pre_gate_build_check event
   payload as the queryable run record.
3. A FAILING build check writes no manifest and removes none — the manifest
   always describes the most recent PASSING check.

Hashing uses hashlib.sha256 over file bytes. No DB schema changes; the event
payload uses the existing record_event path. Failures to write the manifest
file are logged loudly but never raise out of the daemon loop.

## Change surface

| Path | Action |
|---|---|
| `src/coding_model_server/orchestrator_daemon.py` | modify — record the manifest at the passing pre-gate build check |
| `tests/test_tested_manifest_recording.py` | new test file |

## Protected paths — must not be modified

- `src/coding_model_autonomous/` (all of it, including executor.py and delivery.py)
- `src/coding_model_server/config.py`, `llama_server.py`, `routes/`
- `src/coding_model_client/`
- All existing tests.

## Acceptance criteria (hermetic pytest, tmp-dir Database, no model calls)

- B1: after a passing pre-gate build check, `tested_manifest.json` exists in
  the spec workspace and maps each verified artifact path to the SHA-256 of
  its bytes.
- B2: the pre_gate_build_check event payload for that pass carries the same
  path-and-hash list.
- B3: a failing build check writes no manifest, and an existing manifest from
  an earlier passing check is left untouched.

## Constraints

- No new dependencies (`hashlib` only). No DB schema changes. Follow the
  `tests/test_diff_based_edits.py` fixture pattern.

## test_strategy

```yaml
repo: coding-model-server
framework: pytest
required: true
protected_paths:
  - src/coding_model_autonomous/
  - src/coding_model_server/config.py
  - src/coding_model_server/llama_server.py
  - src/coding_model_server/routes/
  - src/coding_model_client/
```
