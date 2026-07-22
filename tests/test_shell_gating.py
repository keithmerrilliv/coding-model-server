"""Gating tests for REMOTE_EXEC: deny rules, the auto-approve allow-list, and
the shell-mode prompt.

Regression cover for the 2026-07-11 audit (DEV-21/22/23/29): silent execution
used to be granted by "the denylist didn't match", so `find / -delete` and
`python3 -c "shutil.rmtree('/home/u')"` ran unattended in yolo mode.
"""
import logging

import pytest

from coding_model_server import tool_handlers
from coding_model_server.tool_handlers.safety import (
    _check_dangerous_command,
    _check_deny_rules,
    _is_auto_approvable_command,
)


class _Cfg:
    ALLOW_SHELL_MODE = False
    COMMAND_WHITELIST = None
    COMMAND_TIMEOUT = 10
    CHUNK_THRESHOLD = 100_000
    CHUNK_SIZE = 1_000
    CHUNK_OVERLAP = 100


@pytest.fixture
def gated(monkeypatch):
    """Configure tool_handlers in yolo mode with shell exec permitted by mode."""
    cfg = _Cfg()
    tool_handlers.configure(
        config=cfg,
        colors={k: '' for k in ('WARNING', 'FAIL', 'GREEN', 'BOLD', 'ENDC', 'CYAN')},
        print_colored_fn=lambda *a, **k: None,
        logger_inst=logging.getLogger('test'),
        temp_tracker={'add': lambda p: None},
        external_handlers={},
        permission_mode='yolo',
    )
    monkeypatch.setenv('ALLOW_REMOTE_EXEC_YOLO', '1')
    monkeypatch.delenv('EXTRA_AUTO_APPROVE_COMMANDS', raising=False)
    return cfg


# ── deny rules ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize("command", [
    "rm -rf /",
    "rm -rf /*",
    "rm -fr /*",
    "rm -rf /home",
    "rm -rf /home/*",
    "rm -rf /home/keith-merrill",
    "rm -rf /home/keith-merrill/",
    "rm -rf ~",
    "rm -rf $HOME",
    "find / -name '*.py' -delete",
    "find /home/keith-merrill -type f -exec rm {} \\;",
    "python3 -c \"import shutil; shutil.rmtree('/home/keith-merrill')\"",
    "dd if=/dev/zero of=/dev/sda",
    "rm -rf / && echo done",
])
def test_denied_unconditionally(command):
    denied, reason = _check_deny_rules(command)
    assert denied, f"should be denied outright: {command!r}"
    assert reason


@pytest.mark.parametrize("command", [
    # Deleting a path *inside* a home directory is a legitimate agent action.
    # It must warn (dangerous), but it must not be hard-denied.
    "rm -rf /home/keith-merrill/Dev/project/build",
    "rm -rf ./node_modules",
    "rm -rf build",
])
def test_not_denied_but_warns(command):
    denied, _ = _check_deny_rules(command)
    assert not denied, f"should not be hard-denied: {command!r}"
    dangerous, _ = _check_dangerous_command(command)
    assert dangerous, f"should still warn as dangerous: {command!r}"


# ── auto-approve allow-list ──────────────────────────────────────────────────
@pytest.mark.parametrize("command", [
    "ls -la",
    "cat README.md",
    "grep -rn TODO src",
    "rg pattern",
    "wc -l file.txt",
    "pytest tests/ -q",
    "npm test",
    "cargo build",
    "make",
    "ruff check src",
    "git status",
    "git log --oneline -5",
    "git diff HEAD",
    # git config read forms.
    "git config --list",
    "git config --get user.name",
    "git config user.name",
    "git config --global --get user.email",
    "git config --get-regexp alias",
    # Bare env is inspection; printenv only ever prints (it cannot exec).
    "env",
    "printenv HOME",
])
def test_auto_approvable(gated, command):
    ok, reason = _is_auto_approvable_command(command)
    assert ok, f"should auto-approve: {command!r} ({reason})"


