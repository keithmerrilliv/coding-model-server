# Electric Sheep: the atmosphere is not displaced under a foveating rate map

Jira: DEV-854, epic DEV-443. It automates item 2 of DEV-845's device checklist. Repo
`electric-sheep`, `main` = `114e3fe`. Every fact below was read from that commit.

This spec adds one test file and changes no production code. A reference run of exactly
that file was made on the Mac Studio and on the attached Apple Vision Pro before this spec
was written. The measured value is the mean largest-channel difference, out of 255,
between the foveated frame and the uniform one:

| Where | Shader | Mean difference | Result |
| --- | --- | --- | --- |
| macOS host, Metal validation on | `main` | 0.82 | passes |
| macOS host | negative control | 16.1 | fails |
| Vision Pro, through the Mac runner's `on_device` path | `main` | 0.75 | passes |
| Vision Pro | negative control | 16.0 | fails |

The negative control changes one line in `atmosphere_fragment`, so that the atmosphere is
placed by `[[position]]` instead of by its interpolated `uv`:

```metal
float2 uv = float2(frag.position.x / 256.0, 1.0 - frag.position.y / 256.0);
```

That is exactly equal to `main` without a rate map, so it differs only under one. The
threshold is 4.

Also from the reference run:

- The whole `ElectricSheepTests` suite passes on the Vision Pro with the new file:
  TEST SUCCEEDED, 62 Swift Testing tests in 18 suites.
- The test target builds for `generic/platform=visionOS`.

Do not re-derive any of this.

## Context

`ElectricSheep` renders its immersive frame under the compositor's rasterization rate map
(foveation). With a rate map bound, the rasterizer works in **physical**, foveated pixels:

- `[[position]]` in a fragment shader is a physical coordinate;
- interpolated varyings stay in screen space.

So anything that places content by `[[position]]` is squeezed at the centre and stretched
at the periphery once the compositor resolves the frame. That is the symptom of the
original 2026-08-14 report ("unable to see the background pattern on Vision Pro",
DEV-591). DEV-845 item 2 asks for it to be checked on the headset, and nothing guards it.

On `main` the atmosphere is placed by the interpolated `uv` of a fullscreen triangle, so
it is correct by construction. This test keeps it that way: a plausible edit to
`[[position]]` fails it, as the negative control shows.

**How the test works.** It renders one frame twice, the way the stereo path renders two
eyes:

- one `uploadParticleData` call, then two `encodeRenderPasses` calls, so both passes see
  identical uniforms and the same time;
- one pass has no rate map; the other has a map at 0.5 rate outside the centre third;
- for a 15×15 grid of screen points, it compares the foveated pixel at
  `physicalCoordinates(screenCoordinates:layer:)` with the uniform pixel at that point;
- it also requires the atmosphere to vary across the frame (spread > 30), so a flat frame
  cannot pass by matching itself.

The test is gated on `supportsRasterizationRateMap(layerCount: 1)`, like DEV-851's.

**Concurrency.** The target builds with `SWIFT_DEFAULT_ACTOR_ISOLATION = MainActor`. The
new suite is a `@MainActor struct`, as the other Swift Testing suites here are.

## Required change

### `ElectricSheepTests/ImmersiveRateMapGeometryTests.swift` (new)

Write this file exactly. It is the file the reference run used:

