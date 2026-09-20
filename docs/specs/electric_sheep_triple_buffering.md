# Triple-buffer the shared CPU→GPU buffers in HalluRenderer (MTKView path)

Jira: DEV-590 (high). The immersive path is split out as DEV-757 — see Scope.
Run 46 of this spec (DEV-758) failed for two spec defects, both fixed here: the
wiring file was never editable, and no acceptance criterion exercised `draw(in:)`.

## Context

Repo: `electric-sheep`. The import line for these types is `import Metal` / `import simd`;
`HalluRenderer` is declared in `ElectricSheep/HalluRenderer.swift`. Every fact below was
read from `main` (`ab3e7ec`) before this spec was submitted — it is a description of the
code, not a guess.

`HalluRenderer` owns three `.storageModeShared` buffers created in `createBuffers()`:

- `uniformBuffer` — atmosphere `Uniforms`, written in `uploadParticleData`. Length is
  `MemoryLayout<Uniforms>.stride` (64 B). **Single-buffered.**
- `particleBuffer` — `HalluParticleData × maxParticles` (`maxParticles = 3000`), written
  in `uploadParticleData`. **Single-buffered.**
- `particleUniformBuffer` — per-eye MVP, written and bound in `encodeRenderPasses`.
  **Already slotted per view** (see below).

Two render paths share the renderer: `MetalViewRepresentable` (MTKView `draw(in:)`,
macOS/iOS, in `ElectricSheep/MetalViewRepresentable.swift`) and `ImmersiveRenderLoop`
(visionOS, 90 Hz). `HalluRenderer.makeCommandBuffer()` vends the command buffer to both.

### The stereo spec has ALREADY LANDED — this is a fact, not a precondition

On `main` today:

```swift
static let kAlignedStride: Int = 256
static let maxViews: Int = 2

static func calculateSlotOffset(slot: Int) -> Int {
    return slot * kAlignedStride
}
```

and `createBuffers()` allocates `particleUniformBuffer` at
`kAlignedStride * maxViews` = 512 bytes. `calculateSlotOffset(slot:)` is called from
exactly one place, `encodeRenderPasses` in the same file; nothing outside
`HalluRenderer.swift` references it.

**Consequences you must design for:**

- `particleUniformBuffer` is a 2-slot, 256-byte-aligned ring **already**. Do not
  reinvent it. Extend it.
- The per-view slot dimension and the per-frame slot dimension are **different axes**.
  This spec multiplies them: the uniform ring becomes `maxViews × maxFramesInFlight`
  = 2 × 3 = **6 slots**, i.e. 1536 bytes.
- The offset arithmetic lives in exactly one tested helper that takes both axes:
  `static func calculateSlotOffset(frame: Int, view: Int) -> Int`, returning
  `(frame * maxViews + view) * kAlignedStride`. The old one-argument form may be deleted
  or kept as a wrapper for frame 0; nothing else calls it.
- `uniformBuffer` and `particleBuffer` have **no** slot dimension today and gain only the
  frame axis: 3 slots each, at offsets `frame * <single-slot length>`.

Also true on `main` and worth stating so no attempt "fixes" something that is already
correct: there is **no** `DispatchSemaphore`, **no** `addCompletedHandler`, and **no**
`waitUntilCompleted` anywhere in the file. Struct layouts are verified correct —
`Uniforms` 64 B, `ParticleUniforms` 208 B raw (256 B aligned stride),
`HalluParticleData` 36 B (nine `Float`s: `px py pz size cr cg cb ca life`, in that order).

### The actor-isolation contract — read this before writing a line

This is what defeated run 44 (DEV-753) and it applies here too, because the project
builds with these settings (`ElectricSheep.xcodeproj/project.pbxproj`):

```
SWIFT_VERSION = 5.0
SWIFT_APPROACHABLE_CONCURRENCY = YES
SWIFT_DEFAULT_ACTOR_ISOLATION = MainActor      // app target ONLY
```

Consequences, stated as rules:

1. **Every unannotated declaration in `ElectricSheep/` is implicitly `@MainActor`.**
   `HalluRenderer`, `MetalViewRepresentable`, and its `Coordinator` are all
   MainActor-isolated today even though no annotation appears. Do not add `@MainActor`
   (redundant) and do not add `nonisolated` to anything that touches mutable renderer
   state. `MTKViewDelegate` is itself a `@MainActor` protocol, so `draw(in:)` needs no
   annotation either.
2. **The test target does NOT have default MainActor isolation.** A plain
   `func testX()` on an `XCTestCase` is a *nonisolated synchronous* context, and calling
   `HalluRenderer(device:)`, `beginFrame()`, `HallucinationSimulator()` or
   `Coordinator(renderer:simulator:)` from it is a compile error:
   `call to main actor-isolated instance method in a synchronous nonisolated context`.
   **Every test method in this spec is declared `@MainActor func test_…() async throws`**
   — exactly the pattern of the existing `ElectricSheepTests/BridgeLifecycleTests.swift`.
