"""Concurrency tests for the Apple Deep Docs MCP client (DEV-25).

call_tool used to hold the lock across the entire request/response (up to 180s),
so doc calls serialized. A reader thread now demuxes stdout by JSON-RPC id, so
the lock only spans the id-allocation + stdin write and requests overlap.
"""
import json
import os
import threading
import time

import pytest

from coding_model_server import mcp_service
from coding_model_server.mcp_service import AppleDeepDocsService


class _FakeStdin:
    """Captures requests the service writes and drives the fake server."""

    def __init__(self, proc):
        self._proc = proc

    def write(self, data):
        self._proc._on_request_line(data)

    def flush(self):
        pass


class _FakeProc:
    """A stand-in llama—er, MCP subprocess: real stdout pipe (so select +
    readline behave), a fake stdin that reacts to requests. Per-tool response
    delay is honored on background threads so slow and fast requests overlap."""

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
            method = req.get("method")
            if method == "initialize":
                self._emit({"jsonrpc": "2.0", "id": req["id"], "result": {}})
            elif method == "tools/call":
                rid = req["id"]
                tool = req["params"]["name"]
                delay = self._delays.get(tool, 0.0)

                def respond(rid=rid, tool=tool, delay=delay):
                    time.sleep(delay)
                    self._emit({
                        "jsonrpc": "2.0", "id": rid,
                        "result": {"content": [{"type": "text", "text": f"result:{tool}:{rid}"}]},
                    })

                threading.Thread(target=respond, daemon=True).start()
            # notifications/initialized has no id -> no response

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
def service(monkeypatch):
    svc = AppleDeepDocsService()
    monkeypatch.setattr(svc, "_get_venv_python_path", lambda: "/fake/python")
    monkeypatch.setattr(os.path, "exists", lambda p: True)
    yield svc
    svc.stop()


def _boot(svc, monkeypatch, delays):
    proc = _FakeProc(delays)
    monkeypatch.setattr(mcp_service.subprocess, "Popen", lambda *a, **k: proc)
    assert svc.start() is True
    return proc


def test_basic_call_returns_matching_result(service, monkeypatch):
    _boot(service, monkeypatch, delays={})
    out = service.call_tool("search", {"q": "swiftui"})
    assert out.startswith("result:search:")


def test_slow_request_does_not_block_a_fast_one(service, monkeypatch):
    """The core fix: with the old lock-the-whole-call design, the fast call
    couldn't start until the slow one finished. Now they overlap."""
    _boot(service, monkeypatch, delays={"slow": 0.6, "fast": 0.0})
    results = {}

    def run(tool):
        results[tool] = (time.time(), service.call_tool(tool, {}))

    t_slow = threading.Thread(target=run, args=("slow",))
    t_slow.start()
    time.sleep(0.05)  # ensure slow is in-flight first
    start_fast = time.time()
    out_fast = service.call_tool("fast", {})
    fast_elapsed = time.time() - start_fast
    t_slow.join(timeout=5)

    assert out_fast == "result:fast:" + out_fast.split(":")[-1]
    assert fast_elapsed < 0.4, "fast call was blocked behind the slow one"
    assert results["slow"][1].startswith("result:slow:")


def test_concurrent_calls_are_not_cross_wired(service, monkeypatch):
    """Each caller must get ITS response, not another's — the demux invariant
    the single reader thread guarantees."""
    _boot(service, monkeypatch, delays={f"t{i}": 0.05 for i in range(8)})
    out = {}
    lock = threading.Lock()

    def run(i):
        r = service.call_tool(f"t{i}", {})
        with lock:
            out[i] = r

    threads = [threading.Thread(target=run, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert len(out) == 8
    for i, r in out.items():
        assert r.startswith(f"result:t{i}:"), f"caller {i} got {r!r}"


def test_process_death_wakes_waiters(service, monkeypatch):
    proc = _boot(service, monkeypatch, delays={"hang": 999})
    result = {}

    def run():
        result["out"] = service.call_tool("hang", {})

    t = threading.Thread(target=run)
    t.start()
    time.sleep(0.1)
    proc.die(1)                 # kill mid-request
    t.join(timeout=5)
    assert not t.is_alive(), "waiter did not wake on process death"
    assert "exited unexpectedly" in result["out"]
