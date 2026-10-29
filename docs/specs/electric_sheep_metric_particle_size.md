# Electric Sheep: a metric-driven particle is the same size as a demo particle

Jira: DEV-860 (audit 2026-09-27). Repo `electric-sheep`, `main` = `2a557bf`. Every fact
below was read from that commit.

A reference implementation of exactly the edits below, with exactly the test file below,
ran on the Mac Studio (macOS host, `platform=macOS`) before this spec was written:

| Tree | `MetricParticleSizeTests` | Whole `ElectricSheepTests` |
| --- | --- | --- |
| unmodified `main` + new test file | 1 passed, 4 failed | not run |
| `main` + the edits below + new test file | 5 passed | TEST SUCCEEDED: 122 passed, 0 failed, 1 skipped (`LiveGenerationTests`) |

On `main` the bridge produced sizes `[0.2, 1.2, 2.2, 2.2, 2.2]` for entropies
`[0, 0.5, 1, 3, 10.8]` nats, in both window and immersive mode. No run was made on the
Vision Pro. Do not re-derive any of this.

## Context

Two particle-size conventions were never reconciled.

`ElectricSheep/HallucinationSimulator.swift`, in `makeParticle(type:)` (line 122), sizes
every demo particle:

```swift
            size: isImmersive ? Float.random(in: 0.04...0.15) : Float.random(in: 0.02...0.08),
```

`ElectricSheep/MetricsParticleBridge.swift`, in `process(metrics:in:audio:)` (lines 22-27),
overrides that for every particle spawned from a token metric:

```swift
            // Size scale based on entropy (clamped to reasonable bounds)
            let sizeScale = (min(max(entropy, 0), 1) + 0.1) * 2.0
            let brightnessScale = min(Swift.max(gap / 0.3, 0.1), 1.0)
            
            var particle = simulator.makeParticle(type: type)
            particle.size = Swift.max(0.01, sizeScale)
```

`ElectricSheep/Shaders.metal` line 452, in `particle_vertex`, turns size into pixels:

```metal
    float diameterPx = (p.size * 500.0) / max(clipPos.w, 0.01);
```

On the macOS window path (`w = 1`) a demo particle is 10-40 px wide. A metric-driven
particle is 0.2-2.2, i.e. 100-1100 px. `TokenMetrics.entropy` is in nats
(`HallucinationForcer.computeEntropy`, `-sum(p * log(p))`), and a corrupted distribution
over the model's ~49k-token vocabulary is routinely above 1 nat, so most metric particles
sit at the 2.2 cap.

**The fix.** The simulator becomes the one place that knows what a size means:

- `particleSizeRange` returns the mode's demo range (window `0.02...0.08`, immersive
  `0.04...0.15`), and `makeParticle` draws from it, so demo particles are unchanged.
- `metricParticleSize(entropy:)` places a metric particle in that same range:
  `lower + t * (upper - lower)` with `t = e / (1 + e)`, where `e` is the entropy clamped to
  `>= 0` (non-finite gives 0). 0 nats gives the lower bound, 1 nat the midpoint, and the
  size keeps growing past 1 nat toward the upper bound, which it never reaches.
- The bridge calls `metricParticleSize(entropy:)` instead of computing its own size.

**Isolation.** The app target sets `SWIFT_DEFAULT_ACTOR_ISOLATION = MainActor`, and
`HallucinationSimulator` is explicitly `@MainActor`. The new members carry no isolation
annotation of their own.

## Required change

### `ElectricSheep/HallucinationSimulator.swift` (modified, two anchored edits)

**Edit 1.** Immediately before the line
`    func makeParticle(type: HallucinationType) -> HalluParticle {`, insert:

