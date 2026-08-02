"""Transport contract for the shared stdio JSON-RPC client (DEV-146).

Replaces the twin concurrency suites (test_mcp_service_concurrency.py and
test_cupertino_mcp_concurrency.py), which carried near-identical ~200-line
fake-subprocess harnesses because the transport itself was duplicated. Both
real subclasses are driven through one parametrized harness here, so a
transport fix is proved once against both — rather than found, fixed and
re-tested twice, which is how the two copies drifted apart in the first
place (the client lacked the writer thread entirely).

Original coverage preserved:
  * DEV-25  — call_tool held the lock across the whole request/response, so
    doc calls serialized; a reader thread now demuxes stdout by JSON-RPC id.
  * DEV-147 — the stdin write happened under the lock, so a wedged child
    that stops reading blocked every caller.
"""
import json
import os
import threading
import time

import pytest

from coding_model_client import services as client_services
from coding_model_client.services import CupertinoMCPClient
from coding_model_server import mcp_service
from coding_model_server.mcp_service import AppleDeepDocsService
from coding_model_server.stdio_jsonrpc import StdioRpcError


class _FakeStdin:
    """Captures requests the client writes and drives the fake server.

    ``wedge()`` models the DEV-147 failure directly: a child that stops
    READING stdin fills the ~64KB pipe, so write() blocks indefinitely.
    """

    def __init__(self, proc):
        self._proc = proc
        self._unwedged = threading.Event()
        self._unwedged.set()

    def wedge(self):
        self._unwedged.clear()

    def unwedge(self):
        self._unwedged.set()

    def write(self, data):
        self._unwedged.wait()  # blocks exactly like a full pipe
        self._proc._on_request_line(data)

    def flush(self):
        pass


