"""DEV-599: the architect receives the files the plan will modify.

Run 16's architect invented an API for a file it never saw; run 20's
(nothink arm) refused to design at all — "Let me examine the executor
module" — three times. The design phase now gets the same existing-file
context DEV-571 gave the implementer, rendered distinctly from the
read-only protected references.
"""
from coding_model_autonomous.executor import build_architect_message


def _user(msgs):
    return msgs[1]["content"]


def test_existing_files_render_with_modify_heading():
    msgs = build_architect_message(
        "# Spec", existing_files=[("src/x.py", "def real_api(): pass")])
    body = _user(msgs)
    assert "files the plan will MODIFY" in body
    assert "def real_api(): pass" in body
    assert "Do not assume or invent an API" in body


def test_existing_precedes_reference_and_spec():
    msgs = build_architect_message(
        "# SPEC-MARKER",
        existing_files=[("src/x.py", "EXISTING-MARKER")],
        reference_files=[("src/p.py", "PROTECTED-MARKER")])
    body = _user(msgs)
    assert body.index("EXISTING-MARKER") < body.index("PROTECTED-MARKER")
    assert body.index("PROTECTED-MARKER") < body.index("# SPEC-MARKER")


def test_absent_existing_files_add_no_section():
    body = _user(build_architect_message("# Spec"))
    assert "files the plan will MODIFY" not in body


def test_empty_list_adds_no_section():
    body = _user(build_architect_message("# Spec", existing_files=[]))
    assert "files the plan will MODIFY" not in body
