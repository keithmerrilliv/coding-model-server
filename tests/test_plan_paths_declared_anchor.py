"""DEV-733: the spec's change surface is the second anchor for plan paths.

DEV-601 resolves paths that EXIST. It cannot reach a typo in a NEW file's name,
and not through any oversight: a file that does not exist yet resolves under no
directory, so the correction ladder has nothing to probe, and "does not read at
base_ref" is exactly what a correct new path and a misspelled one both look
like. No repository lookup can separate them.

The spec can. Run 43's change-surface table said
`ElectricSheepTests/AudioscapeStateTests.swift` while the plan said
`ElectricSheepTests/AudoscapeStateTests.swift` — one missing letter, and the
table had been parsed into `change_surface_path_rows` the whole time. It cost a
plan gate; unnoticed it would have cost an implementer attempt, because
`_missing_planned_outputs` compares planned strings to files on disk literally
and charges the difference.

The bar is deliberately tight. A missed typo is caught at the plan gate, while a
WRONG correction silently retargets a file nobody asked for — so these tests
pin the refusals as hard as the corrections.
"""
import pytest

from coding_model_autonomous.plan_paths import (
    TYPO_MAX_DISTANCE, declared_near_misses, resolve_plan_paths)

# Verbatim from run 43. The plan's spelling, then the spec's.
PLAN_TYPO = "ElectricSheepTests/AudoscapeStateTests.swift"
DECLARED = "ElectricSheepTests/AudioscapeStateTests.swift"
REAL = "ElectricSheep/Audioscape.swift"


def _plan(*outputs, inputs=("spec.md",)):
    return {"phases": [{"name": "implement", "inputs": list(inputs),
                        "outputs": list(outputs)}]}


def _resolve(plan, real=(REAL,), declared=(DECLARED,)):
    return resolve_plan_paths(plan, lambda p: p in set(real),
                              declared_paths=list(declared))


# ── the run-43 fixture ──────────────────────────────────────────────────────

class TestRun43:
    def test_the_typo_is_corrected_to_the_declared_spelling(self):
        report = _resolve(_plan(REAL, PLAN_TYPO))
        assert report.corrections == {PLAN_TYPO: DECLARED}

    def test_the_correction_says_which_anchor_decided_it(self):
        """A reader needs to know whether the file exists elsewhere or the NAME
        was wrong. Those are different problems with the same symptom."""
        report = _resolve(_plan(REAL, PLAN_TYPO))
        corrected = [r for r in report.resolutions if r.status == "corrected"]
        assert [r.source for r in corrected] == ["declared"]

    def test_the_existing_file_is_untouched(self):
        report = _resolve(_plan(REAL, PLAN_TYPO))
        assert REAL not in report.corrections
        assert [r.status for r in report.resolutions if r.path == REAL] == ["resolved"]

    def test_every_occurrence_is_rewritten(self):
        """The typo appeared in phases[implement].outputs AND phases[test].inputs.
        Correcting one and not the other is worse than correcting neither."""
        plan = {"phases": [
            {"name": "implement", "inputs": [REAL], "outputs": [REAL, PLAN_TYPO]},
            {"name": "test", "inputs": [REAL, PLAN_TYPO], "outputs": ["test_report.md"]},
        ]}
        from coding_model_autonomous.plan_paths import apply_corrections
        fixed = apply_corrections(plan, _resolve(plan).corrections)
        assert PLAN_TYPO not in str(fixed)
        assert fixed["phases"][1]["inputs"] == [REAL, DECLARED]


# ── what must NOT be corrected ──────────────────────────────────────────────

