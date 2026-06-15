"""Regression: the test runner must tolerate duplicate test-module basenames.

This reproduces the exact failure that killed spec_031e0aaa on 2026-06-02: the
reviewer wrote `tests/test_spec.py` AND `<outdir>/tests/test_spec.py`, two files
with the same basename in different dirs. Under pytest's default 'prepend'
import mode that aborts collection with `import file mismatch` before any test
runs, so every retry FAILed on a harness artefact rather than the code.

`_run_local_tests` now passes `--import-mode=importlib`, which imports each test
by full path so duplicate basenames coexist. This test fails (collection error,
`passed is False`) without that flag and passes with it.
"""
from coding_model_autonomous import executor


def test_run_local_tests_tolerates_duplicate_test_basenames(tmp_path, monkeypatch):
    spec_dir = tmp_path / "spec"
    (spec_dir / "tests").mkdir(parents=True)
    (spec_dir / "App" / "tests").mkdir(parents=True)

    body = "def test_ok():\n    assert True\n"
    (spec_dir / "tests" / "test_spec.py").write_text(body)
    (spec_dir / "App" / "tests" / "test_spec.py").write_text(body)

    # Run unsandboxed so the regression doesn't depend on bwrap being present
    # in the dev/CI environment — we're exercising the pytest invocation, not
    # the sandbox.
    monkeypatch.setenv("CODING_MODEL_ALLOW_UNSANDBOXED_TESTS", "1")

    passed, output = executor._run_local_tests(spec_dir, "pytest", 60)

    assert passed, f"duplicate-basename collection should succeed; got:\n{output}"
    assert "import file mismatch" not in output
    assert "2 passed" in output
