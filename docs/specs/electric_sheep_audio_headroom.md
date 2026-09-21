# Electric Sheep: soft-limit the 8-mode additive sum so a strike burst cannot hard-clip

Jira: DEV-739 (release-audit finding F10, split out of DEV-594). Repo `electric-sheep`,
`main` = `3829f66` (run 51's delivery). Every fact below was read from that commit through
the runner before this spec was written; do not re-derive them.

## Context

`ElectricSheep/Audioscape.swift` holds `final class AudioscapeState: @unchecked Sendable`
(line 132). Its `render(frameCount:buffers:afterLockSection:)` (lines 207–295) runs on the
audio thread ~86×/s: one lock section merges pending strikes into the envelopes, then a
lock-free loop synthesises eight Chladni modes and accumulates them into the two channel
buffers:

```swift
            for f in 0..<frameCount {                      // 273
                let s = sinf(phase)
                let h2 = sinf(phase * 2.0) * brightAmp
                let sample = s * amp + h2

                if let l = leftPtr  { l[f] += sample * leftGain }
                if let r = rightPtr { r[f] += sample * rightGain }

                phase += phaseInc
                if phase > twoPi { phase -= twoPi }
            }

            phases[mode] = phase
        }

        // Decay envelopes after synthesis; no write-back    // 288
        for i in 0..<8 {
```

Nothing clamps, normalises or limits the accumulated sum. Per mode
`amp = (level * 0.4 + strike * 0.5) * gain` plus `brightAmp = brightness * amp * 0.4`.
Measured on this exact code (a faithful re-implementation of the loop): with the default
`masterGain = 0.5`, `applyStrike(modeIdx: m, strength: 1.0, brightness: 1.0)` on all eight
modes, then one `render(frameCount: 512, …)` at 44.1 kHz, the peak sample is **2.05** on
channel 0 and **1.93** on channel 1 — twice full scale, hard-clipped by the output unit.
Fast token streams produce exactly this multi-mode strike burst.

The existing tests, `ElectricSheepTests/AudioscapeStateTests.swift`, are plain XCTest
methods (`final class AudioscapeStateTests: XCTestCase`, `func testRaceDeterministic()`,
no `@MainActor`) that construct `AudioscapeState()` directly, call `applyStrike` and
`render`, and read `envelopesForTesting()` / `pendingStrikesForTesting()`. They compile and
pass today (run 51). **New tests mirror that file exactly.**

## Goal

A per-sample soft limiter at the end of `render()`: transparent below a knee, smooth above
it, never reaching full scale, no allocation, no second lock. The worst-case burst above
renders bounded; quiet material is untouched; the four existing `AudioscapeStateTests`
stay byte-for-byte unchanged and green.

## Required change

### `ElectricSheep/Audioscape.swift` (modified — two insertions, nothing else)

1. Inside `AudioscapeState`, immediately after

   ```swift
       init() {
           recomputeFrequencies()
       }
   ```

   add:

   ```swift
       /// Output stays inside (-1, 1): identity up to `limiterKnee`, then a tanh
       /// soft knee that approaches but never reaches full scale (DEV-739).
       static let limiterKnee: Float = 0.8

       static func limit(_ x: Float) -> Float {
           let a = fabsf(x)
           if a <= Self.limiterKnee { return x }
           let over = (a - Self.limiterKnee) / (1.0 - Self.limiterKnee)
           let y = Self.limiterKnee + (1.0 - Self.limiterKnee) * tanhf(over)
           return x < 0 ? -y : y
       }
   ```

2. In `render`, immediately BEFORE the line
   `        // Decay envelopes after synthesis; no write-back` add one pass over both
   channels:

   ```swift
           // Soft-limit the accumulated sum (DEV-739): no allocation, no lock.
           for buf in buffers {
               if let ptr = buf.mData?.assumingMemoryBound(to: Float.self) {
                   for f in 0..<frameCount { ptr[f] = Self.limit(ptr[f]) }
               }
           }
   ```

Anchor both SEARCH blocks on the lines quoted above; they are unique in the file. Do not
touch the lock section, the mode loop, the decay loop or the `…ForTesting()` accessors.
`Self.` on every static reference (the diagnostic `static member 'limiterKnee' cannot be
used on instance of type` means ADD the qualifier; DEV-764).

### `ElectricSheepTests/AudioLimiterTests.swift` (new)

Same shape as `AudioscapeStateTests.swift`: `import AVFoundation`, `import XCTest`,
`@testable import ElectricSheep`, a file-private `makeStereoBuffer(frameCount:)` copied
verbatim from that file (it is `private` there, so it must be redeclared here — a
second file-private declaration is legal), `final class AudioLimiterTests: XCTestCase`,
plain `func test…()` methods, **no `@MainActor`, no `async`**.

Given code — the buffer helper and a peak helper:

```swift
private func makeStereoBuffer(frameCount: Int) -> UnsafeMutableAudioBufferListPointer {
    let abl = AudioBufferList.allocate(maximumBuffers: 2)
    let bytes = frameCount * MemoryLayout<Float>.size
    for i in 0..<2 {
        let mem = UnsafeMutableRawPointer.allocate(
            byteCount: bytes, alignment: MemoryLayout<Float>.alignment)
        mem.initializeMemory(as: Float.self, repeating: 0, count: frameCount)
        abl[i] = AudioBuffer(mNumberChannels: 1,
                             mDataByteSize: UInt32(bytes),
                             mData: mem)
    }
    return abl
}

private func peakAbs(_ abl: UnsafeMutableAudioBufferListPointer, frameCount: Int) -> Float {
    var peak: Float = 0
    for buf in abl {
        guard let p = buf.mData?.assumingMemoryBound(to: Float.self) else { continue }
        for i in 0..<frameCount { peak = max(peak, fabsf(p[i])) }
    }
    return peak
}
```

## Change surface

Repo-relative paths. `ElectricSheep/Audioscape.swift` was verified to exist at `main`;
`ElectricSheepTests/AudioLimiterTests.swift` verified NOT to exist. The plan's implement
phase lists exactly these two as outputs. The new test file goes in `ElectricSheepTests/`
(the project uses synchronized root groups; a file elsewhere is never compiled).

| Path | Change |
| --- | --- |
| `ElectricSheep/Audioscape.swift` | modified (two insertions) |
| `ElectricSheepTests/AudioLimiterTests.swift` | new |

The three protected files below are served read-only; do not edit them.

## Acceptance criteria

A green build is NOT sufficient — the tests are the gate. Criterion 5 FAILS on `main`
(peak 2.05); criteria 1–4 do not compile on `main` (`limit` does not exist). Say so in the
report.

1. **Identity below the knee.** `AudioscapeState.limit(0) == 0`, `limit(0.5) == 0.5`,
   `limit(-0.5) == -0.5`, `limit(0.8) == 0.8`, `limit(-0.8) == -0.8` (accuracy 1e-7).
2. **Bounded above it.** For each `x` in `[1.0, 2.0, 5.0, 100.0, 1_000_000.0]`:
   `limit(x) > 0.8`, `limit(x) <= 1.0`, and `limit(-x) == -limit(x)` (accuracy 1e-7).
   (`<=`, not `<`: `tanhf` saturates to exactly `1.0` in 32-bit float once its argument
   passes ~9, i.e. for `x >= 2.8`; full scale is the bound, and run 54 lost an attempt to
   a `<` here.)
3. **Continuous and monotonic.** `limit(0.8001) >= 0.8`, `limit(0.8001) - 0.8 < 1e-3`,
   and `limit(1.0) < limit(2.0)`, `limit(2.0) < limit(5.0)`.
4. **Quiet material passes through.** A fresh `AudioscapeState()`, one
   `applyStrike(modeIdx: 3, strength: 0.2, brightness: 0.0)`, one
   `render(frameCount: 512, buffers:)`: `peakAbs` is `> 0` and `< 0.8` (the limiter never
   engages; combined with criterion 1 the output is exactly the unlimited sum).
5. **The burst is bounded.** A fresh `AudioscapeState()`, `applyStrike(modeIdx: m,
   strength: 1.0, brightness: 1.0)` for every `m` in `0..<8`, one
   `render(frameCount: 512, buffers:)`: `peakAbs` is `<= 1.0` AND `> 0.8` (the limiter
   engaged on real signal). On `main` this peak is 2.05.
6. **Existing behaviour intact.** `ElectricSheepTests/AudioscapeStateTests.swift` is
   unmodified and its four tests pass; the whole `ElectricSheepTests` suite is green under
   the skip filter below.
7. **Inspection.** `render()` still takes the lock exactly once; the inserted pass contains
   no `.map`, no `Array(`, no `lock.` — it is two nested loops over the existing buffers.

## test_strategy

    framework: xcodebuild_test
    required: true
    repo: electric-sheep
    base_ref: main
    scheme: ElectricSheep
    destination: "platform=macOS"
    filter: ElectricSheepTests
    skip_filter: ElectricSheepTests/DtypeContainmentTests,ElectricSheepTests/ProductionDtypeConversionTests
    protected_paths:
      - ElectricSheepTests/AudioscapeStateTests.swift
      - ElectricSheep/AudioManager.swift
      - ElectricSheep/Protocols.swift

## Constraints

- No new dependencies, no `.metal`, no changes outside the two paths.
- No allocation and no lock inside the inserted pass; no change to the merge/decay
  semantics DEV-594 fixed (the four existing tests pin them).
- macOS is the gate: nothing here is platform-gated; do not add `#if os(...)`.

## Risks

- **Rewriting `render()` instead of inserting.** The change is two insertions; a whole-file
  re-emission of `Audioscape.swift` that drifts anywhere else fails criterion 6 or 7.
- **Static qualification.** `limiterKnee` is static; inside `limit` write `Self.limiterKnee`.
- **`@MainActor` on the tests.** `AudioscapeState` is used from the audio thread and its
  existing tests are plain XCTest methods; adding `@MainActor` or `async` to the new tests
  changes nothing they need and may not compile against a nonisolated helper. Mirror
  `AudioscapeStateTests.swift`.
- **Float equality.** Use `XCTAssertEqual(_:_:accuracy:)` for criteria 1–3, never `==` on
  computed floats.
