"""DEV-599: the journal names the role a context fetch served.

Written by run 23 against the per-role fetch helper; since DEV-632 the
fetch belongs to the context stage and the role line is emitted when a role
SELECTS from it, so the same four criteria are asserted against the stage.
"""
import inspect
import logging
import types

import coding_model_server.orchestrator_daemon as d


def _plan(monkeypatch):
    monkeypatch.setattr(d, "_load_plan", lambda s: {"test_strategy": {"repo": "coding-model-server"}})


def test_r1_architect_role_logs_correctly(monkeypatch, caplog):
    """R1: fetch returns one file, role="architect" — log contains 'to the architect:'"""
    _plan(monkeypatch)
    monkeypatch.setattr(d.test_runner, "fetch_repo_files", lambda repo, paths, ref: ([("src/x.py", "content")], []))
    caplog.set_level(logging.INFO, logger="orchestrator")

    view = d._spec_context(None, types.SimpleNamespace(id="spec_t"), "md", role="architect",
                           extra_candidates=["src/x.py"]).select("architect")

    assert "supplied 1 existing file(s) to the architect:" in caplog.text
    assert "to the implementer" not in caplog.text
    assert view.existing_files == [("src/x.py", "content")]


def test_r2_default_implementer_role_logs_correctly(monkeypatch, caplog):
    """R2: same fetch selected by the implementer — log contains 'to the implementer:'"""
    _plan(monkeypatch)
    monkeypatch.setattr(d.test_runner, "fetch_repo_files", lambda repo, paths, ref: ([("src/x.py", "content")], []))
    caplog.set_level(logging.INFO, logger="orchestrator")

    view = d._spec_context(None, types.SimpleNamespace(id="spec_t"), "md", role="implementer",
                           extra_candidates=["src/x.py"]).select("implementer")

    assert "supplied 1 existing file(s) to the implementer:" in caplog.text
    assert view.existing_files == [("src/x.py", "content")]


def test_r3_architect_read_failure_logs_correctly(monkeypatch, caplog):
    """R3: fetch raises, role="architect" — log contains 'the architect will not see...' and the section is empty"""
    _plan(monkeypatch)

    def boom(repo, paths, ref):
        raise RuntimeError("runner down")

    monkeypatch.setattr(d.test_runner, "fetch_repo_files", boom)
    caplog.set_level(logging.WARNING, logger="orchestrator")

    view = d._spec_context(None, types.SimpleNamespace(id="spec_t"), "md", role="architect",
                           extra_candidates=["src/x.py"]).select("architect")

    assert "the architect will not see the files it must modify" in caplog.text
    assert view.existing_files == []


def test_r4_the_stage_is_the_only_fetch_site():
    """R4 (DEV-632): the daemon calls the runner's read path from exactly one
    function — the context stage accessor — so no role can grow its own."""
    src = inspect.getsource(d)
    sites = [ln for ln in src.splitlines() if "fetch_repo_files" in ln]
    assert sites == ["        fetch=test_runner.fetch_repo_files)"], sites
    assert "role: str" in inspect.getsource(d._spec_context)
