"""DEV-838: every reader of compiler and test-runner output is one module.

``diagnostics.read`` composes the parsers the daemon, ``outcome``, the Swift
rules and ``gate_output`` used separately, and returns one report.
The fixtures are real runner output (tests/fixtures/README.md), so these pin
what the pipeline reads today rather than what a hand-written log would say.
"""
import ast
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from coding_model_autonomous import diagnostics as dg
from fixture_files import load_fixture

_SRC = Path(__file__).resolve().parent.parent / "src"


# ── read() on real output ────────────────────────────────────────────────────

def test_swift_compile_failure_is_compile_failed():
    # Two located errors, one style warning, and swiftc's bare `error:
    # fatalError` trailer.
    r = dg.read(load_fixture("dev838_swift_compile_failure.txt"), "swift_test",
                passed=False)
    assert r.verdict == "compile_failed"
    assert r.build_failure.endswith(
        "MushroomField.swift:12:24: error: cannot use mutating member on "
        "immutable value: 'rng' is a 'let' c")
    assert r.diagnostics == (
        "cannot use mutating member on immutable value: 'rng' is a 'let' constant",
        "left side of mutating operator isn't mutable: 'mushroom' is a 'let' constant",
    )
    assert [(w.path, w.line, w.blocking) for w in r.warnings] == [
        ("Sources/CentipedeCore/CentipedeWorld.swift", 71, False)]
    assert (r.roster, r.total, r.crash, r.pass_rate) == ((), None, None, None)


def test_a_passing_run_is_passed_whatever_the_log_says():
    # The runner's result is authoritative: detect_build_failure is only ever
    # consulted on a failing run.
    r = dg.read(load_fixture("dev838_swift_compile_failure.txt"), "swift_test",
                passed=True)
    assert r.verdict == "passed"
    assert r.build_failure is None
    assert len(r.diagnostics) == 2


def test_xcodebuild_test_failure_names_the_failed_cases():
    r = dg.read(load_fixture("dev838_xcodebuild_test_failure.txt"),
                "xcodebuild_test", passed=False)
    assert r.verdict == "tests_failed"
    assert r.roster == (
        "DtypeContainmentTests/a1_float64_input_rejected_with_readable_error()",
        "DtypeContainmentTests/a2_all_strategies_process_valid_float32_correctly()",
    )
    assert (r.passed, r.failed, r.total) == (6, 2, 8)
    assert r.pass_rate == 0.75
    assert r.diagnostics == () and r.build_failure is None


def test_pytest_pass_counts_its_passes():
    r = dg.read(load_fixture("dev838_pytest_pass.txt"), "pytest", passed=True)
    assert r.verdict == "passed"
    assert (r.passed, r.failed, r.total, r.roster) == (4, 0, 4, ())
    assert r.pass_rate == 1.0


def test_node_test_harness_failure_is_a_test_run_the_roster_cannot_read():
    # Every file failed to import, and node:test still printed its TAP footer.
    # The pass rate reads that footer; the gate's roster parser has no TAP
    # shape, so the counts are unknown rather than zero.
    r = dg.read(load_fixture("dev838_node_test_harness_failure.txt"),
                "node_test", passed=False)
    assert r.verdict == "tests_failed"
    assert r.pass_rate == pytest.approx(31 / 71)
    assert (r.passed, r.failed, r.total, r.roster) == (None, None, None, ())
    assert r.build_failure is None and r.crash is None


def test_swift_test_crash_is_crashed_not_compile_failed():
    # Run 9 of DEV-102: Build complete!, then the harness took signal 5.
    text = load_fixture("dev838_swift_test_crash.txt")
    r = dg.read(text, "swift_test", passed=False)
    assert r.verdict == "crashed"
    assert r.crash == "the test process exited on signal 5 before any test reported"
    assert r.build_failure is None
    (w,) = r.warnings
    assert (w.path, w.diag_id, w.blocking) == (
        "Sources/CentipedeCore/World.swift", "no-usage", True)
    # A protected file's warning is recorded but never blocks.
    (w,) = dg.read(text, "swift_test", passed=False,
                   protected_paths=["Sources/CentipedeCore/World.swift"]).warnings
    assert w.blocking is False


def test_no_evidence_is_inconclusive():
    r = dg.read("ssh: connect to host studio port 22: Connection refused\n",
                "swift_test", passed=False)
    assert r.verdict == "inconclusive"
    assert r.verdict in dg.VERDICTS


# ── the old names still reach the moved code ─────────────────────────────────

def test_old_names_are_aliases_of_the_moved_code():
    from coding_model_autonomous import gate_output, outcome
    from coding_model_server import orchestrator_daemon as d

    assert outcome.attributed_diagnostics is dg.attributed_diagnostics
    assert outcome.repo_relative is dg.repo_relative
    assert outcome._diagnostic_identity is dg.diagnostic_identity
    assert outcome.ANSI_SGR_RE is dg.ANSI_SGR_RE
    assert gate_output.summarize_test_output is dg.summarize_test_output
    assert d._detect_build_failure is dg.detect_build_failure
    assert d._observed_a_test_run is dg.observed_a_test_run
    assert d._parse_build_warnings is dg.parse_build_warnings
    assert d._test_pass_rate is dg.pass_rate
    assert d.BuildWarning is dg.BuildWarning


# ── structure ────────────────────────────────────────────────────────────────

def _moved_patterns() -> set:
    """The pattern text of every regex diagnostics.py owns."""
    found: set = set()

    def walk(value):
        if isinstance(value, re.Pattern):
            found.add(value.pattern)
        elif isinstance(value, dict):
            for v in value.values():
                walk(v)
        elif isinstance(value, (tuple, list)):
            for v in value:
                walk(v)

    for name, value in vars(dg).items():
        if name.isupper() or name.endswith("_RE") or name.endswith("_RES"):
            walk(value)
    return found


def _compiled_patterns(path: Path) -> set:
    """The literal first argument of every ``re.compile`` call in *path*."""
    out: set = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "compile"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "re"
                and node.args and isinstance(node.args[0], ast.Constant)):
            out.add(node.args[0].value)
    return out


@pytest.mark.parametrize("module", [
    "coding_model_server/orchestrator_daemon.py",
    "coding_model_autonomous/outcome.py",
    "coding_model_autonomous/languages/swift/rules.py",
    "coding_model_autonomous/citations.py",
    "coding_model_autonomous/gate_output.py",
])
def test_no_moved_regex_is_compiled_anywhere_else(module):
    moved = _moved_patterns()
    assert len(moved) > 30  # the walk found the module's regexes at all
    assert _compiled_patterns(_SRC / module) & moved == set()


def test_diagnostics_is_a_leaf():
    tree = ast.parse((_SRC / "coding_model_autonomous/diagnostics.py")
                     .read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add("." if node.level else (node.module or "").split(".")[0])
    assert imported <= {"__future__", "re", "dataclasses", "typing"}, imported


def test_the_kernel_imports_without_the_executor():
    code = (
        "import sys\n"
        "import coding_model_autonomous.diagnostics\n"
        "for m in ('workspace', 'outcome', 'context', 'retry_policy',\n"
        "          'test_runner', 'db', 'models'):\n"
        "    __import__('coding_model_autonomous.' + m)\n"
        "assert 'coding_model_autonomous.executor' not in sys.modules\n"
        "assert not [m for m in sys.modules if m.startswith('coding_model_server')]\n"
    )
    env = dict(os.environ, PYTHONPATH=str(_SRC))
    subprocess.run([sys.executable, "-c", code], check=True, env=env)
