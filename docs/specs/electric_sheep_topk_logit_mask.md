# Fix top-k logit mask in RestrictedSamplingStrategy

## Context

Repo: `electric-sheep` (registered in the Mac runner's repos.yml). Language: Swift.
macOS/visionOS SwiftUI app; generation runs through MLX.

## Problem

`RestrictedSamplingStrategy.corrupt` in ForcingStrategy.swift masks non-top-k logits with
`Float.leastNormalMagnitude * -1`, which is numerically indistinguishable from zero — so the
"restricted" sampling doesn't actually restrict anything: masked tokens keep essentially
their original softmax probability.

## Required change

Replace the ineffective mask value so that non-top-k logits have near-zero softmax
probability (~-inf behavior). Use `-Float.greatestFiniteMagnitude` or a large negative
sentinel (`-1e9`), whichever keeps MLX numerics stable without producing NaN when an entire
row could be masked.

Change only the mask value and anything strictly needed for numeric safety — do not alter
top-k selection logic, other forcing strategies, or the strategy's public API.

## Acceptance criteria

A green build is NOT sufficient — the tests below are the gate.

- Build succeeds for the macOS target with no new warnings.
- A unit test constructs a known logits vector with known top-k, applies `corrupt` then
  softmax, and asserts every non-top-k probability < 1e-6 while top-k probabilities
  sum ≈ 1.0. This test must fail against current `main` and pass after the fix — say so
  explicitly in the report.
- A unit test asserts no NaN or infinite values appear in the post-softmax distribution,
  including edge cases where k equals vocabulary size and where k = 1.
- A test asserts top-k selection indices are unchanged by the fix (same indices retained
  before and after).

## test_strategy

    framework: xcodebuild_test
    required: true
    repo: electric-sheep
    scheme: ElectricSheep
    destination: "platform=macOS"

## Constraints

- No new dependencies.
- Change only the mask value and anything strictly needed to keep it numerically safe.

## Risks

- If k equals vocabulary size, every logit is top-k so none are masked — ensure this path
  does not introduce NaN from masking logic applied vacuously.
- Using `-Float.greatestFiniteMagnitude` could overflow in MLX softmax if combined with
  other large values; `-1e9` may be safer but needs verification that it still drives
  probability below 1e-6.
- Existing tests or code elsewhere may depend on the old mask sentinel value indirectly.
