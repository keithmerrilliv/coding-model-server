# Triple-buffer the shared CPU→GPU buffers in HalluRenderer

Jira: DEV-590 (high).

## Context

Repo: `electric-sheep`. `HalluRenderer.swift` owns three `.storageModeShared` buffers
created in `createBuffers()` (lines 95-108): `uniformBuffer` (atmosphere uniforms,
written in `uploadParticleData`), `particleUniformBuffer` (per-eye MVP, written in
`encodeRenderPasses`), and `particleBuffer` (`HalluParticleData` × `maxParticles`,
written in `uploadParticleData`). Two render paths share the renderer:
`MetalViewRepresentable` (MTKView `draw(in:)`, macOS/iOS) and `ImmersiveRenderLoop`
(visionOS, 90 Hz).

NOTE: `electric_sheep_stereo_eye_matrices.md` slots `particleUniformBuffer` per view.
Run that spec FIRST; this spec multiplies the whole (slotted) set per in-flight frame.

## Problem

All three buffers are single-buffered. Each frame the CPU memcpy's new contents into the
same buffer the GPU may still be reading for the previous, uncompleted frame — there is
no `DispatchSemaphore`, no `waitUntilCompleted`, no completion handler anywhere in the
render path. Whenever the GPU is one frame behind (common at 90-120 Hz under load), the
vertex shader reads a torn mix of frame N and N+1 particle data: flickering, jumping
particles, mismatched atmosphere uniforms.

## Required change

Standard triple buffering:

1. Allocate 3 copies (ring) of each of the three buffers (or one buffer of 3× length
   with per-frame offsets — implementer's choice; offsets must be 256-byte aligned).
2. Add `DispatchSemaphore(value: 3)` to `HalluRenderer`. The frame path calls `wait()`
   before the first CPU write of a frame, advances the ring index, and every command
   buffer that reads the ring gets `addCompletedHandler { semaphore.signal() }` before
   commit.
3. Both render paths (MTKView and immersive) must go through the same acquire/signal
   discipline. The immersive path creates its command buffer in `runLoop()` — route the
   completed-handler registration through a renderer method (e.g.
   `renderer.finishFrame(commandBuffer:)`) so the discipline lives in one place.
4. `uploadParticleData` and `encodeRenderPasses` write/bind the current ring slot only.

## Acceptance criteria

A green build is NOT sufficient — the tests below are the gate.

- Build succeeds for the macOS target with no new warnings.
- Unit test: ring-index math — three consecutive frame acquisitions use three distinct
  slots and the fourth reuses slot 0.
- Unit test: buffer offsets for slot i are 256-byte aligned and non-overlapping across
  slots (if the single-buffer-with-offsets layout is chosen; otherwise assert 3 distinct
  MTLBuffer allocations exist per logical buffer).
- Unit test (behavioral guard): after `uploadParticleData` for frame N+1, the slot used
  by frame N still contains frame N's first particle bytes (write two distinct particle
  sets, read back both slots via `contents()`) — this test FAILS on current `main`
  (single buffer gets overwritten) and passes after; say so explicitly in the report.
- No `waitUntilCompleted` introduced anywhere (that would serialize, not pipeline).

## test_strategy

    framework: xcodebuild_test
    required: true
    repo: electric-sheep
    scheme: ElectricSheep
    destination: "platform=macOS"

## Constraints

- No new dependencies.
- Do not change shader code or struct layouts (`Uniforms` 64 B, `ParticleUniforms` 208 B,
  `HalluParticleData` 36 B are verified correct).
- Keep `maxParticles` and buffer storage modes as-is.

## Risks

- The MTKView path and immersive path have different pacing; the semaphore must be owned
  by the renderer instance (one per path — each path constructs its own HalluRenderer,
  verify this before sharing state).
- Deadlock risk if an early-exit path (empty drawables, nil pipeline) acquires the
  semaphore but never commits a command buffer that signals — every acquire must have a
  guaranteed matching signal, including error paths.
- If the stereo spec landed first, per-view slots × 3 frames means the uniform ring is
  2 × 3 slots; keep the indexing arithmetic in one tested helper.
