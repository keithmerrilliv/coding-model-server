# Electric Sheep: the Metrics panel appears, and counts every token

Jira: DEV-815, epic DEV-443. Repo `electric-sheep`, `main` = `57eee4a`. Every fact below
was read from that commit. A reference implementation of exactly the edits below, with the
tests described, ran on the Mac runner before this spec was written: 106 passed, 0
failed, 1 skipped (the opt-in `LiveGenerationTests`). The new test file alone, against
unmodified `main`, fails to compile (exit 65, 21 errors, every one "has no member" on
the four new names). The reference also builds for testing on the visionOS Simulator
destination. Do not re-derive the expected values.

## Context

`ElectricSheep/ContentView.swift` shows the Metrics panel only while the engine's metric
array is non-empty:

```swift
                if !engine.tokenMetrics.isEmpty {
                    GroupBox("Metrics") {
                        VStack(alignment: .leading, spacing: 4) {
                            Text("Tokens: \(engine.tokenMetrics.count)")
```

`ElectricSheep/HallucinationEngine.swift` declares that array

```swift
    private(set) var tokenMetrics: [TokenMetrics] = []
```

and `generate(prompt:maxTokens:)` empties it at the start of every run:

```swift
        generatedText = ""
        tokenMetrics = []
```

Nothing ever appends to it.

**The defect.** The Metrics panel (token count, tokens/sec, FPS) never appears, on any
platform.

**Why not just fill the array.** The per-token metrics live in the forcer, and the only
way to drain them, `consumeNewMetrics()`, belongs to the particle bridge
(`MetricsParticleBridge.swift`). If the engine called it, the particles would lose their
metrics. The forcer also trims its stored metrics to `maxMetrics` (200), so a count
taken from them would stop at 200.

**The fix.** The forcer already keeps a monotonic `totalAppended`: one per sampled token,
reset by `prompt(_:)`, and lowered by neither the trim nor `consumeNewMetrics()` (DEV-813).
The forcer exposes it read-only as `appendedMetricCount`. The engine mirrors it into an
observable `generatedTokenCount` as the stream runs, and the panel shows when that count
is above zero. `tokenMetrics` is deleted. The count is kept after the generation ends,
so the finished run's panel stays up, and `installForcer()` resets it to zero for the
next run.

**Isolation.** The app target sets `SWIFT_DEFAULT_ACTOR_ISOLATION = MainActor`. No
declaration in `HallucinationForcer.swift` carries an isolation annotation, so the new
member there carries none either: not `nonisolated`, not `@MainActor`.
`HallucinationEngine` is explicitly `@MainActor`, and its new members need no annotation
of their own.

## Required change

### `ElectricSheep/HallucinationForcer.swift` (modified — one anchored edit)

Immediately after this existing block

```swift
    /// Number of metrics that have been consumed (cursor position).
    var consumedMetricCount: Int {
        lock.withLock { consumedCount }
    }
```

add

```swift

    /// Metrics appended since the last `prompt(_:)`: one per sampled token. Neither the
    /// `maxMetrics` trim nor `consumeNewMetrics()` lowers it, so it is the generation's
    /// token count (DEV-815).
    var appendedMetricCount: Int {
        lock.withLock { totalAppended }
    }
```

Nothing else in the file changes. `totalAppended`, `consumeNewMetrics()`, `appendLocked`,
`prompt(_:)`, `process(logits:)` and `didSample(token:)` stay exactly as they are.

### `ElectricSheep/HallucinationEngine.swift` (modified — four anchored edits)

**Edit 1.** Replace the line `    private(set) var tokenMetrics: [TokenMetrics] = []` with

```swift
    /// Tokens sampled by the current (or most recent) generation, mirrored from its
    /// forcer's `appendedMetricCount`. Kept after the generation ends, so the Metrics
    /// panel still shows the finished run (DEV-815).
    private(set) var generatedTokenCount: Int = 0

    /// Whether the Metrics panel has anything to show.
    var showsMetrics: Bool { generatedTokenCount > 0 }
```

**Edit 2.** In `generate(prompt:maxTokens:)`, delete the line `        tokenMetrics = []`.
The line above it, `        generatedText = ""`, stays.

