# Coding Model Server — Project Notes

This file is kept short on purpose. For day-to-day state see:

- `README.md` — what the project is, how to install/run, file layout
- `docs/CONFIGURATION.md` — env vars, agent-config knobs, systemd services
- `~/.claude/.../memory/MEMORY.md` — VRAM budget, calibrations, action items
  (Claude Code's project memory — not committed to this repo)

## Architecture in one paragraph

Three packages under `src/`, installed via `pip install -e .`:
`coding_model_server` (FastAPI inference server + orchestrator daemon +
shared modules), `coding_model_client` (chat client), `coding_model_autonomous`
(autonomous task store + agents). The server runs as
`python -m coding_model_server.server`; the orchestrator as
`python -m coding_model_server.orchestrator_daemon` (both via systemd).
EVERY agent runs on the `llama-server` subprocess backend — the in-process
`llama_cpp` backend was retired in April 2026 and there is no backend switch
left. Most agents use expert offload (`--cpu-moe`, or `--n-cpu-moe N` for a
partial split: attention sublayers on GPU, experts on CPU). The client streams
completions, executes tool markers (`<<<REMOTE_EXEC>>>`, `<<<WRITE_FILE>>>`,
etc.) on the operator's machine, and supports three permission modes
(`default` / `acceptEdits` / `yolo`). Autonomous mode runs
`planner → architect → implementer → reviewer` against specs, gated on human
approval at each transition, with bwrap+seccomp-sandboxed test execution and
Jira sync.

## Where things live

- Agent registry, model configs, system prompts → `src/coding_model_server/config.py`
  (`Config.AGENTS`, `_create_model_config`). NOT `server.py` — that is now just
  app assembly and router wiring.
- HTTP endpoints → `src/coding_model_server/routes/` (chat, memory, autonomous,
  admin, meta).
- Tool execution → `src/coding_model_server/tool_handlers/`. Lives in the server
  package but is imported and run BY THE CLIENT, on the operator's machine.

## When to look elsewhere

- Modifying agent VRAM tuning → `MEMORY.md`'s VRAM Budget section + the
  per-agent `_create_model_config` calls in `src/coding_model_server/config.py`.
  Each config's comment block records the measurements behind its numbers.
- Adding a new agent → mirror the closest existing pattern in
  `Config.AGENTS`; bump `AUTONOMOUS_*_AGENT`
  env in `~/.config/coding-model-server/.env` if it should be the autonomous
  default.
- Sandbox / security → `coding-model-orchestrator.service`,
  `src/coding_model_autonomous/executor.py::_run_local_tests`,
  `src/coding_model_autonomous/seccomp_filter.py`, the shell allow-list in
  `src/coding_model_server/tool_handlers/safety.py`, and
  `~/.claude/.../memory/project_security_actionables.md`.
- Anything else → `git log --grep '<keyword>'` is usually faster than
  documentation.
