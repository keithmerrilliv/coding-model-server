"""DEV-483 — the plan gate refuses acceptance criteria no run can decide.

The pipeline observes one test-runner dispatch: one worktree at base_ref,
the patch applied, one run. Two ElectricSheep plans (spec_837b167f,
spec_f47132ab) asked for a pre-fix run, a git listing and an exact warning
count; every implementation failed them, an hour of Mac time each. The
replay of their criteria is the acceptance test here, and the benign set is
DEV-440's: a guard that fires on good plans costs more than it saves.
"""
from unittest import mock

import coding_model_server.orchestrator_daemon as d
from coding_model_autonomous import plan_criteria as pc
from coding_model_autonomous.models import GateType, SpecStatus

# Verbatim from the two plans' gates (gate_0e3cb767, gate_81fad907).
REAL_BAD = [
    "Pre-fix test run fails exit status 65 with maskPreservesIndices() failed and "
    "maskSuppressesNonTopK(k:) failing twice/passing once (k=8 control).",
    "Against main, test target fails to build due to missing symbols; after change "
    "builds clean. Report must state this explicitly.",
    "git status --porcelain shows exactly M ElectricSheep/ForcingStrategy.swift and "
    "?? ElectricSheepTests/RestrictedSamplingTests.swift. No pbxproj changes.",
    "git diff --name-only lists exactly four paths and nothing else",
    "Build output contains exactly two pre-existing warning lines",
    "if Task.isCancelled { break } count=1",
]

# Verbatim from later archived plans, each one asking for a second run or a git
# listing; the narrowed patterns must still catch them (DEV-943).
ARCHIVE_BAD = [
    # spec_1b927743 A5
    "Against main, test target fails to compile due to missing symbols; after change "
    "it builds and passes.",
    # spec_1b927743 A9a
    "git diff --name-only lists exactly the four specified files; project.pbxproj is untouched.",
    # spec_beb39733 A4 (spec_4f01a833 and spec_e257f925 carry the same criterion)
    "The new test file lives at ElectricSheepTests/<name>.swift (no SPM-style paths), and "
    "the suite's reported test count STRICTLY EXCEEDS the pre-change count. A run executing "
    "only pre-existing tests must read as failure of this criterion.",
    # spec_5aeaaad8
    "A unit test constructs known logits/top-k, applies corrupt+softmax, asserts non-top-k "
    "probs < 1e-6 and top-k sum ~1.0. Test must fail against main and pass after fix; "
    "report explicitly states this.",
    "Build succeeds for the macOS target with no new warnings.",
]

BENIGN = [
    "maskPreservesIndices() and maskSuppressesNonTopK(k:) pass in RestrictedSamplingTests",
    "The test target builds with zero errors",
    "ElectricSheepTests/RestrictedSamplingTests.swift exists and exercises k=8",
    "topK(8) over a 16-element logit array returns exactly 8 indices, all from the top 8 values",
    "The function returns an empty array for an empty input and does not throw",
    "All existing tests in ForcingStrategyTests continue to pass",
    "Warnings in the changed files are zero",
    "The diff to Package.swift is limited to the new test target",  # 'diff' as a noun, not git
    # DEV-943: three criteria of DONE specs the first patterns bounced.
    # spec_e6dcee50: "prefix" is a string, not a pre-fix run.
    "T7: with_import_root_hint on BUILD_FAILURE with NUMPY_OUT leaves feedback unchanged "
    "(no src. prefix match)",
    # spec_0c2093f3: a count inside a value tuple, asserted by a test.
    "That geometry drawRange start=0 count=0 (empty)",
    # spec_a0305340: "before/after" names a function's two inputs.
    "New tests in tests/test_count_test_declarations.py cover criteria 2-5 including "
    "identical before/after returning empty dict; at least 8 test functions; no hard-coded "
    "total assertions",
]


def _plan(criteria):
    lines = ["test_strategy:", "  framework: pytest", "  required: true",
             "acceptance_criteria:"]
    lines += [f"  - {c!r}" for c in criteria]
    return "\n".join(lines) + "\n"


