# Centipede renderer — slice 1: offscreen Metal renderer for a frame snapshot

Jira: DEV-765 (split from DEV-102, the Metal 4 renderer). This slice is deliberately
**Metal 3 API, offscreen, and independent of the logic core.** The MTL4 migration and the
`Game` → snapshot adapter are later slices.

## Context

Repo: `centipede`, a SwiftPM package. Every fact below was read from `main` (`9ea3306`)
before this spec was written; do not re-derive them.

- `Package.swift` is `swift-tools-version:6.0`, `platforms: [.macOS(.v15)]`, with exactly two
  targets: `.target(name: "CentipedeCore")` and
  `.testTarget(name: "CentipedeCoreTests", dependencies: ["CentipedeCore"])`. **Swift 6
  language mode is therefore in force**: every concurrency-isolation slip is an error, not a
  warning.
- `Sources/CentipedeCore/` is the headless logic core (slices 1–9, complete). It is
  renderer-free and stays that way. `Game`, `World`, `CentipedeChain`, `Mushroom` are
  `internal`; only `Field` and `GameState` are `public`. **This slice does not import
  CentipedeCore and does not change its access control.**
- Existing tests use Swift Testing (`import Testing`, `@Test`, `#expect`, `#require`) in
  `Tests/CentipedeCoreTests/GameStateTests.swift` and XCTest in `GameTests.swift`. New tests
  here use **Swift Testing**.
- There is no `Sources/CentipedeRender/` directory and no `.metal` file anywhere in the
  package. Nothing imports `Metal`.
- The Mac runner tests this package with `swift test` on the host (no VM). The host has a
  real Metal GPU; there is no window server available to tests.

## Goal

A library target `CentipedeRender` that turns a plain, renderer-owned description of one
frame (`FrameSnapshot`) into a rendered image, entirely offscreen, and lets a caller read
the pixels back. Correctness is proven by rendering known snapshots and asserting exact
pixel values at known positions.

## The isolation and API rules — read before writing a line

1. **No `MTKView`, no `MetalKit`, no `AppKit`, no `SwiftUI`, no `@MainActor`, no `async`.**
   Everything is synchronous, offscreen Metal: `MTLDevice` → `MTLCommandQueue` →
   `MTLRenderPassDescriptor` targeting an `MTLTexture` → `commit()` → `waitUntilCompleted()`
   → `getBytes`. `waitUntilCompleted()` is correct here (this is a synchronous offscreen
   renderer, not a display loop).
2. **Shaders are compiled from a Swift string at runtime**:
   `device.makeLibrary(source: OffscreenRenderer.shaderSource, options: nil)`. Do NOT add a
   `.metal` file, a resource bundle, or `Bundle.module`. This removes every build-system
   question about Metal sources in SwiftPM.
3. **Swift 6 mode, no shared mutable state.** `OffscreenRenderer` is a `final class` holding
   the device, queue and pipeline state; it is created and used within one test and is not
   `Sendable` (do not mark it so — Metal objects are not `Sendable`). `FrameSnapshot`,
   `CellKind`, `GridPosition`, `RenderedFrame` are value types and `Sendable`. No
   `static var`, no globals.
4. **Static members referenced inside instance methods MUST be qualified:** write
   `Self.shaderSource`, `OffscreenRenderer.cellSize`, `Palette.background`. The Swift
   diagnostic `static member 'X' cannot be used on instance of type 'T'` means ADD the
   qualifier on that line; it never means remove qualifiers elsewhere.
5. Pixel format is `.bgra8Unorm`, texture `storageMode: .shared`, `usage: [.renderTarget]`.
   Readback is `texture.getBytes(_:bytesPerRow:from:mipmapLevel:)` with
   `bytesPerRow = width * 4`. Bytes are in **B, G, R, A** order per pixel.

## Required change

### `Package.swift` (modified)

Add `.target(name: "CentipedeRender")` (no dependencies) and
`.testTarget(name: "CentipedeRenderTests", dependencies: ["CentipedeRender"])`. Leave the
two existing targets, the tools version and the platform line exactly as they are.

### `Sources/CentipedeRender/FrameSnapshot.swift` (new)

```swift
public struct GridPosition: Hashable, Sendable {
    public var column: Int
    public var row: Int
    public init(column: Int, row: Int)
}

public enum CellKind: Equatable, Sendable {
    case mushroom(damage: Int)      // 0...3
    case segment(isHead: Bool)
    case player
    case spider
    case flea
    case scorpion
}

/// What one frame shows. Renderer-owned; the logic core is adapted to it in a later slice.
public struct FrameSnapshot: Sendable {
    public var columns: Int
    public var rows: Int
    public var cells: [GridPosition: CellKind]
    public init(columns: Int = 30, rows: Int = 30, cells: [GridPosition: CellKind] = [:])
}
```

### `Sources/CentipedeRender/OffscreenRenderer.swift` (new)

```swift
public struct RGBA: Equatable, Sendable { public var r, g, b, a: UInt8 }

public enum Palette {
    public static let background = RGBA(r: 0,   g: 0,   b: 0,   a: 255)
    public static let mushroom: [RGBA]   // index = damage 0...3; four DISTINCT values
    public static let head     = RGBA(r: 255, g: 255, b: 0,   a: 255)
    public static let body     = RGBA(r: 255, g: 0,   b: 0,   a: 255)
    public static let player   = RGBA(r: 255, g: 255, b: 255, a: 255)
    public static let spider   = RGBA(r: 255, g: 0,   b: 255, a: 255)
    public static let flea     = RGBA(r: 0,   g: 255, b: 255, a: 255)
    public static let scorpion = RGBA(r: 255, g: 128, b: 0,   a: 255)
    public static func color(for kind: CellKind) -> RGBA
}

public struct RenderedFrame: Sendable {
    public let width: Int
    public let height: Int
    public let bgra: [UInt8]                          // width * height * 4
    public func pixel(x: Int, y: Int) -> RGBA         // y = 0 is the TOP row
}

public final class OffscreenRenderer {
    public static let shaderSource: String            // the MSL, as a Swift string literal
    public init?(device: MTLDevice? = nil)            // nil when no device / library / pipeline
    public func render(_ snapshot: FrameSnapshot, width: Int, height: Int) -> RenderedFrame?
}
```

