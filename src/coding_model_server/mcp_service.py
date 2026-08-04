#!/usr/bin/env python3
"""
Apple Deep Docs MCP client.

Manages the Apple Deep Docs MCP server subprocess (JSON-RPC over stdio) and
exposes a unified interface used by both the FastAPI server and a small CLI.
Named mcp_service to reflect that it's an MCP *client*, not a manager of this
project's own server.

The transport (reader/writer threads, response demux, fail-all-on-death) lives
in stdio_jsonrpc.StdioJsonRpcClient — shared with the client package's
Cupertino client (DEV-146). Only the spawn, the handshake, and this service's
error phrasing are here.
"""

import subprocess
import time
import sys
import os
import json
import logging

from coding_model_server.stdio_jsonrpc import StdioJsonRpcClient, StdioRpcError

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

class AppleDeepDocsService(StdioJsonRpcClient):
    """Service for interacting with the Apple Deep Docs MCP server."""

    # 3 min — generous for slow doc fetches.
    MAX_TOTAL_WALL_TIME = 180

    def __init__(self, mcp_path: str = None):
        super().__init__("appledeepdocs-mcp")
        if mcp_path is None:
            # Default to tools/appledeepdoc-mcp at the repo root; this file
            # lives at src/coding_model_server/.
            repo_root = os.path.dirname(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            )
            self.mcp_path = os.path.join(repo_root, "tools", "appledeepdoc-mcp")
        else:
            self.mcp_path = mcp_path
        self.venv_python = self._get_venv_python_path()
        # Public liveness flag the CLI and /health surface. Kept in sync with
        # the base's lifecycle via the _on_started/_on_stopped hooks.
        self.is_running = False

    def _get_venv_python_path(self):
        """Get the correct Python executable path for the virtual environment based on OS"""
        if os.name == "nt":  # Windows
            return os.path.join(self.mcp_path, "venv", "Scripts", "python.exe")
        else:  # Unix-like (Linux, macOS)
            return os.path.join(self.mcp_path, "venv", "bin", "python")

    # ── transport hooks ──────────────────────────────────────────────────

    def _spawn(self):
        main_py = os.path.join(self.mcp_path, "main.py")
        if not os.path.exists(self.venv_python):
            logger.error(f"Apple Deep Docs venv not found at {self.venv_python}")
            return None
        # DEVNULL for stderr to avoid deadlocks from full buffers
        return subprocess.Popen(
            [self.venv_python, main_py],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            cwd=self.mcp_path,
        )

    def _handshake(self, proc) -> bool:
        """MCP initialize / initialized exchange, before the reader takes stdout."""
        logger.info("Performing MCP handshake with Apple Deep Docs...")
        init_request = {
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "coding-model-server", "version": "2.0"},
            },
        }
        proc.stdin.write(json.dumps(init_request) + "\n")
        proc.stdin.flush()

        while True:
            line = self._readline_with_timeout(timeout=30)
            if not line:
                logger.error("Failed to receive initialize response from MCP")
                return False
            line = line.strip()
            if not line:
                continue
            try:
                resp = json.loads(line)
            except Exception:
                continue
            if resp.get("id") == 0:
                logger.info("MCP initialize successful")
                break

        proc.stdin.write(
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        proc.stdin.flush()
        return True

    def _on_started(self):
        self.is_running = True
        logger.info("Apple Deep Docs MCP server ready")

    def _on_stopped(self):
        self.is_running = False

    def stop(self):
        had_process = self.process is not None
        super().stop()
        if had_process:
            logger.info("Apple Deep Docs MCP server stopped")

    # ── tool surface ─────────────────────────────────────────────────────

    def call_tool(self, tool_name: str, arguments) -> str:
        """Call a specific tool on the MCP server and return the result as text.

        Error strings are this service's own — the transport raises
        StdioRpcError with a `kind` and each caller phrases its own message.
        """
        try:
            response = self.request(
                "tools/call", {"name": tool_name, "arguments": arguments})
        except StdioRpcError as e:
            if e.kind == "start":
                return "Error: Apple Deep Docs MCP server failed to start."
            if e.kind == "timeout":
                return (f"Error: MCP server response not received within "
                        f"{self.MAX_TOTAL_WALL_TIME}s.")
            if e.kind == "died":
                return "Error: MCP server process exited unexpectedly."
            if e.kind == "backlog":
                return ("Error: Documentation fetch failed: the Deep Docs MCP "
                        "child is not accepting requests (writer backlog full).")
            return f"Error: Documentation fetch failed: {e}"

        result = response.get("result", {})
        content = result.get("content", [])
        text_parts = [
            item.get("text", "") for item in content
            if item.get("type") == "text"
        ]
        if text_parts:
            return "\n\n".join(text_parts)
        return json.dumps(result, indent=2)

    def list_tools(self) -> str:
        """Return the MCP server's tool catalogue as readable text.

        Without this the service was effectively unusable despite working: the
        endpoint requires a `tool` name, and nothing anywhere listed the valid
        names. Every guess came back "Unknown tool", which is exactly why this
        service was mistaken for dead during the DEV-471 audit (DEV-480).

        `tools/list` is standard MCP, and the transport already speaks the
        sibling `tools/call`.
        """
        try:
            response = self.request("tools/list", {})
        except StdioRpcError as e:
            if e.kind == "start":
                return "Error: Apple Deep Docs MCP server failed to start."
            if e.kind == "timeout":
                return (f"Error: MCP server response not received within "
                        f"{self.MAX_TOTAL_WALL_TIME}s.")
            if e.kind == "died":
                return "Error: MCP server process exited unexpectedly."
            return f"Error: Could not list tools: {e}"

        tools = (response.get("result") or {}).get("tools") or []
        if not tools:
            return "No tools reported by the Apple Deep Docs MCP server."
        lines = ["Apple Deep Docs MCP — %d tools:" % len(tools)]
        for t in tools:
            name = t.get("name", "?")
            desc = (t.get("description") or "").strip().splitlines()
            first = desc[0] if desc else ""
            lines.append("  %-34s %s" % (name, first[:110]))
            props = ((t.get("inputSchema") or {}).get("properties") or {})
            if props:
                lines.append("      args: %s" % ", ".join(sorted(props)))
        return "\n".join(lines)


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
