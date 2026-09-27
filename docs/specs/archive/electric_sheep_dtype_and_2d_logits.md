# Accept float16/bfloat16 logits and fix 2-D indexing in forcing strategies

Jira: DEV-585 (critical), DEV-587 (high). These two defects mask each other and MUST be
fixed together in this one spec.

## Context

Repo: `electric-sheep` (registered in the Mac runner's repos.yml). Language: Swift.
macOS/visionOS SwiftUI app; generation runs through MLX (mlx-swift-examples / MLXLMCommon).

Production logits arrive at `LogitProcessor.process` as **2-D `[1, vocab]`** (MLXLMCommon's
`TokenIterator.convertToToken` slices `logits[0..., -1, 0...]`) and, for the three shipped
mlx-community 4-bit models, in **float16 or bfloat16** — never float32.

## Problem

Two coupled defects:

1. `DtypeValidation.swift:37-43` — every strategy begins with
   `guard logitsAreFloat32(logits) else { return logits }`. Since production logits are
   float16/bfloat16, **every** `corrupt()` is a no-op and `MLXFailureRouter` reports an
   error per token (~20-30/sec). The app's core feature (forced hallucinations) is dead.

2. `ForcingStrategy.swift` — three strategies index axis 0 as if logits were 1-D. Current
   code (verbatim from main):

   - `RepetitionBoostStrategy` (lines 68-73):
     ```swift
     let result = logits
     for tokenID in context.recentTokens.suffix(10) {
         let idx = MLXArray(Int32(tokenID))
         let current = result[idx]
         result[idx] = current + MLXArray(boostAmount)
     }
     ```
     On `[1, vocab]` this gathers/scatters **row `tokenID`** of a 1-row array — OOB.

   - `ContextCorruptionStrategy` (lines 88-93): `result[indices]` /
     `result[shuffledIndices]` with random indices in `[0, vocabSize)` — same OOB row
     access.

   - `RestrictedSamplingStrategy` (lines 130-135):
     ```swift
     let sorted = argSort(logits, axis: -1)
     ...
     let threshold = logits[sorted[cutoffIdx]]
     ```
     `sorted[cutoffIdx]` selects an OOB **row**; the subsequent gather would produce a
     `[vocab, vocab]` array (~90 GB at 150k vocab).

   These are currently unreachable only because defect 1 short-circuits first. Fixing the
   dtype guard alone detonates them.

## Required change

1. At the single entry point where strategies receive logits (in `HallucinationForcer`'s
   `process`, before dispatching to the strategy — NOT duplicated in all 8 strategies):
   if `logits.dtype` is `.float16` or `.bfloat16`, convert to `.float32` with `asType`,
   run the strategy, then convert the result back to the original dtype before returning.
   Keep the existing containment path (report + pass-through) for any other dtype
   (e.g. float64), preserving current `MLXFailureRouter` behavior for genuinely
   unsupported dtypes.
2. Make all three broken strategies operate on the **last axis** so they are correct for
   both 1-D `[vocab]` (existing unit tests) and 2-D `[1, vocab]` (production) shapes.
   Do not change the mathematical intent of any strategy.
3. Keep `RestrictedSamplingStrategy`'s `-1e9` mask sentinel and top-k semantics exactly
   as established by DEV-208.

## Acceptance criteria

A green build is NOT sufficient — the tests below are the gate.

- Build succeeds for the macOS target with no new warnings.
- New tests construct **`[1, vocab]` float16 AND bfloat16** logits and, for EVERY one of
  the 8 strategies, assert: output shape equals input shape, output dtype equals input
  dtype, and the corrupted distribution differs from the input (e.g. KL > 0 or any
  element changed) for strategies that are unconditionally active. These tests must FAIL
  against current `main` (strategies return input unchanged) and pass after the fix —
  say so explicitly in the report.
- New tests for the three fixed strategies on `[1, vocab]`: RepetitionBoost boosts
  exactly the recent-token columns; ContextCorruption changes only ~shuffleFraction of
  columns; RestrictedSampling keeps top-k probability mass ≈ 1.0 and every non-top-k
  probability < 1e-6 (mirroring the existing 1-D test from DEV-208).
- Existing 1-D ForcingStrategyTests still pass unchanged.
- No `MLXFailureRouter` report is emitted for float16/bfloat16 input (assert via an
  installed test handler); float64 input still reports and passes through unchanged.

## test_strategy

    framework: xcodebuild_test
    required: true
    repo: electric-sheep
    scheme: ElectricSheep
    destination: "platform=macOS"

## Constraints

- No new dependencies.
- Do not modify `GenerationTaskController`, `HallucinationEngine` state handling, or any
  rendering code in this spec.
- The dtype conversion must happen once per token, not once per strategy invocation
  inside a strategy.

## Risks

- `asType` round-trip (f16 → f32 → f16) quantizes corrupted logits; acceptable — sampling
  operates at f16 precision anyway. Do not attempt to skip the round-trip by making
  strategies dtype-generic; several rely on float32 math.
- MLX subscript/gather semantics differ between 1-D and 2-D arrays; verify against the
  checked-out mlx-swift sources rather than assuming (prior incident: DEV-492).
- Tests should keep vocab small (e.g. 64) so no real model download is needed.
