# Electric Sheep — Terminal Renderer (CLI)

A small command-line app that renders "electric sheep" jumping over a fence as
ASCII art. Two parameters — the number of sheep and their speed — control the
scene. It runs as a terminal animation, but the rendering core is a **pure,
deterministic function** so it can be unit-tested headlessly on Linux.

## Scope
A standalone Python package `electric_sheep/` at the repo root:
- `electric_sheep/__init__.py`
- `electric_sheep/scene.py` — the pure render core (deterministic)
- `electric_sheep/cli.py` — argument parsing + the terminal animation loop
- `electric_sheep/__main__.py` — enables `python -m electric_sheep`
- `tests/test_scene.py` — pytest tests (this is what the orchestrator's
  bwrap+pytest sandbox actually executes)
- `README.md`

**Python 3, standard library only** — no third-party packages. Keep it small.

Out of scope: ANSI color themes, sound, config files, real-time keyboard input,
GUI, packaging/setup.py.

## Fixed rendering conventions
Use exactly these so the rendering is unambiguous and headlessly testable:
- Each sheep is the single ASCII character `'S'`. The fence is the single
  character `'|'`. Empty cells are spaces `' '`.
- The fence is drawn at column `width // 2` in **every** row.
- A frame is `height` lines of exactly `width` characters each, joined by `'\n'`
  with **no trailing newline**.
- **Determinism:** identical `(count, speed, t, width, height)` arguments must
  produce byte-identical output. `scene.py` must NOT import `time`, `random`, or
  read input — all motion is a pure function of the arguments.

## The render core
`electric_sheep/scene.py` exports:

```python
def render_frame(count: int, speed: float, t: int,
                 width: int = 40, height: int = 12) -> str:
    """Render one frame of `count` sheep at time-step `t`.

    Returns a `height`-line string of `width` columns each (see conventions).

    - count: number of sheep, 1..20 (raise ValueError outside that range).
    - speed: positive float multiplier (raise ValueError if <= 0).
    - t: integer time-step; the field advances horizontally as t increases.
    """
```

### Behavioral requirements (the *what* — you choose the math)
- Each sheep moves horizontally as `t` increases and **wraps** around the screen
  width so motion loops.
- A sheep follows a **jump arc**: it rises (smaller row index) as it nears the
  fence, peaks at the fence column, and descends past it.
- **All `count` sheep are simultaneously visible.** Each sheep occupies its own
  distinct column, for **every** valid count 1..20 — so exactly `count` `'S'`
  glyphs appear in the frame (a sheep that lands on the fence column overwrites
  the `'|'` there and still counts as a sheep).
- **The animation must not repeat with a short period.** As `t` advances over a
  full horizontal cycle, the rendered frames must keep changing in a varied way
  rather than snapping back to an earlier frame every few steps. (Spacing the
  sheep on a naive even lattice can make the whole field shift-symmetric, so the
  frame repeats every few `t` — that is **not** acceptable. How you avoid this is
  up to you.)

## CLI
`electric_sheep/cli.py` exposes `main(argv: list[str] | None = None) -> int`:
- `--count N`  (default 3; validated to 1..20)
- `--speed S`  (default 1.0; must be > 0)
- `--frames F` (default 0 = loop forever; F > 0 renders exactly F frames then
  exits — makes it runnable non-interactively)
- Per tick: clear the screen, print `render_frame(count, speed, t)`, sleep
  briefly, increment `t`.
- Invalid args print a helpful message to stderr and return exit code 2.
- `python -m electric_sheep --count 5 --speed 2 --frames 30` runs a finite
  animation and exits 0. (Provide `electric_sheep/__main__.py` so `-m` works.)

## Acceptance criteria — `tests/test_scene.py`
`tests/test_scene.py` MUST be runnable under pytest and MUST contain the
following tests **exactly as written below**. You may add further tests, but
these specific tests and their assertions are the acceptance contract — they
must be present and **unmodified** (do not rename them, weaken the assertions,
change the constants, or delete any). A submission that alters or omits them does
not satisfy this spec.

```python
import pytest

from electric_sheep.scene import render_frame

WIDTH, HEIGHT = 40, 12


def test_determinism_same_args():
    assert render_frame(5, 1.0, 7) == render_frame(5, 1.0, 7)


def test_frame_shape():
    frame = render_frame(3, 1.0, 4, width=WIDTH, height=HEIGHT)
    lines = frame.split("\n")
    assert len(lines) == HEIGHT
    assert all(len(line) == WIDTH for line in lines)


def test_fence_every_row():
    frame = render_frame(3, 1.0, 4, width=WIDTH, height=HEIGHT)
    for line in frame.split("\n"):
        assert line[WIDTH // 2] == "|"


def test_all_sheep_visible_every_count():
    # Every valid count must render exactly `count` distinct 'S' glyphs.
    for count in range(1, 21):
        frame = render_frame(count, 1.0, 0, width=WIDTH, height=HEIGHT)
        assert frame.count("S") == count, (
            f"count={count}: expected {count} 'S' glyphs, got {frame.count('S')}"
        )


def test_no_short_visual_period():
    # The animation must not repeat with a short period. Over a full horizontal
    # cycle, render_frame must produce many distinct frames; a renderer that
    # collapses the field onto a shift-symmetric lattice repeats every few steps.
    for count, speed in [(5, 2.0), (8, 3.0)]:
        frames = [
            render_frame(count, speed, t, width=WIDTH, height=HEIGHT)
            for t in range(WIDTH)
        ]
        distinct = len(set(frames))
        assert distinct >= 10, (
            f"count={count}, speed={speed}: only {distinct} distinct frames over "
            f"{WIDTH} steps — animation repeats with a short period"
        )


def test_motion_advances_with_time():
    assert render_frame(4, 1.0, 0) != render_frame(4, 1.0, 1)


def test_jump_arc_higher_near_fence():
    # A sheep near the fence sits on a smaller (higher) row than one far from it.
    frame = render_frame(6, 1.0, 0, width=WIDTH, height=HEIGHT)
    fence = WIDTH // 2
    positions = [
        (r, c)
        for r, line in enumerate(frame.split("\n"))
        for c, ch in enumerate(line)
        if ch == "S"
    ]
    assert len(positions) == 6
    near = min(positions, key=lambda rc: abs(rc[1] - fence))
    far = max(positions, key=lambda rc: abs(rc[1] - fence))
    assert near[0] <= far[0]


def test_validation():
    for bad_count in (0, 21):
        with pytest.raises(ValueError):
            render_frame(bad_count, 1.0, 0)
    for bad_speed in (0.0, -1.0):
        with pytest.raises(ValueError):
            render_frame(5, bad_speed, 0)
```

### Additional criteria
- `scene.py` is pure (no `time` / `random` / `input` usage).
- No `# type: ignore` and no bare `except:` in production code.
- `python -m electric_sheep --count 5 --speed 2 --frames 10` runs to completion
  and exits 0.
- `README.md` documents how to run it and what `--count` / `--speed` do.

Out-of-scope-but-don't-block: ANSI color, terminal-size autodetection, a richer
sheep sprite.
