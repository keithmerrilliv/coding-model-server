#!/usr/bin/env python3
"""
Startup script for Apple Deep Docs MCP Server and Scraping

This script starts the appledeepdoc-mcp server and then runs the scraping.
"""

import subprocess
import sys
import os
import time
import signal
import atexit

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)

# Global variable to hold the server process
server_process = None

def start_apple_deep_docs_server():
    """Start the appledeepdoc-mcp server"""
    global server_process

    server_dir = os.path.join(REPO_ROOT, "appledeepdoc-mcp")

    print("Starting Apple Deep Docs MCP server...")

    try:
        # Start the server in the background
        server_process = subprocess.Popen(
            [os.path.join(server_dir, "run.sh")],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.PIPE
        )
        
        # Give the server time to initialize
        time.sleep(3)
        
        if server_process.poll() is None:
            print(f"Apple Deep Docs MCP server started successfully (PID: {server_process.pid})")
            return True
        else:
            print("Failed to start server")
            stdout, stderr = server_process.communicate()
            print(f"STDOUT: {stdout.decode()}")
            print(f"STDERR: {stderr.decode()}")
            return False
            
    except Exception as e:
        print(f"Error starting server: {e}")
        return False

def stop_apple_deep_docs_server():
    """Stop the appledeepdoc-mcp server"""
    global server_process
    
    if server_process:
        print("Stopping Apple Deep Docs MCP server...")
        try:
            server_process.terminate()
            server_process.wait(timeout=5)
            print("Server stopped successfully")
        except subprocess.TimeoutExpired:
            print("Force killing server...")
            server_process.kill()
            server_process.wait()
            print("Server force killed")
        except Exception as e:
            print(f"Error stopping server: {e}")
        finally:
            server_process = None

def run_scraping(framework="metal"):
    """Run the scraping with the specified framework"""
    try:
        # Import and run the main scraping
        sys.path.insert(0, SCRIPT_DIR)
        from main import main
        import sys
        
        # Temporarily replace sys.argv to simulate command line arguments
        original_argv = sys.argv[:]
        sys.argv = ['main.py', framework]
        
        try:
            main()
        finally:
            sys.argv = original_argv
            
    except Exception as e:
        print(f"Error running scraping: {e}")
        import traceback
        traceback.print_exc()

def main():
    """Main function to start server and run scraping"""
    if not start_apple_deep_docs_server():
        print("Failed to start server, exiting...")
        sys.exit(1)
    
    # Register cleanup function to stop server on exit
    atexit.register(stop_apple_deep_docs_server)
    
    # Allow time for server to fully start
    time.sleep(2)
    
    # Get framework from command line arguments or default to 'metal'
    framework = sys.argv[1] if len(sys.argv) > 1 else "metal"
    
    print(f"Running scraping for framework: {framework}")
    run_scraping(framework)
    
    # Stop server when done
    stop_apple_deep_docs_server()

if __name__ == "__main__":
    main()