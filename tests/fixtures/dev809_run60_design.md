# Architecture: Electric Sheep Intensity Slider Integration (DEV-814)

## Overview
Wire the dead Intensity slider into the hallucination pipeline so it scales logit corruption from 0 (identity) to 2 (double deviation). The `HallucinationEngine` seeds each new `HallucinationForcer` with the slider value via `installForcer()` and pushes live changes through a `didSet`. The forcer blends original vs corrupted logits behind its existing lock using `original + intensity * (corrupted - original)` in float32 before any dtype cast back.

## Components
- **HallucinationForcer** (`final class`) — intercepts logits during generation; gains thread-safe `intensity` property backed by `_intensity`, static `blend(original:corrupted:intensity:)` helper, and updated `process(logits:)` that reads intensity once outside locks.
- **HallucinationEngine** (`@MainActor @Observable final class`) — manages model lifecycle; gains `didSet` on `forcingIntensity` to push to live forcer, and internal `installForcer()` factory used by `generate()`.
- **ForcingIntensityTests.swift** (new XCTest file) — exercises blend math at intensities 0/0.5/1/2, float16 dtype preservation, clamping bounds, KL divergence monotonicity, engine seeding, and live-scope slider reachability.

## File Structure
```
ElectricSheep/HallucinationForcer.swift   — modified: three anchored edits (intensity getter/setter, process blend call, static blend method)
ElectricSheep/HallucinationEngine.swift   — modified: three anchored edits (forcingIntensity didSet, installForcer(), generate uses installForcer)
ElectricSheepTests/ForcingIntensityTests.swift — new: XCTest suite with OffsetStubStrategy helpers
```

## Data Models

### HallucinationForcer (existing type, reference declaration)
A `final class`, not Sendable (`@unchecked Sendable`). All mutable state protected by an existing `NSLock` named `lock`.

| Member | Type / Signature | Notes |
|---|---|---|
| `activeStrategy` | `any ForcingStrategy` | set in init; never mutated after |
| `intensity` | computed property `Float { get }` / `{ set }` | **NEW**: reads/writes `_intensity` behind `lock`; setter clamps to `[0, 2]` |
| `_intensity` | `private var _intensity: Float = 1.0` | backing storage for intensity |
| `metricsQueue` | `[TokenMetrics]` read-only computed | last 5 metrics |
| `consumeNewMetrics()` | returns `[TokenMetrics]` | marks appended as consumed |
| `consumedMetricCount` | `Int` read-only computed | cursor position |
| `record(metric:)` | test helper extension method | appends directly into queue |
| `process(logits:) -> MLXArray` | LogitProcessor protocol conformance | calls `Self.blend(...)` with captured `strength` |
| `didSample(token:)` | LogitProcessor protocol conformance | unchanged |
| `prompt(_:)` | LogitProcessor protocol conformance | resets state including `_allMetrics`, etc. |
| `static blend(original:corrupted:intensity:) -> MLXArray` | static factory-style function | **NEW**: identity at 0 or 1; arithmetic blend otherwise |

### HallucinationEngine (existing type, reference declaration)
A `@MainActor @Observable final class`. Reference semantics — tests hold strong references to installed forcers and observe mutation through the same instance.

| Member | Type / Signature | Notes |
|---|---|---|
| `state` | `private(set) ModelState` | enum of lifecycle states |
| `generatedText` | `String` | accumulated output |
| `tokensPerSecond` | `Double` | throughput metric |
| `tokenMetrics` | `[TokenMetrics]` | collected metrics array |
| `currentForcer` | `HallucinationForcer?` read-write optional | set by `installForcer()`, cleared on stop/error |
| `activeStrategyType` | `HallucinationType` | current strategy selection |
| `forcingIntensity` | `Float = 1.0 { didSet }` | slider binding; **didSet pushes** value to `currentForcer?.intensity` |
| `generationController` | `GenerationTaskController` | cancellation handle |
| `loadModel(_:) async` | loads model container | unchanged |
| `generate(prompt:maxTokens:) async` | runs generation loop | calls `installForcer()` instead of inline init |
| `startGeneration(prompt:maxTokens:)` | wraps generate in controller task | unchanged |
| `stopGeneration()` | cancels and clears forcer | sets `currentForcer = nil` before state change |
| `installForcer() -> HallucinationForcer` | internal factory method | **NEW**: creates forcer, seeds intensity, assigns as currentForcer |

### ForcingIntensityTests helpers (new file)
- `OffsetStubStrategy`: struct conforming to `ForcingStrategy`; adds `[2, -2, 0, 4]` offset deterministically.
- `baseLogits`: constant `[Float] = [1, 2, 3, 4]`.
- Instance methods on test class: `processed(_:dtype:)`, `values(_:)`, `klDivergence(at:)`.

## Implementation Notes

1. **Lock discipline.** In `process(logits:)`, read `intensity` into a local `strength` BEFORE any `lock.withLock { }` call. The existing lock is non-recursive; reading the computed property inside another locked block deadlocks.

