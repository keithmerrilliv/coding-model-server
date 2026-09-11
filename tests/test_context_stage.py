"""DEV-632: one context-assembly stage, fetched once, selected per role.

The stage replaces the per-role fetches (DEV-571 implementer, DEV-599
architect, DEV-604 manifest, five protected-file sites): one runner
round-trip per spec, persisted as context.json, reused by every role,
refreshed only when the candidate set grows, the ref changes, or a symbolic
ref ages past the refresh window. An outage on the first fetch parks
(DEV-620); an outage on a refresh keeps the last good fetch (DEV-544).
"""
from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest

from coding_model_autonomous import context as c
from coding_model_autonomous.context import (
    CONTEXT_FILE, RunnerOutage, SpecContext, assemble, prior_artifacts,
)

DOWN = ("could not reach the runner's read path: HTTPConnectionPool("
        "host='127.0.0.1', port=5050): Max retries exceeded")

SPEC_MD = """
| Path | Action |
|---|---|
| `src/pkg/mod.py` | modify — add a flag |
| `tests/test_mod.py` | new |
"""

PLAN = {
    "test_strategy": {"repo": "proj", "base_ref": "main",
                      "protected_paths": ["src/pkg/core.py"]},
    "phases": [{"name": "implement", "outputs": ["src/pkg/mod.py", "tests/test_new.py"]}],
}

REPO = {"src/pkg/mod.py": "x = 1\n", "src/pkg/core.py": "class Core: ...\n"}


class Runner:
    """Scripted fetch_repo_files: serves REPO, or answers 'down'."""

    def __init__(self, files=None, mode="ok"):
        self.files = dict(REPO if files is None else files)
        self.mode = mode
        self.calls: list[tuple[str, list[str], str]] = []

    def __call__(self, repo, paths, base_ref="HEAD", timeout=30):
        paths = list(paths)
        self.calls.append((repo, paths, base_ref))
        if self.mode == "down":
            return [], [DOWN]
        if self.mode == "raise":
            raise RuntimeError("exploded")
        got = [(p, self.files[p]) for p in paths if p in self.files]
        bad = [f"{p}: fatal: path '{p}' does not exist in '{base_ref}'"
               for p in paths if p not in self.files]
        return got, bad


def _assemble(tmp_path, runner, *, role="architect", plan=PLAN, spec_md=SPEC_MD,
              now=1000.0, extra=(), force=False):
    return assemble(spec_id="spec_1", spec_dir=tmp_path, plan=plan, spec_md=spec_md,
                    role=role, extra_candidates=extra, fetch=runner, now=now,
                    force=force)


# ── sections ────────────────────────────────────────────────────────────────

class TestSections:
    def test_one_fetch_serves_editable_and_protected(self, tmp_path):
        runner = Runner()
        ctx, fetched = _assemble(tmp_path, runner)
        assert fetched
        assert len(runner.calls) == 1
        # Editable candidates (table rows + plan outputs) and protected paths
        # travel in the one request; the protected set is asked last.
        assert runner.calls[0][1] == [
            "src/pkg/mod.py", "tests/test_mod.py", "tests/test_new.py",
            "src/pkg/core.py"]
        assert ctx.editable == {"src/pkg/mod.py": "x = 1\n"}
        assert ctx.protected == {"src/pkg/core.py": "class Core: ...\n"}
        assert ctx.declared == ["src/pkg/mod.py"]

    def test_omissions_are_recorded_with_the_runner_reason(self, tmp_path):
        ctx, _ = _assemble(tmp_path, Runner())
        omitted = {o.path: (o.section, o.reason) for o in ctx.omitted}
        assert set(omitted) == {"tests/test_mod.py", "tests/test_new.py"}
        assert omitted["tests/test_new.py"][0] == "editable"
        assert "does not exist in 'main'" in omitted["tests/test_new.py"][1]

    def test_protected_wins_over_editable(self, tmp_path):
        plan = json.loads(json.dumps(PLAN))
        plan["test_strategy"]["protected_paths"] = ["src/pkg/mod.py"]
        ctx, _ = _assemble(tmp_path, Runner(), plan=plan)
        assert "src/pkg/mod.py" in ctx.protected
        assert "src/pkg/mod.py" not in ctx.editable
        assert ctx.new_files(["src/pkg/mod.py", "tests/test_new.py"]) == ["tests/test_new.py"]

    def test_select_gives_the_prompt_shapes_and_logs_the_role(self, tmp_path, caplog):
        ctx, _ = _assemble(tmp_path, Runner())
        with caplog.at_level(logging.INFO, logger="orchestrator.context"):
            view = ctx.select("architect", planned=["src/pkg/mod.py", "tests/test_new.py"])
        assert view.existing_files == [("src/pkg/mod.py", "x = 1\n")]
        assert view.reference_files == [("src/pkg/core.py", "class Core: ...\n")]
        assert view.new_files == ["tests/test_new.py"]
        assert view.existing_by_path["src/pkg/mod.py"] == "x = 1\n"
        assert "supplied 1 existing file(s) to the architect: src/pkg/mod.py" in caplog.text
        assert "supplied 1 protected file(s) as read-only context to the architect" in caplog.text

    def test_declared_but_unread_warns_that_the_role_is_blind(self, tmp_path, caplog):
        ctx, _ = _assemble(tmp_path, Runner(files={}))
        with caplog.at_level(logging.WARNING, logger="orchestrator.context"):
            ctx.select("implementer")
        assert "1 file(s) marked modify but none could be read — implementer is working blind" in caplog.text

    def test_no_repo_means_no_fetch(self, tmp_path):
        runner = Runner()
        ctx, fetched = _assemble(tmp_path, runner, plan={"test_strategy": {"framework": "pytest"}})
        assert not fetched and runner.calls == []
        assert ctx.editable == {} and ctx.repo is None

    def test_no_candidates_means_no_fetch(self, tmp_path):
        runner = Runner()
        ctx, fetched = _assemble(tmp_path, runner,
                                 plan={"test_strategy": {"repo": "proj"}}, spec_md="prose only")
        assert not fetched and runner.calls == []
        assert not (tmp_path / CONTEXT_FILE).exists()

    def test_long_candidate_lists_are_chunked_to_the_runner_cap(self, tmp_path, monkeypatch):
        monkeypatch.setattr(c, "FETCH_CHUNK", 3)
        files = {f"src/f{i}.py": str(i) for i in range(7)}
        runner = Runner(files=files)
        ctx, _ = _assemble(tmp_path, runner, plan={"test_strategy": {"repo": "proj"}},
                           spec_md="", extra=list(files))
        assert [len(p) for _, p, _ in runner.calls] == [3, 3, 1]
        assert ctx.editable == files


