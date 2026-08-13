# Stop the render callback from destroying concurrent audio strikes

Jira: DEV-594 (high).

## Context

Repo: `electric-sheep`. `Audioscape.swift` — additive Chladni synth rendered by an
`AVAudioSourceNode` callback (`render(frameCount:buffers:)`, lines 196-282). Strikes
arrive from the main thread via `applyStrike()` writing `strikeAmplitude[mode]` under
`lock`. The render callback runs ~86×/sec at 512-frame buffers.

## Problem

`render()` takes the lock twice with unlocked work between:

```swift
lock.lock()
...
var strikeAmp = strikeAmplitude       // snapshot (line 203)
let strikeBri = strikeBrightness
lock.unlock()
... synthesize ~512 frames ...
for i in 0..<8 { strikeAmp[i] *= strikeDecay; if strikeAmp[i] < 0.001 { strikeAmp[i] = 0 } }
lock.lock()
strikeAmplitude = strikeAmp           // write-back (line 279) — CLOBBERS concurrent strikes
strikeBrightness = strikeBri.map { $0 * strikeDecay }   // heap alloc on audio thread (line 280)
lock.unlock()
```

Any `applyStrike()` that lands between snapshot and write-back is overwritten and never
sounds. Against a fast token stream a steady fraction of per-token hammer hits silently
drop. Secondary defects in the same lines: `strikeBrightness = strikeBri.map { ... }`
allocates on the audio render thread every buffer, and `strikeBrightness` decays
multiplicatively with no zero-floor (drifts into denormals), unlike `strikeAmp`.

## Required change

1. Restructure so main-thread strikes are never overwritten: keep the authoritative
   decaying envelopes in audio-thread-only state (plain properties touched ONLY inside
   the callback), and turn the shared, locked state into a pending-strike accumulator:
   `applyStrike` adds energy to `pendingStrikeAmplitude[mode]` (max-combine or sum —
   pick max to match current strike semantics) under the lock; `render()` under ONE lock
   acquisition swaps pending into local storage and zeroes it, then merges into its own
   envelopes (`envelope[i] = max(envelope[i], pending[i])`) and decays them lock-free.
   No write-back of decayed values into shared state.
2. Give `strikeBrightness` the same 0.001 zero-floor as `strikeAmplitude`.
3. Remove the per-buffer heap allocation: fixed-size stack tuples or preallocated arrays
   mutated in place — no `.map` and no new `Array` inside the callback.

## Acceptance criteria

A green build is NOT sufficient — the tests below are the gate.

- Build succeeds for the macOS target with no new warnings.
- Unit test (the race, deterministically): snapshot-equivalent sequence — call
  `render()` once so envelopes are mid-decay, call `applyStrike(mode: 3, ...)`, call
  `render()` again capturing output; assert mode 3's contribution is present at full
  strike amplitude (compare RMS of a render with vs without the strike). On current
  `main` an interleaving-equivalent test (strike applied between snapshot and write-back
  via exposed test seams) loses the strike — structure the test against the new
  pending-accumulator API and state in the report why the old structure made the race
  possible and the new one makes it impossible by construction (single lock section, no
  write-back).
- Unit test: after ~200 renders with no strikes, every envelope and brightness value is
  exactly 0 (zero-floor reached, no denormal tail).
- Unit test: two `applyStrike` calls to the same mode between renders produce the max
  (not doubled) amplitude.
- The report must confirm by inspection that the callback contains no allocation,
  no `.map`, and exactly one `lock.lock()`/`unlock()` pair.

## test_strategy

    framework: xcodebuild_test
    required: true
    repo: electric-sheep
    scheme: ElectricSheep
    destination: "platform=macOS"

## Constraints

- No new dependencies; keep `NSLock` (no os_unfair_lock migration in this spec).
- Do not change synthesis math (eigenfrequencies, harmonics, panning, decay constants),
  the audio graph, or session handling (that is `electric_sheep_audio_lifecycle.md`).
- `render()` must remain callable on the audio thread with no Swift concurrency hops.

## Risks

- `render()` may be exercised by tests without a running engine — keep it a pure
  function of injected buffers (it already is; preserve that).
- Choosing sum instead of max for pending merge would change perceived loudness under
  fast token streams; max preserves current single-strike semantics.
- The headroom/clipping medium finding (8-mode sum can exceed ±1.0) is OUT of scope —
  do not add a limiter here.
