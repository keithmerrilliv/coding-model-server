# Changelog

## Unreleased

### Shipped

- [DEV-823](https://keith-merrill4.atlassian.net/browse/DEV-823) — the rotation's window-fit check now estimates the next prompt from the largest single call of the last attempt, not the attempt's total. Run 61's first attempt made five manifest-mode calls totalling 71,399 prompt tokens. The check read that as one prompt, ruled out every 64K agent and sent a retry of about 18K tokens to `deep_implementer`, undoing DEV-821. Implementer events now record `max_call_prompt_tokens`; an older multi-call event without it counts as "cannot tell", and the dispatch-time budget check decides. When the agent from attempt 0 no longer fits, retry 1 now takes the first agent that does. Before, it skipped that agent, so a `[moe, deep]` filter picked deep. On the archive, 3 of 75 retries had their eligible set narrowed by a summed estimate.
- [DEV-822](https://keith-merrill4.atlassian.net/browse/DEV-822) — two fixes to the design testability check from run 61. `undeclared_mutability` now reads only top-level `struct`/`enum` declarations. It had flagged `ModelState` and `AvailableModel`, enums nested inside `final class HallucinationEngine`, as value types the design was adding state to. `prose_seam` still fires on a setup that continues another seam ("continues from C5a"), because run 61's implementer wrote that seam as its own test, lost the state and produced a test that could not pass. Its message now names the continuation and asks for the calls in full; the old generic "names no API" wording left both revision rounds unused. The architect prompt now warns against continuations. Each round's findings text is recorded on the check's event, because the feedback file and the design are overwritten by the next round. Measured on the archive: one mutability finding disappears (run 61's), none appears, and the seam-check counts over 445 designs are unchanged.
- [DEV-821](https://keith-merrill4.atlassian.net/browse/DEV-821), [DEV-720](https://keith-merrill4.atlassian.net/browse/DEV-720) — `deep_implementer` becomes the rotation's window fallback, not its first retry. DEV-720's investigation found the rotation does change agent on every retry; its "never rotates" premise came from a `tasks.agent` field each dispatch overwrites. From 2026-09-13 to 09-26 it rescued 7 of 33 specs, but deep made 38 attempts and delivered none, 18 of them from the slot right after `implementer`, while Glimmer delivered 4 of 14 from later slots. The chain is now `implementer, glimmer, moe, fast, deep`, and the "high" complexity tier maps to `implementer`. Deep still takes every prompt only its 256K window fits.
- [DEV-748](https://keith-merrill4.atlassian.net/browse/DEV-748), [DEV-650](https://keith-merrill4.atlassian.net/browse/DEV-650) — two retirements. `devstral_implementer` is removed. It was in no implementer rotation, tier or allow-list, so no pipeline dispatch could ever reach it, and the VRAM rung DEV-748 re-tuned could never be proven live. Its DEV-414 eval results stay in git history. `executor._write_artifact`, the write path that bypassed the DEV-642 ledger and its guards, is deleted. Its last production caller retired with the adversarial test writer (DEV-717), and every workspace write now goes through the ledger.
- [DEV-760](https://keith-merrill4.atlassian.net/browse/DEV-760) — an architect rejection retry gets twice the completion budget of a first pass (`AUTONOMOUS_ARCHITECT_RETRY_MAX_TOKENS`, 20,000 here). Run 46's first pass wrote a complete design in 4,879 tokens; its retry spent 10,000 four times and was cut off forty words into the correct design. A reasoning-only 502, where the model spent the budget thinking, is no longer re-sent unchanged by the transport ladder; other 502s keep their backoff. It is recorded as an empty completion carrying the server's spent-token count, where before requests' generic "Bad Gateway" discarded it. Every completion now reports `reasoning_chars` and `visible_chars`, carried onto the agent events, so a budget spent thinking is a query instead of a journal read.
- [DEV-809](https://keith-merrill4.atlassian.net/browse/DEV-809) — the design testability check stops rejecting correct designs. Run 57 spent both architect revision rounds on a static lookup whose setup honestly needed no fixture; run 60's design was correct from round 0 and still drew four findings. Four fixes, each with a negative control. An explicit `setup: (none …)` is accepted when act and assert are real calls, and the architect prompt now names that spelling. Lettered sub-seams (`C6a`, `C6b`) count as one criterion. A type that a File Structure line places in a file ("NEW: Position, Direction", "Position struct", "OffsetStubStrategy helpers") has a file, but one merely used there does not. A Data Models heading that is a type name declares that type. Replayed over all 443 archived designs: nothing new fires, and 159 findings disappear, 149 of them `type_without_file` on designs that put several types in one file. That was ordinary Swift and 58% of that check's archived findings.
- [DEV-810](https://keith-merrill4.atlassian.net/browse/DEV-810) — the delivery guard tells a renamed test from a deleted one, and says "stale base" only when the base moved. Run 57 renamed a test because its slice changed what the test asserts, on a base identical to `main`, and was refused as a stale base and hand-delivered. Now a test name that disappears while the file keeps as many tests is a probable rename. On a current base it is delivered, with both name lists on the record; on a stale or unrecorded base it is refused and named as a rename. A deletion (the count falls) is refused on any base, and the remedy line matches the diagnosis. Replayed over the archive: of 9 real deliveries only run 57 changes verdict, and the three decayed Centipede branches that motivated [DEV-756](https://keith-merrill4.atlassian.net/browse/DEV-756) are still refused as stale.
- [DEV-817](https://keith-merrill4.atlassian.net/browse/DEV-817) — the Mac runner retries a guest's ssh authentication refusal instead of failing the dispatch. Twice (runs 59 and 60) a tart guest refused the worktree sync moments after the boot probe had logged in, while every other dispatch on the same host and image passed. Run 60's refusal cost two implementer attempts before [DEV-816](https://keith-merrill4.atlassian.net/browse/DEV-816). The worktree sync and the package-cache push now retry an auth refusal after 2, 4 and 8 seconds, and only that failure; anything else still fails at once. Each refusal's offset from the boot probe is written into the run's output, so the next occurrence can confirm or refute the settling-guest theory.
- [DEV-816](https://keith-merrill4.atlassian.net/browse/DEV-816) — a reviewer test run that never ran the code is no longer charged to the implementer. [DEV-705](https://keith-merrill4.atlassian.net/browse/DEV-705)'s classifier guarded only the build check. On run 60 the reviewer's VM refused the worktree sync, and the red run was recorded as `tests_failed`, which discarded a byte-perfect retry 0 and spent two attempts. A VM that never came up, or a runner that could not be reached, is now a no-verdict at the reviewer stage too: the reviewer requeues uncharged and parks behind an infrastructure gate past the cap. A suite that ran and failed is still a verdict, even if a `[vm]` line rides along.
- [DEV-811](https://keith-merrill4.atlassian.net/browse/DEV-811) — an auth rejection on the Mac runner now names itself on both hosts. 747 of 1,050 `read_files` requests were refused 401 across nine days and left no trace anywhere but the runner's access log: the server logged nothing on a rejection, and `fetch_repo_files` returned the non-200 as a soft `problems` entry without logging, so the orchestrator's journal held zero lines and `RunnerOutage` was never raised once. The reads degraded silently and each execution pass carried on with less context than it asked for. A rejection now logs the caller, the endpoint, and the presented key as `absent` or as a fingerprint against the expected one, never the key or a prefix of it. `fetch_repo_files` logs every whole-fetch failure before returning, and a 401 names both causes to check; the fail-soft shape [DEV-620](https://keith-merrill4.atlassian.net/browse/DEV-620) depends on is unchanged. One contributor is confirmed and one is not: zooshly's `coding-model-runner-shim` unit runs this same server on Linux against a Linux clone, and its own journal shows it rejecting reads — the [DEV-701](https://keith-merrill4.atlassian.net/browse/DEV-701) provenance failure wearing the Mac's name. It is a deliberate stopgap, so `main()` now refuses to start off Darwin unless `CODING_MODEL_RUNNER_ALLOW_NON_DARWIN=1` declares it. Whether the shim accounts for all 747, and whether any delivered artefact was harmed by a degraded fetch, **cannot be established**: nothing recorded enough to attribute them, which is the defect itself.

## v0.4.0 — 2026-09-22

The Swift-loop release ([DEV-776](https://keith-merrill4.atlassian.net/browse/DEV-776)): the implementer loop learns the compiler's shape, and retrieval is measured on the role that writes the code.

### Shipped

- [DEV-777](https://keith-merrill4.atlassian.net/browse/DEV-777) — the host-side Swift precheck covers every runs-44–49 diagnostic class a text scan can catch (`throws` on `@Test`, `Hashable` on dictionary keys, `nil` for a non-optional argument, MainActor types called from nonisolated tests); ambiguous-SEARCH refusals name the copies and their `#if` branches.
- [DEV-778](https://keith-merrill4.atlassian.net/browse/DEV-778) — a standing diagnostic-to-fix table in the retry and repair prompts, rendered only for the diagnostics present; build-failure feedback is ANSI-stripped and headlined by the first located diagnostic ([DEV-768](https://keith-merrill4.atlassian.net/browse/DEV-768)).
- [DEV-751](https://keith-merrill4.atlassian.net/browse/DEV-751), [DEV-774](https://keith-merrill4.atlassian.net/browse/DEV-774), [DEV-775](https://keith-merrill4.atlassian.net/browse/DEV-775), [DEV-787](https://keith-merrill4.atlassian.net/browse/DEV-787) — "Tests added: N" counts snake_case Swift tests; `swift test` and parallel `xcodebuild` output carry a verdict instead of "inconclusive"; `PackageDescription` symbols no longer read as unresolved.
- [DEV-762](https://keith-merrill4.atlassian.net/browse/DEV-762), [DEV-756](https://keith-merrill4.atlassian.net/browse/DEV-756) — a tunnel flap during the plan probe parks the spec instead of aborting it; every delivery records its base (`delivery_base.json`) and refuses a stale snapshot. Proven on runs 51 and 53.
- [DEV-497](https://keith-merrill4.atlassian.net/browse/DEV-497), [DEV-781](https://keith-merrill4.atlassian.net/browse/DEV-781), [DEV-510](https://keith-merrill4.atlassian.net/browse/DEV-510) — every agent call retrieves on the spec title; Objective-C and Objective-C++ join Swift in the covered languages; the implementer is opted into retrieval from 2026-09-21T11:28:40Z with the retrieval-off era banked as the control. The A/B verdict is recorded on [DEV-657](https://keith-merrill4.atlassian.net/browse/DEV-657) and it is **not proven**: retrieval reaches the implementer on every call and injects on four specs in five, at a cost bounded above by 1.7% of the prompt, but no outcome has been shown to change. The Swift-loop fixes landed across both arms, and the query is the spec title alone — so every attempt on a spec gets byte-identical retrieved context, making the real sample five specs rather than eighteen calls. Retrieval stays on at negligible cost; nothing here says it earns its keep.
- [DEV-784](https://keith-merrill4.atlassian.net/browse/DEV-784) — a default-MainActor target is declared in the spec's test strategy; that arms a precheck for un-hopped observer, sink, dispatch and timer closures, a sharper MainActor hint, and a standing rule in the architect and implementer prompts. Proven on run 55, which closes [DEV-753](https://keith-merrill4.atlassian.net/browse/DEV-753)'s acceptance.
- [DEV-782](https://keith-merrill4.atlassian.net/browse/DEV-782), [DEV-783](https://keith-merrill4.atlassian.net/browse/DEV-783), [DEV-786](https://keith-merrill4.atlassian.net/browse/DEV-786), [DEV-790](https://keith-merrill4.atlassian.net/browse/DEV-790), [DEV-791](https://keith-merrill4.atlassian.net/browse/DEV-791), [DEV-792](https://keith-merrill4.atlassian.net/browse/DEV-792) — fixes from runs 50–54: the prompt's placeholder line is stripped from whole-file artifacts; the invariant guard keys on the diagnostic, not the file; backticked plan paths are unquoted; a retry is served the previous attempt's new files as editable content; a `#require` without `try` is located, hinted and caught before the Mac trip; Swift runner output yields a pass rate, so a near-miss synthesis gets its repair round.
- [DEV-603](https://keith-merrill4.atlassian.net/browse/DEV-603) — the two Electric Sheep dtype suites come out of a five-week quarantine. `MLXFailureRouter` gains a scoped `withHandler` that restores the handler installed before it and holds a recursive gate for the body, so concurrent tests stop clobbering one shared slot; new tests pin nesting, isolation and 8-way concurrency. Measured 10 red of 15 before, 0 of 8 after.
- [DEV-752](https://keith-merrill4.atlassian.net/browse/DEV-752), [DEV-805](https://keith-merrill4.atlassian.net/browse/DEV-805) — a VM dispatch opens with `[vm] phases: boot … cache push … resolve … test`, so a starved Mac is legible from zooshly instead of only from the Mac's own log; the resolve step has its own capped budget and a timeout there fails the phase by name rather than letting a doomed test start. `GET /v1/version` reports the commit the runner is serving and whether it is dirty, and the caller logs it at every dispatch — a merge on zooshly is not a deploy of `mac_runner/*`, and this is how you tell.
- [DEV-807](https://keith-merrill4.atlassian.net/browse/DEV-807) — a review that states no verdict is no longer recorded as one that said FAIL. A missing `### Verdict` heading is a parse failure routed through the [DEV-629](https://keith-merrill4.atlassian.net/browse/DEV-629) classifier, so the reviewer re-runs on its own budget and the implementer is charged nothing; the diagnostic names which of the two adjacent headings was written.

### Runs 50 to 56

Four delivered (51, Electric Sheep main `3829f66`; 53, Centipede main `5c9fa53`; 54, Electric Sheep main `6070eb0`; 55, Electric Sheep main `80bcfce`, the first ES run green with no hand-written isolation contract), two failed at synthesis (50, 52). On the four runs no source file was wrong after retry 0 of runs 51–53; every lost attempt was a spec number, a design seam, or a pipeline mechanic, and each has a ticket above or in [DEV-784](https://keith-merrill4.atlassian.net/browse/DEV-784) / [DEV-792](https://keith-merrill4.atlassian.net/browse/DEV-792).

Run 56 delivered on its first attempt with no retries at all — architect, implementer and reviewer each once — to Electric Sheep main `1ae39e3`, 71 tests passed and 0 failed with the dtype quarantine removed. It is the first run of the release in which nothing went wrong, which is also why [DEV-776](https://keith-merrill4.atlassian.net/browse/DEV-776)'s first gate item is still open: the Swift precheck has never fired on a live attempt, because since v0.3.0 no attempt has made a slip it catches. Measured against the spec archive instead, all nine detectors fire — 68 hits on file sets the Mac rejected, 0 on sets that compiled green, 4 unknown.

## v0.3.0 — 2026-09-20

The serving release. `tools/llama-server` moves from the pinned August build (`a94d563`) to upstream **v0.4.1**, and the architect stops paying for prompt depth. Nothing in the pipeline changed; what changed is the substrate underneath all eleven agents. Numbers below are **managed-path** figures — measured through the coding-model-server exactly as the pipeline calls it — not standalone bench numbers, because the two are not comparable and the gap between them is unexplained ([DEV-742](https://keith-merrill4.atlassian.net/browse/DEV-742)).

### The finding

`--n-cpu-ffn` keeps a dense model's FFN weights on the CPU while attention and the KV cache stay resident on the GPU. FFN cost is per-token and flat with depth; attention cost scales with context. The old `-ngl 46` rung put 19 of the architect's 65 blocks entirely on the CPU — their attention *compute* included — so it paid more the deeper the prompt went:

| prompt depth | old rung | new rung |
| --- | --- | --- |
| 28 tokens | 18.30 t/s | 21.58 t/s |
| ~26K (the band production uses) | 15.06 t/s | 22.33 t/s |
| ~51K | 10.70 t/s | 21.93 t/s |

The old curve falls 42% across that range. The new one is flat. Every architect decode figure on record was measuring the price of CPU attention ([DEV-742](https://keith-merrill4.atlassian.net/browse/DEV-742)).

### Shipped

- [DEV-741](https://keith-merrill4.atlassian.net/browse/DEV-741) — the model argv moves off the `--mmap` family, which 0.4.x **removes**, to `--load-mode`. Landed and proven on the *old* binary first, so the one change that could have taken every agent down at once was never in the same commit as the binary swap.
- [DEV-744](https://keith-merrill4.atlassian.net/browse/DEV-744) — the upgrade, and `dense_architect` to `-ngl 66 --n-cpu-ffn 33`. Managed path at a 53,333-token architect prompt: **17.35 t/s decode against 8.3–9.0 on the old rung**, prefill 1,160 t/s, on more headroom than before. All 12 distinct models load on the new binary and 11 consecutive model swaps ran clean.
- [DEV-748](https://keith-merrill4.atlassian.net/browse/DEV-748) — `devstral_implementer` was loading with **25 MiB** of VRAM free against a 500 MiB cushion its own config comment said the rung was chosen to clear. Nothing had regressed: ~595 MiB of the box's VRAM is now permanently held by desktop software, which is most of the margin. Now 491 MiB, at a 12,288 window with one FFN layer offloaded.

### Measured and rejected

- [DEV-745](https://keith-merrill4.atlassian.net/browse/DEV-745) — **CUDA 13.4**. Prefill identical to 0.1% and VRAM identical to 1 MiB, so [DEV-598](https://keith-merrill4.atlassian.net/browse/DEV-598)'s long-standing Blackwell MMQ concern is retired — but decode is ~9% *worse*, because different `nvcc` codegen changes the inference numerics. MTP draft acceptance is 0.860 on 13.2 and 0.739 on 13.4 at temperature 0, each reproducing byte-identically within its own build. The stack stays on 13.2. Two consequences worth carrying: measurements do not transfer across a toolkit boundary, and a deterministic pipeline's outputs can change under a toolkit upgrade with nothing moving in our own version numbers.
- The 128K context window was shown **affordable** — `--n-cpu-ffn 52` holds it on 1,404 MiB free at 15.96 t/s, against 8.03 for the old 36/131072 rung — and deliberately not taken. At today's prompt sizes it buys no speed, and the window is not the constraint. It is a two-value change if that stops being true ([DEV-742](https://keith-merrill4.atlassian.net/browse/DEV-742)).

### Superseded

[DEV-707](https://keith-merrill4.atlassian.net/browse/DEV-707)'s premise — "the context window is the only thing we can trade for GPU layers on a 16 GB card" — was true of `--n-gpu-layers` and is not true under `--n-cpu-ffn`. [DEV-708](https://keith-merrill4.atlassian.net/browse/DEV-708)'s adaptive-window design rests on the same premise.

### The proving runs — 42 to 49 (2026-09-18 to 09-20)

Eight runs, all against Swift targets on the Mac runner. Two delivered autonomously (42, 49), three failed and were finished by hand (43, 47, 48), three failed outright (44, 45, 46). Every failure was in the implementer or synthesis-repair loop; none was in serving, which is what this release changed. Run 49 is the v0.3.0 proving run: the first autonomous delivery on llama-server v0.4.1, merged to Centipede main `5185167`. The run records carry the evidence:

- [DEV-728](https://keith-merrill4.atlassian.net/browse/DEV-728) — Run 42, Electric Sheep bridge dedup (DEV-592/593): delivered, merged by hand; the run that raised DEV-601 to High
- [DEV-732](https://keith-merrill4.atlassian.net/browse/DEV-732) — Run 43, Electric Sheep audio strike race (DEV-594/200/201): failed at synthesis on a test helper the spec left to the model; retry 2 corrected by hand and merged
- [DEV-750](https://keith-merrill4.atlassian.net/browse/DEV-750) — Run 44, Electric Sheep audio lifecycle: the first run on v0.4.1; Swift 6 actor isolation beat five implementers and synthesis (DEV-753); serving clean
- [DEV-754](https://keith-merrill4.atlassian.net/browse/DEV-754) — Run 45, Centipede slice 9 as a controlled old/new-binary comparison: failed; found the ANSI blind spot in the repair gate (DEV-755)
- [DEV-758](https://keith-merrill4.atlassian.net/browse/DEV-758) — Run 46, Electric Sheep triple buffering: failed on an operator approval condition that could not change the plan's file set (DEV-546)
- [DEV-761](https://keith-merrill4.atlassian.net/browse/DEV-761) — Run 47, the same spec re-issued: failed on an ambiguous SEARCH and an unqualified static (DEV-763, DEV-764); retry 1 hand-fixed and merged as ElectricSheep `6506baf`
- [DEV-766](https://keith-merrill4.atlassian.net/browse/DEV-766) — Run 48, Centipede renderer slice 1 (DEV-765): failed at synthesis; the repair edited an uncited file (DEV-767); hand-fixed to green, not merged
- [DEV-769](https://keith-merrill4.atlassian.net/browse/DEV-769) — Run 49, the identical spec on the DEV-764/767 pipeline: **delivered**, five new tests, DEV-744 closed on it

### Pipeline fixes shipped since v0.2.0

Everything below is on main and in this tag. Items marked *(In Review)* are merged and awaiting the live run that proves them; the rest are Done on run evidence.

- [DEV-601](https://keith-merrill4.atlassian.net/browse/DEV-601) — `plan_paths.py` resolves every phase path at plan validation: resolved, corrected (rewritten, not rejected), placeholder (rejected) or new (surfaced), with corrections probed on the same plan-time fetch *(In Review)*
- [DEV-733](https://keith-merrill4.atlassian.net/browse/DEV-733) — A plan's new-file path within two edits of a change-surface basename is corrected to it, and the planner is told to copy paths, never retype them *(In Review)*
- [DEV-698](https://keith-merrill4.atlassian.net/browse/DEV-698) — Context assembly names the symbols an editable file calls that the served set cannot show, to the architect and the implementer, and never suppresses a long list *(In Review)*
- [DEV-730](https://keith-merrill4.atlassian.net/browse/DEV-730) — The architect is told which declared modifications it was not given and that `READ_FILE` exists, in the one case where it must use it *(In Review)*
- [DEV-714](https://keith-merrill4.atlassian.net/browse/DEV-714) — The production architect has the tool loop the eval harness had; it really emits `<<<READ_FILE>>>` (Done, run 42)
- [DEV-700](https://keith-merrill4.atlassian.net/browse/DEV-700) — The code-review gate says how many tests the attempt actually added, from `count_test_declarations` *(In Review; the Swift counter's camelCase-only match is DEV-751)*
- [DEV-731](https://keith-merrill4.atlassian.net/browse/DEV-731) — The release gate shows the test verdict and roster first and the log tail, not the build preamble *(In Review)*
- [DEV-710](https://keith-merrill4.atlassian.net/browse/DEV-710) — A criterion seam that is syntactically valid and asserts nothing is not a seam *(In Review)*
- [DEV-715](https://keith-merrill4.atlassian.net/browse/DEV-715) — The suite-level hatch DEV-710 added is reachable from the architect prompt, and suite-level criteria are excluded from the seam count *(In Review)*
- [DEV-711](https://keith-merrill4.atlassian.net/browse/DEV-711) — The reviewer no longer asserts one Swift test layout, and an evidence trail that does not resolve is not evidence *(In Review)*
- [DEV-712](https://keith-merrill4.atlassian.net/browse/DEV-712) — The spec's test-strategy section parses in every dialect the archive contains; it had returned empty on 23% of real specs (Done, run 42)
- [DEV-709](https://keith-merrill4.atlassian.net/browse/DEV-709) — The planner substituting `test_strategy.framework` is caught by DEV-712's overlay *(In Review)*
- [DEV-713](https://keith-merrill4.atlassian.net/browse/DEV-713) — A known-flaky test can be quarantined by a skip filter instead of charged to the model (Done, run 42)
- [DEV-722](https://keith-merrill4.atlassian.net/browse/DEV-722) — A design that adds stored mutable state to a Swift value type must declare the contract (Done, run 42)
- [DEV-738](https://keith-merrill4.atlassian.net/browse/DEV-738) — A byte-identical reviewer duplicate is contained without writing into a compiled source directory *(In Review)*
- [DEV-705](https://keith-merrill4.atlassian.net/browse/DEV-705) — Leaked tart VMs: a concurrency guard, a hard teardown failure, evidence kept when a VM dispatch fails without a verdict, VM failures classified by boundary, the 1200 s xcodebuild budget on both hosts, and a one-command Mac runner update *(In Review)*
- [DEV-755](https://keith-merrill4.atlassian.net/browse/DEV-755) — ANSI escapes are stripped before diagnostics, an unmeasurable build is not "unimproved", and synthesis and repair outputs are retained under `retry_history/` *(In Review; fixes 1 and 3 proven on runs 46 and 48)*
- [DEV-764](https://keith-merrill4.atlassian.net/browse/DEV-764) — Standing Swift rules in every implementer, synthesis and repair prompt on a Swift file set, and a static-member precheck that fires on run 47's line *(In Review)*
- [DEV-767](https://keith-merrill4.atlassian.net/browse/DEV-767) — The synthesis repair round must edit what the compiler cited: uncited files are dropped, and a repair that touches no cited line is refused without a Mac trip *(In Review)*
- [DEV-770](https://keith-merrill4.atlassian.net/browse/DEV-770) — A SEARCH/REPLACE block inside a `<<<FILE: path>>>` block edits that file instead of costing the attempt *(In Review)*
- [DEV-717](https://keith-merrill4.atlassian.net/browse/DEV-717) — The `adversarial_test_writer` stage is retired: 15 lifetime invocations, one test *(In Review)*
- [DEV-440](https://keith-merrill4.atlassian.net/browse/DEV-440) — The automated design-review stage is off by default *(In Review)*
- [DEV-734](https://keith-merrill4.atlassian.net/browse/DEV-734) — The planner has the telemetry every other role has, reads `finish_reason`, and appears in the role table *(In Review)*
- [DEV-719](https://keith-merrill4.atlassian.net/browse/DEV-719) — `tasks.agent` is documented as destructive and guarded *(In Review)*
- [DEV-723](https://keith-merrill4.atlassian.net/browse/DEV-723) — `eval_agents.py` has a real token budget and a retry, and records `finish_reason` *(In Review)*
- [DEV-657](https://keith-merrill4.atlassian.net/browse/DEV-657) — Part 1: retrieval is gated on the spec's language, not only the role; the ticket's part 3 (proving it earns its keep) is v0.4.0
- [DEV-692](https://keith-merrill4.atlassian.net/browse/DEV-692) — Muse-Glimmer-30B is registered for the architect and implementer slots and joins the implementer rotation on retries only; the architect slot is unusable because of DEV-747 (partial; the rest is v0.4.0)
- [DEV-727](https://keith-merrill4.atlassian.net/browse/DEV-727) — The Glimmer 502 is a chat-template parse failure, not VRAM; the rung is ngl=40 / 64K (partial)
- [DEV-721](https://keith-merrill4.atlassian.net/browse/DEV-721) — A warm SwiftPM cache pushed into the guest, prototype (partial)
- [DEV-773](https://keith-merrill4.atlassian.net/browse/DEV-773) — `scripts/merge_gate.sh` runs ruff, mypy and pytest on the merged tree and merges only on all three; the hand-typed chain had let a lint error sit on main for 12 commits

### Serving and environment, beyond the headline

- [DEV-740](https://keith-merrill4.atlassian.net/browse/DEV-740) — `qwen38_architect` re-swept onto the incumbent's rung, so a comparison measures the model and not a stale sweep *(In Review)*
- [DEV-725](https://keith-merrill4.atlassian.net/browse/DEV-725) — The resource monitor writes "unmeasured" rather than a zero when RAPL is unreadable *(In Review)*
- [DEV-726](https://keith-merrill4.atlassian.net/browse/DEV-726) — Dashboard Metrics shows CPU utilization and CPU/GPU power, with unmeasured rendered as unmeasured *(In Review)*
- [DEV-729](https://keith-merrill4.atlassian.net/browse/DEV-729) — llama-server v0.4.1 evaluated against the pinned build for Glimmer's harmony tool calls: it does not fix them (DEV-747) *(In Review)*
- [DEV-702](https://keith-merrill4.atlassian.net/browse/DEV-702) — The Qwen3.8 vs dense_architect re-challenge under the tool loop: Qwen3.8 wins 5–1 with tools, having lost 5–1 without; production kept dense_architect because it now has the tool loop too (DEV-714) *(In Review)*

### Specs and target repositories

- [DEV-590](https://keith-merrill4.atlassian.net/browse/DEV-590) — The Electric Sheep triple-buffering spec is narrowed to the MTKView path (the immersive path is DEV-757); delivered by hand from run 47
- [DEV-595](https://keith-merrill4.atlassian.net/browse/DEV-595) — The audio-lifecycle spec is corrected against `AudioManager` and given a change surface
- [DEV-765](https://keith-merrill4.atlassian.net/browse/DEV-765) — Centipede renderer slice 1, split from DEV-102: an offscreen Metal 3 renderer with pixel-asserted tests; delivered by run 49

## v0.2.0 — 2026-09-16

The Pipeline Kernel Refactor ([DEV-628](https://keith-merrill4.atlassian.net/browse/DEV-628)): the decisions that kept killing runs moved out of the orchestrator daemon into four typed kernel modules — `workspace.py`, `outcome.py`, `context.py`, `retry_policy.py` (~3,300 lines) — plus fixed event payload schemas, behind a fault-injecting seam tier. The daemon itself did not shrink (5,804 lines at v0.1.0, 7,185 now); what moved is the deciding. Seven phases, one ticket each; every line below links the ticket that carries the evidence and the live proof. Every ticket in the seven phase sections is Done. Where a fix could not be proven by a live run under conditions we can manufacture, green dedicated tests stand as the proof. The "After the proving runs" section is different: it lists what merged from the proving runs, and where a ticket shipped only in part it says which half is still open. Run 31 (Centipede logic core slice 7, `spec_c1e1c9ac`, 2026-09-13) was the proving run for phases 4–6: it delivered on the Mac runner with 52 tests green after one build-failure retry, and the tickets below naming a run-time behaviour were moved to Done on its events.

### Phase 0 — the seam tier ([DEV-662](https://keith-merrill4.atlassian.net/browse/DEV-662))

A fault-injecting test tier under the real dispatch loop, so the refactor was not itself found by live runs.

- [DEV-634](https://keith-merrill4.atlassian.net/browse/DEV-634) — Seam-level fault-injection test tier — runner, model-server and sandbox faults driven through the real dispatch loop, so interface drift and misfiled failures are caught before a live run finds them

### Phase 1 — the artifact ledger ([DEV-663](https://keith-merrill4.atlassian.net/browse/DEV-663))

One door for every artifact write: role and hash on every row, cross-role collisions renamed, shrinkage judged against the repository, the synthesis corpus built from the ledger.

- [DEV-642](https://keith-merrill4.atlassian.net/browse/DEV-642) — Artifact ledger (kernel refactor phase 1) — one door for every artifact write: role and hash on every row, cross-role same-path collisions renamed or refused by policy, shrinkage judged against the repo version, synthesis corpus built from the ledger
- [DEV-602](https://keith-merrill4.atlassian.net/browse/DEV-602) — The reviewer overwrites the implementer's artifact at the same path and delivery ships the overwrite — run 17 delivered a 6-line comment stub in place of a 419-line test suite, past a green release gate
- [DEV-636](https://keith-merrill4.atlassian.net/browse/DEV-636) — Synthesis can regenerate an existing file as a stub and carry it to a green release gate — run 21 v4 offered a 61-line orchestrator_daemon.py in place of 6,130 lines with 9/9 tests passing
- [DEV-639](https://keith-merrill4.atlassian.net/browse/DEV-639) — Synthesis sweeps the sandbox overlay, pytest cache and diagnostics into its attempt corpus — run 21's attempt 0 was 88 "files", 79 of them .repo_overlay (1.35M chars), which is the 413 the merge call hit
- [DEV-553](https://keith-merrill4.atlassian.net/browse/DEV-553) — Synthesis merges attempts written against superseded designs, so a defect the architect already fixed comes back from an old attempt
- [DEV-647](https://keith-merrill4.atlassian.net/browse/DEV-647) — The ledger's emptying guard refuses a design.md REVISION, so the design-review loop is silently a no-op and the gate carries the design the reviewer just rejected
- [DEV-641](https://keith-merrill4.atlassian.net/browse/DEV-641) — Whole-file <<<FILE>>> blocks are written without their trailing newline — the parser strips it and nothing restores it, so every delivered file ends mid-line
- [DEV-646](https://keith-merrill4.atlassian.net/browse/DEV-646) — Placeholder paths from the prompt's own examples (`path`, `...`, `another/file.py`, `relative/path/to/file.ext`) are written as artifacts — run 25's synthesis landed four of them beside the real files
- [DEV-656](https://keith-merrill4.atlassian.net/browse/DEV-656) — A path with an unsubstituted format brace or a glob metacharacter passes the ledger's placeholder door — `{p}` and `test_*.py` are not in DEV-646's vocabulary

### Phase 2 — one classifier, one disposition ([DEV-664](https://keith-merrill4.atlassian.net/browse/DEV-664))

Every failed attempt is classified as no-verdict, verdict or terminal in one place; no-verdicts never charge a retry and never end a spec.

- [DEV-629](https://keith-merrill4.atlassian.net/browse/DEV-629) — Central failure classification — every catch site routes through one verdict / no-verdict / design-terminal classifier; a no-verdict outcome never charges an attempt and never terminates a spec
- [DEV-623](https://keith-merrill4.atlassian.net/browse/DEV-623) — A truncated implementer response (finish_reason=length) degenerates into garbage edit paths and is charged as unappliable edits — rotation burns the attempt on a harness budgeting failure
- [DEV-507](https://keith-merrill4.atlassian.net/browse/DEV-507) — A manifest parse failure costs a full implementer retry and discards the evidence, with no near-miss delimiter recovery
- [DEV-532](https://keith-merrill4.atlassian.net/browse/DEV-532) — The exhaustion→synthesis→fail path closes the reviewer task and never the implementer, so every failed run leaves a task row claiming to be RUNNING
- [DEV-433](https://keith-merrill4.atlassian.net/browse/DEV-433) — MAX_RETRIES synthesis is unreachable when retries came from human gate rejections
- [DEV-617](https://keith-merrill4.atlassian.net/browse/DEV-617) — Server-side guard: a completion with completion_tokens > 0 and empty visible content should error or retry, never return silently empty
- [DEV-620](https://keith-merrill4.atlassian.net/browse/DEV-620) — A runner outage at implement time silently empties the existing-file fetch and the implementer proceeds blind — run 19's retry regenerated an 8.5K-line surface from priors with no warning logged
- [DEV-622](https://keith-merrill4.atlassian.net/browse/DEV-622) — Latent AttributeError in the DEV-538 requeue counters: Event has no `payload` accessor, so the second unreachable-runner requeue would crash the daemon loop
- [DEV-538](https://keith-merrill4.atlassian.net/browse/DEV-538) — An inconclusive pre-gate build check opens a human gate instead of retrying, so one unreachable runner stalls the pipeline indefinitely
- [DEV-543](https://keith-merrill4.atlassian.net/browse/DEV-543) — The architect burns its whole 8000-token budget and returns EMPTY content on half its generations — and the artifact we keep for diagnosis is empty by construction
- [DEV-624](https://keith-merrill4.atlassian.net/browse/DEV-624) — Implementer rotation is context-blind and a dispatch exception is terminal — a 152K-token prompt was rotated onto moe_implementer's 116K context and the spec died in 46 seconds
- [DEV-651](https://keith-merrill4.atlassian.net/browse/DEV-651) — Phase-2 residual: the synthesis REPAIR call swallows a transport failure and returns it as a failing test run, so a dead server is charged to the implementer as a verdict
- [DEV-652](https://keith-merrill4.atlassian.net/browse/DEV-652) — Phase-2 residual: five sites still charge a retry and twelve still fail a spec outside the classifier, and the runner-unreachable requeue is a parallel re-implementation of dispose's no-verdict branch

### Phase 3 — one context stage, one prompt budget ([DEV-665](https://keith-merrill4.atlassian.net/browse/DEV-665))

The runner is read once per spec and every role selects from it; every prompt section is summed against the destination window before dispatch.

- [DEV-632](https://keith-merrill4.atlassian.net/browse/DEV-632) — One context-assembly stage shared by architect, implementer, reviewer and synthesis — roles select from a single fetch instead of each plumbing its own
- [DEV-633](https://keith-merrill4.atlassian.net/browse/DEV-633) — Aggregate prompt budget — one place sums every section against the destination agent's window before dispatch; per-section knobs are inputs, not independent ceilings
- [DEV-544](https://keith-merrill4.atlassian.net/browse/DEV-544) — A transient runner outage silently strips the architect's protected-file context and still charges the attempt, so the design that reaches the gate is generated blind
- [DEV-599](https://keith-merrill4.atlassian.net/browse/DEV-599) — The architect only ever sees protected_paths — a modify-spec without them designs against an invented API, and DEV-571 closed this hole for the implementer only
- [DEV-571](https://keith-merrill4.atlassian.net/browse/DEV-571) — DEV-492's existing-file guard is keyed to an optional spec table nothing enforces — omit it and the implementer silently works blind again
- [DEV-604](https://keith-merrill4.atlassian.net/browse/DEV-604) — Manifest mode regenerates existing files whole and does not receive DEV-571's existing-file content — run 18 rewrote a 117-line class as a 33-line stub that lost its imports
- [DEV-627](https://keith-merrill4.atlassian.net/browse/DEV-627) — Protected-file reference context shares the existing-files budget knob — inflating AUTONOMOUS_EXISTING_FILES_MAX_CHARS silently inflates it too, and run 21 died on a 413 at implementer dispatch
- [DEV-648](https://keith-merrill4.atlassian.net/browse/DEV-648) — A per-section char knob drops a file the window could hold 2.3x over — run 28's architect designed blind against a 146K executor.py with 343K chars of budget free
- [DEV-649](https://keith-merrill4.atlassian.net/browse/DEV-649) — Synthesis always emits whole files, so for a modification target larger than its output budget it can only ever produce a stub — run 28 spent 77 minutes proving that arithmetic and failed the spec
- [DEV-645](https://keith-merrill4.atlassian.net/browse/DEV-645) — Single-call mode never checks that every planned implement output was produced — run 25's attempt 0 emitted only the test file, and the omission surfaced as "behaviour failures" at a human gate
- [DEV-644](https://keith-merrill4.atlassian.net/browse/DEV-644) — Nothing tells the implementer how workspace files are importable in the sandbox — run 24's three implementers all wrote `from src.coding_model_autonomous.workspace import …` and burned the rotation on ModuleNotFoundError
- [DEV-643](https://keith-merrill4.atlassian.net/browse/DEV-643) — estimate_design_file_count counts the Criterion Seams' fixture paths as design files — run 24's two-file change was dispatched in manifest mode as "~9 files"

### Phase 4 — absent is not unknown ([DEV-666](https://keith-merrill4.atlassian.net/browse/DEV-666))

Every parser and fetch a guard keys on distinguishes "nothing there" from "could not tell", and a guard fed the second arms by name.

- [DEV-630](https://keith-merrill4.atlassian.net/browse/DEV-630) — Parse and fetch helpers return unknown, not empty — downstream guards refuse or warn on unknown instead of silently disarming together
- [DEV-536](https://keith-merrill4.atlassian.net/browse/DEV-536) — The runner's overwrite/reconstruction signal is produced and never consumed
- [DEV-621](https://keith-merrill4.atlassian.net/browse/DEV-621) — The change-surface parser only matches rows whose second column starts with "modif…" — run 19's descriptive table parsed as zero declared modifications, disarming the DEV-492 plan guard and the working-blind warning
- [DEV-573](https://keith-merrill4.atlassian.net/browse/DEV-573) — The planner silently drops test_strategy.protected_paths from the normalized plan — every protection keyed to it disarms at once
- [DEV-625](https://keith-merrill4.atlassian.net/browse/DEV-625) — A plan missing test_strategy.repo is terminally failed by the DEV-492 guard at acceptance — before any gate — though a planner round with a note demonstrably fixes it
- [DEV-654](https://keith-merrill4.atlassian.net/browse/DEV-654) — The self-target sandbox overlay copies the live working tree, not a git ref — an uncommitted edit on the dev box silently becomes what a dogfood run's tests are judged against
- [DEV-655](https://keith-merrill4.atlassian.net/browse/DEV-655) — DEV-638's \"File modes — MANDATORY\" prompt section renders a syntactically valid <<<FILE:>>> block, so a model that echoes the instruction writes a junk file — runs 28 and 29 both landed `{p}` containing `...`

### Phase 5 — a retry must differ ([DEV-667](https://keith-merrill4.atlassian.net/browse/DEV-667))

Every dispatch is planned before the call; a failure two agents produced identically goes to synthesis; citations have a position; the failure stream is the identity.

The phase ticket closed on live evidence from run 39 (`spec_a5b68678`, 2026-09-15): `implementer` and `moe_implementer` each produced the coarse key `review_rejected||`, and the retry-2 `failure_classified` event carries `disposition: synthesize` with `invariant across 2 agents` — three retries before exhaustion. The synthesis delivered the slice. That was the one bar in this release no test could manufacture.

- [DEV-631](https://keith-merrill4.atlassian.net/browse/DEV-631) — Retry loop recognises invariant failures — a retry must change something (prompt, agent, temperature, environment) or classify the failure as environmental and stop spending the rotation
- [DEV-539](https://keith-merrill4.atlassian.net/browse/DEV-539) — Targeted retry treats any filename mentioned in rejection notes as a citation, so saying "the bug is not in X.swift" regenerates X.swift and can destroy a file that compiled
- [DEV-530](https://keith-merrill4.atlassian.net/browse/DEV-530) — Rotation is failure-triggered, so per-agent outcomes are confounded by construction — the current data ranks agents backwards
- [DEV-640](https://keith-merrill4.atlassian.net/browse/DEV-640) — Implementer rotation re-anchors on the previous pick when complexity.json is absent — retries 3 and 4 both dispatch to fast_implementer, breaking the "never repeat a model" contract
- [DEV-619](https://keith-merrill4.atlassian.net/browse/DEV-619) — Colon-rich clarification notes poison the planner's YAML — a Python signature in gate notes failed spec_27b1959f terminally after two identical unparseable attempts

### Phase 6 — observability, cancel, documentation ([DEV-668](https://keith-merrill4.atlassian.net/browse/DEV-668))

Three event kinds with fixed schemas, a diagnostic taxonomy, an honest Jira mirror, a first-class operator cancel, and documentation that describes the kernel.

- [DEV-529](https://keith-merrill4.atlassian.net/browse/DEV-529) — Build failures are recorded as prose, so the failure taxonomy exists only in Jira comments and cannot be queried
- [DEV-669](https://keith-merrill4.atlassian.net/browse/DEV-669) — CONTEXT_ASSEMBLED event kind and fixed, tested payload schemas for the three taxonomy events
- [DEV-482](https://keith-merrill4.atlassian.net/browse/DEV-482) — Jira mirror records a FAILED spec as Done/Done, so failed runs are indistinguishable from successful ones
- [DEV-583](https://keith-merrill4.atlassian.net/browse/DEV-583) — No safe operator cancel: cutting a run needs a raw DB write, and nothing drains the in-flight request or resets a wedged swap
- [DEV-493](https://keith-merrill4.atlassian.net/browse/DEV-493) — No way to cancel a running spec — the operator can only kill the daemon, and crash recovery resumes it
- [DEV-567](https://keith-merrill4.atlassian.net/browse/DEV-567) — Spec cancellation loses the race with an in-flight phase pass — the pass's completion write overwrites CANCELLED
- [DEV-582](https://keith-merrill4.atlassian.net/browse/DEV-582) — Cancelling a spec with an in-flight model call leaks active_requests and deadlocks every subsequent model swap
- [DEV-609](https://keith-merrill4.atlassian.net/browse/DEV-609) — scraping/ has 10 ruff errors invisible to CI (lint scope is src tests scripts)
- [DEV-611](https://keith-merrill4.atlassian.net/browse/DEV-611) — Post-release doc polish backlog from the DEV-607 audit (non-blocking WARN/NIT items)
- [DEV-670](https://keith-merrill4.atlassian.net/browse/DEV-670) — PIPELINE.md and CONFIGURATION.md rewrite for v0.2.0 — routing as the kernel does it, every AUTONOMOUS_* knob documented, .env.example matching

### After the proving runs — found by runs 31–41 (2026-09-13 to 09-16)

Every proving run found defects in what it was proving. Runs 35–37 (2026-09-14) were the second wave: a cancel drill and two self-target runs. Runs 39–41 (2026-09-15/16) were the third: two Centipede slices on the Mac runner and one self-target run, the last of them the v0.2.0 proving run (DEV-706). Fixes filed from those runs and merged before the tag are listed here; defects filed from them and not yet fixed (DEV-698/699/709/710/711) are v0.2.1 by the default-defer rule and do not appear; DEV-700 and DEV-705 appear below because part of each merged, with the open half stated.

- [DEV-660](https://keith-merrill4.atlassian.net/browse/DEV-660) — Build-failure feedback for `No module named 'src.<pkg>'` names the cause — the import root — not the missing module (pipeline-written on run 32)
- [DEV-661](https://keith-merrill4.atlassian.net/browse/DEV-661) — The testability check requires a Python design's Criterion Seams to import the code under test, never through `src.` (pipeline-written on run 34)
- [DEV-676](https://keith-merrill4.atlassian.net/browse/DEV-676) — The implementer rotation is restricted to the agents whose window fits; a rotation of one is recorded as `sole_fit` and its second identical failure is invariant; a fit-check reroute is recorded with `planned_agent`
- [DEV-674](https://keith-merrill4.atlassian.net/browse/DEV-674) — Self-target context is read from this repository's own HEAD, never from the Mac runner's clone of it
- [DEV-675](https://keith-merrill4.atlassian.net/browse/DEV-675) — The self-target pre-gate check runs the repository's own tests that import an edited module beside the spec's new tests; the gate says "N new + M existing", and a red existing test is a `tests_failed` verdict naming the test ids before any human gate (run 32 reverted DEV-672 with 8/8 green)
- [DEV-672](https://keith-merrill4.atlassian.net/browse/DEV-672) — coarse_key keys on the repository-relative path, so Mac build failures can match across attempts
- [DEV-618](https://keith-merrill4.atlassian.net/browse/DEV-618) — eval_agents.py `--tool-loop N`: inspection markers answered from an empty sandbox before judging
- [DEV-653](https://keith-merrill4.atlassian.net/browse/DEV-653) — A terminal spec status retires the spec's open gates, so no gate outlives the spec it belongs to and a stale AUTO issue can no longer reverse-sync as approval of dead work (pipeline-written on run 36)
- [DEV-677](https://keith-merrill4.atlassian.net/browse/DEV-677) — A targeted retry's planned outputs that the feedback did not cite are carried forward from the previous attempt rather than charged as missing
- [DEV-678](https://keith-merrill4.atlassian.net/browse/DEV-678) — A pass still in flight when the spec ends is discarded whole: no charge, no requeue, no gate, and the store is left exactly as the cancel left it
- [DEV-679](https://keith-merrill4.atlassian.net/browse/DEV-679) — `cancel_spec` writes one status event carrying the reason and the counts, so the Jira epic gets one mirror note instead of two
- [DEV-680](https://keith-merrill4.atlassian.net/browse/DEV-680) — A lossy Jira collapse tells the truth: the resolution write uses the operation form (so `Won't Do` now lands on a stock workflow), the note's wording matches the resolution actually set, and a `pipeline-failed` label makes the collapse queryable
- [DEV-688](https://keith-merrill4.atlassian.net/browse/DEV-688) — The pre-gate sandbox never collects the attempt's own `src/` tree, and everything under it counts as a module: a source file named `test_*.py` (this repository ships `test_runner.py`) was imported as a test module and red every attempt at collection, while being dropped from the edited-module list left DEV-675's guard silently disarmed for it
- [DEV-689](https://keith-merrill4.atlassian.net/browse/DEV-689) — A repository test that spawns its own sandbox, git checkout or npm install declares `PREGATE_SANDBOX_UNSAFE` and the existing-tests selection skips it, so it never reds an attempt for something the model did not do

- [DEV-687](https://keith-merrill4.atlassian.net/browse/DEV-687) — The two Swift-only testability rules (`missing_equatable`, `type_without_file`) are language-scoped and no longer fire on a Python design; runs 39 and 40 were the natural A/B
- [DEV-690](https://keith-merrill4.atlassian.net/browse/DEV-690) — An edit block naming a prompt-example placeholder path is named in the feedback rather than charged as an unappliable edit and sent to the ledger's closed door
- [DEV-691](https://keith-merrill4.atlassian.net/browse/DEV-691) — A truncated implementer response is a no-verdict, not a `parse_failure` charge for the outputs the truncation prevented — DEV-645's planned-output check had reopened the hole DEV-623 closed
- [DEV-701](https://keith-merrill4.atlassian.net/browse/DEV-701) — `CONTEXT_ASSEMBLED` carries `ref_state` (the runner clone's local and remote SHA and whether they agree), so a Mac runner whose clone silently lags origin is visible before a run reads a stale tree; the payload schema and its test carry the production shape
- [DEV-700](https://keith-merrill4.atlassian.net/browse/DEV-700) — `count_test_declarations` / `declaration_delta` in `test_runner.py`, so a zero or negative test delta is measurable (landed by hand from run 40; wiring the delta into the gate is the open half of the ticket)
- [DEV-705](https://keith-merrill4.atlassian.net/browse/DEV-705) — `scripts/reclaim_tart_vms.sh` reclaims leaked tart VMs on the Mac runner (the concurrency guard and hard teardown failure are the open half of the ticket)
- [DEV-707](https://keith-merrill4.atlassian.net/browse/DEV-707) — The architect runs at a 64K window with 46 of its 65 layers on the GPU instead of 128K with 36: ~+37% decode on a 16 GB card; run 41 was its first workload, four passes clean
- [DEV-703](https://keith-merrill4.atlassian.net/browse/DEV-703) — The unit's CUDA pin names the version actually in use (13.2, since August); the comment warning against 13.x was wrong
- [DEV-704](https://keith-merrill4.atlassian.net/browse/DEV-704) — README leads with "What changed since v0.1.0" — every claim traceable to a ticket or a run, and a "what has not changed" list at the same prominence; `docs/PIPELINE.md` sections 6–9 corrected against the kernel
- [DEV-508](https://keith-merrill4.atlassian.net/browse/DEV-508) — The suite is green with no `--deselect`: the month-old "known env failure" was test order-dependence, fixed in the test; the underlying import-time `load_dotenv()` leak this ticket names stays open
- [DEV-657](https://keith-merrill4.atlassian.net/browse/DEV-657) — Part 2: retrieval outcomes (`rag`, `finish_reason`) are durable on `AGENT_RAN`, so whether RAG earned its keep can be read from the event stream rather than inferred
- [DEV-297](https://keith-merrill4.atlassian.net/browse/DEV-297) — `scripts/fix_ufw_exposure.sh` no longer lists this host's addresses; a new test lane refuses any globally-routable address of this network in a tracked file, with a negative control that proves the lane fires

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
- [DEV-541](https://keith-merrill4.atlassian.net/browse/DEV-541) — The synthesis repair round overlays its output unconditionally, so a repair that regresses the build is committed and the spec dies worse than it started
- [DEV-556](https://keith-merrill4.atlassian.net/browse/DEV-556) — The architect's reasoning is unbounded, invisible and shares max_tokens with the design — add a per-role enable_thinking knob and measure whether the architect should think at all
- [DEV-558](https://keith-merrill4.atlassian.net/browse/DEV-558) — Crash recovery caps on retry_count, which human gate rejections also increment — a well-reviewed spec has ZERO crash budget and dies on the first restart
- [DEV-560](https://keith-merrill4.atlassian.net/browse/DEV-560) — A reviewer FAIL verdict over a green test run silently discards the implementation — tests are canonical in one direction only
- [DEV-563](https://keith-merrill4.atlassian.net/browse/DEV-563) — A reviewer test file that fails to PARSE still counts as a red run when other tests passed — the implementer is charged for the reviewer's SyntaxError
- [DEV-581](https://keith-merrill4.atlassian.net/browse/DEV-581) — Pipeline: apply implementer edits as diffs/anchored replacements, not whole-file re-emission
- [DEV-610](https://keith-merrill4.atlassian.net/browse/DEV-610) — Pre-flip doc accuracy blockers: scraping/ docs describe a fictional program; SECURITY_MIGRATION uses --user against system units; TUTORIAL contradicts README on llama-server and documents removed /cupertino
- [DEV-614](https://keith-merrill4.atlassian.net/browse/DEV-614) — Install Qwen3.8-27B and profile it on the RTX 5080 (bring-up + sweep, eval-only registration)
- [DEV-615](https://keith-merrill4.atlassian.net/browse/DEV-615) — Eval: Qwen3.8-27B vs dense_architect (Qwen3.6-27B MTP) for the architect/supervisor slots — DEV-99 method
- [DEV-616](https://keith-merrill4.atlassian.net/browse/DEV-616) — qwen38_architect returned an empty design on design_offline_sync — 4,677 tokens spent entirely inside the reasoning block, stopped with budget remaining
- [DEV-626](https://keith-merrill4.atlassian.net/browse/DEV-626) — Self-target pytest specs cannot pass the sandboxed pre-gate check — the bwrap sandbox never provisions the target repo's package, so every implementation reds on ModuleNotFoundError
- [DEV-635](https://keith-merrill4.atlassian.net/browse/DEV-635) — Edit-mode prompts never bound SEARCH anchor length — implementers pick 18-line anchors, fail byte-exact transcription, and burn the whole retry rotation (run 21 v3 died this way)
- [DEV-637](https://keith-merrill4.atlassian.net/browse/DEV-637) — Unappliable-edit diagnostics keep only their first line and the implementer's raw response is never persisted — run 21's six anchor misses cannot be classified after the fact
- [DEV-638](https://keith-merrill4.atlassian.net/browse/DEV-638) — Anchored-edit application is byte-exact against a transcribing model — add a tolerant match ladder that still refuses on ambiguity, and reject new-file edit blocks before they cost an attempt

## v0.1.0 — 2026-08-19

First public release: the multi-agent server, the interactive client, the autonomous pipeline through run 16, the Mac runner, the dashboard, and the security migration to loopback plus an admin key ([DEV-607](https://keith-merrill4.atlassian.net/browse/DEV-607)).
