"""Admin-key enforcement tests (DEV-127).

Three holes made a default-ish install remotely exploitable: verify_admin_key
no-opped on an empty key, the refuse-to-start guard lived only in server.py's
__main__ block (bare `uvicorn ...:app` never runs it), and the .env.example
placeholder satisfied the non-empty check. These tests pin the closures:

* enforce_admin_key_config raises on empty key without the explicit opt-in,
  on the placeholder key always, and on opt-in with a non-loopback bind;
* the app lifespan runs that check, so every launch path is covered;
* in opted-in key-less mode, verify_admin_key rejects non-loopback clients.
"""
import asyncio
from types import SimpleNamespace
from unittest import mock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from coding_model_server import runtime
from coding_model_server.config import Config


def _enforce():
    runtime.enforce_admin_key_config()


def _patch_key(key):
    return mock.patch.object(Config, "ADMIN_API_KEY", key)


def _patch_host(host):
    return mock.patch.object(Config, "HOST", host)


# ── enforce_admin_key_config ─────────────────────────────────────────────────

def test_empty_key_without_opt_in_refuses(monkeypatch):
    monkeypatch.delenv("CODING_MODEL_ALLOW_UNAUTH", raising=False)
    with _patch_key(""), _patch_host("127.0.0.1"):
        with pytest.raises(RuntimeError, match="ADMIN_API_KEY is not set"):
            _enforce()


def test_placeholder_key_refuses_even_with_opt_in(monkeypatch):
    monkeypatch.setenv("CODING_MODEL_ALLOW_UNAUTH", "1")
    with _patch_key("your-secret-key-here"), _patch_host("127.0.0.1"):
        with pytest.raises(RuntimeError, match="placeholder"):
            _enforce()


def test_opt_in_with_non_loopback_bind_refuses(monkeypatch):
    monkeypatch.setenv("CODING_MODEL_ALLOW_UNAUTH", "1")
    with _patch_key(""), _patch_host("0.0.0.0"):
        with pytest.raises(RuntimeError, match="loopback-only"):
            _enforce()


def test_opt_in_on_loopback_is_allowed(monkeypatch):
    monkeypatch.setenv("CODING_MODEL_ALLOW_UNAUTH", "1")
    with _patch_key(""), _patch_host("127.0.0.1"):
        _enforce()


def test_real_key_allowed_on_any_bind(monkeypatch):
    monkeypatch.delenv("CODING_MODEL_ALLOW_UNAUTH", raising=False)
    with _patch_key("a-real-generated-key"), _patch_host("0.0.0.0"):
        _enforce()


# ── lifespan coverage: the check runs on every launch path ───────────────────

def test_lifespan_aborts_startup_without_key_or_opt_in(monkeypatch):
    """A bare `uvicorn coding_model_server.server:app` skips __main__; the
    lifespan must still refuse. The check raises before any service init, so
    no heavy mocking is needed — startup dies first."""
    import coding_model_server.server as srv

    monkeypatch.delenv("CODING_MODEL_ALLOW_UNAUTH", raising=False)
    with _patch_key(""), _patch_host("127.0.0.1"):
        with pytest.raises(RuntimeError, match="ADMIN_API_KEY is not set"):
            with TestClient(srv.app):
                pass  # pragma: no cover — startup must fail


# ── verify_admin_key ─────────────────────────────────────────────────────────

def _request_from(host):
    return SimpleNamespace(client=SimpleNamespace(host=host) if host else None)


def _verify(request, x_admin_key=None, authorization=None):
    return asyncio.run(runtime.verify_admin_key(request, x_admin_key, authorization))


def test_unauth_mode_allows_loopback_client():
    with _patch_key(""):
        _verify(_request_from("127.0.0.1"))
        _verify(_request_from("::1"))


def test_unauth_mode_rejects_lan_client():
    with _patch_key(""):
        with pytest.raises(HTTPException) as exc:
            _verify(_request_from("192.168.1.50"))
        assert exc.value.status_code == 401


def test_unauth_mode_rejects_clientless_request():
    with _patch_key(""):
        with pytest.raises(HTTPException):
            _verify(_request_from(None))


def test_key_mode_accepts_admin_header_and_bearer():
    with _patch_key("sekrit"):
        _verify(_request_from("192.168.1.50"), x_admin_key="sekrit")
        _verify(_request_from("192.168.1.50"), authorization="Bearer sekrit")


def test_key_mode_rejects_wrong_or_missing_key():
    with _patch_key("sekrit"):
        for kwargs in (
            {},
            {"x_admin_key": "wrong"},
            {"authorization": "Bearer wrong"},
        ):
            with pytest.raises(HTTPException) as exc:
                _verify(_request_from("127.0.0.1"), **kwargs)
            assert exc.value.status_code == 401