2. **Blend early returns are mandatory.** At `intensity == 1` return `corrupted` directly (not arithmetic). At `intensity == 0` return `original` directly. This preserves bit-exact identity with pre-fix behaviour at default and zero positions.

3. **Float16/bfloat16 path.** Blend runs in float32 space (`asF32`) then casts back via `.asType(logits.dtype)` AFTER blending — never before or after the strategy call separately.

4. **Clamping invariant.** Setter clamps to closed interval `[0, 2]`. Values outside this range saturate at the nearest bound. No error thrown.

5. **Isolation annotations.** Do NOT add `@MainActor` or `nonisolated` to anything in HallucinationForcer.swift. Default actor isolation applies implicitly from build settings but the forcer's methods run on MLX threads; explicit annotation would break that contract.

6. **installForcer access level.** Internal (default Swift visibility). Tests reach it through `@testable import ElectricSheep`.

7. **stopGeneration clears currentForcer.** The existing implementation already sets `currentForcer = nil`; criterion 9 depends on this clearing so subsequent slider moves do not reach a stale forcer reference.

8. **Equatable requirement.** Criterion seams compare arrays of Float values using `==`. `[Float]` is Equatable because `Float: Equatable`. No custom conformance needed beyond what stdlib provides.

## Acceptance Criteria Checklist
- [ ] C1: Fresh forcer has intensity == 1 and processed logits equal [3, 0, 3, 8].
- [ ] C2: Intensity set to 0 yields processed logits exactly [1, 2, 3, 4].
- [ ] C3: Intensity set to 0.5 yields processed logits exactly [2, 1, 3, 6].
- [ ] C4: Intensity set to 2 yields processed logits exactly [5, -2, 3, 12].
- [ ] C5: With float16 input at intensity 0.5, result dtype is .float16 and values are [2, 1, 3, 6].
- [ ] C6: Setting intensity to 5 reads back 2; setting to -1 reads back 0.
- [ ] C7: klDivergence(at: 0) >= 0 && < 1e-5; klDivergence(at: 1) > 0.5; klDivergence(at: 2) > klDivergence(at: 1).
- [ ] C8: After engine.forcingIntensity = 0.25 then installForcer(), forcer.intensity == 0.25 and currentForcer === that forcer instance.
- [ ] C9: On fresh engine, installForcer() returns f with intensity 1; forcingIntensity = 1.5 updates f.intensity to 1.5; stopGeneration() makes currentForcer nil; further forcingIntensity change leaves f.intensity at 1.5.
- [ ] C10 (suite-level): MetricCursorTests.swift, ForcingStrategyTests.swift, DtypeContainmentTests.swift unmodified and pass; full suite green.

## Criterion Seams
- C1 | setup: `let f = HallucinationForcer(strategy: OffsetStubStrategy())` | act: `let result = processed(f)` | assert: `f.intensity == 1 && values(result) == [3, 0, 3, 8]`
- C2 | setup: `var f = HallucinationForcer(strategy: OffsetStubStrategy()); f.intensity = 0` | act: `let result = processed(f)` | assert: `values(result) == [1, 2, 3, 4]`
- C3 | setup: `var f = HallucinationForcer(strategy: OffsetStubStrategy()); f.intensity = 0.5` | act: `let result = processed(f)` | assert: `values(result) == [2, 1, 3, 6]`
- C4 | setup: `var f = HallucinationForcer(strategy: OffsetStubStrategy()); f.intensity = 2` | act: `let result = processed(f)` | assert: `values(result) == [5, -2, 3, 12]`
- C5 | setup: `var f = HallucinationForcer(strategy: OffsetStubStrategy()); f.intensity = 0.5` | act: `let r = processed(f, dtype: .float16)` | assert: `r.dtype == .float16 && values(r) == [2, 1, 3, 6]`
- C6a | setup: `var f = HallucinationForcer(strategy: OffsetStubStrategy())` | act: `f.intensity = 5; let v = f.intensity` | assert: `v == 2`
- C6b | setup: `var f = HallucinationForcer(strategy: OffsetStubStrategy())` | act: `f.intensity = -1; let v = f.intensity` | assert: `v == 0`
- C7 | setup: (none — klDivergence helper constructs its own forcers internally) | act: `let d0 = klDivergence(at: 0); let d1 = klDivergence(at: 1); let d2 = klDivergence(at: 2)` | assert: `d0 >= 0 && d0 < 1e-5 && d1 > 0.5 && d2 > d1`
- C8 | setup: `let engine = HallucinationEngine(); engine.forcingIntensity = 0.25` | act: `let f = engine.installForcer()` | assert: `f.intensity == 0.25 && engine.currentForcer === f`
- C9a | setup: `let engine = HallucinationEngine()` | act: `let f = engine.installForcer()` | assert: `f.intensity == 1`
- C9b | setup: `(engine, f) from C9a` | act: `engine.forcingIntensity = 1.5` | assert: `f.intensity == 1.5`
- C9c | setup: `(engine, f) from C9b` | act: `engine.stopGeneration()` | assert: `engine.currentForcer == nil`
- C9d | setup: `(engine, f) from C9c` | act: `engine.forcingIntensity = 0.5` | assert: `f.intensity == 1.5`
