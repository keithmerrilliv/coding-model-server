# Triple-buffer the shared CPU→GPU buffers in HalluRenderer (MTKView path)

Jira: DEV-590 (high). The immersive path is split out as DEV-757 — see Scope.

## Context

Repo: `electric-sheep`. The import line for these types is `import Metal` / `import simd`;
`HalluRenderer` is declared in `ElectricSheep/HalluRenderer.swift` and is a plain `class`
(no actor, no `@MainActor`, no `async`).

It owns three `.storageModeShared` buffers created in `createBuffers()`:

- `uniformBuffer` — atmosphere `Uniforms`, written in `uploadParticleData`. Length is
  `MemoryLayout<Uniforms>.stride` (64 B). **Single-buffered.**
- `particleBuffer` — `HalluParticleData × maxParticles` (`maxParticles = 3000`), written
  in `uploadParticleData`. **Single-buffered.**
- `particleUniformBuffer` — per-eye MVP, bound in `encodeRenderPasses`. **Already slotted
  per view** (see below).

Two render paths share the renderer: `MetalViewRepresentable` (MTKView `draw(in:)`,
macOS/iOS) and `ImmersiveRenderLoop` (visionOS, 90 Hz). `HalluRenderer.makeCommandBuffer()`
vends the command buffer to both.

### The stereo spec has ALREADY LANDED — this is a fact, not a precondition

`electric_sheep_stereo_eye_matrices.md` is merged into `electric-sheep` `main`. Verified
against the current file, not assumed. On `main` today:

```swift
static let kAlignedStride: Int = 256
static let maxViews: Int = 2

static func calculateSlotOffset(slot: Int) -> Int {
    return slot * kAlignedStride
}
```

and `createBuffers()` allocates `particleUniformBuffer` at
`kAlignedStride * maxViews` = 512 bytes.

**Consequences you must design for:**

- `particleUniformBuffer` is a 2-slot, 256-byte-aligned ring **already**. Do not
  reinvent it. Extend it.
- The per-view slot dimension and the per-frame slot dimension are **different axes**.
  This spec multiplies them: the uniform ring becomes `maxViews × maxFramesInFlight`
  = 2 × 3 = **6 slots**, i.e. 1536 bytes.
- `calculateSlotOffset(slot:)` currently takes one index. It must become a single tested
  helper that takes both axes. Keep the arithmetic in exactly one place.
- `uniformBuffer` and `particleBuffer` have **no** slot dimension today and gain only the
  frame axis: 3 slots each.

Also true on `main` and worth stating so no attempt "fixes" something that is already
correct: there is **no** `DispatchSemaphore`, **no** `addCompletedHandler`, and **no**
`waitUntilCompleted` anywhere in the file. Struct layouts are verified correct —
`Uniforms` 64 B, `ParticleUniforms` 208 B raw (256 B aligned stride),
`HalluParticleData` 36 B.

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

### The split must leave the immersive path bit-for-bit unchanged

This is a correctness requirement, not a nicety. Put the discipline in two
`HalluRenderer` methods:

- `beginFrame()` — `semaphore.wait()`, then advance the frame index.
- `finishFrame(commandBuffer:)` — register `addCompletedHandler { signal() }`, before
  commit.

The MTKView path calls both. The immersive path calls neither, so its frame index stays
at 0 and it keeps using frame slot 0 forever — exactly today's behaviour, still exposed
to the original tear, but **not made worse and not left half-wired**. If your design
makes `uploadParticleData` advance the frame index on its own, the immersive path would
silently rotate slots without ever waiting on the semaphore, which is a *new* defect.
Do not do that: advancing belongs in `beginFrame()`.

## Required change

1. Introduce `maxFramesInFlight = 3` and allocate a per-frame ring for all three buffers
   — 3 copies each, or one buffer of 3× length with per-frame offsets (implementer's
   choice). Offsets must be 256-byte aligned. For `particleUniformBuffer` this ring is
   `maxViews × maxFramesInFlight` = 6 slots, because the per-view axis already exists.
2. Add `DispatchSemaphore(value: maxFramesInFlight)` to `HalluRenderer`, owned by the
   renderer instance.
3. Add `beginFrame()` and `finishFrame(commandBuffer:)` as described in Scope. Every
   acquire must have a guaranteed matching signal on **every** path out of the frame,
   including early exits (no drawable, `nil` pipeline) — otherwise the renderer wedges
   permanently after three frames.
4. `uploadParticleData` and `encodeRenderPasses` write and bind the **current** frame
   slot only, via the single offset helper.
5. Wire the MTKView path (`draw(in:)`) to `beginFrame()` / `finishFrame(commandBuffer:)`.

## Acceptance criteria

A green build is NOT sufficient — the tests below are the gate.

- Build succeeds for the macOS target with no new warnings.
- Unit test: frame-index math — three consecutive `beginFrame()` calls use three distinct
  frame slots and the fourth reuses slot 0.
- Unit test: the offset helper is correct on **both** axes — for every
  `(frame, view)` in `0..<3 × 0..<2` the returned offset is 256-byte aligned, and all six
  offsets are distinct and non-overlapping within `particleUniformBuffer`'s length.
- Unit test (behavioral guard): after `uploadParticleData` for frame N+1, the slot used
  by frame N still contains frame N's first particle bytes (write two distinct particle
  sets, read back both slots via `contents()`). **This test FAILS on current `main`** —
  the single buffer is overwritten — and passes after. Say so explicitly in the report.
- Unit test: a renderer that has acquired three frames and signalled all three can
  acquire again without blocking (proves the signal path is actually wired, not merely
  present).
- No `waitUntilCompleted` introduced anywhere (that would serialize, not pipeline).
- `ImmersiveRenderLoop.swift` is unmodified.

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
- Do not edit `ImmersiveRenderLoop.swift` (see Scope).

## Risks

- **Deadlock.** An early-exit path that acquires the semaphore but never commits a
  command buffer that signals wedges the renderer after three frames. Every acquire needs
  a guaranteed matching signal, including error paths. This is the single most likely way
  to ship a green build that hangs at runtime.
- **The two slot axes get conflated.** Per-view and per-frame are independent; collapsing
  them into one index silently reintroduces the stereo bug that DEV-586 already fixed.
  Keep the arithmetic in one tested helper — that is what the two-axis offset test is
  for.
- Each render path constructs its own `HalluRenderer`, so the semaphore is per-instance.
  Verify this before sharing any state between paths.
