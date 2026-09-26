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


import socket  # noqa: E402

import pytest  # noqa: E402

# On zooshly 127.0.0.1:5050 is the reverse tunnel to the live Mac runner, and
# test_runner defaults MAC_RUNNER_URL to it. A test that drives the daemon
# without stubbing the runner therefore reads real repositories on the Mac —
# with no key it is refused (two tests did this on every merge-gate run until
# 2026-09-26), and with the key sourced it would be served. The fetch fails
# soft by design, so a raised connect error would vanish inside it; record the
# attempt instead and fail the test at teardown.
_RUNNER_PORT = 5050
_runner_connects: list = []
_real_connect = socket.socket.connect


def _guarded_connect(self, address):
    if isinstance(address, tuple) and len(address) >= 2 \
            and address[1] == _RUNNER_PORT:
        _runner_connects.append(address)
        raise ConnectionRefusedError(
            f"tests must not reach the live Mac runner at {address}")
    return _real_connect(self, address)


socket.socket.connect = _guarded_connect


@pytest.fixture(autouse=True)
def _no_live_runner():
    _runner_connects.clear()
    yield
    if _runner_connects:
        pytest.fail(
            f"this test opened a connection to the live Mac runner port "
            f"{_RUNNER_PORT} ({_runner_connects[0]}); stub "
            "test_runner.fetch_repo_files or patch MAC_RUNNER_URL", pytrace=False)