```swift
import Testing
import Metal
import simd
@testable import ElectricSheep

/// DEV-845 item 2: under a foveating rasterization rate map, the atmosphere lands where it
/// would without one — no centre-squeeze, no periphery-stretch.
///
/// With a rate map bound, the rasterizer runs in physical (foveated) pixels, and
/// `[[position]]` in a fragment shader is a physical coordinate. Anything that places
/// content by physical position is squeezed at the centre and stretched at the edges once
/// the compositor resolves the frame. That was the shape of the original 2026-08-14
/// "can't see the background" report on Vision Pro (DEV-591).
///
/// The test renders one frame twice, the way the stereo path renders two eyes: one upload,
/// two encodes, so both see identical uniforms. One pass has no rate map; the other has a
/// map that halves the resolution everywhere except the centre third. For a grid of screen
/// points, the foveated frame's pixel at the mapped physical coordinate must match the
/// uniform frame's pixel at that screen point.
@Suite("Immersive geometry under a rasterization rate map")
@MainActor
struct ImmersiveRateMapGeometryTests {

    private static let screen = 256

    private func makeTarget(_ device: any MTLDevice, width: Int, height: Int) throws -> any MTLTexture {
        let d = MTLTextureDescriptor.texture2DDescriptor(
            pixelFormat: .bgra8Unorm, width: width, height: height, mipmapped: false)
        d.usage = [.renderTarget, .shaderRead]
        d.storageMode = .shared
        return try #require(device.makeTexture(descriptor: d))
    }

    private func pass(_ texture: any MTLTexture, rateMap: (any MTLRasterizationRateMap)?) -> MTLRenderPassDescriptor {
        let d = MTLRenderPassDescriptor()
        d.colorAttachments[0].texture = texture
        d.colorAttachments[0].loadAction = .clear
        d.colorAttachments[0].storeAction = .store
        d.colorAttachments[0].clearColor = MTLClearColor(red: 0, green: 0, blue: 0, alpha: 1)
        d.rasterizationRateMap = rateMap
        return d
    }

    private func pixels(_ texture: any MTLTexture) -> [UInt8] {
        var out = [UInt8](repeating: 0, count: texture.width * texture.height * 4)
        out.withUnsafeMutableBytes { raw in
            texture.getBytes(raw.baseAddress!, bytesPerRow: texture.width * 4,
                             from: MTLRegionMake2D(0, 0, texture.width, texture.height),
                             mipmapLevel: 0)
        }
        return out
    }

    /// Largest per-channel difference between two BGRA pixels, 0...255.
    private func difference(_ a: ArraySlice<UInt8>, _ b: ArraySlice<UInt8>) -> Int {
        zip(a.prefix(3), b.prefix(3)).map { abs(Int($0) - Int($1)) }.max() ?? 0
    }

    @Test(
        "The atmosphere is not displaced by a foveating rate map",
        .enabled(if: MTLCreateSystemDefaultDevice()?.supportsRasterizationRateMap(layerCount: 1) == true)
    )
    func atmosphereLandsInScreenSpace() throws {
        let renderer = try #require(
            HalluRenderer(colorPixelFormat: .bgra8Unorm, depthPixelFormat: .invalid))
        let device = renderer.device
        let n = Self.screen

        // Half resolution in the outer thirds, full in the centre: a foveated eye.
        let layer = MTLRasterizationRateLayerDescriptor(sampleCount: MTLSize(width: 3, height: 3, depth: 0))
        for i in 0..<3 {
            layer.horizontal[i] = i == 1 ? 1.0 : 0.5
            layer.vertical[i] = i == 1 ? 1.0 : 0.5
        }
        let map = try #require(device.makeRasterizationRateMap(
            descriptor: MTLRasterizationRateMapDescriptor(
                screenSize: MTLSize(width: n, height: n, depth: 0), layer: layer)))
        let physical = map.physicalSize(layer: 0)
        #expect(physical.width < n, "the map must actually foveate, or the test proves nothing")

        let uniform = try makeTarget(device, width: n, height: n)
        let foveated = try makeTarget(device, width: physical.width, height: physical.height)

        // Atmosphere only: particles would add content that is not under test.
        let simulator = HallucinationSimulator()
        simulator.isDemoMode = false
        simulator.clear()

        let commandBuffer = try #require(renderer.makeCommandBuffer())
        let count = renderer.uploadParticleData(simulator: simulator)
        for (slot, (texture, rateMap)) in [(uniform, nil), (foveated, map)].enumerated() {
            let encoder = try #require(commandBuffer.makeRenderCommandEncoder(
                descriptor: pass(texture, rateMap: rateMap)))
            renderer.encodeRenderPasses(
                encoder: encoder, particleCount: count,
                projection: matrix_identity_float4x4, view: matrix_identity_float4x4,
                model: matrix_identity_float4x4, viewSlot: slot,
                viewportSize: SIMD2(Float(n), Float(n)))
            encoder.endEncoding()
        }
        commandBuffer.commit()
        commandBuffer.waitUntilCompleted()
        #expect(commandBuffer.error == nil, "GPU error: \(String(describing: commandBuffer.error))")

        let reference = pixels(uniform)
        let mapped = pixels(foveated)

        var differences: [Int] = []
        var references: [Int] = []
        for y in stride(from: 16, to: n - 15, by: 16) {
            for x in stride(from: 16, to: n - 15, by: 16) {
                let p = map.physicalCoordinates(
                    screenCoordinates: MTLCoordinate2D(x: Float(x) + 0.5, y: Float(y) + 0.5), layer: 0)
                let px = min(Int(p.x), physical.width - 1)
                let py = min(Int(p.y), physical.height - 1)
                let r = (y * n + x) * 4
                let f = (py * physical.width + px) * 4
                differences.append(difference(reference[r..<r + 4], mapped[f..<f + 4]))
                references.append(Int(reference[r + 2]) + Int(reference[r + 1]) + Int(reference[r]))
            }
        }

        // A flat background would match itself anywhere and prove nothing.
        let spread = (references.max() ?? 0) - (references.min() ?? 0)
        #expect(spread > 30, "the atmosphere must vary across the frame for this test to mean anything (spread \(spread))")

        let mean = Double(differences.reduce(0, +)) / Double(differences.count)
        #expect(mean < 4, "foveated frame is displaced from the uniform one: mean difference \(mean) of 255")
    }
}
```

