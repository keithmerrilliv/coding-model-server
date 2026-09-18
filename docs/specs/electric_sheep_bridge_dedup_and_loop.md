# Consume token metrics exactly once and give the bridge loop a real lifecycle

Jira: DEV-592 (high), DEV-593 (high). Same loop, one change set.
Run 42 (DEV-728). Third submission — the two previous attempts died of pipeline
defects, not model error, and both are now fixed: DEV-698 (context served no
protected files) and DEV-722 (the design never declared the mutability contract).
The "Required change" section below is rewritten to remove the trap that caused
the second failure rather than relying on the new checker to catch it.

## Context

Repo: `electric-sheep`. `ElectricSheepApp.swift` spawns `runBridgeLoop()` (lines 75-100)
from `.onAppear` (lines 36-40 visionOS, 51-54 macOS) as `Task { await runBridgeLoop() }`.
`runBridgeLoop()` itself sits OUTSIDE any `#if`, so it compiles and runs under
`platform=macOS`; only the two call sites are platform-gated. This spec changes the
macOS call site only — see "Required change" below.
The loop ticks at ~90 Hz: calls `bridge.update(from:in:audio:)`, steps the simulator with
its own `dt`, and pushes audio levels. `MetricsParticleBridge.update` (lines 13-19) does:

```swift
guard let forcer = engine.currentForcer else { return }
let recentMetrics = Array(forcer.metricsQueue.suffix(5))
if !recentMetrics.isEmpty {
    process(metrics: recentMetrics, in: simulator, audio: audio)
}
```

`process` spawns one particle AND one `audio?.strike(metric)` per metric, per call.

## Problem

1. **No consumption tracking (DEV-592):** `suffix(5)` re-reads the same metrics every
   tick. At ~25 tok/s each metric sits in the window ~18 ticks → ~18 duplicate particles
   and ~18 duplicate audio strikes per token (~450 spawns/sec), saturating the simulator's
   particle cap in seconds. When generation pauses, the last 5 metrics replay forever.
2. **No cancellation, duplicate loops (DEV-593):** the `Task` is unstructured, never
   stored, never cancelled — `while !Task.isCancelled` can never trip. On macOS, closing
   and reopening the window from the Dock re-runs `.onAppear`, starting a second
   concurrent loop: 2× simulation speed (each loop integrates its own dt) and 180 Hz
   `pushLevels`; orphaned loops burn CPU at 90 Hz forever. Every reopen adds one more.

## Required change

1. Give metric consumption a monotonic cursor, and **put the cursor in
   `HallucinationForcer`, not in the bridge.**

   The forcer is already a `final class` holding `_allMetrics` behind an `NSLock`, so a
   cursor there is thread-safe by construction and needs no new synchronisation. Add a
   method that returns the metrics not yet consumed and advances the cursor in the same
   locked region — for example `consumeNewMetrics() -> [TokenMetrics]`. Reset it when a
   new generation starts (the existing `_allMetrics = []` path).

   `MetricsParticleBridge` then **stays a stateless `struct` with a non-mutating
   `update`**, and its call sites are untouched.

   *This is a deliberate change from the previous attempt at this spec, which asked the
   BRIDGE to hold `lastConsumedCount`. That is where three implementations died.* Adding
   stored state to a `@MainActor struct` whose `update` is non-mutating forces a choice
   — mark the members `mutating` and every `let bridge` call site breaks, or leave them
   non-mutating and the assignment does not compile. Both branches are compile errors and
   no rule says which way out to take, because it is not a rule question. Keeping the
   state in the class that already has a lock removes the dilemma instead of resolving it.

   If you have a reason to put the cursor in the bridge anyway, that is allowed — but the
   design must then **state the mutability contract explicitly**: whether
   `MetricsParticleBridge` remains a value type, which members become `mutating`, and
   which call sites must become `var`, *including in the tests*. A design that adds
   stored state to a served value type without saying which it is will be rejected.
