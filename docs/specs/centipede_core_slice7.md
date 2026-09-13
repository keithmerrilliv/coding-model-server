# Centipede logic core — slice 7: life-loss mushroom restoration, extra lives, and player-zone heads

Extend the headless `CentipedeCore` SwiftPM library (slices 1–6 landed, 46 tests green: 26 XCTest in GameTests plus 20 Swift Testing cases in WorldTests/GameStateTests)
with three classic rules the core still lacks: damaged mushrooms are **restored for points
when the player loses a life**, an **extra life** is awarded every 12,000 points, and once a
centipede has reached the player zone, **extra heads enter the zone from the sides** at a
fixed interval. Renderer-free, `swift_test`, deterministic.

## Repository & delivery
- **Target repo:** `centipede` → `~/Dev/Centipede` (SwiftPM, branch `main`, github remote). Deliver to `pipeline/<spec_id>`; operator merges. `execution_target: client`.
- **Test:** `swift_test` (scheme `CentipedeCore`, destination `platform=macOS`, filter `CentipedeCoreTests`). No `import Metal / AppKit / SpriteKit / CoreGraphics`.

## Change surface
This spec **modifies three existing files and creates none** — the current contents of the
three files are ground truth. Make only the changes described; leave every other line,
method, test, and accessor exactly as it is (emit anchored edits against the provided
contents).

| Path | Change |
| --- | --- |
| `Sources/CentipedeCore/World.swift` | modify |
| `Sources/CentipedeCore/Game.swift` | modify |
| `Tests/CentipedeCoreTests/GameTests.swift` | modify |

Modify no other file (`Spider.swift`, `Flea.swift`, `Scorpion.swift`, `GameState.swift`,
`Player.swift`, `Wave.swift`, `Collision.swift`, `Position.swift`, `Mushroom.swift`,
`HitResult.swift`, `CentipedeChain.swift`, `Shot.swift`, `MoveDirection.swift`,
`SeededRNG.swift`, `Package.swift`, `WorldTests.swift`, `GameStateTests.swift`).

## 🔒 Access control (recurring build-breaker)
All new members are `internal` (the default). Do **NOT** write `public`. Every method that
assigns to `self` or a stored property is `mutating`. Tests use `@testable import CentipedeCore`.
`Mushroom` already has the memberwise initializer `Mushroom(damageLevel:poisoned:)`; use it,
do not add one.

## EDIT — `Sources/CentipedeCore/World.swift`
Add two methods to `World`, placed after `poisonMushroom(at:)` and before
`snapshotMushrooms()`. Change nothing else in `World.swift`.

```swift
    // Restore every damaged or poisoned mushroom to a fresh one (the classic
    // between-lives repair). Returns how many mushrooms were changed, so the
    // caller can award points for each.
    mutating func restoreMushrooms() -> Int {
        var restored = 0
        for (position, mushroom) in mushrooms where mushroom.damageLevel > 0 || mushroom.poisoned {
            mushrooms[position] = Mushroom()
            restored += 1
        }
        return restored
    }

    // Add a chain to the field (a head entering the player zone from the side).
    mutating func addChain(_ chain: CentipedeChain) {
        chains.append(chain)
    }
```

## EDIT — `Sources/CentipedeCore/Game.swift`
Make these edits against the file's current contents; leave everything else (all existing
properties, both initializers' existing lines, `movePlayer`, `fire`, `isGameOver`, the
existing tick steps 1–6 and the flea-spawn block, and every accessor) exactly as-is.

1. **New constants** — next to `fleaSpawnMushroomThreshold`:
   ```swift
    private static let restoredMushroomPoints = 5
    private static let extraLifeEvery = 12_000
    private static let zoneHeadInterval = 16
   ```
2. **New stored properties** — after `private(set) var spidersDestroyed: Int = 0`:
   ```swift
    private var nextExtraLifeAt: Int
    private var ticksSinceZoneHead: Int = 0
    private var nextZoneHeadFromLeft: Bool = true
   ```
3. **Both initializers** — set the extra-life threshold from the starting score, as the
   last line of each init body:
   ```swift
        self.nextExtraLifeAt = (self.state.score / Game.extraLifeEvery + 1) * Game.extraLifeEvery
   ```
   (In `init(seed:)` the score is 0, so this is 12,000; in the test-seam init a game
   started at 11,950 gets its first extra life at 12,000, not 24,000.)
