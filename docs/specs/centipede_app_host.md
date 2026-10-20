# Centipede: a windowed app host that presents the Metal 4 frames

Jira: DEV-871 (scope item 2, the last one), epic DEV-442. Repo `centipede`, `main` =
`d9f7d1b` (run 66: `Metal4Renderer` and the README's Renderer section). Every fact below
was read from that commit. A reference implementation of exactly the files below ran on
the Mac runner before this spec was written (macOS 27.0.1, Swift 6.2, Apple M2 Ultra):
every new test passes and the whole package builds and passes, `CentipedeApp` included
(40 tests in 8 suites in `CentipedeRenderTests`, 12 in 4 suites in `CentipedeHostTests`,
24 in `CentipedeCoreTests`). Do not re-derive the expected values.

## Context

Run 66 left Centipede able to render a frame on the Metal 4 command model, offscreen only:
`Metal4Renderer.render(_:width:height:)` makes its own texture, draws into it, and reads
the bytes back. `GameSession` (DEV-827) turns one `PlayerInput` into the next frame.
Nothing opens a window, reads a keyboard, paces the game, or puts a frame on screen.

**The addition, in three layers so that what can be tested headless is.**

1. `CentipedeHost`, a new library target with no AppKit import, holds everything about the
   host that is logic: `HostKeyMap` (macOS virtual key codes to `MoveDirection` or fire),
   `InputAccumulator` (key events between ticks into one `PlayerInput` per tick),
   `TickClock` (how many ticks are due at a wall-clock time, at a fixed rate, with capped
   catch-up), and `HostLoop` (owns a `GameSession` and the two above; keys in, frames out).
   Twelve tests drive it with explicit key codes and times.
2. `Metal4Renderer` gains `render(_:into:presenting:)`: draw into a caller's `.bgra8Unorm`
   render target, and when a drawable is passed, wait for it on the `MTL4CommandQueue`
   before the pass and signal and present it after. `render(_:width:height:)` becomes a
   wrapper that allocates the texture and reads it back, so every run-66 test still holds.
   Two tests compare the into-texture path with the offscreen one byte for byte.
3. `CentipedeApp`, a new executable target: `NSApplication`, one window, one `MTKView`
   subclass that is its own delegate and first responder, key events forwarded to a
   `HostLoop`, and each `draw(in:)` advancing the loop to `CACurrentMediaTime()` and
   rendering into `currentDrawable`. `swift test` compiles it (measured: a type error in
   `main.swift` fails the run), so the shell is build-checked on every attempt; it cannot
   be run on the runner, which has no window server.

**Toolchain facts.** `Package.swift` stays at `.macOS(.v15)`; the Metal 4 and app code is
`@available(macOS 26.0, *)` (class level in the app, per-test in the test files, never on a
`@Suite`). `MTKView.init(frame:device:)` is non-failable, so the view is built by a static
`make(frame:)` that returns nil when there is no device or no Metal 4; a failable
`init?(frame:)` override does not compile (`failable initializer 'init(frame:)' cannot
override a non-failable initializer`, the second probe's error).

**Concurrency.** Swift tools 6.0, no default actor isolation. `HostLoop` and its parts are
plain structs; `InputAccumulator` and `TickClock` are `Sendable` and `Equatable`. The app
types are `final class`es on the main thread; add no actor annotations.

## Required change

### `Package.swift` (modified: whole file)

Replace the whole file with exactly this. It adds the host library, its tests and the
executable; the four existing targets are unchanged.

```swift
// swift-tools-version:6.0
import PackageDescription

let package = Package(
    name: "Centipede",
    platforms: [.macOS(.v15)],
    targets: [
        // All game logic lives here, renderer-free, so the suite runs headless
        // on the Mac runner via swift_test (DEV-102).
        .target(name: "CentipedeCore"),
        .testTarget(name: "CentipedeCoreTests", dependencies: ["CentipedeCore"]),
        .target(name: "CentipedeRender", dependencies: ["CentipedeCore"]),
        .testTarget(name: "CentipedeRenderTests", dependencies: ["CentipedeRender", "CentipedeCore"]),
        // The windowed host, split so its logic is tested headless (DEV-871):
        // CentipedeHost maps keys to input and paces the game; CentipedeApp is
        // the AppKit/MetalKit shell, compiled by `swift test` but not run by it.
        .target(name: "CentipedeHost", dependencies: ["CentipedeRender", "CentipedeCore"]),
        .testTarget(name: "CentipedeHostTests", dependencies: ["CentipedeHost", "CentipedeRender", "CentipedeCore"]),
        .executableTarget(name: "CentipedeApp", dependencies: ["CentipedeHost", "CentipedeRender", "CentipedeCore"]),
    ]
)
```

### `Sources/CentipedeRender/Metal4Renderer.swift` (modified: whole file)

Replace the whole file with exactly this. Relative to run 66: `render(_:into:presenting:)`
is new and holds the encoding; `render(_:width:height:)` allocates and reads back;
the empty-frame fast path and the unused `background(width:height:)` helper are gone,
because the clear already paints the background and the into-texture path must clear a
caller's texture too. Everything else, the shaders and vertex stream included, is as it was.

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
        guard render(snapshot, into: texture) else { return nil }
        var pixelData = [UInt8](repeating: 0, count: width * height * 4)
        texture.getBytes(&pixelData, bytesPerRow: width * 4,
                         from: MTLRegion(origin: MTLOrigin(x: 0, y: 0, z: 0),
                                         size: MTLSize(width: width, height: height, depth: 1)),
                         mipmapLevel: 0)
        return RenderedFrame(width: width, height: height, bgra: pixelData)
    }

    /// Draws the frame into `texture`, which must be a `.bgra8Unorm` render
    /// target. Returns once the GPU has finished, so the caller may read the
    /// texture back. With `drawable`, the texture is the drawable's own and the
    /// queue waits for it before the pass and signals and presents it after, which
    /// is how a window shows the frame (DEV-871).
    @discardableResult
    public func render(_ snapshot: FrameSnapshot, into texture: any MTLTexture,
                       presenting drawable: (any MTLDrawable)? = nil) -> Bool {
        let width = texture.width, height = texture.height
        let vertices = Self.vertices(for: snapshot, width: width, height: height)

        // Metal 4 does not track residency per command buffer: anything the GPU
        // touches by address must be in a committed residency set on the queue.
        guard let residency = try? device.makeResidencySet(descriptor: MTLResidencySetDescriptor()) else {
            return false
        }
        let vertexBuffer: (any MTLBuffer)?
        if vertices.isEmpty {
            vertexBuffer = nil
        } else {
            guard let buffer = device.makeBuffer(
                    bytes: vertices, length: vertices.count * MemoryLayout<Vertex>.stride,
                    options: .storageModeShared) else { return false }
            vertexBuffer = buffer
            residency.addAllocation(buffer)
        }
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

        guard let commandBuffer = device.makeCommandBuffer() else { return false }
        commandBuffer.beginCommandBuffer(allocator: allocator)
        guard let encoder = commandBuffer.makeRenderCommandEncoder(descriptor: pass) else {
            commandBuffer.endCommandBuffer()
            return false
        }
        if let vertexBuffer {
            argumentTable.setAddress(vertexBuffer.gpuAddress, index: 0)
            encoder.setRenderPipelineState(pipelineState)
            encoder.setArgumentTable(argumentTable, stages: .vertex)
            encoder.drawPrimitives(primitiveType: .triangle, vertexStart: 0, vertexCount: vertices.count)
        }
        encoder.endEncoding()
        commandBuffer.endCommandBuffer()

        completionValue += 1
        let target = completionValue
        if let drawable { commandQueue.waitForDrawable(drawable) }
        commandQueue.commit([commandBuffer])
        if let drawable {
            commandQueue.signalDrawable(drawable)
            drawable.present()
        }
        commandQueue.signalEvent(completion, value: target)
        return completion.wait(untilSignaledValue: target, timeoutMS: 5_000)
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
}
```

### `Sources/CentipedeHost/HostLoop.swift` (new — write exactly this)

```swift
import Foundation
import CentipedeCore
import CentipedeRender

/// macOS virtual key codes the host understands (DEV-871). Virtual key codes
/// name physical keys, so the mapping holds on any keyboard layout.
public enum HostKey: UInt16, Sendable, CaseIterable {
    case a = 0, s = 1, d = 2, w = 13
    case space = 49
    case leftArrow = 123, rightArrow = 124, downArrow = 125, upArrow = 126
}

/// Keys to game input: arrows or WASD move, space fires.
public enum HostKeyMap {
    public static func move(for keyCode: UInt16) -> MoveDirection? {
        switch HostKey(rawValue: keyCode) {
        case .leftArrow, .a: return .left
        case .rightArrow, .d: return .right
        case .upArrow, .w: return .up
        case .downArrow, .s: return .down
        default: return nil
        }
    }

    public static func isFire(_ keyCode: UInt16) -> Bool {
        HostKey(rawValue: keyCode) == .space
    }
}

/// Collects key events between ticks and hands out one `PlayerInput` per tick.
/// The most recently pressed direction still held wins; a fire press is
/// latched until the next tick takes it, so a tap between ticks is not lost.
public struct InputAccumulator: Sendable, Equatable {
    private var held: [UInt16] = []   // movement keys, in press order
    private var fireLatched = false

    public init() {}

    public mutating func keyDown(_ keyCode: UInt16) {
        if HostKeyMap.isFire(keyCode) {
            fireLatched = true
        } else if HostKeyMap.move(for: keyCode) != nil {
            held.removeAll { $0 == keyCode }
            held.append(keyCode)
        }
    }

    public mutating func keyUp(_ keyCode: UInt16) {
        held.removeAll { $0 == keyCode }
    }

    /// The input for one tick. Movement persists while a key is held; fire is
    /// consumed.
    public mutating func take() -> PlayerInput {
        let move = held.last.flatMap(HostKeyMap.move(for:))
        let fire = fireLatched
        fireLatched = false
        return PlayerInput(move: move, fire: fire)
    }
}

/// How many game ticks are due at a given wall-clock time, at a fixed rate.
/// The first call establishes the origin, so a window that opens late does
/// not owe a burst of ticks. The cap bounds catch-up after a stall.
public struct TickClock: Sendable, Equatable {
    public let interval: TimeInterval
    public let maxTicksPerCall: Int
    private var nextDue: TimeInterval?

    public init(ticksPerSecond: Double = 8, maxTicksPerCall: Int = 4) {
        precondition(ticksPerSecond > 0 && maxTicksPerCall > 0)
        self.interval = 1 / ticksPerSecond
        self.maxTicksPerCall = maxTicksPerCall
    }

    public mutating func ticksDue(at now: TimeInterval) -> Int {
        guard let due = nextDue else {
            nextDue = now + interval
            return 0
        }
        var ticks = 0
        var next = due
        while now >= next && ticks < maxTicksPerCall {
            ticks += 1
            next += interval
        }
        if ticks == maxTicksPerCall && now >= next {
            next = now + interval   // drop the backlog instead of replaying it
        }
        nextDue = next
        return ticks
    }
}

/// The host side of one game: keys in, frames out, at a fixed tick rate.
/// Owns a `GameSession`, an `InputAccumulator` and a `TickClock`; the app
/// forwards key events and calls `advance(to:)` once per display frame.
public struct HostLoop {
    public private(set) var session: GameSession
    public private(set) var input = InputAccumulator()
    public private(set) var clock: TickClock
    public private(set) var ticks = 0

    public init(seed: UInt64 = 42, ticksPerSecond: Double = 8) {
        self.session = GameSession(seed: seed)
        self.clock = TickClock(ticksPerSecond: ticksPerSecond)
    }

    public var frame: FrameSnapshot { session.frame }

    public mutating func keyDown(_ keyCode: UInt16) { input.keyDown(keyCode) }
    public mutating func keyUp(_ keyCode: UInt16) { input.keyUp(keyCode) }

    /// Steps the game once per due tick, taking the accumulated input on each,
    /// and returns the frame to draw.
    @discardableResult
    public mutating func advance(to now: TimeInterval) -> FrameSnapshot {
        let due = clock.ticksDue(at: now)
        for _ in 0..<due {
            _ = session.step(input.take())
            ticks += 1
        }
        return session.frame
    }
}
```

### `Tests/CentipedeHostTests/HostLoopTests.swift` (new — write exactly this)

```swift
import Testing
import CentipedeCore
import CentipedeRender
import CentipedeHost

@Suite struct HostKeyMapTests {
    @Test func arrowsAndWASDMoveSpaceFires() {
        #expect(HostKeyMap.move(for: HostKey.leftArrow.rawValue) == .left)
        #expect(HostKeyMap.move(for: HostKey.rightArrow.rawValue) == .right)
        #expect(HostKeyMap.move(for: HostKey.upArrow.rawValue) == .up)
        #expect(HostKeyMap.move(for: HostKey.downArrow.rawValue) == .down)
        #expect(HostKeyMap.move(for: HostKey.a.rawValue) == .left)
        #expect(HostKeyMap.move(for: HostKey.d.rawValue) == .right)
        #expect(HostKeyMap.move(for: HostKey.w.rawValue) == .up)
        #expect(HostKeyMap.move(for: HostKey.s.rawValue) == .down)
        #expect(HostKeyMap.isFire(HostKey.space.rawValue))
        #expect(!HostKeyMap.isFire(HostKey.leftArrow.rawValue))
        #expect(HostKeyMap.move(for: 36) == nil)   // return key: not a game key
        #expect(!HostKeyMap.isFire(36))
    }
}

@Suite struct InputAccumulatorTests {
    @Test func nothingHeldIsNoInput() {
        var acc = InputAccumulator()
        #expect(acc.take() == .none)
    }

    @Test func heldMovementPersistsAcrossTicks() {
        var acc = InputAccumulator()
        acc.keyDown(HostKey.leftArrow.rawValue)
        #expect(acc.take() == PlayerInput(move: .left))
        #expect(acc.take() == PlayerInput(move: .left))
        acc.keyUp(HostKey.leftArrow.rawValue)
        #expect(acc.take() == .none)
    }

    @Test func latestPressedDirectionWins() {
        var acc = InputAccumulator()
        acc.keyDown(HostKey.leftArrow.rawValue)
        acc.keyDown(HostKey.rightArrow.rawValue)
        #expect(acc.take() == PlayerInput(move: .right))
        acc.keyUp(HostKey.rightArrow.rawValue)
        #expect(acc.take() == PlayerInput(move: .left), "the still-held key takes over")
    }

    @Test func fireIsLatchedUntilTakenOnce() {
        var acc = InputAccumulator()
        acc.keyDown(HostKey.space.rawValue)
        acc.keyUp(HostKey.space.rawValue)      // released before the tick
        #expect(acc.take() == PlayerInput(fire: true))
        #expect(acc.take() == .none)
    }

    @Test func unknownKeysAreIgnored() {
        var acc = InputAccumulator()
        acc.keyDown(36)
        acc.keyUp(36)
        #expect(acc.take() == .none)
    }
}

@Suite struct TickClockTests {
    @Test func firstCallSetsTheOriginAndOwesNothing() {
        var clock = TickClock(ticksPerSecond: 10)
        #expect(clock.ticksDue(at: 100.0) == 0)
        #expect(clock.ticksDue(at: 100.05) == 0)
        #expect(clock.ticksDue(at: 100.1) == 1)
        #expect(clock.ticksDue(at: 100.1) == 0)
        #expect(clock.ticksDue(at: 100.35) == 2)
    }

    @Test func catchUpIsCappedAndTheBacklogDropped() {
        var clock = TickClock(ticksPerSecond: 10, maxTicksPerCall: 4)
        _ = clock.ticksDue(at: 0)
        #expect(clock.ticksDue(at: 5.0) == 4)      // 50 ticks behind: 4, not 50
        #expect(clock.ticksDue(at: 5.05) == 0)      // the backlog is gone
        #expect(clock.ticksDue(at: 5.1) == 1)
    }
}

@Suite struct HostLoopTests {
    @Test func aFreshLoopShowsTheStartingFrame() {
        var loop = HostLoop(seed: 42, ticksPerSecond: 8)
        let frame = loop.advance(to: 10.0)
        #expect(loop.ticks == 0)
        #expect(frame.cells[GridPosition(column: 15, row: 29)] == .player)
        #expect(frame.hud == HUD(score: 0, lives: 3, wave: 1, isGameOver: false))
    }

    @Test func holdingLeftMovesThePlayerOneCellPerTick() {
        var loop = HostLoop(seed: 42, ticksPerSecond: 8)
        loop.advance(to: 10.0)
        loop.keyDown(HostKey.leftArrow.rawValue)
        let frame = loop.advance(to: 10.0 + 3 * 0.125)
        #expect(loop.ticks == 3)
        #expect(frame.cells[GridPosition(column: 12, row: 29)] == .player)
        #expect(frame.cells[GridPosition(column: 15, row: 29)] != .player)
    }

    @Test func ticksMatchAGameSessionFedTheSameInputs() {
        var loop = HostLoop(seed: 7, ticksPerSecond: 8)
        var reference = GameSession(seed: 7)
        loop.advance(to: 0)
        loop.keyDown(HostKey.rightArrow.rawValue)
        loop.keyDown(HostKey.space.rawValue)
        loop.keyUp(HostKey.space.rawValue)
        _ = loop.advance(to: 2 * 0.125)          // tick 1: right + fire, tick 2: right
        _ = reference.step(PlayerInput(move: .right, fire: true))
        _ = reference.step(PlayerInput(move: .right))
        loop.keyUp(HostKey.rightArrow.rawValue)
        let frame = loop.advance(to: 3 * 0.125)   // tick 3: nothing held
        _ = reference.step(.none)
        #expect(loop.ticks == 3)
        #expect(frame.cells == reference.frame.cells)
        #expect(frame.hud == reference.frame.hud)
    }

    @Test func noTimePassedMeansNoTicks() {
        var loop = HostLoop(seed: 42, ticksPerSecond: 8)
        loop.advance(to: 1.0)
        loop.keyDown(HostKey.upArrow.rawValue)
        let before = loop.frame
        let after = loop.advance(to: 1.01)
        #expect(loop.ticks == 0)
        #expect(after.cells == before.cells)
    }
}
```

### `Tests/CentipedeRenderTests/Metal4RendererIntoTextureTests.swift` (new — write exactly this)

```swift
import Testing
import Metal

@testable import CentipedeRender

@Suite struct Metal4RendererIntoTextureTests {
    @available(macOS 26.0, *)
    @Test func renderingIntoACallerTextureMatchesTheOffscreenPath() throws {
        let renderer = try #require(Metal4Renderer())
        let device = try #require(MTLCreateSystemDefaultDevice())
        var snap = FrameSnapshot()
        for c in 0..<30 { snap.cells[GridPosition(column: c, row: (c * 11) % 30)] = .segment(isHead: c == 0) }
        snap.cells[GridPosition(column: 15, row: 29)] = .player
        let desc = MTLTextureDescriptor.texture2DDescriptor(
            pixelFormat: .bgra8Unorm, width: 300, height: 300, mipmapped: false)
        desc.usage = [.renderTarget]
        desc.storageMode = .shared
        let texture = try #require(device.makeTexture(descriptor: desc))
        #expect(renderer.render(snap, into: texture))
        var bytes = [UInt8](repeating: 0, count: 300 * 300 * 4)
        texture.getBytes(&bytes, bytesPerRow: 1200,
                         from: MTLRegion(origin: MTLOrigin(x: 0, y: 0, z: 0),
                                         size: MTLSize(width: 300, height: 300, depth: 1)),
                         mipmapLevel: 0)
        let reference = try #require(renderer.render(snap, width: 300, height: 300))
        #expect(bytes.elementsEqual(reference.bgra))
    }

    @available(macOS 26.0, *)
    @Test func anEmptyFrameClearsTheTextureToBackground() throws {
        let renderer = try #require(Metal4Renderer())
        let device = try #require(MTLCreateSystemDefaultDevice())
        let desc = MTLTextureDescriptor.texture2DDescriptor(
            pixelFormat: .bgra8Unorm, width: 60, height: 60, mipmapped: false)
        desc.usage = [.renderTarget]
        desc.storageMode = .shared
        let texture = try #require(device.makeTexture(descriptor: desc))
        #expect(renderer.render(FrameSnapshot(), into: texture))
        let frame = try #require(renderer.render(FrameSnapshot(), width: 60, height: 60))
        var bytes = [UInt8](repeating: 1, count: 60 * 60 * 4)
        texture.getBytes(&bytes, bytesPerRow: 240,
                         from: MTLRegion(origin: MTLOrigin(x: 0, y: 0, z: 0),
                                         size: MTLSize(width: 60, height: 60, depth: 1)),
                         mipmapLevel: 0)
        #expect(bytes.elementsEqual(frame.bgra))
        #expect(frame.pixel(x: 30, y: 30) == Palette.background)
    }
}
```

### `Sources/CentipedeApp/main.swift` (new — write exactly this)

```swift
import AppKit
import MetalKit
import QuartzCore
import CentipedeCore
import CentipedeRender
import CentipedeHost

/// The windowed host (DEV-871): one window, one `MTKView`, keys in, frames out.
/// Everything that can be tested headless lives in `CentipedeHost`; this file
/// is the AppKit and MetalKit shell around it.
@available(macOS 26.0, *)
final class GameView: MTKView, MTKViewDelegate {
    var loop = HostLoop(seed: UInt64(Date().timeIntervalSince1970), ticksPerSecond: 8)
    let renderer: Metal4Renderer

    /// nil when there is no Metal device or Metal 4 cannot be set up on it.
    static func make(frame: NSRect) -> GameView? {
        guard let device = MTLCreateSystemDefaultDevice(),
              let renderer = Metal4Renderer(device: device) else { return nil }
        return GameView(frame: frame, device: device, renderer: renderer)
    }

    init(frame: NSRect, device: any MTLDevice, renderer: Metal4Renderer) {
        self.renderer = renderer
        super.init(frame: frame, device: device)
        colorPixelFormat = .bgra8Unorm
        framebufferOnly = false
        preferredFramesPerSecond = 60
        delegate = self
    }

    @available(*, unavailable)
    required init(coder: NSCoder) { fatalError("not used") }

    override var acceptsFirstResponder: Bool { true }
    override func keyDown(with event: NSEvent) {
        if event.isARepeat { return }
        loop.keyDown(event.keyCode)
    }
    override func keyUp(with event: NSEvent) { loop.keyUp(event.keyCode) }

    func mtkView(_ view: MTKView, drawableSizeWillChange size: CGSize) {}

    func draw(in view: MTKView) {
        let frame = loop.advance(to: CACurrentMediaTime())
        guard let drawable = currentDrawable else { return }
        renderer.render(frame, into: drawable.texture, presenting: drawable)
    }
}

@available(macOS 26.0, *)
final class AppDelegate: NSObject, NSApplicationDelegate {
    var window: NSWindow?

    func applicationDidFinishLaunching(_ notification: Notification) {
        let size = NSRect(x: 0, y: 0, width: 600, height: 660)
        guard let view = GameView.make(frame: size) else {
            NSLog("Centipede: Metal 4 is not available on this Mac")
            NSApp.terminate(nil)
            return
        }
        let window = NSWindow(contentRect: size, styleMask: [.titled, .closable, .miniaturizable],
                              backing: .buffered, defer: false)
        window.title = "Centipede"
        window.contentView = view
        window.center()
        window.makeKeyAndOrderFront(nil)
        window.makeFirstResponder(view)
        self.window = window
        NSApp.activate(ignoringOtherApps: true)
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
}

if #available(macOS 26.0, *) {
    let app = NSApplication.shared
    app.setActivationPolicy(.regular)
    let delegate = AppDelegate()
    app.delegate = delegate
    app.run()
} else {
    FileHandle.standardError.write(Data("Centipede needs macOS 26 or later.\n".utf8))
    exit(1)
}
```

### `README.md` (modify — two anchored edits, nothing else)

**Edit 1.** Replace the `Metal4Renderer` bullet of the Renderer section, these five lines:

```
- **`Metal4Renderer`** — the same frame on the Metal 4 command model (DEV-871):
  `MTL4CommandQueue`, `MTL4CommandAllocator`, a pipeline from `MTL4Compiler`,
  the vertex buffer bound through an `MTL4ArgumentTable`, residency through an
  `MTLResidencySet`, completion on an `MTLSharedEvent`. Requires macOS 26; the
  tests compare it pixel for pixel with the Metal 3 renderer.
```

with these twelve:

```
- **`Metal4Renderer`** — the same frame on the Metal 4 command model (DEV-871):
  `MTL4CommandQueue`, `MTL4CommandAllocator`, a pipeline from `MTL4Compiler`,
  the vertex buffer bound through an `MTL4ArgumentTable`, residency through an
  `MTLResidencySet`, completion on an `MTLSharedEvent`. Requires macOS 26; the
  tests compare it pixel for pixel with the Metal 3 renderer. It draws into any
  `.bgra8Unorm` render target, and into a window's drawable through the queue's
  `waitForDrawable` / `signalDrawable` (`render(_:into:presenting:)`).
- **`CentipedeHost`** — the windowed host's logic, headless-tested: arrows or
  WASD move and space fires (`HostKeyMap`), the most recently pressed held key
  wins and a fire tap is latched until the next tick (`InputAccumulator`), a
  fixed 8 ticks/s clock that caps catch-up (`TickClock`), and `HostLoop`, which
  steps a `GameSession` once per due tick.
```

**Edit 2.** Replace the line

```
A windowed app host that presents these frames is not built yet.
```

with this section:

```
## Run the app

    swift run CentipedeApp

`CentipedeApp` is the AppKit and MetalKit shell around `CentipedeHost`: one
window, one `MTKView` at 60 fps, each frame drawn by `Metal4Renderer` straight
into the drawable. Arrows or WASD move, space fires. It needs macOS 26 or later;
`swift test` compiles it but cannot open a window, so the window itself is a
manual check.
```

## Change surface

`Sources/CentipedeHost/HostLoop.swift`, `Tests/CentipedeHostTests/HostLoopTests.swift`,
`Tests/CentipedeRenderTests/Metal4RendererIntoTextureTests.swift` and
`Sources/CentipedeApp/main.swift` were verified NOT to exist at `main`. `Package.swift`,
`Sources/CentipedeRender/Metal4Renderer.swift` and `README.md` exist and change as above.
The plan's implement phase lists exactly these seven as outputs. No other file changes.

| Path | Change |
| --- | --- |
| `Package.swift` | modify — whole file, 17 lines |
| `Sources/CentipedeRender/Metal4Renderer.swift` | modify — whole file, replaces run 66's |
| `Sources/CentipedeHost/HostLoop.swift` | new |
| `Tests/CentipedeHostTests/HostLoopTests.swift` | new |
| `Tests/CentipedeRenderTests/Metal4RendererIntoTextureTests.swift` | new |
| `Sources/CentipedeApp/main.swift` | new |
| `README.md` | modify — Edit 1 and Edit 2, as two anchored SEARCH/REPLACE blocks |

The four new files and the two whole-file replacements are emitted WHOLE. `README.md` is
emitted as anchored SEARCH/REPLACE blocks whose SEARCH text is the quoted anchor lines.
The protected files below are served read-only. Do not edit them.

## Acceptance criteria

A green build is NOT sufficient; the tests are the gate. Every criterion is decided by the
one `swift test` run of the patched tree.

1. **The twelve host tests pass**, by name: suite `HostKeyMapTests`
   (`arrowsAndWASDMoveSpaceFires`); suite `InputAccumulatorTests` (`nothingHeldIsNoInput`,
   `heldMovementPersistsAcrossTicks`, `latestPressedDirectionWins`,
   `fireIsLatchedUntilTakenOnce`, `unknownKeysAreIgnored`); suite `TickClockTests`
   (`firstCallSetsTheOriginAndOwesNothing`, `catchUpIsCappedAndTheBacklogDropped`); suite
   `HostLoopTests` (`aFreshLoopShowsTheStartingFrame`, `holdingLeftMovesThePlayerOneCellPerTick`,
   `ticksMatchAGameSessionFedTheSameInputs`, `noTimePassedMeansNoTicks`).
2. **Rendering into a caller's texture equals the offscreen path byte for byte**, and an
   empty frame clears a caller's texture to the background: suite
   `Metal4RendererIntoTextureTests` (`renderingIntoACallerTextureMatchesTheOffscreenPath`,
   `anEmptyFrameClearsTheTextureToBackground`).
3. **Holding left for three ticks moves the player three cells**: after `advance(to:)` at
   three tick intervals with the left arrow held, the player is at column 12 row 29 and
   not at column 15 row 29 (`holdingLeftMovesThePlayerOneCellPerTick`).
4. **The run-66 renderer tests still pass unchanged**: all seven `Metal4RendererTests`,
   and the five `OffscreenRendererTests`.
5. **The package builds with the executable**: the run's log reports `Build complete!`
   with `CentipedeApp` among the targets, and the whole suite passes: 40 tests in 8 suites
   in `CentipedeRenderTests`, 12 tests in 4 suites in `CentipedeHostTests`, 24 in
   `CentipedeCoreTests`.
6. **README updated.** `README.md` contains the line `## Run the app`, the line
   `    swift run CentipedeApp`, and the text `` `CentipedeHost` ``, and no longer contains
   `A windowed app host that presents these frames is not built yet.`

## Operator check after merge (not a pipeline criterion)

On the Studio: `swift run CentipedeApp` opens a 600×660 window titled Centipede showing
the starting board; arrows or WASD move the player, space fires, the game advances at
8 ticks per second; closing the window quits. The runner cannot do this, so it is yours.

## test_strategy

    framework: swift_test
    required: true
    repo: centipede
    base_ref: main
    protected_paths:
      - Sources/CentipedeRender/OffscreenRenderer.swift
      - Sources/CentipedeRender/FrameSnapshot.swift
      - Sources/CentipedeRender/BoardAdapter.swift
      - Sources/CentipedeRender/GameSession.swift
      - Sources/CentipedeCore/Game.swift
      - Tests/CentipedeRenderTests/Metal4RendererTests.swift
      - Tests/CentipedeRenderTests/OffscreenRendererTests.swift
      - Tests/CentipedeRenderTests/GameSessionTests.swift
      - Tests/CentipedeRenderTests/HUDFrameTests.swift
      - Tests/CentipedeRenderTests/PublicGameTests.swift
      - Tests/CentipedeRenderTests/BoardAdapterTests.swift
      - Tests/CentipedeRenderTests/ShotAndPoisonTests.swift

## Constraints

- No new dependencies. The platform stays `.macOS(.v15)`; Metal 4 and the app are
  `@available(macOS 26.0, *)`, never on a `@Suite`.
- `CentipedeHost` imports `Foundation`, `CentipedeCore` and `CentipedeRender` only: no
  AppKit, no Metal. Everything that needs a window lives in `CentipedeApp`.
- `OffscreenRenderer.swift` and every existing test file do not change. The seven
  `Metal4RendererTests` must pass against the replaced renderer unchanged.
- `GameView` is built through `make(frame:)`; no failable `init?(frame:)` override.
- README edits are the two above and nothing else.

## Risks

- **Failable `init?(frame:)` on the `MTKView` subclass** does not compile (measured). The
  static factory is the shape that does.
- **Empty frame through the into-texture path.** Without the clear-only command buffer an
  empty frame would leave a caller's texture untouched; `anEmptyFrameClearsTheTextureToBackground`
  pins it.
- **Drawable ordering.** `waitForDrawable` must precede `commit`, `signalDrawable` and
  `present` must follow it; the runner cannot test this, which is why it is the operator
  check and why the offscreen path keeps the shared-event wait.
- **Catch-up after a stall.** Without the cap a window hidden for a minute would replay
  480 ticks on return; `catchUpIsCappedAndTheBacklogDropped` pins four, then a fresh origin.
