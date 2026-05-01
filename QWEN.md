# Qwen Server — Project Notes

This file is kept short on purpose. For day-to-day state see:

- `README.md` — what the project is, how to install/run, file layout
- `docs/CONFIGURATION.md` — env vars, agent-config knobs, systemd services
- `~/.claude/.../memory/MEMORY.md` — VRAM budget, calibrations, action items
  (Claude Code's project memory — not committed to this repo)

## Architecture in one paragraph

FastAPI inference server (`server.py`) + autonomous orchestrator daemon
(`orchestrator_daemon.py`). Most agents run on the `llama_server`
subprocess backend with `--cpu-moe` (attention sublayers on GPU,
experts on CPU); `debugger` and `fast_implementer` still use the
in-process `llama_cpp` backend. A modular client (`qwen_client/`)
streams completions, executes tool markers (`<<<REMOTE_EXEC>>>`,
`<<<WRITE_FILE>>>`, etc.) on the operator's machine, and supports
three permission modes (`default` / `acceptEdits` / `yolo`).
Autonomous mode runs `architect → implementer → reviewer` against
specs, with bwrap-sandboxed test execution and Jira sync.

## When to look elsewhere

- Modifying agent VRAM tuning → `MEMORY.md`'s VRAM Budget section + the
  per-agent `_create_model_config` calls in `server.py`.
- Adding a new agent → mirror the closest existing pattern in
  `server.py`'s `Config.AGENTS`; bump `AUTONOMOUS_*_AGENT` env in
  `~/.config/qwen-server/.env` if it should be the autonomous default.
- Sandbox / security → `qwen-orchestrator.service`,
  `qwen_autonomous/executor.py::_run_local_tests`, and
  `~/.claude/.../memory/project_security_actionables.md`.
- Anything else → `git log --grep '<keyword>'` is usually faster than
  documentation.
