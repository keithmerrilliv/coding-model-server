# Centipede renderer — slice 2: the `Game` → `FrameSnapshot` adapter

Jira: DEV-788 (split from DEV-102; follows DEV-765, renderer slice 1). This slice decides
`CentipedeCore`'s public surface: **one read-only projection type, and `Game` stays
`internal`.** The MTL4 migration and a window host are later slices.

## Context

Repo: `centipede`, a SwiftPM package. Every fact below was read from `main` (`5185167`)
before this spec was written; do not re-derive them.

- `Package.swift` is `swift-tools-version:6.0`, `platforms: [.macOS(.v15)]`, four targets:

  ```swift
          .target(name: "CentipedeCore"),
          .testTarget(name: "CentipedeCoreTests", dependencies: ["CentipedeCore"]),
          .target(name: "CentipedeRender"),
          .testTarget(name: "CentipedeRenderTests", dependencies: ["CentipedeRender"]),
  ```

  **Swift 6 language mode is in force**: every concurrency-isolation slip is an error.
- `Sources/CentipedeCore/` is the headless logic core. `Game`, `World`, `CentipedeChain`,
  `Mushroom`, `Position`, `Spider`, `Flea`, `Scorpion`, `Shot` are `internal`; only `Field`
  (`Field.columns == 30`, `Field.rows == 30`) and `GameState` (`score`, `lives`, `wave`;
  `Sendable`, NOT `Equatable`) are `public`.
- `Game`'s stored properties are **`private`** (`world`, `player`, `activeShot`, `spider`,
  `flea`, `scorpion`) — a new file cannot read them. `Game` exposes these `internal`
  accessors instead, all at the bottom of `Sources/CentipedeCore/Game.swift`:

  ```swift
  private(set) var state: GameState
  var playerPosition: Position
  var activeShotValue: Shot?          // Shot has `position: Position`
  var mushroomsSnapshot: [Position: Mushroom]   // Mushroom: damageLevel 0...3, poisoned: Bool
  var chainsSnapshot: [CentipedeChain]          // segments: [Position]; segments[0] is the HEAD
  var spiderSnapshot: Spider?         // position: Position
  var fleaSnapshot: Flea?             // position: Position
  var scorpionSnapshot: Scorpion?     // position: Position
  ```

  A stored mushroom's `damageLevel` is always 0...3 (`World.hit` removes it at 4).
- `Game` has an internal test seam
  `init(world: World, gameState: GameState, playerPosition: Position, spider: Spider? = nil, flea: Flea? = nil, scorpion: Scorpion? = nil)`
  and `init(seed: UInt64)` (player starts at `Position(column: 15, row: 29)`). `fire()`
  places the shot at `Position(column: player.column, row: player.row - 1)` immediately.
  `World(mushrooms:chains:)`, `CentipedeChain(segments:direction:)`, `Mushroom(damageLevel:poisoned:)`,
  `Spider(position:columnDelta:rowDelta:)`, `Flea(position:health:)`, `Scorpion(position:columnDelta:)`
  are the internal memberwise inits the existing tests use.
- `Sources/CentipedeRender/FrameSnapshot.swift` (public, delivered by slice 1):
  `GridPosition(column:row:)` (`Hashable`), `CellKind` with EXACTLY the cases
  `.mushroom(damage: Int)`, `.segment(isHead: Bool)`, `.player`, `.spider`, `.flea`,
  `.scorpion`, and `FrameSnapshot(columns: Int = 30, rows: Int = 30, cells: [GridPosition: CellKind] = [:])`.
  `Sources/CentipedeRender/OffscreenRenderer.swift`: `OffscreenRenderer.init?(device:)`,
  `render(_:width:height:) -> RenderedFrame?`, `RenderedFrame.pixel(x:y:) -> RGBA` with
  y = 0 at the TOP, `Palette.head`, `Palette.player`, `Palette.background`, and an
  exhaustive `Palette.color(for: CellKind)` switch. **`CellKind` is not extended in this
  slice** — a new case would break that switch in a file this slice does not edit.
- Existing tests: `Tests/CentipedeCoreTests/GameTests.swift` is XCTest with
  `@testable import CentipedeCore`; `GameStateTests.swift` and slice 1's
  `Tests/CentipedeRenderTests/OffscreenRendererTests.swift` are Swift Testing
  (`import Testing`, `@Test`, `#expect`, `try #require`). New tests here use **Swift Testing**.
