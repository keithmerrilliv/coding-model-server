# Per-eye uniform buffers and per-eye view transforms for stereo rendering

Jira: DEV-586 (critical), DEV-588 (high). One change set: both defects corrupt the same
per-eye matrix path and are verified together.

## Context

Repo: `electric-sheep`. Swift + Metal. visionOS immersive rendering goes through
CompositorServices: `ImmersiveRenderLoop.swift` (inside `#if os(visionOS)`) drives
`HalluRenderer.encodeRenderPasses` once per view (eye) within a single command buffer.

## Problem

1. **Shared uniform buffer across eyes** — `HalluRenderer.swift`:
   `particleUniformBuffer` is allocated with length `MemoryLayout<ParticleUniforms>.stride`
   (one slot, `createBuffers`, lines 100-103). `encodeRenderPasses` (lines 200-209) does:
   ```swift
   var particleUniforms = ParticleUniforms(projectionMatrix: projection, viewMatrix: view,
                                           modelMatrix: model, time: time)
   if let buf = particleUniformBuffer {
       memcpy(buf.contents(), &particleUniforms, MemoryLayout<ParticleUniforms>.stride)
   }
   ```
   The GPU reads buffer contents at execution time — after BOTH per-eye `memcpy`s have
   run — so both eyes render with the second (right) eye's matrices.

2. **No per-eye view transform** — `ImmersiveRenderLoop.swift:107-109`:
   ```swift
   let viewMatrix: simd_float4x4 = deviceAnchor
       .map { $0.originFromAnchorTransform.inverse }
       ?? simd_float4x4(1)
   ```
   computed once per frame and reused for every view; `drawable.views[viewIndex].transform`
   is never used. Both eyes share one cyclopean viewpoint — zero interpupillary offset.

## Required change

1. Size `particleUniformBuffer` for at least 2 views (256-byte-align each slot per Metal
   argument buffer offset rules). `encodeRenderPasses` gains a view-slot parameter,
   writes its `ParticleUniforms` into that slot's offset, and binds the buffer with the
   matching `setVertexBuffer(..., offset:)`. The macOS/MTKView path passes slot 0.
2. In `ImmersiveRenderLoop`, compute the view matrix per eye:
   `(deviceAnchor.originFromAnchorTransform * drawable.views[viewIndex].transform).inverse`,
   falling back to identity when the anchor is nil.
3. Extract the per-eye math into a pure, platform-independent helper (e.g.
   `func perEyeViewMatrix(originFromAnchor: simd_float4x4, eyeFromOrigin eyeTransform: simd_float4x4) -> simd_float4x4`)
   in a file compiled on ALL platforms so it is unit-testable on macOS. Verify the exact
   CompositorServices semantics of `LayerRenderer.Drawable.View.transform` against the
   SDK headers/swiftinterface before writing the math — do not assume the multiplication
   order from memory.

## Acceptance criteria

A green build is NOT sufficient — the tests below are the gate.

- Build succeeds for the macOS target with no new warnings.
- Unit test: `perEyeViewMatrix` with identity anchor and two eye transforms offset by
  ±32 mm on x returns matrices whose camera positions differ by 64 mm on x (extract
  translation and compare against simd reference math).
- Unit test: uniform-slot offset math — two views write to non-overlapping,
  256-byte-aligned offsets within the buffer's length.
- Unit test: with identical eye transforms the function reduces to the old
  `originFromAnchorTransform.inverse` (regression guard for the macOS path).
- These tests must FAIL against current `main` where applicable (helper won't exist —
  state clearly which tests are new-behavior vs regression guards).

## test_strategy

    framework: xcodebuild_test
    required: true
    repo: electric-sheep
    scheme: ElectricSheep
    destination: "platform=macOS"

## Constraints

- No new dependencies.
- Do not restructure the render loop's frame phases in this spec (that is
  `electric_sheep_frame_phase_and_foveation.md`); touch only matrix computation and
  uniform-buffer slotting.
- The macOS MTKView render path must keep working identically (slot 0).

## Risks

- Metal buffer-offset alignment: `setVertexBuffer` offsets must be 256-byte aligned on
  Apple GPUs for constant address space buffers — use `stride` rounded up to 256, not raw
  `MemoryLayout<ParticleUniforms>.stride` (208).
- `views[viewIndex].transform` semantics (eye-from-device vs device-from-eye) must be
  read from the SDK; a wrong inverse produces subtly swapped/exaggerated stereo that a
  simulator run will not catch.
- This spec and `electric_sheep_triple_buffering.md` both touch `particleUniformBuffer`;
  run this spec FIRST, then triple buffering rebases on the slotted layout.
