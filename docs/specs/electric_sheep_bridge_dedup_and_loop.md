# Consume token metrics exactly once and give the bridge loop a real lifecycle

Jira: DEV-592 (high), DEV-593 (high). Same loop, one change set.

## Context

Repo: `electric-sheep`. `ElectricSheepApp.swift` spawns `runBridgeLoop()` (lines 75-100)
from `.onAppear` (lines 36-40 visionOS, 51-54 macOS) as `Task { await runBridgeLoop() }`.
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

1. Give the bridge a monotonic consumption cursor. Preferred shape: `HallucinationForcer`
   already appends metrics to a bounded queue — expose a monotonically increasing total
   count (`totalMetricsProduced`) alongside the bounded queue, and have the bridge keep
   `lastConsumedCount`, consuming only metrics with index > lastConsumedCount (clamped to
   what's still in the bounded queue; if more than queue-capacity tokens arrived since
   the last tick, consume what remains and advance the cursor — dropped metrics are
   acceptable, duplicates are not). Reset the cursor when a new generation starts
   (`forcer` identity changes or `prompt()` resets).
2. Store the loop task (e.g. `@State private var bridgeTask: Task<Void, Never>?`), guard
   `.onAppear` against double-start (`if bridgeTask == nil`), cancel it in
   `.onDisappear`, and keep the existing `while !Task.isCancelled` as the exit condition.
   Apply to BOTH platform branches (visionOS and macOS window groups).

## Acceptance criteria

A green build is NOT sufficient — the tests below are the gate.

- Build succeeds for the macOS target with no new warnings.
- Unit test: feed a forcer/bridge pair 3 metrics, call `bridge.update` TWICE with no new
  metrics — exactly 3 particles spawned and 3 strikes recorded total (use a spy/stub for
  audio). This test FAILS on current `main` (6 particles / 6 strikes) — say so
  explicitly in the report.
- Unit test: produce 12 metrics between two ticks with a queue capacity of 5 — the
  bridge consumes at most the 5 available, never re-consumes, and the cursor lands at 12.
- Unit test: cursor resets when the forcer is replaced (simulate a new generation) so
  the new run's metrics are consumed from its start.
- MetricsParticleBridge currently has ZERO tests — the above establishes its suite.

## test_strategy

    framework: xcodebuild_test
    required: true
    repo: electric-sheep
    scheme: ElectricSheep
    destination: "platform=macOS"

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