Nothing else changes. Every API the file calls exists at `main`:

- `HalluRenderer.init?(colorPixelFormat:depthPixelFormat:)`, `device`,
  `makeCommandBuffer()`, `uploadParticleData(simulator:)` and
  `encodeRenderPasses(encoder:particleCount:projection:view:model:viewSlot:viewportSize:)`,
  which `@testable` makes internal access sufficient for;
- `HallucinationSimulator.isDemoMode` and `clear()`;
- Metal's `physicalCoordinates(screenCoordinates:layer:)`. Its older spelling
  `mapScreenToPhysicalCoordinates(_:forLayer:)` is a compile error with this SDK.

## Change surface

`ElectricSheepTests/ImmersiveRateMapGeometryTests.swift` was verified NOT to exist at
`main`. The test target is a synchronized folder, so a new file there needs no project
edit. The plan's implement phase lists exactly this one file as its output.

| Path | Change |
| --- | --- |
| `ElectricSheepTests/ImmersiveRateMapGeometryTests.swift` | new |

The protected files below are served read-only. Do not edit them.

## Acceptance criteria

The tests are the gate. This file is a guard against regression, so on `main` the new test
**passes**. That is expected, and the report should say so. It is not a sign that the test
is vacuous: the negative control above shows it fails on the `[[position]]` regression.

1. **The atmosphere lands in screen space.** `atmosphereLandsInScreenSpace` passes: the
   map foveates (`physical.width < 256`), the command buffer completes without error, the
   atmosphere varies (spread > 30), and the mean difference is < 4 of 255.
2. **Existing suites unchanged.** `AtmosphereRenderTests`, `ImmersiveFrameDriverTests` and
   `ImmersiveRateMapDrawTests` pass unmodified.
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
  - ElectricSheep/Shaders.metal
  - ElectricSheep/HalluRenderer.swift
  - ElectricSheepTests/AtmosphereRenderTests.swift
  - ElectricSheepTests/ImmersiveRateMapDrawTests.swift
  - ElectricSheep.xcodeproj/project.pbxproj
```

## Constraints

- No production file changes. In particular, `Shaders.metal` is not touched. The
  atmosphere is already correct, and this spec guards it.
- Do not add `#if os(visionOS)` around the test. It runs wherever rate maps are
  supported: the Mac, the VM and the Vision Pro.
- Use Swift Testing (`import Testing`), not XCTest.

## Risks

- **Rendering the two passes in separate frames.** `render(...)` advances the renderer's
  time, so two `render` calls would compare different moments of an animated atmosphere,
  and the test would fail on `main`. One `uploadParticleData` followed by two
  `encodeRenderPasses`, as written, keeps both passes on identical uniforms.
- **A uniform rate map.** With every sample at 1.0, physical equals screen, and the test
  cannot tell `uv` from `[[position]]`. The `physical.width < n` expectation guards this.
- **Sampling at the frame's edge.** Physical coordinates at the far edge round to the
  texture's width; the `min(..., physical.width - 1)` clamp keeps every read in bounds.
