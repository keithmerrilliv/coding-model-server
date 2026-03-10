#!/usr/bin/env python3
"""
Apple Deep Docs MCP Server Manager
Provides a unified interface for both the server and the CLI.
"""

import subprocess
import time
import signal
import sys
import os
import json
import select
import logging
from threading import Lock, Thread
from typing import Dict, List, Optional, Any

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
            # Default to tools/appledeepdoc-mcp relative to this script
            base_dir = os.path.dirname(os.path.abspath(__file__))
            self.mcp_path = os.path.join(base_dir, "tools", "appledeepdoc-mcp")
        else:
            self.mcp_path = mcp_path
            
        self.process = None
        self.msg_id = 1
        self.lock = Lock()
        self.venv_python = self._get_venv_python_path()
        self.is_running = False

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
        """Start the MCP server process and perform handshake if not already running"""
        if self.process and self.process.poll() is None:
            self.is_running = True
            return True

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
                    "clientInfo": {"name": "qwen-server", "version": "2.0"}
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
            return True
        except Exception as e:
            logger.error(f"Error starting Apple Deep Docs MCP: {e}")
            self.is_running = False
            return False

    def stop(self):
        """Stop the MCP server process"""
        if self.process:
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
            logger.info("Apple Deep Docs MCP server stopped")

    def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> str:
        """Call a specific tool on the MCP server and return the result as text"""
        if not self.start():
            return "Error: Apple Deep Docs MCP server failed to start."
            
        with self.lock:
            req_id = self.msg_id
            self.msg_id += 1
            
            request = {
                "jsonrpc": "2.0",
                "id": req_id,
                "method": "tools/call",
                "params": {
                    "name": tool_name,
                    "arguments": arguments
                }
            }
            
            try:
                # Write request
                payload = json.dumps(request)
                logger.info(f"Sending MCP Request: {payload[:200]}...")
                self.process.stdin.write(payload + "\n")
                self.process.stdin.flush()

                # Read response with timeout
                max_attempts = 50
                for _ in range(max_attempts):
                    line = self._readline_with_timeout(timeout=60)
                    if not line:
                        if self.process.poll() is not None:
                            self.is_running = False
                            return "Error: MCP server process exited unexpectedly."
                        return "Error: No response from MCP server (timed out)."

                    line = line.strip()
                    if not line:
                        continue

                    try:
                        response = json.loads(line)
                        if response.get("id") == req_id:
                            result = response.get("result", {})
                            content = result.get("content", [])
                            text_parts = []
                            for item in content:
                                if item.get("type") == "text":
                                    text_parts.append(item.get("text", ""))

                            if text_parts:
                                return "\n\n".join(text_parts)

                            return json.dumps(result, indent=2)

                    except json.JSONDecodeError:
                        continue

                return "Error: MCP server sent too many non-matching responses."

            except Exception as e:
                logger.error(f"Communication error with Deep Docs MCP: {e}")
                return f"Error: Documentation fetch failed: {str(e)}"

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
                print("Usage: python server_manager.py call <tool_name> [args_json]")
            else:
                tool = sys.argv[2]
                args = json.loads(sys.argv[3]) if len(sys.argv) > 3 else {}
                print(service.call_tool(tool, args))
        else:
            print("Usage: python server_manager.py [start|stop|status|call]")
    else:
        print("Usage: python server_manager.py [start|stop|status|call]")
