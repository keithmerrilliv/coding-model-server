# RETIRED — Make temporalDistortion velocity warp frame-rate independent

> **WITHDRAWN FROM THE DOGFOOD ROTATION 2026-08-13. Do not submit this spec.**
> Fixed by hand instead: ElectricSheep `main` @ a0f9318, verified on hardware
> (33 tests, 0 failures). DEV-596 is closed.
>
> Why it was withdrawn rather than re-run: the acceptance criteria below are
> mutually unsatisfiable, including by this document's own reference formula.
> Run 16 (spec_b3b3fe6e) produced a correct one-line implementation and honest
> tests, and the tests failed on the mathematics.
>
> * "Net velocity scale returns to ~1 over a full cycle" is impossible for
>   `pow(1 + 0.3*sin, dt*90)`: `ln(1 + 0.3*sin)` has a negative mean, so the
>   scale decays to 0.0014 per cycle, not 1.0. An `exp(k*sin*dt)` form does
>   integrate to 1 (measured 0.9994) — but then it no longer reproduces the
>   90 Hz behaviour this document also demands be preserved.
> * "Final speeds agree within 5% at 30 Hz vs 90 Hz" is unreachable at any
>   window with either form (5.7% at 0.1 s, 22% at 2 s), because velocity grows
>   ~1e8 over two seconds and any sampling difference is amplified exponentially.
>   Preserving pathological 90 Hz behaviour and matching it at other rates are
>   contradictory goals when the pathology *is* the exponential runaway.
>
> The shipped fix resolves this by adding what the spec never specified: a speed
> clamp (`maxTemporalSpeed`, mirroring DEV-205's `maxGenesisSize`). Bounding the
> warp is what makes frame-rate correctness meaningful.
>
> Lesson for spec authors: an acceptance criterion asserting a mathematical
> property ("symmetric", "no secular growth", "returns to ~1") needs to be
> checked numerically before the spec is written, not discovered by a run.

Jira: DEV-596 (high). Same bug class as DEV-204/DEV-205 (fixed for demo spawn and
genesis growth in commit 1ea928e); this site was missed.

## Context

Repo: `electric-sheep`. `HallucinationSimulator.swift`, per-particle physics switch in
the update path. The simulator is stepped by the app's bridge loop at ~90 Hz with real
`dt`, and existing tests (from DEV-204/205) step it at different rates to assert
frame-rate independence for spawn and genesis behaviors.

## Problem

Lines 211-214 (verbatim from main):

```swift
case .temporalDistortion:
    // Warping time - accelerating/decelerating based on temporal phase
    let temporalPhase = sin(time * 2.0)
    particle.velocity *= (1.0 + temporalPhase * 0.3)
```

The multiplier is applied per FRAME with no `dt`. At 90 Hz, over the ~1.6 s positive
half-period of `sin(time * 2)`, velocity compounds by roughly (1.3)^141 ≈ 10^16 — in
practice with varying phase ≈ e^(0.17·141) ≈ 10^10 — teleporting particles to positions
~10^7+ where they are invisible for the rest of their lifetime. At 30 Hz the compounding
is orders of magnitude smaller: behavior is wildly frame-rate dependent.

## Required change

Replace the per-frame multiply with a dt-corrected form that produces the same velocity
scaling per unit time at any frame rate. Reference formulation (implementer may use an
equivalent):

```swift
let temporalPhase = sin(time * 2.0)
// Continuous-time growth rate; 90 Hz was the historical tuning reference.
particle.velocity *= pow(1.0 + temporalPhase * 0.3, dt * 90.0)
```

Tune so that behavior at 90 Hz matches current behavior at 90 Hz (the visual has been
accepted at that rate); the fix is that 30/45/120 Hz now match it.

## Acceptance criteria

A green build is NOT sufficient — the tests below are the gate.

- Build succeeds for the macOS target with no new warnings.
- Frame-rate-independence test in the DEV-204/205 style: seed a `.temporalDistortion`
  particle with known velocity, hold `time` progression identical, step one simulator
  2 s at dt=1/90 and another 2 s at dt=1/30; final speeds agree within 5%. This test
  FAILS against current `main` by orders of magnitude — say so explicitly in the report.
- Boundedness test: over a full 2π phase cycle at 90 Hz the net velocity scale returns
  to ≈1 (symmetric warp, no secular growth), tolerance 10%.
- Existing DEV-204/205 tests (demo spawn credit, genesis growth clamp) still pass
  unchanged.

## test_strategy

    framework: xcodebuild_test
    required: true
    repo: electric-sheep
    scheme: ElectricSheep
    destination: "platform=macOS"

## Constraints

- No new dependencies.
- Change ONLY the `.temporalDistortion` case; do not retune other particle physics.
- Keep the `sin(time * 2.0)` phase source so the audio/visual coupling timing is
  unchanged.

## Risks

- `pow` per particle per frame is slightly more expensive; with ≤3000 particles this is
  negligible — do not micro-optimize with approximations that break the tests.
- If other physics cases implicitly relied on temporalDistortion particles being flung
  out of view (e.g. density metrics), the fix increases visible particle counts;
  simulator caps (maxParticles) already bound this.
