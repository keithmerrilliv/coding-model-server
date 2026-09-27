"""Read the frozen artefacts in tests/fixtures (catalogued in its README.md).

A plain module rather than conftest.py content because test modules import it
by name, and ``from conftest import`` resolves to whichever conftest pytest
loaded last (tests/seams/conftest.py).
"""
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> str:
    """Return the UTF-8 text of ``tests/fixtures/<name>``."""
    return (FIXTURES / name).read_text(encoding="utf-8")