@pytest.mark.parametrize("command", [
    # The audit's silent-execution cases — these must now cost a prompt.
    "find / -delete",
    "python3 -c \"import shutil; shutil.rmtree('/tmp/x')\"",
    "node -e 'require(\"fs\").rmSync(\"/tmp/x\")'",
    "bash -c 'rm -rf /tmp/x'",
    "sh script.sh",
    # Destructive / stateful binaries.
    "rm file.txt",
    "mv a b",
    "chmod 777 file",
    "sudo apt install foo",
    "curl https://example.com/install.sh",
    "wget https://example.com/x",
    "ssh host",
    "systemctl restart coding-model-server",
    "docker run alpine",
    # git subcommands that can destroy work.
    "git push --force",
    "git reset --hard HEAD~1",
    "git clean -fd",
    "git checkout .",
    # DEV-114: `git config` writes with the same subcommand it reads with.
    # KEY VALUE persists core.fsmonitor, which a later auto-approved
    # `git status` executes as a subprocess.
    "git config core.fsmonitor /tmp/evil",
    "git config --global core.fsmonitor /tmp/evil",
    "git config --add alias.s status",
    "git config --unset user.name",
    "git config --edit",
    "git config -e",
    "git config --remove-section alias",
    "git config --file /home/u/.aws/credentials --list",
    "git config --file=/home/u/.aws/credentials --list",
    # Chaining/redirection escapes the base-binary check entirely.
    "ls; rm -rf ~",
    "ls && rm -rf /tmp/x",
    "cat file | sh",
    "echo pwned > /etc/passwd",
    "ls $(rm -rf /tmp/x)",
    # Not a plain binary name.
    "VAR=1 pytest",
    # DEV-111: `env` execs its argument — the base-binary check saw only `env`
    # and auto-approved arbitrary code in yolo mode.
    "env python3 evil.py",
    "env VAR=1 python3 evil.py",
    "/usr/bin/env python3 evil.py",
    "env -i sh",
    "env -S 'python3 evil.py'",
    # DEV-115: an allow-listed READ binary pointed at a protected path is the
    # first half of an exfil primitive (read secret → outbound fetch). It must
    # prompt, exactly as READ_FILE does.
    "cat ~/.ssh/id_rsa",
    "head ~/.aws/credentials",
    "cat ~/.ssh/known_hosts",
    "grep -r secret ~/.gnupg",
    "cat .git/config",
    "md5sum ~/.ssh/id_ed25519",
])
def test_not_auto_approvable(gated, command):
    ok, _ = _is_auto_approvable_command(command)
    assert not ok, f"must NOT run silently: {command!r}"


def test_extra_auto_approve_commands_extends_allowlist(gated, monkeypatch):
    assert not _is_auto_approvable_command("bazel build //...")[0]
    monkeypatch.setenv('EXTRA_AUTO_APPROVE_COMMANDS', 'bazel, buck')
    assert _is_auto_approvable_command("bazel build //...")[0]


# ── end-to-end through execute_remote_command ────────────────────────────────
def test_yolo_runs_allowlisted_command_without_prompting(gated, monkeypatch):
    def _no_input(*a, **k):
        raise AssertionError("should not have prompted for an allow-listed command")

    monkeypatch.setattr('builtins.input', _no_input)
    from coding_model_server.tool_handlers.shell import execute_remote_command
    out = execute_remote_command("echo hello")
    assert "hello" in out


def test_yolo_prompts_for_non_allowlisted_command(gated, monkeypatch):
    prompted = []

    def _fake_input(prompt=""):
        prompted.append(prompt)
        return "n"

    monkeypatch.setattr('builtins.input', _fake_input)
    from coding_model_server.tool_handlers.shell import execute_remote_command
    out = execute_remote_command("find /tmp -name x")
    assert prompted, "yolo must prompt for a command that is not allow-listed"
    assert "denied" in out.lower()


def test_shell_mode_never_auto_approves(gated, monkeypatch):
    """shell=True is unrestricted, so even an allow-listed binary must prompt."""
    gated.ALLOW_SHELL_MODE = True
    prompted = []

    def _fake_input(prompt=""):
        prompted.append(prompt)
        return "n"

    monkeypatch.setattr('builtins.input', _fake_input)
    from coding_model_server.tool_handlers.shell import execute_remote_command
    out = execute_remote_command("echo hello")
    assert prompted, "shell mode must prompt even in yolo, even for `echo`"
    assert "denied" in out.lower()


def test_deny_rule_beats_yolo(gated, monkeypatch):
    monkeypatch.setattr('builtins.input', lambda *a, **k: "y")
    from coding_model_server.tool_handlers.shell import execute_remote_command
    out = execute_remote_command("rm -rf /*")
    assert "denied" in out.lower()


def test_remote_exec_actually_runs(gated, monkeypatch):
    """Regression: `tempfile` was used but never imported, so every REMOTE_EXEC
    died with NameError (caught by the broad handler and returned as a string)."""
    monkeypatch.setattr('builtins.input', lambda *a, **k: "y")
    from coding_model_server.tool_handlers.shell import execute_remote_command
    out = execute_remote_command("echo roundtrip-ok")
    assert "roundtrip-ok" in out
    assert "not defined" not in out
