# Electric Sheep: the Intensity slider actually changes the hallucination

Jira: DEV-814, epic DEV-443. Repo `electric-sheep`, `main` = `3a34f5a` (run 59's
delivery). Every fact below was read from that commit. A reference implementation of
exactly the edits below, with the tests described, ran on the Mac runner before this spec
was written: 93 passed, 0 failed. Criteria 1 to 7, run against unmodified `main`, gave 1
pass and 6 failures, as stated per criterion. Do not re-derive the expected values.

## Context

`ElectricSheep/ContentView.swift` binds the Intensity slider to the engine:

```swift
                            Slider(value: $engine.forcingIntensity, in: 0...2)
```

Nothing reads that value. `ElectricSheep/HallucinationEngine.swift` declares it

```swift
    var forcingIntensity: Float = 1.0
```

and `generate(prompt:maxTokens:)` builds each generation's forcer without it:

```swift
            // Create a forcer with the active strategy
            let forcer = HallucinationForcer(strategy: activeStrategyType.makeStrategy())
            self.currentForcer = forcer
```

`ElectricSheep/HallucinationForcer.swift` has its own `intensity` that nothing reads.
It is a plain stored property:

```swift
    var intensity: Float = 1.0
```

and `process(logits:)` applies the strategy at full strength every time:

```swift
        let corruptedLogits: MLXArray
        if logits.dtype == .float16 || logits.dtype == .bfloat16 {
            let asF32 = logits.asType(.float32)
            corruptedLogits = activeStrategy
                .corrupt(logits: asF32, context: context)
                .asType(logits.dtype)
        } else {
            corruptedLogits = activeStrategy.corrupt(logits: logits, context: context)
        }
```

**The defect.** The slider is a dead control. Moving it from 0 to 2 changes nothing
about the generated text, the particles, or the audio.

**The fix.** The forcer scales how far the strategy moves the logits:
`original + intensity * (corrupted - original)`. At 0 the logits are untouched, at 1
the output is exactly what it is today, and at 2 the deviation doubles. The blend runs
in float32 before the existing cast back, so the float16 and bfloat16 path is unchanged
in shape. The engine seeds every new forcer with `forcingIntensity`, and a slider move
during generation reaches the forcer that is currently running.

**Threading.** The slider writes on the main actor, and `process(logits:)` runs on the
MLX generation thread. `intensity` therefore moves behind the forcer's existing `lock`,
the same one that protects every other field `process` touches. It is clamped to
`0...2`, the slider's range.

**Isolation.** The app target sets `SWIFT_DEFAULT_ACTOR_ISOLATION = MainActor`. No
declaration in `HallucinationForcer.swift` carries an isolation annotation, so the new
members below carry none either: not `nonisolated`, not `@MainActor`.
`HallucinationEngine` is explicitly `@MainActor`, and its new method needs no annotation
of its own.

## Required change

### `ElectricSheep/HallucinationForcer.swift` (modified — three anchored edits)

**Edit 1.** Replace the line `    var intensity: Float = 1.0` with

```swift
    /// Corruption strength, clamped to 0...2: 0 leaves the logits untouched, 1 is the
    /// strategy's output exactly, 2 doubles its deviation (DEV-814). The Intensity slider
    /// writes it on the main actor while `process` reads it on the generation thread, so
    /// it lives behind `lock`.
    var intensity: Float {
        get { lock.withLock { _intensity } }
        set { lock.withLock { _intensity = Swift.min(Swift.max(newValue, 0), 2) } }
    }
    private var _intensity: Float = 1.0
```

**Edit 2.** In `process(logits:)`, replace the nine-line `corruptedLogits` block shown
verbatim above with

```swift
        let strength = intensity
        let corruptedLogits: MLXArray
        if logits.dtype == .float16 || logits.dtype == .bfloat16 {
            let asF32 = logits.asType(.float32)
            corruptedLogits = Self.blend(
                original: asF32,
                corrupted: activeStrategy.corrupt(logits: asF32, context: context),
                intensity: strength
            ).asType(logits.dtype)
        } else {
            corruptedLogits = Self.blend(
                original: logits,
                corrupted: activeStrategy.corrupt(logits: logits, context: context),
                intensity: strength
            )
        }
```

