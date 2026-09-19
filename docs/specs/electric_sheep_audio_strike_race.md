# Stop the render callback from destroying concurrent audio strikes

Jira: DEV-594 (High). The same edit closes DEV-200 (Highest — heap allocation on the
real-time audio thread) and DEV-201 (High — the lock taken twice per callback): all
three findings are in the same twelve lines and cannot be fixed independently.

## Context

Repo `electric-sheep`, one source file: **`ElectricSheep/Audioscape.swift`**. Every
line number below was read from `main` before this spec was submitted.

That file declares two types:

- `Audioscape` — `@MainActor final class` (lines 16–117), owns the `AVAudioEngine`
  graph. **Not under test and not to be edited.** Its render block (lines 101–105)
  calls `sharedState.render(frameCount:buffers:)`.
- `AudioscapeState` — `final class AudioscapeState: @unchecked Sendable` (lines
  125–283). This holds all synth state and is the type under test. It is `internal`,
  so `@testable import ElectricSheep` reaches it; every *stored property* is
  `private`, which is why this spec adds accessors (item 5).

`render(frameCount:buffers:)` (lines 196–282) runs on the audio thread ~86×/sec
(44,100 Hz ÷ 512 frames). Strikes arrive on the main thread via
`Audioscape.strike(_:)` → `AudioscapeState.applyStrike(modeIdx:strength:brightness:)`
(lines 172–179).

## Problem

Three defects, all inside `render()`, all quoted verbatim from `main`.

**1 — the write-back destroys concurrent strikes (DEV-594).** `render()` takes the
lock twice with the whole synthesis pass in between:

```swift
lock.lock()                                             // 198
var strikeAmp = strikeAmplitude                         // 203  snapshot
let strikeBri = strikeBrightness                        // 204
lock.unlock()                                           // 207
    ... ~512 frames of synthesis, unlocked ...
for i in 0..<8 {                                        // 274–277
    strikeAmp[i] *= strikeDecay
    if strikeAmp[i] < 0.001 { strikeAmp[i] = 0 }
}
lock.lock()                                             // 278
strikeAmplitude = strikeAmp                             // 279  CLOBBERS
strikeBrightness = strikeBri.map { $0 * strikeDecay }   // 280
lock.unlock()                                           // 281
```

Any `applyStrike()` landing between line 203 and line 279 is overwritten and never
sounds. Against a fast token stream a steady fraction of per-token hammer hits drop
silently — no error, no log, just a quieter instrument than the simulator asked for.

**2 — heap allocation on the render thread (DEV-200).** Line 280's `.map` allocates a
new 8-element array every buffer and frees the old one, ~86 times a second, inside the
lock. Line 275 mutates `strikeAmp`, a copy of a shared array, so it mallocs again via
copy-on-write. Allocator-lock contention — worst exactly when MLX generation is
hammering `malloc`, which is when the instrument is supposed to be playing — stalls the
callback past its deadline and produces clicks and dropouts.

**3 — the lock is taken twice per callback (DEV-201).** Lines 198/207 and 278/281. The
second acquisition is only there to serve the write-back that defect 1 deletes.

Secondary, same lines: `strikeBrightness` decays multiplicatively (line 280) with **no
zero-floor**, unlike `strikeAmplitude` (line 276), so brightness drifts downward
forever into denormals instead of reaching 0.

## The semantics that must NOT change — read this before writing the merge

The intuitive guess here is wrong, so it is stated explicitly. In `applyStrike`
(lines 176–177):

```swift
strikeAmplitude[modeIdx] = min(1.0, strikeAmplitude[modeIdx] + strength)  // SUM, saturating
strikeBrightness[modeIdx] = max(strikeBrightness[modeIdx], brightness)    // MAX
```

**Amplitude combines by saturating sum; brightness combines by max.** Two strikes of
0.3 to the same mode between two renders must still give **0.6**, not 0.3. Two of 0.7
must give **1.0**. Preserve both rules exactly — a merge rule of `max` for amplitude
would change how the instrument sounds under a fast token stream, and this spec is a
correctness fix, not a voicing change.

Two more behaviours to preserve, both load-bearing for the tests below:

- **A strike sounds at full amplitude in the buffer it first reaches.** Decay is
  applied *after* synthesis (lines 274–277), not before it.
- **A silent mode does not advance its phase.** `if amp < 0.0001 { continue }` (line
  238) sits above `phases[mode] = phase` (line 270).

## Required change

1. **One lock section, no write-back.** Keep the authoritative decaying envelopes in
   audio-thread-owned storage — the same treatment `phases` and `smoothedLevels`
   already get (lines 139–140) — and turn the shared state into a *pending-strike
   accumulator*:
   - `applyStrike` adds into `pendingStrikeAmplitude` / `pendingStrikeBrightness`
     under the lock, using the saturating-sum and max rules above, unchanged.
   - `render()` takes the lock **exactly once**, copies the parameters it needs, folds
     pending into its own envelopes (`envelope[i] = min(1.0, envelope[i] + pending[i])`,
     `brightEnvelope[i] = max(brightEnvelope[i], pendingBright[i])`), zeroes the pending
     arrays in place, and releases the lock.
   - Synthesis and decay then run entirely lock-free on audio-thread-owned state, and
     **nothing is written back into shared state**. A strike arriving after the lock is
     released survives in `pending` and sounds in the next buffer, which is the
     strongest form of the fix: the race is impossible by construction, not by timing.
2. **Give `strikeBrightness` the same zero-floor as the amplitude** — after decaying,
   `if brightEnvelope[i] < 0.001 { brightEnvelope[i] = 0 }`, matching line 276.
3. **No heap allocation inside `render()`.** Concretely: no `.map`, no `Array(...)`
   constructor, no array *copy that is then mutated*. Envelope and pending storage are
   preallocated 8-element `[Float]` properties **mutated in place and never reassigned
   inside the callback** — in-place mutation of a uniquely referenced array does not
   allocate, which is why the existing `phases`/`smoothedLevels` writes are already
   safe. Line 209's `freqs = Array(repeating: 110.0, count: 8)` fallback is a second
   (currently unreachable) allocation on the same thread: replace it with a
   preallocated `defaultFrequencies` stored property built once in `init`. Do not
   simply delete the guard.
4. **Add a render test seam** so the race is testable without threads:
   ```swift
   func render(frameCount: Int, buffers: UnsafeMutableAudioBufferListPointer,
               afterLockSection: (() -> Void)? = nil)
   ```
   Invoked exactly once, immediately after the single lock section is released, and
   only when non-nil. The default argument keeps the production call site at lines
   101–105 compiling unchanged; a nil closure allocates nothing.
5. **Add two test-visible accessors on `AudioscapeState`.** They are never called from
   the audio thread. Each returns fresh copies (`map { $0 }`), so no live reference to
   the envelope buffers outlives the call:
   ```swift
   func envelopesForTesting() -> (amplitude: [Float], brightness: [Float])
   func pendingStrikesForTesting() -> (amplitude: [Float], brightness: [Float])
   ```
   `pendingStrikesForTesting()` must read under the lock. Do not name either of these
   with a leading `test`.

## Change surface

Repo-relative paths, so context assembly can resolve them. The modified path was
verified to exist at `main` before this spec was submitted.

| Path | Change |
| --- | --- |
| `ElectricSheep/Audioscape.swift` | modified |
| `ElectricSheepTests/AudioscapeStateTests.swift` | new |

The new test file goes in **`ElectricSheepTests/`** — that exact directory at the
repository root, alongside the existing `ElectricSheepTests/BridgeLifecycleTests.swift`.
It is also the value of `test_strategy.filter`. The Xcode project uses synchronized
root groups for exactly `ElectricSheep/` and `ElectricSheepTests/`, so *a source file
written anywhere else is never compiled and never joins a target* — a test placed at
the repository root or under `Tests/` would leave the suite green while the new tests
silently do not exist.

`ElectricSheep/Audioscape.swift` is 296 lines; edit it in place. Do not regenerate it
whole, and do not touch the `Audioscape` class or the `HallucinationType` extension at
lines 287–296.

## Reference files (read-only)

The test strategy protects three files. They are **reference, not scope** — read them
if something is unclear, do not edit them.