2. Store the loop task (e.g. `@State private var bridgeTask: Task<Void, Never>?`), guard
   `.onAppear` against double-start (`if bridgeTask == nil`), cancel it in
   `.onDisappear`, and keep the existing `while !Task.isCancelled` as the exit condition.

   **Apply this to the macOS window-group branch ONLY.** Leave the `#if os(visionOS)`
   branch exactly as it is — do not edit it, do not mirror the change into it, and do
   not mention it in the design's file structure. The test destination is
   `platform=macOS`, which never compiles a `#if os(visionOS)` branch, so an edit
   there would ship unverified under a green suite. The visionOS half is tracked
   separately and will be done when a device is available.

## What the metric window actually is

Read this before designing the cursor — the previous spec's wording implied a capacity
you could configure, and there is none.

- `HallucinationForcer._allMetrics` is the backing array, capped at
  `private static let maxMetrics = 200`. **That cap is private and static: a test
  cannot change it.**
- `metricsQueue` is a *computed property*, not a stored queue:
  `lock.withLock { Array(_allMetrics.suffix(5)) }`. The 5 is hardcoded in the accessor.
- So a consumer reading `metricsQueue` can see at most the newest 5 metrics, however
  many were produced. It cannot tell from the window alone how many it missed — which
  is exactly why the cursor needs a monotonic *count* from the forcer, not just the
  window contents.

## Reference files (read-only)

The test strategy protects six files. They are **reference, not scope** — read
them, do not edit them. They are listed because the first attempt at this spec
failed to compile entirely on types it could not see:

    error: inheritance from a final class 'AudioManager'
    error: type 'StubForcingStrategy' does not conform to protocol 'ForcingStrategy'
    error: cannot find type 'Particle' in scope

`AudioManager` is `final`, so an audio spy must be built by protocol or closure
injection rather than by subclassing it. `ForcingStrategy` is a protocol whose
requirements a stub has to satisfy exactly. `Particle` is nested inside
`HallucinationSimulator`. None of that is guessable, and guessing it is what
cost the first attempt.

## Acceptance criteria

A green build is NOT sufficient — the tests below are the gate.

- Build succeeds for the macOS target with no new warnings.
- Unit test: feed a forcer/bridge pair 3 metrics, call `bridge.update` TWICE with no new
  metrics — exactly 3 particles spawned and 3 strikes recorded total (use a spy/stub for
  audio). This test FAILS on current `main` (6 particles / 6 strikes) — say so
  explicitly in the report.
- Unit test: produce 12 metrics between two ticks — only the newest 5 are reachable,
  so the consumer takes those 5, never re-consumes them, and the cursor ends at 12 so
  the 7 it never saw are not replayed later. Dropped metrics are acceptable here;
  duplicates are not.
- Unit test: cursor resets when the forcer is replaced (simulate a new generation) so
  the new run's metrics are consumed from its start.
- MetricsParticleBridge currently has ZERO tests — the above establishes its suite.
- The `#if os(visionOS)` branch of `ElectricSheepApp.swift` is UNCHANGED. A diff that
  touches it fails this criterion, because `platform=macOS` cannot compile or test it
  and the change would reach the repository unverified.

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
      - ElectricSheep/AudioManager.swift
      - ElectricSheep/Audioscape.swift
      - ElectricSheep/ForcingStrategy.swift
      - ElectricSheep/HallucinationSimulator.swift
      - ElectricSheep/HallucinationEngine.swift
      - ElectricSheep/TokenMetrics.swift

## Constraints

- No new dependencies.
- Do not change particle physics, `simulator.update`, or audio synthesis.
- Do not switch the loop to a Combine timer or CADisplayLink — keep the async loop shape.
- Keep the forcer's bounded-queue memory caps (50 recent tokens / 200 metrics) intact.

## Risks

- `HallucinationForcer` is `@unchecked Sendable` with an internal lock; the new counter
  must be read/written under that same lock.
- SwiftUI may call `.onAppear` without a matching `.onDisappear` in some window
  lifecycles; the `bridgeTask == nil` guard is the primary defense, cancellation the
  secondary.
- The entropy→size clamp bug (DEV audit, low finding) lives in `process()` — do NOT fix
  it here; keep this spec's diff reviewable.