4. **`tick()` — player-zone heads.** Immediately AFTER `world.step()` (the end of the
   existing "Step 2: advance the centipede" line) and BEFORE the spider step, insert:
   ```swift
        // Step 2b: once any segment is inside the player zone, extra heads enter
        // the zone from alternating sides every zoneHeadInterval ticks.
        let zoneTopRow = Field.rows - Field.playerZoneRows
        let centipedeInZone = world.snapshotChains().contains { chain in
            chain.segments.contains { $0.row >= zoneTopRow }
        }
        if centipedeInZone {
            ticksSinceZoneHead += 1
            if ticksSinceZoneHead >= Game.zoneHeadInterval {
                let column = nextZoneHeadFromLeft ? 0 : Field.columns - 1
                let direction: Direction = nextZoneHeadFromLeft ? .right : .left
                world.addChain(CentipedeChain(segments: [Position(column: column, row: zoneTopRow)], direction: direction))
                nextZoneHeadFromLeft.toggle()
                ticksSinceZoneHead = 0
            }
        } else {
            ticksSinceZoneHead = 0
        }
   ```
5. **`tick()` — restoration on life loss.** In the existing Step 5 collision block, the
   respawn branch currently reads
   ```swift
            if state.lives > 0 {
                player = Player(position: Position(column: Field.columns / 2, row: Field.rows - 1))
            }
   ```
   Change it to
   ```swift
            if state.lives > 0 {
                player = Player(position: Position(column: Field.columns / 2, row: Field.rows - 1))
                // The field is repaired between lives; each repaired mushroom scores.
                let restored = world.restoreMushrooms()
                state.score += restored * Game.restoredMushroomPoints
            }
   ```
   Nothing is restored when the last life is lost (the game is over).
6. **`tick()` — extra lives.** At the very END of `tick()`, after the existing flea-spawn
   block, insert:
   ```swift
        // An extra life every extraLifeEvery points, however the points arrived.
        while state.score >= nextExtraLifeAt {
            state.lives += 1
            nextExtraLifeAt += Game.extraLifeEvery
        }
   ```

## EDIT — `Tests/CentipedeCoreTests/GameTests.swift`
**Append** the following tests to the existing `GameTests` class. Do NOT remove, rewrite, or
reorder any existing test — the file currently has **26** methods (the last is
`testScorpionSpawnsOnWaveClear`); after this change it must have **32**.

```swift
    // MARK: - Slice 7: restoration, extra lives, player-zone heads

    // Four mushrooms up top (>= the flea threshold, so no flea interferes): two
    // need repair (damaged, poisoned), two are already fresh.
    private func repairableField() -> [Position: Mushroom] {
        return [
            Position(column: 3, row: 3): Mushroom(damageLevel: 2),
            Position(column: 4, row: 4): Mushroom(),
            Position(column: 5, row: 5): Mushroom(damageLevel: 1, poisoned: true),
            Position(column: 6, row: 6): Mushroom(),
        ]
    }

    func testLifeLossRestoresDamagedMushroomsForPoints() {
        // Chain head at (9,29) steps right onto the player at (10,29) this tick.
        var g = Game(
            world: World(mushrooms: repairableField(), chains: [
                CentipedeChain(segments: [Position(column: 9, row: 29)], direction: .right)
            ]),
            gameState: GameState(),
            playerPosition: Position(column: 10, row: 29)
        )
        g.tick()
        XCTAssertEqual(g.state.lives, 2)
        XCTAssertEqual(g.state.score, 10)                       // 2 repaired x 5
        XCTAssertEqual(g.mushroomsSnapshot.count, 4)
        XCTAssertEqual(g.mushroomsSnapshot[Position(column: 3, row: 3)], Mushroom())
        XCTAssertEqual(g.mushroomsSnapshot[Position(column: 5, row: 5)], Mushroom())
        XCTAssertEqual(g.mushroomsSnapshot[Position(column: 4, row: 4)], Mushroom())
    }

    func testNoRestorationWhenTheLastLifeIsLost() {
        var g = Game(
            world: World(mushrooms: repairableField(), chains: [
                CentipedeChain(segments: [Position(column: 9, row: 29)], direction: .right)
            ]),
            gameState: GameState(lives: 1),
            playerPosition: Position(column: 10, row: 29)
        )
        g.tick()
        XCTAssertTrue(g.isGameOver)
        XCTAssertEqual(g.state.score, 0)
        XCTAssertEqual(g.mushroomsSnapshot[Position(column: 3, row: 3)]?.damageLevel, 2)
        XCTAssertEqual(g.mushroomsSnapshot[Position(column: 5, row: 5)]?.poisoned, true)
    }

    func testExtraLifeAtTwelveThousandPoints() {
        // A head at (15,27): the shot fired from (15,29) spawns at (15,28) and
        // reaches (15,27) on this tick's step 1, before the centipede moves.
        var g = Game(
            world: World(mushrooms: [:], chains: [
                CentipedeChain(segments: [Position(column: 15, row: 27)], direction: .right)
            ]),
            gameState: GameState(score: 11_950, lives: 3),
            playerPosition: Position(column: 15, row: 29)
        )
        g.fire()
        g.tick()
        XCTAssertEqual(g.state.score, 12_050)                   // head hit: +100
        XCTAssertEqual(g.state.lives, 4)                        // crossed 12,000
    }

    func testNoExtraLifeBelowTheThreshold() {
        var g = Game(
            world: World(mushrooms: [:], chains: [
                CentipedeChain(segments: [Position(column: 15, row: 27)], direction: .right)
            ]),
            gameState: GameState(score: 11_850, lives: 3),
            playerPosition: Position(column: 15, row: 29)
        )
        g.fire()
        g.tick()
        XCTAssertEqual(g.state.score, 11_950)
        XCTAssertEqual(g.state.lives, 3)
    }

    // Four mushrooms on row 0 keep the flea away; a lone head at (5,27) is inside
    // the player zone (rows 25-29) and walks right, never reaching the player at
    // (25,29) within these ticks.
    private func zoneGame() -> Game {
        let mush: [Position: Mushroom] = [
            Position(column: 0, row: 0): Mushroom(), Position(column: 1, row: 0): Mushroom(),
            Position(column: 2, row: 0): Mushroom(), Position(column: 3, row: 0): Mushroom()]
        return Game(
            world: World(mushrooms: mush, chains: [
                CentipedeChain(segments: [Position(column: 5, row: 27)], direction: .right)
            ]),
            gameState: GameState(),
            playerPosition: Position(column: 25, row: 29)
        )
    }

    func testZoneHeadEntersFromTheLeftAfterTheInterval() {
        var g = zoneGame()
        for _ in 0..<15 { g.tick() }
        XCTAssertEqual(g.chainsSnapshot.count, 1)               // not yet
        g.tick()                                                // 16th tick
        XCTAssertEqual(g.chainsSnapshot.count, 2)
        let zoneTop = Field.rows - Field.playerZoneRows
        XCTAssertTrue(g.chainsSnapshot.contains(
            CentipedeChain(segments: [Position(column: 0, row: zoneTop)], direction: .right)))
        XCTAssertEqual(g.state.lives, 3)
    }

    func testZoneHeadsAlternateSides() {
        var g = zoneGame()
        for _ in 0..<32 { g.tick() }
        XCTAssertEqual(g.chainsSnapshot.count, 3)
        let zoneTop = Field.rows - Field.playerZoneRows
        XCTAssertTrue(g.chainsSnapshot.contains(
            CentipedeChain(segments: [Position(column: Field.columns - 1, row: zoneTop)], direction: .left)))
        XCTAssertEqual(g.state.lives, 3)
    }
```