```swift
    /// Size range of a freshly spawned particle in the current mode. The one place that
    /// knows what a particle size means: `particle_vertex` in `Shaders.metal` draws
    /// `size * 500 / w` pixels, so on the window path (w = 1) this is 10-40 px (DEV-860).
    var particleSizeRange: ClosedRange<Float> {
        isImmersive ? 0.04...0.15 : 0.02...0.08
    }

    /// Size of a particle spawned from a token metric: placed in `particleSizeRange` by the
    /// metric's entropy in nats. 0 nats (or a non-finite or negative value) gives the lower
    /// bound; the size grows as e / (1 + e) toward the upper bound and never reaches it, so
    /// it keeps growing past 1 nat instead of saturating there (DEV-860).
    func metricParticleSize(entropy: Float) -> Float {
        let e = entropy.isFinite ? Swift.max(entropy, 0) : 0
        let t = e / (1 + e)
        let range = particleSizeRange
        return range.lowerBound + t * (range.upperBound - range.lowerBound)
    }

```

**Edit 2.** In `makeParticle(type:)`, replace the line

```swift
            size: isImmersive ? Float.random(in: 0.04...0.15) : Float.random(in: 0.02...0.08),
```

with

```swift
            size: Float.random(in: particleSizeRange),
```

Nothing else in the file changes. In particular `maxGenesisSize`, the
`.genesisDistortion` growth and the `.entropyCascade` shrink in `applyTypeBehavior` stay
exactly as they are.

### `ElectricSheep/MetricsParticleBridge.swift` (modified, two anchored edits)

**Edit 1.** Delete these two lines:

```swift
            // Size scale based on entropy (clamped to reasonable bounds)
            let sizeScale = (min(max(entropy, 0), 1) + 0.1) * 2.0
```

**Edit 2.** Replace the line

```swift
            particle.size = Swift.max(0.01, sizeScale)
```

with

```swift
            particle.size = simulator.metricParticleSize(entropy: entropy)
```

Everything else in the file stays: the `entropy` and `gap` locals, `brightnessScale`,
`particle.color.w = brightnessScale`, `spawnParticle`, and `audio?.strike(metric)`.

### `ElectricSheepTests/MetricParticleSizeTests.swift` (new)

Write this file exactly. It is the file the reference run used. It uses only APIs that
exist at `main`, so it compiles there and fails for the right reason.

