# Contain MLX runtime errors: one bad call fails one test, never the host

Do not create, comment on, or transition any Jira issue in the DEV project — this spec and its own AUTO task are the only work record.

## Context

Repo: `electric-sheep` (registered in the Mac runner's `repos.yml`). Language: Swift. macOS/visionOS SwiftUI app; text generation runs through MLX (`mlx-swift`, `mlx-swift-examples`).

Today, any MLX runtime error raised inside a forcing strategy escalates through
mlx-swift's error trampoline into a fatal trap (`EXC_BREAKPOINT`/SIGTRAP) that
kills the shared XCTest host process. Measured on 2026-08-05: a single test
fixture built with bare float literals (`MLXArray([3.0, 5.0, 1.0, 4.0])` —
which is **float64**, and Metal cannot `argSort` float64) killed the host, and
**twelve completely unrelated pre-existing tests** were reported as 0.000s
failures alongside the three new ones. The suite looked comprehensively broken
over one bad line in one test. This spec makes an MLX error cost exactly what
it should: the test that caused it.

**There is no type or file named `ErrorHandler` in this repo** — the fatal path
lives in the mlx-swift dependency (`_mlx_error → errorHandlerTrampoline →
ErrorHandler.dispatch → assertionFailure`). The fix is therefore installed from
the app side; do not go looking for an app-owned ErrorHandler to edit.

Everything a planner might otherwise ask about (repo layout, test framework,
test infrastructure, dependency policy) is answered below. **Do not raise a
clarification gate.**

## Repository layout

`ElectricSheep.xcodeproj` sits at the **repo root**. This is a **plain Xcode
project, not a SwiftPM package.**

```
<repo root>/
├── ElectricSheep.xcodeproj/          # plain Xcode project, objectVersion = 77
├── ElectricSheep/                    # app sources (.swift + Shaders.metal + assets)
├── ElectricSheepTests/               # unit tests: ElectricSheepTests.swift,
│                                     #   ForcingStrategyTests.swift,
│                                     #   GenerationCancellationTests.swift
├── ElectricSheepUITests/             # Apple XCTest boilerplate — do not touch
```

**There is no `Sources/` and no `Tests/` directory — do not invent SPM-style
paths.** The project uses Xcode 16 file-system-synchronized root groups:
target membership is determined purely by which directory a file sits in, and
`project.pbxproj` names no `.swift` file at all. Two hard requirements follow:

- **To add a file to a target, drop it in the directory. Make NO edit to
  `project.pbxproj`** (it is a protected path in this spec).
- **A file written outside `ElectricSheep/` or `ElectricSheepTests/` is
  compiled by nothing and fails silently green.** A prior run on this repo
  wrote SPM-style paths, reported PASS in 2.3s, and had run only the
  pre-existing tests. Guarding that false green is acceptance criterion A4.

## Required behavior

1. **An MLX runtime error raised during a `ForcingStrategy.corrupt` call must
   not terminate the process.** It must surface as a readable per-test failure
   (a thrown Swift error reaching the test, or an XCTFail with the MLX message
   — the design decides the mechanism and states it once).
2. **Float64 input is rejected before it can reach a Metal op.** At the entry
   to each forcing strategy's `corrupt(logits:context:)`, a non-float32
   `logits` array produces a clear, recoverable failure naming the actual
   dtype. NOTE: `assertionFailure` and `precondition` also trap the host — a
   trap with a better message does not satisfy this. The strategy protocol
   `ForcingStrategy.corrupt(logits:context:) -> MLXArray` is currently
   non-throwing; if the design changes it to `throws`, every conforming
   strategy and call site changes with it, stated in the design's File
   Structure — or the design may keep the signature and route the failure
   through the installed error path of requirement 1. Either is acceptable;
   pick one and state it once.
3. **The app's normal generation path behaves as before** when inputs are
   valid — no behavioral change to hallucination output, particles, or UI.

## Change surface

Ground truth for the DEV-492 read path: the implementer must be shown the
current contents of every file marked *modified* below before it writes
anything.

| Path | Action |
|---|---|
| `ElectricSheep/ForcingStrategy.swift` | modified — insert one dtype-guard line at the top of each of the eight `corrupt` implementations; every other declaration is preserved byte-for-byte |
| `ElectricSheep/DtypeValidation.swift` | created — error type, failure router, guard helper |
| `ElectricSheepTests/DtypeContainmentTests.swift` | created — the A1/A2 tests |

## Out of scope

- Fixing or changing any pre-existing test.
- Changes to mlx-swift itself (it is a dependency; the containment is
  installed from app code).
- Upstreaming, sound, UI work, or anything not listed above.

## Acceptance criteria

`xcodebuild test` (filtered to `ElectricSheepTests`) passes, and coverage
includes at minimum:

- A1: A test that deliberately passes a float64 `MLXArray` into a forcing
  strategy **fails that test with a readable message naming the dtype** — and
  every other test in the suite still runs and reports its own result. No
  0.000s cascade.
- A2: A test that passes a valid float32 array through each of the EIGHT
  conforming strategies still succeeds (behavioral no-change pin). The
  conformers all live in `ElectricSheep/ForcingStrategy.swift`:
  `GaussianNoiseStrategy`, `RankInversionStrategy`, `ConfidenceClampStrategy`,
  `RepetitionBoostStrategy`, `ContextCorruptionStrategy`,
  `TemperatureOscillationStrategy`, `RestrictedSamplingStrategy`,
  `ProgressiveNoiseStrategy` — every one has a zero-argument initializer, and
  `HallucinationType` is `CaseIterable` with a `makeStrategy()` factory
  covering all eight.
- A3: All pre-existing tests pass unchanged — `ElectricSheepTests.swift`,
  `ForcingStrategyTests.swift`, `GenerationCancellationTests.swift` are
  protected paths and are the regression contract.
- A4: The new test file lives at `ElectricSheepTests/<name>.swift` (no
  SPM-style paths), and the suite's reported test count STRICTLY EXCEEDS the
  pre-change count — a run that executes only the pre-existing tests must
  read as a failure of this criterion, not a pass.

## test_strategy

```yaml
framework: xcodebuild_test
required: true
repo: electric-sheep
scheme: ElectricSheep
destination: "platform=macOS"
filter: ElectricSheepTests
execution_target: client
protected_paths:
  - ElectricSheep.xcodeproj/project.pbxproj
  - ElectricSheepTests/ElectricSheepTests.swift
  - ElectricSheepTests/ForcingStrategyTests.swift
  - ElectricSheepTests/GenerationCancellationTests.swift
```

`filter` and `execution_target` are **real YAML keys, not prose**. `filter`
becomes `-only-testing:`; without it xcodebuild also runs
`ElectricSheepUITests`, which cannot launch an app under the runner and
crashes the whole run (DEV-394). `execution_target: client` because the
implement phase needs macOS-only tooling. `project.pbxproj` is protected
because file-system-synchronized groups make every edit to it unnecessary and
therefore wrong.

## Risks

- The trap fires inside the mlx-swift dependency, so containment depends on
  whatever override surface mlx-swift exposes (error handler installation /
  fatal-path configuration). If no such surface exists, requirement 2's dtype
  rejection at strategy entry is the containment on its own — the design must
  say which mechanism it is using and why.
- The dtype guard must not itself trap (see requirement 2's note); the exact
  failure plumbing through a non-throwing protocol is the design's central
  decision. Runs 9–13's lesson applies: state it once, as a conclusion.
- Baseline test counts and layout claims were verified on main at `e2984a1`
  from the zooshly-side clone, and the Mac runner's clone was confirmed at that
  commit on 2026-08-12 (runner `read_files` returned the post-merge protected
  test files).
