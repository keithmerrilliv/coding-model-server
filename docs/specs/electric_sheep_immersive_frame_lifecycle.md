# Electric Sheep: the immersive frame lifecycle, in Apple's order, with a slot held for every committed frame

Jira: DEV-589 (drawables queried before `startSubmission`), DEV-757 (the immersive path
never takes a triple-buffer slot), DEV-591 (foveation enabled, rate map never bound).
Epic DEV-443. Repo `electric-sheep`, `main` = `1ae39e3` (run 56's delivery). Every fact
below was read from that commit before this spec was written; do not re-derive them.

## Why this spec is shaped the way it is

All three defects live in `ElectricSheep/ImmersiveRenderLoop.swift`, and that file is
wrapped **entirely** in `#if os(visionOS)`. This run's test destination is macOS, which
never compiles it. So a change made only there would pass every gate in this pipeline
while being unchecked by any compiler or test.

The fix is to move the thing that can be wrong — the ORDER of the frame's steps, and the
balance of slot acquire against slot release — into a new platform-neutral file that the
macOS suite can drive with recording closures. `ImmersiveRenderLoop.swift` becomes a thin
adapter that hands the real CompositorServices calls to it. That adapter is given below
**verbatim**: transcribe it exactly. It is compile-checked separately against the visionOS
SDK after this phase, and behaviour is checked on a Vision Pro.

## Context — current `runLoop()` (lines 69–181), the parts that matter

```swift
            guard let frame = layerRenderer.queryNextFrame() else { continue }
            frame.startUpdate()
            frame.endUpdate()

            guard let timing = frame.predictTiming() else { continue }
            try? await clock.sleep(until: timing.optimalInputTime, tolerance: nil)

            let drawables = frame.queryDrawables()          // line 86  <- DEV-589: before startSubmission
            if drawables.isEmpty { continue }
            ...                                               // device anchor, set on each drawable
            let particleCount = await snapshotAndUpload()
            await MainActor.run { renderer.updateTiming() }

            frame.startSubmission()                           // line 107
            guard let commandBuffer = renderer.makeCommandBuffer() else {
                frame.endSubmission()
                continue
            }
            ...                                               // per drawable, per view: build an
                                                              // MTLRenderPassDescriptor (lines 123-139),
                                                              // encode, endEncoding; encodePresent
            commandBuffer.commit()
            frame.endSubmission()
```

Three defects:

1. **DEV-589.** `queryDrawables()` runs before `startSubmission()`. CompositorServices
   requires drawables to be acquired inside the submission phase.
2. **DEV-757.** `HalluRenderer` has a triple-buffer discipline — `beginFrame()` waits on a
   3-slot semaphore and advances `frameIndex`; `finishFrame(commandBuffer:)` registers a
   completion handler that signals it; `abandonFrame()` signals immediately for a frame
   begun but not committed. The MTKView path (`MetalViewRepresentable.swift:44,58`) uses
   all three. **The immersive path calls none of them**, so its `frameIndex` never moves
   and the CPU overwrites buffers the GPU may still be reading. `uploadParticleData` and
   `updateTiming` write into slot `frameIndex`, so they must run AFTER `beginFrame()`.
3. **DEV-591.** Line 24 sets `configuration.isFoveationEnabled = capabilities.supportsFoveation`,
   so drawable textures are allocated warped, but no render pass descriptor ever sets
   `rasterizationRateMap`. Content is rasterized linearly into a warped texture.

`HalluRenderer.swift` already provides, verbatim:

```swift
    func beginFrame() {
        _ = frameSemaphore.wait(timeout: .distantFuture)
        frameIndex = (frameIndex + 1) % HalluRenderer.maxFramesInFlight
        framesBegun += 1
    }
    func abandonFrame() { frameSemaphore.signal() }
    func finishFrame(commandBuffer: MTLCommandBuffer) {
        let sem = frameSemaphore
        commandBuffer.addCompletedHandler { _ in sem.signal() }
    }
```

**Isolation.** The app target sets `SWIFT_DEFAULT_ACTOR_ISOLATION = MainActor`, so every
declaration in a new app-target file is `@MainActor` unless marked otherwise. That is
intended here: `ImmersiveRenderLoop` and `HalluRenderer` are main-actor today, and the new
driver must be too. **Do NOT mark the new types `nonisolated`.** Tests that call them are
`@MainActor`.

## Required change

### `ElectricSheep/ImmersiveFrameDriver.swift` (new, platform-neutral — no `#if`)

Transcribe exactly:

```swift
import Metal

/// How one immersive frame's submission phase ended (DEV-589, DEV-757).
enum ImmersiveFrameOutcome: Equatable {
    /// Drawables were encoded, presented and committed.
    case rendered
    /// The compositor offered no drawables. No triple-buffer slot was taken.
    case noDrawables
    /// A slot was taken but no command buffer could be made; the slot was given back.
    case noCommandBuffer
}

/// The submission half of one CompositorServices frame, in the order Apple requires,
/// holding one triple-buffer slot for exactly the frames it commits.
///
/// Platform-neutral on purpose. `ImmersiveRenderLoop` (visionOS only) passes the real
/// `LayerRenderer.Frame` and `HalluRenderer` calls; the macOS tests pass recording
/// closures. The ordering is therefore proven on the only platform a test can run on.
///
/// Every path out calls `endSubmission` exactly once, and every `beginSlot` is matched by
/// exactly one `finishSlot` or `abandonSlot` — an unmatched acquire wedges a 3-slot
/// semaphore after three frames, at 90 Hz, on a headset.
enum ImmersiveSubmission {
    static func run<Drawable, Buffer, Prepared>(
        startSubmission: () -> Void,
        queryDrawables: () -> [Drawable],
        beginSlot: () -> Void,
        abandonSlot: () -> Void,
        prepare: () async -> Prepared,
        makeCommandBuffer: () -> Buffer?,
        encode: ([Drawable], Prepared, Buffer) -> Void,
        finishSlot: (Buffer) -> Void,
        commit: (Buffer) -> Void,
        endSubmission: () -> Void
    ) async -> ImmersiveFrameOutcome {
        startSubmission()
        let drawables = queryDrawables()
        guard !drawables.isEmpty else {
            endSubmission()
            return .noDrawables
        }
        beginSlot()
        let prepared = await prepare()
        guard let buffer = makeCommandBuffer() else {
            abandonSlot()
            endSubmission()
            return .noCommandBuffer
        }
        encode(drawables, prepared, buffer)
        finishSlot(buffer)
        commit(buffer)
        endSubmission()
        return .rendered
    }
}

/// One eye's render pass for the immersive path (DEV-591).
enum ImmersivePass {
    /// Clears to the scene colour with reverse-Z depth, targets `slice` of an array
    /// texture when given, and binds the compositor's rasterization rate map when
    /// foveation supplies one. Without the map, content is drawn linearly into a warped
    /// texture and the compositor's unwarp distorts it.
    static func descriptor(
        color: any MTLTexture,
        depth: any MTLTexture,
        slice: Int?,
        rateMap: (any MTLRasterizationRateMap)?
    ) -> MTLRenderPassDescriptor {
        let d = MTLRenderPassDescriptor()
        d.colorAttachments[0].texture = color
        d.colorAttachments[0].loadAction = .clear
        d.colorAttachments[0].storeAction = .store
        d.colorAttachments[0].clearColor = MTLClearColor(red: 0.02, green: 0.01, blue: 0.05, alpha: 1)
        d.depthAttachment.texture = depth
        d.depthAttachment.loadAction = .clear
        d.depthAttachment.storeAction = .store
        d.depthAttachment.clearDepth = 0.0 // Reverse-Z: 0 = far
        d.renderTargetArrayLength = 1
        if let slice {
            d.colorAttachments[0].slice = slice
            d.depthAttachment.slice = slice
        }
        d.rasterizationRateMap = rateMap
        return d
    }
}
```

### `ElectricSheep/ImmersiveRenderLoop.swift` (modified — replace `runLoop()` whole, add one method)

This file is visionOS-only and **no gate in this run can compile it**. Transcribe exactly.
Replace the entire `private func runLoop() async { ... }` (current lines 69–181) with the
two methods below. Change nothing else in the file: not the compositor configuration, not
`init`, not `start()`, not `snapshotAndUpload()`.

```swift
    private func runLoop() async {
        let clock = LayerRenderer.Clock()

        while true {
            guard layerRenderer.state == .running else {
                if layerRenderer.state == .invalidated { return }
                try? await Task.sleep(nanoseconds: 10_000_000)
                continue
            }

            guard let frame = layerRenderer.queryNextFrame() else { continue }
            frame.startUpdate()
            frame.endUpdate()

            guard let timing = frame.predictTiming() else { continue }
            try? await clock.sleep(until: timing.optimalInputTime, tolerance: nil)

            // The device anchor needs only the timing, not the drawables, so it is
            // queried before the submission phase begins.
            let anchorTime = LayerRenderer.Clock.Instant.epoch
                .duration(to: timing.trackableAnchorTime)
            let anchorTimestamp = Double(anchorTime.components.seconds)
                + Double(anchorTime.components.attoseconds) * 1e-18
            let deviceAnchor = worldTracking.queryDeviceAnchor(atTimestamp: anchorTimestamp)

            // DEV-589 / DEV-757: the submission phase in Apple's order, holding one
            // triple-buffer slot for exactly the frames it commits.
            _ = await ImmersiveSubmission.run(
                startSubmission: { frame.startSubmission() },
                queryDrawables: { frame.queryDrawables() },
                beginSlot: { self.renderer.beginFrame() },
                abandonSlot: { self.renderer.abandonFrame() },
                prepare: {
                    let particleCount = await self.snapshotAndUpload()
                    await MainActor.run { self.renderer.updateTiming() }
                    return particleCount
                },
                makeCommandBuffer: { self.renderer.makeCommandBuffer() },
                encode: { drawables, particleCount, commandBuffer in
                    self.encode(
                        drawables: drawables,
                        particleCount: particleCount,
                        deviceAnchor: deviceAnchor,
                        commandBuffer: commandBuffer
                    )
                },
                finishSlot: { self.renderer.finishFrame(commandBuffer: $0) },
                commit: { $0.commit() },
                endSubmission: { frame.endSubmission() }
            )
        }
    }

    private func encode(
        drawables: [LayerRenderer.Drawable],
        particleCount: Int,
        deviceAnchor: DeviceAnchor?,
        commandBuffer: MTLCommandBuffer
    ) {
        let model = simd_float4x4(1)

        for drawable in drawables {
            drawable.deviceAnchor = deviceAnchor

            let viewCount = drawable.views.count
            let textureCount = drawable.colorTextures.count
            // Layered layout: ONE array texture, one slice per view. Dedicated layout
            // (the fallback at line 28): one texture per view, no slices. The old code
            // always used colorTextures[0] and so drew both eyes into one texture on the
            // dedicated layout.
            let layered = textureCount == 1 && viewCount > 1
            let rateMaps = drawable.rasterizationRateMaps

            for viewIndex in 0..<viewCount {
                let textureIndex = layered ? 0 : min(viewIndex, textureCount - 1)

                // DEV-591: bind the compositor's rate map for the texture this view
                // draws into. Behaviour is confirmed on device, not here.
                let rateMap: (any MTLRasterizationRateMap)? = rateMaps.isEmpty
                    ? nil
                    : rateMaps[min(textureIndex, rateMaps.count - 1)]

                let renderPassDesc = ImmersivePass.descriptor(
                    color: drawable.colorTextures[textureIndex],
                    depth: drawable.depthTextures[textureIndex],
                    slice: layered ? viewIndex : nil,
                    rateMap: rateMap
                )

                guard let encoder = commandBuffer.makeRenderCommandEncoder(
                    descriptor: renderPassDesc
                ) else { continue }

                let eyeTransform = drawable.views[viewIndex].transform
                let perEyeViewMat: simd_float4x4
                if let anchor = deviceAnchor {
                    perEyeViewMat = perEyeViewMatrix(
                        originFromAnchor: anchor.originFromAnchorTransform,
                        eyeTransform: eyeTransform
                    )
                } else {
                    perEyeViewMat = simd_float4x4(1)
                }

                let projection = drawable.computeProjection(viewIndex: viewIndex)

                renderer.encodeRenderPasses(
                    encoder: encoder,
                    particleCount: particleCount,
                    projection: projection,
                    view: perEyeViewMat,
                    model: model,
                    viewSlot: viewIndex
                )

                encoder.endEncoding()
            }

            drawable.encodePresent(commandBuffer: commandBuffer)
        }
    }
```

The per-eye matrix, projection and `encodeRenderPasses` call are the current lines
145–170 unchanged; only the descriptor construction moved into `ImmersivePass`.

### `ElectricSheepTests/ImmersiveFrameDriverTests.swift` (new)

Swift Testing: `import Testing`, `import Metal`, `@testable import ElectricSheep`, a
`struct ImmersiveFrameDriverTests`, and every test `@Test @MainActor func …() async` —
the driver is main-actor by the app target's default isolation. A test that calls
`try #require` must also be `throws` (DEV-791). No XCTest.

Given code — the recorder and a one-frame helper. Transcribe exactly; the tests use them:

```swift
@MainActor
final class CallLog {
    private(set) var calls: [String] = []
    func note(_ s: String) { calls.append(s) }
    func count(_ s: String) -> Int { calls.filter { $0 == s }.count }
}

@MainActor
private func runFrame(_ log: CallLog, drawables: [Int], buffer: String?) async -> ImmersiveFrameOutcome {
    await ImmersiveSubmission.run(
        startSubmission: { log.note("startSubmission") },
        queryDrawables: { log.note("queryDrawables"); return drawables },
        beginSlot: { log.note("beginSlot") },
        abandonSlot: { log.note("abandonSlot") },
        prepare: { log.note("prepare"); return 42 },
        makeCommandBuffer: { log.note("makeCommandBuffer"); return buffer },
        encode: { _, _, _ in log.note("encode") },
        finishSlot: { _ in log.note("finishSlot") },
        commit: { _ in log.note("commit") },
        endSubmission: { log.note("endSubmission") }
    )
}
```

For the descriptor tests, make textures with `MTLCreateSystemDefaultDevice()` —
`try #require` it — an `.rgba16Float` `type2DArray` colour texture with `arrayLength = 2`
and a `.depth32Float` `type2DArray` depth texture with `arrayLength = 2`, both 16×16,
`storageMode = .private`, usage `.renderTarget`.

## Change surface

`ElectricSheep/ImmersiveRenderLoop.swift` was verified to exist at `main`;
`ElectricSheep/ImmersiveFrameDriver.swift` and `ElectricSheepTests/ImmersiveFrameDriverTests.swift`
verified NOT to exist. The plan's implement phase lists exactly these three as outputs. New
files go in exactly these directories — the project uses synchronized root groups, and a
file elsewhere is never compiled.

| Path | Change |
| --- | --- |
| `ElectricSheep/ImmersiveFrameDriver.swift` | new (transcribe as given) |
| `ElectricSheep/ImmersiveRenderLoop.swift` | modified (`runLoop()` replaced, `encode(...)` added, as given) |
| `ElectricSheepTests/ImmersiveFrameDriverTests.swift` | new |

The three protected files below are served read-only; do not edit them.

## Acceptance criteria

A green build is NOT sufficient — the tests are the gate. Criteria 1–6 do not compile on
`main` (`ImmersiveSubmission` and `ImmersivePass` do not exist); say so in the report.

1. **The rendered path runs in Apple's order.** `runFrame(log, drawables: [1, 2], buffer: "cb")`
   returns `.rendered` and `log.calls` equals exactly
   `["startSubmission", "queryDrawables", "beginSlot", "prepare", "makeCommandBuffer", "encode", "finishSlot", "commit", "endSubmission"]` (nine entries).
2. **Drawables are queried inside the submission phase (DEV-589).** In that same log,
   `startSubmission` is the first entry and `queryDrawables` comes after it; and `prepare`
   (which writes into the slot) comes after `beginSlot`.
3. **No drawables takes no slot.** `runFrame(log, drawables: [], buffer: "cb")` returns
   `.noDrawables` and `log.calls` equals exactly `["startSubmission", "queryDrawables", "endSubmission"]` —
   `beginSlot` never called.
4. **A failed command buffer gives its slot back (DEV-757).** `runFrame(log, drawables: [1], buffer: nil)`
   returns `.noCommandBuffer` and `log.calls` equals exactly
   `["startSubmission", "queryDrawables", "beginSlot", "prepare", "makeCommandBuffer", "abandonSlot", "endSubmission"]` (seven entries).
5. **Slots balance across any mix of frames.** On one `CallLog`, run 30 frames cycling
   through the three cases in order (rendered, no drawables, no command buffer, rendered,
   …). After every frame, `log.count("beginSlot") == log.count("finishSlot") + log.count("abandonSlot")`,
   and after all 30, `log.count("endSubmission") == 30` and `log.count("startSubmission") == 30`.
6. **The pass descriptor is built correctly (DEV-591).** With `rateMap: nil` and `slice: 1`:
   `rasterizationRateMap` is nil; colour attachment `loadAction == .clear`,
   `storeAction == .store`, `clearColor` red 0.02 / green 0.01 / blue 0.05 / alpha 1
   (compare each with tolerance 1e-9); `depthAttachment.clearDepth == 0`;
   `renderTargetArrayLength == 1`; both attachments' `slice == 1`; and the attachments'
   `texture` are the ones passed in (`===`). With `slice: nil`, both `slice == 0`.
7. **The rate map is bound when one is supplied.** A separate test, enabled only when the
   device supports it —
   `@Test(.enabled(if: MTLCreateSystemDefaultDevice()?.supportsRasterizationRateMap(layerCount: 1) == true))` —
   builds a map and asserts `descriptor.rasterizationRateMap === map`. Build the map exactly
   like this:

   ```swift
   let layer = MTLRasterizationRateLayerDescriptor(sampleCount: MTLSize(width: 1, height: 1, depth: 0))
   layer.horizontal[0] = 1.0
   layer.vertical[0] = 1.0
   let mapDescriptor = MTLRasterizationRateMapDescriptor(
       screenSize: MTLSize(width: 16, height: 16, depth: 0), layer: layer)
   let map = try #require(device.makeRasterizationRateMap(descriptor: mapDescriptor))
   ```

   This test may be SKIPPED on a virtualised GPU; that is acceptable and must not be
   worked around. Criterion 6 always runs.
8. **Existing behaviour intact.** The whole `ElectricSheepTests` suite is green, including
   `HalluRendererTripleBufferTests`, which pins the `beginFrame`/`finishFrame` API this
   spec calls.

## test_strategy

    framework: xcodebuild_test
    required: true
    repo: electric-sheep
    base_ref: main
    scheme: ElectricSheep
    destination: "platform=macOS"
    filter: ElectricSheepTests
    default_actor_isolation: MainActor
    protected_paths:
      - ElectricSheep/HalluRenderer.swift
      - ElectricSheep/MetalViewRepresentable.swift
      - ElectricSheepTests/HalluRendererTripleBufferTests.swift

## Constraints

- No new dependencies, no `.metal` changes, no change to `HalluRenderer`'s API.
- `ImmersiveFrameDriver.swift` has **no** `#if os(...)` and imports only `Metal`. It must
  compile on macOS; that is the whole point of it.
- Do not mark `ImmersiveFrameOutcome`, `ImmersiveSubmission` or `ImmersivePass`
  `nonisolated`. They take the target's default `@MainActor`, which is what the render
  loop and the renderer already use.
- `ImmersiveRenderLoop.swift` is transcribed, not redesigned. Keep its `#if os(visionOS)`
  wrapper and every line outside `runLoop()`.

## Risks

- **An edit to the visionOS file that no gate here can see.** A typo in
  `ImmersiveRenderLoop.swift` would pass this run's macOS gate. That is why its code is
  given verbatim and why it is compile-checked against the visionOS SDK separately.
- **An unmatched slot.** Every `beginSlot` needs exactly one `finishSlot` or `abandonSlot`,
  or the loop wedges after three frames. Criteria 4 and 5 exist for this.
- **Upload before the slot.** `prepare` writes into slot `frameIndex`; running it before
  `beginSlot` writes into the slot the GPU may still be reading — the original DEV-590
  tear. Criterion 2 pins the order.
- **`@MainActor` on the tests.** The driver is main-actor by default isolation; a
  nonisolated test cannot call it synchronously. Every test is `@Test @MainActor func … async`.
- **Rate-map indexing on the layered layout** cannot be judged on macOS. The spec binds
  one map per texture and leaves the per-eye question to the device.
- **The dedicated-layout texture choice changes behaviour** on that fallback only: each
  view now draws into its own texture instead of both into `colorTextures[0]`. The Vision
  Pro takes the layered branch (line 28 prefers it), where behaviour is unchanged.
