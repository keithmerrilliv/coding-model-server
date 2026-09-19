"""DEV-601: resolving the plan's phase paths against the repository.

The fixtures are the four runs that produced this ticket and the one that
proved how expensive it is. Each is the real shape, not an invented one.
"""
import pytest

from coding_model_autonomous.plan_paths import (
    apply_corrections, correction_candidates, is_placeholder, is_run_artifact,
    known_directories, phase_paths, resolve_plan_paths,
)

# The ElectricSheep layout: everything under two synchronized root groups.
ELECTRIC_SHEEP = {
    "ElectricSheep/HallucinationForcer.swift",
    "ElectricSheep/HallucinationSimulator.swift",
    "ElectricSheep/MetricsParticleBridge.swift",
    "ElectricSheep/ElectricSheepApp.swift",
    "ElectricSheep/ForcingStrategy.swift",
    "ElectricSheep/AudioManager.swift",
    "ElectricSheepTests/DtypeContainmentTests.swift",
}
PROTECTED = [
    "ElectricSheep/AudioManager.swift",
    "ElectricSheep/ForcingStrategy.swift",
]


def _exists(repo):
    return lambda p: p in repo


def _plan(outputs, inputs=("spec.md", "design.md"), name="implement"):
    return {"phases": [{"name": name, "inputs": list(inputs),
                        "outputs": list(outputs)}]}


class TestRun42:
    """spec_a3f6c9e7 — the run that cost four attempts and nearly three files."""

    def test_bare_paths_are_corrected_not_treated_as_new(self):
        plan = _plan(["HallucinationForcer.swift",
                      "MetricsParticleBridge.swift",
                      "ElectricSheepApp.swift"])
        r = resolve_plan_paths(plan, _exists(ELECTRIC_SHEEP), PROTECTED)
        assert r.corrections == {
            "HallucinationForcer.swift": "ElectricSheep/HallucinationForcer.swift",
            "MetricsParticleBridge.swift": "ElectricSheep/MetricsParticleBridge.swift",
            "ElectricSheepApp.swift": "ElectricSheep/ElectricSheepApp.swift",
        }
        assert r.new_paths == []          # none of these is a new file
        assert r.placeholders == []

    def test_the_correction_needs_only_the_protected_paths(self):
        """The signal was available: all six protected files sat under
        ElectricSheep/. No phase path had to resolve first."""
        plan = _plan(["HallucinationForcer.swift"])
        r = resolve_plan_paths(plan, _exists(ELECTRIC_SHEEP), PROTECTED)
        assert r.corrections == {
            "HallucinationForcer.swift": "ElectricSheep/HallucinationForcer.swift"}

    def test_applying_them_rewrites_every_phase_that_named_the_path(self):
        """Run 42's bare names appeared 3x each — design inputs, implement
        outputs, test inputs — so a fix that misses one is no fix."""
        plan = {"phases": [
            {"name": "design", "inputs": ["spec.md", "HallucinationForcer.swift"],
             "outputs": ["design.md"]},
            {"name": "implement", "inputs": ["design.md"],
             "outputs": ["HallucinationForcer.swift"]},
            {"name": "test", "inputs": ["HallucinationForcer.swift"],
             "outputs": ["test_report.md"]},
        ], "test_strategy": {"repo": "electric-sheep", "base_ref": "main"}}
        r = resolve_plan_paths(plan, _exists(ELECTRIC_SHEEP), PROTECTED)
        out = apply_corrections(plan, r.corrections)
        real = "ElectricSheep/HallucinationForcer.swift"
        assert out["phases"][0]["inputs"] == ["spec.md", real]
        assert out["phases"][1]["outputs"] == [real]
        assert out["phases"][2]["inputs"] == [real]
        # and nothing else moved
        assert out["test_strategy"] == plan["test_strategy"]
        assert out["phases"][0]["outputs"] == ["design.md"]


class TestRun16:
    """Plan named `HallucinationSimulator.swift`; the real path has a directory."""

    def test_bare_path_is_corrected(self):
        plan = _plan(["HallucinationSimulator.swift"])
        r = resolve_plan_paths(plan, _exists(ELECTRIC_SHEEP), PROTECTED)
        assert r.corrections == {
            "HallucinationSimulator.swift":
                "ElectricSheep/HallucinationSimulator.swift"}


