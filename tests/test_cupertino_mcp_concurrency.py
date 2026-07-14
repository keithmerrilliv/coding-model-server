"""Concurrency tests for the Cupertino MCP client (DEV-78).

_send_request used to hold the lock across the entire request/response — a loop
bounded at 120s total — so every Cupertino call serialized behind the one in
flight. Narrowing the lock alone would have been unsafe: each caller read the
shared stdout pipe and discarded lines whose id didn't match, so two concurrent
readers would swallow each other's responses.

A single reader thread now owns stdout and demuxes by JSON-RPC id, so the lock
spans only the id allocation + stdin write and requests overlap.

Sibling of tests/test_mcp_service_concurrency.py (DEV-25), which pins the same
invariants on the server-side Apple Deep Docs client.
"""
import json
import os
import threading
import time

import pytest

from coding_model_client import services
from coding_model_client.services import CupertinoMCPClient


class _FakeStdin:
    """Captures requests the client writes and drives the fake server."""

    def __init__(self, proc):
        self._proc = proc

    def write(self, data):
        self._proc._on_request_line(data)

    def flush(self):
        pass


class _FakeProc:
    """Stand-in MCP subprocess: a real stdout pipe (so readline behaves like the
    real thing) and a fake stdin that reacts to requests. Per-tool response delay
    is honoured on background threads, so slow and fast requests can overlap."""

    def __init__(self, delays):
        self._delays = delays  # tool_name -> seconds
        r, w = os.pipe()
        self.stdout = os.fdopen(r, "r")
        self._w = os.fdopen(w, "w")
        self._w_lock = threading.Lock()
        self.stdin = _FakeStdin(self)
        self._alive = True
        self._returncode = None
        self.request_log = []

    def _emit(self, obj):
        with self._w_lock:
            if not self._w.closed:
                self._w.write(json.dumps(obj) + "\n")
                self._w.flush()

    def _on_request_line(self, data):
        for line in data.splitlines():
            line = line.strip()
            if not line:
                continue
            req = json.loads(line)
            self.request_log.append(req)
            rid = req["id"]
            # Cupertino's _send_request is generic: search() sends tools/call,
            # read_resource() sends resources/read. Key the delay off whichever
            # name the params carry.
            params = req.get("params", {})
            tool = params.get("name") or params.get("uri") or req.get("method")
            delay = self._delays.get(tool, 0.0)

            def respond(rid=rid, tool=tool, delay=delay):
                time.sleep(delay)
                self._emit({
                    "jsonrpc": "2.0", "id": rid,
                    "result": {"content": [{"type": "text", "text": f"result:{tool}:{rid}"}]},
                })

            threading.Thread(target=respond, daemon=True).start()

    def poll(self):
        return None if self._alive else self._returncode

    def terminate(self):
        self.die(0)

    def kill(self):
        self.die(-9)

    def wait(self, timeout=None):
        return self._returncode

    def die(self, code=0):
        self._alive = False
        self._returncode = code
        with self._w_lock:
            if not self._w.closed:
                self._w.close()  # EOF wakes the reader


@pytest.fixture
def client():
    c = CupertinoMCPClient()
    yield c
    c.stop()


def _boot(c, monkeypatch, delays):
    proc = _FakeProc(delays)
    monkeypatch.setattr(
        services.subprocess, "check_output", lambda *a, **k: "/usr/bin/cupertino\n"
    )
    monkeypatch.setattr(services.subprocess, "Popen", lambda *a, **k: proc)
    assert c.start() is True
    return proc


def _text(result):
    """Pull the text payload out of a tools/call result dict."""
    return result["content"][0]["text"]


def test_basic_call_returns_matching_result(client, monkeypatch):
    _boot(client, monkeypatch, delays={})
    out = client.search("swiftui")
    assert _text(out).startswith("result:search_docs:")


def test_slow_request_does_not_block_a_fast_one(client, monkeypatch):
    """The core fix: with the old lock-the-whole-call design, the fast call
    couldn't even start until the slow one finished. Now they overlap."""
    _boot(client, monkeypatch, delays={"slow_docs": 0.6, "search_docs": 0.0})
    slow_result = {}

    def run_slow():
        slow_result["out"] = client._send_request(
            "tools/call", {"name": "slow_docs", "arguments": {}}
        )

    t_slow = threading.Thread(target=run_slow)
    t_slow.start()
    time.sleep(0.05)  # ensure the slow call is in flight first

    start_fast = time.time()
    out_fast = client.search("swiftui")
    fast_elapsed = time.time() - start_fast
    t_slow.join(timeout=5)

    assert _text(out_fast).startswith("result:search_docs:")
    assert fast_elapsed < 0.4, "fast call was blocked behind the slow one"
    assert _text(slow_result["out"]).startswith("result:slow_docs:")


def test_concurrent_calls_are_not_cross_wired(client, monkeypatch):
    """Each caller must get ITS response, not another's — the demux invariant
    the single reader thread guarantees. Pre-fix, concurrent readers of the
    shared pipe would swallow each other's replies."""
    _boot(client, monkeypatch, delays={f"t{i}": 0.05 for i in range(8)})
    out = {}
    lock = threading.Lock()

    def run(i):
        r = client._send_request("tools/call", {"name": f"t{i}", "arguments": {}})
        with lock:
            out[i] = r

    threads = [threading.Thread(target=run, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert len(out) == 8
    for i, r in out.items():
        assert _text(r).startswith(f"result:t{i}:"), f"caller {i} got {r!r}"


def test_process_death_wakes_waiters(client, monkeypatch):
    """A None sentinel from the reader must wake waiters immediately, instead of
    letting them block to the 120s timeout."""
    proc = _boot(client, monkeypatch, delays={"hang": 999})
    result = {}

    def run():
        result["out"] = client._send_request(
            "tools/call", {"name": "hang", "arguments": {}}
        )

    t = threading.Thread(target=run)
    t.start()
    time.sleep(0.1)
    proc.die(1)  # kill mid-request
    t.join(timeout=5)

    assert not t.is_alive(), "waiter did not wake on process death"
    assert "exited unexpectedly" in result["out"]["error"]


def test_read_resource_shares_the_demux_path(client, monkeypatch):
    """_send_request is generic — resources/read must demux like tools/call."""
    _boot(client, monkeypatch, delays={})
    out = client.read_resource("doc://swiftui/View")
    assert _text(out).startswith("result:doc://swiftui/View:")


def test_waiters_are_deregistered_after_each_call(client, monkeypatch):
    """The pending map must not leak an entry per request."""
    _boot(client, monkeypatch, delays={})
    for _ in range(5):
        client.search("swiftui")
    assert client._pending == {}