- `ElectricSheep/TokenMetrics.swift` and `ElectricSheep/HalluParticle.swift` define
  `TokenMetrics` and `HallucinationType`. The tests below need **neither**: they drive
  `AudioscapeState` directly with plain `Int` and `Float` arguments and never construct
  an `Audioscape`, a `TokenMetrics` or an `AVAudioEngine`.
- `ElectricSheepTests/BridgeLifecycleTests.swift` is the house style for a new test
  file in this repo — `import XCTest`, `@testable import ElectricSheep`, a
  `final class ...: XCTestCase`. Follow it. Four of the five existing test files use
  swift-testing (`import Testing`) instead; either framework is accepted, but do not
  mix the two in one file.

Every path in the plan's phases should be one of the repo-relative paths in the change
surface above. A bare filename such as `Audioscape.swift` is not a harmless shorthand:
it is the defect class of DEV-601.

## Given code — the test's buffer helpers

**Use these as written.** They are provided, not an exercise. Nothing in
`electric-sheep` has ever *allocated* an `AudioBufferList` — the repository's only
two mentions both consume the one `AVAudioSourceNode` hands them — so there is no
precedent to copy and no reason for four different inventions of it.

```swift
import AVFoundation
import XCTest
@testable import ElectricSheep

/// A stereo, zero-filled buffer list of `frameCount` Float frames per channel,
/// shaped exactly as `render(frameCount:buffers:)` expects.
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

/// Root-mean-square across every channel. 0 when the buffer is silent.
private func rms(_ abl: UnsafeMutableAudioBufferListPointer,
                 frameCount: Int) -> Float {
    var sum: Float = 0
    var n = 0
    for buf in abl {
        guard let p = buf.mData?.assumingMemoryBound(to: Float.self) else { continue }
        for i in 0..<frameCount { sum += p[i] * p[i]; n += 1 }
    }
    return n == 0 ? 0 : sqrtf(sum / Float(n))
}

/// Channel-major copy, for sample-for-sample comparison of two renders.
private func samples(_ abl: UnsafeMutableAudioBufferListPointer,
                     frameCount: Int) -> [[Float]] {
    abl.map { buf in
        guard let p = buf.mData?.assumingMemoryBound(to: Float.self) else { return [] }
        return (0..<frameCount).map { p[$0] }
    }
}
```

Three rules about them:

1. **Do not substitute an `AVAudioPCMBuffer`.** It is the obvious alternative and
   it carries a lifetime trap: `UnsafeMutableAudioBufferListPointer` does not
   retain the buffer, so a helper that builds a `PCMBuffer` locally and returns
   only the pointer hands back a dangling one the moment ARC releases it. The raw
   allocation above has no such coupling.
2. **Allocate a fresh buffer per captured render.** Two renders compared against
   each other must not share storage, or the comparison is of one buffer with
   itself.
3. **If any of this does not compile, repair it MINIMALLY and say so in the
   report.** These helpers were written against the compiler's own diagnostics
   from a previous attempt but were not compiled by the author. Fix the line, keep
   the shape, and do not redesign the test around a different buffer strategy.

## Acceptance criteria

A green build is NOT sufficient — the four tests below are the gate. All of them
are deterministic: single-threaded, fixed `frameCount` of 512, and the default sample rate
of 44,100 (construct `AudioscapeState()` directly and never call `setSampleRate`).

- **Build succeeds for the macOS target.** If the change introduces a new compiler
  warning in `Audioscape.swift`, quote it in the report rather than leaving it to be
  discovered.

- **The race, deterministically (DEV-594).** Two sequences on two fresh
  `AudioscapeState` instances, each rendering 512 frames into a 2-channel buffer:
  - *Control*: `render()`; then `applyStrike(modeIdx: 3, strength: 1.0, brightness: 0.5)`;
    then `render()` capturing the output.
  - *Interleaved*: `render(frameCount:buffers:afterLockSection:)` where the closure
    calls the identical `applyStrike`; then `render()` capturing the output.

  Each capture uses its own `makeStereoBuffer(frameCount: 512)`. Compare with
  `samples(...)` and measure with `rms(...)` from the Given code above. Assert the two
  captured buffers are **equal sample-for-sample** (accuracy 1e-6) and that their RMS
  is **> 0**. Both halves are required: equality alone passes on two silent buffers. This is exact rather than approximate because the first render of
  each sequence is silent for mode 3 (`amp < 0.0001` → `continue`), so it advances no
  phase and both second renders start from the same state. On `main` the interleaved
  strike is destroyed by the line 279 write-back and its buffer is silent, so the test
  distinguishes the old structure from the new one.