class TestRun17:
    """`<source files>` carried as phases[test].inputs."""

    @pytest.mark.parametrize("junk", [
        "<source files>", "<new test files>", "...", "TBD", "N/A", "none"])
    def test_placeholders_are_never_paths(self, junk):
        assert is_placeholder(junk)
        plan = _plan(["X.swift"], inputs=[junk])
        r = resolve_plan_paths(plan, _exists(ELECTRIC_SHEEP), PROTECTED)
        assert r.placeholders == [junk]

    def test_a_new_file_outside_a_known_directory_is_surfaced_as_new(self):
        """`SharedMathHelpers.swift` at the repo root would never be compiled
        under this project's synchronized root groups. It cannot be corrected —
        the basename exists nowhere — so it must at least be VISIBLE."""
        plan = _plan(["SharedMathHelpers.swift"])
        r = resolve_plan_paths(plan, _exists(ELECTRIC_SHEEP), PROTECTED)
        assert r.new_paths == ["SharedMathHelpers.swift"]
        assert r.corrections == {}


class TestRun18:
    """Three files that exist at NO path; the one that had to change was absent."""

    def test_invented_files_are_reported_as_new_rather_than_corrected(self):
        invented = ["RepetitionBoostStrategy.swift",
                    "ContextCorruptionStrategy.swift",
                    "RestrictedSamplingStrategy.swift"]
        r = resolve_plan_paths(_plan(invented), _exists(ELECTRIC_SHEEP), PROTECTED)
        assert r.new_paths == sorted(invented)
        assert r.corrections == {}
        # Deliberately NOT a rejection here: "does not exist" is also what a
        # legitimate greenfield file looks like. Making these visible at the
        # human gate is this module's job; requiring the plan to MARK new files
        # is a planner-prompt change and is not in this commit.


class TestNoFalseRejections:
    """DEV-440's lesson: a correct plan must pass through untouched."""

    def test_a_plan_whose_paths_all_resolve_is_unchanged(self):
        plan = _plan(["ElectricSheep/HallucinationForcer.swift"],
                     inputs=["spec.md", "design.md"])
        r = resolve_plan_paths(plan, _exists(ELECTRIC_SHEEP), PROTECTED)
        assert r.corrections == {}
        assert r.placeholders == []
        assert r.new_paths == []
        assert apply_corrections(plan, r.corrections) == plan

    def test_run_artifacts_are_never_corrected_into_the_repo(self):
        """design.md and friends are spec-workspace artifacts. A repo that
        happens to contain a docs/design.md must not capture them."""
        repo = ELECTRIC_SHEEP | {"docs/design.md", "docs/spec.md"}
        plan = _plan(["ElectricSheep/HallucinationForcer.swift"],
                     inputs=["spec.md", "design.md"])
        r = resolve_plan_paths(plan, _exists(repo), PROTECTED + ["docs/x.md"])
        assert r.corrections == {}
        assert all(is_run_artifact(p) for p in ("spec.md", "design.md"))

    def test_an_ambiguous_basename_is_reported_not_guessed(self):
        repo = {"Foo/Util.swift", "Bar/Util.swift"}
        plan = _plan(["Util.swift"])
        r = resolve_plan_paths(plan, _exists(repo), ["Foo/a.swift", "Bar/b.swift"])
        assert r.corrections == {}
        assert [x.path for x in r.ambiguous] == ["Util.swift"]
        assert sorted(r.ambiguous[0].ambiguous) == ["Bar/Util.swift", "Foo/Util.swift"]


class TestHelpers:
    def test_phase_paths_walks_inputs_and_outputs_in_order(self):
        plan = {"phases": [{"name": "implement", "inputs": ["a"], "outputs": ["b", "c"]}]}
        assert phase_paths(plan) == [
            ("implement", "inputs", 0, "a"),
            ("implement", "outputs", 0, "b"),
            ("implement", "outputs", 1, "c"),
        ]

    def test_phase_paths_tolerates_junk_shapes(self):
        for junk in ({}, {"phases": None}, {"phases": ["x"]},
                     {"phases": [{"name": "i", "outputs": "notalist"}]},
                     {"phases": [{"name": "i", "outputs": [None, 3, "  "]}]}):
            assert phase_paths(junk) == []

    def test_known_directories_are_deduped_nearest_root_first(self):
        assert known_directories(
            ["a/b/c.swift", "a/d.swift", "a/b/e.swift", "f.swift"]) == ["a", "a/b"]

    def test_correction_candidates_never_include_the_path_itself(self):
        assert "a/x.swift" not in correction_candidates("a/x.swift", ["a"])


