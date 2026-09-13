"""DEV-630, second pass: a path that did not read is ABSENT or UNKNOWN.

``SpecContext.new_files`` — what marks a path EMIT WHOLE for the implementer
(DEV-638) and what the DEV-645 feedback reads — used to be "every planned
path that is not in the fetch". A path the runner could not read for any
reason but "does not exist" was therefore presented as a creation, and the
daemon-side failure branch of ``assemble`` recorded no omission at all, so a
read failure turned every existing file into a NEW one. Alongside: the
allocator's unknown-agent lookups and the DEV-573 overlay's no-mapping shape,
the last two "disarming" sites from the first pass's audit.
"""
import logging

import pytest

import coding_model_server.orchestrator_daemon as d
from coding_model_autonomous import context as c

REPO = "coding-model-server"
PLAN = {"test_strategy": {"repo": REPO, "base_ref": "HEAD",
                          "protected_paths": ["src/coding_model_autonomous/"]},
        "phases": [{"name": "implement", "role": "implementer",
                    "outputs": ["src/a.py", "tests/t.py"]}]}
SPEC = "| Path | Action |\n|---|---|\n| `src/a.py` | modify — x |\n| `tests/t.py` | new |\n"


def _fetch(files, problems):
    return lambda repo, paths, ref: (
        [(p, files[p]) for p in paths if p in files],
        [problems[p] for p in paths if p in problems])


class TestStatus:
    def test_absent_needs_the_runner_to_say_so(self):
        assert c.omission_status("fatal: path 'x' does not exist in 'HEAD'") == "absent"
        assert c.omission_status("x: not found") == "absent"
        assert c.omission_status("No such file or directory") == "absent"

    def test_everything_else_is_unknown(self):
        assert c.omission_status("not returned by the runner") == "unknown"
        assert c.omission_status("read failed: disk error") == "unknown"
        assert c.omission_status("timed out") == "unknown"
        assert c.omission_status("") == "unknown"

    def test_the_three_statuses_from_one_fetch(self, tmp_path):
        ctx, fetched = c.assemble(
            spec_id="s", spec_dir=tmp_path, plan=PLAN, spec_md=SPEC,
            role="implementer",
            fetch=_fetch({"src/a.py": "x = 1\n"},
                         {"tests/t.py": "tests/t.py: fatal: path 'tests/t.py' does not exist in 'HEAD'",
                          "src/coding_model_autonomous/": "src/coding_model_autonomous/: error: read failed"}))
        assert fetched
        assert ctx.status("src/a.py") == "found"
        assert ctx.status("tests/t.py") == "absent"
        assert ctx.status("src/coding_model_autonomous/") == "unknown"
        assert ctx.status("never/asked.py") == "unknown"

    def test_new_files_is_the_absent_set_only(self, tmp_path):
        ctx, _ = c.assemble(
            spec_id="s", spec_dir=tmp_path, plan=PLAN, spec_md=SPEC,
            role="implementer",
            fetch=_fetch({}, {"src/a.py": "src/a.py: error: could not read",
                              "tests/t.py": "tests/t.py: not found"}))
        assert ctx.new_files(["src/a.py", "tests/t.py"]) == ["tests/t.py"]
        assert ctx.unknown_files(["src/a.py", "tests/t.py"]) == ["src/a.py"]

    def test_no_repo_means_every_path_is_a_creation(self):
        ctx, _ = c.assemble(spec_id="s", spec_dir=None, plan={"phases": []},
                            spec_md="", role="implementer", fetch=_fetch({}, {}))
        assert ctx.repo is None
        assert ctx.status("anything.py") == "absent"
        assert ctx.new_files(["a.py", "b.py"]) == ["a.py", "b.py"]

    def test_a_daemon_side_read_failure_makes_every_path_unknown(self, tmp_path, caplog):
        """The branch that used to return an empty context with no omissions
        — read downstream as 'every planned file is new'."""
        def boom(repo, paths, ref):
            raise RuntimeError("json decode error")
        with caplog.at_level(logging.WARNING):
            ctx, fetched = c.assemble(spec_id="s", spec_dir=tmp_path, plan=PLAN,
                                      spec_md=SPEC, role="implementer", fetch=boom)
        assert not fetched
        assert ctx.new_files(["src/a.py", "tests/t.py"]) == []
        assert ctx.unknown_files(["src/a.py", "tests/t.py"]) == ["src/a.py", "tests/t.py"]
        assert {o.path for o in ctx.omitted} == {"src/a.py", "tests/t.py",
                                                 "src/coding_model_autonomous/"}
        assert all(o.reason.startswith("read failed: ") for o in ctx.omitted)

    def test_a_stored_context_survives_a_round_trip_with_its_statuses(self, tmp_path):
        ctx, _ = c.assemble(
            spec_id="s", spec_dir=tmp_path, plan=PLAN, spec_md=SPEC,
            role="implementer",
            fetch=_fetch({"src/a.py": "x"}, {"tests/t.py": "tests/t.py: error: read failed"}))
        again = c.SpecContext.load(tmp_path)
        assert again is not None
        assert again.status("tests/t.py") == "unknown"
        assert again.new_files(["src/a.py", "tests/t.py"]) == []


