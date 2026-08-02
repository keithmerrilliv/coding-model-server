"""Shared JSON-RPC-over-stdio client for MCP child processes.

Both MCP clients in this repo — the server's Apple Deep Docs service and the
client's Cupertino service — spawn a child, speak JSON-RPC over its stdin/
stdout, and need the same three things to not deadlock:

  * a single reader thread owning stdout, demuxing responses to per-request
    queues by id (without it, every caller reads the pipe and discards lines
    whose id doesn't match, so concurrent callers eat each other's replies);
  * a single writer thread owning stdin behind a bounded queue, so a wedged
    child that stops reading fills the ~64KB pipe and blocks only that thread
    — never a caller holding the lock;
  * a fail-all sweep that wakes every waiter when the child dies, instead of
    leaving them to their full timeout.

Both copies grew those independently and DRIFTED: the same concurrency bug
had to be found and fixed twice, and by 2026-07-25 the server copy had a
writer thread and a bounded handshake readline that the client copy lacked
entirely (DEV-147). This base is lifted from the server copy — the one that
embodies every fix — so there is one implementation to reason about.

Subclasses supply only what genuinely differs: how to spawn the child
(``_spawn``), an optional post-spawn handshake (``_handshake``), and their
own error phrasing around ``request()``.
"""
import json
import logging
import subprocess
from queue import Empty, Full, Queue
from threading import Lock, RLock, Thread
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class StdioRpcError(RuntimeError):
    """A request could not be completed.

    ``kind`` lets callers phrase their own user-facing message per failure
    mode without re-deriving it: one of ``start``, ``backlog``, ``timeout``,
    ``died``, ``write``.
    """

    def __init__(self, kind: str, message: str):
        self.kind = kind
        super().__init__(message)


