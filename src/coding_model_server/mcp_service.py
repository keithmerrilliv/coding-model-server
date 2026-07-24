#!/usr/bin/env python3
"""
Apple Deep Docs MCP client.

Manages the Apple Deep Docs MCP server subprocess (JSON-RPC over stdio) and
exposes a unified interface used by both the FastAPI server and a small CLI.
Named mcp_service to reflect that it's an MCP *client*, not a manager of this
project's own server.
"""

import subprocess
import time
import sys
import os
import json
import select
import logging
from queue import Queue, Empty
from threading import RLock, Lock, Thread
from typing import Dict, Optional, Any

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

class AppleDeepDocsService:
    """Service for interacting with the Apple Deep Docs MCP server"""

    def __init__(self, mcp_path: str = None):
        if mcp_path is None:
            # Default to tools/appledeepdoc-mcp at the repo root; this file
            # lives at src/coding_model_server/.
            repo_root = os.path.dirname(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            )
            self.mcp_path = os.path.join(repo_root, "tools", "appledeepdoc-mcp")
        else:
            self.mcp_path = mcp_path
            
        self.process = None
        self.msg_id = 1
        # RLock so call_tool can hold the lock and call start() inside it
        # without deadlocking. It now guards only the fast operations —
        # process lifecycle, the msg_id counter, and the stdin write — NOT the
        # response wait. A dedicated reader thread (below) owns stdout and hands
        # each response to the waiting caller's queue, so concurrent call_tool
        # requests no longer serialize behind one another for up to 180s.
        self.lock = RLock()
        self.venv_python = self._get_venv_python_path()
        self.is_running = False
        # req_id -> Queue on which the reader thread delivers that request's
        # response (or a None sentinel if the process dies). Guarded by its own
        # lock so the reader can route without contending for self.lock.
        self._pending: Dict[int, "Queue"] = {}
        self._pending_lock = Lock()
        self._reader: Optional[Thread] = None
        self._reader_stop = False

    def _readline_with_timeout(self, timeout: float = 30) -> Optional[str]:
        """Read a line from the MCP subprocess stdout with a timeout using select."""
        if not self.process or not self.process.stdout:
            return None

        # Poll stdout for data
        ready, _, _ = select.select([self.process.stdout], [], [], timeout)
        if ready:
            return self.process.stdout.readline()
        
        logger.error("MCP readline timed out after %.1f seconds", timeout)
        return None

    def _get_venv_python_path(self):
        """Get the correct Python executable path for the virtual environment based on OS"""
        if os.name == "nt":  # Windows
            return os.path.join(self.mcp_path, "venv", "Scripts", "python.exe")
        else:  # Unix-like (Linux, macOS)
            return os.path.join(self.mcp_path, "venv", "bin", "python")

    def start(self):
        """Start the MCP server process and perform handshake if not already running.

        Lock-protected start: previously the alive check was outside the lock,
        so two concurrent callers could both observe "not running" and each
        Popen a fresh subprocess. The second one would overwrite self.process
        and leak the first.
        """
        with self.lock:
            if self.process and self.process.poll() is None:
                self.is_running = True
                return True
            return self._start_unlocked()

    def _start_unlocked(self):
        """Internal: caller must hold self.lock."""
        try:
            main_py = os.path.join(self.mcp_path, "main.py")
            if not os.path.exists(self.venv_python):
                logger.error(f"Apple Deep Docs venv not found at {self.venv_python}")
                return False

            # Use DEVNULL for stderr to avoid deadlocks from full buffers
            self.process = subprocess.Popen(
                [self.venv_python, main_py],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
                cwd=self.mcp_path
            )

            # Perform MCP Handshake
            logger.info("Performing MCP handshake with Apple Deep Docs...")

            # 1. Send initialize
            init_request = {
                "jsonrpc": "2.0",
                "id": 0,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "coding-model-server", "version": "2.0"}
                }
            }
            self.process.stdin.write(json.dumps(init_request) + "\n")
            self.process.stdin.flush()

            # 2. Wait for initialize response
            while True:
                line = self._readline_with_timeout(timeout=30)
                if not line:
                    logger.error("Failed to receive initialize response from MCP")
                    return False
                line = line.strip()
                if not line: continue
                try:
                    resp = json.loads(line)
                    if resp.get("id") == 0:
                        logger.info("MCP initialize successful")
                        break
                except Exception:
                    continue
            
            # 3. Send initialized notification
            initialized_notif = {
                "jsonrpc": "2.0",
                "method": "notifications/initialized"
            }
            self.process.stdin.write(json.dumps(initialized_notif) + "\n")
            self.process.stdin.flush()
            
            logger.info("Apple Deep Docs MCP server ready")
            self.is_running = True
            # Hand stdout to the reader thread only now that the synchronous
            # handshake (which read stdout directly) is done — otherwise the two
            # would race for the same pipe.
            self._start_reader(self.process)
            return True
        except Exception as e:
            logger.error(f"Error starting Apple Deep Docs MCP: {e}")
            self.is_running = False
            return False

    def _start_reader(self, proc):
        """Spawn the stdout demux thread for `proc` (caller holds self.lock)."""
        self._reader_stop = False
        self._reader = Thread(
            target=self._reader_loop, args=(proc,),
            name="appledeepdocs-mcp-reader", daemon=True,
        )
        self._reader.start()

    def _reader_loop(self, proc):
        """Own `proc`'s stdout: parse each JSON-RPC line and deliver it to the
        queue registered for its id.

        This is what lets call_tool wait off-lock. A single reader means only
        one consumer of the shared stdout pipe, so responses can't be swallowed
        by the wrong caller (the old design had every caller read the pipe and
        discard lines whose id didn't match — two at once ate each other's
        replies).
        """
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
            # Reader is exiting (EOF, stop, or error): wake every waiter with a
            # sentinel so they fail fast instead of blocking to their timeout.
            if proc.poll() is not None:
                self.is_running = False
            self._fail_all_pending()

    def _fail_all_pending(self):
        with self._pending_lock:
            waiters = list(self._pending.values())
        for q in waiters:
            q.put(None)  # None = connection lost; call_tool maps it to an error

    def stop(self):
        """Stop the MCP server process"""
        if self.process:
            self._reader_stop = True
            try:
                self.process.terminate()
                self.process.wait(timeout=5)
            except (subprocess.TimeoutExpired, OSError):
                try:
                    self.process.kill()
                    self.process.wait(timeout=2)
                except OSError:
                    pass
            finally:
                self.process = None
                self.is_running = False
            # readline() returns EOF once the child is gone, so the reader loop
            # falls out on its own; join briefly so a restart gets a clean slate.
            if self._reader is not None:
                self._reader.join(timeout=5)
                self._reader = None
            self._fail_all_pending()
            logger.info("Apple Deep Docs MCP server stopped")

    # 3 min — generous for slow doc fetches. This is now a per-request wait, not
    # a lock hold, so a slow request no longer blocks every other call_tool.
    MAX_TOTAL_WALL_TIME = 180

    def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> str:
        """Call a specific tool on the MCP server and return the result as text"""
        if not self.start():
            return "Error: Apple Deep Docs MCP server failed to start."

        response_q: "Queue" = Queue()

        # Short critical section: allocate the id, register the waiter, and write
        # the request. The response wait happens AFTER releasing the lock, so
        # concurrent callers overlap instead of queueing for up to 180s each.
        with self.lock:
            req_id = self.msg_id
            self.msg_id += 1
            with self._pending_lock:
                self._pending[req_id] = response_q
            request = {
                "jsonrpc": "2.0",
                "id": req_id,
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": arguments},
            }
            try:
                payload = json.dumps(request)
                logger.info(f"Sending MCP Request: {payload[:200]}...")
                self.process.stdin.write(payload + "\n")
                self.process.stdin.flush()
            except Exception as e:
                with self._pending_lock:
                    self._pending.pop(req_id, None)
                logger.error(f"Communication error with Deep Docs MCP: {e}")
                return f"Error: Documentation fetch failed: {str(e)}"

        try:
            try:
                response = response_q.get(timeout=self.MAX_TOTAL_WALL_TIME)
            except Empty:
                return (f"Error: MCP server response not received within "
                        f"{self.MAX_TOTAL_WALL_TIME}s.")

            if response is None:
                # Reader delivered the process-died sentinel.
                self.is_running = False
                return "Error: MCP server process exited unexpectedly."

            result = response.get("result", {})
            content = result.get("content", [])
            text_parts = [
                item.get("text", "") for item in content
                if item.get("type") == "text"
            ]
            if text_parts:
                return "\n\n".join(text_parts)
            return json.dumps(result, indent=2)
        finally:
            with self._pending_lock:
                self._pending.pop(req_id, None)

