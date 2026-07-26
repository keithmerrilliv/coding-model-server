"""DEV-103: the pipeline can run JS specs with Node's built-in `node:test`.

Covers the three pieces that make `framework: node_test` work on the Linux
orchestrator:
  1. `_run_local_tests` dispatches node_test to `node --test`.
  2. `_wrap_in_sandbox` binds a configured Node toolchain into the sandbox and
     puts it first on PATH (top-level mountpoint, since /opt is bound read-only).
  3. The orchestrator's structural guard recognizes the node:test TAP summary,
     so a genuine run isn't force-failed as "no summary detected".
"""
import shutil
import subprocess
from pathlib import Path

import pytest

from coding_model_autonomous import test_runner
from coding_model_server import orchestrator_daemon as od


# ── 1. dispatch ──────────────────────────────────────────────────────────────

@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_node_test_dispatch_runs_green(tmp_path, monkeypatch):
    spec_dir = tmp_path / "spec"
    spec_dir.mkdir()
    (spec_dir / "logic.test.js").write_text(
        "const test = require('node:test');\n"
        "const assert = require('node:assert');\n"
        "test('adds', () => { assert.strictEqual(2 + 2, 4); });\n"
    )
    # Unsandboxed so the test exercises the node --test invocation itself, not
    # bwrap (which may be absent/broken in CI). node resolves from the dev PATH.
    monkeypatch.setenv("CODING_MODEL_ALLOW_UNSANDBOXED_TESTS", "1")

    passed, output = test_runner._run_local_tests(spec_dir, "node_test", 60)

    assert passed, f"node:test run should pass; got:\n{output}"
    assert "# fail 0" in output


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_node_test_dispatch_reports_failure(tmp_path, monkeypatch):
    spec_dir = tmp_path / "spec"
    spec_dir.mkdir()
    (spec_dir / "bad.test.js").write_text(
        "const test = require('node:test');\n"
        "const assert = require('node:assert');\n"
        "test('wrong', () => { assert.strictEqual(1, 2); });\n"
    )
    monkeypatch.setenv("CODING_MODEL_ALLOW_UNSANDBOXED_TESTS", "1")

    passed, output = test_runner._run_local_tests(spec_dir, "node_test", 60)

    assert not passed, f"a failing assertion must fail the run; got:\n{output}"
    assert "not ok 1" in output


def test_default_timeouts_has_node_test():
    assert test_runner.DEFAULT_TIMEOUTS.get("node_test") == 120


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_node_test_excludes_retry_history(tmp_path, monkeypatch):
    # Regression (spec_54b2c1b3, 2026-07-15): node --test auto-discovery re-ran
    # snapshots under retry_history/, so a fixed bug kept failing from an old
    # snapshot and a JS spec could never pass once it had retried. The live test
    # files must run; the retry_history snapshot must NOT.
    spec_dir = tmp_path / "spec"
    (spec_dir / "test").mkdir(parents=True)
    (spec_dir / "retry_history" / "retry_0" / "test").mkdir(parents=True)

    (spec_dir / "test" / "live.test.js").write_text(
        "const test = require('node:test');\n"
        "const assert = require('node:assert');\n"
        "test('live passes', () => { assert.ok(true); });\n"
    )
    # A snapshot that would FAIL if re-run.
    (spec_dir / "retry_history" / "retry_0" / "test" / "old.test.js").write_text(
        "const test = require('node:test');\n"
        "const assert = require('node:assert');\n"
        "test('stale snapshot fails', () => { assert.strictEqual(1, 2); });\n"
    )
    monkeypatch.setenv("CODING_MODEL_ALLOW_UNSANDBOXED_TESTS", "1")

    passed, output = test_runner._run_local_tests(spec_dir, "node_test", 60)

    assert passed, f"retry_history snapshot must be excluded; got:\n{output}"
    assert "stale snapshot" not in output
    assert "live passes" in output


# ── 2. sandbox wrapping ──────────────────────────────────────────────────────

def _path_value(args: list[str]) -> str:
    i = args.index("PATH")
    return args[i + 1]


def test_wrap_in_sandbox_binds_node_when_configured(tmp_path, monkeypatch):
    spec_dir = tmp_path / "spec"
    spec_dir.mkdir()
    monkeypatch.setattr(test_runner, "SANDBOX_NODE_ROOT", Path("/fake/node/root"))

    args = test_runner._wrap_in_sandbox(["node", "--test"], spec_dir)

    # The toolchain is bound at the top-level mountpoint (not nested under the
    # read-only /opt bind) and placed first on PATH.
    joined = " ".join(args)
    assert "--ro-bind /fake/node/root /coding-model-node" in joined
    assert _path_value(args).startswith("/coding-model-node/bin:")


def test_wrap_in_sandbox_no_node_bind_when_unset(tmp_path, monkeypatch):
    spec_dir = tmp_path / "spec"
    spec_dir.mkdir()
    monkeypatch.setattr(test_runner, "SANDBOX_NODE_ROOT", None)

    args = test_runner._wrap_in_sandbox(["python", "-m", "pytest"], spec_dir)

    assert "/coding-model-node" not in " ".join(args)
    assert _path_value(args) == "/usr/local/bin:/usr/bin:/bin"


def test_wrap_in_sandbox_skips_bind_for_system_node(tmp_path, monkeypatch):
    # A Node whose bin is already a bound, on-PATH dir needs no extra bind.
    spec_dir = tmp_path / "spec"
    spec_dir.mkdir()
    monkeypatch.setattr(test_runner, "SANDBOX_NODE_ROOT", Path("/usr"))

    args = test_runner._wrap_in_sandbox(["node", "--test"], spec_dir)

    assert "/coding-model-node" not in " ".join(args)
    assert _path_value(args) == "/usr/local/bin:/usr/bin:/bin"