class StdioJsonRpcClient:
    """Reader/writer-threaded JSON-RPC client over a child's stdio."""

    # Per-request wait. This is a wait, NOT a lock hold, so a slow request
    # doesn't block every other caller for its duration.
    MAX_TOTAL_WALL_TIME = 120
    # Bounded so a wedged child surfaces as a fast, clear failure rather than
    # unbounded memory growth behind a stalled writer.
    WRITE_QUEUE_MAXSIZE = 8
    WRITE_ENQUEUE_TIMEOUT = 5.0

    def __init__(self, name: str):
        self.name = name
        self.process: Optional[subprocess.Popen] = None
        self.msg_id = 1
        # RLock: request() holds it and may call start() inside.
        self.lock = RLock()
        # req_id -> Queue on which the reader delivers that request's response
        # (or a None sentinel if the child dies). Its own lock, so the reader
        # routes without contending for self.lock.
        self._pending: Dict[int, "Queue"] = {}
        self._pending_lock = Lock()
        self._reader: Optional[Thread] = None
        self._reader_stop = False
        self._write_q: Optional[Queue] = None
        self._writer: Optional[Thread] = None

    # ── subclass hooks ───────────────────────────────────────────────────

    def _spawn(self) -> Optional[subprocess.Popen]:
        """Start the child process. Return None (or raise) on failure."""
        raise NotImplementedError

    def _handshake(self, proc: subprocess.Popen) -> bool:
        """Optional synchronous handshake, before the reader takes stdout.

        Runs while this thread still owns the pipe — use
        ``_readline_with_timeout``. Default: no handshake.
        """
        return True

    def _on_started(self) -> None:
        """Hook after a successful start (subclass bookkeeping/logging)."""

    def _on_stopped(self) -> None:
        """Hook after the child is stopped."""

    # ── lifecycle ────────────────────────────────────────────────────────

    def start(self) -> bool:
        """Idempotent start. Lock-protected: without it two concurrent
        first-time callers each Popen a child and the second orphans the
        first."""
        with self.lock:
            if self.process and self.process.poll() is None:
                return True
            return self._start_unlocked()

    def _start_unlocked(self) -> bool:
        """Caller must hold self.lock."""
        proc = None
        try:
            proc = self._spawn()
            if proc is None:
                return False
            self.process = proc
            if not self._handshake(proc):
                self._kill_failed_start(proc)
                return False
            # Hand stdout to the reader only now: during a synchronous
            # handshake this thread reads the pipe, and two readers on one
            # pipe race.
            self._start_reader(proc)
            # Retire a writer bound to a previous (crashed) child before
            # starting a fresh one on a fresh queue.
            if self._write_q is not None:
                try:
                    self._write_q.put_nowait(None)
                except Full:
                    pass
            self._write_q = Queue(maxsize=self.WRITE_QUEUE_MAXSIZE)
            self._start_writer(proc, self._write_q)
            self._on_started()
            return True
        except Exception as e:
            logger.error("Error starting %s: %s", self.name, e)
            if proc is not None:
                self._kill_failed_start(proc)
            return False

    def _kill_failed_start(self, proc: subprocess.Popen) -> None:
        """A start that fails after Popen must not leave the live child in
        self.process: start() would see it alive and report success forever,
        while the reader/writer plumbing was never built — so request() dies
        on the missing write queue and the half-initialized child leaks until
        a server restart. Kill it and clear the slot so the next start()
        respawns from scratch."""
        try:
            proc.kill()
            proc.wait(timeout=2)
        except (subprocess.TimeoutExpired, OSError):
            pass
        self.process = None

    def _readline_with_timeout(self, timeout: float = 30) -> Optional[str]:
        """Read one line from the child's stdout, bounded by *timeout*.

        A helper thread does the blocking readline and join(timeout) bounds
        the wait. A select()-based version can time out falsely: select
        reports the fd, but a complete line may already sit in Python's
        stream buffer with nothing left on the fd. Handshake use only — the
        reader thread owns the pipe afterwards.
        """
        if not self.process or not self.process.stdout:
            return None
        holder: Dict[str, Optional[str]] = {}
        stream = self.process.stdout

        def _read():
            try:
                holder["line"] = stream.readline()
            except Exception:
                holder["line"] = None

        t = Thread(target=_read, daemon=True, name=f"{self.name}-handshake-read")
        t.start()
        t.join(timeout)
        if t.is_alive():
            logger.error("%s readline timed out after %.1f seconds",
                         self.name, timeout)
            return None
        return holder.get("line")

    def _start_reader(self, proc: subprocess.Popen) -> None:
        self._reader_stop = False
        self._reader = Thread(
            target=self._reader_loop, args=(proc,),
            name=f"{self.name}-reader", daemon=True,
        )
        self._reader.start()

    def _start_writer(self, proc: subprocess.Popen, write_q: Queue) -> None:
        self._writer = Thread(
            target=self._writer_loop, args=(proc, write_q),
            name=f"{self.name}-writer", daemon=True,
        )
        self._writer.start()

    def _writer_loop(self, proc: subprocess.Popen, write_q: Queue) -> None:
        """Own the child's stdin so a wedged child blocks only this thread.
        Exits on the None sentinel (a successor child was started)."""
        while True:
            item = write_q.get()
            if item is None:
                return
            payload, req_id = item
            try:
                proc.stdin.write(payload + "\n")
                proc.stdin.flush()
            except Exception as e:
                logger.error("%s writer: stdin write failed for id %s: %s",
                             self.name, req_id, e)
                with self._pending_lock:
                    waiter = self._pending.pop(req_id, None)
                if waiter is not None:
                    waiter.put({"_writer_error": str(e)})

    def _reader_loop(self, proc: subprocess.Popen) -> None:
        """Own the child's stdout: parse each JSON-RPC line and deliver it to
        the queue registered for its id."""
        stdout = proc.stdout
        try:
            while not self._reader_stop:
                try:
                    line = stdout.readline()
                except (ValueError, OSError):
                    break  # pipe closed under us
                if line == "":
                    break  # EOF — the child exited
                line = line.strip()
                if not line:
                    continue
                try:
                    resp = json.loads(line)
                except json.JSONDecodeError:
                    continue
                rid = resp.get("id")
                if rid is None:
                    continue  # server-initiated notification — nobody waiting
                with self._pending_lock:
                    q = self._pending.get(rid)
                if q is not None:
                    q.put(resp)
        finally:
            # Reader exiting (EOF, stop, or error): wake every waiter with a
            # sentinel so they fail fast instead of blocking to their timeout.
            if proc.poll() is not None:
                self._on_stopped()
            self._fail_all_pending()

    def _fail_all_pending(self) -> None:
        with self._pending_lock:
            waiters = list(self._pending.values())
        for q in waiters:
            q.put(None)  # None = connection lost

    def stop(self, term_timeout: float = 5) -> None:
        if not self.process:
            return
        self._reader_stop = True
        try:
            self.process.terminate()
            self.process.wait(timeout=term_timeout)
        except (subprocess.TimeoutExpired, OSError):
            try:
                self.process.kill()
                self.process.wait(timeout=2)
            except OSError:
                pass
        finally:
            self.process = None
            self._on_stopped()
        # readline() returns EOF once the child is gone, so the reader falls
        # out on its own; join briefly so a restart gets a clean slate.
        if self._reader is not None:
            self._reader.join(timeout=5)
            self._reader = None
        if self._write_q is not None:
            try:
                self._write_q.put_nowait(None)
            except Full:
                pass
            self._write_q = None
        self._fail_all_pending()

    # ── request/response ─────────────────────────────────────────────────

    def request(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """Send one JSON-RPC request and return the raw response dict.

        Raises StdioRpcError (with a ``kind``) on any failure so callers can
        phrase their own message. The critical section covers only id
        allocation and waiter registration — the write goes through the
        writer thread's bounded queue, and the wait happens off-lock so
        concurrent callers overlap.
        """
        if not self.start():
            raise StdioRpcError("start", f"{self.name} failed to start")

        response_q: "Queue" = Queue()
        with self.lock:
            req_id = self.msg_id
            self.msg_id += 1
            with self._pending_lock:
                self._pending[req_id] = response_q
            payload = json.dumps({
                "jsonrpc": "2.0",
                "id": req_id,
                "method": method,
                "params": params,
            })
            write_q = self._write_q

        if write_q is None:
            # stop() doesn't take self.lock, so it can retire the writer
            # between the start() check above and the snapshot — surface that
            # as the start failure it is, not an AttributeError on .put.
            with self._pending_lock:
                self._pending.pop(req_id, None)
            raise StdioRpcError("start", f"{self.name} is not running")

        try:
            write_q.put((payload, req_id), timeout=self.WRITE_ENQUEUE_TIMEOUT)
        except Full:
            with self._pending_lock:
                self._pending.pop(req_id, None)
            logger.error("%s writer backlog full — child appears wedged "
                         "(not reading stdin)", self.name)
            raise StdioRpcError(
                "backlog",
                f"the {self.name} child is not accepting requests "
                f"(writer backlog full)")

        try:
            try:
                response = response_q.get(timeout=self.MAX_TOTAL_WALL_TIME)
            except Empty:
                raise StdioRpcError(
                    "timeout",
                    f"response not received within {self.MAX_TOTAL_WALL_TIME}s")
            if response is None:
                # Reader delivered the process-died sentinel.
                raise StdioRpcError("died", "server process exited unexpectedly")
            if isinstance(response, dict) and "_writer_error" in response:
                raise StdioRpcError("write", str(response["_writer_error"]))
            return response
        finally:
            # Always deregister, so a late/never response can't leak the queue.
            with self._pending_lock:
                self._pending.pop(req_id, None)
