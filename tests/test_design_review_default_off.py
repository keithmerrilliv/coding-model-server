"""DEV-440: the design-review stage is OFF by default.

It returned 37 FAIL and 1 PASS across its entire 38-run life (2026-07-12 to
2026-09-16). A verdict that is FAIL 97.4% of the time is a constant, not a
signal, and each FAIL cost an architect revision. These tests pin the default
so it cannot drift back on without someone deciding to, and pin the escape
hatch so re-enabling it for a replay set stays a one-line env change.

The env cases run in a SUBPROCESS on purpose. `executor` reads the flag at
import, so the obvious `importlib.reload` rebinds the module object underneath
every other test that already holds references into it — that poisoned 31
unrelated tests when this file first used it. A fresh interpreter is the only
honest way to test import-time env parsing.
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

_PROBE = (
    "import sys; sys.path.insert(0, %r);"
    "from coding_model_autonomous import executor;"
    "print(executor.DESIGN_REVIEW_ENABLED)"
) % str(SRC)


def _enabled_with(env_value):
    """Import executor in a clean interpreter and report the flag."""
    import os
    env = {k: v for k, v in os.environ.items() if k != "AUTONOMOUS_DESIGN_REVIEW"}
    if env_value is not None:
        env["AUTONOMOUS_DESIGN_REVIEW"] = env_value
    out = subprocess.run([sys.executable, "-c", _PROBE], capture_output=True,
                         text=True, env=env, cwd=str(ROOT), timeout=120)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip() == "True"


def test_design_review_is_off_by_default():
    assert _enabled_with(None) is False


def test_the_probe_can_observe_True():
    """Negative control: if the probe could never report True, the default-off
    test above would pass no matter what the code said."""
    assert _enabled_with("1") is True


def test_the_documented_off_spellings_all_disable():
    for v in ("0", "false", "no", "FALSE", "No"):
        assert _enabled_with(v) is False, v


def test_the_agent_and_budgets_were_not_changed():
    """Only the default moved. A later re-enable should get the same stage
    back, not a differently-configured one."""
    from coding_model_autonomous import executor
    assert executor.DESIGN_REVIEW_AGENT == "reviewer"
    assert executor.DESIGN_REVIEW_MAX_REVISIONS == 1
    assert executor.DESIGN_REVIEW_MAX_TOKENS == 8000