- The Mac runner tests this package with `swift test` on the host (no VM, real Metal GPU).

## Goal

A renderer can draw a live game. `CentipedeCore` publishes a value-type snapshot of the
board (`BoardSnapshot`) that names every entity and its cell; `CentipedeRender` turns a
`BoardSnapshot` into the `FrameSnapshot` slice 1 already renders. Proven by core tests on
the projection and by one pixel-asserted render of an adapted board.

## Rules — read before writing a line

1. **`Game` stays `internal`.** Do not add `public` to `Game`, `World`, `Position`,
   `CentipedeChain`, `Mushroom`, `Spider`, `Flea`, `Scorpion` or `Shot`. Do not edit any
   existing file except `Package.swift`.
2. **`Game.boardSnapshot()` lives in a NEW file as an `extension Game`.** It may use ONLY
   the internal accessors listed above (`state`, `playerPosition`, `activeShotValue`,
   `mushroomsSnapshot`, `chainsSnapshot`, `spiderSnapshot`, `fleaSnapshot`,
   `scorpionSnapshot`). The stored properties are `private` and the diagnostic
   `'world' is inaccessible due to 'private' protection level` means USE THE ACCESSOR, never
   "change the access level".
3. **Swift Testing rules** (they cost attempts on runs 47, 48 and 52): `#require` is ALWAYS
   written `try #require(...)` — a bare `#require(...)` is the compile error `call can throw
   but is not marked with 'try'`. A `@Test` function that uses `try` is declared `throws`. A
   type used as a
   `Dictionary` key or in a `Set` is `Hashable`. Static members referenced from instance
   context are qualified (`Self.x` / `TypeName.x`).
4. **Swift 6 mode, value types only, `Sendable` on every new public type.** No classes,
   no `static var`, no globals, no `@MainActor`, no `async`.
5. **Precedence when two things share a cell** — `entities` is one dictionary, so exactly one
   entity wins, in this fixed order (later wins): mushrooms, then chain segments, then
   scorpion, then flea, then spider, then shot, then player. The player is always visible.
6. **The adapter drops what slice 1 cannot draw**: a `.shot` entity produces NO cell, and a
   mushroom's `poisoned` flag is ignored (`.mushroom(damage: d)` either way). Both are
   slice 3 work with palette additions; say so in a comment, do not add `CellKind` cases.

## Required change

### `Package.swift` (modified — two lines change, nothing else)

```swift
        .target(name: "CentipedeRender", dependencies: ["CentipedeCore"]),
        .testTarget(name: "CentipedeRenderTests", dependencies: ["CentipedeRender", "CentipedeCore"]),
```

Leave the two `CentipedeCore` target lines, the tools version, the name and the platform
line exactly as they are.

### `Sources/CentipedeCore/BoardSnapshot.swift` (new)

```swift
public struct BoardCell: Hashable, Sendable {
    public var column: Int
    public var row: Int
    public init(column: Int, row: Int)
}

public enum BoardEntity: Equatable, Sendable {
    case mushroom(damage: Int, poisoned: Bool)   // damage 0...3, as stored by the core
    case segment(isHead: Bool)
    case player
    case shot
    case spider
    case flea
    case scorpion
}

/// Everything a renderer needs for one tick. Read-only, renderer-free, value type.
public struct BoardSnapshot: Sendable {
    public var columns: Int
    public var rows: Int
    public var entities: [BoardCell: BoardEntity]
    public var state: GameState
    public init(columns: Int = Field.columns, rows: Int = Field.rows,
                entities: [BoardCell: BoardEntity] = [:], state: GameState = GameState())
}

extension Game {
    /// Projects the current tick. Internal, like `Game`; built from the internal accessors.
    func boardSnapshot() -> BoardSnapshot
}
```

`boardSnapshot()` fills `entities` in the precedence order of rule 5: every mushroom as
`.mushroom(damage: m.damageLevel, poisoned: m.poisoned)`; for every chain, `segments[0]` as
`.segment(isHead: true)` and the rest as `.segment(isHead: false)`; then scorpion, flea,
spider (each at its `position`, when present); then `.shot` at `activeShotValue.position`
when present; then `.player` at `playerPosition`. `columns`/`rows` are `Field.columns` /
`Field.rows`; `state` is `state`. `BoardSnapshot` is NOT `Equatable` (`GameState` is not);
tests compare fields.

