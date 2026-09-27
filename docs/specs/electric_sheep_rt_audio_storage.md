# Electric Sheep: the audio render path holds no Swift Array

Jira: DEV-848, epic DEV-443. Repo `electric-sheep`, `main` = `a0c0803`. Every fact below
was read from that commit.

A reference implementation of exactly the edits below, with the test file below, ran on
the Mac Studio before this spec was written:

- The whole `ElectricSheepTests` suite passes: 115 passed, 0 failed, 1 skipped (the
  opt-in `LiveGenerationTests`).
- It builds for `generic/platform=visionOS`.
- The rendered audio is bit-identical to `main`'s. A probe rendered 40 buffers of 512
  frames, with strikes and level changes, through both versions, and got the same
  checksum.
- The new test file alone, against unmodified `main`, compiles, and exactly one of its two
  tests fails: `testRenderStateHoldsNoSwiftArrays`.

Do not re-derive the expected values.

## Context

`ElectricSheep/Audioscape.swift` synthesizes the audio. `AudioscapeState.render(...)` is
called on the real-time audio thread for every buffer. DEV-200 asked for "fixed-size
preallocated storage; zero Array mutation in render()". What landed removed the two
allocations DEV-200 named, but kept every piece of render-path state as a Swift `[Float]`.

A uniquely referenced `Array` mutated in place does not allocate. But nothing enforces
"uniquely referenced": one future `let copy = envelopeAmplitude` and the next write copies
the buffer on the audio thread. That malloc waits on the allocator's lock, which MLX
generation keeps busy. Keith decided on 2026-09-27 (DEV-848) that the render path holds no
Swift `Array` at all.

The fix: every stored property that `render()` reads or writes becomes
`InlineArray<8, Float>`. That is Swift 6.2's fixed-size value array, stored inside the
object, with no heap and no reference counting. The deployment targets are 26.4 on every
platform, and the toolchain is Swift 6.2 or later, so it is available. The project builds
in Swift 5 language mode, which is fine: `InlineArray` needs the compiler, not the
language mode.

`InlineArray` is used exactly like the arrays it replaces:
- `InlineArray<8, Float>(repeating: 0)` creates it;
- `x[i]` reads and writes an element;
- `let copy = x` copies all 8 values.

It has no `.map`, `.count`-driven fallback or `append`. The edits below never need them on
the render path.

Keep `setLevels(_ levels: [Float])` and the two `…ForTesting()` methods returning
`[Float]`: they run on the main thread and in tests, not in `render()`.

**Concurrency.** The target builds with `SWIFT_DEFAULT_ACTOR_ISOLATION = MainActor`.
`AudioscapeState` is already `final class …: @unchecked Sendable` and is unchanged in that
respect. The new test class follows `AudioscapeStateTests`: a plain `XCTestCase` with
synchronous methods and no actor annotations.

## Required change

### `ElectricSheep/Audioscape.swift` (modified: six anchored replacements)

Replace the stored-property block, from `targetLevels` through `defaultFrequencies`:

```swift
    private var targetLevels: [Float] = Array(repeating: 0, count: 8)

    // Pending strikes accumulator (lock-protected writes from main thread)
    private var pendingStrikeAmplitude: [Float] = Array(repeating: 0, count: 8)
    private var pendingStrikeBrightness: [Float] = Array(repeating: 0, count: 8)

    // Audio-thread-owned envelopes (merged from pending under lock, decayed lock-free)
    private var envelopeAmplitude: [Float] = Array(repeating: 0, count: 8)
    private var envelopeBrightness: [Float] = Array(repeating: 0, count: 8)

    // Audio-thread oscillator phases (no lock — only touched in render())
    private var phases: [Float] = Array(repeating: 0, count: 8)
    private var smoothedLevels: [Float] = Array(repeating: 0, count: 8)

    // Cached frequencies for the 8 modes (Hz). Recomputed when fundamental changes.
    private var modeFrequencies: [Float] = []
    private let defaultFrequencies: [Float] = Array(repeating: 110.0, count: 8)
```

