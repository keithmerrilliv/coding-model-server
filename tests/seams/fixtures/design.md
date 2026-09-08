# Architecture: Existing-file fetch role parameterization

## Overview
Add a keyword-only `role` parameter (default `"implementer"`) to `_fetch_existing_files_for_spec` in `src/coding_model_server/orchestrator_daemon.py`, make its three role-naming log messages use that value via percent-style formatting arguments, and have `_run_architect` pass `role="architect"`. Journal lines then say which agent a fetch served (DEV-599 residual). Two files change: the daemon module and one new test file.

## Components
1. **Orchestrator Daemon (`src/coding_model_server/orchestrator_daemon.py`)**
   - `_fetch_existing_files_for_spec`: signature gains `*, role: str = "implementer"` after `extra_paths`.
   - The three log calls inside it that name the implementer take `role` as a formatting argument (exact text below).
   - `_run_architect`: its existing call to the helper gains `role="architect"`. No other call site changes.

2. **Test Suite (`tests/test_existing_fetch_role_log.py`)**
   - New pytest file covering R1–R4; hermetic (no runner, no model, no plan.yaml on disk).

## File Structure
```
src/
  coding_model_server/
    orchestrator_daemon.py          # Modified: signature + 3 log calls + 1 call site
tests/
  test_existing_fetch_role_log.py   # New: hermetic tests for R1-R4
```

## Data Models
None added. `_fetch_existing_files_for_spec` still returns `list[tuple[str, str]]`; exception behaviour and the DEV-620 `RunnerOutageAtImplement` path are untouched.

## Implementation Notes
1. **Signature**: `def _fetch_existing_files_for_spec(spec: Spec, spec_md: str, extra_paths: "tuple | list" = (), *, role: str = "implementer") -> list[tuple[str, str]]:`
2. **The three log calls** keep percent-style formatting; only the role word becomes an argument. Exact forms:
   - Read-failed warning: `logger.warning("spec %s: existing-file read failed (%s); the %s will not see the files it must modify", spec.id, e, role)`
   - Supplied info line: `logger.info("spec %s: supplied %d existing file(s) to the %s: %s", spec.id, len(files), role, ", ".join(p for p, _ in files))`
   - Working-blind warning: `logger.warning("spec %s: %d file(s) marked modify but none could be read — %s is working blind", spec.id, len(declared), role)`
   With the default, every message is byte-identical to today.
3. **Call site**: in `_run_architect`, the call `_fetch_existing_files_for_spec(spec, spec_md, extra_paths=_planned_implement_outputs(spec))` becomes `_fetch_existing_files_for_spec(spec, spec_md, extra_paths=_planned_implement_outputs(spec), role="architect")`.
4. **Invariant**: the helper still returns `[]` on a plain fetch exception after logging, and still raises `RunnerOutageAtImplement` only on the DEV-620 transport-outage path.
5. **Test setup** (shared by R1–R3): `import coding_model_server.orchestrator_daemon as d`; a spec stand-in `spec = types.SimpleNamespace(id="spec_t")`; `monkeypatch.setattr(d, "_load_plan", lambda s: {"test_strategy": {"repo": "coding-model-server"}})`; the fetch stub returns the pair `([("src/x.py", "content")], [])`; `caplog.set_level(logging.INFO, logger="orchestrator")`; every call passes `extra_paths=["src/x.py"]`.

## Acceptance Criteria Checklist
- [ ] R1: fetch returns one file, `role="architect"` — caplog contains `supplied 1 existing file(s) to the architect:` and not `to the implementer`
- [ ] R2: same fetch, no `role` argument — caplog contains `to the implementer:`
- [ ] R3: fetch raises, `role="architect"` — caplog contains `the architect will not see the files it must modify` and the helper returns `[]`
- [ ] R4: `inspect.signature` shows `role` keyword-only with default `"implementer"`

## Criterion Seams
- R1 | setup: `monkeypatch.setattr(d, "_load_plan", lambda s: {"test_strategy": {"repo": "coding-model-server"}}); monkeypatch.setattr(d.test_runner, "fetch_repo_files", lambda repo, paths, ref: ([("src/x.py", "content")], [])); caplog.set_level(logging.INFO, logger="orchestrator")` | act: `result = d._fetch_existing_files_for_spec(types.SimpleNamespace(id="spec_t"), "md", extra_paths=["src/x.py"], role="architect")` | assert: `assert "supplied 1 existing file(s) to the architect:" in caplog.text; assert "to the implementer" not in caplog.text; assert result == [("src/x.py", "content")]`
- R2 | setup: `same as R1` | act: `result = d._fetch_existing_files_for_spec(types.SimpleNamespace(id="spec_t"), "md", extra_paths=["src/x.py"])` | assert: `assert "supplied 1 existing file(s) to the implementer:" in caplog.text`
- R3 | setup: `monkeypatch.setattr(d, "_load_plan", lambda s: {"test_strategy": {"repo": "coding-model-server"}}); def boom(repo, paths, ref): raise RuntimeError("runner down"); monkeypatch.setattr(d.test_runner, "fetch_repo_files", boom); caplog.set_level(logging.INFO, logger="orchestrator")` | act: `result = d._fetch_existing_files_for_spec(types.SimpleNamespace(id="spec_t"), "md", extra_paths=["src/x.py"], role="architect")` | assert: `assert "the architect will not see the files it must modify" in caplog.text; assert result == []`
- R4 | setup: `(none)` | act: `param = inspect.signature(d._fetch_existing_files_for_spec).parameters["role"]` | assert: `assert param.kind is inspect.Parameter.KEYWORD_ONLY; assert param.default == "implementer"`