### `Sources/CentipedeRender/BoardAdapter.swift` (new)

```swift
import CentipedeCore

extension FrameSnapshot {
    /// Adapts a core board to the renderer's frame. `.shot` and the poisoned flag are
    /// dropped until the palette grows (slice 3).
    public init(board: BoardSnapshot)
}
```

Mapping: `BoardCell(column:row:)` → `GridPosition(column:row:)`;
`.mushroom(damage: d, poisoned: _)` → `.mushroom(damage: d)`; `.segment(isHead:)` →
`.segment(isHead:)`; `.player` → `.player`; `.spider` → `.spider`; `.flea` → `.flea`;
`.scorpion` → `.scorpion`; `.shot` → no cell. `columns`/`rows` are copied from the board.

### `Tests/CentipedeCoreTests/BoardSnapshotTests.swift` (new)

`import Testing` and `@testable import CentipedeCore`; a `struct BoardSnapshotTests` suite
or free `@Test` functions — either, but do not mix XCTest in. Tests below.

### `Tests/CentipedeRenderTests/BoardAdapterTests.swift` (new)

`import Testing`, `import CentipedeCore`, `import CentipedeRender` (no `@testable` needed:
everything used is public). Tests below.

## Change surface

Repo-relative paths. `Package.swift` was verified to exist at `main`; the other four were
verified NOT to exist. The plan's implement phase must list all five as outputs and
`Package.swift` as an input.

| Path | Change |
| --- | --- |
| `Package.swift` | modified |
| `Sources/CentipedeCore/BoardSnapshot.swift` | new |
| `Sources/CentipedeRender/BoardAdapter.swift` | new |
| `Tests/CentipedeCoreTests/BoardSnapshotTests.swift` | new |
| `Tests/CentipedeRenderTests/BoardAdapterTests.swift` | new |

Do not touch any other file. The four protected files below are served read-only so the
accessors and the renderer API can be read, not guessed.

## Acceptance criteria

A green build is NOT sufficient — the tests below are the gate. None of them can pass on
`main`: `BoardSnapshot` does not exist there, so neither test file compiles. Say so in the
report.

1. `swift build` succeeds for the whole package with no new warnings; the existing
   `CentipedeCoreTests` (GameTests + GameStateTests) and `CentipedeRenderTests`
   (OffscreenRendererTests) still pass unchanged.
2. **Deterministic setup projects every entity.** A `Game` built with the test seam from
   `World(mushrooms: [Position(column: 2, row: 3): Mushroom(damageLevel: 2, poisoned: true)], chains: [CentipedeChain(segments: [Position(column: 5, row: 0), Position(column: 4, row: 0), Position(column: 3, row: 0)], direction: .right)])`,
   `GameState(score: 10, lives: 2, wave: 3)`, player `Position(column: 15, row: 29)`,
   `Spider(position: Position(column: 10, row: 27), columnDelta: 1, rowDelta: 1)`,
   `Flea(position: Position(column: 7, row: 4), health: 2)`,
   `Scorpion(position: Position(column: 0, row: 12), columnDelta: 1)`: `boardSnapshot()`
   has `columns == 30`, `rows == 30`, `entities.count == 8`, and exactly
   `.mushroom(damage: 2, poisoned: true)` at (2, 3), `.segment(isHead: true)` at (5, 0),
   `.segment(isHead: false)` at (4, 0) and (3, 0), `.player` at (15, 29), `.spider` at
   (10, 27), `.flea` at (7, 4), `.scorpion` at (0, 12); `state.score == 10`,
   `state.lives == 2`, `state.wave == 3`.
3. **The shot is projected.** Exactly this shape (runs 52 and 53 both reached for it):

   ```swift
   var game = Game(world: World(mushrooms: [:], chains: []),
                   gameState: GameState(), playerPosition: Position(column: 15, row: 29))
   game.fire()
   let snap = game.boardSnapshot()
   #expect(snap.entities.count == 2)
   #expect(snap.entities[BoardCell(column: 15, row: 28)] == .shot)
   #expect(snap.entities[BoardCell(column: 15, row: 29)] == .player)
   ```