class TestRefusals:
    def test_a_genuinely_new_file_is_left_alone(self):
        """Named nowhere in the spec. Creating files is legitimate; this module
        does not get to veto one because it has never heard of it."""
        report = _resolve(_plan("ElectricSheepTests/SomethingElse.swift"))
        assert report.corrections == {}
        assert report.new_paths == ["ElectricSheepTests/SomethingElse.swift"]

    def test_a_longer_name_that_merely_contains_the_declared_one(self):
        """`FooTests` -> `FooBarTests` is three edits, not a slip. A ratio-based
        similarity check passes this at ~0.90 and would retarget a real file."""
        declared = "Tests/FooTests.swift"
        report = _resolve(_plan("Tests/FooBarTests.swift"),
                          declared=(declared,))
        assert report.corrections == {}

    def test_short_basenames_are_never_matched(self):
        """`a.py` and `b.py` are one edit apart and unrelated."""
        report = _resolve(_plan("src/b.py"), declared=("src/a.py",))
        assert report.corrections == {}

    def test_a_different_extension_is_never_a_typo_of_this_one(self):
        report = _resolve(_plan("ElectricSheepTests/AudioscapeStateTests.kt"),
                          declared=(DECLARED,))
        assert report.corrections == {}

    def test_a_path_that_reads_is_never_corrected(self):
        """It exists. Whatever the spec called it, the repository wins."""
        report = _resolve(_plan(REAL), declared=("ElectricSheep/Audioscope.swift",))
        assert report.corrections == {}

    def test_the_declared_twin_already_in_the_plan_blocks_the_correction(self):
        """The plan names BOTH spellings. Collapsing them would drop a file the
        plan asked for, and which one is intended is not this module's call."""
        report = _resolve(_plan(REAL, DECLARED, PLAN_TYPO))
        assert report.corrections == {}

    def test_ambiguity_is_reported_rather_than_guessed(self):
        """Two declared paths within a typo's distance of the same plan path."""
        a, b = "Tests/RenderStateTests.swift", "Tests/RenderStatsTests.swift"
        report = _resolve(_plan("Tests/RenderStateTest.swift"), declared=(a, b))
        assert report.corrections == {}
        amb = [r for r in report.resolutions if r.ambiguous]
        assert len(amb) == 1
        assert sorted(amb[0].ambiguous) == sorted([a, b])
        assert amb[0].source == "declared"

    def test_no_declared_paths_behaves_exactly_as_before(self):
        """A greenfield spec with no change-surface table must be byte-identical
        to the pre-DEV-733 behaviour."""
        plan = _plan(REAL, PLAN_TYPO)
        before = resolve_plan_paths(plan, lambda p: p == REAL)
        after = _resolve(plan, declared=())
        assert before.summary() == after.summary()
        assert after.corrections == {}


# ── the distance bar itself ─────────────────────────────────────────────────

class TestTheBar:
    @pytest.mark.parametrize("typo", [
        "ElectricSheepTests/AudoscapeStateTests.swift",     # dropped letter
        "ElectricSheepTests/AudioscpaeStateTests.swift",    # transposition
        "ElectricSheepTests/AudioscapeStateTest.swift",     # dropped plural
    ])
    def test_one_and_two_edit_slips_are_caught(self, typo):
        assert declared_near_misses(typo, [DECLARED]) == [DECLARED]

    def test_a_wrong_directory_is_corrected_even_with_no_typo(self):
        """Run 42's shape: the right name at a root-level path outside the
        project's synchronized groups, where nothing is ever compiled."""
        assert declared_near_misses("AudioscapeStateTests.swift",
                                    [DECLARED]) == [DECLARED]

    def test_three_edits_is_past_the_bar(self):
        assert declared_near_misses("ElectricSheepTests/AudscpeStateTests.swift",
                                    [DECLARED]) == []

    def test_the_bar_is_two(self):
        """Pinned, because widening it is how a correction starts retargeting
        real files — the failure this module is least able to detect."""
        assert TYPO_MAX_DISTANCE == 2


class TestTheChangeSurfaceIsNotAllPaths:
    """Found by measuring this guard against var/tasks_db/specs before shipping.

    A change-surface table's first column is backticked prose as often as it is
    a path, and `change_surface_path_rows` returns whatever is backticked. Two
    archived specs carry a grep invocation in one, and its BASENAME is a real
    filename — so without a plausibility filter the guard rewrote a correct
    repository path into a shell command. That is the failure mode this module
    fears most: a wrong correction is silent, while a missed typo stops at the
    plan gate.
    """

    COMMAND = ("grep -c 'if Task.isCancelled { break }' "
               "ElectricSheep/HallucinationEngine.swift")
    GOOD = "ElectricSheep/HallucinationEngine.swift"

    def test_a_command_in_the_table_is_never_a_correction_target(self):
        report = resolve_plan_paths(
            _plan("ElectricSheep/HallucinationEngin.swift"),
            lambda _p: False, declared_paths=[self.COMMAND])
        assert report.corrections == {}

    def test_the_real_path_beside_it_still_corrects(self):
        """The filter must reject the command WITHOUT disarming the table."""
        report = resolve_plan_paths(
            _plan("ElectricSheep/HallucinationEngin.swift"),
            lambda _p: False, declared_paths=[self.COMMAND, self.GOOD])
        assert report.corrections == {
            "ElectricSheep/HallucinationEngin.swift": self.GOOD}

    @pytest.mark.parametrize("junk", [
        "see `Foo.swift` and `Bar.swift`",       # prose
        "rm -rf Foo.swift",                      # whitespace
        "Foo*.swift",                            # a glob
        'say "Foo.swift"',                       # quotes
    ])
    def test_nothing_with_shell_or_prose_shape_survives_the_filter(self, junk):
        from coding_model_autonomous.plan_paths import is_plausible_path
        assert not is_plausible_path(junk)

    def test_ordinary_repository_paths_pass_the_filter(self):
        from coding_model_autonomous.plan_paths import is_plausible_path
        for p in (DECLARED, REAL, "src/coding_model_autonomous/plan_paths.py",
                  "Sources/CentipedeCore/World.swift", "a-b_c.1.tsx"):
            assert is_plausible_path(p), p
