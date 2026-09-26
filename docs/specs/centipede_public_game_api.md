# Centipede: a public Game API that another module can drive

Jira: DEV-825, epic DEV-442. Repo `centipede`, `main` = `71de1f1`. Every fact below was
read from that commit. A reference implementation of exactly the edits below, with the
tests described, ran on the Mac runner before this spec was written: the suite passed,
with all 7 new tests passing. The new test file alone, against unmodified `main`, fails to
compile: `cannot find 'Game' in scope`, 16 times. Do not re-derive the expected values.

## Context

`Sources/CentipedeCore/Game.swift` declares the game with no access modifier, so it is
internal to `CentipedeCore`:

```swift
struct Game {
    private(set) var state: GameState
```

Its initialiser and every control are internal too:

```swift
    init(seed: UInt64) {
```

```swift
    mutating func movePlayer(_ direction: MoveDirection) {
```

```swift
    mutating func fire() {
```

```swift
    var isGameOver: Bool { state.lives <= 0 }
```

```swift
    mutating func tick() {
```

`Sources/CentipedeCore/MoveDirection.swift` is the whole enum:

```swift
enum MoveDirection {
    case up, down, left, right
}
```

and `Sources/CentipedeCore/BoardSnapshot.swift` produces the board from an internal
method:

```swift
    func boardSnapshot() -> BoardSnapshot {
```

`GameState`, `BoardSnapshot`, `BoardCell` and `BoardEntity` are already `public`.

**The defect.** No code outside `CentipedeCore` can create or drive a game. Every core test
uses `@testable import`, and the renderer's tests build `BoardSnapshot` values by hand. An
app has no way to play.

**The fix.** Make public exactly the surface needed to play: create a game, move, fire,
advance a tick, read the score state, ask whether the game is over, and take a board
snapshot. Everything else stays internal. In particular that means the second
initialiser (the test seam that takes a `World` and a `Position`), every entity type, and
every `...Snapshot` accessor.

**Concurrency.** The package uses Swift tools 6.0 and sets no default actor isolation.
Add no actor annotations. `MoveDirection` gains `Sendable`; nothing else changes
conformance.

## Required change

### `Sources/CentipedeCore/Game.swift` (modified — seven one-line edits)

Each edit adds `public` to one existing line and changes nothing else on it:

| today | becomes |
| --- | --- |
| `struct Game {` | `public struct Game {` |
| `    private(set) var state: GameState` | `    public private(set) var state: GameState` |
| `    init(seed: UInt64) {` | `    public init(seed: UInt64) {` |
| `    mutating func movePlayer(_ direction: MoveDirection) {` | `    public mutating func movePlayer(_ direction: MoveDirection) {` |
| `    mutating func fire() {` | `    public mutating func fire() {` |
| `    var isGameOver: Bool { state.lives <= 0 }` | `    public var isGameOver: Bool { state.lives <= 0 }` |
| `    mutating func tick() {` | `    public mutating func tick() {` |

That is seven lines, and each appears exactly once in the file. The other initialiser,
`init(world:gameState:playerPosition:spider:flea:scorpion:)`, stays internal: it takes
internal types, and making it public does not compile.

### `Sources/CentipedeCore/MoveDirection.swift` (modified — one edit)

Replace `enum MoveDirection {` with `public enum MoveDirection: Sendable {`. The `case`
line is unchanged.

### `Sources/CentipedeCore/BoardSnapshot.swift` (modified — one edit)

Replace `    func boardSnapshot() -> BoardSnapshot {` with
`    public func boardSnapshot() -> BoardSnapshot {`. Nothing else in the file changes.

### `Tests/CentipedeRenderTests/PublicGameTests.swift` (new)

swift-testing, mirroring `BoardAdapterTests.swift`. It imports with **plain** imports,
never `@testable`, because compiling without `@testable` is the point:

```swift
import Testing
import CentipedeCore
import CentipedeRender
```

`struct PublicGameTests`, with each test an `@Test func` (synchronous, not `async`, not
`throws`). Declare this private helper inside the struct, verbatim:

```swift
    private func cells(_ board: BoardSnapshot, _ entity: BoardEntity) -> [BoardCell] {
        board.entities.filter { $0.value == entity }.map(\.key)
    }
```