Read `intensity` once, into `strength`, before the branch. It must NOT be read inside a
`lock.withLock` closure, because `NSLock` is not recursive and that would deadlock.

**Edit 3.** Immediately before `    private func computeEntropy(`, add

```swift
    /// `original + intensity * (corrupted - original)`. Intensity 1 returns `corrupted`
    /// itself, so the slider's default position is exactly the pre-DEV-814 behaviour.
    static func blend(original: MLXArray, corrupted: MLXArray, intensity: Float) -> MLXArray {
        if intensity == 1 { return corrupted }
        if intensity == 0 { return original }
        return original + MLXArray(intensity) * (corrupted - original)
    }

```

Nothing else in the file changes. Specifically, the metric code, `consumeNewMetrics`,
`appendLocked`, `prompt(_:)` and `didSample(token:)` stay exactly as they are.

### `ElectricSheep/HallucinationEngine.swift` (modified — three anchored edits)

**Edit 1.** Replace the line `    var forcingIntensity: Float = 1.0` with

```swift
    /// The Intensity slider. Seeds each new forcer and reaches the live one
    /// mid-generation (DEV-814).
    var forcingIntensity: Float = 1.0 {
        didSet { currentForcer?.intensity = forcingIntensity }
    }
```

**Edit 2.** In `generate(prompt:maxTokens:)`, replace the three lines shown verbatim
above (the comment, `let forcer = ...` and `self.currentForcer = forcer`) with

```swift
            let forcer = installForcer()
```

`forcer` is still captured by the `StreamRelay.make` closure below it, exactly as
before.

**Edit 3.** Immediately before `    func startGeneration(prompt: String, maxTokens: Int = 200) {`,
add

```swift
    /// Creates the forcer for one generation from the current strategy and intensity and
    /// makes it `currentForcer`, so later slider moves reach it (DEV-814).
    @discardableResult
    func installForcer() -> HallucinationForcer {
        let forcer = HallucinationForcer(strategy: activeStrategyType.makeStrategy())
        forcer.intensity = forcingIntensity
        currentForcer = forcer
        return forcer
    }

```

`installForcer()` is internal, not private, because the tests call it.

### `ElectricSheepTests/ForcingIntensityTests.swift` (new)

XCTest, mirroring `MetricCursorTests.swift`: `import Foundation`, `import XCTest`,
`@testable import ElectricSheep`, `import MLX`, and
`final class ForcingIntensityTests: XCTestCase`. Every test method is
`@MainActor func test_...() async throws`.

Declare these file-private helpers, verbatim:

```swift
/// Deterministic: adds a fixed offset, so every expected logit is exact in float32.
private struct OffsetStubStrategy: ForcingStrategy, Sendable {
    let hallucinationType: HallucinationType = .factualErrors
    func corrupt(logits: MLXArray, context: ForcingContext) -> MLXArray {
        logits + MLXArray([Float](arrayLiteral: 2, -2, 0, 4))
    }
}

private let baseLogits: [Float] = [1, 2, 3, 4]
```

and these private helpers inside the class, verbatim:

```swift
    @MainActor private func processed(_ f: HallucinationForcer, dtype: DType = .float32) -> MLXArray {
        f.process(logits: MLXArray(baseLogits).asType(dtype))
    }

    @MainActor private func values(_ a: MLXArray) -> [Float] {
        a.asType(.float32).asArray(Float.self)
    }

    /// KL divergence of one processed token, read back through the metric path.
    @MainActor private func klDivergence(at intensity: Float) -> Float {
        let f = HallucinationForcer(strategy: OffsetStubStrategy())
        f.intensity = intensity
        _ = processed(f)
        f.didSample(token: MLXArray(Int32(7)))
        return f.consumeNewMetrics().last?.klDivergence ?? -1
    }
```

Each forcer test makes `HallucinationForcer(strategy: OffsetStubStrategy())`. Each
engine test makes `HallucinationEngine()`, as `GenerationCancellationTests.swift` does.
No test loads a model. Compare logit values with `XCTAssertEqual(values(...), [...])`.
Every expected value is exactly representable, so exact equality is correct.

## Change surface

The two modified files were verified to exist at `main`, and
`ElectricSheepTests/ForcingIntensityTests.swift` was verified NOT to exist. The plan's
implement phase lists exactly these three as outputs.