_NVM_NODE = sorted(Path.home().glob(".nvm/versions/node/*/bin/node"))


def _node_version(node_bin) -> str:
    return subprocess.run([str(node_bin), "--version"],
                          capture_output=True, text=True).stdout.strip()


# The bound toolchain is only PROVABLY in use if it differs from whatever Node
# the sandbox already has on PATH via /usr. When they match, this test cannot
# tell the two apart and would pass without the bind doing anything.
_SYS_NODE = shutil.which("node", path="/usr/local/bin:/usr/bin:/bin")
_BIND_IS_DISTINGUISHABLE = bool(_NVM_NODE) and (
    _SYS_NODE is None or _node_version(_NVM_NODE[-1]) != _node_version(_SYS_NODE))


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="bwrap not installed")
@pytest.mark.skipif(not _NVM_NODE, reason="no nvm Node to bind into the sandbox")
@pytest.mark.skipif(not _BIND_IS_DISTINGUISHABLE,
                    reason="nvm and system Node are the same version — the bind "
                           "would be unobservable, so this test would be vacuous")
def test_sandbox_runs_the_BOUND_node_not_the_system_one(tmp_path, monkeypatch):
    """DEV-103's acceptance criterion, executed rather than asserted about.

    Every other test here runs unsandboxed on purpose, and the sandbox tests
    only inspect the argv `_wrap_in_sandbox` builds. That leaves the real risk
    untested: whether bwrap ACCEPTS those arguments and whether `node` actually
    resolves from the BOUND toolchain once /home is masked by tmpfs.

    Asserting merely that a JS test passes in the sandbox does not show that.
    This box also has a system Node at /usr/bin, which is already inside the
    sandbox via the /usr bind — so a green run proves nothing about the bind.
    (Confirmed: with the bind removed, a naive version of this test still
    passed.) Pinning process.version to the bound toolchain's version is what
    makes it discriminating: drop the bind and the system Node answers with a
    different version, and this fails.
    """
    node_root = _NVM_NODE[-1].parent.parent
    expected = _node_version(_NVM_NODE[-1])
    monkeypatch.setattr(test_runner, "SANDBOX_NODE_ROOT", node_root)
    # No CODING_MODEL_ALLOW_UNSANDBOXED_TESTS here — the point is the sandbox.
    monkeypatch.delenv("CODING_MODEL_ALLOW_UNSANDBOXED_TESTS", raising=False)

    spec_dir = tmp_path / "spec"
    spec_dir.mkdir()
    (spec_dir / "logic.test.js").write_text(
        "const test = require('node:test');\n"
        "const assert = require('node:assert');\n"
        "test('runs in the sandbox', () => { assert.strictEqual(2 + 2, 4); });\n"
        f"test('uses the bound toolchain', () => {{ "
        f"assert.strictEqual(process.version, '{expected}'); }});\n"
    )

    passed, output = test_runner._run_local_tests(spec_dir, "node_test", 60)

    assert passed, (
        f"sandboxed node:test run failed (expected the bound {expected}):\n{output}")
    assert "# pass 2" in output
    assert "# fail 0" in output


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="bwrap not installed")
@pytest.mark.skipif(not _NVM_NODE, reason="no nvm Node to bind into the sandbox")
def test_sandboxed_node_test_has_no_network(tmp_path, monkeypatch):
    """The sandbox runs --unshare-all, so LLM-written tests cannot reach the
    network. Worth proving on the Node path specifically: `node --test` is new
    here, and a sandbox that silently stopped isolating would look identical to
    one that works."""
    node_root = _NVM_NODE[-1].parent.parent
    monkeypatch.setattr(test_runner, "SANDBOX_NODE_ROOT", node_root)
    monkeypatch.delenv("CODING_MODEL_ALLOW_UNSANDBOXED_TESTS", raising=False)

    spec_dir = tmp_path / "spec"
    spec_dir.mkdir()
    # Connecting to a routable address must fail inside an unshared netns.
    (spec_dir / "net.test.js").write_text(
        "const test = require('node:test');\n"
        "const assert = require('node:assert');\n"
        "const net = require('node:net');\n"
        "test('no network', (t, done) => {\n"
        "  const s = net.connect(53, '1.1.1.1');\n"
        "  s.on('connect', () => { s.destroy(); done(new Error('network reachable')); });\n"
        "  s.on('error', () => { done(); });\n"
        "});\n"
    )

    passed, output = test_runner._run_local_tests(spec_dir, "node_test", 60)

    assert passed, f"expected the connection to be refused inside the sandbox:\n{output}"


# ── 3. structural guard ──────────────────────────────────────────────────────

_NODE_TAP = (
    "TAP version 13\n# Subtest: adds\nok 1 - adds\n1..1\n"
    "# tests 1\n# suites 0\n# pass 1\n# fail 0\n# duration_ms 27.8\n"
)


def test_validator_accepts_node_test_summary():
    ok, reason = od._validate_test_output_structure(_NODE_TAP, "node_test")
    assert ok, reason


def test_validator_rejects_node_test_without_summary():
    # A sandbox/collection error that exits without the TAP footer must not be
    # trusted as a real run.
    ok, reason = od._validate_test_output_structure("node: command not found", "node_test")
    assert not ok
    assert "node:test summary" in reason


def test_validator_rejects_empty_node_test_output():
    ok, reason = od._validate_test_output_structure("   ", "node_test")
    assert not ok
