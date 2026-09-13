"""DEV-670: every environment knob the code reads is documented.

The source tree is the inventory: any `AUTONOMOUS_*`, `LLAMA_*` or
`CODING_MODEL_*` name that appears in `src/` must appear in
docs/CONFIGURATION.md and in .env.example. Add the row before adding the
read, and the docs cannot drift again.
"""
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PATTERN = re.compile(r"\b(?:AUTONOMOUS|LLAMA|CODING_MODEL)_[A-Z0-9_]+\b")

# Names the regex matches that are not environment variables.
NOT_KNOBS = {
    "LLAMA_SERVER_PORT",                 # a class constant (8081), not env
    "CODING_MODEL_REVIEWER_AGENT",       # Python constants in review.py; the env
    "CODING_MODEL_DEEP_REVIEWER_AGENT",  #   names are REVIEW_CODING_MODEL_*
}
# Documented as a prefix: the code reads AUTONOMOUS_DELIVERY_SSH_KEY_<REPO>.
PREFIXES = {"AUTONOMOUS_DELIVERY_SSH_KEY_"}


def _inventory() -> set:
    names = set()
    for path in (REPO / "src").rglob("*.py"):
        names |= set(PATTERN.findall(path.read_text(errors="replace")))
    return {n for n in names if n not in NOT_KNOBS}


def _present(name: str, text: str) -> bool:
    if name in text:
        return True
    return any(name.startswith(p) and p in text for p in PREFIXES)


def test_every_knob_is_in_configuration_md():
    text = (REPO / "docs" / "CONFIGURATION.md").read_text()
    missing = sorted(n for n in _inventory() if not _present(n, text))
    assert not missing, f"undocumented in docs/CONFIGURATION.md: {missing}"


def test_every_knob_is_in_env_example():
    text = (REPO / ".env.example").read_text()
    missing = sorted(n for n in _inventory() if not _present(n, text))
    assert not missing, f"missing from .env.example: {missing}"


def test_the_inventory_is_not_empty():
    assert len(_inventory()) > 80
