# RPN Calculator Core (DEV-420 dogfood)

## Context

Brand-new standalone project — there is no existing repository. Build a small,
dependency-free reverse-Polish-notation calculator core that evaluates an
expression supplied as a string and returns the numeric result.

Write it in TypeScript (or plain ES modules) — whichever suits a zero-build-step
project best. It runs headless under Node's built-in test runner; there is no UI
and no browser code.

## Required behavior

- `evaluate(expr)` takes a whitespace-separated RPN string and returns a number.
  `"3 4 +"` → `7`. `"5 1 2 + 4 * + 3 -"` → `14`.
- Supports `+`, `-`, `*`, `/` and negative numeric literals (`-2.5`).
- Division by zero throws an `Error` whose message contains `division by zero`.
- Malformed input throws an `Error`: too few operands for an operator, an
  unrecognised token, or leftover operands once the stream is consumed.
- An empty or whitespace-only expression throws an `Error`.
- The evaluator is pure — no I/O, no globals, no mutation of its argument.

## Testing

Unit tests run under `node --test` with Node's built-in `node:assert`. Cover each
listed behaviour including every error path. No network access at test time.

## Constraints

- Zero runtime dependencies. No bundler, no transpiler, no build step.
- A single module exporting `evaluate`, plus its test file.
