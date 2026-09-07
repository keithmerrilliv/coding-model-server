import inspect
import logging
import types

import coding_model_server.orchestrator_daemon as d


def test_r1_architect_role_logs_correctly(monkeypatch, caplog):
    """R1: fetch returns one file, role="architect" — log contains 'to the architect:'"""
    monkeypatch.setattr(d, "_load_plan", lambda s: {"test_strategy": {"repo": "coding-model-server"}})
    monkeypatch.setattr(d.test_runner, "fetch_repo_files", lambda repo, paths, ref: ([("src/x.py", "content")], []))
    caplog.set_level(logging.INFO, logger="orchestrator")

    result = d._fetch_existing_files_for_spec(
        types.SimpleNamespace(id="spec_t"), "md", extra_paths=["src/x.py"], role="architect"
    )

    assert "supplied 1 existing file(s) to the architect:" in caplog.text
    assert "to the implementer" not in caplog.text
    assert result == [("src/x.py", "content")]


def test_r2_default_implementer_role_logs_correctly(monkeypatch, caplog):
    """R2: same fetch, no role argument — log contains 'to the implementer:'"""
    monkeypatch.setattr(d, "_load_plan", lambda s: {"test_strategy": {"repo": "coding-model-server"}})
    monkeypatch.setattr(d.test_runner, "fetch_repo_files", lambda repo, paths, ref: ([("src/x.py", "content")], []))
    caplog.set_level(logging.INFO, logger="orchestrator")

    result = d._fetch_existing_files_for_spec(
        types.SimpleNamespace(id="spec_t"), "md", extra_paths=["src/x.py"]
    )

    assert "supplied 1 existing file(s) to the implementer:" in caplog.text
    assert result == [("src/x.py", "content")]


def test_r3_architect_read_failure_logs_correctly(monkeypatch, caplog):
    """R3: fetch raises, role="architect" — log contains 'the architect will not see...' and returns []"""
    monkeypatch.setattr(d, "_load_plan", lambda s: {"test_strategy": {"repo": "coding-model-server"}})

    def boom(repo, paths, ref):
        raise RuntimeError("runner down")

    monkeypatch.setattr(d.test_runner, "fetch_repo_files", boom)
    caplog.set_level(logging.WARNING, logger="orchestrator")

    result = d._fetch_existing_files_for_spec(
        types.SimpleNamespace(id="spec_t"), "md", extra_paths=["src/x.py"], role="architect"
    )

    assert "the architect will not see the files it must modify" in caplog.text
    assert result == []


def test_r4_role_parameter_signature():
    """R4: inspect.signature shows role as keyword-only with default 'implementer'"""
    sig = inspect.signature(d._fetch_existing_files_for_spec)
    param = sig.parameters["role"]

    assert param.kind is inspect.Parameter.KEYWORD_ONLY
    assert param.default == "implementer"