# ── reuse and refresh ───────────────────────────────────────────────────────

class TestReuse:
    def test_the_next_role_reuses_the_fetch(self, tmp_path, caplog):
        runner = Runner()
        _assemble(tmp_path, runner, role="architect")
        with caplog.at_level(logging.INFO, logger="orchestrator.context"):
            ctx, fetched = _assemble(tmp_path, runner, role="implementer", now=1100.0)
        assert not fetched and len(runner.calls) == 1
        assert ctx.fetched_by == "architect"
        assert ctx.editable == {"src/pkg/mod.py": "x = 1\n"}
        assert "context reused for the implementer" in caplog.text

    def test_a_grown_candidate_set_refetches(self, tmp_path):
        runner = Runner()
        _assemble(tmp_path, runner)
        runner.files["src/pkg/extra.py"] = "e\n"
        ctx, fetched = _assemble(tmp_path, runner, role="implementer",
                                 extra=["src/pkg/extra.py"], now=1001.0)
        assert fetched and len(runner.calls) == 2
        assert ctx.editable["src/pkg/extra.py"] == "e\n"
        assert ctx.fetches == 2

    def test_a_changed_ref_or_protected_set_refetches(self, tmp_path):
        runner = Runner()
        _assemble(tmp_path, runner)
        plan = json.loads(json.dumps(PLAN))
        plan["test_strategy"]["base_ref"] = "release"
        _, fetched = _assemble(tmp_path, runner, plan=plan, now=1001.0)
        assert fetched
        plan["test_strategy"]["protected_paths"] = ["src/pkg/core.py", "src/pkg/other.py"]
        _, fetched = _assemble(tmp_path, runner, plan=plan, now=1002.0)
        assert fetched and len(runner.calls) == 3

    def test_a_symbolic_ref_is_reverified_after_the_window(self, tmp_path, monkeypatch, caplog):
        monkeypatch.setattr(c, "REFRESH_SECONDS", 600)
        runner = Runner()
        _assemble(tmp_path, runner, now=1000.0)
        _, fetched = _assemble(tmp_path, runner, now=1599.0)
        assert not fetched
        runner.files["src/pkg/mod.py"] = "x = 2\n"
        with caplog.at_level(logging.INFO, logger="orchestrator.context"):
            ctx, fetched = _assemble(tmp_path, runner, role="implementer", now=1601.0)
        assert fetched and ctx.editable["src/pkg/mod.py"] == "x = 2\n"
        assert "context refreshed at main for the implementer — 1 changed (src/pkg/mod.py)" in caplog.text

    def test_a_pinned_ref_is_never_reverified(self, tmp_path, monkeypatch):
        monkeypatch.setattr(c, "REFRESH_SECONDS", 1)
        plan = json.loads(json.dumps(PLAN))
        plan["test_strategy"]["base_ref"] = "7dc0a0f5c0ffee"
        runner = Runner()
        _assemble(tmp_path, runner, plan=plan, now=1000.0)
        _, fetched = _assemble(tmp_path, runner, plan=plan, now=99999.0)
        assert not fetched and len(runner.calls) == 1

    def test_force_refetches(self, tmp_path):
        runner = Runner()
        _assemble(tmp_path, runner)
        _, fetched = _assemble(tmp_path, runner, force=True, now=1001.0)
        assert fetched

    def test_context_json_round_trips(self, tmp_path):
        runner = Runner()
        ctx, _ = _assemble(tmp_path, runner)
        loaded = SpecContext.load(tmp_path)
        assert loaded == ctx
        assert loaded.omitted[0].section == "editable"
        data = json.loads((tmp_path / CONTEXT_FILE).read_text())
        assert "stale" not in data
        assert data["fetched_by"] == "architect"

    def test_an_unreadable_context_json_is_refetched(self, tmp_path):
        (tmp_path / CONTEXT_FILE).write_text("{not json")
        runner = Runner()
        _, fetched = _assemble(tmp_path, runner)
        assert fetched


