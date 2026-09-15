# A spec that ends retires its open gates (DEV-653)

## Context

Target repo: **coding-model-server** (self). No terminal path retires a spec's
open review gates. `Database.cancel_gate` exists and is correct, but the two
terminal transitions — `outcome.terminate` setting FAILED, and every other
`update_spec_status(..., FAILED | CANCELLED | DONE)` site — never call it. Two
`code_review` gates sat PENDING for 30 days on specs cancelled the same day:
the daemon warned about them on every tick (DEV-430's stalled-spec signal
turned into background noise) and jira-sync kept polling their AUTO issues,
where a tidy-up close in the Jira UI would have reverse-synced as an
APPROVAL of dead work (DEV-149). The operator cancel (DEV-583) retires gates
itself before it sets CANCELLED; nothing else does.

The fix lives at the one choke point every terminal transition already goes
through: `Database.update_spec_status`. When the status it just wrote is
terminal, it retires every gate still PENDING on that spec through
`cancel_gate` — the existing primitive that bypasses `respond_to_gate`'s
PENDING-only CAS, records `GATE_RESPONDED` `decision=cancelled`, and drops the
gate out of `list_open_gates` so reverse-sync can no longer reach it. The
forward sync then pushes the AUTO issue closed on its own (jira-sync's
`_handle_gate_responded` transitions a non-approved gate to Rejected; a gate
with no reviewer notes posts no comment). DEV-372 (a decided-then-consumed
gate re-posting its reviewer notes) is a separate defect in jira-sync and is
NOT in scope here.

## Authoritative design — reproduce this in your design document

All edits are in `src/coding_model_autonomous/db.py`. No other source file
changes.

1. Add a module-level constant immediately before the line `class Database:`
   (leave one blank line on each side):
   `TERMINAL_SPEC_STATUSES = (SpecStatus.DONE, SpecStatus.FAILED, SpecStatus.CANCELLED)`
   preceded by a two-line comment naming DEV-653: a spec in one of these
   statuses can never act on a gate again.

2. Add a method `retire_open_gates(self, spec_id: str) -> list[str]` to
   `Database`, placed immediately after the existing `cancel_gate` method.
   Behaviour:
   - `gates = self.list_open_gates(spec_id)`.
   - For each gate, in that order, call `self.cancel_gate(gate.id)`.
   - If any were retired, log ONE line at WARNING:
     `"spec %s: %d open gate(s) retired on terminal status: %s (DEV-653)"`
     with the spec id, the count, and the gate ids joined by `", "`.
   - Return the list of retired gate ids (empty when there were none; no log
     line in that case).

3. In `update_spec_status`, the successful path currently ends inside the
   `with self.transaction() as conn:` block with `self._record_event(...)`
   followed by `return True`. Change it so that the `return True` moves OUT of
   the `with` block (the transaction commits when the block ends), and
   between the end of the block and `return True` insert:
   ```python
   if status in TERMINAL_SPEC_STATUSES:
       self.retire_open_gates(spec_id)
   ```
   The early `return False` (the DEV-567 discarded write) stays exactly where
   it is, inside the block — a discarded write retires nothing. The
   `SPEC_STATUS_CHANGED` event is therefore recorded BEFORE the gate
   cancellations, in its own committed transaction; each `cancel_gate` then
   runs in its own transaction as today.
   Nothing else in `update_spec_status` changes: the signature, the
   `force`/CAS guard, the `event_payload` merge (DEV-679) and the docstring's
   existing text stay as they are. Add one sentence to the docstring naming
   DEV-653.

4. In `cancel_spec`, the status write (`self.update_spec_status(spec_id,
   SpecStatus.CANCELLED, event_payload=...)`) comes BEFORE the gate loop
   (DEV-567/DEV-679 ordering), so the new hook has already retired the open
   gates by the time the loop runs. Replace the loop
   ```python
           for gate in gates:
               self.cancel_gate(gate.id)
   ```
   with
   ```python
           # DEV-653: the status write above already retired the open gates;
           # cancel only what is still open (normally nothing) so no gate
           # gets a second GATE_RESPONDED event.
           for gate in self.list_open_gates(spec_id):
               self.cancel_gate(gate.id)
   ```
   Everything else in `cancel_spec` is unchanged: the `gates` list computed
   before the status write still feeds `gates_cancelled` in the event payload
   and the returned summary. (Run 36's first artifact found this: the spec
   originally claimed the loop ran before the write, and T8 recorded two
   cancelled events per gate.)

