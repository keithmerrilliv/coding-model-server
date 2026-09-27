# scripts/

Operational tools for this repository. Every script here is live; one-off
investigations that have served their purpose are in `archive/`, kept for
history and not maintained.

Run Python scripts from the repo root with `venv/bin/python scripts/<name>`.

## Release and merge

| Script | What it does |
| --- | --- |
| `merge_gate.sh <branch>` | The local merge gate: merges a branch into `main` and runs ruff, mypy and pytest on the **merged** tree, then pushes. Refuses a dirty tree. Excludes `network`-marked tests; CI runs them. |
| `release_tag.sh vX.Y.Z [--execute]` | Dates the CHANGELOG heading, tags, pushes and creates the GitHub release. Dry run by default. Needs green CI on `HEAD` and a `## vX.Y.Z — unreleased` heading. |
| `check_mermaid.mjs [repo]` | Parses every mermaid diagram in `README.md` and `docs/*.md`; exits 1 on a parse error. Uses the dashboard's mermaid (`cd dashboard && npm ci`). Run it after editing a diagram. |

## Deploy and hosts

| Script | What it does |
| --- | --- |
| `redeploy.sh [--restart-only]` | Syncs the systemd units and restarts the Linux services. `--restart-only` needs no sudo. Deploys nothing to the Mac: see [docs/MAC_RUNNER.md](../docs/MAC_RUNNER.md). |
| `mac_update_runner.sh [--execute]` | Run **on the Mac**, from the runner's checkout: pulls, reclaims leaked tart VMs, restarts the runner. Dry run by default. The other half of every deploy that touches `mac_runner/`. |
| `reclaim_tart_vms.sh` | Run on the Mac: deletes leaked `coding-model-runner` tart VMs. Called by `mac_update_runner.sh` and the runner itself. |
| `env_keys.sh [file]` | Lists the keys an `.env` sets, with value lengths only, never values. |
| `enable_rapl_reading.sh` | Lets the resource monitor read CPU package power (needs sudo once). |
| `monitor_resources.py` | GPU, CPU and power sampler behind `coding-model-monitor.service`. |
| `serve_dashboard.py` | Static server for the built dashboard (`dashboard/dist`); behind `coding-model-dashboard.service`. |

## Pipeline analysis

| Script | What it does |
| --- | --- |
| `failure_taxonomy.py` | Failures by class, agent and retry index, from the event store. |
| `agent_cost_stats.py` | Wall-clock and token cost per attempt, by agent and role. |
| `pipeline_branches.py <path-to-repo>` | Inventories a target repo's open `pipeline/*` delivery branches and lists the tests each would delete if merged as-is. Read-only. |
| `replay_testability_check.py [ref]` | Replays the design testability and completeness checks over every archived design, on `ref` vs the working tree, and prints what appears and disappears. **Run it before shipping any change to `design_testability.py`.** |
| `sweep_swift_prechecks.py` | The same idea for the Swift prechecks. |
| `auto_approve_gates.py <spec>` | Approves every gate of one spec in a loop. **Bypasses the human gates**; refuses unless `CODING_MODEL_ALLOW_AUTO_APPROVE=1`. Test drives only. |

## Models, serving and evals

| Script | What it does |
| --- | --- |
| `download_models.py` | Downloads model GGUFs from Hugging Face. |
| `benchmark_prefill.py`, `benchmark_decode.py` | Prefill and decode speed through the real server path. |
| `benchmark_builds.py` | A/B two `llama-server` builds on the same model and flags. |
| `_llama_bench.py` | Shared harness for the benchmarks above. |
| `sweep_cpu_moe.py` | Sweeps `--n-cpu-moe` per agent to find each model's expert-offload point. |
| `eval_agents.py` + `eval_tasks.json` | Blind, counterbalanced head-to-head between two agents, scored by an external judge. |

## RAG maintenance

| Script | What it does |
| --- | --- |
| `rag_utils.py` | Inspect and query the memory database. |
| `cleanup_memory.py` | Removes low-quality entries. |
| `purge_bulk_code.py` | Removes bulk-ingested code samples. |
| `update_deep_docs_mcp.sh` | Updates the vendored Apple Deep Docs MCP and keeps the update only if it still works; behind `coding-model-mcp-update.timer`. |