class TestReplay:
    def test_every_criterion_from_the_two_plans_is_flagged(self):
        problems = pc.unevaluable_criteria(_plan(REAL_BAD))
        assert len(problems) == len(REAL_BAD)
        for i, text in enumerate(REAL_BAD, start=1):
            assert any(f"criterion {i} " in p and text[:40] in p for p in problems), text

    def test_each_problem_says_why_and_what_instead(self):
        problems = pc.unevaluable_criteria(_plan(REAL_BAD))
        assert "before/after" in problems[0] and "Instead," in problems[0]
        assert "git or worktree" in problems[2] and "protected_paths" in problems[2]
        assert "incidental output" in problems[4] and "name the test cases" in problems[4]

    def test_every_later_archived_true_case_is_still_flagged(self):
        problems = pc.unevaluable_criteria(_plan(ARCHIVE_BAD))
        assert len(problems) == len(ARCHIVE_BAD)
        for i, text in enumerate(ARCHIVE_BAD, start=1):
            assert any(f"criterion {i} " in p for p in problems), text

    def test_one_reason_per_criterion(self):
        """criterion 3 hits both the git and the exact-count shape; the planner
        needs one actionable sentence, not a pile."""
        problems = pc.unevaluable_criteria(_plan([REAL_BAD[2]]))
        assert len(problems) == 1


class TestNoFalseRejections:
    def test_decidable_criteria_pass_unchanged(self):
        assert pc.unevaluable_criteria(_plan(BENIGN)) == []

    def test_a_plan_without_criteria_is_not_this_checks_problem(self):
        assert pc.unevaluable_criteria("test_strategy:\n  framework: pytest\n") == []
        assert pc.unevaluable_criteria("acceptance_criteria: just a string\n") == []

    def test_malformed_yaml_is_dev_426s_problem(self):
        assert pc.unevaluable_criteria("acceptance_criteria: [: :") == []
        assert pc.unevaluable_criteria("- a\n- b\n") == []

    def test_criteria_are_read_as_strings_whatever_the_planner_emitted(self):
        assert pc.plan_criteria("acceptance_criteria:\n  - 1\n  - {a: b}\n") == ["1", "{'a': 'b'}"]


# ── the transition ───────────────────────────────────────────────────────────

SPEC_MD = "# demo\n\n## test_strategy\n\n    framework: pytest\n    required: true\n"


def _accept(db, yaml_text, spec_md=SPEC_MD):
    spec = db.create_spec(title="demo", source_md_path="spec.md")
    spec_dir = db.spec_dir(spec.id)
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / "spec.md").write_text(spec_md)
    d._accept_plan(db, db.get_spec(spec.id), spec_dir, mock.Mock(yaml_text=yaml_text))
    return db.get_spec(spec.id)


class TestPlanGate:
    def test_an_unevaluable_criterion_never_reaches_a_reviewer(self, db):
        spec = _accept(db, _plan([REAL_BAD[0], BENIGN[0]]))
        assert not [g for g in db.list_gates_for_spec(spec.id)
                    if g.gate_type is GateType.PLAN_APPROVAL]
        clar = db.list_gates_for_spec(spec.id, GateType.CLARIFICATION)
        assert len(clar) == 1
        notes = clar[0].reviewer_notes
        assert notes.startswith("## Plan rejection feedback")
        assert "Pre-fix" in notes and "criterion 1" in notes
        assert "criterion 2" not in notes
        # The remedy is the criteria one, not DEV-426's "move it under test_strategy".
        assert "one test-runner dispatch" in notes
        assert "structured keys under `test_strategy`" not in notes
        assert spec.status is SpecStatus.PENDING_PLAN

    def test_a_plan_with_decidable_criteria_opens_the_gate(self, db):
        spec = _accept(db, _plan(BENIGN))
        gates = [g for g in db.list_gates_for_spec(spec.id)
                 if g.gate_type is GateType.PLAN_APPROVAL]
        assert len(gates) == 1 and spec.status is SpecStatus.PLAN_REVIEW

    def test_test_strategy_problems_still_get_their_own_guidance(self, db):
        """DEV-426's remedy text is unchanged for DEV-426's problems."""
        apple = SPEC_MD.replace("framework: pytest", "framework: xcodebuild_test")
        spec = _accept(db, "test_strategy:\n  framework: xcodebuild_test\n  required: true\n",
                       spec_md=apple)
        clar = db.list_gates_for_spec(spec.id, GateType.CLARIFICATION)
        assert clar and "structured keys under `test_strategy`" in clar[0].reviewer_notes


class TestPlannerPrompt:
    def test_the_planner_is_told_the_observation_model(self):
        from coding_model_autonomous import planner
        text = planner.PLANNER_SYSTEM_PROMPT if hasattr(planner, "PLANNER_SYSTEM_PROMPT") else \
            next(v for k, v in vars(planner).items()
                 if isinstance(v, str) and "ACCEPTANCE CRITERIA REST ONLY" in v)
        assert "one test-runner dispatch" in text
        assert "protected_paths" in text
        assert "before/after" in text
