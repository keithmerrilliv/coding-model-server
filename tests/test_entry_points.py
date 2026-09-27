"""The console scripts in pyproject.toml start and parse their arguments.

Each script runs in a subprocess, the way the installed wrapper runs it:
import the module, call the function, exit with what it returns. A subprocess
also keeps coding_model_client.autonomous's import-time load_dotenv out of the
suite's environment. HOME is a temp dir so the chat client's log file lands
there, and the server address points at a closed local port, so nothing here
can reach a real server.
"""
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["scripts"]
CLOSED_PORT = "9"          # discard; nothing listens, so a connect is refused


def _run(script: str, *args: str, home: Path) -> subprocess.CompletedProcess:
    module, func = SCRIPTS[script].split(":")
    # The form setuptools' generated wrapper uses. "import pkg.mod as m" would
    # not do: coding_model_client/__init__ rebinds .main to the function.
    launcher = (f"import sys; from {module} import {func}; "
                f"sys.argv[0] = {script!r}; sys.exit({func}())")
    env = {**os.environ, "HOME": str(home),
           "PYTHONPATH": os.pathsep.join([str(ROOT / "src"), str(ROOT)]),
           "CODING_MODEL_SERVER_IP": "127.0.0.1",
           "CODING_MODEL_SERVER_PORT": CLOSED_PORT}
    return subprocess.run([sys.executable, "-c", launcher, *args], cwd=home,
                          env=env, capture_output=True, text=True, timeout=60)


def test_the_two_scripts_are_the_ones_these_tests_cover():
    assert SCRIPTS == {
        "coding-model-client": "coding_model_client.main:main",
        "coding-model-autonomous": "coding_model_client.autonomous:main",
    }


# ── coding-model-client ──────────────────────────────────────────────────────

def test_client_help_exits_zero_with_usage(tmp_path):
    r = _run("coding-model-client", "--help", home=tmp_path)
    assert r.returncode == 0, r.stderr
    assert r.stdout.startswith("usage: coding-model-client")
    assert "--model" in r.stdout and "--name" in r.stdout


def test_client_rejects_an_unknown_flag(tmp_path):
    r = _run("coding-model-client", "--no-such-flag", home=tmp_path)
    assert r.returncode == 2
    assert "unrecognized arguments: --no-such-flag" in r.stderr


# ── coding-model-autonomous ──────────────────────────────────────────────────

SUBCOMMANDS = ("submit", "status", "gates", "review", "events", "cancel",
               "swap-reset", "logs")


def test_autonomous_help_lists_every_subcommand(tmp_path):
    r = _run("coding-model-autonomous", "--help", home=tmp_path)
    assert r.returncode == 0, r.stderr
    assert r.stdout.startswith("usage: coding-model-autonomous")
    for sub in SUBCOMMANDS:
        assert sub in r.stdout


_SEP = "\n=== exit "


@pytest.fixture(scope="module")
def subcommand_helps(tmp_path_factory) -> dict:
    """Every subcommand's --help from ONE interpreter: {sub: (exit, stdout)}.

    Eight launches cost the suite about a second; the parser is rebuilt per
    call, so one process running them in turn sees what eight would."""
    home = tmp_path_factory.mktemp("home")
    module, func = SCRIPTS["coding-model-autonomous"].split(":")
    launcher = (
        "import contextlib, io, sys\n"
        f"from {module} import {func}\n"
        f"for sub in {SUBCOMMANDS!r}:\n"
        "    out = io.StringIO()\n"
        "    sys.argv = ['coding-model-autonomous', sub, '--help']\n"
        "    try:\n"
        "        with contextlib.redirect_stdout(out):\n"
        f"            code = {func}()\n"
        "    except SystemExit as e:\n"
        "        code = e.code\n"
        f"    print(out.getvalue() + {_SEP!r} + sub + ' ' + str(code or 0))\n")
    env = {**os.environ, "HOME": str(home),
           "PYTHONPATH": os.pathsep.join([str(ROOT / "src"), str(ROOT)]),
           "CODING_MODEL_SERVER_IP": "127.0.0.1",
           "CODING_MODEL_SERVER_PORT": CLOSED_PORT}
    r = subprocess.run([sys.executable, "-c", launcher], cwd=home, env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    helps, text = {}, r.stdout
    for sub in SUBCOMMANDS:
        body, _, rest = text.partition(_SEP)
        name, code = rest.split("\n", 1)[0].split()
        assert name == sub
        helps[sub] = (int(code), body)
        text = rest.split("\n", 1)[1] if "\n" in rest else ""
    return helps


@pytest.mark.parametrize("sub", SUBCOMMANDS)
def test_autonomous_subcommand_help_exits_zero(subcommand_helps, sub):
    code, out = subcommand_helps[sub]
    assert code == 0
    assert out.startswith(f"usage: coding-model-autonomous {sub}")


def test_autonomous_requires_a_subcommand(tmp_path):
    r = _run("coding-model-autonomous", home=tmp_path)
    assert r.returncode == 2
    assert "required: cmd" in r.stderr


def test_autonomous_review_needs_a_gate_id(tmp_path):
    r = _run("coding-model-autonomous", "review", home=tmp_path)
    assert r.returncode == 2
    assert "gate_id" in r.stderr


def test_autonomous_reports_an_unreachable_server_and_exits_one(tmp_path):
    """Parsing succeeded and the command ran: the one request it made was
    refused, and that is an error message and exit 1, not a traceback."""
    r = _run("coding-model-autonomous", "gates", home=tmp_path)
    assert r.returncode == 1
    assert r.stderr.startswith(
        f"error: server unreachable at http://127.0.0.1:{CLOSED_PORT}/v1/autonomous/gates")
    assert "Traceback" not in r.stderr