class TestSelectSaysSo:
    def test_unknown_files_are_named_by_the_guard_they_disarm(self, tmp_path, caplog):
        ctx, _ = c.assemble(
            spec_id="s", spec_dir=tmp_path, plan=PLAN, spec_md=SPEC,
            role="implementer",
            fetch=_fetch({"src/a.py": "x"}, {"tests/t.py": "tests/t.py: error: read failed"}))
        with caplog.at_level(logging.WARNING):
            view = ctx.select("implementer", planned=["src/a.py", "tests/t.py"])
        assert view.new_files == [] and view.unknown_files == ["tests/t.py"]
        msg = [r.getMessage() for r in caplog.records if "DEV-638" in r.getMessage()]
        assert len(msg) == 1
        assert "NOT armed for tests/t.py" in msg[0] and "the implementer" in msg[0]

    def test_no_warning_when_nothing_is_unknown(self, tmp_path, caplog):
        ctx, _ = c.assemble(
            spec_id="s", spec_dir=tmp_path, plan=PLAN, spec_md=SPEC,
            role="implementer",
            fetch=_fetch({"src/a.py": "x"}, {"tests/t.py": "tests/t.py: not found"}))
        with caplog.at_level(logging.WARNING):
            view = ctx.select("implementer", planned=["src/a.py", "tests/t.py"])
        assert view.new_files == ["tests/t.py"] and view.unknown_files == []
        assert not [r for r in caplog.records if "DEV-638" in r.getMessage()]


class TestAllocatorUnknownAgent:
    @pytest.fixture(autouse=True)
    def _fresh(self, monkeypatch):
        monkeypatch.setattr(d, "_UNKNOWN_AGENTS_WARNED", set())

    def test_an_unknown_agent_warns_once_by_name(self, caplog):
        with caplog.at_level(logging.WARNING):
            assert d._agent_ctx_limit("no_such_agent") is None
            assert d._agent_reasoning_reserve("no_such_agent") == 0
            assert d._agent_ctx_limit("no_such_agent") is None
        msgs = [r.getMessage() for r in caplog.records if "DEV-630" in r.getMessage()]
        assert len(msgs) == 1
        assert "'no_such_agent'" in msgs[0] and "NOT armed" in msgs[0]

    def test_a_known_agent_is_quiet(self, caplog):
        from coding_model_server.config import Config
        name = next(iter(Config.AGENTS))
        with caplog.at_level(logging.WARNING):
            assert d._agent_ctx_limit(name) == int(Config.AGENTS[name]["model_config"]["n_ctx"])
            d._agent_reasoning_reserve(name)
        assert not [r for r in caplog.records if "DEV-630" in r.getMessage()]

    def test_no_agent_is_not_unknown(self, caplog):
        with caplog.at_level(logging.WARNING):
            assert d._agent_ctx_limit(None) is None
            assert d._agent_reasoning_reserve("") == 0
        assert not caplog.records


DECLARING_SPEC = """# Spec

## test_strategy

```yaml
framework: pytest
repo: coding-model-server
protected_paths:
  - src/coding_model_autonomous/
```
"""


class TestNoStrategyMapping:
    @pytest.mark.parametrize("plan", ["test_strategy: null\nphases: []\n",
                                      "test_strategy: pytest\nphases: []\n",
                                      "phases: []\n"])
    def test_validation_bounces_a_plan_that_dropped_the_whole_block(self, plan):
        problems = d._validate_test_strategy(plan, DECLARING_SPEC)
        assert len(problems) == 1
        assert "`protected_paths`" in problems[0] and "`repo`" in problems[0]
        assert "no `test_strategy` mapping" in problems[0]

    def test_a_spec_declaring_nothing_is_the_old_shape(self):
        assert d._validate_test_strategy("phases: []\n", "# Spec\n") == []

    def test_the_overlay_says_it_cannot_restore(self, caplog):
        with caplog.at_level(logging.WARNING):
            out = d._overlay_operator_test_strategy("test_strategy: null\n", DECLARING_SPEC, "spec_x")
        assert out == "test_strategy: null\n"
        msgs = [r.getMessage() for r in caplog.records if "DEV-573 overlay" in r.getMessage()]
        assert len(msgs) == 1 and "NOT armed" in msgs[0] and "protected_paths" in msgs[0]
