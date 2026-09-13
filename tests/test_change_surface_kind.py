"""DEV-630: "nothing declared" and "could not tell" are different answers.

Both change-surface parsers return [] for a spec with no table AND for a spec
with a table they cannot read a path from. Every guard keyed on that list —
the DEV-492 repo-key check, edit mode — stood down identically on both, which
is how run 19 lost its whole table with no log line (DEV-621). The typed
reading keeps the parsers exactly as DEV-621 pinned them and adds the one
fact they could not express.
"""
import logging

import coding_model_server.orchestrator_daemon as d
from coding_model_autonomous.context import change_surface

PROSE = "# Spec\n\nJust prose. No table.\n"
KEYWORD = "| Path | Action |\n|---|---|\n| `src/a.py` | modify — add x |\n| `tests/t.py` | new |\n"
# DEV-621's run-19 shape: backticked paths, descriptive second column → path tier
RUN19 = "| Path | What changes |\n|---|---|\n| `src/a.py` | the guard |\n"
# a table neither tier can read: no backticks, so no path row either
UNREAD = "| File | Description |\n|---|---|\n| src/a.py | Adds the widget |\n| tests/t.py | Covers it |\n"
HEADER_ONLY = "| Path | Action |\n|---|---|\n"


class TestKind:
    def test_no_table_is_absent(self):
        cs = change_surface(PROSE)
        assert cs.kind == "absent" and cs.rows == 0 and not cs.any

    def test_empty_spec_is_absent(self):
        assert change_surface("").kind == "absent"

    def test_keyword_table_is_recognised(self):
        cs = change_surface(KEYWORD)
        assert cs.kind == "recognised"
        assert cs.declared == ["src/a.py"] and set(cs.paths) == {"src/a.py", "tests/t.py"}

    def test_dev621_descriptive_table_is_recognised_via_the_path_tier(self):
        cs = change_surface(RUN19)
        assert cs.kind == "recognised" and cs.declared == [] and cs.paths == ["src/a.py"]

    def test_a_table_no_tier_can_read_is_unrecognised_not_absent(self):
        """The whole ticket: the spec SAID something and we could not tell what."""
        cs = change_surface(UNREAD)
        assert cs.declared == [] and cs.paths == []      # what the guards saw before
        assert cs.kind == "unrecognised" and cs.rows == 3  # what they can see now
        assert not cs.any                                # truthiness unchanged

    def test_a_header_only_table_is_unrecognised(self):
        assert change_surface(HEADER_ONLY).kind == "unrecognised"

    def test_separator_rows_are_not_rows(self):
        assert change_surface("|---|---|\n|:--|--:|\n").rows == 0

    def test_any_matches_the_pre_dev630_truthiness(self):
        for md in (PROSE, KEYWORD, RUN19, UNREAD):
            cs = change_surface(md)
            assert cs.any == bool(d._declared_file_modifications(md)
                                  or d._change_surface_path_rows(md))


class TestGuardsArmByName:
    """The consumers must SAY which guard could not tell, at WARNING — and
    must not change what they DO, which was the pre-DEV-630 truthiness."""

    NO_REPO = "test_strategy:\n  framework: pytest\n"

    def test_the_repo_key_check_warns_on_an_unrecognised_table(self, caplog):
        with caplog.at_level(logging.WARNING, logger="orchestrator"):
            problems = d._validate_test_strategy(self.NO_REPO, UNREAD)
        assert any("DEV-492 repo-key check" in r.message and "NOT armed" in r.message
                   for r in caplog.records)
        # it could not tell, so it does not CLAIM a modification either
        assert not any("no `repo` key" in p for p in problems)

    def test_a_recognised_table_still_raises_the_repo_problem(self, caplog):
        with caplog.at_level(logging.WARNING, logger="orchestrator"):
            problems = d._validate_test_strategy(self.NO_REPO, RUN19)
        assert any("no `repo` key" in p for p in problems)   # unchanged behaviour
        assert not any("NOT armed" in r.message for r in caplog.records)

    def test_no_table_is_quiet(self, caplog):
        with caplog.at_level(logging.WARNING, logger="orchestrator"):
            d._validate_test_strategy(self.NO_REPO, PROSE)
        assert not any("DEV-630" in r.message for r in caplog.records)


class TestCapsNeverResetSilently:
    """A safety cap that cannot be read must not read as 'unused' without a word."""

    class _BrokenDB:
        def list_events_by_kind(self, **kw):
            raise RuntimeError("disk on fire")

    def test_crash_recovery_cap_warns_by_name(self, caplog):
        with caplog.at_level(logging.WARNING, logger="orchestrator"):
            assert d._crash_recoveries_used(self._BrokenDB(), "spec_x", "task_x") == 0
        assert any("recovery cap is NOT enforced" in r.message for r in caplog.records)

    def test_testability_round_cap_warns_by_name(self, caplog):
        with caplog.at_level(logging.WARNING, logger="orchestrator"):
            assert d._testability_rounds_used(self._BrokenDB(), "spec_x") == 0
        assert any("revision cap is NOT enforced" in r.message for r in caplog.records)