| Path | Change |
| --- | --- |
| `ElectricSheep/HallucinationForcer.swift` | modified (three anchored edits) |
| `ElectricSheep/HallucinationEngine.swift` | modified (three anchored edits) |
| `ElectricSheepTests/ForcingIntensityTests.swift` | new |

The protected files below are served read-only. Do not edit them. `ContentView.swift`
needs no change, because the slider already binds `forcingIntensity`.

## Acceptance criteria

A green build is NOT sufficient; the tests are the gate. The notes say how each
criterion behaves on unmodified `main`, and the report must say the same.

1. **The default is today's behaviour.** A fresh forcer reports `intensity == 1`, and
   `processed` returns `[3, 0, 3, 8]`. Passes on `main` too, as a regression guard.
2. **Zero leaves the logits untouched.** With `intensity = 0`, `processed` returns
   `[1, 2, 3, 4]`. On `main` it returns `[3, 0, 3, 8]`.
3. **Half is the midpoint.** With `intensity = 0.5`, `processed` returns `[2, 1, 3, 6]`.
   On `main` it returns `[3, 0, 3, 8]`.
4. **Two doubles the deviation.** With `intensity = 2`, `processed` returns
   `[5, -2, 3, 12]`. On `main` it returns `[3, 0, 3, 8]`.
5. **The float16 path blends and keeps its dtype.** With `intensity = 0.5` and
   `processed(f, dtype: .float16)`, the result's `dtype == .float16` and its values are
   `[2, 1, 3, 6]`. On `main` the values are `[3, 0, 3, 8]`.
6. **Intensity is clamped.** Setting `intensity = 5` reads back `2`, and setting
   `intensity = -1` reads back `0`. On `main` they read back `5` and `-1`.
7. **The divergence the visualisation sees follows the slider.** `klDivergence(at: 0)`
   is `>= 0` and `< 1e-5`, `klDivergence(at: 1) > 0.5`, and
   `klDivergence(at: 2) > klDivergence(at: 1)`. On `main` all three are the same value,
   about 1.1 by hand calculation.
8. **The engine seeds a new forcer.** Set `engine.forcingIntensity = 0.25`, then
   `let f = engine.installForcer()`. Then `f.intensity == 0.25` and
   `engine.currentForcer === f`. This does not compile on `main`, which has no
   `installForcer()`.
9. **The slider reaches the live forcer, and only while it is live.** Call
   `let f = engine.installForcer()` on a fresh engine; `f.intensity == 1`. Set
   `engine.forcingIntensity = 1.5`; `f.intensity == 1.5`. Call `engine.stopGeneration()`;
   `engine.currentForcer` is nil. Set `engine.forcingIntensity = 0.5`; `f.intensity` is
   still `1.5`. This does not compile on `main`.
10. **Existing behaviour intact.** `MetricCursorTests.swift`, `ForcingStrategyTests.swift`
    and `DtypeContainmentTests.swift` are unmodified and pass, and the whole
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
      - ElectricSheepTests/ForcingStrategyTests.swift
      - ElectricSheepTests/DtypeContainmentTests.swift
      - ElectricSheep/ForcingStrategy.swift
      - ElectricSheep/TokenMetrics.swift
      - ElectricSheep/ContentView.swift

## Constraints

- No new dependencies. Do not change `ForcingStrategy` or any strategy. The intensity is
  applied once, in the forcer, so every strategy gets it without being edited.
- `TokenMetrics`, `MetricsParticleBridge` and `ContentView` are not changed.
- `intensity` keeps its name and type (`Float`), and the default stays 1.
- macOS is the gate, and nothing here is platform-gated. Do not add `#if os(...)`.

## Risks

- **A deadlock.** Reading `intensity` inside `lock.withLock { ... }` in `process` locks
  a non-recursive `NSLock` twice. Read it once into `strength`, before any `withLock`.
- **Losing today's default.** If `blend` does arithmetic at intensity 1 instead of
  returning `corrupted`, the default path is no longer the same array. Criterion 1
  still passes, but the early returns are part of the contract. Keep them.
- **Blending after the cast.** In the float16 path the blend must run on the float32
  arrays, before `.asType(logits.dtype)`. Criterion 5 pins the values and the dtype.
- **A slider that only works for the next generation.** Without the `didSet`, criterion
  8 passes and criterion 9 fails.