**Edit 3.** In `generate(prompt:maxTokens:)`, the stream loop ends like this today:

```swift
                default:
                    break
                }
            }

            if !Task.isCancelled {
```

Replace those six lines with

```swift
                default:
                    break
                }
                recordProgress(from: forcer)
            }
            recordProgress(from: forcer)

            if !Task.isCancelled {
```

The first call updates the count once per stream element. The second, after the loop,
picks up a final token that arrived without a chunk after it. `forcer` is the local that
`let forcer = installForcer()` already declares at the top of the `do` block.

**Edit 4.** In `installForcer()`, the body ends like this today:

```swift
        forcer.intensity = forcingIntensity
        currentForcer = forcer
        return forcer
    }
```

Replace those four lines with

```swift
        forcer.intensity = forcingIntensity
        currentForcer = forcer
        generatedTokenCount = 0
        return forcer
    }

    /// Copies `forcer`'s token count into `generatedTokenCount`. Reads the count only,
    /// never `consumeNewMetrics()`, which belongs to the particle bridge (DEV-815).
    func recordProgress(from forcer: HallucinationForcer) {
        generatedTokenCount = forcer.appendedMetricCount
    }
```

`recordProgress(from:)` is internal, not private, because the tests call it.

### `ElectricSheep/ContentView.swift` (modified — one anchored edit)

Replace the four lines shown verbatim in the Context section (from
`if !engine.tokenMetrics.isEmpty {` to `Text("Tokens: \(engine.tokenMetrics.count)")`)
with

```swift
                if engine.showsMetrics {
                    GroupBox("Metrics") {
                        VStack(alignment: .leading, spacing: 4) {
                            Text("Tokens: \(engine.generatedTokenCount)")
```

Nothing else in the file changes. The panel is outside every `#if os(...)` block, so one
edit covers macOS and visionOS. Once `tokenMetrics` is deleted, this file does not compile
without this edit.

### `ElectricSheepTests/MetricsPanelTests.swift` (new)

XCTest, mirroring `MetricCursorTests.swift`: `import Foundation`, `import XCTest`,
`@testable import ElectricSheep`, `import MLX`, and
`final class MetricsPanelTests: XCTestCase`. Every test method is
`@MainActor func test_...() async throws`.

Declare these file-private helpers, verbatim:

```swift
private struct PanelStubStrategy: ForcingStrategy, Sendable {
    let hallucinationType: HallucinationType = .factualErrors
    func corrupt(logits: MLXArray, context: ForcingContext) -> MLXArray { logits }
}

private func metric(_ id: Int) -> TokenMetrics {
    TokenMetrics(
        tokenID: id,
        tokenString: "t\(id)",
        originalTop1Probability: 0.9,
        modifiedTop1Probability: 0.85,
        klDivergence: 0.2,
        entropy: 1.0,
        activeStrategy: .factualErrors,
        confidenceGap: 0.05,
        timestamp: 0
    )
}
```

and this private helper inside the class, verbatim:

```swift
    @MainActor private func record(_ n: Int, into f: HallucinationForcer) {
        (0..<n).forEach { i in f.record(metric: metric(i)) }
    }
```

`record(metric:)` is the existing test helper at the bottom of `HallucinationForcer.swift`.
Each forcer test makes `HallucinationForcer(strategy: PanelStubStrategy())`. Each engine
test makes `HallucinationEngine()`, as `ForcingIntensityTests.swift` does. No test loads a
model.

## Change surface

The three modified files were verified to exist at `main`, and
`ElectricSheepTests/MetricsPanelTests.swift` was verified NOT to exist. The plan's
implement phase lists exactly these four as outputs.

| Path | Change |
| --- | --- |
| `ElectricSheep/HallucinationForcer.swift` | modified (one anchored edit) |
| `ElectricSheep/HallucinationEngine.swift` | modified (four anchored edits) |
| `ElectricSheep/ContentView.swift` | modified (one anchored edit) |
| `ElectricSheepTests/MetricsPanelTests.swift` | new |

The protected files below are served read-only. Do not edit them.

## Acceptance criteria