## Acceptance criteria
1. When the player loses a life and lives remain, every mushroom with `damageLevel > 0` or
   `poisoned == true` becomes a fresh `Mushroom()`, and the score rises by 5 per mushroom
   restored (`World.restoreMushrooms()` returns the count). Losing the last life restores
   nothing and scores nothing.
2. Whenever the score reaches or passes a multiple of 12,000, one life is added per multiple
   crossed, at the end of that tick; a game started mid-way (test seam) counts from its next
   multiple, not from zero.
3. While any centipede segment is inside the player zone (rows `Field.rows -
   Field.playerZoneRows` and below), every 16 ticks a new single-segment chain enters at the
   zone's top row from alternating sides — first from column 0 heading `.right`, then from
   column `Field.columns - 1` heading `.left`; the interval counter resets whenever no
   segment is in the zone.
4. **No regression:** all pre-existing tests pass — `swift test` runs **52 tests
   total** (46 existing + 6 new: 26 GameTests→32, plus the 20 Swift Testing world/state cases), 0 failures.
5. `internal` only; only `World.swift`, `Game.swift`, `GameTests.swift` edited; no new
   files; no other file touched.

## Constraints
- Emit anchored edits for the three existing files against their provided contents. Preserve
  every existing line and test. No new files.
- `internal` only; reuse `Mushroom()`, `CentipedeChain(segments:direction:)`, `Direction`,
  `Field.playerZoneRows`, and the existing life-loss path; pure Swift; `Package.swift` untouched.

## test_strategy

```yaml
framework: swift_test
required: true
repo: centipede
base_ref: main
scheme: CentipedeCore
destination: "platform=macOS"
filter: CentipedeCoreTests
execution_target: client
protected_paths:
  - Package.swift
  - Sources/CentipedeCore/GameState.swift
  - Sources/CentipedeCore/CentipedeChain.swift
  - Sources/CentipedeCore/Mushroom.swift
  - Sources/CentipedeCore/Position.swift
  - Sources/CentipedeCore/Spider.swift
  - Sources/CentipedeCore/Flea.swift
  - Sources/CentipedeCore/Scorpion.swift
  - Tests/CentipedeCoreTests/WorldTests.swift
  - Tests/CentipedeCoreTests/GameStateTests.swift
notes: "Renderer-free deterministic logic core. No import Metal/AppKit/SpriteKit/CoreGraphics."
```