5. The test file imports the code under test by its PACKAGE name, and the
   design's Criterion Seams quote the import lines they rely on as shared
   setup (for example
   `from coding_model_autonomous.db import Database, TERMINAL_SPEC_STATUSES`
   and
   `from coding_model_autonomous.models import EventKind, GateStatus, GateType, SpecStatus, TaskStatus`).
   In the test sandbox the repository's `src/` directory is the package root
   on `sys.path`; it is NOT a package, so `from src.coding_model_autonomous…`
   fails at collection with `ModuleNotFoundError`. Never import through `src.`.

## Change surface

| Path | Action |
|---|---|
| `src/coding_model_autonomous/db.py` | modify — add `TERMINAL_SPEC_STATUSES`, `Database.retire_open_gates`; restructure the tail of `update_spec_status` |
| `tests/test_terminal_retires_gates.py` | new test file |

## Protected paths — must not be modified

- `src/coding_model_autonomous/models.py`
- `src/coding_model_autonomous/outcome.py`
- `src/coding_model_autonomous/jira_sync.py`
- `src/coding_model_server/orchestrator_daemon.py` (read-only; it is NOT supplied as context — the change does not need it)
- All existing tests.

## Test scaffolding

The test file builds its store the way `tests/test_cancel_spec.py` does:

```python
@pytest.fixture
def db(tmp_path):
    database = Database(db_path=tmp_path / "t.sqlite", workspace_root=tmp_path / "ws")
    yield database
    database.close_all()
```

A helper `_spec_with_gates(db, n)` creates a spec with
`db.create_spec(title="demo", source_md_path="spec.md")`, writes
`db.spec_dir(spec.id) / "spec.md"`, sets it EXECUTING via
`db.update_spec_status(spec.id, SpecStatus.EXECUTING)`, creates one
implementer task with `db.create_task(spec_id=spec.id, agent="implementer", role="implementer", title="b")`,
and opens `n` gates with `db.create_gate(spec_id=spec.id, gate_type=GateType.CODE_REVIEW, prompt_md="## review")`.
It returns `(spec, [gates])`.

`cancelled_events(db, spec_id)` returns the payloads of
`db.list_events_by_kind(spec_id=spec_id, kind=EventKind.GATE_RESPONDED)`
whose `payload["decision"] == "cancelled"` (each `Event` has `.payload`, a
dict, and `.gate_id`).

## Acceptance criteria (hermetic pytest, no model calls, no runner, no network)

- **T1** — `TERMINAL_SPEC_STATUSES == (SpecStatus.DONE, SpecStatus.FAILED, SpecStatus.CANCELLED)`.
- **T2** — on a spec with two open gates, `db.update_spec_status(spec.id, SpecStatus.FAILED)` returns True; afterwards `db.list_open_gates(spec.id) == []`, `db.get_gate(g.id).status is GateStatus.CANCELLED` for both, and `cancelled_events` has exactly two entries whose `gate_id`s are the two gate ids.
- **T3** — the same with `SpecStatus.CANCELLED` (one open gate → one cancelled event).
- **T4** — the same with `SpecStatus.DONE` (one open gate → retired).
- **T5** — a NON-terminal write (`SpecStatus.PLAN_REVIEW` on a spec with one open gate) leaves the gate PENDING (`db.get_gate(g.id).status is GateStatus.PENDING`) and records no cancelled event.
- **T6** — `db.retire_open_gates(spec.id)` on a spec whose gates were already retired returns `[]`, and the total number of cancelled events does not change.
- **T7** — isolation: two specs A and B each with one open gate; `update_spec_status(A, FAILED)` leaves B's gate PENDING.
- **T8** — `db.cancel_spec(spec.id, reason="drill")` on a spec with one open gate leaves exactly ONE cancelled event for that gate (the hook does not double-cancel).
- **T9** — the retire logs at WARNING: with `caplog.at_level(logging.WARNING)`, after T2's write some record's `getMessage()` contains both `"retired on terminal status"` and `"DEV-653"`; after T6's no-op call no NEW record contains `"DEV-653"`.
- **T10** — the `SPEC_STATUS_CHANGED` event for the terminal write is recorded BEFORE the cancelled gate events: in `db.list_events_by_kind(spec_id=spec.id, kind=EventKind.SPEC_STATUS_CHANGED)` the newest entry has `payload["new_status"] == "failed"`, and every cancelled `GATE_RESPONDED` event's `.id` is greater than that entry's `.id`.

## Constraints

- No new dependencies. No change to `cancel_gate`, `list_open_gates`,
  `create_gate` or any model; `cancel_spec` changes only as step 4 says.
- The plan must carry the `repo` and `protected_paths` keys exactly as written
  below, so the existing file is fetched at `base_ref`.

## test_strategy

```yaml
repo: coding-model-server
framework: pytest
required: true
protected_paths:
  - src/coding_model_autonomous/models.py
  - src/coding_model_autonomous/outcome.py
  - src/coding_model_autonomous/jira_sync.py
```
