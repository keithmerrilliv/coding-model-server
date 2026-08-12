"""The Apple Deep Docs MCP child runs with a scrubbed environment (DEV-485).

The vendored MCP is third-party code, auto-updated monthly, spawned as a child
of the inference server, and reachable by autonomous agents through
<<<APPLE_DEEP_DOCS>>>. It used to inherit the server's full environment —
including ADMIN_API_KEY — and a 2026-08 upstream release added a dormant
sandboxed-Python execution mode gated on CODE_EXECUTION_MODE. These tests pin
both halves of the fix: no server secrets reach the child, and the execution
mode is forced off regardless of what the server inherited.
"""
from unittest import mock

from coding_model_server import mcp_service
from coding_model_server.mcp_service import _scrubbed_child_env


def test_admin_key_and_secrets_are_stripped(monkeypatch):
    monkeypatch.setenv("ADMIN_API_KEY", "super-secret-admin")
    monkeypatch.setenv("MAC_RUNNER_API_KEY", "runner-key")
    monkeypatch.setenv("JIRA_API_TOKEN", "jira-tok")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-xxx")
    monkeypatch.setenv("SOME_PASSWORD", "hunter2")
    monkeypatch.setenv("GH_SECRET", "s3cr3t")

    env = _scrubbed_child_env()

    for leaked in ("ADMIN_API_KEY", "MAC_RUNNER_API_KEY", "JIRA_API_TOKEN",
                   "ANTHROPIC_API_KEY", "SOME_PASSWORD", "GH_SECRET"):
        assert leaked not in env, f"{leaked} must not reach the MCP child"


def test_benign_env_survives(monkeypatch):
    # The MCP still needs to run: PATH to find binaries, HOME for its venv/caches.
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("HOME", "/home/someone")
    monkeypatch.setenv("LANG", "en_US.UTF-8")

    env = _scrubbed_child_env()

    assert env.get("PATH") == "/usr/bin:/bin"
    assert env.get("HOME") == "/home/someone"
    assert env.get("LANG") == "en_US.UTF-8"


def test_code_execution_mode_pinned_off_even_if_set(monkeypatch):
    # The dormant execution mode must not be switchable via the inherited env.
    monkeypatch.setenv("CODE_EXECUTION_MODE", "true")

    env = _scrubbed_child_env()

    assert env["CODE_EXECUTION_MODE"] == "0"


def test_code_execution_mode_pinned_off_when_absent(monkeypatch):
    monkeypatch.delenv("CODE_EXECUTION_MODE", raising=False)

    env = _scrubbed_child_env()

    assert env["CODE_EXECUTION_MODE"] == "0"


def test_spawn_passes_the_scrubbed_env(monkeypatch, tmp_path):
    """_spawn must hand the scrubbed env to Popen, not inherit the server's."""
    monkeypatch.setenv("ADMIN_API_KEY", "super-secret-admin")

    svc = mcp_service.AppleDeepDocsService(mcp_path=str(tmp_path))
    # Make the venv-python existence check pass without a real venv.
    monkeypatch.setattr(mcp_service.os.path, "exists", lambda _p: True)

    captured = {}

    def _fake_popen(argv, **kwargs):
        captured["env"] = kwargs.get("env")
        return mock.MagicMock(name="proc")

    monkeypatch.setattr(mcp_service.subprocess, "Popen", _fake_popen)
    svc._spawn()

    assert captured["env"] is not None, "_spawn must pass an explicit env"
    assert "ADMIN_API_KEY" not in captured["env"]
    assert captured["env"]["CODE_EXECUTION_MODE"] == "0"