- **The zero-floor is reached, in both envelopes (secondary defect).**
  `applyStrike(modeIdx: 0, strength: 1.0, brightness: 1.0)`, then **300** renders of
  512 frames. Assert `envelopesForTesting().amplitude[0] == 0` **and**
  `.brightness[0] == 0`, exactly. 300 is not arbitrary: the per-buffer decay is
  `expf(-512 / (44100 × 0.4))` = 0.971392, and 0.971392^238 first falls below the 0.001
  floor, so 238 renders are required and 300 leaves margin. A test that renders ~200
  times fails on correct code; a test that renders without striking first passes
  vacuously, because the envelopes start at 0.

- **Accumulation semantics are preserved.** On a fresh instance, before any render:
  - `applyStrike(2, 0.3, 0.1)` then `applyStrike(2, 0.3, 0.4)` →
    `pendingStrikesForTesting().amplitude[2]` ≈ **0.6** and `.brightness[2]` ≈ **0.4**
    (sum for amplitude, max for brightness; accuracy 1e-6).
  - `applyStrike(5, 0.7, 0.0)` twice → `.amplitude[5]` ≈ **1.0** (saturates).

- **The merge consumes pending and decays after synthesis.** `applyStrike(2, 0.3, 0.4)`,
  then one `render()`. Assert `pendingStrikesForTesting().amplitude[2] == 0` (pending
  was consumed and zeroed) and `envelopesForTesting().amplitude[2]` ≈ **0.291418**
  (= 0.3 × 0.971392, accuracy 1e-5) — which pins that the strike sounded at its full
  0.3 in that buffer and was decayed only afterwards.

- **The report must state, by quoting the final `render()` body:** that it contains
  exactly one `lock.lock()`/`lock.unlock()` pair, no `.map`, no `Array(...)`
  constructor, and no assignment into `pendingStrikeAmplitude` or
  `pendingStrikeBrightness` other than zeroing them inside that one lock section.

## test_strategy

    framework: xcodebuild_test
    required: true
    repo: electric-sheep
    base_ref: main
    scheme: ElectricSheep
    destination: "platform=macOS"
    filter: ElectricSheepTests
    skip_filter: ElectricSheepTests/DtypeContainmentTests
    protected_paths:
      - ElectricSheep/TokenMetrics.swift
      - ElectricSheep/HalluParticle.swift
      - ElectricSheepTests/BridgeLifecycleTests.swift

## Constraints

- No new dependencies; keep `NSLock` — no `os_unfair_lock` migration in this spec.
- Do not change synthesis math: eigenfrequencies, the harmonic, panning, `smoothCoef`,
  `strikeDecay`, the `0.4`/`0.5`/`0.0001` constants, or `masterGain` handling.
- Do not change the audio graph or session handling — that is
  `electric_sheep_audio_lifecycle.md`.
- `render()` must stay callable on the audio thread with no Swift concurrency hops:
  no `await`, no `Task`, no actor isolation on `AudioscapeState`.
- Do not edit `ElectricSheepTests/DtypeContainmentTests.swift`; it is quarantined by
  `skip_filter` under DEV-603 and is not this spec's problem.

## Risks

- **Choosing `max` for the amplitude merge** is the likeliest wrong turn — see "The
  semantics that must NOT change". It is a behaviour change disguised as a
  simplification.
- `render()` may be exercised by tests with no running engine. It is already a pure
  function of its injected buffers; preserve that. It zeroes the output buffers itself
  (lines 224–230), so a test may reuse one allocation across renders.
- `envelopesForTesting()` must not hand out a live reference to the envelope storage:
  a retained second reference would make the next in-place write copy-on-write, which
  is the allocation this spec exists to remove.
- The headroom/clipping finding (the 8-mode sum can exceed ±1.0) is **out of scope** —
  do not add a limiter here.