4. **A seeded game agrees with the core's own accessors.** The accessors live on the
   `Game`, never on the snapshot (`snap.mushroomsSnapshot` does not exist and cost run 52
   an attempt). Exactly this shape:

   ```swift
   let g = Game(seed: 7)
   let snap = g.boardSnapshot()
   for (pos, m) in g.mushroomsSnapshot {
       #expect(snap.entities[BoardCell(column: pos.column, row: pos.row)]
               == .mushroom(damage: m.damageLevel, poisoned: m.poisoned))
   }
   #expect(snap.entities[BoardCell(column: 15, row: 29)] == .player)
   for chain in g.chainsSnapshot {
       let head = chain.segments[0]
       #expect(snap.entities[BoardCell(column: head.column, row: head.row)] == .segment(isHead: true))
   }
   #expect(snap.entities.keys.allSatisfy { (0..<30).contains($0.column) && (0..<30).contains($0.row) })
   ```
5. **Precedence.** Two games, no tick, exactly these:

   ```swift
   // A: spider on a mushroom's cell → the spider wins
   let a = Game(world: World(mushrooms: [Position(column: 10, row: 27): Mushroom()], chains: []),
                gameState: GameState(), playerPosition: Position(column: 15, row: 29),
                spider: Spider(position: Position(column: 10, row: 27), columnDelta: 1, rowDelta: 1))
   #expect(a.boardSnapshot().entities[BoardCell(column: 10, row: 27)] == .spider)
   // B: spider on the player's cell → the player wins
   let b = Game(world: World(mushrooms: [:], chains: []),
                gameState: GameState(), playerPosition: Position(column: 15, row: 29),
                spider: Spider(position: Position(column: 15, row: 29), columnDelta: 1, rowDelta: 1))
   #expect(b.boardSnapshot().entities[BoardCell(column: 15, row: 29)] == .player)
   ```

   A game with a mushroom under the PLAYER and no spider tests nothing here; do not write
   that (run 52's design seam did, and synthesis copied it).
6. **Adapter maps every drawable kind and drops the rest.** A `BoardSnapshot` built with the
   public init holding one of each `BoardEntity` in distinct cells (the mushroom
   `.mushroom(damage: 2, poisoned: true)`) adapts to a `FrameSnapshot` with
   `columns == 30`, `rows == 30`, `cells.count == 7` (eight entities, the shot dropped),
   `.mushroom(damage: 2)` at the
   mushroom's cell, `.segment(isHead: true)`, `.segment(isHead: false)`, `.player`,
   `.spider`, `.flea`, `.scorpion` at theirs, and NO cell at the shot's position.
7. **An adapted board renders where the board said.** `FrameSnapshot(board:)` of a board with
   `.segment(isHead: true)` at `BoardCell(column: 0, row: 0)` and `.player` at
   `BoardCell(column: 0, row: 29)`, rendered by `try #require(OffscreenRenderer())` at
   `width: 300, height: 300`: `pixel(x: 5, y: 5) == Palette.head`,
   `pixel(x: 5, y: 295) == Palette.player`, `pixel(x: 150, y: 150) == Palette.background`.
8. No existing file other than `Package.swift` is modified, and `Package.swift` differs from
   `main` only in the two dependency lists quoted above.

## test_strategy

    framework: swift_test
    required: true
    repo: centipede
    base_ref: main
    protected_paths:
      - Sources/CentipedeCore/Game.swift
      - Sources/CentipedeCore/World.swift
      - Sources/CentipedeRender/FrameSnapshot.swift
      - Sources/CentipedeRender/OffscreenRenderer.swift

## Constraints

- No new dependencies beyond the two intra-package edges above. No `.metal` files, no
  resources, no `Bundle.module`, no change to the tools version or platforms.
- No `public` added to any existing type. No edits outside the change surface.
- No Metal code in this slice beyond calling the delivered `OffscreenRenderer` from one test.

## Risks

- **Reaching for `private` state.** The one way `BoardSnapshot.swift` fails to compile is
  reading `world`/`player`/`activeShot` directly from the extension. The accessors exist for
  exactly this; rule 2.
- **Module import in the render tests.** `BoardAdapterTests` imports `CentipedeCore`
  directly, which is why `CentipedeRenderTests` gains it as an explicit dependency in
  `Package.swift`; relying on the transitive edge is not enough for `import`.
- **`GameState` is not `Equatable`.** Do not write `#expect(board.state == …)`; compare the
  three fields. Do not add a conformance in `GameState.swift` (protected).
- **Precedence off by one entity.** Criterion 5 exists to pin the order in rule 5; a
  `merge`-style dictionary build that lets the mushroom win fails it.
