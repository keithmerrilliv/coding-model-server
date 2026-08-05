"""DEV-526 — the integration guard must catch the case it was built for.

`check_patch_integrated` exists to stop a patch that reaches no build target
from passing on the repo's existing tests (DEV-399). DEV-502 was exactly that
failure and the guard stayed silent, for three independent reasons:

  1. it was disabled for swift_test — the framework the live Apple pipeline
     uses — on the false premise that "Sources/ is correct for SwiftPM so the
     check would invert". SwiftPM compiles only directories a target CLAIMS,
     so a typo'd Tests/CentipegeCoreTests/ belongs to nothing and is exactly
     what this check detects.
  2. it raised only when NOTHING landed, so one correct source file exonerated
     a mis-placed one in the same patch — the ratio was computed, logged, and
     discarded.
  3. it skipped itself with a single INFO line when git could not answer.
     Reachable: the runner is a LaunchAgent whose PATH comes from its plist.
"""
from pathlib import Path

import pytest

from mac_runner.integration import IntegrationError, check_patch_integrated


def _repo(tmp_path: Path, tracked: list[str]) -> Path:
    """A real git repo, because the check shells out to `git ls-files`."""
    import subprocess

    for rel in tracked:
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("// existing\n")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "base"], cwd=tmp_path, check=True)
    return tmp_path


def _patch(*paths: str) -> list[dict]:
    return [{"path": p, "content": "// new\n"} for p in paths]


# The real DEV-502 shape: correct sources beside a one-character typo.
CENTIPEDE_TRACKED = [
    "Sources/CentipedeCore/GameState.swift",
    "Tests/CentipedeCoreTests/GameStateTests.swift",
    "Package.swift",
]


class TestTheDev502Case:
    def test_a_stranded_test_file_is_reported(self, tmp_path):
        """The exact patch that went green having compiled no new tests."""
        repo = _repo(tmp_path, CENTIPEDE_TRACKED)
        warnings = check_patch_integrated(repo, _patch(
            "Sources/CentipedeCore/World.swift",          # lands
            "Tests/CentipegeCoreTests/CoreLogicTests.swift",  # typo — lands nowhere
        ), "swift_test")
        assert warnings, "the stranded file must be reported, not exonerated"
        assert "CentipegeCoreTests" in warnings[0]

    def test_the_warning_says_a_green_result_does_not_cover_it(self, tmp_path):
        """The point is not that a file is odd — it is that PASS means less."""
        repo = _repo(tmp_path, CENTIPEDE_TRACKED)
        w = check_patch_integrated(repo, _patch(
            "Sources/CentipedeCore/World.swift",
            "Tests/CentipegeCoreTests/X.swift",
        ), "swift_test")[0]
        assert "does NOT cover" in w

    def test_swift_test_is_checked_at_all(self, tmp_path):
        """Was disabled for the framework the live pipeline actually uses."""
        repo = _repo(tmp_path, CENTIPEDE_TRACKED)
        with pytest.raises(IntegrationError):
            check_patch_integrated(
                repo, _patch("Nowhere/A.swift", "Nowhere/B.swift"), "swift_test")


class TestConservatismPreserved:
    """The module's stated design: fail only when confident nothing landed."""

    def test_a_fully_landing_patch_is_silent(self, tmp_path):
        repo = _repo(tmp_path, CENTIPEDE_TRACKED)
        assert check_patch_integrated(repo, _patch(
            "Sources/CentipedeCore/World.swift",
            "Tests/CentipedeCoreTests/WorldTests.swift",
        ), "swift_test") == []

    def test_editing_a_tracked_file_is_silent(self, tmp_path):
        repo = _repo(tmp_path, CENTIPEDE_TRACKED)
        assert check_patch_integrated(
            repo, _patch("Sources/CentipedeCore/GameState.swift"),
            "swift_test") == []

    def test_nothing_landing_still_raises(self, tmp_path):
        repo = _repo(tmp_path, CENTIPEDE_TRACKED)
        with pytest.raises(IntegrationError):
            check_patch_integrated(repo, _patch("Guessed/A.swift"), "xcodebuild_test")

    def test_a_partial_patch_does_not_raise(self, tmp_path):
        """Warn, don't block — a new module in a new directory is legitimate."""
        repo = _repo(tmp_path, CENTIPEDE_TRACKED)
        check_patch_integrated(repo, _patch(
            "Sources/CentipedeCore/World.swift", "Brand/New/Thing.swift",
        ), "swift_test")  # must not raise

    def test_unrelated_frameworks_are_untouched(self, tmp_path):
        repo = _repo(tmp_path, CENTIPEDE_TRACKED)
        assert check_patch_integrated(
            repo, _patch("Guessed/A.swift"), "pytest") == []

    def test_docs_only_patches_are_untouched(self, tmp_path):
        repo = _repo(tmp_path, CENTIPEDE_TRACKED)
        assert check_patch_integrated(
            repo, _patch("README.md"), "swift_test") == []


class TestGitFailureIsLoud:
    def test_a_non_repo_reports_that_it_skipped(self, tmp_path):
        """git can't answer here — previously one INFO line and silent skip.

        The run must say it lost its protection rather than imply it passed
        one.
        """
        warnings = check_patch_integrated(
            tmp_path, _patch("Sources/A.swift"), "swift_test")
        assert warnings and "SKIPPED" in warnings[0]

    def test_it_does_not_raise_on_git_failure(self, tmp_path):
        """Infrastructure trouble must not be scored as a bad patch."""
        check_patch_integrated(tmp_path, _patch("Sources/A.swift"), "swift_test")


class TestWarningsReachTheOutput:
    def test_the_runner_folds_warnings_into_the_run_output(self):
        """A signal nobody reads is worth nothing — DEV-492's `overwrites`
        field is produced and consumed by no one. These ride `output`, which
        the orchestrator already forwards to the reviewer and the gate."""
        import inspect

        from mac_runner import server

        src = inspect.getsource(server.run_tests_endpoint)
        assert "integration_warnings" in src
        assert 'join(integration_warnings)' in src
