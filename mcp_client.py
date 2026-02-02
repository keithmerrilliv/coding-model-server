#!/usr/bin/env python3
"""
MCP Client for Apple Deep Docs Server

This script provides a client interface to communicate with the appledeepdoc-mcp server.
"""

import subprocess
import json
import time
import threading
import queue
import os

class MCPClient:
    def __init__(self, server_cmd):
        self.server_cmd = server_cmd
        self.process = None
        self.response_queue = queue.Queue()
        self.running = False
        self.reader_thread = None
        
    def start(self):
        """Start the MCP server process and begin communication"""
        try:
            self.process = subprocess.Popen(
                self.server_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.PIPE,
                universal_newlines=True,
                bufsize=1
            )
            
            self.running = True
            
            # Start the response reader thread
            self.reader_thread = threading.Thread(target=self._read_responses, daemon=True)
            self.reader_thread.start()
            
            # Wait a bit for the server to initialize
            time.sleep(1)
            
            return True
        except Exception as e:
            print(f"Error starting MCP server: {e}")
            return False
    
    def _read_responses(self):
        """Read responses from the MCP server in a separate thread"""
        try:
            while self.running:
                if self.process.stdout:
                    line = self.process.stdout.readline()
                    if line:
                        try:
                            response = json.loads(line.strip())
                            self.response_queue.put(response)
                        except json.JSONDecodeError:
                            # Not all output might be JSON, just continue
                            continue
                    else:
                        # EOF reached
                        break
        except Exception as e:
            print(f"Error reading responses: {e}")
    
    def send_request(self, request):
        """Send a request to the MCP server"""
        if not self.process or self.process.stdin.closed:
            raise Exception("Server is not running")
        
        # Convert request to JSON and send
        request_json = json.dumps(request) + '\n'
        self.process.stdin.write(request_json)
        self.process.stdin.flush()
        
        # Wait for response (with timeout)
        try:
            response = self.response_queue.get(timeout=30)  # 30 second timeout
            return response
        except queue.Empty:
            raise Exception("Timeout waiting for server response")
    
    def stop(self):
        """Stop the MCP server process"""
        self.running = False
        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()

# Global MCP client instance
mcp_client = None

def get_mcp_client():
    global mcp_client
    if mcp_client is None:
        server_path = "/Users/km4/Dev/Qwen/appledeepdoc-mcp/run.sh"
        mcp_client = MCPClient([server_path])
        if not mcp_client.start():
            raise Exception("Failed to start MCP client")
    return mcp_client

def call_mcp_tool(tool_name, **kwargs):
    """Call an MCP tool with the given parameters"""
    client = get_mcp_client()
    
    # Create MCP request
    request = {
        "method": "tools/call",
        "params": {
            "name": tool_name,
            "arguments": kwargs
        }
    }
    
    try:
        response = client.send_request(request)
        return response
    except Exception as e:
        print(f"Error calling MCP tool {tool_name}: {e}")
        return None

def search_docs(query):
    """Search Apple documentation using the MCP server"""
    return call_mcp_tool("search_docs", query=query)

def search_apple_online(query):
    """Search Apple documentation online using the MCP server"""
    return call_mcp_tool("search_apple_online", query=query)

def get_framework_info(framework):
    """Get framework info using the MCP server"""
    return call_mcp_tool("get_framework_info", framework=framework)

def stop_mcp_client():
    global mcp_client
    if mcp_client:
        mcp_client.stop()
        mcp_client = None