3. **No background-thread calls into the renderer.** `DispatchQueue.global().async {
   renderer.beginFrame() }`, `Thread.detachNewThread { … }` and `Task.detached { … }`
   are nonisolated closures; any renderer call inside them will not compile. Do not
   write them. "Does not block" is proven with a *timed* wait on the semaphore
   (rule 5), never with a thread.
4. **`let` stored properties of `Sendable` type are nonisolated.** `DispatchSemaphore`
   is `Sendable`, so `let frameSemaphore = DispatchSemaphore(value: 3)` can be read
   and signalled from the `addCompletedHandler` closure, which runs on a Metal
   completion thread. Capture the semaphore, not `self`:
   `let sem = frameSemaphore; commandBuffer.addCompletedHandler { _ in sem.signal() }`.
5. **Timed waits are how tests observe the semaphore.**
   `frameSemaphore.wait(timeout: .now() + 2)` returns `.success` or `.timedOut` and never
   hangs the suite. `frameSemaphore` is therefore `internal` (not `private`) and the
   test file uses `@testable import ElectricSheep`.
6. `HallucinationSimulator` is `@MainActor @Observable final class`. Its API, verbatim
   from `main`: `init()`; `var isDemoMode: Bool` (set it `false` in tests or the
   simulator spawns random particles); `func spawnParticle(_ particle: HalluParticle)`
   (appends, capped at `maxParticles`); `func clear()`; `private(set) var particles`.
   There is **no** `HallucinationSimulator.Particle` type and **no** `init(particles:)`
   — run 46 invented both. The particle type is the top-level
   `struct HalluParticle` with memberwise
   `init(position:velocity:color:size:age:lifetime:type:)`, `type: HallucinationType`
   (e.g. `.factualErrors`).

## Problem

`uniformBuffer` and `particleBuffer` are single-buffered, and `particleUniformBuffer`
rotates only by view, never by frame. Each frame the CPU memcpy's new contents into the
same memory the GPU may still be reading for the previous, uncompleted frame — there is
no frame-completion synchronisation of any kind in the render path. Whenever the GPU is
one frame behind (common at 90–120 Hz under load), the vertex shader reads a torn mix of
frame N and N+1 particle data: flickering, jumping particles, mismatched atmosphere
uniforms.

## Scope

**IN scope: the MTKView path only** (`MetalViewRepresentable`, macOS/iOS).

**OUT of scope: the immersive path** (`ImmersiveRenderLoop`, visionOS) — tracked as
DEV-757.

This is not arbitrary. `ImmersiveRenderLoop.swift` is wrapped entirely in
`#if os(visionOS)`, and this spec's `destination` is `platform=macOS`, which **never
compiles that file**. An edit there could not be verified by this run's own gate: a
syntax error would be invisible, the tests would pass, and half the change would ship
unchecked.

**Do not edit `ImmersiveRenderLoop.swift`.** A change to it is not evidence of progress
here and cannot be validated.

### Four signatures the immersive path calls — they must not change

Because `ImmersiveRenderLoop.swift` is not compiled by this run, a signature change to
anything it calls would ship broken and invisible. It calls exactly these four, which
must keep their names, labels, parameter types, defaults and return types:

```swift
func uploadParticleData(simulator: HallucinationSimulator) -> Int
func updateTiming()
func makeCommandBuffer() -> MTLCommandBuffer?
func encodeRenderPasses(encoder: MTLRenderCommandEncoder, particleCount: Int,
                        projection: simd_float4x4, view: simd_float4x4,
                        model: simd_float4x4, viewSlot: Int = 0)
```

### The split must leave the immersive path bit-for-bit unchanged

This is a correctness requirement, not a nicety. Put the discipline in three
`HalluRenderer` methods:

- `beginFrame()` — `frameSemaphore.wait()`, then advance the frame index. **The only
  place the frame index advances.**
- `finishFrame(commandBuffer:)` — register `addCompletedHandler { signal }`; must be
  called before `commit()`.
- `abandonFrame()` — signal immediately, for a frame that was begun but will never be
  committed (early exit). Does not touch the frame index.

The MTKView path calls these. The immersive path calls none of them, so its frame index
stays at 0 and it keeps using frame slot 0 forever — and because
`calculateSlotOffset(frame: 0, view: v)` equals today's `calculateSlotOffset(slot: v)`,
it binds exactly the bytes it binds today: still exposed to the original tear, but
**not made worse and not left half-wired**. If your design makes `uploadParticleData`
advance the frame index on its own, the immersive path would silently rotate slots
without ever waiting on the semaphore, which is a *new* defect. Do not do that.

