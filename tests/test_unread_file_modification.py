"""DEV-492 — never overwrite a file the implementer was not allowed to read.

`build_implementer_message` takes spec, design, rejection notes and
clarifications. It has no parameter for existing sources, no caller supplies
them, and the Mac runner exposes no read path. So a file a spec marks "modify"
is re-emitted from imagination and the runner writes that invention over the
real thing.

On spec_f47132ab a 163+ line ContentView.swift came back as 45 lines — roughly
120 lines of working UI destroyed. It was caught only because the invention
happened not to compile; had it compiled, the loss would have been silent and
the tests would still have passed, since the acceptance criteria targeted
cancellation behaviour rather than the rest of the UI.

Two guards, at the only two points where the information exists:

  - the plan gate, which can read the spec's change-surface table
  - the runner, the only component that ever holds both the original and the
    replacement at once
"""
import pytest

from coding_model_server import orchestrator_daemon as od


class TestChangeSurfaceDetection:
    """Keyed on the table, not on the word "modify" appearing in prose."""

    MODIFY_SPEC = """
## Change surface

| Path | Action |
|---|---|
| `ElectricSheep/GenerationTaskController.swift` | **create** (new file, app target) |
| `ElectricSheep/HallucinationEngine.swift` | modify (line 56; loop at 103) |
| `ElectricSheep/ContentView.swift` | modify (line 163 only) |
| `ElectricSheepTests/GenerationCancellationTests.swift` | **create** (new file) |
"""

    # Verbatim shape from the Centipede spec, which is greenfield and must NOT
    # be blocked: it says "existing" and "do not modify" in prose while every
    # file it writes is new.
    GREENFIELD_SPEC = """
# Centipede — logic core, slice 1

This targets an **existing** repository, not a greenfield project. Extend the
existing SwiftPM package with the deterministic simulation core.

- Do not modify `Package.swift`'s tools version or platforms, and do not add targets.
"""

    def test_modify_rows_are_found(self):
        found = od._declared_file_modifications(self.MODIFY_SPEC)
        assert "ElectricSheep/HallucinationEngine.swift" in found
        assert "ElectricSheep/ContentView.swift" in found

    def test_create_rows_are_not_flagged(self):
        found = od._declared_file_modifications(self.MODIFY_SPEC)
        assert not any("GenerationTaskController" in p for p in found)
        assert not any("GenerationCancellationTests" in p for p in found)
        assert len(found) == 2

    def test_greenfield_spec_is_not_blocked(self):
        """The false positive that would matter most: a naive search for
        "modify" blocks a working spec. Centipede has no change-surface table
        and its only "modify" is a negative constraint."""
        assert od._declared_file_modifications(self.GREENFIELD_SPEC) == []

    def test_header_row_is_not_a_path(self):
        assert "Path" not in od._declared_file_modifications(self.MODIFY_SPEC)

    def test_empty_spec(self):
        assert od._declared_file_modifications("") == []

    @pytest.mark.parametrize("action", ["modify", "Modify", "modified", "**modify**"])
    def test_action_spellings(self, action):
        md = f"| Path | Action |\n|---|---|\n| `a/b.swift` | {action} (line 1) |\n"
        assert od._declared_file_modifications(md) == ["a/b.swift"]


class TestGuardIsWiredIntoTheGate:
    def test_the_override_exists_and_defaults_off(self):
        """Refusing by default is the point — the override is for after the
        read path lands, or a deliberate accepted risk."""
        assert od.ALLOW_UNREAD_FILE_MODIFICATION is False

    def test_block_marks_the_spec_failed_not_replanned(self):
        """Routing this back to the planner would loop: replanning cannot give
        the pipeline an ability it structurally lacks."""
        import inspect
        src = inspect.getsource(od._block_plan_for_unreadable_modification)
        assert "SpecStatus.FAILED" in src
        # The call, not the name — the docstring explains why replanning is
        # wrong here, so a bare substring match would flag its own rationale.
        assert "_reject_plan_for_validation(" not in src

    def test_accept_plan_checks_before_creating_the_human_gate(self):
        """An operator should not spend a review round approving a plan that
        will destroy work.

        The check moved behind _unreadable_declared_modifications once the
        DEV-492 read path landed: the question is no longer "does this spec
        modify a file" but "is there a modified file we still cannot read".
        Its position relative to the gate is what this test protects.
        """
        import inspect
        src = inspect.getsource(od._accept_plan)
        assert "_unreadable_declared_modifications" in src
        gate_at = src.index("GateType.PLAN_APPROVAL")
        check_at = src.index("_unreadable_declared_modifications")
        assert check_at < gate_at, "the guard must run before the gate is created"

    def test_a_readable_modification_is_no_longer_blocked(self, monkeypatch):
        """The guard existed because the pipeline could not read files. Once it
        can, blocking would stop exactly the specs the read path enables."""
        from coding_model_autonomous import test_runner

        monkeypatch.setattr(
            test_runner, "fetch_repo_files",
            lambda repo, paths, base_ref: ([(p, "x") for p in paths], []))

        class _S:
            id = "spec_x"

        spec_md = "| path | change |\n| `A/B.swift` | modify (line 1) |\n"
        assert od._unreadable_declared_modifications(
            _S(), "test_strategy:\n  repo: proj\n", spec_md) == []


class TestRunnerOverwriteDetector:
    """The runner is the only component that sees both versions."""

    def _write(self, tmp_path, existing: "str | None", new: str):
        from mac_runner.workspace import _write_patch_files

        if existing is not None:
            (tmp_path / "f.swift").write_text(existing)
        overwrites: list[dict] = []
        _write_patch_files(tmp_path, [{"path": "f.swift", "content": new}],
                           overwrites=overwrites)
        return overwrites

    def test_new_file_is_not_an_overwrite(self, tmp_path):
        assert self._write(tmp_path, None, "x" * 100) == []

    def test_reconstruction_is_flagged(self, tmp_path):
        """The spec_f47132ab shape: 163+ lines replaced by 45."""
        ow = self._write(tmp_path, "line\n" * 163, "line\n" * 45)
        assert len(ow) == 1
        assert ow[0]["suspected_reconstruction"] is True
        assert ow[0]["old_lines"] > ow[0]["new_lines"]

    def test_ordinary_edit_is_recorded_but_not_flagged(self, tmp_path):
        """A real edit changes a little. Recording it is still useful; calling
        it a reconstruction would be noise."""
        ow = self._write(tmp_path, "line\n" * 100, "line\n" * 98)
        assert len(ow) == 1
        assert ow[0]["suspected_reconstruction"] is False

    def test_growth_is_never_a_reconstruction(self, tmp_path):
        ow = self._write(tmp_path, "line\n" * 10, "line\n" * 200)
        assert ow[0]["suspected_reconstruction"] is False

    def test_the_file_is_still_written(self, tmp_path):
        """Detection must not become enforcement — the runner does not know
        whether the spec intended a rewrite."""
        self._write(tmp_path, "old\n" * 100, "new content")
        assert (tmp_path / "f.swift").read_text() == "new content"

    def test_absent_out_param_costs_nothing(self, tmp_path):
        """Existing callers that pass no list must not pay for the read."""
        from mac_runner.workspace import _write_patch_files

        (tmp_path / "f.swift").write_text("old")
        _write_patch_files(tmp_path, [{"path": "f.swift", "content": "new"}])
        assert (tmp_path / "f.swift").read_text() == "new"

    def test_response_carries_overwrites(self):
        from mac_runner.server import RunTestsResponse

        r = RunTestsResponse(passed=True, output="", duration_sec=1.0)
        assert r.overwrites == [], "must default empty, not None"
