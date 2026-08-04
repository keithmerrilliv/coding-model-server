# Centipede — Classic Arcade Game (WebGPU renderer)

## Context

Brand-new standalone project (this spec workspace) — there is no existing repository.
Recreate the classic 1981 Atari arcade game **Centipede** as a browser-based single-page
application rendered entirely with WebGPU. Pure vanilla JavaScript ES modules only — no
TypeScript, zero dependencies, no build step. A pure deterministic game-logic core runs
headless under `node --test`, while a separate WebGPU rendering layer draws the play field
in stable desktop browsers.

Pin concrete stable versions in the README: Node.js 22.x LTS (the runtime used to run
`node --test` for the unit tests) and a minimum WebGPU-capable browser of Chrome/Edge 113+
(the first stable release to ship WebGPU), while recommending the current stable browser
release. No beta or experimental channels or flags.

## Required behavior

- Play field grid seeded with mushrooms; player blaster confined to bottom rows moving
  left/right and firing darts upward.
- Centipede chain enters from top, weaves horizontally, drops one row and reverses
  direction when hitting mushrooms or screen edges.
- Shooting a body segment turns it into a mushroom and splits the centipede into two
  independent chains; shooting a head scores more points.
- Mushrooms take exactly 4 hits to destroy and render progressively damaged states.
- Spider zig-zags through the player zone eating mushrooms; Flea drops vertically leaving a
  mushroom trail; Scorpion crosses the field poisoning mushrooms, causing centipedes that
  touch poisoned ones to dive straight down.
- Scoring system tracks lives, waves, game-over state, and restart capability.
- Arrow keys/mouse move the blaster; space/mouse button fires darts (one/few at a time).

## Architecture requirements

- The pure logic core has zero DOM/WebGPU/browser globals and is deterministic given
  inputs + a seeded RNG.
- The WebGPU renderer draws the entire play field via GPU pipelines/WGSL shaders — no
  Canvas2D/WebGL/DOM-sprite fallback for the game area.
- If `navigator.gpu` is absent, display a clear "WebGPU required" message instead of
  crashing.
- The game loop uses a fixed update timestep decoupled from render, targeting smooth 60fps
  with proper drawable resize handling.
- Expected outputs: `core.js`, `renderer.js`, `input.js`, `index.html`, `README.md`, plus
  `*.test.js` unit tests.

## Constraints

- Vanilla JavaScript ES modules only — no TypeScript (.ts/tsconfig/tsc/transpile/bundle/build step).
- Zero dependencies — no node_modules, no runtime or dev packages, no bundler.
- Only permitted package.json is exactly `{"type":"module"}`, or omit it entirely and use
  .mjs extensions. All source files end in .js. JSDoc comments acceptable for type hints;
  never TypeScript syntax.

## Acceptance criteria

All unit tests pass under `node --test` — no failures. Coverage must include:

- Centipede stepping + edge/mushroom turning behavior
- Body-hit splitting into two independent chains (new head grows behind hit)
- Head-hit scoring differential vs body hits
- Mushroom progressive damage (4 hits to destroy)
- Spider zig-zag movement through player zone eating mushrooms
- Flea vertical drop leaving mushroom trail when player zone sparse
- Scorpion crossing poisoning mushrooms, causing centipedes touching poisoned ones to dive
  straight down
- Collision detection between darts/blaster and enemies
- Life loss on blaster collision
- Wave advancement after clearing all segments, with speed increase
- Deterministic seeded RNG producing reproducible sequences

## test_strategy

    framework: node_test
    required: true
    notes: |
      Tests are plain *.test.js files using ONLY built-in node:test and node:assert modules.
      Run headless via `node --test` with zero external packages, no network access, no GPU,
      no DOM. The pure logic core must be fully testable under these constraints.
      Rendering/WebGPU code is exercised manually only.

## Risks

- WebGPU API surface is large; shader complexity for rendering many entities at 60fps may
  require careful buffer management and draw call batching.
- Determinism of the game loop depends entirely on fixed-timestep discipline; any drift or
  variable update count per frame breaks reproducibility in tests.
- Centipede split logic has subtle state-management requirements (two new heads, correct
  segment ownership, direction preservation) that are easy to get wrong.
- Enemy behaviors (spider pathfinding through mushrooms, flea spawn conditions based on
  sparsity threshold, scorpion poison propagation timing) have interdependent edge cases
  requiring thorough test coverage.