## Required change

1. Introduce `static let maxFramesInFlight = 3` and allocate a per-frame ring for all
   three buffers: one buffer of 3× length per buffer, with per-frame offsets. Offsets must
   be 256-byte aligned. For `particleUniformBuffer` the ring is
   `maxViews × maxFramesInFlight` = 6 slots, because the per-view axis already exists.
2. Add `let frameSemaphore = DispatchSemaphore(value: HalluRenderer.maxFramesInFlight)`
   to `HalluRenderer`, owned by the instance, `internal` visibility.
3. Add `private(set) var frameIndex: Int = 0` (the slot the *current* frame writes and
   binds) and `private(set) var framesBegun: Int = 0` (monotonic count of `beginFrame()`
   calls). `beginFrame()` waits, then sets `frameIndex = (frameIndex + 1) %
   maxFramesInFlight` and increments `framesBegun`.
4. Add `beginFrame()`, `finishFrame(commandBuffer:)` and `abandonFrame()` as described in
   Scope. Every acquire must have exactly one matching signal on **every** path out of
   the frame, including early exits — otherwise the renderer wedges permanently after
   three frames.
5. `uploadParticleData` and `encodeRenderPasses` write and bind the **current** frame
   slot only, via the offset helper (frame axis for `uniformBuffer` / `particleBuffer`;
   both axes for `particleUniformBuffer`). `setFragmentBuffer` / `setVertexBuffer` bind
   with the slot's offset, never offset 0 unconditionally.
6. The three `MTLBuffer?` properties become `private(set) var` (internal read) so the
   tests can inspect `length` and `contents()`.
7. Wire `draw(in:)` in `ElectricSheep/MetalViewRepresentable.swift`. The method exists
   twice, under `#if os(macOS)` and `#elseif os(iOS) || os(visionOS)`; the two bodies are
   identical on `main` and must stay identical. Required order:

   ```swift
   guard let renderer = renderer else { return }
   renderer.beginFrame()
   guard let drawable = view.currentDrawable,
         let descriptor = view.currentRenderPassDescriptor,
         let commandBuffer = renderer.makeCommandBuffer(),
         let encoder = commandBuffer.makeRenderCommandEncoder(descriptor: descriptor) else {
       renderer.abandonFrame()
       return
   }
   renderer.render(encoder: encoder, simulator: simulator)
   encoder.endEncoding()
   commandBuffer.present(drawable)
   renderer.finishFrame(commandBuffer: commandBuffer)
   commandBuffer.commit()
   ```

   `beginFrame()` goes *before* the drawable guard on purpose: it makes the wiring
   observable from a test that has no window (criterion 6), and `abandonFrame()` keeps
   the early exit balanced.
8. Write the test file named in the change surface. **The implementer writes the tests;
   they are part of the implement phase, not something the reviewer adds later.**

## Change surface

Repo-relative paths, so context assembly can resolve them. The two existing paths were
verified to exist at `main` before this spec was submitted; the test file was verified
NOT to exist. The plan's implement phase must list all three as outputs, and the first
two as inputs.

| Path | Change |
| --- | --- |
| `ElectricSheep/HalluRenderer.swift` | modified |
| `ElectricSheep/MetalViewRepresentable.swift` | modified |
| `ElectricSheepTests/HalluRendererTripleBufferTests.swift` | new |

New tests go in **`ElectricSheepTests/`** — that exact directory, at the repository
root, alongside the existing `ElectricSheepTests/BridgeLifecycleTests.swift`. It is
also the value of `test_strategy.filter`. The Xcode project uses synchronized root
groups for exactly `ElectricSheep/` and `ElectricSheepTests/`, so *a source file
written anywhere else is never compiled and never joins a target*.

The test file header is:

```swift
import XCTest
import Metal
import MetalKit
@testable import ElectricSheep

final class HalluRendererTripleBufferTests: XCTestCase {
    // every test: @MainActor func test_…() async throws
    // first line of every test that needs a GPU:
    //   guard let device = MTLCreateSystemDefaultDevice() else { throw XCTSkip("no Metal device") }
}
```

## Acceptance criteria

A green build is NOT sufficient — the tests below are the gate. Each numbered criterion
2–6 is one XCTest method in `ElectricSheepTests/HalluRendererTripleBufferTests.swift`,
declared `@MainActor … async throws`.

1. Build succeeds for the macOS target with no new warnings.
2. **Slot rotation.** Call `beginFrame()` then `abandonFrame()` four times in a row
   (abandoning keeps the semaphore from running dry; it does not rewind the index),
   recording `frameIndex` after each `beginFrame()`. The first three recorded slots are
   pairwise distinct; the fourth equals the first. `framesBegun` is 4 at the end.
