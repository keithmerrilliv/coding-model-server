"""Pytest bootstrap.

The project is normally installed editable (``pip install -e .``), so the
``coding_model_server`` / ``coding_model_client`` / ``coding_model_autonomous`` packages import without
help. As a fallback for a bare checkout, put ``src/`` on the path too so the
suite runs regardless of install state.
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

# mac_runner lives at the repo root, outside src/, and is deliberately not in
# the installed package set (it deploys by git checkout on the Mac, not by
# pip). CI installs the package and runs pytest from the repo root, where
# nothing puts the root on sys.path — so without this, every mac_runner test
# module dies at collection with ModuleNotFoundError.
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# The suite drives the app through starlette's TestClient — a loopback client —
# with no ADMIN_API_KEY configured. Since DEV-127 that combination is an
# explicit opt-in enforced at lifespan startup, so opt the whole suite in here.
# Tests that assert the enforcement itself remove this var via monkeypatch.
os.environ.setdefault("CODING_MODEL_ALLOW_UNAUTH", "1")
