# Electric Sheep: no eye is rasterized with the other eye's foveation map

Jira: DEV-909, epic DEV-443. Repo `electric-sheep`, `main` = `2a557bf`. Every fact below
was read from that commit.

This spec changes one visionOS-only production file, adds one platform-neutral production
file and adds one test file. A reference run of exactly this change was made on the Mac
Studio before the spec was written:

- **With the fix**, on macOS with Metal API validation on (`TEST_RUNNER_MTL_DEBUG_LAYER=1`,
  ad-hoc signing as the Mac runner does it), the whole `ElectricSheepTests` suite passed:
  125 passed, 0 failed, 1 skipped (the opt-in `LiveGenerationTests`). All 9 new tests
  passed.
- **The app builds for `generic/platform=visionOS`** with the fix. That is the only build
  that compiles `ImmersiveRenderLoop.swift`. There were no errors and no warnings in the
  changed files.
- **Negative control.** The two new functions were rewritten to do what `main` does
  (keep foveation and pick `.layered` when it is offered; bind map `viewIndex`, falling
  back to map 0). Then 5 of the 9 new tests failed: `foveationPrefersDedicated`,
  `foveationWithoutDedicatedIsDropped`, `foveationImpliesDedicated`,
  `layeredRightEyeBindsNothing` and `noFallbackToMapZero`.
- On `main` itself the new test file does not compile, because the symbols it tests do
  not exist yet.
