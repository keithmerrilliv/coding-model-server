# Existing-file fetch names the role it served (DEV-599 residual, run 23)

## Context

Target repo: **coding-model-server** (self). DEV-599 gave the architect the
same existing-file context the implementer gets, through the shared helper
`_fetch_existing_files_for_spec` in `src/coding_model_server/orchestrator_daemon.py`.
That helper's log lines were written when only the implementer called it,
so every one of them says "implementer" — including the line the ARCHITECT's
fetch emits. Run 21's design phase logged
`supplied 1 existing file(s) to the implementer: src/coding_model_server/orchestrator_daemon.py`
at 21:22:02 on 2026-09-05, six minutes before any implementer ran. A reader
of the journal cannot tell which role a fetch served, and DEV-599's live
proof stayed invisible because of it.

## Authoritative design — reproduce this in your design document

1. Add a keyword-only parameter `role: str = "implementer"` to
   `_fetch_existing_files_for_spec`. The default keeps every existing call
   site byte-for-byte in behaviour and wording.
2. Every log message inside that helper that names the implementer must
   name `role` instead. There are three: the read-failed warning
   ("the implementer will not see the files it must modify"), the
   "supplied N existing file(s) to the implementer: ..." info line, and the
   "implementer is working blind" warning. Only the role word changes;
   keep the rest of each message identical so existing journal greps
   still match.
3. `_run_architect` passes `role="architect"` in its call to the helper.
   No other call site changes.

## Change surface

| Path | Action |
|---|---|
| `src/coding_model_server/orchestrator_daemon.py` | modify — `role` parameter on the fetch helper, three log messages use it, the architect call passes it |
| `tests/test_existing_fetch_role_log.py` | new test file |

## Protected paths — must not be modified

- `src/coding_model_autonomous/` (all of it)
- `src/coding_model_client/`
- All existing tests.

## Acceptance criteria (hermetic pytest, tmp workspace, no model calls, no runner)

Tests monkeypatch `test_runner.fetch_repo_files` on the daemon module (it is
imported there as `test_runner`) and use pytest's `caplog` at INFO level.

- R1: with the fetch returning one file and the helper called with
  `role="architect"`, the log contains
  `supplied 1 existing file(s) to the architect:` and does NOT contain
  `to the implementer`.
- R2: the same call with no `role` argument logs `to the implementer:`
  — the default preserves today's wording exactly.
- R3: with the fetch raising, `role="architect"` logs a warning containing
  `the architect will not see the files it must modify` and the helper
  returns an empty list (it never raises).
- R4: the helper's signature exposes `role` as keyword-only with default
  `"implementer"` (`inspect.signature`).

## Constraints

- No new dependencies. No DB schema changes. No change to the helper's
  return value, its exception behaviour, or its DEV-620 outage handling.
- The spec needs a `test_strategy.repo` of `coding-model-server` so the
  existing files are fetched; the plan must carry the `repo` and
  `protected_paths` keys exactly as written below.

## test_strategy

```yaml
repo: coding-model-server
framework: pytest
required: true
protected_paths:
  - src/coding_model_autonomous/
  - src/coding_model_client/
```
