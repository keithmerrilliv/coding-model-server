# Changelog

## v0.2.0 — unreleased (main since v0.1.0, 2026-08-19)

The Pipeline Kernel Refactor ([DEV-628](https://keith-merrill4.atlassian.net/browse/DEV-628)): the decisions that kept killing runs moved out of a 6,300-line daemon into five typed kernel modules — `workspace.py`, `outcome.py`, `context.py`, `retry_policy.py` and the event schemas — behind a fault-injecting seam tier. Seven phases, one ticket each; every line below links the ticket that carries the evidence and the live proof. A dagger (†) marks a ticket merged and In Review: it awaits live proof on a run before it moves to Done.

### Phase 0 — the seam tier ([DEV-662](https://keith-merrill4.atlassian.net/browse/DEV-662))

A fault-injecting test tier under the real dispatch loop, so the refactor was not itself found by live runs.

- [DEV-634](https://keith-merrill4.atlassian.net/browse/DEV-634) † — Seam-level fault-injection test tier — runner, model-server and sandbox faults driven through the real dispatch loop, so interface drift and misfiled failures are caught before a live run finds them

### Phase 1 — the artifact ledger ([DEV-663](https://keith-merrill4.atlassian.net/browse/DEV-663))

One door for every artifact write: role and hash on every row, cross-role collisions renamed, shrinkage judged against the repository, the synthesis corpus built from the ledger.

- [DEV-642](https://keith-merrill4.atlassian.net/browse/DEV-642) — Artifact ledger (kernel refactor phase 1) — one door for every artifact write: role and hash on every row, cross-role same-path collisions renamed or refused by policy, shrinkage judged against the repo version, synthesis corpus built from the ledger
- [DEV-602](https://keith-merrill4.atlassian.net/browse/DEV-602) — The reviewer overwrites the implementer's artifact at the same path and delivery ships the overwrite — run 17 delivered a 6-line comment stub in place of a 419-line test suite, past a green release gate
- [DEV-636](https://keith-merrill4.atlassian.net/browse/DEV-636) — Synthesis can regenerate an existing file as a stub and carry it to a green release gate — run 21 v4 offered a 61-line orchestrator_daemon.py in place of 6,130 lines with 9/9 tests passing
- [DEV-639](https://keith-merrill4.atlassian.net/browse/DEV-639) — Synthesis sweeps the sandbox overlay, pytest cache and diagnostics into its attempt corpus — run 21's attempt 0 was 88 "files", 79 of them .repo_overlay (1.35M chars), which is the 413 the merge call hit
- [DEV-553](https://keith-merrill4.atlassian.net/browse/DEV-553) † — Synthesis merges attempts written against superseded designs, so a defect the architect already fixed comes back from an old attempt
- [DEV-647](https://keith-merrill4.atlassian.net/browse/DEV-647) — The ledger's emptying guard refuses a design.md REVISION, so the design-review loop is silently a no-op and the gate carries the design the reviewer just rejected
- [DEV-641](https://keith-merrill4.atlassian.net/browse/DEV-641) — Whole-file <<<FILE>>> blocks are written without their trailing newline — the parser strips it and nothing restores it, so every delivered file ends mid-line
- [DEV-646](https://keith-merrill4.atlassian.net/browse/DEV-646) — Placeholder paths from the prompt's own examples (`path`, `...`, `another/file.py`, `relative/path/to/file.ext`) are written as artifacts — run 25's synthesis landed four of them beside the real files
- [DEV-656](https://keith-merrill4.atlassian.net/browse/DEV-656) — A path with an unsubstituted format brace or a glob metacharacter passes the ledger's placeholder door — `{p}` and `test_*.py` are not in DEV-646's vocabulary

### Phase 2 — one classifier, one disposition ([DEV-664](https://keith-merrill4.atlassian.net/browse/DEV-664))

Every failed attempt is classified as no-verdict, verdict or terminal in one place; no-verdicts never charge a retry and never end a spec.

- [DEV-629](https://keith-merrill4.atlassian.net/browse/DEV-629) — Central failure classification — every catch site routes through one verdict / no-verdict / design-terminal classifier; a no-verdict outcome never charges an attempt and never terminates a spec
- [DEV-623](https://keith-merrill4.atlassian.net/browse/DEV-623) — A truncated implementer response (finish_reason=length) degenerates into garbage edit paths and is charged as unappliable edits — rotation burns the attempt on a harness budgeting failure
- [DEV-507](https://keith-merrill4.atlassian.net/browse/DEV-507) † — A manifest parse failure costs a full implementer retry and discards the evidence, with no near-miss delimiter recovery
- [DEV-532](https://keith-merrill4.atlassian.net/browse/DEV-532) — The exhaustion→synthesis→fail path closes the reviewer task and never the implementer, so every failed run leaves a task row claiming to be RUNNING
- [DEV-433](https://keith-merrill4.atlassian.net/browse/DEV-433) — MAX_RETRIES synthesis is unreachable when retries came from human gate rejections
- [DEV-617](https://keith-merrill4.atlassian.net/browse/DEV-617) † — Server-side guard: a completion with completion_tokens > 0 and empty visible content should error or retry, never return silently empty
- [DEV-620](https://keith-merrill4.atlassian.net/browse/DEV-620) † — A runner outage at implement time silently empties the existing-file fetch and the implementer proceeds blind — run 19's retry regenerated an 8.5K-line surface from priors with no warning logged
- [DEV-622](https://keith-merrill4.atlassian.net/browse/DEV-622) † — Latent AttributeError in the DEV-538 requeue counters: Event has no `payload` accessor, so the second unreachable-runner requeue would crash the daemon loop
- [DEV-538](https://keith-merrill4.atlassian.net/browse/DEV-538) † — An inconclusive pre-gate build check opens a human gate instead of retrying, so one unreachable runner stalls the pipeline indefinitely
- [DEV-543](https://keith-merrill4.atlassian.net/browse/DEV-543) † — The architect burns its whole 8000-token budget and returns EMPTY content on half its generations — and the artifact we keep for diagnosis is empty by construction
- [DEV-624](https://keith-merrill4.atlassian.net/browse/DEV-624) † — Implementer rotation is context-blind and a dispatch exception is terminal — a 152K-token prompt was rotated onto moe_implementer's 116K context and the spec died in 46 seconds
- [DEV-651](https://keith-merrill4.atlassian.net/browse/DEV-651) † — Phase-2 residual: the synthesis REPAIR call swallows a transport failure and returns it as a failing test run, so a dead server is charged to the implementer as a verdict
- [DEV-652](https://keith-merrill4.atlassian.net/browse/DEV-652) † — Phase-2 residual: five sites still charge a retry and twelve still fail a spec outside the classifier, and the runner-unreachable requeue is a parallel re-implementation of dispose's no-verdict branch

### Phase 3 — one context stage, one prompt budget ([DEV-665](https://keith-merrill4.atlassian.net/browse/DEV-665))

The runner is read once per spec and every role selects from it; every prompt section is summed against the destination window before dispatch.

- [DEV-632](https://keith-merrill4.atlassian.net/browse/DEV-632) — One context-assembly stage shared by architect, implementer, reviewer and synthesis — roles select from a single fetch instead of each plumbing its own
- [DEV-633](https://keith-merrill4.atlassian.net/browse/DEV-633) — Aggregate prompt budget — one place sums every section against the destination agent's window before dispatch; per-section knobs are inputs, not independent ceilings
- [DEV-544](https://keith-merrill4.atlassian.net/browse/DEV-544) — A transient runner outage silently strips the architect's protected-file context and still charges the attempt, so the design that reaches the gate is generated blind
- [DEV-599](https://keith-merrill4.atlassian.net/browse/DEV-599) — The architect only ever sees protected_paths — a modify-spec without them designs against an invented API, and DEV-571 closed this hole for the implementer only
- [DEV-571](https://keith-merrill4.atlassian.net/browse/DEV-571) † — DEV-492's existing-file guard is keyed to an optional spec table nothing enforces — omit it and the implementer silently works blind again
- [DEV-604](https://keith-merrill4.atlassian.net/browse/DEV-604) † — Manifest mode regenerates existing files whole and does not receive DEV-571's existing-file content — run 18 rewrote a 117-line class as a 33-line stub that lost its imports
- [DEV-627](https://keith-merrill4.atlassian.net/browse/DEV-627) — Protected-file reference context shares the existing-files budget knob — inflating AUTONOMOUS_EXISTING_FILES_MAX_CHARS silently inflates it too, and run 21 died on a 413 at implementer dispatch
- [DEV-648](https://keith-merrill4.atlassian.net/browse/DEV-648) — A per-section char knob drops a file the window could hold 2.3x over — run 28's architect designed blind against a 146K executor.py with 343K chars of budget free
- [DEV-649](https://keith-merrill4.atlassian.net/browse/DEV-649) — Synthesis always emits whole files, so for a modification target larger than its output budget it can only ever produce a stub — run 28 spent 77 minutes proving that arithmetic and failed the spec
- [DEV-645](https://keith-merrill4.atlassian.net/browse/DEV-645) — Single-call mode never checks that every planned implement output was produced — run 25's attempt 0 emitted only the test file, and the omission surfaced as "behaviour failures" at a human gate
- [DEV-644](https://keith-merrill4.atlassian.net/browse/DEV-644) — Nothing tells the implementer how workspace files are importable in the sandbox — run 24's three implementers all wrote `from src.coding_model_autonomous.workspace import …` and burned the rotation on ModuleNotFoundError
- [DEV-643](https://keith-merrill4.atlassian.net/browse/DEV-643) — estimate_design_file_count counts the Criterion Seams' fixture paths as design files — run 24's two-file change was dispatched in manifest mode as "~9 files"
- [DEV-601](https://keith-merrill4.atlassian.net/browse/DEV-601) † — Nothing resolves the plan's phase paths against the target repo — three runs in one evening produced wrong paths, a placeholder, and three wholly invented files

### Phase 4 — absent is not unknown ([DEV-666](https://keith-merrill4.atlassian.net/browse/DEV-666))

Every parser and fetch a guard keys on distinguishes "nothing there" from "could not tell", and a guard fed the second arms by name.

- [DEV-630](https://keith-merrill4.atlassian.net/browse/DEV-630) † — Parse and fetch helpers return unknown, not empty — downstream guards refuse or warn on unknown instead of silently disarming together
- [DEV-536](https://keith-merrill4.atlassian.net/browse/DEV-536) † — The runner's overwrite/reconstruction signal is produced and never consumed
- [DEV-621](https://keith-merrill4.atlassian.net/browse/DEV-621) † — The change-surface parser only matches rows whose second column starts with "modif…" — run 19's descriptive table parsed as zero declared modifications, disarming the DEV-492 plan guard and the working-blind warning
- [DEV-573](https://keith-merrill4.atlassian.net/browse/DEV-573) — The planner silently drops test_strategy.protected_paths from the normalized plan — every protection keyed to it disarms at once
- [DEV-625](https://keith-merrill4.atlassian.net/browse/DEV-625) † — A plan missing test_strategy.repo is terminally failed by the DEV-492 guard at acceptance — before any gate — though a planner round with a note demonstrably fixes it
- [DEV-654](https://keith-merrill4.atlassian.net/browse/DEV-654) † — The self-target sandbox overlay copies the live working tree, not a git ref — an uncommitted edit on the dev box silently becomes what a dogfood run's tests are judged against
- [DEV-655](https://keith-merrill4.atlassian.net/browse/DEV-655) † — DEV-638's \"File modes — MANDATORY\" prompt section renders a syntactically valid <<<FILE:>>> block, so a model that echoes the instruction writes a junk file — runs 28 and 29 both landed `{p}` containing `...`

### Phase 5 — a retry must differ ([DEV-667](https://keith-merrill4.atlassian.net/browse/DEV-667))

Every dispatch is planned before the call; a failure two agents produced identically goes to synthesis; citations have a position; the failure stream is the identity.

- [DEV-631](https://keith-merrill4.atlassian.net/browse/DEV-631) † — Retry loop recognises invariant failures — a retry must change something (prompt, agent, temperature, environment) or classify the failure as environmental and stop spending the rotation
- [DEV-539](https://keith-merrill4.atlassian.net/browse/DEV-539) † — Targeted retry treats any filename mentioned in rejection notes as a citation, so saying "the bug is not in X.swift" regenerates X.swift and can destroy a file that compiled
- [DEV-530](https://keith-merrill4.atlassian.net/browse/DEV-530) † — Rotation is failure-triggered, so per-agent outcomes are confounded by construction — the current data ranks agents backwards
- [DEV-640](https://keith-merrill4.atlassian.net/browse/DEV-640) † — Implementer rotation re-anchors on the previous pick when complexity.json is absent — retries 3 and 4 both dispatch to fast_implementer, breaking the "never repeat a model" contract
- [DEV-619](https://keith-merrill4.atlassian.net/browse/DEV-619) † — Colon-rich clarification notes poison the planner's YAML — a Python signature in gate notes failed spec_27b1959f terminally after two identical unparseable attempts

### Phase 6 — observability, cancel, documentation ([DEV-668](https://keith-merrill4.atlassian.net/browse/DEV-668))

Three event kinds with fixed schemas, a diagnostic taxonomy, an honest Jira mirror, a first-class operator cancel, and documentation that describes the kernel.

- [DEV-529](https://keith-merrill4.atlassian.net/browse/DEV-529) † — Build failures are recorded as prose, so the failure taxonomy exists only in Jira comments and cannot be queried
- [DEV-669](https://keith-merrill4.atlassian.net/browse/DEV-669) † — CONTEXT_ASSEMBLED event kind and fixed, tested payload schemas for the three taxonomy events
- [DEV-482](https://keith-merrill4.atlassian.net/browse/DEV-482) † — Jira mirror records a FAILED spec as Done/Done, so failed runs are indistinguishable from successful ones
- [DEV-583](https://keith-merrill4.atlassian.net/browse/DEV-583) † — No safe operator cancel: cutting a run needs a raw DB write, and nothing drains the in-flight request or resets a wedged swap
- [DEV-493](https://keith-merrill4.atlassian.net/browse/DEV-493) † — No way to cancel a running spec — the operator can only kill the daemon, and crash recovery resumes it
- [DEV-567](https://keith-merrill4.atlassian.net/browse/DEV-567) † — Spec cancellation loses the race with an in-flight phase pass — the pass's completion write overwrites CANCELLED
- [DEV-582](https://keith-merrill4.atlassian.net/browse/DEV-582) † — Cancelling a spec with an in-flight model call leaks active_requests and deadlocks every subsequent model swap
- [DEV-609](https://keith-merrill4.atlassian.net/browse/DEV-609) † — scraping/ has 10 ruff errors invisible to CI (lint scope is src tests scripts)
- [DEV-611](https://keith-merrill4.atlassian.net/browse/DEV-611) † — Post-release doc polish backlog from the DEV-607 audit (non-blocking WARN/NIT items)
- [DEV-670](https://keith-merrill4.atlassian.net/browse/DEV-670) † — PIPELINE.md and CONFIGURATION.md rewrite for v0.2.0 — routing as the kernel does it, every AUTONOMOUS_* knob documented, .env.example matching

### Before the plan — August fixes and evaluations

Instances fixed one by one between v0.1.0 and the refactor, plus the model evaluations.

- [DEV-99](https://keith-merrill4.atlassian.net/browse/DEV-99) — Eval dense_architect (Qwen3.6-27B) as the interactive `architect` — it already beat the retired flagship and runs ~2x faster than the 480B
- [DEV-106](https://keith-merrill4.atlassian.net/browse/DEV-106) — Targeted-retry can drop a manifest-declared file from the workspace
- [DEV-400](https://keith-merrill4.atlassian.net/browse/DEV-400) — Design: use the local git server as transport and source of truth for spec attempts
- [DEV-414](https://keith-merrill4.atlassian.net/browse/DEV-414) — Devstral Small 2 24B as a VRAM-resident fast tier
- [DEV-426](https://keith-merrill4.atlassian.net/browse/DEV-426) — Validate test_strategy against the framework's required keys before creating the plan_approval gate
- [DEV-434](https://keith-merrill4.atlassian.net/browse/DEV-434) — Manifest-mode targeted retry cannot fix cross-file defects, so access-control errors loop forever
- [DEV-477](https://keith-merrill4.atlassian.net/browse/DEV-477) — Pre-gate build check reports "compiled" when the build produced no recognisable output at all
- [DEV-492](https://keith-merrill4.atlassian.net/browse/DEV-492) — The implementer is never given the contents of files it must modify, so every "modify" silently rewrites the file from imagination
- [DEV-509](https://keith-merrill4.atlassian.net/browse/DEV-509) — A design that names a type but allocates no file for it burns both budgets: the implementer improvises an off-manifest file, diagnostics oscillate, and architect-routing never fires
- [DEV-528](https://keith-merrill4.atlassian.net/browse/DEV-528) — agent_ran records no duration and no token usage, and names the agent in only 54% of calls — no cost, latency or per-model claim is derivable
- [DEV-541](https://keith-merrill4.atlassian.net/browse/DEV-541) † — The synthesis repair round overlays its output unconditionally, so a repair that regresses the build is committed and the spec dies worse than it started
- [DEV-556](https://keith-merrill4.atlassian.net/browse/DEV-556) — The architect's reasoning is unbounded, invisible and shares max_tokens with the design — add a per-role enable_thinking knob and measure whether the architect should think at all
- [DEV-558](https://keith-merrill4.atlassian.net/browse/DEV-558) — Crash recovery caps on retry_count, which human gate rejections also increment — a well-reviewed spec has ZERO crash budget and dies on the first restart
- [DEV-560](https://keith-merrill4.atlassian.net/browse/DEV-560) — A reviewer FAIL verdict over a green test run silently discards the implementation — tests are canonical in one direction only
- [DEV-563](https://keith-merrill4.atlassian.net/browse/DEV-563) — A reviewer test file that fails to PARSE still counts as a red run when other tests passed — the implementer is charged for the reviewer's SyntaxError
- [DEV-581](https://keith-merrill4.atlassian.net/browse/DEV-581) † — Pipeline: apply implementer edits as diffs/anchored replacements, not whole-file re-emission
- [DEV-610](https://keith-merrill4.atlassian.net/browse/DEV-610) — Pre-flip doc accuracy blockers: scraping/ docs describe a fictional program; SECURITY_MIGRATION uses --user against system units; TUTORIAL contradicts README on llama-server and documents removed /cupertino
- [DEV-614](https://keith-merrill4.atlassian.net/browse/DEV-614) — Install Qwen3.8-27B and profile it on the RTX 5080 (bring-up + sweep, eval-only registration)
- [DEV-615](https://keith-merrill4.atlassian.net/browse/DEV-615) — Eval: Qwen3.8-27B vs dense_architect (Qwen3.6-27B MTP) for the architect/supervisor slots — DEV-99 method
- [DEV-616](https://keith-merrill4.atlassian.net/browse/DEV-616) — qwen38_architect returned an empty design on design_offline_sync — 4,677 tokens spent entirely inside the reasoning block, stopped with budget remaining
- [DEV-626](https://keith-merrill4.atlassian.net/browse/DEV-626) — Self-target pytest specs cannot pass the sandboxed pre-gate check — the bwrap sandbox never provisions the target repo's package, so every implementation reds on ModuleNotFoundError
- [DEV-635](https://keith-merrill4.atlassian.net/browse/DEV-635) † — Edit-mode prompts never bound SEARCH anchor length — implementers pick 18-line anchors, fail byte-exact transcription, and burn the whole retry rotation (run 21 v3 died this way)
- [DEV-637](https://keith-merrill4.atlassian.net/browse/DEV-637) — Unappliable-edit diagnostics keep only their first line and the implementer's raw response is never persisted — run 21's six anchor misses cannot be classified after the fact
- [DEV-638](https://keith-merrill4.atlassian.net/browse/DEV-638) † — Anchored-edit application is byte-exact against a transcribing model — add a tolerant match ladder that still refuses on ambiguity, and reject new-file edit blocks before they cost an attempt

## v0.1.0 — 2026-08-19

First public release: the multi-agent server, the interactive client, the autonomous pipeline through run 16, the Mac runner, the dashboard, and the security migration to loopback plus an admin key ([DEV-607](https://keith-merrill4.atlassian.net/browse/DEV-607)).