# Global instance for CLI / shared use
_instance = None

def get_service():
    global _instance
    if _instance is None:
        _instance = AppleDeepDocsService()
    return _instance

def start_server():
    return get_service().start()

def stop_server():
    if _instance:
        _instance.stop()

def get_server_status():
    if _instance:
        return _instance.is_running
    return False

if __name__ == "__main__":
    service = get_service()
    if len(sys.argv) > 1:
        if sys.argv[1] == "start":
            if service.start():
                print("Apple Deep Docs MCP server started. Press Ctrl+C to stop.")
                try:
                    while True:
                        time.sleep(1)
                        if service.process.poll() is not None:
                            print("Server exited unexpectedly.")
                            break
                except KeyboardInterrupt:
                    service.stop()
        elif sys.argv[1] == "stop":
            service.stop()
        elif sys.argv[1] == "status":
            print(f"Server running: {service.is_running}")
        elif sys.argv[1] == "call":
            if len(sys.argv) < 3:
                print("Usage: python -m coding_model_server.mcp_service call <tool_name> [args_json]")
            else:
                tool = sys.argv[2]
                args = json.loads(sys.argv[3]) if len(sys.argv) > 3 else {}
                print(service.call_tool(tool, args))
        else:
            print("Usage: python -m coding_model_server.mcp_service [start|stop|status|call]")
    else:
        print("Usage: python -m coding_model_server.mcp_service [start|stop|status|call]")