class TestDaemonWiring:
    """_resolve_plan_phase_paths: the seam between the resolver and the run.

    The resolver is pure; this is the part that decides what the plan a human
    approves — and every role afterwards — actually says.
    """

    @staticmethod
    def _spec():
        class _S:
            id = "spec_test"
        return _S()

    def _run(self, monkeypatch, yaml_text, repo):
        from coding_model_server import orchestrator_daemon as od

        class _Ctx:
            def existing(self, p):
                return "contents" if p in repo else None

        monkeypatch.setattr(od, "_spec_context",
                            lambda *a, **k: _Ctx())
        return od._resolve_plan_phase_paths(None, self._spec(), "", yaml_text)

    RUN42_PLAN = """title: bridge
test_strategy:
  repo: electric-sheep
  base_ref: main
  skip_filter: ElectricSheepTests/DtypeContainmentTests
  protected_paths:
  - ElectricSheep/AudioManager.swift
  - ElectricSheep/ForcingStrategy.swift
phases:
- name: implement
  inputs:
  - design.md
  outputs:
  - HallucinationForcer.swift
  - MetricsParticleBridge.swift
"""

    def test_it_rewrites_the_plan_a_human_approves(self, monkeypatch):
        import yaml
        text, problems = self._run(monkeypatch, self.RUN42_PLAN, ELECTRIC_SHEEP)
        assert problems == []
        plan = yaml.safe_load(text)
        assert plan["phases"][0]["outputs"] == [
            "ElectricSheep/HallucinationForcer.swift",
            "ElectricSheep/MetricsParticleBridge.swift",
        ]
        # the operator's keys survive the round-trip — DEV-712's ground
        assert plan["test_strategy"]["skip_filter"] == \
            "ElectricSheepTests/DtypeContainmentTests"
        assert plan["test_strategy"]["protected_paths"] == [
            "ElectricSheep/AudioManager.swift",
            "ElectricSheep/ForcingStrategy.swift"]
        assert plan["title"] == "bridge"

    def test_a_correct_plan_is_returned_byte_identical(self, monkeypatch):
        """DEV-440: no churn, no reformat, when there is nothing to fix."""
        good = self.RUN42_PLAN.replace(
            "- HallucinationForcer.swift", "- ElectricSheep/HallucinationForcer.swift"
        ).replace(
            "- MetricsParticleBridge.swift", "- ElectricSheep/MetricsParticleBridge.swift")
        text, problems = self._run(monkeypatch, good, ELECTRIC_SHEEP)
        assert problems == []
        assert text == good

    def test_a_placeholder_is_a_problem_not_a_correction(self, monkeypatch):
        bad = self.RUN42_PLAN.replace("- design.md", "- <source files>")
        text, problems = self._run(monkeypatch, bad, ELECTRIC_SHEEP)
        assert len(problems) == 1 and "<source files>" in problems[0]

    def test_a_runner_outage_does_not_fail_the_plan(self, monkeypatch):
        """DEV-620 semantics: unreachable is not invalid."""
        from coding_model_server import orchestrator_daemon as od

        def _boom(*a, **k):
            raise RuntimeError("runner down")

        monkeypatch.setattr(od, "_spec_context", _boom)
        text, problems = od._resolve_plan_phase_paths(
            None, self._spec(), "", self.RUN42_PLAN)
        assert problems == []
        assert text == self.RUN42_PLAN          # unchanged, not mangled

    def test_a_plan_with_no_repo_still_catches_placeholders(self, monkeypatch):
        no_repo = self.RUN42_PLAN.replace("  repo: electric-sheep\n", "")
        no_repo = no_repo.replace("- design.md", "- <source files>")
        text, problems = self._run(monkeypatch, no_repo, ELECTRIC_SHEEP)
        assert len(problems) == 1 and "<source files>" in problems[0]
        assert text == no_repo
