"""DEV-644: the prompts state the sandbox's import root.

The sandbox puts the repository's `src/` directory on sys.path, so `src/` is
the package root and is NOT itself a package. Nothing in any prompt said so,
and the workspace's `src/<pkg>/...` layout invited the wrong prefix: on run 24
three different implementers each wrote `from src.coding_model_autonomous...`
and burned the rotation on ModuleNotFoundError at collection. Two more runs
(28, 29) then died trying to deliver this change through the pipeline itself,
both on the size of executor.py rather than on the change — so it is landed
by hand. These are the spec's nine acceptance criteria, T1–T9, verbatim.
"""
from coding_model_autonomous.executor import (
    build_implementer_message, build_reviewer_message, import_packages,
    render_import_root,
)

PY_PATHS = ["src/coding_model_autonomous/executor.py", "tests/test_x.py"]
SWIFT = [("Sources/Core/Game.swift", "struct Game {}")]


def _user(messages):
    return "\n".join(m["content"] for m in messages if m["role"] == "user")


def test_T1_only_src_packages():
    assert import_packages(PY_PATHS) == ["coding_model_autonomous"]


def test_T2_sorted_and_unique():
    assert import_packages(["src/b/y.py", "src/a/x.py", "src/a/z.py"]) == ["a", "b"]


def test_T3_non_python_yields_nothing():
    assert import_packages(["Sources/Core/Game.swift", "README.md", "src/pkg/"]) == []


def test_T4_render_is_empty_without_packages():
    assert render_import_root([]) == ""
    assert render_import_root(["Sources/Core/Game.swift"]) == ""


def test_T5_render_names_the_package_and_forbids_src():
    out = render_import_root(PY_PATHS)
    assert "Import root" in out
    assert "from coding_model_autonomous.<module> import" in out
    assert "`coding_model_autonomous`" in out
    assert "No module named 'src.coding_model_autonomous'" in out
    assert out.endswith("\n\n")  # spec item 4: concatenates cleanly


def test_T6_implementer_prompt_carries_it():
    text = _user(build_implementer_message(
        "spec", "design",
        existing_files=[("src/coding_model_autonomous/executor.py", "x = 1")],
        new_files=["tests/test_x.py"]))
    assert "Import root" in text and "coding_model_autonomous" in text


def test_T7_a_swift_implementer_prompt_is_unchanged():
    assert "Import root" not in _user(
        build_implementer_message("spec", "design", existing_files=SWIFT))


def test_T8_reviewer_prompt_carries_it():
    text = _user(build_reviewer_message(
        "spec", "design",
        [("src/coding_model_autonomous/executor.py", "x = 1"),
         ("tests/test_x.py", "def test_a(): pass")]))
    assert "Import root" in text and "coding_model_autonomous" in text


def test_T9_a_swift_reviewer_prompt_is_unchanged():
    assert "Import root" not in _user(build_reviewer_message("spec", "design", SWIFT))


def test_the_section_is_not_itself_a_parseable_file_block():
    """DEV-655 taught the lesson: instructional text must never parse as a
    write. This section carries no <<<FILE:>>> markers."""
    from coding_model_autonomous.executor import parse_implementer_response
    out = render_import_root(PY_PATHS)
    assert "<<<" not in out
    assert getattr(parse_implementer_response(out), "files", []) == []
