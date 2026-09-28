# Architecture: Immersive Rate Map Geometry Test (DEV-854)

## Overview
Adds a single Swift Testing file that verifies the atmosphere shader places content by interpolated screen-space UVs rather than physical fragment positions under a foveating rasterization rate map. No production code changes — only a guard test rendered into two targets (uniform vs. foveated), compared pixel-by-pixel over a grid. The renderer (`HalluRenderer`) and simulator (`HallucinationSimulator`) are pre-existing; all Metal shaders remain untouched.

## Components
- **ImmersiveRateMapGeometryTests** — `@MainActor` test suite containing one test method that renders an atmosphere frame twice (once with no rate map, once with a 0.5× outer-third rate map) using identical uniforms via one `uploadParticleData` + two `encodeRenderPasses`, then compares corresponding pixels across a 16-stride grid.

## File Structure
```
ElectricSheepTests/ImmersiveRateMapGeometryTests.swift   # new — sole deliverable
```

All other files listed in the plan as protected are read-only context:
- `ElectricSheep/Shaders.metal` — existing Metal shaders (atmosphere uses interpolated `uv`, not `[[position]]`)
- `ElectricSheep/HalluRenderer.swift` — existing renderer class providing `makeCommandBuffer()`, `uploadParticleData(simulator:)`, `encodeRenderPasses(...)`
- `ElectricSheepTests/AtmosphereRenderTests.swift` — existing test suite (must pass unmodified)
- `ElectricSheepTests/ImmersiveRateMapDrawTests.swift` — existing test suite (must pass unmodified)
- `ElectricSheep.xcodeproj/project.pbxproj` — project file (no edits needed; synchronized folder auto-discovers new `.swift` in ElectricSheepTests/)

## Data Models
No new types declared. The design references only pre-existing symbols from the app target (`@testable import ElectricSheep`):
- `HalluRenderer` — constructor, `device`, `makeCommandBuffer()`, `uploadParticleData(simulator:)`, `encodeRenderPasses(encoder:particleCount:projection:view:model:viewSlot:viewportSize:)`
- `HallucinationSimulator` — default init, `isDemoMode`, `clear()`
- Metal API — `MTLRasterizationRateLayerDescriptor`, `MTLRasterizationRateMapDescriptor`, `physicalCoordinates(screenCoordinates:layer:)`

Constants used by the test (named for testability per rule 10):
| Symbol | Value | Purpose |
|---|---|---|
| `Self.screen` | `256` | Uniform frame dimension and rate-map screen size |
| grid stride | `16` | Step between sampled points (y: 16..n-15, x: 16..n-15) |
| spread threshold | `30` | Minimum atmosphere variation required to prove non-triviality |
| mean-diff threshold | `4/255` | Maximum allowed displacement under foveation |

## Implementation Notes
- The file is a **verbatim copy** of the source provided in the specification. Do not alter any line.
- One upload + two encodes keeps both passes on identical uniforms (`time`, density, type levels). Calling `render(...)` twice would advance internal time and produce different frames.
- Rate map layer has three samples horizontally/vertically; centre sample at 1.0, outer two at 0.5 — this guarantees `physical.width < n`.
- Physical coordinate reads are clamped with `min(Int(p.x), physical.width - 1)` to avoid out-of-bounds texture fetches at edges.
- The test is gated by `.enabled(if:)` checking `supportsRasterizationRateMap(layerCount: 1)`. It runs wherever Metal supports it (Mac host, VM, Vision Pro) without `#if os(visionOS)`.
- Conformance: suite struct annotated `@MainActor`; no explicit annotation needed per project build setting but included for clarity matching sibling suites.
- Pixel comparison uses largest-channel difference across RGB only (alpha excluded); BGRA layout means indices `[r] = blue`, `[r+1] = green`, `[r+2] = red`.

## Acceptance Criteria Checklist
- [ ] C1: atmosphereLandsInScreenSpace passes: the rate map foveates (`physical.width < 256`), command buffer completes without error, atmosphere varies across frame (spread > 30), mean pixel difference between uniform and foveated frames < 4 of 255
- [ ] C2: Existing suites AtmosphereRenderTests, ImmersiveFrameDriverTests, ImmersiveRateMapDrawTests pass unmodified
- [ ] C3: xcodebuild test over ElectricSheepTests scheme passes on macOS host with Metal validation enabled
- [ ] C4: Device leg passes on attached Apple Vision Pro through Mac runner's on_device path

## Criterion Seams
- C1 | setup: `(none — self-contained test method)` | act: `` try atmosphereLandsInScreenSpace() `` | assert: all four internal expectations succeed (`#expect(physical.width < n)`, `#expect(commandBuffer.error == nil)`, `#expect(spread > 30)`, `#expect(mean < 4)`)
- C2 | suite-level
- C3 | suite-level
- C4 | suite-level
