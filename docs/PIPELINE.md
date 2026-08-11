# How the autonomous pipeline works

A spec goes in as markdown; code comes out, or the run fails with a reason. This
document is the map.

**Is it a state machine?** Partly. The lifecycle below is a genuine state
machine and diagram 1 is the whole of it. But two things drive behaviour that a
state machine cannot express, and they are where the complexity actually lives:

- **The transition is chosen by a classifier, not by the state.** One test
  dispatch produces output that is sorted into six outcomes, and the outcome
  decides where control goes. Same state, same event, six destinations
  (diagram 3).
- **Budgets are orthogonal counters that gate transitions.** The same state and
  the same event lead to different places depending on a counter that is
  nowhere in the state (the budget table, and diagram 4).

So: a state machine for the spine, a decision tree for the diagnosis, and a
table for the accounting. Any one of the three alone is misleading.

---

## 1. Spec lifecycle

The outer state machine. One spec, one row in `specs`, these statuses.

```mermaid
stateDiagram-v2
    [*] --> pending_plan: spec submitted
    pending_plan --> needs_clarification: planner has questions
    needs_clarification --> pending_plan: human answers
    pending_plan --> plan_review: plan.yaml produced
    plan_review --> pending_plan: rejected (replan)
    plan_review --> executing: approved
    executing --> done: release gate approved
    executing --> failed: budget exhausted / unrecoverable
    executing --> cancelled: operator cancels
    done --> [*]
    failed --> [*]
    cancelled --> [*]
```

`plan_review` is also entered automatically when plan validation rejects the
plan itself — up to `PLAN_VALIDATION_MAX_ROUNDS` (2) times, with no human
involved. A third failure fails the spec before anyone sees it.

---

## 2. Inside `executing`: three tasks, four gates

`executing` bootstraps three tasks — architect, implementer, reviewer — which
run in order. Each has its own `retry_count`. Human gates are the diamonds.

```mermaid
flowchart TD
    START([plan approved]) --> ARCH[architect<br/>produces design.md]
    ARCH --> TEST{testability check<br/>max 2 rounds}
    TEST -->|findings, rounds left| ARCH
    TEST -->|clean or rounds spent| DREV[automated design review<br/>max 1 revision]
    DREV --> DGATE{design_approval<br/>HUMAN}
    DGATE -->|rejected + notes| ARCH
    DGATE -->|approved| IMPL[implementer<br/>writes the files]

    IMPL --> BUILD[/pre-gate build check<br/>dispatch to runner/]
    BUILD --> CLASS{classify the output<br/>see diagram 3}
    CLASS -->|clean| CGATE{code_review<br/>HUMAN}
    CLASS -->|build failed / blocking warning| IMPL
    CLASS -->|same diagnostic twice| ARCH
    CLASS -->|runner unreachable| BUILD

    CGATE -->|rejected + notes| IMPL
    CGATE -->|approved| REV[reviewer<br/>runs the real suite]
    REV --> RGATE{release_approval<br/>HUMAN}
    RGATE -->|approved| DONE([done])
    RGATE -->|rejected| IMPL

    IMPL -.->|retry_count reaches 5| SYNTH[synthesis<br/>merge all attempts]
    SYNTH --> REPAIR{one repair round<br/>see diagram 4}
    REPAIR -->|passes| RGATE
    REPAIR -->|still failing| FAIL([failed])
```

Two edges are worth naming because they are the ones people miss:

- **`CLASS --> ARCH`** is the upstream route. When the same *located* diagnostic
  survives repeated implementer attempts, the implementer is not the author of
  the defect — the design is, and the design is re-read unchanged on every
  retry, so no number of implementer attempts can converge. The implementer is
  not charged for that attempt.
- **`IMPL -.-> SYNTH`** is the escape hatch. Exhausting the retry budget does
  not fail the spec; the accumulated attempts are merged, because the union of
  six near-misses is often closer to correct than any single one of them.

---

## 3. Classifying one test dispatch

This is the part a state machine cannot draw. A single runner call returns
`(passed, output)`, and *six* different conclusions can follow. Order matters —
each check exists because a real run was misdiagnosed without it.

```mermaid
flowchart TD
    IN[/runner returns passed + output/] --> P{passed?}
    P -->|yes| OK([compiled, suite green<br/>→ human code_review gate])
    P -->|no| ATTR{"a diagnostic naming<br/>file:line:col?"}

    ATTR -->|yes| BF([BUILD FAILURE<br/>→ implementer, charged])
    ATTR -->|no| DRV{"compile-stage driver error?<br/>emit-module / compile /<br/>link command failed, fatalError"}

    DRV -->|yes| BF
    DRV -->|no| DONEMARK{"bare error AFTER<br/>'Build complete!'?"}

    DONEMARK -->|no| BF
    DONEMARK -->|yes| CRASH{"test process died<br/>on a signal?"}

    CRASH -->|yes| TRAP([COMPILED, THEN CRASHED<br/>runtime defect, not a build one])
    CRASH -->|no| WARN{"blocking warning on<br/>a generated file?"}

    TRAP --> WARN
    WARN -->|yes| WB([WARNING BLOCK<br/>→ implementer, charged])
    WARN -->|no| SUMM{"a test summary<br/>in the output?"}

    SUMM -->|yes| TF([TEST FAILURE<br/>behaviour → human gate])
    SUMM -->|no| UNREACH{"runner unreachable?"}

    UNREACH -->|yes| RQ([INCONCLUSIVE<br/>→ requeue, NOT charged])
    UNREACH -->|no| INC([INCONCLUSIVE<br/>→ human gate, build unverified])
```

Why each branch exists, since every one of them is a scar:

| Branch | The failure it prevents |
|---|---|
| `passed?` first | Some suites legitimately print the word "error"; a green suite proves the build was fine. |
| attributed before driver | Cascade errors in test files are consequences; the module-level cause has no file:line. |
| compile-stage driver errors | `emit-module command failed` is a real build failure with no attribution — it must not be demoted. |
| the `Build complete!` ordering test | The harness prints `error:` long after the build finished. Without this, a run that compiled and then trapped was reported to the model as "your code does not compile". |
| crash as its own outcome | Under a parallel runner one trap takes every test down, so "no summary" is not "we learned nothing". |
| warnings | The compiler frequently names the defect on the exact line and nothing was reading it. |
| unreachable → requeue | A sleeping laptop is not the implementer's mistake, and must not spend its budget. |

---

## 4. Where a failure goes, and who pays

Same failure, three destinations, decided by evidence rather than by state.

```mermaid
flowchart LR
    F[/attempt failed/] --> Q1{same located diagnostic<br/>as the previous attempt?}
    Q1 -->|yes| A[route to ARCHITECT<br/>implementer NOT charged]
    Q1 -->|no| Q2{implementer<br/>retry_count &lt; 5?}
    Q2 -->|yes| I[retry IMPLEMENTER<br/>charged, notes attached]
    Q2 -->|no| S[SYNTHESIS<br/>merge every attempt]
    S --> Q3{does it build?}
    Q3 -->|no| R1[repair round<br/>aimed at the diagnostic]
    Q3 -->|"yes, but ≥80% tests pass"| R2[repair round<br/>aimed at the failures]
    Q3 -->|"yes, but &lt;80%"| X([fail — too far from passing<br/>to be worth a call])
    R1 --> V{did the repair<br/>strictly reduce diagnostics?}
    R2 --> V
    V -->|yes| K[keep it]
    V -->|no| RB[ROLL BACK<br/>restore pre-repair files]
    RB --> X
```

The rollback exists because a repair round once fixed the defect it was given
and simultaneously stripped `import Foundation` from every file, taking the
build from 3 errors to 14. Getting the target right is not sufficient.

---

## 5. Budgets

The counters that decide whether a transition is available at all. This is the
table to read first when a run ends somewhere surprising.

| Budget | Default | Env var | What it bounds |
|---|---|---|---|
| `MAX_RETRIES` | 5 | `AUTONOMOUS_MAX_RETRIES` | Per-task retries. Shared by human rejections, automated rotations and crash recovery. |
| Testability rounds | 2 | `AUTONOMOUS_TESTABILITY_CHECK_MAX_ROUNDS` | Architect revisions the testability check may force. |
| Design review revisions | 1 | `AUTONOMOUS_DESIGN_REVIEW_MAX_REVISIONS` | Automated design-review rejections. |
| Plan validation rounds | 2 | `AUTONOMOUS_PLAN_VALIDATION_MAX_ROUNDS` | Automatic replans before the spec fails. |
| Upstream-routing threshold | 1 | `AUTONOMOUS_BUILD_FAILURE_ARCHITECT_THRESHOLD` | Consecutive identical diagnostics before the design is blamed. |
| Unreachable-runner requeues | 3 | — | Consecutive free requeues before escalating to a human. |
| Synthesis repair rounds | 1 | — | Hard-coded. One repair, then the run ends. |
| Repair pass-rate floor | 0.8 | `AUTONOMOUS_SYNTHESIS_REPAIR_MIN_RATE` | Below this, a repair call is not worth making. |
| Parse retries | 2 / 2 | `AUTONOMOUS_ARCHITECT_PARSE_RETRIES`, `AUTONOMOUS_PER_FILE_PARSE_RETRIES` | Malformed agent output before giving up. |

**Three traps in the accounting**, each of which has cost a real run:

1. **`retry_count` is shared.** Human design rejections, the testability check
   and upstream routing all draw on the same counter. Spend three rejections
   arguing about a design and the automatic recovery has nothing left.
2. **Crash recovery charges a retry.** A daemon restart mid-generation costs the
   task an attempt, deliberately — otherwise a call that reliably crashes the
   daemon loops forever. A power cut therefore costs budget.
3. **Architect rejections are uncapped, but crash recovery is not.** Rejecting a
   design re-runs the architect with no `MAX_RETRIES` check, so you can iterate
   as long as you like. What runs out is crash-recovery headroom: past
   `MAX_RETRIES`, a daemon crash during that generation fails the spec outright.

---

## 6. What reaches which agent

Feedback is not symmetric, and the asymmetries are deliberate.

| Channel | Reaches | Notes |
|---|---|---|
| Gate **rejection** notes | The agent being retried | Design rejections → architect; code rejections → implementer. |
| Gate **approval** notes | The *next* role | Design approval → implementer, plan approval → architect, as conditions on an approved artefact — not as a rejection, so it is not invited to redesign. |
| Clarification answers | Planner, then implementer | Rendered at spec authority. |
| Plan `acceptance_criteria` | Architect | Supersede the spec where they differ; a criterion struck from the plan was struck on purpose. |
| Protected files | Architect and implementer, read-only | They are compiled in but must not be edited. Without them, agents redeclare types that already exist. |
| Compiler diagnostics | Implementer, or architect when recurring | See diagram 4. |

---

## 7. Reading a run

```bash
coding-model-autonomous status <spec_id>   # where it is now
coding-model-autonomous events <spec_id>   # every transition, with payloads
coding-model-autonomous gates              # what is waiting on you
```

The `events` table is the audit trail: every agent call, every test dispatch,
every gate, with the payload that drove the decision. When a run ends somewhere
surprising, the answer is almost always a budget in section 5 or a branch in
diagram 3 — in that order.