3. **Two-axis offset helper.** For every `(frame, view)` in `0..<3 × 0..<2`,
   `HalluRenderer.calculateSlotOffset(frame:view:)` is a multiple of 256; the six offsets
   are distinct; and `max + kAlignedStride <= particleUniformBuffer!.length` on a live
   renderer (i.e. the buffer really was grown to 1536 bytes).
4. **Behavioural guard — this is the test that cannot pass on single buffering.** Build
   `simA = HallucinationSimulator()`, `isDemoMode = false`, spawn one `HalluParticle` at
   position `(1, 2, 3)`; `simB` likewise at `(7, 8, 9)`. `beginFrame()`, `_ =
   uploadParticleData(simulator: simA)`, note `frameIndex` as N and copy the first 36
   bytes of slot N of `particleBuffer` (offset `N * MemoryLayout<HalluParticleData>.stride
   * maxParticles`) into a `Data`. `beginFrame()`, upload `simB`. Assert slot N's first
   36 bytes are byte-for-byte unchanged and decode to `px, py, pz == 1, 2, 3`, and that
   slot `frameIndex`'s first 36 bytes decode to `7, 8, 9`. Call `abandonFrame()` twice
   at the end. On `main` this file does not compile (no `beginFrame`), and its assertion
   would fail against a single buffer. Say so explicitly in the report.
5. **The signal path is wired, not merely present.** `beginFrame()` three times; for
   each, `makeCommandBuffer()`, `finishFrame(commandBuffer:)`, `commit()`, then
   `waitUntilCompleted()` **in the test** (permitted in the test file only — see
   criterion 7). Then assert `frameSemaphore.wait(timeout: .now() + 2) == .success`, and
   `frameSemaphore.signal()` to put the permit back. A renderer whose handler never
   signals times out here instead of hanging.
6. **`draw(in:)` is wired — the criterion run 46 lacked.** Build a renderer, a
   `HallucinationSimulator()` with `isDemoMode = false`, a
   `MetalViewRepresentable.Coordinator(renderer:simulator:)`, and
   `MTKView(frame: .zero, device: device)` that is never placed in a window. Call
   `coordinator.draw(in: view)` three times. Assert `renderer.framesBegun == 3` and then
   `renderer.frameSemaphore.wait(timeout: .now() + 2) == .success` (signal it back
   afterwards). This passes only if `draw(in:)` calls `beginFrame()` and releases the
   permit on every path — early exit via `abandonFrame()` or commit via
   `finishFrame` — whichever path a headless view takes. It is the test that would have
   caught run 46's ring buffer wired to nothing.
7. No `waitUntilCompleted` anywhere under `ElectricSheep/` (that would serialize, not
   pipeline). The test file may use it.
8. `ImmersiveRenderLoop.swift` is unmodified, and the four signatures listed under Scope
   are unchanged.

## test_strategy

    framework: xcodebuild_test
    required: true
    repo: electric-sheep
    base_ref: main
    scheme: ElectricSheep
    destination: "platform=macOS"
    filter: ElectricSheepTests
    skip_filter: ElectricSheepTests/DtypeContainmentTests

## Constraints

- No new dependencies.
- Do not change shader code or struct layouts (`Uniforms` 64 B, `ParticleUniforms` 208 B,
  `HalluParticleData` 36 B are verified correct).
- Keep `maxParticles` and buffer storage modes as-is.
- Do not edit `ImmersiveRenderLoop.swift` (see Scope), and do not touch any file outside
  the change-surface table.
- Do not use `waitUntilCompleted` in production code.

## Risks

- **Deadlock.** An early-exit path that acquires the semaphore but never signals wedges
  the renderer after three frames. Criteria 5 and 6 exist because every other test passes
  against a semaphore that is created and never signalled.
- **The two slot axes get conflated.** Per-view and per-frame are independent; collapsing
  them into one index silently reintroduces the stereo bug that DEV-586 already fixed.
  Keep the arithmetic in one tested helper — that is what criterion 3 is for.
- **Actor isolation.** A test method without `@MainActor`, or any renderer call from a
  dispatch-queue closure, fails the build. This is the single most likely compile error
  in this spec; see the contract in Context.
- **Headless MTKView.** With a zero frame and no window, `currentDrawable` is normally
  `nil`, so criterion 6 usually exercises the `abandonFrame()` path; if the view does
  produce a drawable, the `finishFrame` path runs and the completion handler signals
  within the 2 s window. The criterion is written to hold either way.
- Each render path constructs its own `HalluRenderer`, so the semaphore is per-instance.
  Do not share any state between paths.