# ── outages ─────────────────────────────────────────────────────────────────

class TestOutages:
    def test_first_fetch_down_raises_for_the_daemon_to_park(self, tmp_path):
        with pytest.raises(RunnerOutage):
            _assemble(tmp_path, Runner(mode="down"))
        assert not (tmp_path / CONTEXT_FILE).exists()

    def test_refresh_down_keeps_the_last_good_fetch(self, tmp_path, monkeypatch, caplog):
        """DEV-544: a transient outage never strips the context a role had."""
        monkeypatch.setattr(c, "REFRESH_SECONDS", 10)
        runner = Runner()
        _assemble(tmp_path, runner, now=1000.0)
        runner.mode = "down"
        with caplog.at_level(logging.WARNING, logger="orchestrator.context"):
            ctx, fetched = _assemble(tmp_path, runner, role="implementer", now=2000.0)
            view = ctx.select("implementer")
        assert not fetched and ctx.stale
        assert view.existing_files == [("src/pkg/mod.py", "x = 1\n")]
        assert view.reference_files == [("src/pkg/core.py", "class Core: ...\n")]
        assert "found the runner down" in caplog.text
        assert "implementer is working from the last good context fetch" in caplog.text

    def test_a_daemon_side_read_failure_degrades_soft(self, tmp_path, caplog):
        with caplog.at_level(logging.WARNING, logger="orchestrator.context"):
            ctx, fetched = _assemble(tmp_path, Runner(mode="raise"), role="architect")
        assert not fetched and ctx.editable == {} and ctx.protected == {}
        assert "the architect will not see the files it must modify" in caplog.text

    def test_a_grown_set_hitting_an_outage_keeps_nothing_it_did_not_have(self, tmp_path):
        """A refresh forced by NEW candidates with the runner down: the old
        context is not a superset, so this is a first fetch for those paths
        and the role must park rather than proceed without them."""
        runner = Runner()
        _assemble(tmp_path, runner)
        runner.mode = "down"
        with pytest.raises(RunnerOutage):
            _assemble(tmp_path, runner, extra=["src/pkg/extra.py"], now=1001.0)


# ── prior artifacts (workspace state, no runner) ────────────────────────────

class TestPriorArtifacts:
    def test_latest_row_per_path_from_disk(self, tmp_path):
        (tmp_path / "a.py").write_text("v2")
        (tmp_path / "b.bin").write_bytes(b"\xff\xfe")
        rows = [SimpleNamespace(path="a.py"), SimpleNamespace(path="a.py"),
                SimpleNamespace(path="b.bin"), SimpleNamespace(path="gone.py")]
        db = SimpleNamespace(list_artifacts=lambda spec_id, kind=None: rows)
        out = dict(prior_artifacts(db, "s", tmp_path))
        assert out["a.py"] == "v2"
        assert out["b.bin"].startswith("[binary file, 2 bytes")
        assert "gone.py" not in out
        assert len(out) == 2


# ── candidate derivation (moved from the daemon) ────────────────────────────

class TestCandidates:
    def test_planned_outputs_come_from_the_implement_phase_only(self):
        plan = {"phases": [{"name": "design", "outputs": ["design.md"]},
                           {"name": "implement", "outputs": ["a.py", " a.py ", "", 3]}]}
        assert c.planned_outputs(plan) == ["a.py"]
        assert c.planned_outputs({}) == [] and c.planned_outputs(None) == []

    def test_protected_paths_are_deduplicated_and_stripped(self):
        assert c.protected_paths({"test_strategy": {"protected_paths": [" a ", "a", "", None, "b"]}}) == ["a", "b"]
        assert c.protected_paths({"test_strategy": "scalar"}) == []
