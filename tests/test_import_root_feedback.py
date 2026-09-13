"""Tests for import root feedback hinting (DEV-660)."""
from coding_model_autonomous.outcome import (
    Failure,
    FailureClass,
    Hooks,
    classify_test_run,
    dispose,
    import_root_hint,
    with_import_root_hint,
)
from coding_model_autonomous.db import Database
from coding_model_autonomous import GateType, SpecStatus

SRC_OUT = "E   ModuleNotFoundError: No module named 'src.coding_model_autonomous.workspace'"
NUMPY_OUT = "E   ModuleNotFoundError: No module named 'numpy'"


def test_T1_import_root_hint_returns_non_empty_with_required_substrings():
    """T1 — import_root_hint(SRC_OUT) returns non-empty string containing required substrings."""
    h = import_root_hint(SRC_OUT)
    assert len(h) > 0
    substrings = [
        "import root is wrong",
        "coding_model_autonomous",
        "from coding_model_autonomous.<module> import <name>",
        "is not a package",
        "Do not change the module's own imports",
    ]
    for s in substrings:
        assert s in h, f"Missing substring {s!r}"


def test_T2_import_root_hint_numpy_returns_empty():
    """T2 — import_root_hint(NUMPY_OUT) == ""."""
    result = import_root_hint(NUMPY_OUT)
    assert result == ""


def test_T3_import_root_hint_empty_and_bare_src_return_empty():
    """T3 — import_root_hint('') == '' and import_root_hint("No module named 'src'") == ""."""
    r1 = import_root_hint("")
    r2 = import_root_hint("No module named 'src'")
    assert r1 == ""
    assert r2 == ""


def test_T4_classify_test_run_returns_BUILD_FAILURE_for_both_outputs():
    """T4 — classify_test_run with SRC_OUT or NUMPY_OUT both return Failure whose cls is BUILD_FAILURE."""
    f_src = classify_test_run(
        SRC_OUT,
        role="implementer",
        passed=False,
        build_reason=SRC_OUT,
        unreachable=False,
        packages=("coding_model_autonomous",),
    )
    f_np = classify_test_run(
        NUMPY_OUT,
        role="implementer",
        passed=False,
        build_reason=NUMPY_OUT,
        unreachable=False,
        packages=("coding_model_autonomous",),
    )
    assert f_src.cls is FailureClass.BUILD_FAILURE
    assert f_np.cls is FailureClass.BUILD_FAILURE


def test_T5_with_import_root_hint_prepends_hint_to_build_failure_feedback():
    """T5 — with_import_root_hint on a BUILD_FAILURE prepends hint to feedback; result is same object; idempotent second application leaves feedback unchanged."""
    initial_fb = "## The code does not compile\n\n" + SRC_OUT
    f = Failure(FailureClass.BUILD_FAILURE, "implementer", "build_check", SRC_OUT, feedback=initial_fb)
    
    # First call mutates in place and returns same object
    g = with_import_root_hint(f)
    assert g is f
    
    # Feedback starts with hint, ends with original, contains the header
    hint_text = import_root_hint(SRC_OUT)
    assert f.feedback.startswith(hint_text)
    assert f.feedback.endswith(SRC_OUT)
    assert "## The code does not compile" in f.feedback
    
    # Second call is idempotent
    original_feedback = f.feedback
    _ = with_import_root_hint(f)
    assert f.feedback == original_feedback


def test_T6_with_import_root_hint_leaves_non_BUILD_FAILURE_unchanged():
    """T6 — TESTS_FAILED Failure passed through with_import_root_hint has feedback unchanged."""
    f = Failure(
        FailureClass.TESTS_FAILED,
        "implementer",
        "tests",
        SRC_OUT,
        feedback=SRC_OUT,
    )
    orig_fb = f.feedback
    _ = with_import_root_hint(f)
    assert f.feedback == orig_fb


def test_T7_with_import_root_hint_leaves_NON_SRC_BUILD_FAILURE_unchanged():
    """T7 — BUILD_FAILURE with NUMPY_OUT passed through with_import_root_hint has feedback unchanged."""
    f = Failure(FailureClass.BUILD_FAILURE, "implementer", "build_check", NUMPY_OUT, feedback=NUMPY_OUT)
    orig_fb = f.feedback
    _ = with_import_root_hint(f)
    assert f.feedback == orig_fb


def test_T8_end_to_end_through_dispose(tmp_path):
    """T8 — end to end through dispose using temporary Database results in action=='charge'; one CODE_REVIEW gate whose reviewer_notes starts with hint and ends with SRC_OUT."""
    db = Database(db_path=tmp_path / "t.sqlite", workspace_root=tmp_path / "ws")
    try:
        spec = db.create_spec(title="t", source_md_path="spec.md", status=SpecStatus.EXECUTING)
        impl_task = db.get_task(
            db.create_task(spec_id=spec.id, agent="implementer", role="implementer", title="implement").id
        )
        hooks = Hooks(max_retries=lambda: 5, synthesize=None, supervisor=None, reviewer_parse_retries=lambda: 1)
        
        f = Failure(FailureClass.BUILD_FAILURE, "implementer", "build_check", SRC_OUT, feedback=SRC_OUT)
        d = dispose(db, spec, impl_task, f, hooks)
        
        assert d.action == "charge"
        
        gates = db.list_gates_for_spec(spec.id, GateType.CODE_REVIEW)
        assert len(gates) == 1
        
        hint_text = import_root_hint(SRC_OUT)
        notes = gates[0].reviewer_notes
        assert notes.startswith(hint_text), f"Notes should start with hint:\n{notes[:200]}"
        assert notes.endswith(SRC_OUT), f"Notes should end with SRC_OUT:\n{notes[-200:]}"
    finally:
        db.close_all()
