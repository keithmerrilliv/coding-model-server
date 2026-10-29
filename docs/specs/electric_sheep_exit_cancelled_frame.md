# Electric Sheep: exiting the immersive space leaves the cancelled frame alone instead of aborting

Jira: DEV-908 (labels `release-blocker`, `device-confirmed`). Related: DEV-589 and DEV-757,
which wrote the invariant this spec corrects; DEV-858 (the loop runs on the main thread),
which is NOT part of this spec. Repo `electric-sheep`, `main` = `2a557bf`. Every fact below
was read from that commit.

A reference run of exactly the change below was made on the Mac Studio (macOS
destination, in a throwaway worktree of `2a557bf`) before this spec was written:

| Code | Tests | Result |
| --- | --- | --- |
| `main` + the test edits only | `ImmersiveFrameDriverTests` | **2 of 7 fail**: `noDrawablesLeavesCancelledFrameAlone`, `slotsBalanceAcrossFrames` |
| `main` + test edits + the production edit | whole `ElectricSheepTests` | **TEST SUCCEEDED**: 117 passed, 0 failed, 1 skipped (the opt-in `LiveGenerationTests`) |
| same | `build-for-testing`, `generic/platform=visionOS` | TEST BUILD SUCCEEDED |

No device run was made for this spec: device signing needs the logged-in GUI session
(DEV-853), which an SSH session does not have. The device leg (below) does that. The
crash itself is device-confirmed in DEV-908. Do not re-derive any of this.

## Context

### The crash (from DEV-908)

On the Vision Pro (visionOS 27.0), pressing **Exit Immersive** aborts the app:

```
EXC_CRASH SIGABRT — "BUG IN CLIENT"
  CompositorNonUI            __BUG_IN_CLIENT__
  CompositorNonUI            cp_frame_end_submission
  ElectricSheep.debug.dylib  closure #10 in ImmersiveRenderLoop.runLoop()   <- endSubmission: { frame.endSubmission() }
  ElectricSheep.debug.dylib  static ImmersiveSubmission.run<A,B,C>(...)
```

### Why