class _FakeProc:
    """Stand-in MCP subprocess: a real stdout pipe (so readline behaves) and
    a fake stdin that reacts to requests. Per-tool delays are honored on
    background threads so slow and fast requests genuinely overlap."""

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
            elif method in ("tools/call", "resources/read"):
                rid = req["id"]
                params = req.get("params") or {}
                tool = params.get("name") or params.get("uri") or "?"
                delay = self._delays.get(tool, 0.0)

                def respond(rid=rid, tool=tool, delay=delay):
                    time.sleep(delay)
                    self._emit({
                        "jsonrpc": "2.0", "id": rid,
                        "result": {"content": [
                            {"type": "text", "text": f"result:{tool}:{rid}"}]},
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


# ── per-subclass adapters ────────────────────────────────────────────────
#
# Each real client is booted the way its own _spawn works, and its result is
# normalized to (text, error) so one set of assertions covers both.

class _ServerAdapter:
    name = "appledeepdocs"

    def make(self, monkeypatch):
        svc = AppleDeepDocsService()
        monkeypatch.setattr(svc, "_get_venv_python_path", lambda: "/fake/python")
        monkeypatch.setattr(os.path, "exists", lambda p: True)
        return svc

    def patch_spawn(self, monkeypatch, proc):
        monkeypatch.setattr(mcp_service.subprocess, "Popen", lambda *a, **k: proc)

    def call(self, svc, tool):
        out = svc.call_tool(tool, {})
        if out.startswith("Error:"):
            return None, out
        return out, None


class _ClientAdapter:
    name = "cupertino"

    def make(self, monkeypatch):
        return CupertinoMCPClient()

    def patch_spawn(self, monkeypatch, proc):
        monkeypatch.setattr(client_services.subprocess, "Popen",
                            lambda *a, **k: proc)
        monkeypatch.setattr(client_services.subprocess, "check_output",
                            lambda *a, **k: "/usr/local/bin/cupertino\n")

    def call(self, cli, tool):
        res = cli._send_request("tools/call", {"name": tool, "arguments": {}})
        if "error" in res:
            return None, res["error"]
        parts = [c.get("text", "") for c in res.get("content", [])]
        return "\n\n".join(parts), None


ADAPTERS = [_ServerAdapter(), _ClientAdapter()]


@pytest.fixture(params=ADAPTERS, ids=[a.name for a in ADAPTERS])
def rpc(request, monkeypatch):
    adapter = request.param
    client = adapter.make(monkeypatch)
    yield adapter, client, monkeypatch
    client.stop()


def _boot(adapter, client, monkeypatch, delays=None):
    proc = _FakeProc(delays or {})
    adapter.patch_spawn(monkeypatch, proc)
    assert client.start() is True
    return proc


# ── the shared transport contract ────────────────────────────────────────

def test_basic_call_returns_matching_result(rpc):
    adapter, client, mp = rpc
    _boot(adapter, client, mp)
    text, err = adapter.call(client, "alpha")
    assert err is None
    assert text.startswith("result:alpha:")


def test_slow_request_does_not_block_a_fast_one(rpc):
    """The DEV-25 property: the lock spans id allocation only, so a 1s call
    doesn't hold a 0s call hostage."""
    adapter, client, mp = rpc
    _boot(adapter, client, mp, delays={"slow": 1.0})
    out = {}

    def run(tool):
        out[tool] = adapter.call(client, tool)

    t_slow = threading.Thread(target=run, args=("slow",))
    t_slow.start()
    time.sleep(0.1)  # let the slow one take the lock and release it

    started = time.monotonic()
    run("fast")
    fast_elapsed = time.monotonic() - started
    t_slow.join(timeout=10)

    assert fast_elapsed < 0.7, (
        f"fast call waited {fast_elapsed:.2f}s behind the slow one — "
        "the lock is being held across the response wait")
    assert out["fast"][0].startswith("result:fast:")
    assert out["slow"][0].startswith("result:slow:")


def test_concurrent_calls_are_not_cross_wired(rpc):
    """Every caller must receive ITS OWN response. The pre-reader design had
    each caller read the shared pipe and discard non-matching ids, so two at
    once ate each other's replies."""
    adapter, client, mp = rpc
    _boot(adapter, client, mp)
    results = {}
    lock = threading.Lock()

    def run(i):
        text, err = adapter.call(client, f"tool{i}")
        with lock:
            results[i] = (text, err)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert len(results) == 8
    for i, (text, err) in results.items():
        assert err is None, f"call {i} errored: {err}"
        assert f"result:tool{i}:" in text, f"call {i} got another call's reply"


def test_process_death_wakes_waiters(rpc):
    """A dying child must wake every waiter with the sentinel rather than
    leaving them to their full timeout."""
    adapter, client, mp = rpc
    proc = _boot(adapter, client, mp, delays={"never": 30.0})
    out = {}

    def run():
        out["res"] = adapter.call(client, "never")

    t = threading.Thread(target=run)
    t.start()
    time.sleep(0.2)
    proc.die()
    t.join(timeout=5)

    assert not t.is_alive(), "waiter was not woken by the child's death"
    _, err = out["res"]
    assert err is not None and "exited unexpectedly" in err


def test_waiters_are_deregistered_after_each_call(rpc):
    adapter, client, mp = rpc
    _boot(adapter, client, mp)
    adapter.call(client, "alpha")
    assert client._pending == {}, "a completed call left its queue registered"


def test_wedged_child_fails_fast_instead_of_blocking_callers(rpc, monkeypatch):
    """DEV-147: a child that stops READING stdin fills the ~64KB pipe, so the
    write blocks forever. The writer thread absorbs that block; callers hit
    the bounded queue and fail fast instead of piling up on the lock. Before
    this extraction only the server copy had a writer thread — the client
    blocked its callers on the write itself.
    """
    adapter, client, mp = rpc
    # One queue slot and a short enqueue timeout keep the test quick; the
    # class attrs must be set before start() builds the queue.
    monkeypatch.setattr(type(client), "WRITE_QUEUE_MAXSIZE", 1)
    monkeypatch.setattr(type(client), "WRITE_ENQUEUE_TIMEOUT", 0.2)
    monkeypatch.setattr(type(client), "MAX_TOTAL_WALL_TIME", 2)
    proc = _boot(adapter, client, mp)

    proc.stdin.wedge()
    try:
        # Occupy the writer (blocked inside write) and then the single queue
        # slot. Both of these callers are stuck by design — daemon threads.
        for tool in ("stuck1", "stuck2"):
            threading.Thread(target=adapter.call, args=(client, tool),
                             daemon=True).start()
        time.sleep(0.3)

        started = time.monotonic()
        _text, err = adapter.call(client, "alpha")
        elapsed = time.monotonic() - started
    finally:
        proc.stdin.unwedge()

    assert err is not None, "a wedged child must surface as an error"
    assert elapsed < 2.0, f"caller blocked {elapsed:.1f}s behind a wedged child"


def test_start_failure_raises_a_typed_error(rpc, monkeypatch):
    adapter, client, mp = rpc
    monkeypatch.setattr(client, "_spawn", lambda: None)
    with pytest.raises(StdioRpcError) as exc:
        client.request("tools/call", {"name": "x", "arguments": {}})
    assert exc.value.kind == "start"


def test_failed_handshake_kills_child_and_allows_clean_restart(rpc, monkeypatch):
    """DEV-333: a failed handshake used to leave the live child in
    self.process with no write queue — start() then reported success forever,
    and every later request() raised AttributeError('NoneType' object has no
    attribute 'put') instead of the typed error, until a server restart."""
    adapter, client, mp = rpc
    bad = _FakeProc({})
    adapter.patch_spawn(mp, bad)
    monkeypatch.setattr(client, "_handshake", lambda proc: False)

    assert client.start() is False
    assert client.process is None, "failed start left the child registered"
    assert bad.poll() is not None, "failed start leaked a live child"

    # Repeated calls keep raising the typed start error, never AttributeError.
    with pytest.raises(StdioRpcError) as exc:
        client.request("tools/call", {"name": "x", "arguments": {}})
    assert exc.value.kind == "start"

    # Once the child cooperates, start() recovers without a server restart.
    good = _FakeProc({})
    adapter.patch_spawn(mp, good)
    monkeypatch.setattr(client, "_handshake", lambda proc: True)
    assert client.start() is True
    text, err = adapter.call(client, "alpha")
    assert err is None
    assert text.startswith("result:alpha:")


def test_handshake_exception_kills_child_too(rpc, monkeypatch):
    """The exception path through _start_unlocked must clean up the same way
    the return-False path does."""
    adapter, client, mp = rpc
    bad = _FakeProc({})
    adapter.patch_spawn(mp, bad)

    def boom(proc):
        raise RuntimeError("handshake blew up")

    monkeypatch.setattr(client, "_handshake", boom)
    assert client.start() is False
    assert client.process is None
    assert bad.poll() is not None, "exception during start leaked a live child"


def test_request_without_writer_raises_typed_error(rpc):
    """A retired write queue (stop() racing request(), which snapshots it
    without stop() holding the same lock) must surface as the typed 'start'
    error, not an AttributeError."""
    adapter, client, mp = rpc
    _boot(adapter, client, mp)
    client._write_q = None
    with pytest.raises(StdioRpcError) as exc:
        client.request("tools/call", {"name": "x", "arguments": {}})
    assert exc.value.kind == "start"
    assert client._pending == {}, "the failed call left its waiter registered"
