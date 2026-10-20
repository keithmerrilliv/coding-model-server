# Centipede: an offscreen renderer on the Metal 4 command model

Jira: DEV-871 (scope items 1 and 4; the windowed app host, item 2, is a separate spec),
epic DEV-442. Repo `centipede`, `main` = `56e5308` (run 65, DEV-827's `GameSession`).
Every fact below was read from that commit. A reference implementation of exactly the
two files below ran on the Mac runner before this spec was written (macOS 27.0.1,
Swift 6.2, Apple M2 Ultra): all 7 new tests pass, and the whole suite passes with them
(38 tests in 7 suites in `CentipedeRenderTests`, 24 in `CentipedeCoreTests`). The new
test file alone, against unmodified `main`, fails to compile
(`cannot find 'Metal4Renderer' in scope`, 5 times). Do not re-derive the expected values.

## Context

`CentipedeRender` already draws a frame offscreen. `OffscreenRenderer` (Metal 3, DEV-765)
takes a `FrameSnapshot` and returns a `RenderedFrame` of BGRA bytes: one coloured quad
per occupied cell, row 0 at the top, `Palette.background` everywhere else, and
`RenderedFrame.pixel(x:y:)` reads one pixel back as an `RGBA`. The shaders are the string
`OffscreenRenderer.shaderSource`, compiled at runtime. The vertex layout is the internal
`struct Vertex` in the same file: 32 bytes, `x, y, r, g, b, a` and two floats of padding,
read in the vertex shader as eight floats per vertex from `buffer(0)`. `Palette` is public
and `Palette.color(for:)` maps every `CellKind`.

**The gap.** DEV-871 carries forward the Metal 4 requirement from DEV-102. Nothing in the
package uses the Metal 4 command model: `MTL4CommandQueue`, command buffers that begin
against an `MTL4CommandAllocator`, pipelines built by an `MTL4Compiler`, buffers bound by
GPU address through an `MTL4ArgumentTable`, residency declared with an `MTLResidencySet`,
and completion observed on an `MTLSharedEvent`.

**The addition.** One new class, `Metal4Renderer`, in a new file, with the same contract
as `OffscreenRenderer`: same inputs, same `RenderedFrame`, same palette, same shaders,
same vertex stream. `OffscreenRenderer` stays exactly as it is; the test that renders one
frame through both and compares the bytes is what makes the migration checkable. The app
host (a later spec) picks which renderer it presents.

**Toolchain facts, measured on the runner.** `Package.swift` declares `.macOS(.v15)` and
does not change. The Metal 4 API is `@available(macOS 26.0, *)`, so the class and every
test carry that annotation. The annotation must NOT go on the `@Suite` type: Swift
Testing rejects it (`Attribute 'Suite' cannot be applied to this structure because it has
been marked '@available(macOS 26.0, *)'`), which is exactly how the first probe failed.
It goes on each `@Test func` instead, where Swift Testing accepts it and would skip the
test on an older macOS.

**Concurrency.** Swift tools 6.0, no default actor isolation. `Metal4Renderer` is a
`final class` with no conformances and no actor annotations, like `OffscreenRenderer`.

## Required change

### `Sources/CentipedeRender/Metal4Renderer.swift` (new — write exactly this)

```swift
import Metal

/// Offscreen renderer on the Metal 4 command model (DEV-871).
///
/// Same contract as `OffscreenRenderer`: a `FrameSnapshot` in, a `RenderedFrame`
/// of BGRA bytes out, one coloured quad per occupied cell, row 0 at the top,
/// `Palette.background` everywhere else. What changes is how the GPU work is
/// described: an `MTL4CommandQueue`, command buffers that begin against an
/// `MTL4CommandAllocator`, a pipeline built by an `MTL4Compiler`, the vertex
/// buffer bound through an `MTL4ArgumentTable` by GPU address, resources made
/// resident through an `MTLResidencySet`, and completion observed on an
/// `MTLSharedEvent`. Shaders are `OffscreenRenderer.shaderSource`, unchanged.
@available(macOS 26.0, *)
public final class Metal4Renderer {
    private let device: any MTLDevice
    private let commandQueue: any MTL4CommandQueue
    private let allocator: any MTL4CommandAllocator
    private let pipelineState: any MTLRenderPipelineState
    private let argumentTable: any MTL4ArgumentTable
    private let completion: any MTLSharedEvent
    private var completionValue: UInt64 = 0

    public init?(device: (any MTLDevice)? = nil) {
        guard let dev = device ?? MTLCreateSystemDefaultDevice(),
              let queue = dev.makeMTL4CommandQueue(),
              let allocator = dev.makeCommandAllocator(),
              let event = dev.makeSharedEvent() else {
            return nil
        }
        self.device = dev
        self.commandQueue = queue
        self.allocator = allocator
        self.completion = event
        do {
            let library = try dev.makeLibrary(source: OffscreenRenderer.shaderSource, options: nil)
            let vertex = MTL4LibraryFunctionDescriptor()
            vertex.name = "vertexShader"
            vertex.library = library
            let fragment = MTL4LibraryFunctionDescriptor()
            fragment.name = "fragmentShader"
            fragment.library = library

            let pipeline = MTL4RenderPipelineDescriptor()
            pipeline.vertexFunctionDescriptor = vertex
            pipeline.fragmentFunctionDescriptor = fragment
            pipeline.colorAttachments[0].pixelFormat = .bgra8Unorm

            let compiler = try dev.makeCompiler(descriptor: MTL4CompilerDescriptor())
            self.pipelineState = try compiler.makeRenderPipelineState(descriptor: pipeline)

            let table = MTL4ArgumentTableDescriptor()
            table.maxBufferBindCount = 1
            self.argumentTable = try dev.makeArgumentTable(descriptor: table)
        } catch {
            return nil
        }
    }

    public func render(_ snapshot: FrameSnapshot, width: Int, height: Int) -> RenderedFrame? {
        let textureDesc = MTLTextureDescriptor.texture2DDescriptor(
            pixelFormat: .bgra8Unorm, width: width, height: height, mipmapped: false)
        textureDesc.usage = [.renderTarget]
        textureDesc.storageMode = .shared
        guard let texture = device.makeTexture(descriptor: textureDesc) else { return nil }

        let vertices = Self.vertices(for: snapshot, width: width, height: height)
        guard !vertices.isEmpty else {
            return RenderedFrame(width: width, height: height,
                                 bgra: Self.background(width: width, height: height))
        }
        guard let vertexBuffer = device.makeBuffer(
                bytes: vertices, length: vertices.count * MemoryLayout<Vertex>.stride,
                options: .storageModeShared) else { return nil }

        // Metal 4 does not track residency per command buffer: anything the GPU
        // touches by address must be in a committed residency set on the queue.
        guard let residency = try? device.makeResidencySet(descriptor: MTLResidencySetDescriptor()) else {
            return nil
        }
        residency.addAllocation(vertexBuffer)
        residency.addAllocation(texture)
        residency.commit()
        commandQueue.addResidencySet(residency)
        defer { commandQueue.removeResidencySet(residency) }

        let pass = MTL4RenderPassDescriptor()
        pass.colorAttachments[0].texture = texture
        pass.colorAttachments[0].loadAction = .clear
        pass.colorAttachments[0].storeAction = .store
        let bg = Palette.background
        pass.colorAttachments[0].clearColor = MTLClearColor(
            red: Double(bg.r) / 255.0, green: Double(bg.g) / 255.0,
            blue: Double(bg.b) / 255.0, alpha: Double(bg.a) / 255.0)

        guard let commandBuffer = device.makeCommandBuffer() else { return nil }
        commandBuffer.beginCommandBuffer(allocator: allocator)
        guard let encoder = commandBuffer.makeRenderCommandEncoder(descriptor: pass) else {
            commandBuffer.endCommandBuffer()
            return nil
        }
        argumentTable.setAddress(vertexBuffer.gpuAddress, index: 0)
        encoder.setRenderPipelineState(pipelineState)
        encoder.setArgumentTable(argumentTable, stages: .vertex)
        encoder.drawPrimitives(primitiveType: .triangle, vertexStart: 0, vertexCount: vertices.count)
        encoder.endEncoding()
        commandBuffer.endCommandBuffer()

        completionValue += 1
        let target = completionValue
        commandQueue.commit([commandBuffer])
        commandQueue.signalEvent(completion, value: target)
        guard completion.wait(untilSignaledValue: target, timeoutMS: 5_000) else { return nil }

        var pixelData = [UInt8](repeating: 0, count: width * height * 4)
        texture.getBytes(&pixelData, bytesPerRow: width * 4,
                         from: MTLRegion(origin: MTLOrigin(x: 0, y: 0, z: 0),
                                         size: MTLSize(width: width, height: height, depth: 1)),
                         mipmapLevel: 0)
        return RenderedFrame(width: width, height: height, bgra: pixelData)
    }

    // One quad (two triangles) per occupied cell, cells in column-then-row order
    // so the vertex stream, and therefore the frame, is deterministic.
    static func vertices(for snapshot: FrameSnapshot, width: Int, height: Int) -> [Vertex] {
        var vertices: [Vertex] = []
        let cellWidth = Float(width) / Float(snapshot.columns)
        let cellHeight = Float(height) / Float(snapshot.rows)
        let sorted = snapshot.cells.sorted {
            $0.key.column < $1.key.column
                || ($0.key.column == $1.key.column && $0.key.row < $1.key.row)
        }
        for (pos, kind) in sorted {
            let minX = Float(pos.column) * cellWidth
            let maxX = Float(pos.column + 1) * cellWidth
            let minY = Float(pos.row) * cellHeight
            let maxY = Float(pos.row + 1) * cellHeight
            let clipMinX = minX / Float(width) * 2.0 - 1.0
            let clipMaxX = maxX / Float(width) * 2.0 - 1.0
            let clipMinY = -(maxY / Float(height) * 2.0 - 1.0)
            let clipMaxY = -(minY / Float(height) * 2.0 - 1.0)
            let color = Palette.color(for: kind)
            vertices.append(Vertex(x: clipMinX, y: clipMaxY, color: color))
            vertices.append(Vertex(x: clipMaxX, y: clipMaxY, color: color))
            vertices.append(Vertex(x: clipMinX, y: clipMinY, color: color))
            vertices.append(Vertex(x: clipMinX, y: clipMinY, color: color))
            vertices.append(Vertex(x: clipMaxX, y: clipMaxY, color: color))
            vertices.append(Vertex(x: clipMaxX, y: clipMinY, color: color))
        }
        return vertices
    }

    static func background(width: Int, height: Int) -> [UInt8] {
        var buffer = [UInt8](repeating: 0, count: width * height * 4)
        let bg = Palette.background
        for i in stride(from: 0, to: buffer.count, by: 4) {
            buffer[i] = bg.b
            buffer[i + 1] = bg.g
            buffer[i + 2] = bg.r
            buffer[i + 3] = bg.a
        }
        return buffer
    }
}
```

### `Tests/CentipedeRenderTests/Metal4RendererTests.swift` (new — write exactly this)

```swift
import Testing
import Metal

@testable import CentipedeRender

@Suite struct Metal4RendererTests {
    @available(macOS 26.0, *)
    @Test func emptyFrameIsBackground() throws {
        let renderer = try #require(Metal4Renderer())
        let frame = try #require(renderer.render(FrameSnapshot(), width: 300, height: 300))
        #expect(frame.width == 300 && frame.height == 300)
        #expect(frame.bgra.count == 360_000)
        for c in 0..<30 {
            for r in 0..<30 {
                #expect(frame.pixel(x: c * 10 + 5, y: r * 10 + 5) == Palette.background)
            }
        }
    }

    @available(macOS 26.0, *)
    @Test func oneMushroomExactExtentAndColor() throws {
        let renderer = try #require(Metal4Renderer())
        var s = FrameSnapshot()
        s.cells[GridPosition(column: 3, row: 4)] = .mushroom(damage: 0)
        let f = try #require(renderer.render(s, width: 300, height: 300))
        let expected = Palette.mushroom[0]
        #expect(f.pixel(x: 30, y: 40) == expected)
        #expect(f.pixel(x: 39, y: 49) == expected)
        #expect(f.pixel(x: 35, y: 45) == expected)
        #expect(f.pixel(x: 29, y: 45) == Palette.background)
        #expect(f.pixel(x: 40, y: 45) == Palette.background)
        #expect(f.pixel(x: 35, y: 39) == Palette.background)
        #expect(f.pixel(x: 35, y: 50) == Palette.background)
    }

    @available(macOS 26.0, *)
    @Test func rowZeroIsTop() throws {
        let r = try #require(Metal4Renderer())
        var s1 = FrameSnapshot()
        s1.cells[GridPosition(column: 0, row: 0)] = .player
        let f1 = try #require(r.render(s1, width: 300, height: 300))
        #expect(f1.pixel(x: 5, y: 5) == Palette.player)
        #expect(f1.pixel(x: 5, y: 295) == Palette.background)
        var s2 = FrameSnapshot()
        s2.cells[GridPosition(column: 0, row: 29)] = .player
        let f2 = try #require(r.render(s2, width: 300, height: 300))
        #expect(f2.pixel(x: 5, y: 295) == Palette.player)
        #expect(f2.pixel(x: 5, y: 5) == Palette.background)
    }

    @available(macOS 26.0, *)
    @Test func everyKindHasItsColor() throws {
        let renderer = try #require(Metal4Renderer())
        var snap = FrameSnapshot()
        snap.cells[GridPosition(column: 0, row: 0)] = .segment(isHead: true)
        snap.cells[GridPosition(column: 1, row: 0)] = .segment(isHead: false)
        snap.cells[GridPosition(column: 2, row: 0)] = .player
        snap.cells[GridPosition(column: 3, row: 0)] = .spider
        snap.cells[GridPosition(column: 4, row: 0)] = .flea
        snap.cells[GridPosition(column: 5, row: 0)] = .scorpion
        snap.cells[GridPosition(column: 6, row: 0)] = .shot
        for d in 0..<4 {
            snap.cells[GridPosition(column: 7 + d, row: 0)] = .mushroom(damage: d)
            snap.cells[GridPosition(column: 11 + d, row: 0)] = .poisonedMushroom(damage: d)
        }
        let frame = try #require(renderer.render(snap, width: 300, height: 300))
        #expect(frame.pixel(x: 5, y: 5) == Palette.head)
        #expect(frame.pixel(x: 15, y: 5) == Palette.body)
        #expect(frame.pixel(x: 25, y: 5) == Palette.player)
        #expect(frame.pixel(x: 35, y: 5) == Palette.spider)
        #expect(frame.pixel(x: 45, y: 5) == Palette.flea)
        #expect(frame.pixel(x: 55, y: 5) == Palette.scorpion)
        #expect(frame.pixel(x: 65, y: 5) == Palette.shot)
        for d in 0..<4 {
            #expect(frame.pixel(x: 75 + d * 10, y: 5) == Palette.mushroom[d])
            #expect(frame.pixel(x: 115 + d * 10, y: 5) == Palette.poisonedMushroom[d])
        }
    }

    @available(macOS 26.0, *)
    @Test func twoFramesFromOneRendererAreIdentical() throws {
        let renderer = try #require(Metal4Renderer())
        var snap = FrameSnapshot()
        for c in 0..<30 { snap.cells[GridPosition(column: c, row: c)] = .mushroom(damage: c % 4) }
        snap.cells[GridPosition(column: 15, row: 29)] = .player
        let a = try #require(renderer.render(snap, width: 300, height: 300))
        let b = try #require(renderer.render(snap, width: 300, height: 300))
        #expect(a.bgra.elementsEqual(b.bgra))
    }

    @available(macOS 26.0, *)
    @Test func matchesTheMetal3RendererPixelForPixel() throws {
        let metal4 = try #require(Metal4Renderer())
        let metal3 = try #require(OffscreenRenderer())
        var snap = FrameSnapshot()
        for c in 0..<30 { snap.cells[GridPosition(column: c, row: (c * 7) % 30)] = .mushroom(damage: c % 4) }
        snap.cells[GridPosition(column: 15, row: 29)] = .player
        snap.cells[GridPosition(column: 15, row: 20)] = .shot
        snap.cells[GridPosition(column: 2, row: 1)] = .spider
        let a = try #require(metal4.render(snap, width: 300, height: 300))
        let b = try #require(metal3.render(snap, width: 300, height: 300))
        #expect(a.bgra.elementsEqual(b.bgra))
    }

    @available(macOS 26.0, *)
    @Test func nonSquareFrameScalesCells() throws {
        let renderer = try #require(Metal4Renderer())
        var s = FrameSnapshot()
        s.cells[GridPosition(column: 29, row: 0)] = .flea
        let f = try #require(renderer.render(s, width: 600, height: 300))
        #expect(f.pixel(x: 590, y: 5) == Palette.flea)
        #expect(f.pixel(x: 570, y: 5) == Palette.background)
        #expect(f.pixel(x: 590, y: 15) == Palette.background)
    }
}
```

### `README.md` (modify — two anchored edits, nothing else)

The README lists slices 1–7 only and says the renderer direction was dropped. Both edits
below are anchored on lines that occur exactly once in the file.

**Edit 1.** Replace these three lines of the opening paragraph:

```
**no Metal / AppKit / SpriteKit / Core Graphics**. The Mac runner tests it with
`swift_test`. (An earlier plan paired this with a Metal 4 renderer in a separate
app target; that direction is dropped — this repo is logic-only.)
```

with these four:

```
**no Metal / AppKit / SpriteKit / Core Graphics**. The Mac runner tests it with
`swift_test`. `Sources/CentipedeRender` draws it: a `FrameSnapshot` per tick, an
offscreen Metal 3 renderer and, since DEV-871, an offscreen renderer on the
Metal 4 command model, both pixel-tested headless.
```

**Edit 2.** Immediately before the line `## Test` (the last heading in the file), insert
this section, followed by one blank line:

```
## Renderer

`Sources/CentipedeRender`, built after the logic core, tested headless with
`swift test` like everything else:

- **`FrameSnapshot`** — the renderer-owned frame: a 30×30 grid of `CellKind`
  cells plus a `HUD` (score, lives, wave, game over). `BoardSnapshot` from the
  core maps onto it through `BoardAdapter` (run 53); shots and poisoned
  mushrooms have their own cell kinds (DEV-808).
- **`OffscreenRenderer`** — Metal 3 (DEV-765): one coloured quad per occupied
  cell, shaders compiled from a string at runtime, pixel-verified tests.
- **`Metal4Renderer`** — the same frame on the Metal 4 command model (DEV-871):
  `MTL4CommandQueue`, `MTL4CommandAllocator`, a pipeline from `MTL4Compiler`,
  the vertex buffer bound through an `MTL4ArgumentTable`, residency through an
  `MTLResidencySet`, completion on an `MTLSharedEvent`. Requires macOS 26; the
  tests compare it pixel for pixel with the Metal 3 renderer.
- **`Game` and `GameSession`** — the public game API (DEV-825), the HUD on every
  frame (DEV-826), and `GameSession`, which turns one tick of `PlayerInput`
  into the next frame (DEV-827).

A windowed app host that presents these frames is not built yet.
```

## Change surface

`Sources/CentipedeRender/Metal4Renderer.swift` and
`Tests/CentipedeRenderTests/Metal4RendererTests.swift` were verified NOT to exist at
`main`. `README.md` exists and gets exactly the two edits above. The plan's implement
phase lists exactly these three as outputs. No other file changes.

| Path | Change |
| --- | --- |
| `Sources/CentipedeRender/Metal4Renderer.swift` | new |
| `Tests/CentipedeRenderTests/Metal4RendererTests.swift` | new |
| `README.md` | modify — Edit 1 and Edit 2, as two anchored SEARCH/REPLACE blocks |

The two new files are emitted WHOLE. `README.md` is emitted as anchored SEARCH/REPLACE
blocks whose SEARCH text is the quoted anchor lines, never as a whole file. The protected
files below are served read-only. Do not edit them.

## Acceptance criteria

A green build is NOT sufficient; the tests are the gate. Every criterion is decided by
the one `swift test` run of the patched tree.

1. **The seven new tests pass**, by name, in suite `Metal4RendererTests`:
   `emptyFrameIsBackground`, `oneMushroomExactExtentAndColor`, `rowZeroIsTop`,
   `everyKindHasItsColor`, `twoFramesFromOneRendererAreIdentical`,
   `matchesTheMetal3RendererPixelForPixel`, `nonSquareFrameScalesCells`.
2. **A Metal 4 frame equals a Metal 3 frame byte for byte** for the frame in
   `matchesTheMetal3RendererPixelForPixel` (30 mushrooms on a diagonal pattern, the
   player at column 15 row 29, a shot at column 15 row 20, a spider at column 2 row 1,
   300×300).
3. **Every cell kind renders its palette colour**, including `.shot` and the four
   `.poisonedMushroom` damage levels, at the cell centres named in `everyKindHasItsColor`.
4. **An empty frame is all background** at every cell centre of a 300×300 render.
5. **Existing behaviour intact.** Every existing test file is unmodified, and the whole
   `swift test` suite passes: 38 tests in `CentipedeRenderTests` and 24 in
   `CentipedeCoreTests`.
6. **README updated.** `README.md` contains the line `## Renderer`, the line
   `A windowed app host that presents these frames is not built yet.`, and no longer
   contains the text `that direction is dropped`.

## test_strategy

    framework: swift_test
    required: true
    repo: centipede
    base_ref: main
    protected_paths:
      - Package.swift
      - Sources/CentipedeRender/OffscreenRenderer.swift
      - Sources/CentipedeRender/FrameSnapshot.swift
      - Sources/CentipedeRender/BoardAdapter.swift
      - Sources/CentipedeRender/GameSession.swift
      - Sources/CentipedeCore/Game.swift
      - Tests/CentipedeRenderTests/OffscreenRendererTests.swift
      - Tests/CentipedeRenderTests/GameSessionTests.swift
      - Tests/CentipedeRenderTests/HUDFrameTests.swift
      - Tests/CentipedeRenderTests/PublicGameTests.swift
      - Tests/CentipedeRenderTests/BoardAdapterTests.swift
      - Tests/CentipedeRenderTests/ShotAndPoisonTests.swift

## Constraints

- No new dependencies. `Package.swift` does not change: the platform stays `.macOS(.v15)`
  and availability is expressed with `@available(macOS 26.0, *)` on the class and on each
  test function.
- `OffscreenRenderer.swift` does not change. `Metal4Renderer` reuses
  `OffscreenRenderer.shaderSource`, `Palette`, `RGBA`, `RenderedFrame` and the internal
  `Vertex`; it declares none of its own.
- No `@available` on the `@Suite` type. No `#if canImport` guards.
- The README edits are the two above and nothing else; the slice list stays as it is.

## Risks

- **Availability on the suite.** The macro refuses it and the whole test target fails to
  compile. Criterion 1 cannot pass that way; the annotation belongs on each test.
- **Residency.** Metal 4 does not track residency per command buffer. Without the
  `MTLResidencySet` on the queue the vertex buffer and texture are not guaranteed
  resident and the frame is garbage or black. Criteria 2–4 catch it.
- **Completion.** Reading the texture before the shared event reaches the signalled value
  returns the cleared or stale texture. Criteria 2 and 3 catch it; the reference waits
  with a 5 s timeout and returns nil on expiry.
- **Vertex order.** The sort by column then row is what makes two renders, and the two
  renderers, byte-identical. Criteria 2 and the determinism test pin it.
