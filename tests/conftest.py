"""Pytest bootstrap.

The project is normally installed editable (``pip install -e .``), so the
``coding_model_server`` / ``coding_model_client`` / ``coding_model_autonomous`` packages import without
help. As a fallback for a bare checkout, put ``src/`` on the path too so the
suite runs regardless of install state.
"""
import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

# The suite drives the app through starlette's TestClient — a loopback client —
# with no ADMIN_API_KEY configured. Since DEV-127 that combination is an
# explicit opt-in enforced at lifespan startup, so opt the whole suite in here.
# Tests that assert the enforcement itself remove this var via monkeypatch.
os.environ.setdefault("CODING_MODEL_ALLOW_UNAUTH", "1")
