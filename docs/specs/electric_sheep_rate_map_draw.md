# Electric Sheep: a real frame draws under a rasterization rate map

Jira: DEV-851, epic DEV-443. Repo `electric-sheep`, `main` = `a0c0803`. Every fact below
was read from that commit.

This spec adds one test file and changes no production code. A reference run of exactly
that file happened on the Mac Studio before this spec was written:

- On macOS with Metal API validation on (`TEST_RUNNER_MTL_DEBUG_LAYER=1`), the new test
  passes, and so do the 7 tests of `ImmersiveFrameDriverTests` beside it.
- The whole `ElectricSheepTests` suite passes on macOS with the new file: 114 passed,
  0 failed, 1 skipped (the opt-in `LiveGenerationTests`).
- **Negative control.** The one change was putting the particle draw in
  `HalluRenderer.swift` back to `type: .point`, the pre-DEV-844 code.
  - With validation on, the new test crashes the test host. The crashing frame is
    `-[MTLDebugRenderCommandEncoder validateDrawPrimitives:…]` called from
    `HalluRenderer.encodeRenderPasses`. That is the "only triangles may be drawn when
    using a rasterization rate map" assertion that DEV-844 hit on the Vision Pro.
  - With validation off, the same `.point` code passes. This is why the suite never saw
    DEV-844.
- The test target builds for `generic/platform=visionOS` with the new file.

Do not re-derive any of this.

## Context

`ElectricSheep` renders its immersive frame under the compositor's rasterization rate map
(foveation, DEV-591). Metal rejects every non-triangle primitive while a rate map is
bound. It rejects it **only as an API-validation assertion**, which fires only when
validation is on *and* a rate map is bound. DEV-844 was exactly that crash:
- the particle pass drew `.point`;
- every test passed;
- the app aborted on the first frame on the Vision Pro.

It was fixed in `a0c0803` by drawing particles as instanced triangles. No test guards
the fix. `ImmersiveFrameDriverTests.rateMapBoundWhenSupplied` builds a rate map and binds
it to a pass descriptor, but never draws.

This spec adds the guard. It draws one real frame, particles included, into a pass built
by the same `ImmersivePass.descriptor(color:depth:slice:rateMap:)` that the immersive
loop uses, with a rate map bound. It then checks that the command buffer completes
without error.

The test is only as strong as the validation layer under it. The spec therefore
sets `device_destination` (below): after code review, the reviewer re-runs the suite on the
attached Apple Vision Pro with Metal API validation on (DEV-850). On macOS the same test
also runs, gated on `supportsRasterizationRateMap(layerCount: 1)`. Where that is false
(for example, a VM), it is skipped, as `rateMapBoundWhenSupplied` already is.

**Concurrency.** The target builds with `SWIFT_DEFAULT_ACTOR_ISOLATION = MainActor`.
`HalluRenderer` and `HallucinationSimulator` are main-actor types. The new suite is a
`@MainActor struct`, as the existing Swift Testing suites in this target are.

## Required change

### `ElectricSheepTests/ImmersiveRateMapDrawTests.swift` (new)

Write this file exactly. It is the file the reference run used:

```swift
import Testing
import Metal
import simd
@testable import ElectricSheep

/// DEV-851: the real immersive frame draws under a rasterization rate map.
///
/// Metal rejects every non-triangle primitive while a rate map is bound, and the
/// rejection is an API-validation assertion: it only fires with Metal API validation
/// on and a rate map bound. DEV-844 shipped exactly that crash past a green suite,
/// because no test ever drew with a map bound. On the device leg (DEV-850), which
/// runs with validation on, this test is what catches a `.point` draw coming back.
@Suite("Immersive frame under a rasterization rate map")
@MainActor
struct ImmersiveRateMapDrawTests {

    @Test(
        "One real frame, particles included, draws with a rate map bound",
        .enabled(if: MTLCreateSystemDefaultDevice()?.supportsRasterizationRateMap(layerCount: 1) == true)
    )
    func realFrameDrawsUnderARateMap() throws {
        let renderer = try #require(
            HalluRenderer(colorPixelFormat: .rgba16Float, depthPixelFormat: .depth32Float)
        )
        let device = renderer.device

        let layer = MTLRasterizationRateLayerDescriptor(sampleCount: MTLSize(width: 1, height: 1, depth: 0))
        layer.horizontal[0] = 1.0
        layer.vertical[0] = 1.0
        let map = try #require(device.makeRasterizationRateMap(
            descriptor: MTLRasterizationRateMapDescriptor(
                screenSize: MTLSize(width: 64, height: 64, depth: 0), layer: layer)))
        let physical = map.physicalSize(layer: 0)

        func target(_ format: MTLPixelFormat) throws -> any MTLTexture {
            let d = MTLTextureDescriptor.texture2DDescriptor(
                pixelFormat: format, width: physical.width, height: physical.height,
                mipmapped: false)
            d.textureType = .type2DArray
            d.arrayLength = 1
            d.usage = .renderTarget
            d.storageMode = .private
            return try #require(device.makeTexture(descriptor: d))
        }

        let simulator = HallucinationSimulator()
        simulator.isDemoMode = false
        simulator.clear()
        simulator.spawnParticle(
            HalluParticle(
                position: SIMD3(0, 0, 0),
                velocity: .zero,
                color: HallucinationType.empathicResonance.baseColor,
                size: 0.04,
                age: 0,
                lifetime: 10,
                type: .empathicResonance
            )
        )

        let desc = ImmersivePass.descriptor(
            color: try target(.rgba16Float), depth: try target(.depth32Float),
            slice: nil, rateMap: map)
        #expect(desc.rasterizationRateMap === map)

        let commandBuffer = try #require(renderer.makeCommandBuffer())
        let encoder = try #require(commandBuffer.makeRenderCommandEncoder(descriptor: desc))
        renderer.render(
            encoder: encoder,
            simulator: simulator,
            viewportSize: SIMD2(Float(physical.width), Float(physical.height))
        )
        encoder.endEncoding()
        commandBuffer.commit()
        commandBuffer.waitUntilCompleted()

        #expect(commandBuffer.error == nil,
                "GPU error drawing under a rate map: \(String(describing: commandBuffer.error))")
    }
}
```

