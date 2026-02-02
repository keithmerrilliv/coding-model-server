#!/usr/bin/env python3
"""
Apple Deep Docs MCP Server Manager

This script manages the appledeepdoc-mcp server lifecycle.
"""

import subprocess
import time
import signal
import sys
import os
from threading import Thread
import json

class AppleDeepDocsServer:
    def __init__(self, server_dir="/Users/km4/Dev/Qwen/appledeepdoc-mcp"):
        self.server_dir = server_dir
        self.process = None
        self.is_running = False
        
    def start_server(self):
        """Start the appledeepdoc-mcp server"""
        print("Starting Apple Deep Docs MCP server...")
        
        # Change to the server directory
        original_dir = os.getcwd()
        os.chdir(self.server_dir)
        
        try:
            # Start the server using the run.sh script
            self.process = subprocess.Popen(
                ['./run.sh'],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.PIPE
            )
            
            # Give the server a moment to initialize
            time.sleep(2)
            
            # Check if the process is still running
            if self.process.poll() is None:
                self.is_running = True
                print("Apple Deep Docs MCP server started successfully")
                print(f"PID: {self.process.pid}")
                
                # Start monitoring thread
                monitor_thread = Thread(target=self._monitor_process, daemon=True)
                monitor_thread.start()
                
                return True
            else:
                print("Failed to start server - process exited early")
                stdout, stderr = self.process.communicate()
                print(f"STDOUT: {stdout.decode()}")
                print(f"STDERR: {stderr.decode()}")
                return False
                
        except Exception as e:
            print(f"Error starting server: {e}")
            return False
        finally:
            os.chdir(original_dir)
    
    def _monitor_process(self):
        """Monitor the server process"""
        if self.process:
            self.process.wait()
            self.is_running = False
            print("Apple Deep Docs MCP server stopped")
    
    def stop_server(self):
        """Stop the appledeepdoc-mcp server"""
        if self.process and self.is_running:
            print("Stopping Apple Deep Docs MCP server...")
            try:
                self.process.terminate()
                self.process.wait(timeout=5)  # Wait up to 5 seconds
            except subprocess.TimeoutExpired:
                print("Force killing server process...")
                self.process.kill()
                self.process.wait()
            except Exception as e:
                print(f"Error stopping server: {e}")
            
            self.is_running = False
            print("Apple Deep Docs MCP server stopped")
    
    def send_request(self, request_data):
        """Send a request to the MCP server"""
        if not self.is_running or not self.process:
            print("Server is not running")
            return None
            
        try:
            # Convert request to JSON string and encode
            request_str = json.dumps(request_data) + '\n'
            request_bytes = request_str.encode('utf-8')
            
            # Send the request
            self.process.stdin.write(request_bytes)
            self.process.stdin.flush()
            
            # Read response (this is simplified - real MCP protocol is more complex)
            # For demonstration purposes, we'll just return a placeholder
            return {"status": "request_sent", "data": request_data}
            
        except Exception as e:
            print(f"Error sending request: {e}")
            return None

# Global server instance
server_instance = None

def start_server():
    global server_instance
    if server_instance is None:
        server_instance = AppleDeepDocsServer()
    return server_instance.start_server()

def stop_server():
    global server_instance
    if server_instance:
        server_instance.stop_server()
        server_instance = None

def get_server_status():
    global server_instance
    if server_instance:
        return server_instance.is_running
    return False

if __name__ == "__main__":
    if len(sys.argv) > 1:
        if sys.argv[1] == "start":
            start_server()
            print("Press Ctrl+C to stop the server...")
            try:
                # Keep the process alive
                while get_server_status():
                    time.sleep(1)
            except KeyboardInterrupt:
                print("\nReceived interrupt signal")
                stop_server()
        elif sys.argv[1] == "stop":
            stop_server()
        elif sys.argv[1] == "status":
            print(f"Server running: {get_server_status()}")
        else:
            print("Usage: python server_manager.py [start|stop|status]")
    else:
        print("Usage: python server_manager.py [start|stop|status]")