- **No device run.** The Vision Pro showed as `unavailable` in `devicectl` during this
  work. The visual effect (the right eye's periphery) cannot be checked by any automated
  test. See "Device check" below.

Do not re-derive any of this.

## Context

`ElectricSheep` renders the immersive space through CompositorServices, with foveation
when the device supports it (DEV-591). A device diagnostic on the Vision Pro M5
(visionOS 27.0, `main` = `2a557bf`) reported this:

```
config foveation=true chosen=layered
drawable[0] views=2 colorTextures=1 depthTextures=1 rateMaps=1
  color[0] 2048x1984 arrayLength=2 textureType=2DArray pixelFormat=rgba16Float
  rateMap[0] layerCount=2 screen=5087x4081 physical(layer0)=2048x1984
  view[0] textureIndex=0 slice=0 rateMapIdx=0
  view[1] textureIndex=0 slice=1 rateMapIdx=0
```

Under the `.layered` layout, the compositor supplies **one** texture with two slices and
**one** rate map with **two layers**, one layer per eye. The render loop draws each eye in
its own pass, with `renderTargetArrayLength = 1` and the eye's slice set on the
attachment. Metal rasterizes a pass with the rate-map layer whose index equals the render
target array index. In a single-layer pass that index is always 0. So the right eye
(slice 1) is rasterized with layer 0, the **left** eye's foveal warp. The compositor
then unwarps slice 1 with layer 1. The expected symptom is blur or distortion in the right
eye's periphery that the left eye does not have. Nobody has confirmed this visually yet.

CompositorServices has no per-view rate-map index. `drawable.rasterizationRateMaps` has
one entry per texture. Under `.dedicated`, each eye has its own 2D texture and its own
single-layer map, so a per-eye pass with map `textureIndex` is correct.

**The fix in this spec** (option 2 in the ticket):

1. Turn foveation on only together with `.dedicated`. When `.dedicated` is not offered
   with foveation, turn foveation off. Do not render `.layered` with the wrong layer.
   Without foveation there are no rate maps, so `.layered` is correct again.
2. Bind a rate map to a pass only when the map's layer 0 really belongs to that pass:
   map `textureIndex`, and only when the pass draws slice 0 or has no slice. There is no
   fallback to map 0.

Both decisions are pure functions in a **new platform-neutral file**, so the macOS suite
can test them. `ImmersiveRenderLoop.swift` is entirely inside `#if os(visionOS)`, so macOS
never compiles it. Its edits are given below **verbatim**. Transcribe them exactly.

**Concurrency.** The app target builds with `SWIFT_DEFAULT_ACTOR_ISOLATION = MainActor`.
The three new types are marked `nonisolated`. They are pure values, and
`HallucinationCompositorConfig.makeConfiguration` must be able to call them whatever
isolation CompositorServices calls it with. The test suite is a `@MainActor struct`, like
the other Swift Testing suites in this target.

### Left out (not this spec)

- **Vertex amplification** (ticket option 1). This is one `.layered` pass with
  `renderTargetArrayLength = 2`, `[[amplification_id]]` and `render_target_array_index`.
  It would keep foveation together with the layered layout's memory saving. It needs
  changes in `Shaders.metal` and `HalluRenderer.swift`, and DEV-910 and DEV-860 are
  working on those files now.
- Any diagnostic logging on the device.

## Required change

### 1. `ElectricSheep/ImmersiveRateMapLayout.swift` (new, platform-neutral, no `#if`)

Write this file exactly. It is the file the reference run used:

```swift
/// The two CompositorServices texture layouts this app can render (DEV-909).
///
/// Platform-neutral on purpose, like `ImmersiveSubmission`: `LayerRenderer.Layout`
/// exists only on visionOS, so the decision is made on this mirror and the macOS
/// suite tests it. `HallucinationCompositorConfig` maps it to the real type.
nonisolated enum ImmersiveLayout: Equatable, Sendable {
    /// One texture per eye, and one rasterization rate map per texture.
    case dedicated
    /// One 2D-array texture, one slice per eye, and ONE rate map with one layer per eye.
    case layered
}

/// What the compositor layer is configured with: foveation on or off, and which layout.
nonisolated struct ImmersiveLayerChoice: Equatable, Sendable {
    let foveation: Bool
    let layout: ImmersiveLayout

    /// DEV-909: foveation is used only with `.dedicated`.
    ///
    /// The render loop draws each eye in its own pass with `renderTargetArrayLength = 1`.
    /// Metal rasterizes such a pass with LAYER 0 of the bound rate map. Under `.layered`
    /// the compositor supplies one map whose layer 1 belongs to the right eye, so the
    /// right eye would be rasterized with the left eye's foveation. Under `.dedicated`
    /// each eye has its own texture and its own single-layer map, and per-eye passes
    /// are correct. When `.dedicated` is not offered with foveation, foveation is
    /// turned off rather than warp one eye with the other eye's map.
    ///
    /// - Parameters:
    ///   - supportsFoveation: `capabilities.supportsFoveation`.
    ///   - foveatedLayouts: the layouts offered with `.foveationEnabled`.
    ///   - plainLayouts: the layouts offered with no options.
    static func choose(
        supportsFoveation: Bool,
        foveatedLayouts: [ImmersiveLayout],
        plainLayouts: [ImmersiveLayout]
    ) -> ImmersiveLayerChoice {
        if supportsFoveation && foveatedLayouts.contains(.dedicated) {
            return ImmersiveLayerChoice(foveation: true, layout: .dedicated)
        }
        return ImmersiveLayerChoice(
            foveation: false,
            layout: plainLayouts.contains(.layered) ? .layered : .dedicated
        )
    }
}

/// Which of a drawable's rasterization rate maps one eye's pass may bind (DEV-909).
nonisolated enum ImmersiveRateMapSelection {
    /// The compositor supplies one rate map per TEXTURE, not per view. A pass with
    /// `renderTargetArrayLength = 1` is rasterized with layer 0 of the bound map, so a
    /// map is correct for the pass only when the pass draws texture `textureIndex` at
    /// slice 0 (or with no slice). Any other case returns `nil`: no fallback to map 0,
    /// which is the left eye's foveation.
    ///
    /// - Returns: the index into `drawable.rasterizationRateMaps`, or `nil` to bind none.
    static func index(textureIndex: Int, slice: Int?, rateMapCount: Int) -> Int? {
        guard textureIndex >= 0, textureIndex < rateMapCount else { return nil }
        guard (slice ?? 0) == 0 else { return nil }
        return textureIndex
    }
}
```

### 2. `ElectricSheep/ImmersiveRenderLoop.swift` (modified: two edits, nothing else)

**Edit A: `HallucinationCompositorConfig.makeConfiguration` (lines 17–34 on `main`).**
On `main` it reads:

```swift
struct HallucinationCompositorConfig: CompositorLayerConfiguration {
    func makeConfiguration(
        capabilities: LayerRenderer.Capabilities,
        configuration: inout LayerRenderer.Configuration
    ) {
        configuration.depthFormat = .depth32Float
        configuration.colorFormat = .rgba16Float
        let foveation = capabilities.supportsFoveation
        configuration.isFoveationEnabled = foveation

        // The valid layouts depend on whether foveation is on, so ask under the same
        // terms the layer is then built with (DEV-591).
        var options = LayerRenderer.Capabilities.SupportedLayoutsOptions()
        if foveation { options.insert(.foveationEnabled) }
        let layouts = capabilities.supportedLayouts(options: options)
        configuration.layout = layouts.contains(.layered) ? .layered : .dedicated
    }
}
```

Replace that whole struct with:

```swift
struct HallucinationCompositorConfig: CompositorLayerConfiguration {
    func makeConfiguration(
        capabilities: LayerRenderer.Capabilities,
        configuration: inout LayerRenderer.Configuration
    ) {
        configuration.depthFormat = .depth32Float
        configuration.colorFormat = .rgba16Float

        // DEV-909: foveation only with `.dedicated`. Each eye is drawn in its own
        // single-layer pass, which Metal rasterizes with layer 0 of the bound rate
        // map; under `.layered` that is the left eye's map for both eyes. The valid
        // layouts depend on whether foveation is on, so both sets are asked for
        // (DEV-591), and `ImmersiveLayerChoice` decides.
        let supportsFoveation = capabilities.supportsFoveation
        let choice = ImmersiveLayerChoice.choose(
            supportsFoveation: supportsFoveation,
            foveatedLayouts: supportsFoveation
                ? Self.mirror(capabilities.supportedLayouts(options: .foveationEnabled))
                : [],
            plainLayouts: Self.mirror(capabilities.supportedLayouts(options: []))
        )
        configuration.isFoveationEnabled = choice.foveation
        configuration.layout = choice.layout == .layered ? .layered : .dedicated
    }

    /// The layouts this app renders, as the platform-neutral mirror. `.shared` is dropped.
    private static func mirror(_ layouts: [LayerRenderer.Layout]) -> [ImmersiveLayout] {
        var result: [ImmersiveLayout] = []
        if layouts.contains(.dedicated) { result.append(.dedicated) }
        if layouts.contains(.layered) { result.append(.layered) }
        return result
    }
}
```

**Edit B: the rate-map selection in `encode(...)` (lines 152–164 on `main`).** On `main`
it reads:

```swift
                // DEV-591: bind the compositor's rate map, per view with a fallback to
                // the first — correct whether the layered layout reports one map or one
                // per view. Behaviour is confirmed on device, not here.
                let rateMap: (any MTLRasterizationRateMap)? = rateMaps.isEmpty
                    ? nil
                    : (viewIndex < rateMaps.count ? rateMaps[viewIndex] : rateMaps[0])

                let renderPassDesc = ImmersivePass.descriptor(
                    color: drawable.colorTextures[textureIndex],
                    depth: drawable.depthTextures[textureIndex],
                    slice: layered ? viewIndex : nil,
                    rateMap: rateMap
                )
```

Replace exactly those lines with:

```swift
                // DEV-591 / DEV-909: bind the compositor's rate map for THIS texture,
                // and only when this single-layer pass is rasterized with that map's
                // own layer 0. No fallback to map 0: under `.layered` that is the left
                // eye's foveation, and the right eye would be warped with it.
                let slice: Int? = layered ? viewIndex : nil
                let rateMap: (any MTLRasterizationRateMap)? = ImmersiveRateMapSelection
                    .index(textureIndex: textureIndex, slice: slice, rateMapCount: rateMaps.count)
                    .map { rateMaps[$0] }

                let renderPassDesc = ImmersivePass.descriptor(
                    color: drawable.colorTextures[textureIndex],
                    depth: drawable.depthTextures[textureIndex],
                    slice: slice,
                    rateMap: rateMap
                )
```

Leave every other line of `ImmersiveRenderLoop.swift` unchanged. That includes the
`#if os(visionOS)` wrapper, `runLoop()`, and the `textureIndex` and `layered` lines just
above Edit B. The local `let rateMaps = drawable.rasterizationRateMaps` (line 139) stays,
because Edit B uses it.

### 3. `ElectricSheepTests/ImmersiveRateMapLayoutTests.swift` (new)

Write this file exactly:

```swift
import Testing
@testable import ElectricSheep

/// DEV-909: no eye is rasterized with another eye's foveation map.
///
/// On the Vision Pro the layered layout supplied ONE rate map with two layers, and the
/// render loop bound it to both eyes' single-layer passes, so the right eye was
/// rasterized with layer 0, the left eye's warp. These tests pin the two decisions
/// that prevent it: foveation only with `.dedicated`, and no map-0 fallback.
@Suite("Rate map per eye (DEV-909)")
@MainActor
struct ImmersiveRateMapLayoutTests {

    // MARK: - Layer configuration

    @Test("Foveation with both layouts offered picks dedicated, never layered")
    func foveationPrefersDedicated() {
        let choice = ImmersiveLayerChoice.choose(
            supportsFoveation: true,
            foveatedLayouts: [.layered, .dedicated],
            plainLayouts: [.layered, .dedicated]
        )
        #expect(choice == ImmersiveLayerChoice(foveation: true, layout: .dedicated))
    }

    @Test("Foveation without dedicated turns foveation off instead of going layered")
    func foveationWithoutDedicatedIsDropped() {
        let choice = ImmersiveLayerChoice.choose(
            supportsFoveation: true,
            foveatedLayouts: [.layered],
            plainLayouts: [.layered, .dedicated]
        )
        #expect(choice == ImmersiveLayerChoice(foveation: false, layout: .layered))
    }

    @Test("No foveation keeps the layered layout when it is offered")
    func noFoveationKeepsLayered() {
        let choice = ImmersiveLayerChoice.choose(
            supportsFoveation: false,
            foveatedLayouts: [],
            plainLayouts: [.layered, .dedicated]
        )
        #expect(choice == ImmersiveLayerChoice(foveation: false, layout: .layered))
    }

    @Test("No foveation and no layered falls back to dedicated")
    func noFoveationNoLayered() {
        let choice = ImmersiveLayerChoice.choose(
            supportsFoveation: false,
            foveatedLayouts: [],
            plainLayouts: [.dedicated]
        )
        #expect(choice == ImmersiveLayerChoice(foveation: false, layout: .dedicated))
    }

    @Test("Whenever foveation is on, the layout is dedicated")
    func foveationImpliesDedicated() {
        let all: [[ImmersiveLayout]] = [[], [.layered], [.dedicated], [.layered, .dedicated], [.dedicated, .layered]]
        for supports in [false, true] {
            for foveated in all {
                for plain in all {
                    let choice = ImmersiveLayerChoice.choose(
                        supportsFoveation: supports,
                        foveatedLayouts: foveated,
                        plainLayouts: plain
                    )
                    if choice.foveation {
                        #expect(choice.layout == .dedicated)
                        #expect(supports)
                        #expect(foveated.contains(.dedicated))
                    }
                }
            }
        }
    }

    // MARK: - Rate map per pass

    @Test("Dedicated: each eye binds the map of its own texture")
    func dedicatedBindsOwnMap() {
        #expect(ImmersiveRateMapSelection.index(textureIndex: 0, slice: nil, rateMapCount: 2) == 0)
        #expect(ImmersiveRateMapSelection.index(textureIndex: 1, slice: nil, rateMapCount: 2) == 1)
    }

    @Test("Layered, one two-layer map: the right eye's slice binds no map (the DEV-909 case)")
    func layeredRightEyeBindsNothing() {
        #expect(ImmersiveRateMapSelection.index(textureIndex: 0, slice: 0, rateMapCount: 1) == 0)
        #expect(ImmersiveRateMapSelection.index(textureIndex: 0, slice: 1, rateMapCount: 1) == nil)
    }

    @Test("No fallback to map 0 for a texture that has no map of its own")
    func noFallbackToMapZero() {
        #expect(ImmersiveRateMapSelection.index(textureIndex: 1, slice: nil, rateMapCount: 1) == nil)
    }

    @Test("No maps, nothing bound")
    func noMaps() {
        #expect(ImmersiveRateMapSelection.index(textureIndex: 0, slice: nil, rateMapCount: 0) == nil)
        #expect(ImmersiveRateMapSelection.index(textureIndex: 0, slice: 0, rateMapCount: 0) == nil)
    }
}
```

## Change surface

`ElectricSheep/ImmersiveRenderLoop.swift` was verified to exist at `main`.
`ElectricSheep/ImmersiveRateMapLayout.swift` and
`ElectricSheepTests/ImmersiveRateMapLayoutTests.swift` were verified NOT to exist, and no
type named `ImmersiveLayout`, `ImmersiveLayerChoice` or `ImmersiveRateMapSelection` exists
anywhere at `main`. Both folders are synchronized root groups, so a new file in them needs
no project edit. The reference run compiled both new files with no `project.pbxproj`
change. The plan's implement phase lists exactly these three files as outputs.

| Path | Change |
| --- | --- |
| `ElectricSheep/ImmersiveRateMapLayout.swift` | new (transcribe as given) |
| `ElectricSheep/ImmersiveRenderLoop.swift` | modified (Edits A and B only, as given) |
| `ElectricSheepTests/ImmersiveRateMapLayoutTests.swift` | new (transcribe as given) |

The protected files below are served read-only. Do not edit them.

## Acceptance criteria

The tests are the gate. On `main`, the new test file does not compile, because the
symbols are missing. With the fix, all of its tests pass. The negative control above shows
that 5 of them fail if `main`'s logic is put back.

1. **Foveation only with `.dedicated`.** `foveationPrefersDedicated`,
   `foveationWithoutDedicatedIsDropped` and `foveationImpliesDedicated` pass.
2. **Non-foveated layouts unchanged.** `noFoveationKeepsLayered` and
   `noFoveationNoLayered` pass. Without foveation, the choice matches what `main` picks.
3. **No eye binds another eye's map.** `dedicatedBindsOwnMap`,
   `layeredRightEyeBindsNothing`, `noFallbackToMapZero` and `noMaps` pass.
4. **Existing suites unchanged.** `ImmersiveFrameDriverTests`, `ImmersiveRateMapDrawTests`,
   `ImmersiveRateMapGeometryTests` and `AtmosphereRenderTests` pass unmodified.
5. **Whole suite green on macOS.** `xcodebuild test` over `ElectricSheepTests` passes. The
   opt-in `LiveGenerationTests` is skipped, as on `main`.
6. **visionOS compiles.** The device leg builds the app for visionOS, which is the only
   build that compiles `ImmersiveRenderLoop.swift`. It must build. If the device is
   unavailable, the reviewer reports that the visionOS compile was not checked.

### Device check (manual, after merge; not a pipeline gate)

In the immersive space on the Vision Pro, check that the periphery is equally sharp in
both eyes (close one eye, then the other). If foveation was turned off because `.dedicated`
was not offered with foveation, the GPU frame time goes up. That is a known trade-off of
this slice, not a bug in it.

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
  - ElectricSheep/ImmersiveFrameDriver.swift
  - ElectricSheep/HalluRenderer.swift
  - ElectricSheep/Shaders.metal
  - ElectricSheepTests/ImmersiveFrameDriverTests.swift
  - ElectricSheepTests/ImmersiveRateMapDrawTests.swift
  - ElectricSheepTests/ImmersiveRateMapGeometryTests.swift
  - ElectricSheep.xcodeproj/project.pbxproj
```

## Constraints

- `ElectricSheep/ImmersiveRateMapLayout.swift` has **no** `#if os(...)` and no imports. It
  must compile on macOS, because the tests run there.
- Keep `nonisolated` on the three new types. Do not add `@MainActor` to them.
- Do not touch `ImmersiveFrameDriver.swift`, including `ImmersivePass`. DEV-908 is
  changing that file. The new types go in the new file for that reason.
- Do not change `ImmersivePass.descriptor`. `renderTargetArrayLength` stays 1.
- No shader or `HalluRenderer` changes. Vertex amplification is out of scope.
- `ImmersiveRenderLoop.swift` is transcribed, not redesigned. Keep its `#if os(visionOS)`
  wrapper and every line outside Edits A and B.
- Use Swift Testing (`import Testing`), not XCTest.

## Risks

- **An edit no macOS gate can see.** A typo in `ImmersiveRenderLoop.swift` passes the
  macOS run. That is why its code is given verbatim, and why the spec sets
  `device_destination`: the device leg builds for visionOS. The reference run confirmed
  that the verbatim code compiles for `generic/platform=visionOS`.
- **The device leg may fail for a reason that is not this spec.** DEV-910 reports a Metal
  validation abort in `ImmersiveRateMapDrawTests` on the Vision Pro at `2a557bf`. This
  spec does not touch that test or `HalluRenderer`. If the device leg fails there, it is
  DEV-910, not a regression from this change.
- **`.dedicated` may not be offered with foveation on the device.** The device diagnostic
  logged only the chosen layout. If `.dedicated` is not offered, this change turns
  foveation off. Every eye is then correct, but the GPU does more work. The manual device
  check above shows which case applies. If foveation does turn off, vertex amplification
  (the part left out) is the way to get it back.
- **`supportedLayouts(options: [])`** uses the `OptionSet` empty literal. It compiled
  against the XROS 27.0 SDK in the reference run. `.foveationEnabled` is the SDK's own
  static member.
- **Removing the `rateMaps.isEmpty` check.** `ImmersiveRateMapSelection.index` returns
  `nil` when `rateMapCount` is 0 (`noMaps` tests this). The `.map { rateMaps[$0] }` is
  never called with an out-of-range index.
