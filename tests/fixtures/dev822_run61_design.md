# Architecture: Metrics Panel Token Counting (DEV-815)

## Overview
Replace the unused `tokenMetrics` array in `HallucinationEngine` with a monotonic token count mirrored from the forcer's `appendedMetricCount`. This makes the Metrics panel appear during and after generation while preserving exact-once metric consumption by the particle bridge. Four files touch: three modified via anchored edits, one new test file added.

## Components

| Component | Responsibility |
|---|---|
| `HallucinationForcer.appendedMetricCount` | Read-only accessor exposing `totalAppended`, which increments per sampled token and resets only on `prompt(_:)`. Neither trimming nor `consumeNewMetrics()` lowers it. |
| `HallucinationEngine.generatedTokenCount` | Observable Int mirroring the current/most-recent forcer's appended count; kept across run completion so the finished panel stays visible. |
| `HallucinationEngine.showsMetrics` | Computed Bool (`generatedTokenCount > 0`) gating the Metrics GroupBox visibility. |
| `HallucinationEngine.recordProgress(from:)` | Internal method copying `forcer.appendedMetricCount` into `generatedTokenCount`; called once per stream element plus once post-loop. Never calls `consumeNewMetrics()`. |
| `ContentView` metrics guard | Changed from `!engine.tokenMetrics.isEmpty` to `engine.showsMetrics`; display text bound to `engine.generatedTokenCount`. |
| `MetricsPanelTests` | New XCTest suite covering criteria C1–C9 against the four new symbols. |

## File Structure

```
ElectricSheep/
├── HallucinationForcer.swift        — MODIFIED: add appendedMetricCount property after consumedMetricCount block
├── HallucinationEngine.swift        — MODIFIED: replace tokenMetrics, add generatedTokenCount/showsMetrics/recordProgress, edit installForcer and generate loop
└── ContentView.swift                — MODIFIED: update metrics panel condition and binding (one anchored replacement)
ElectricSheepTests/
└── MetricsPanelTests.swift          — NEW: test suite for C1–C9
```

**Read-only files (not modified):** MetricCursorTests.swift, BridgeLifecycleTests.swift, ForcingIntensityTests.swift, MetricsParticleBridge.swift, TokenMetrics.swift, ForcingStrategy.swift.

## Data Models

### Existing types (unchanged semantics)

- **`HallucinationForcer`**: `final class`, already declared in source. All state behind `NSLock`. Adding a read-only computed property requires no mutability changes.
- **`HallucinationEngine`**: `@Observable final class @MainActor`. Being a reference type with `@Observable`, all stored properties mutate freely; no `mutating` keyword needed anywhere.
- **`ModelState`**: `enum Sendable Equatable` inside Engine — unchanged.
- **`AvailableModel`**: `enum String CaseIterable Identifiable Sendable` inside Engine — unchanged.
- **`TokenMetrics`**: value struct, read-only context only. Deleted from engine storage but still used by bridge/tests.

### New declarations

| Symbol | Type | Location | Conformance | Purpose |
|---|---|---|---|---|
| `appendedMetricCount` | `var -> Int` (computed, get-only) | `HallucinationForcer` extension or body | none | Exposes monotonic append count |
| `generatedTokenCount` | `private(set) var: Int = 0` | `HallucinationEngine` | none | Observable token counter for UI binding |
| `showsMetrics` | `var -> Bool` (computed) | `HallucinationEngine` | none | Visibility gate (`generatedTokenCount > 0`) |
| `recordProgress(from:)` | `(HallucinationForcer) -> Void` | `HallucinationEngine` method | internal | Copies forcer count into engine state |
| `PanelStubStrategy` | private struct | MetricsPanelTests.swift | ForcingStrategy, Sendable | No-op strategy for tests |
| `metric(_ id:)` | function returning TokenMetrics | MetricsPanelTests.swift file-private | — | Factory helper |
| `record(_:into:)` | method on test class | MetricsPanelTests.swift @MainActor | — | Convenience loop calling existing `forcer.record(metric:)` |

### Invariants

1. **Monotonicity**: `totalAppended` only increases via `appendLocked` and resets to 0 in `prompt(_:)`. It is never decremented by trimming or consumption.
2. **No bridge stealing**: The engine reads `appendedMetricCount` but NEVER calls `consumeNewMetrics()`. Only the particle bridge consumes metrics.
3. **Reset boundary**: `generatedTokenCount` resets ONLY inside `installForcer()` at generation start. Neither `stopGeneration()` nor end-of-loop clears it (criterion 8).
4. **Post-run persistence**: After a run completes normally or via cancellation, `currentForcer` becomes nil but `generatedTokenCount` retains its final value so the panel stays visible.

## Implementation Notes

- **Isolation**: App target has `SWIFT_DEFAULT_ACTOR_ISOLATION = MainActor`. `HallucinationEngine` is explicitly annotated; new members inherit isolation implicitly. Test methods are `@MainActor func ... async throws`. Do NOT add `nonisolated` annotations.
- **Anchored edits only** — do not reformat surrounding code. Insert exactly where specified.
- **Edit order for HallucinationEngine.swift**: Apply Edit 1 (replace tokenMetrics), then Edit 2 (delete line in generate), then Edit 3 (stream loop tail), then Edit 4 (installForcer + recordProgress). Edits 3 and 4 can be applied independently if careful with context lines.
- **ContentView edit**: Replace four consecutive lines starting from `if !engine.tokenMetrics.isEmpty {` through `Text("Tokens: \(engine.tokenMetrics.count)")`. The closing braces of GroupBox/VStack remain untouched.
- **Test file structure**: Mirror MetricCursorTests pattern — imports at top, private helpers before class, all test methods `@MainActor func test_...() async throws`, use existing `forcer.record(metric:)` helper on Forcer.
- **No model loading required** in tests — criteria exercise the count path directly via `record`/`process`/`didSample` without invoking MLX generation infrastructure.