Every test builds its own game; no test relies on another.

## Change surface

The three modified files were verified to exist at `main`, and
`Tests/CentipedeRenderTests/PublicGameTests.swift` was verified NOT to exist. The plan's
implement phase lists exactly these four as outputs.

| Path | Change |
| --- | --- |
| `Sources/CentipedeCore/Game.swift` | modified (seven one-line edits) |
| `Sources/CentipedeCore/MoveDirection.swift` | modified (one edit) |
| `Sources/CentipedeCore/BoardSnapshot.swift` | modified (one edit) |
| `Tests/CentipedeRenderTests/PublicGameTests.swift` | new |

The protected files below are served read-only. Do not edit them.

## Acceptance criteria

A green build is NOT sufficient; the tests are the gate. On `main` the new test file does
not compile (`cannot find 'Game' in scope`), and the report must say so. The player starts
at column 15, row 29 (`Field.columns / 2`, the bottom row).

1. **A fresh game.** `let game = Game(seed: 42)`: `game.state.score == 0`,
   `game.state.lives == 3`, `game.state.wave == 1`, and `game.isGameOver` is false.
2. **The board.** `let board = Game(seed: 42).boardSnapshot()`:
   `cells(board, .player) == [BoardCell(column: 15, row: 29)]`, and
   `cells(board, .segment(isHead: true))` is not empty.
3. **Moving.** `var game = Game(seed: 42)`, `game.movePlayer(.left)`: the player cell is
   `[BoardCell(column: 14, row: 29)]`.
4. **Firing.** `var game = Game(seed: 42)`, `game.fire()`: `cells(board, .shot)` of
   `game.boardSnapshot()` is `[BoardCell(column: 15, row: 28)]`.
5. **Ticking.** `var game = Game(seed: 42)`. Take the set of `.segment(isHead: true)`
   cells, call `game.tick()`, and take it again. The two sets differ.
6. **Determinism through the public API.** `var a = Game(seed: 7)` and
   `var b = Game(seed: 7)`. For `step` in `0..<20`: when `step % 3 == 0` call
   `movePlayer(.right)` on both; when `step % 5 == 0` call `fire()` on both; then `tick()`
   both. Afterwards `a.boardSnapshot().entities == b.boardSnapshot().entities`,
   `a.state.score == b.state.score` and `a.state.lives == b.state.lives`.
7. **A public game reaches the renderer.** `var game = Game(seed: 42)`, `game.fire()`,
   `let frame = FrameSnapshot(board: game.boardSnapshot())`:
   `frame.cells[GridPosition(column: 15, row: 29)] == .player` and
   `frame.cells[GridPosition(column: 15, row: 28)] == .shot`.
8. **Existing behaviour intact.** Every existing test file is unmodified, and the whole
   `swift test` suite passes.

## test_strategy

    framework: swift_test
    required: true
    repo: centipede
    base_ref: main
    protected_paths:
      - Tests/CentipedeCoreTests/GameTests.swift
      - Tests/CentipedeCoreTests/BoardSnapshotTests.swift
      - Tests/CentipedeRenderTests/BoardAdapterTests.swift
      - Sources/CentipedeRender/BoardAdapter.swift
      - Sources/CentipedeRender/FrameSnapshot.swift

## Constraints

- No new dependencies, and no new types. Behaviour does not change: this spec only adds
  `public`, plus `Sendable` on `MoveDirection`.
- Do not make any entity type (`World`, `Position`, `Player`, `Shot`, `Spider`, `Flea`,
  `Scorpion`, `CentipedeChain`, `Mushroom`) public, and do not make the test-seam
  initialiser public.
- The new test file never uses `@testable import`.

## Risks

- **Reaching for `@testable`.** It makes the new tests compile on `main` and proves
  nothing. The imports above are part of the contract.
- **Publishing too much.** Making the test-seam initialiser public forces `World` and
  `Position` public, and the change spreads through the module. Only the listed lines
  change.
- **Losing `private(set)`.** The line becomes `public private(set) var state`. A plain
  `public var state` would let any caller rewrite the score and lives.