Nothing else changes. Every API the file calls exists at `main` with exactly these
signatures:
- `HalluRenderer.init?(colorPixelFormat:depthPixelFormat:)`, `renderer.device`, and
  `renderer.makeCommandBuffer()`;
- `render(encoder:simulator:viewportSize:)`;
- `ImmersivePass.descriptor(color:depth:slice:rateMap:)`;
- `HallucinationSimulator.isDemoMode`, `clear()` and `spawnParticle(_:)`.

## Change surface

`ElectricSheepTests/ImmersiveRateMapDrawTests.swift` was verified NOT to exist at `main`.
The test target is a synchronized folder, so a new file there needs no project edit. The
plan's implement phase lists exactly this one file as its output.

| Path | Change |
| --- | --- |
| `ElectricSheepTests/ImmersiveRateMapDrawTests.swift` | new |

The protected files below are served read-only. Do not edit them.

## Acceptance criteria

The tests are the gate. This file guards against a regression, so on `main` the new test
**passes**. That is expected, and the report should say so. It does not mean the test is
vacuous: the negative control above shows it fails on the `.point` draw.

1. **A real frame draws under a rate map.** `realFrameDrawsUnderARateMap` passes: the
   command buffer completes with `error == nil`, and the pass carries the map.
2. **Existing suites unchanged.** `ImmersiveFrameDriverTests` and `AtmosphereRenderTests`
   pass unmodified.
3. **Whole suite green on macOS.** `xcodebuild test` over `ElectricSheepTests` passes. The
   opt-in `LiveGenerationTests` skips, as on `main`.
4. **Green on the device.** The reviewer's device leg passes on the attached Apple Vision
   Pro with Metal API validation on.

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
  - ElectricSheep/HalluRenderer.swift
  - ElectricSheep/ImmersiveFrameDriver.swift
  - ElectricSheepTests/ImmersiveFrameDriverTests.swift
  - ElectricSheepTests/AtmosphereRenderTests.swift
  - ElectricSheep.xcodeproj/project.pbxproj
```

## Constraints

- No production file changes. In particular, do not touch `HalluRenderer.swift`. The
  particle draw there is already correct, and this spec guards it.
- Do not add `#if os(visionOS)` around the test. It must run on both platforms, gated
  only by `supportsRasterizationRateMap(layerCount: 1)`.
- Swift Testing (`import Testing`), not XCTest. `ImmersiveFrameDriverTests` uses the same
  framework and the same gate.

## Risks

- **Weakening the gate.** Dropping `.enabled(if:)` fails the suite on any Metal device
  without rate-map support, and a VM is one. Gating on `#if os(visionOS)` instead removes
  the macOS coverage the reference run relied on.
- **Drawing with no particles.** Without `spawnParticle`, the particle draw has an
  instance count of 0, and the frame never exercises the primitive type the guard exists
  for. Keep `isDemoMode = false`, `clear()` and the one spawned particle.
- **Sizing the targets from the screen size.** A texture bound under a rate map must be
  at least the map's **physical** size. `map.physicalSize(layer: 0)` gives it. Using the
  64×64 screen size instead happens to work at a 1.0 rate, but is wrong in general.