## Acceptance Criteria Checklist

- [ ] C1: A fresh forcer's `appendedMetricCount` is 0; after recording 3 metrics it is 3.
- [ ] C2: After recording 250 metrics, `appendedMetricCount` is 250 — not capped at 200.
- [ ] C3: Two calls to `consumeNewMetrics()` do not lower `appendedMetricCount` from 10 (and `consumedMetricCount` reaches 10).
- [ ] C4: Calling `prompt(_:)` resets `appendedMetricCount` to 0.
- [ ] C5: `process(logits:)` alone yields count 0; each subsequent `didSample(token:)` increments by exactly 1.
- [ ] C6: Fresh `HallucinationEngine` has `generatedTokenCount == 0` and `showsMetrics == false`.
- [ ] C7: After `installForcer()`, recording 250 metrics, then `recordProgress(from:)`: engine shows 250 with `showsMetrics == true`; calling `consumeNewMetrics()` on the forcer does NOT change the engine's count on a second `recordProgress`.
- [ ] C8: After installing forcer, recording 12 metrics, mirroring progress, then `stopGeneration()`: `currentForcer` is nil but `generatedTokenCount == 12` and `showsMetrics == true`.
- [ ] C9: Following C8 state, calling `installForcer()` again resets `generatedTokenCount` to 0 and `showsMetrics` to false.
- [ ] C10: Existing MetricCursorTests, BridgeLifecycleTests, and ForcingIntensityTests remain green (suite-level).

## Criterion Seams

- **C1** | setup: `let f = HallucinationForcer(strategy: PanelStubStrategy())` | act: `f.record(metric: metric(0)); f.record(metric: metric(1)); f.record(metric: metric(2))` | assert: `` XCTAssertEqual(f.appendedMetricCount, 3) ``
- **C2** | setup: `let f = HallucinationForcer(strategy: PanelStubStrategy()); record(250, into: f)` | act: `(none — read-only property)` | assert: `` XCTAssertEqual(f.appendedMetricCount, 250) ``
- **C3** | setup: `let f = HallucinationForcer(strategy: PanelStubStrategy()); record(10, into: f)` | act: `_ = f.consumeNewMetrics(); _ = f.consumeNewMetrics()` | assert: `` XCTAssertEqual(f.appendedMetricCount, 10); XCTAssertEqual(f.consumedMetricCount, 10) ``
- **C4** | setup: `let f = HallucinationForcer(strategy: PanelStubStrategy()); record(5, into: f)` | act: `f.prompt(MLXArray([Int32(1)]))` | assert: `` XCTAssertEqual(f.appendedMetricCount, 0) ``
- **C5a** | setup: `let f = HallucinationForcer(strategy: PanelStubStrategy())` | act: `_ = f.process(logits: MLXArray([Float](arrayLiteral: 1, 2, 3, 4)))` | assert: `` XCTAssertEqual(f.appendedMetricCount, 0) ``
- **C5b** | setup: (continues from C5a state) | act: `f.didSample(token: MLXArray(Int32(7)))` | assert: `` XCTAssertEqual(f.appendedMetricCount, 1) ``
- **C5c** | setup: (continues from C5b; two more process+didSample pairs) | act: `_ = f.process(logits: ...); f.didSample(token: MLXArray(Int32(8))); _ = f.process(logits: ...); f.didSample(token: MLXArray(Int32(9)))` | assert: `` XCTAssertEqual(f.appendedMetricCount, 3) ``
- **C6** | setup: `let engine = HallucinationEngine()` | act: `(none — initial state)` | assert: `` XCTAssertEqual(engine.generatedTokenCount, 0); XCTAssertFalse(engine.showsMetrics) ``
- **C7a** | setup: `let engine = HallucinationEngine(); let f = engine.installForcer(); record(250, into: f)` | act: `engine.recordProgress(from: f)` | assert: `` XCTAssertEqual(engine.generatedTokenCount, 250); XCTAssertTrue(engine.showsMetrics) ``
- **C7b** | setup: (same as C7a post-recordProgress) | act: `_ = f.consumeNewMetrics(); engine.recordProgress(from: f)` | assert: `` XCTAssertEqual(engine.generatedTokenCount, 250) ``
- **C8** | setup: `let engine = HallucinationEngine(); let f = engine.installForcer(); record(12, into: f); engine.recordProgress(from: f)` | act: `engine.stopGeneration()` | assert: `` XCTAssertNil(engine.currentForcer); XCTAssertEqual(engine.generatedTokenCount, 12); XCTAssertTrue(engine.showsMetrics) ``
- **C9** | setup: (continues from C8 post-stopGeneration) | act: `_ = engine.installForcer()` | assert: `` XCTAssertEqual(engine.generatedTokenCount, 0); XCTAssertFalse(engine.showsMetrics) ``
- **C10**: suite-level — no seam; verified by xcodebuild_test running the full ElectricSheepTests scheme with protected files unmodified.