with:

```swift
    // Everything render() touches is fixed-size inline storage: no Swift Array,
    // so the audio thread never reaches the allocator or an ARC retain, however
    // the code around it changes (DEV-848).
    private var targetLevels = InlineArray<8, Float>(repeating: 0)

    // Pending strikes accumulator (lock-protected writes from main thread)
    private var pendingStrikeAmplitude = InlineArray<8, Float>(repeating: 0)
    private var pendingStrikeBrightness = InlineArray<8, Float>(repeating: 0)

    // Audio-thread-owned envelopes (merged from pending under lock, decayed lock-free)
    private var envelopeAmplitude = InlineArray<8, Float>(repeating: 0)
    private var envelopeBrightness = InlineArray<8, Float>(repeating: 0)

    // Audio-thread oscillator phases (no lock — only touched in render())
    private var phases = InlineArray<8, Float>(repeating: 0)
    private var smoothedLevels = InlineArray<8, Float>(repeating: 0)

    // Cached frequencies for the 8 modes (Hz). Recomputed when fundamental changes.
    private var modeFrequencies = InlineArray<8, Float>(repeating: 110.0)
```

In `recomputeFrequencies()`, replace:

```swift
        modeFrequencies = modes.map { mn in
            fundamental * sqrtf(mn.0 * mn.0 + mn.1 * mn.1)
        }
```

with:

```swift
        for (i, mn) in modes.enumerated() {
            modeFrequencies[i] = fundamental * sqrtf(mn.0 * mn.0 + mn.1 * mn.1)
        }
```

In `render(...)`, replace the one line:

```swift
        var freqs = modeFrequencies
```

with:

```swift
        let freqs = modeFrequencies
```

In `render(...)`, delete this line and the blank line after it. `modeFrequencies` now
always holds 8 values, so the fallback is dead:

```swift
        if freqs.count < 8 { freqs = defaultFrequencies }
```

In `envelopesForTesting()`, replace:

```swift
        return (amplitude: envelopeAmplitude.map { $0 }, brightness: envelopeBrightness.map { $0 })
```

with:

```swift
        return (amplitude: (0..<8).map { envelopeAmplitude[$0] },
                brightness: (0..<8).map { envelopeBrightness[$0] })
```

In `pendingStrikesForTesting()`, replace:

```swift
        let amp = pendingStrikeAmplitude.map { $0 }
        let bri = pendingStrikeBrightness.map { $0 }
```

with:

```swift
        let amp = (0..<8).map { pendingStrikeAmplitude[$0] }
        let bri = (0..<8).map { pendingStrikeBrightness[$0] }
```

Nothing else in the file changes. In particular, the body of `render(...)` keeps every
`x[i]` access exactly as it is: `InlineArray` subscripts the same way.

### `ElectricSheepTests/AudioscapeStorageTests.swift` (new)

Write this file exactly. It is the file the reference run used:

```swift
import AVFoundation
import XCTest
@testable import ElectricSheep

/// DEV-848: the audio render path holds no Swift Array. In-place mutation of a
/// uniquely referenced Array does not allocate, but nothing enforces "uniquely
/// referenced": one added `let copy = envelopeAmplitude` and the next write
/// copies on the render thread. Fixed-size inline storage makes that
/// impossible, and this test keeps it that way.
final class AudioscapeStorageTests: XCTestCase {

    func testRenderStateHoldsNoSwiftArrays() {
        let state = AudioscapeState()
        let arrays = Mirror(reflecting: state).children
            .filter { $0.value is [Float] }
            .compactMap { $0.label }
        XCTAssertEqual(arrays, [], "render-path storage must be fixed-size, not Array")
    }

    func testEveryModeStillHasItsFrequencyAfterAFundamentalChange() {
        let state = AudioscapeState()
        state.setFundamental(220)
        state.applyStrike(modeIdx: 7, strength: 1.0, brightness: 0)
        let abl = AudioBufferList.allocate(maximumBuffers: 2)
        let frames = 256
        for i in 0..<2 {
            let mem = UnsafeMutableRawPointer.allocate(
                byteCount: frames * 4, alignment: 4)
            mem.initializeMemory(as: Float.self, repeating: 0, count: frames)
            abl[i] = AudioBuffer(mNumberChannels: 1, mDataByteSize: UInt32(frames * 4),
                                 mData: mem)
        }
        state.render(frameCount: frames, buffers: abl)
        let left = abl[0].mData!.assumingMemoryBound(to: Float.self)
        let energy = (0..<frames).reduce(Float(0)) { $0 + left[$1] * left[$1] }
        XCTAssertGreaterThan(energy, 0, "the highest mode renders at the new fundamental")
        XCTAssertEqual(state.envelopesForTesting().amplitude.count, 8)
    }
}
```

