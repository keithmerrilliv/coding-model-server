# Phase 3 — Autonomous Mode Proposal (DRAFT)

**Status:** working draft for amendment. Not signed off. Each section is a
starting point — strike, replace, or extend as you see fit.

**Context:** Phase 2 shipped the architect → implementer → reviewer pipeline
with Jira sync. Today's manual testing exercised it end-to-end and surfaced
several real bugs (system-prompt collision, sandbox priority, reviewer
hallucination, retry feedback noise) which were patched in flight. Phase 3
is about taking the working pipeline and making it *measurable, predictable,
and harder to break*.

---

## Headline feature: telemetry for continuous improvement

Today there is no way to answer "is this getting better?" The only signal is
"did this spec finish?" That makes it hard to:
- Decide whether a model upgrade actually helped, or just shifted failure modes
- Catch regressions when prompts or parsers change
- Justify the cost of a heavier model (deep_implementer vs implementer) with
  a number rather than a hunch

### What to collect (per spec, per phase)

The orchestrator already records `events` rows. Telemetry sits *on top of*
that, denormalizing into a per-spec summary plus per-phase rows for fast
querying and historical comparison. Initial schema sketch (sqlite, same DB
or a sibling):

| Field | Type | Source |
|-------|------|--------|
| `spec_id` | text | events |
| `phase` | text | events (`planner`, `architect`, `implementer`, `reviewer`, `tests`) |
| `agent` | text | task_created.payload.agent |
| `started_at` / `ended_at` | iso ts | task_status_changed |
| `outcome` | text | `done` / `failed` / `parse_error` / `timeout` / `crashed` |
| `retry_count` | int | task.retry_count |
| `tokens_in` / `tokens_out` | int | qwen-server response (need to capture & forward) |
| `tests_passed` / `tests_failed` / `tests_collected` | int | parsed from pytest output |
| `guard_fired` | text | which Layer (1/2/3) downgraded a verdict, if any |
| `failure_reason` | text | one-line classification (`bwrap_exec_error`, `parse_error`, `tests_failed_real`, `connection_aborted`, …) |
| `wall_time_seconds` | int | derived |

### Surface layer

- A `qwen-autonomous metrics` subcommand: per-spec table, success rate,
  median wall time per phase, retry distribution.
- Optionally an HTML/markdown weekly report generated from the same table.
- Don't over-build a UI; for a single-user dev box, a `metrics` CLI plus the
  raw sqlite is enough.

### Why this lands first

Every other Phase 3 item below benefits from being measurable. "Did the
diff-based retry reduce wall time?" is unanswerable without baseline
numbers. Build the meter first; tune against it second.

---

## Candidate items (numbered for easy amendment)

### 1. Telemetry & continuous-improvement loop  *[headline; see above]*

### 2. Reviewer-after-tests verdict
Today the reviewer's verdict is *static code review* and is overridden by
`tests_passed` (the pre-existing AND-check). Real benefit if the reviewer
runs *after* pytest and gets the test output as input — its verdict could
then cite specific failures by file:line rather than rely on heuristics.
Closes the largest remaining hallucination axis.

**Trade-off:** doubles reviewer LLM cost (or requires structuring as a
two-pass call). May not be worth it if telemetry shows hallucinations are
rare in practice — measure first, decide second.

### 3. Spec linter / pre-flight check
A fast pass between `submit` and the planner that flags ambiguous spec
language before paying for a 4-retry exhaustion. Today's punctuation
example ("Strips common punctuation" — delete vs. replace-with-space)
would have been caught with one disambiguation pass.

**Implementation:** small dedicated agent or rule-based linter for common
ambiguity patterns (passive voice on transformations, list-without-handler
clauses, missing edge-case definitions).

### 4. Diff-based retry
Currently the implementer rewrites every file from scratch on retry. If
only `wordfreq.py:42` is wrong, asking for the full file again wastes
tokens AND lets the model re-introduce already-fixed bugs in unrelated
sections. Switch retry mode to diff-against-previous-attempt + the
specific failing tests.

**Risk:** diff parsing is fragile. Need a clean format and rejection path
when the model flubs the diff syntax.

### 5. Crash recovery / persistent queue
Today's qwen-server segfault during deep_reviewer left an orphan task that
the daemon recovered via "stuck in RUNNING → reset to PENDING" — but the
inference itself was lost and the spec failed at retry exhaustion. A
per-call retry on transient transport errors (`ConnectionError`,
`RemoteDisconnected`) would have masked the crash entirely (qwen-server
auto-restarts in 10s).

### 6. Multi-language test runners
Pytest/jest local; Swift via mac-runner. Adding `cargo test`, `go test`,
and `swift test` (linux) to `_run_local_tests` would expand reach without
much surface-area cost. The framework selection already routes by
`test_strategy.framework`.

### 7. Concurrency: model-aware scheduler
Single-inference-at-a-time today (sequential lock on qwen-server). A
queue that batches specs by *required model* could amortize the model
load cost — load q36_architect once, run 3 architects, swap once to
deep_implementer, run 3 implementers. Big wall-time win if you ever push
multiple specs through.

**Caveat:** complicated. Defer until usage justifies it.

### 8. Smarter retry feedback (partially shipped)
Already shipped today: actionable test output extraction
(`_extract_actionable_test_output`). Future passes could:
- Quote the specific assertion lines verbatim with surrounding context
- Strip already-passing tests to keep retry messages focused
- Extract reviewer `Notes` field separately and weight it highly

### 9. Auto-disambiguation via clarification round
The planner has a `<<<CLARIFY>>>` path; today it asks at most once. For
complex specs, allowing 2-3 clarification rounds (with the human or with
a second reviewer agent) would catch ambiguity earlier and cheaper than
retry-exhaustion.

### 10. *(reserved — your additions go here)*

---

## Out of scope for Phase 3

- Web UI / dashboard for spec management (sqlite + CLI is enough)
- Distributed orchestrator (single host, single GPU)
- Cost tracking / billing (irrelevant on a self-hosted box)
- Generic agent framework abstractions (this is application code, not a library)

## Open questions

- **Telemetry storage**: same sqlite or a sibling `metrics.sqlite`? Keeping
  a single DB simplifies queries; splitting protects telemetry from
  schema churn on the events table.
- **Telemetry granularity**: per-phase or per-LLM-call? Per-call gives
  finer attribution but doubles row count.
- **Retention**: rolling 90 days? Keep forever? On a dev box, "keep
  forever" is fine until the disk groans.
- **Reviewer-after-tests** (#2): worth the cost? Decide after telemetry
  shows hallucination rate.

## Suggested sequencing

1. **Telemetry schema + collection** (#1) — lands first, every other item
   benefits from baseline numbers.
2. **Crash recovery** (#5) — small change, big reliability win, cheap to
   measure once telemetry exists.
3. **Reviewer-after-tests** OR **diff-based retry** — pick one based on
   what telemetry says is the bigger pain.
4. **Spec linter** (#3) — quality-of-life improvement once the core loop
   is reliable.

Everything else is candidate, not committed.