```swift
import Testing
import MLX
@testable import ElectricSheep

/// DEV-860: a particle spawned from a token metric is sized in the same range as a demo
/// particle. `Shaders.metal` draws `size * 500 / w` pixels, so on the macOS path (w = 1)
/// a demo particle is 10-40 px. Before this fix the bridge set `(entropy + 0.1) * 2`,
/// i.e. 0.2-2.2, and a metric-driven particle was 100-1100 px.
@Suite("Metric-driven particle size")
@MainActor
struct MetricParticleSizeTests {

    /// Demo ranges from `HallucinationSimulator.makeParticle` at the time of DEV-860.
    private static let windowRange: ClosedRange<Float> = 0.02...0.08
    private static let immersiveRange: ClosedRange<Float> = 0.04...0.15

    /// 0 nats up to ln(49_152) ≈ 10.8, the entropy of a uniform distribution over the
    /// model's vocabulary. Exactly five, the bridge's per-tick window.
    private static let entropies: [Float] = [0, 0.5, 1, 3, 10.8]

    private func metric(_ i: Int, entropy: Float) -> TokenMetrics {
        TokenMetrics(
            tokenID: i, tokenString: "t\(i)",
            originalTop1Probability: 0.9, modifiedTop1Probability: 0.85,
            klDivergence: 0.2, entropy: entropy,
            activeStrategy: .factualErrors, confidenceGap: 0.05,
            timestamp: 0)
    }

    /// Runs the real bridge over one metric per entropy and returns the spawned sizes,
    /// in metric order.
    private func bridgeSizes(_ entropies: [Float], immersive: Bool) -> [Float] {
        let sim = HallucinationSimulator()
        sim.isDemoMode = false
        sim.isImmersive = immersive
        let forcer = HallucinationForcer(strategy: SizeStubStrategy())
        for (i, e) in entropies.enumerated() {
            forcer.record(metric: metric(i, entropy: e))
        }
        MetricsParticleBridge().update(from: SizeTestEngine(forcer: forcer), in: sim, audio: nil)
        return sim.particles.map(\.size)
    }

    @Test("A metric-driven particle is sized within the window demo range")
    func windowSizesInDemoRange() {
        let sizes = bridgeSizes(Self.entropies, immersive: false)
        #expect(sizes.count == Self.entropies.count)
        for (e, s) in zip(Self.entropies, sizes) {
            #expect(Self.windowRange.contains(s), "entropy \(e) gave size \(s), outside \(Self.windowRange)")
        }
    }

    @Test("A metric-driven particle is sized within the immersive demo range")
    func immersiveSizesInDemoRange() {
        let sizes = bridgeSizes(Self.entropies, immersive: true)
        #expect(sizes.count == Self.entropies.count)
        for (e, s) in zip(Self.entropies, sizes) {
            #expect(Self.immersiveRange.contains(s), "entropy \(e) gave size \(s), outside \(Self.immersiveRange)")
        }
    }

    @Test("Size grows with entropy and does not saturate at 1 nat")
    func sizeGrowsWithEntropy() {
        let sizes = bridgeSizes(Self.entropies, immersive: false)
        #expect(sizes.count == Self.entropies.count)
        #expect(abs(sizes[0] - Self.windowRange.lowerBound) < 1e-6, "0 nats must give the range's lower bound, got \(sizes[0])")
        for i in 1..<sizes.count {
            #expect(sizes[i] > sizes[i - 1], "size must grow with entropy: \(sizes)")
        }
    }

    @Test("A non-finite or negative entropy gives the smallest size")
    func nonFiniteEntropy() {
        let sizes = bridgeSizes([.nan, .infinity, -1], immersive: false)
        #expect(sizes.count == 3)
        for s in sizes {
            #expect(abs(s - Self.windowRange.lowerBound) < 1e-6, "got \(s)")
        }
    }

    @Test("Demo particles keep their ranges")
    func demoRangesUnchanged() {
        let sim = HallucinationSimulator()
        for _ in 0..<200 {
            #expect(Self.windowRange.contains(sim.makeParticle(type: .factualErrors).size))
        }
        sim.isImmersive = true
        for _ in 0..<200 {
            #expect(Self.immersiveRange.contains(sim.makeParticle(type: .factualErrors).size))
        }
    }
}

private struct SizeStubStrategy: ForcingStrategy, Sendable {
    let hallucinationType: HallucinationType = .factualErrors
    func corrupt(logits: MLXArray, context: ForcingContext) -> MLXArray { logits }
}

private final class SizeTestEngine: CurrentForcerProvider {
    let currentForcer: HallucinationForcer?
    init(forcer: HallucinationForcer) { currentForcer = forcer }
}
```

Every API the file calls exists at `main`: `HallucinationForcer.init(strategy:)` and its
test-only `record(metric:)` (bottom of `HallucinationForcer.swift`),
`MetricsParticleBridge.update(from:in:audio:)`, the `CurrentForcerProvider` and
`ForcingStrategy` protocols, and `HallucinationSimulator.isDemoMode`, `isImmersive`,
`particles` and `makeParticle(type:)`. The helper types are named `SizeStubStrategy` and
`SizeTestEngine`, and are `private`, because `BridgeLifecycleTests.swift` already declares
an internal `TestEngine` at file scope; reusing that name would not compile.

## Change surface

The two modified files were verified to exist at `main`, and
`ElectricSheepTests/MetricParticleSizeTests.swift` was verified NOT to exist. The test
target is a file-system synchronized folder, so a new file there needs no project edit.
The plan's implement phase lists exactly these three as outputs.

| Path | Change |
| --- | --- |
| `ElectricSheep/HallucinationSimulator.swift` | modified (two anchored edits) |
| `ElectricSheep/MetricsParticleBridge.swift` | modified (two anchored edits) |
| `ElectricSheepTests/MetricParticleSizeTests.swift` | new |