## Change surface

`ElectricSheep/Audioscape.swift` was verified to exist at `main`, and
`ElectricSheepTests/AudioscapeStorageTests.swift` was verified NOT to exist. The test
target is a synchronized folder, so a new file there needs no project edit. The plan's
implement phase lists exactly these two as outputs.

| Path | Change |
| --- | --- |
| `ElectricSheep/Audioscape.swift` | modified (six anchored replacements) |
| `ElectricSheepTests/AudioscapeStorageTests.swift` | new |

Each quoted anchor occurs exactly once in its file at `main`. The protected files below are
served read-only. Do not edit them.

## Acceptance criteria

A green build is NOT sufficient; the tests are the gate. On `main` the new test file
compiles and `testRenderStateHoldsNoSwiftArrays` fails, and the report must say so.

1. **No Array on the render path.** `testRenderStateHoldsNoSwiftArrays` passes: no stored
   property of `AudioscapeState` is a `[Float]`.
2. **Every mode still has its frequency.** After `setFundamental(220)` and a strike on mode
   7, one 256-frame `render` puts energy in the left channel, and `envelopesForTesting()`
   still returns 8 values.
3. **Behaviour unchanged.** `AudioscapeStateTests` (4 tests) and `AudioLimiterTests`
   (5 tests) pass unmodified.
4. **Whole suite green.** `xcodebuild test` over `ElectricSheepTests` passes. The opt-in
   `LiveGenerationTests` skips, as on `main`.

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
  - ElectricSheepTests/AudioscapeStateTests.swift
  - ElectricSheepTests/AudioLimiterTests.swift
  - ElectricSheepTests/AudioLifecycleTests.swift
  - ElectricSheep/AudioManager.swift
  - ElectricSheep.xcodeproj/project.pbxproj
```

## Constraints

- No `UnsafeMutablePointer`, `malloc` or manual `deinit` for this storage: `InlineArray`
  needs none.
- No `[Float]`-typed stored property left on `AudioscapeState`. That includes
  `defaultFrequencies`, which is deleted.
- These signatures do not change: `render(frameCount:buffers:afterLockSection:)`,
  `setLevels(_:)`, `applyStrike(modeIdx:strength:brightness:)`, and the two
  `…ForTesting()` methods.
- The synthesis arithmetic does not change. The output is bit-identical to `main`.

## Risks

- **Rebuilding `modeFrequencies` with `.map`.** `InlineArray` has no `map` that returns an
  `InlineArray`. Assigning the result of `modes.map { … }` to it does not compile. Use the
  loop given above.
- **Keeping the `freqs.count < 8` fallback.** `InlineArray<8, Float>` always has 8
  elements. The fallback references the deleted `defaultFrequencies` and would not
  compile.
- **Converting `setLevels` or the test accessors to `InlineArray`.** Those run off the
  audio thread and their callers pass and read `[Float]`. Changing them breaks
  `AudioscapeStateTests`, which is protected.
- **visionOS.** `Audioscape.swift` is not `#if os(visionOS)`-gated, so the macOS build
  compiles all of it. The reference also built clean for `generic/platform=visionOS`.