When the layer pauses (the user exits the immersive space), `queryDrawables()` returns an
empty array. The visionOS SDK's `CompositorServices.framework/Headers/frame.h`, on
`cp_frame_query_drawables`, says (lines 99-110 of the header in the Mac Studio's Xcode):

> if the layer is in the paused or invalidated states the array will have a count of 0
> and frame is invalid. [...] If array count is 0, the frame has been cancelled as there
> are no drawables to draw to and the frame should be discarded and is invalid to access.

`ImmersiveSubmission.run` then calls `endSubmission()` on that cancelled frame. That call is
the abort.

### The code at `main`

`ElectricSheep/ImmersiveFrameDriver.swift` is platform-neutral (no `#if`), so the macOS
suite compiles and tests it. Lines 3-55, the parts that matter:

```swift
/// How one immersive frame's submission phase ended (DEV-589, DEV-757).
enum ImmersiveFrameOutcome: Equatable {
    /// Drawables were encoded, presented and committed.
    case rendered
    /// The compositor offered no drawables. No triple-buffer slot was taken.     // line 7
    case noDrawables
    /// A slot was taken but no command buffer could be made; the slot was given back.
    case noCommandBuffer
}
...
/// Every path out calls `endSubmission` exactly once, and every `beginSlot` is matched by   // lines 20-22
/// exactly one `finishSlot` or `abandonSlot` — an unmatched acquire wedges a 3-slot
/// semaphore after three frames, at 90 Hz, on a headset.
enum ImmersiveSubmission {
    static func run<Drawable, Buffer, Prepared>(
        ...
    ) async -> ImmersiveFrameOutcome {
        startSubmission()                       // line 36
        let drawables = queryDrawables()
        guard !drawables.isEmpty else {
            endSubmission()                     // line 39  <- DEV-908: the abort
            return .noDrawables
        }
        beginSlot()
        ...
```

The doc comment at lines 20-22 states the wrong invariant, and
`ElectricSheepTests/ImmersiveFrameDriverTests.swift` pins it, so the macOS suite passes
while the device aborts:

- line 62, in `noDrawablesNoSlot()`:
  `#expect(log.calls == ["startSubmission", "queryDrawables", "endSubmission"])`
- line 82, in `slotsBalanceAcrossFrames()`: `#expect(log.count("endSubmission") == 30)`.
  That test runs 30 frames, and one in three (`i % 3 == 1`) has no drawables.

`ElectricSheep/ImmersiveRenderLoop.swift` (visionOS-only) passes
`endSubmission: { frame.endSubmission() }` (line 121) into `ImmersiveSubmission.run`.
**It needs no change**: the fix is entirely in the platform-neutral driver.

### Decision: `startSubmission()` stays where it is

DEV-908 asks whether `startSubmission()` should move after the emptiness check. It should
not. `queryDrawables()` cannot be called before you know whether there are drawables, and
DEV-589 requires drawables to be queried *inside* the submission phase (after
`startSubmission()`). The existing test `drawablesInsideSubmissionPhase` pins that order.
Keep it unchanged.

The `.noCommandBuffer` path is also unchanged. There the frame has drawables, so it is
valid, and it must still be ended with `endSubmission()`.

## Required change

Two files. Make exactly these edits, and nothing else.

### 1. `ElectricSheep/ImmersiveFrameDriver.swift`

**Edit 1a, lines 7-8.** Replace

```swift
    /// The compositor offered no drawables. No triple-buffer slot was taken.
    case noDrawables
```

with

```swift
    /// The compositor offered no drawables: the frame was cancelled and is invalid to
    /// access, so it is not ended (DEV-908). No triple-buffer slot was taken.
    case noDrawables
```

**Edit 1b, lines 20-22.** Replace

```swift
/// Every path out calls `endSubmission` exactly once, and every `beginSlot` is matched by
/// exactly one `finishSlot` or `abandonSlot` — an unmatched acquire wedges a 3-slot
/// semaphore after three frames, at 90 Hz, on a headset.
```

with

```swift
/// Every path out that received drawables calls `endSubmission` exactly once. The path
/// that received none calls nothing after `queryDrawables`: CompositorServices' frame.h
/// says an empty array means the frame was cancelled and "is invalid to access", and
/// `endSubmission()` on it aborts the app with BUG IN CLIENT when the immersive space
/// is exited (DEV-908). Every `beginSlot` is matched by exactly one `finishSlot` or
/// `abandonSlot` — an unmatched acquire wedges a 3-slot semaphore after three frames,
/// at 90 Hz, on a headset.
```

**Edit 1c, lines 38-41.** Replace

```swift
        guard !drawables.isEmpty else {
            endSubmission()
            return .noDrawables
        }
```

with

```swift
        guard !drawables.isEmpty else {
            // Cancelled frame (paused/invalidated layer): discard it without touching
            // it again. Do not call endSubmission here (DEV-908).
            return .noDrawables
        }
```

The function's signature, its other paths, `ImmersiveFrameOutcome`'s cases and
`ImmersivePass` do not change.

### 2. `ElectricSheepTests/ImmersiveFrameDriverTests.swift`

**Edit 2a, lines 58-63.** Replace the whole `noDrawablesNoSlot()` test

```swift
    @Test @MainActor func noDrawablesNoSlot() async {
        let log = CallLog()
        let result = await runFrame(log, drawables: [], buffer: "cb")
        #expect(result == .noDrawables)
        #expect(log.calls == ["startSubmission", "queryDrawables", "endSubmission"])
    }
```

with

```swift
    /// DEV-908: an empty drawables array means the compositor cancelled the frame, and
    /// frame.h says a cancelled frame "is invalid to access". `endSubmission()` on it is
    /// a BUG IN CLIENT abort on the Vision Pro, at every Exit Immersive.
    @Test @MainActor func noDrawablesLeavesCancelledFrameAlone() async {
        let log = CallLog()
        let result = await runFrame(log, drawables: [], buffer: "cb")
        #expect(result == .noDrawables)
        #expect(log.calls == ["startSubmission", "queryDrawables"])
    }
```

**Edit 2b, line 82**, in `slotsBalanceAcrossFrames()`. Replace

```swift
        #expect(log.count("endSubmission") == 30)
```

with

```swift
        // 10 of the 30 frames were cancelled (no drawables) and must not be ended (DEV-908);
        // the other 20 each end exactly once.
        #expect(log.count("endSubmission") == 20)
```

The next line, `#expect(log.count("startSubmission") == 30)`, stays. Every other test in
the file stays byte-for-byte the same, as do `CallLog`, `runFrame` and `arrayTexture`.

## Change surface

Both paths were verified to exist at `main`. The plan's implement phase lists exactly these
two as outputs.

| Path | Change |
| --- | --- |
| `ElectricSheep/ImmersiveFrameDriver.swift` | modified (edits 1a, 1b, 1c) |
| `ElectricSheepTests/ImmersiveFrameDriverTests.swift` | modified (edits 2a, 2b) |

The protected files below are served read-only. Do not edit them.

## Acceptance criteria

The tests are the gate. With only the test edits applied, criteria 1 and 2 **fail on
`main`** (shown in the reference run). Say so in the report.

1. **A cancelled frame is not touched again.** `runFrame(log, drawables: [], buffer: "cb")`
   returns `.noDrawables`, and `log.calls` equals exactly
   `["startSubmission", "queryDrawables"]`. There is no `endSubmission` and no `beginSlot`.
2. **Ends balance across a mix of frames.** In `slotsBalanceAcrossFrames`, after 30 frames
   cycling rendered / no drawables / no command buffer: `startSubmission` is called 30
   times, `endSubmission` 20 times, and `beginSlot == finishSlot + abandonSlot` after every
   frame.
3. **The other paths are unchanged.** `renderedPathOrder`, `drawablesInsideSubmissionPhase`
   and `failedCommandBufferAbandonsSlot` pass unmodified. The `.noCommandBuffer` path still
   ends with `"abandonSlot", "endSubmission"`.
4. **Whole suite green on macOS.** `xcodebuild test` over `ElectricSheepTests` passes. The
   opt-in `LiveGenerationTests` skips, as on `main`.
5. **Green on the device.** The reviewer's device leg passes on the attached Apple Vision
   Pro.

Behaviour on the headset (Exit Immersive no longer aborts, and re-entering renders again)
is checked by hand after the run. No gate here can open an immersive space.

## test_strategy

```yaml
framework: xcodebuild_test
required: true
repo: electric-sheep
base_ref: main
scheme: ElectricSheep
destination: "platform=macOS"
device_destination: "platform=visionOS"
filter: ElectricSheepTests
default_actor_isolation: MainActor
protected_paths:
  - ElectricSheep/ImmersiveRenderLoop.swift
  - ElectricSheep/HalluRenderer.swift
  - ElectricSheepTests/ImmersiveRateMapDrawTests.swift
  - ElectricSheep.xcodeproj/project.pbxproj
```

## Constraints

- Do not touch `ElectricSheep/ImmersiveRenderLoop.swift`. It is visionOS-only, so the macOS
  gate cannot compile it, and the fix does not need it. Its `endSubmission:` closure stays
  as it is. The driver simply stops calling it on the cancelled path.
- Do not move `startSubmission()` or `queryDrawables()`. DEV-589 requires the drawables to
  be queried after `startSubmission()`.
- Do not change `ImmersivePass.descriptor` or the two descriptor tests
  (`passDescriptorBuiltCorrectly`, `rateMapBoundWhenSupplied`). The rate-map layer bug
  (DEV-909) is a separate ticket and may edit them.
- Do not mark anything `nonisolated`, and do not move the loop off the main actor. That is
  DEV-858. The target uses `SWIFT_DEFAULT_ACTOR_ISOLATION = MainActor`, and the tests stay
  `@Test @MainActor func … async`.
- Swift Testing (`import Testing`), not XCTest. No new files, no new dependencies.

## Risks

- **Removing the wrong `endSubmission()`.** There are three calls in `run`: the cancelled
  path (line 39, remove it), `.noCommandBuffer` (line 46, keep it) and `.rendered` (line 52,
  keep it). Criterion 3 catches a wrong removal.
- **Weakening the test instead of the code.** Deleting the `endSubmission` expectation
  instead of asserting the exact two-entry log would let the bug come back. Criterion 1
  requires the exact array.
- **A frame that is started but never ended.** After this change a cancelled frame gets
  `startSubmission()` and no `endSubmission()`. That is what frame.h asks for ("discarded"),
  and it is what DEV-908 proposes. (That Apple's sample code does the same is from memory,
  not checked against a template, so do not rely on it.) Whether
  the compositor's frame pacing recovers on re-entering the space is a device check, not
  a test.
- **Overlap with DEV-909.** That ticket may edit `ImmersivePass` in the same source file and
  the descriptor tests in the same test file. The functions do not overlap, but a parallel
  run may cause a textual merge conflict in these two files.
