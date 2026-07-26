"""SSRF-via-redirect guard (DEV-24).

The initial-URL check was fine, but requests follows redirects internally, so a
public URL could 302 to a metadata/loopback address and be fetched anyway.
_get_revalidating_redirects re-runs the guard on every hop.
"""
from unittest import mock

import pytest

from coding_model_client import services


def _resp(status, location=None):
    r = mock.Mock()
    r.status_code = status
    r.headers = {"Location": location} if location else {}
    r.close = mock.Mock()
    return r


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch):
    """Resolve on host, not real DNS: anything with 'internal' or an
    IP-literal metadata/loopback host is unsafe. Patches the validator that
    also returns the pinned IPs (DEV-163), plus the boolean wrapper the
    entry points gate on."""
    real = services._validate_public_url

    def fake(url):
        from urllib.parse import urlparse
        host = urlparse(url).hostname or ""
        if urlparse(url).scheme not in ("http", "https"):
            return False, "unsupported scheme", []
        if host in ("169.254.169.254", "127.0.0.1", "localhost") or "internal" in host:
            return False, f"refusing non-public host {host}", []
        return True, "", ["93.184.216.34"]

    monkeypatch.setattr(services, "_validate_public_url", fake)
    monkeypatch.setattr(services, "_is_safe_public_url",
                        lambda url: fake(url)[:2])
    return real


def test_direct_public_fetch_succeeds(monkeypatch):
    get = mock.Mock(return_value=_resp(200))
    monkeypatch.setattr(services, "_SESSION", mock.Mock(get=get))
    r = services._get_revalidating_redirects("https://example.com/doc", timeout=20)
    assert r.status_code == 200
    _, kwargs = get.call_args
    assert kwargs["allow_redirects"] is False  # we drive redirects ourselves


def test_redirect_to_metadata_is_blocked(monkeypatch):
    # public URL 302s to the cloud metadata endpoint
    get = mock.Mock(side_effect=[
        _resp(302, location="http://169.254.169.254/latest/meta-data/"),
    ])
    monkeypatch.setattr(services, "_SESSION", mock.Mock(get=get))
    with pytest.raises(ValueError, match="non-public"):
        services._get_revalidating_redirects("https://example.com/x", timeout=20)


def test_redirect_to_loopback_is_blocked(monkeypatch):
    get = mock.Mock(side_effect=[
        _resp(301, location="http://127.0.0.1:8000/admin"),
    ])
    monkeypatch.setattr(services, "_SESSION", mock.Mock(get=get))
    with pytest.raises(ValueError, match="non-public"):
        services._get_revalidating_redirects("https://example.com/x", timeout=20)


def test_legit_public_redirect_is_followed(monkeypatch):
    get = mock.Mock(side_effect=[
        _resp(302, location="https://cdn.example.com/final"),
        _resp(200),
    ])
    monkeypatch.setattr(services, "_SESSION", mock.Mock(get=get))
    r = services._get_revalidating_redirects("https://example.com/x", timeout=20)
    assert r.status_code == 200
    assert get.call_count == 2


def test_redirect_bomb_is_bounded(monkeypatch):
    get = mock.Mock(return_value=_resp(302, location="https://example.com/next"))
    monkeypatch.setattr(services, "_SESSION", mock.Mock(get=get))
    with pytest.raises(ValueError, match="too many redirects"):
        services._get_revalidating_redirects("https://example.com/x", timeout=20)


def test_ingest_reports_redirect_block(monkeypatch):
    get = mock.Mock(side_effect=[_resp(302, location="http://internal.svc/secret")])
    monkeypatch.setattr(services, "_SESSION", mock.Mock(get=get, post=mock.Mock()))
    out = services.ingest_url_content("https://example.com/x")
    assert "Error" in out and "non-public" in out
