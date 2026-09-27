# Working on coding-model-server

Orientation for a coding agent (or a person) about to change this repository.
[README.md](README.md) says what the project is; this says how the code is laid
out and how a change gets in.

## The three packages

All under `src/`, installed editable with `pip install -e .`, so a service
restart picks up a code change with no reinstall.

- **`coding_model_autonomous`** — the pipeline, and the project's centre of
  gravity. The kernel (`workspace`, `outcome`, `context`, `retry_policy`) makes
  the decisions; `executor` holds the agents' prompts and parsers; the guards
  (`design_testability`, `swift_prechecks`, `swift_rules`) check designs and
  generated code; `test_runner` and `delivery` run tests and push results.
- **`coding_model_server`** — the FastAPI inference server (`server.py`,
  `routes/`, `llama_server.py`) and the orchestrator daemon
  (`orchestrator_daemon.py`), which drives specs through the pipeline. Both
  run under systemd.
- **`coding_model_client`** — the interactive chat client. `tool_handlers/`
  lives in the server package but is imported and run **by the client**, on
  the operator's machine; the server never executes a tool.

`mac_runner/` is a fourth, separate service that runs Swift and Xcode tests on
a Mac. It deploys there and nowhere else; see [docs/MAC_RUNNER.md](docs/MAC_RUNNER.md).

## Where things live

| To change | Look in |
| --- | --- |
| The spec lifecycle, gates, failure routing | [docs/PIPELINE.md](docs/PIPELINE.md) first (the map), then `orchestrator_daemon.py` |
| How a failure is classified and charged | `coding_model_autonomous/outcome.py` |
| Which agent retries, and what a retry keeps | `coding_model_autonomous/retry_policy.py` |
| What each role is shown, and the prompt budget | `coding_model_autonomous/context.py` |
| Agent prompts and response parsing | `coding_model_autonomous/executor.py` |
| The agent roster, model configs, VRAM numbers | `coding_model_server/config.py` (each model config's comments record the measurements behind it) |
| Model loading, swapping, VRAM admission | `coding_model_server/llama_server.py` |
| Env vars and their defaults | [docs/CONFIGURATION.md](docs/CONFIGURATION.md) |
| Operational scripts | [scripts/README.md](scripts/README.md) |

## How a change gets in

1. **Merge through `bash scripts/merge_gate.sh <branch>`.** It merges into
   `main` and runs ruff, mypy and pytest on the merged tree, then pushes. It
   refuses a dirty tree. Never hand-type the three checks instead.
2. **The suite is green with no `--deselect`.** A red test is real until
   proven otherwise.
3. **Measure a guard change on the spec archive before shipping it.**
   `scripts/replay_testability_check.py` replays every archived design through
   the design checks, on `main` and on your tree, and prints what appears and
   disappears. A guard that stops firing on its false positives must still fire
   on at least one known-true case.
4. **Redeploy when nothing is in flight**, or at a human gate:
   `bash scripts/redeploy.sh --restart-only`. That redeploys the Linux server
   only; `mac_runner/` changes need a pull on the Mac.
5. **Write comments that explain why the code looks the way it does**, not what
   it used to be. The story of a change belongs in its commit and the
   CHANGELOG; a comment that narrates history goes stale and buries the
   invariant next to it.

## Before you trust a number

- **An empty result usually means "not recorded", not "did not happen".** The
  design testability check emits no event when it finds nothing, so a clean
  pass is proven by replaying the stored design, not by the absence of an event.
- **A green `swift_test` run says nothing about the Mac's VM.** Only
  `xcodebuild_test` runs in it.
- **Anything else:** `git log --grep '<keyword>'` is usually faster than the docs.
