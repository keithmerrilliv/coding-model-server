# Centipede: a GameSession that turns one tick of player input into the next frame

Jira: DEV-827, epic DEV-442. Repo `centipede`, `main` = `9c4eaaa` (runs 63 and 64:
DEV-825's public `Game` API and DEV-826's HUD are both on it). Every fact below was read from that commit.
A reference implementation of exactly the file below, with the tests described, ran on the
Mac runner before this spec was written: the suite passed, with all 6 new tests passing.
The new test file alone, against unmodified `main`, fails to compile
(`cannot find 'PlayerInput' in scope`, 16 times; `cannot find 'GameSession' in scope`, 12
times). Do not re-derive the expected values.

## Context

`CentipedeCore` exposes a playable game: `public struct Game` with `public init(seed:)`,
`public mutating func movePlayer(_ direction: MoveDirection)`,
`public mutating func fire()`, `public mutating func tick()`, `public var isGameOver` and
`public func boardSnapshot() -> BoardSnapshot`. `MoveDirection` is
`public enum MoveDirection: Sendable { case up, down, left, right }`. `Game.tick()`
returns immediately once the game is over.

`CentipedeRender` turns a board into a frame with `FrameSnapshot(board:)`, and that frame
carries `hud: HUD` (score, lives, wave, isGameOver). `HUD` is `Equatable`. A frame's
`cells` is `[GridPosition: CellKind]`, and `CellKind` is `Equatable`.

**The gap.** Every caller that wants to play has to wire each tick by hand: move, fire,
`tick()`, `boardSnapshot()`, `FrameSnapshot(board:)`. Nothing owns that loop, so nothing
fixes its order. Firing before moving puts the shot above the wrong column.

**The addition.** One new file in `CentipedeRender`. `PlayerInput` is one tick's input.
`GameSession` owns a `Game` and steps it: move first, then fire, then tick. It returns the
new frame and counts its steps. `GameSession` has no game-over branch, because
`Game.tick()` already changes nothing after game over, and a branch here could not be
reached through the public API to test it.

**Concurrency.** Swift tools 6.0, with no default actor isolation. Add no actor
annotations. `PlayerInput` is `Sendable`. `GameSession` declares no conformances.

## Required change

### `Sources/CentipedeRender/GameSession.swift` (new — write exactly this)

```swift
import CentipedeCore

/// One tick's worth of player input (DEV-827).
public struct PlayerInput: Equatable, Sendable {
    public var move: MoveDirection?
    public var fire: Bool

    public init(move: MoveDirection? = nil, fire: Bool = false) {
        self.move = move
        self.fire = fire
    }

    /// No movement and no shot.
    public static let none = PlayerInput()
}

/// Drives a `Game` one tick at a time and hands back the frame to draw (DEV-827).
public struct GameSession {
    public private(set) var game: Game
    /// How many times `step(_:)` has run.
    public private(set) var steps: Int = 0

    public init(seed: UInt64) {
        self.game = Game(seed: seed)
    }

    /// The frame for the game as it stands, HUD included.
    public var frame: FrameSnapshot {
        FrameSnapshot(board: game.boardSnapshot())
    }

    /// Applies `input` (move first, then fire), advances the game one tick, and returns
    /// the new frame. Once the game is over, `Game.tick()` changes nothing.
    public mutating func step(_ input: PlayerInput) -> FrameSnapshot {
        if let direction = input.move {
            game.movePlayer(direction)
        }
        if input.fire {
            game.fire()
        }
        game.tick()
        steps += 1
        return frame
    }
}
```

### `Tests/CentipedeRenderTests/GameSessionTests.swift` (new)

swift-testing, mirroring `BoardAdapterTests.swift`, with plain imports, never
`@testable`:

```swift
import Testing
import CentipedeCore
import CentipedeRender
```

Use `struct GameSessionTests`, with each test a synchronous `@Test func`. Compare frames by
their `cells` and their `hud`: `FrameSnapshot` itself is not `Equatable`, and must not be
made so. Every test builds its own sessions and games.