A green build is NOT sufficient; the tests are the gate. Criteria 1 to 9 use
`appendedMetricCount`, `generatedTokenCount`, `showsMetrics` or `recordProgress(from:)`,
none of which exists on `main`, so on `main` the test file does not compile. The report
must say so.

1. **The forcer counts appends.** A fresh forcer's `appendedMetricCount` is `0`. After
   `record(3, into: f)` it is `3`.
2. **The count is not capped.** After `record(250, into: f)`, `appendedMetricCount` is
   `250`, past the 200-metric trim.
3. **Consuming does not lower it.** After `record(10, into: f)` and two calls to
   `f.consumeNewMetrics()`, `appendedMetricCount` is `10` and `consumedMetricCount` is
   `10`.
4. **`prompt(_:)` resets it.** After `record(5, into: f)` and
   `f.prompt(MLXArray([Int32(1)]))`, `appendedMetricCount` is `0`.
5. **The real sampling path counts one per token.** With
   `let logits = MLXArray([Float](arrayLiteral: 1, 2, 3, 4))`: after
   `_ = f.process(logits: logits)` alone the count is `0`; after
   `f.didSample(token: MLXArray(Int32(7)))` it is `1`; after two more
   `process` + `didSample(token: MLXArray(Int32(8)))` pairs it is `3`.
6. **A fresh engine shows no metrics.** `HallucinationEngine()` has
   `generatedTokenCount == 0` and `showsMetrics == false`.
7. **The engine mirrors the forcer, and the bridge cannot steal the count.**
   `let f = engine.installForcer()`, `record(250, into: f)`,
   `engine.recordProgress(from: f)`: `generatedTokenCount == 250` and `showsMetrics` is
   true. Then `_ = f.consumeNewMetrics()` and `engine.recordProgress(from: f)` again:
   still `250`.
8. **The finished run's panel stays.** `let f = engine.installForcer()`,
   `record(12, into: f)`, `engine.recordProgress(from: f)`, `engine.stopGeneration()`:
   `engine.currentForcer` is nil, `generatedTokenCount == 12`, `showsMetrics` is true.
9. **The next generation starts from zero.** As in 8 up to `recordProgress`, then
   `_ = engine.installForcer()`: `generatedTokenCount == 0` and `showsMetrics` is false.
10. **Existing behaviour intact.** `MetricCursorTests.swift`, `BridgeLifecycleTests.swift`
    and `ForcingIntensityTests.swift` are unmodified and pass, and the whole
    `ElectricSheepTests` suite is green.

## test_strategy

    framework: xcodebuild_test
    required: true
    repo: electric-sheep
    base_ref: main
    scheme: ElectricSheep
    destination: "platform=macOS"
    filter: ElectricSheepTests
    default_actor_isolation: MainActor
    protected_paths:
      - ElectricSheepTests/MetricCursorTests.swift
      - ElectricSheepTests/BridgeLifecycleTests.swift
      - ElectricSheepTests/ForcingIntensityTests.swift
      - ElectricSheep/MetricsParticleBridge.swift
      - ElectricSheep/TokenMetrics.swift
      - ElectricSheep/ForcingStrategy.swift

## Constraints

- No new dependencies. `TokenMetrics`, `MetricsParticleBridge` and every strategy are not
  changed.
- The engine never calls `consumeNewMetrics()`. The bridge is its only caller.
- `tokenMetrics` is deleted, not kept alongside the count. Nothing else reads it.
- macOS is the gate, and nothing here is platform-gated. Do not add `#if os(...)`.

## Risks

- **Stealing the bridge's metrics.** Filling `tokenMetrics` from `consumeNewMetrics()`
  would make the panel appear and the particles stop. Criterion 7 pins that the engine
  reads the count only.
- **A count that sticks at 200.** Counting `metricsQueue` or the trimmed store stops at
  `maxMetrics`. Criterion 2 pins `250`.
- **A panel that vanishes when the run ends.** Resetting the count in `stopGeneration()`
  or at the end of `generate` hides the finished run's numbers. Criterion 8 pins that it
  stays; the reset belongs in `installForcer()` only (criterion 9).
- **Forgetting ContentView.** Deleting `tokenMetrics` without the ContentView edit is a
  compile error, so the build catches it before any test runs.