The protected files below are served read-only. Do not edit them.

## Acceptance criteria

A green build is NOT sufficient; the tests are the gate. The notes say how each
criterion behaved on unmodified `main` in the reference run, and the report must say the
same.

1. **Window mode is in the demo range.** `windowSizesInDemoRange` passes: five metric
   particles, every size within `0.02...0.08`. On `main` all five fail
   (`0.2, 1.2, 2.2, 2.2, 2.2`).
2. **Immersive mode is in the demo range.** `immersiveSizesInDemoRange` passes: every
   size within `0.04...0.15`. On `main` all five fail, with the same sizes.
3. **Size tracks entropy past 1 nat.** `sizeGrowsWithEntropy` passes: 0 nats gives exactly
   `0.02`, and the sizes strictly increase over `[0, 0.5, 1, 3, 10.8]`. With the fix they
   are 0.02, 0.04, 0.05, 0.065, about 0.0749. On `main` it fails: 0 nats gives 0.2, and
   1, 3 and 10.8 nats all give 2.2.
4. **Bad entropy is the smallest size.** `nonFiniteEntropy` passes: NaN, +infinity and -1
   all give `0.02`. On `main` all three give 0.2.
5. **Demo particles unchanged.** `demoRangesUnchanged` passes, on `main` too, as a
   regression guard on `makeParticle`.
6. **Existing suites unchanged.** `BridgeLifecycleTests.swift` and `ElectricSheepTests.swift`
   are unmodified and pass. The whole `ElectricSheepTests` suite is green on macOS; the
   opt-in `LiveGenerationTests` skips, as on `main`.

## test_strategy

```yaml
framework: xcodebuild_test
required: true
repo: electric-sheep
base_ref: main
scheme: ElectricSheep
destination: "platform=macOS"
filter: ElectricSheepTests
default_actor_isolation: MainActor
protected_paths:
  - ElectricSheep/Shaders.metal
  - ElectricSheep/HalluRenderer.swift
  - ElectricSheep/HalluParticle.swift
  - ElectricSheep/HalluParticleData.swift
  - ElectricSheep/TokenMetrics.swift
  - ElectricSheep/HallucinationForcer.swift
  - ElectricSheepTests/BridgeLifecycleTests.swift
  - ElectricSheepTests/ElectricSheepTests.swift
  - ElectricSheep.xcodeproj/project.pbxproj
```

## Constraints

- No new dependencies. Swift Testing (`import Testing`) for the new file, not XCTest.
- Do not change the shader's `* 500.0` pixel formula, `HalluParticle`, or
  `HalluParticleData`. The fix is on the Swift side, where the size is chosen.
- Do not change the demo ranges `0.02...0.08` and `0.04...0.15`; they only move into
  `particleSizeRange`.
- Do not change brightness (`brightnessScale`, `color.w`) or the audio strike in the bridge.
- `MetricsParticleBridge` stays a stateless `struct`; no stored properties.
- Nothing here is platform-gated. Do not add `#if os(...)`.

## Risks

- **Keeping a saturating clamp.** Mapping `min(entropy, 1)` linearly into the range keeps
  criteria 1, 2 and 4 green but fails criterion 3: 1, 3 and 10.8 nats would all get the
  upper bound. Use `e / (1 + e)` exactly as written.
- **Reaching above the range.** Any formula of the form `base * (1 + entropy)` (the
  ticket's first suggestion) is unbounded, since entropy reaches ~10.8 nats; criterion 1
  catches it.
- **Visual check not done.** The tests pin the numbers; nobody has looked at the result
  in the running app. A metric particle is now 10-40 px wide in the window instead of
  100-1100 px.
- **No overlap expected** with the in-flight Vision Pro tickets DEV-908, DEV-909 and
  DEV-910, which touch `ImmersiveFrameDriver`, `ImmersiveRenderLoop` and
  `HalluRenderer`, not the simulator or the bridge.
