# Query drawables inside the submission phase and bind foveation rate maps

Jira: DEV-589 (high), DEV-591 (high). Both defects live in the same ~60 lines of
`ImmersiveRenderLoop.swift` and are fixed together.

## Context

Repo: `electric-sheep`. visionOS CompositorServices render loop in
`ImmersiveRenderLoop.swift` (all inside `#if os(visionOS)`). The correct frame lifecycle
is: `startUpdate → endUpdate → wait for optimal input time → startSubmission →
queryDrawables → encode → drawable.encodePresent(commandBuffer:) → commandBuffer.commit()
→ frame.endSubmission`.

## Problem

1. **Drawables queried outside the submission phase** — current order in `runLoop()`:
   line 79 `let drawables = frame.queryDrawables()`, then anchor query (88-93), then an
   async particle upload with two `await MainActor.run` hops (96-97), and only then
   line 100 `frame.startSubmission()`. Drawables must be acquired inside the submission
   phase; acquiring them early can return empty/invalid drawables and stalls frame pacing.

2. **Foveation configured but never bound** — the layer config sets
   `configuration.isFoveationEnabled = capabilities.supportsFoveation` (line 17), so
   drawable textures are allocated at warped/variable resolution, but the
   `MTLRenderPassDescriptor` built at lines 119-135 never sets `rasterizationRateMap`.
   Content is rasterized linearly into the warped texture and un-warped by the
   compositor at present — the scene appears distorted on device (center squeezed,
   periphery stretched).

## Required change

1. Reorder `runLoop()` so `queryDrawables()` is called after `startSubmission()`. The
   device-anchor query needs `timing.trackableAnchorTime` (already computed from
   `predictTiming()`), which does not depend on the drawable — keep anchor querying and
   the particle upload before `startSubmission()`; move only the drawable acquisition
   and the per-drawable `deviceAnchor` assignment inside the submission phase.
2. Bind the drawable's rasterization rate map on each render pass descriptor:
   `renderPassDesc.rasterizationRateMap = drawable.rasterizationRateMaps[...]` — consult
   the CompositorServices swiftinterface for how `rasterizationRateMaps` is indexed for
   `.layered` vs `.dedicated` layouts (per-view index vs single map) rather than assuming.
   Guard for the empty case (foveation unsupported → array may be empty).
3. Preserve the already-correct tail: `encodePresent → commit → endSubmission`.

## Acceptance criteria

A green build is NOT sufficient.

- Build succeeds for the macOS target with no new warnings (visionOS-only file must not
  break macOS compilation — it is `#if os(visionOS)` guarded; keep it that way).
- The report must show the final ordering of lifecycle calls in `runLoop()` as a quoted
  code excerpt, matching: startUpdate/endUpdate → sleep(optimalInputTime) → anchor query
  → particle upload → startSubmission → queryDrawables → encode passes (with
  rasterizationRateMap set when non-empty) → encodePresent → commit → endSubmission.
- A unit test is REQUIRED for any logic extracted in the reorder that is platform-neutral
  (e.g. if a pure helper is introduced for pass-descriptor configuration, test that it
  sets the rate map when one is provided and leaves it nil otherwise). If no
  platform-neutral logic exists after the reorder, the report must state that explicitly
  and the macOS test suite must still pass unchanged.

## test_strategy

    framework: xcodebuild_test
    required: true
    repo: electric-sheep
    scheme: ElectricSheep
    destination: "platform=macOS"

## Constraints

- No new dependencies.
- Do not change matrix math or uniform buffering here (covered by
  `electric_sheep_stereo_eye_matrices.md` and `electric_sheep_triple_buffering.md`).
- Do not change the compositor configuration (formats, layout selection, foveation flag).

## Risks

- If `queryDrawables()` after `startSubmission()` returns empty, the loop must still call
  `endSubmission()` before continuing — failing to do so wedges the frame; mirror the
  existing early-out pattern at lines 102-105.
- `rasterizationRateMaps` indexing differs by layout; binding the wrong map per view is
  worse than binding none. Verify against the SDK (feedback memory: read the
  swiftinterface, never assume).
- The particle upload's `await MainActor.run` hops add latency before submission; do NOT
  move them inside the submission phase (keeps submission tight), even though that might
  look tidier.