## Change surface

`Sources/CentipedeRender/GameSession.swift` and
`Tests/CentipedeRenderTests/GameSessionTests.swift` were verified NOT to exist at `main`.
The plan's implement phase lists exactly these two as outputs. No existing file changes.

| Path | Change |
| --- | --- |
| `Sources/CentipedeRender/GameSession.swift` | new |
| `Tests/CentipedeRenderTests/GameSessionTests.swift` | new |

The protected files below are served read-only. Do not edit them.

## Acceptance criteria

A green build is NOT sufficient; the tests are the gate. On `main` the new test file does
not compile, and the report must say so. The player starts at column 15, row 29.

1. **A new session shows the starting frame.** `let session = GameSession(seed: 42)`:
   `session.steps == 0`,
   `session.frame.cells[GridPosition(column: 15, row: 29)] == .player`, and
   `session.frame.hud == HUD(score: 0, lives: 3, wave: 1, isGameOver: false)`.
2. **A step counts and returns the session's frame.** `var session = GameSession(seed: 42)`,
   `let returned = session.step(.none)`: `session.steps == 1`,
   `returned.cells == session.frame.cells` and `returned.hud == session.frame.hud`.
3. **A move input moves the player.** `var session = GameSession(seed: 42)`,
   `let frame = session.step(PlayerInput(move: .left))`:
   `frame.cells[GridPosition(column: 14, row: 29)] == .player`, and
   `frame.cells[GridPosition(column: 15, row: 29)] != .player`.
4. **A step is exactly move, then fire, then tick.** With these inputs, in order:
   `[PlayerInput(move: .left, fire: true), .none, PlayerInput(move: .up), PlayerInput(fire: true), PlayerInput(move: .right, fire: true), .none]`.
   Run `var session = GameSession(seed: 11)` and `var game = Game(seed: 11)` side by side.
   For each input: `let frame = session.step(input)`. On `game`, call `movePlayer` if
   `input.move` is set, `fire()` if `input.fire`, then `tick()`, and take
   `let expected = FrameSnapshot(board: game.boardSnapshot())`. Assert
   `frame.cells == expected.cells` and `frame.hud == expected.hud`.
5. **The same seed and inputs give the same frames.** `var a = GameSession(seed: 7)` and
   `var b = GameSession(seed: 7)`. For `step` in `0..<30`, use
   `let input = PlayerInput(move: step % 4 == 0 ? .right : nil, fire: step % 3 == 0)`;
   step both, and compare the returned frames' `cells` and `hud`. Afterwards
   `a.steps == 30`.
6. **`.none` is no move and no fire.**
   `PlayerInput.none == PlayerInput(move: nil, fire: false)`.
7. **Existing behaviour intact.** Every existing test file is unmodified, and the whole
   `swift test` suite passes.

## test_strategy

    framework: swift_test
    required: true
    repo: centipede
    base_ref: main
    protected_paths:
      - Tests/CentipedeRenderTests/PublicGameTests.swift
      - Tests/CentipedeRenderTests/HUDFrameTests.swift
      - Tests/CentipedeRenderTests/BoardAdapterTests.swift
      - Sources/CentipedeRender/FrameSnapshot.swift
      - Sources/CentipedeRender/BoardAdapter.swift
      - Sources/CentipedeCore/Game.swift

## Constraints

- No new dependencies. No existing file changes, `CentipedeCore` included.
- `FrameSnapshot` does not become `Equatable`. Tests compare `cells` and `hud`.
- No game-over branch in `step(_:)`, and no `@testable import` in the new test file.

## Risks

- **Firing before moving.** The shot then leaves from the old column. Criterion 4
  compares every step against move → fire → tick on a bare `Game`.
- **Counting only effective steps.** `steps` counts every `step(_:)` call. Criterion 5
  pins 30.
- **Returning a stale frame.** `step` must return the frame after the tick. Criterion 2
  compares it with `frame` read afterwards.