Semantics:

- The frame is `width × height` pixels. Cell `(c, r)` covers the pixel rectangle
  `x ∈ [c * width / columns, (c + 1) * width / columns)`,
  `y ∈ [r * height / rows, (r + 1) * height / rows)`, with **row 0 at the top** of the image
  (Metal's clip-space y is up, so the vertex shader or the coordinate mapping must flip; the
  test reads `pixel(x:y:)` with y = 0 at the top and the spec's contract is in those terms).
- Every pixel of an occupied cell's rectangle is exactly `Palette.color(for:)` of its kind;
  every pixel of an empty cell is exactly `Palette.background`. Use a clear colour equal to
  the background and draw each occupied cell as two triangles (six vertices) with the
  colour carried as a vertex attribute; the fragment shader returns that colour unchanged.
  No blending, no anti-aliasing, no MSAA, no texture sampling.
- Rendering is deterministic: the same snapshot rendered twice yields identical bytes.

### `Tests/CentipedeRenderTests/OffscreenRendererTests.swift` (new)

Swift Testing. Each test creates its own `OffscreenRenderer` via
`let renderer = try #require(OffscreenRenderer())`. All tests render at
`width: 300, height: 300` with the default 30 × 30 snapshot, so every cell is a 10 × 10 pixel
square and cell `(c, r)`'s centre pixel is `(c * 10 + 5, r * 10 + 5)`.

## Change surface

Repo-relative paths. `Package.swift` was verified to exist at `main`; the other three were
verified NOT to exist. The plan's implement phase must list all four as outputs and
`Package.swift` as an input.

| Path | Change |
| --- | --- |
| `Package.swift` | modified |
| `Sources/CentipedeRender/FrameSnapshot.swift` | new |
| `Sources/CentipedeRender/OffscreenRenderer.swift` | new |
| `Tests/CentipedeRenderTests/OffscreenRendererTests.swift` | new |

Do not touch `Sources/CentipedeCore/` or `Tests/CentipedeCoreTests/`.

## Acceptance criteria

A green build is NOT sufficient — the tests below are the gate. None of them can pass on
`main`: the target does not exist there, so the test file does not compile. Say so in the
report.

1. `swift build` succeeds for the whole package with no new warnings, and the existing
   `CentipedeCoreTests` still pass unchanged.
2. **Empty frame.** Rendering `FrameSnapshot()` gives a `RenderedFrame` with `width == 300`,
   `height == 300`, `bgra.count == 360_000`, and the centre pixel of every one of the 900
   cells equals `Palette.background`.
3. **One cell, exact colour and extent.** A snapshot with a single `.mushroom(damage: 0)` at
   `GridPosition(column: 3, row: 4)`: pixels `(30, 40)`, `(39, 49)` and `(35, 45)` all equal
   `Palette.mushroom[0]`; pixels `(29, 45)`, `(40, 45)`, `(35, 39)` and `(35, 50)` all equal
   `Palette.background`.
4. **Row 0 is the top.** A single `.player` at `(0, 0)`: pixel `(5, 5)` is `Palette.player`
   and pixel `(5, 295)` is `Palette.background`. A single `.player` at `(0, 29)`: pixel
   `(5, 295)` is `Palette.player` and `(5, 5)` is background.
5. **Every kind has its colour.** One snapshot placing, in distinct cells, a head, a body
   segment, a player, a spider, a flea, a scorpion, and mushrooms at damage 0, 1, 2 and 3:
   each cell's centre pixel equals `Palette.color(for:)` of what was placed, and the four
   mushroom colours are pairwise distinct.
6. **Determinism.** The snapshot from criterion 5 rendered twice produces byte-identical
   `bgra` arrays.
7. No file under `Sources/CentipedeRender/` imports `MetalKit`, `AppKit` or `SwiftUI`, and
   `Sources/CentipedeCore/` and `Tests/CentipedeCoreTests/` are unmodified.

## test_strategy

    framework: swift_test
    required: true
    repo: centipede
    base_ref: main

## Constraints

- No new dependencies. No `.metal` files, no resources, no `Bundle.module`.
- No change to `CentipedeCore` or its tests. No change to the tools version or platforms.
- Metal 3 API only (`MTLDevice`, `MTLCommandQueue`, `MTLRenderPipelineDescriptor`,
  `MTLRenderCommandEncoder`); nothing from the `MTL4` family.

## Risks

- **Swift 6 isolation.** The one way this fails to compile is a non-`Sendable` Metal object
  crossing an isolation boundary — a global renderer, a `static var`, or a `@MainActor`
  annotation. Keep every Metal object inside an instance that one test owns.
- **Vertical flip.** Metal clip space has +y up; the contract here has row 0 at the top.
  Criterion 4 exists to catch a renderer that draws upside down.
- **Off-by-one at cell edges.** Criterion 3 samples both inside corners and all four
  outside neighbours of one cell. Integer pixel rectangles as defined above, with
  half-pixel-correct vertex positions, satisfy it; sloppy centre-plus-size maths does not.
- **Runtime shader compile failure.** `makeLibrary(source:options:)` throws on an MSL
  error; `init?` returns `nil` rather than crashing, which every test then reports through
  `#require`.
