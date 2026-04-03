"""External service clients — memory, web search, PDF ingestion, Apple docs."""
import os
import json
import subprocess
import threading
import select
import atexit
import logging
from pathlib import Path

import requests

from qwen_client.config import config, COLORS, print_colored

logger = logging.getLogger(__name__)

# Extensions worth ingesting (code + docs). Matches CodeChunker's extension_map.
CODE_EXTENSIONS = {
    '.swift', '.metal', '.h', '.m', '.mm', '.cpp', '.cc', '.c',
    '.py', '.js', '.ts', '.tsx', '.jsx',
    '.go', '.rs', '.java', '.kt', '.cs', '.rb',
    '.sh', '.bash', '.zsh',
    '.md', '.markdown',
}

# Directories to skip during ingestion
IGNORE_DIRS = {
    '.git', '.svn', '.hg', '.idea', '.vscode', '.vs',
    'build', 'Build', 'dist', 'DerivedData', 'cmake-build-debug',
    'out', 'output', 'target', 'bin', 'obj',
    '.xcodeproj', '.xcworkspace', '.xcassets', 'Pods', '.build',
    'node_modules', 'vendor', 'Vendor',
    'ThirdParty', 'thirdparty', 'third_party', 'third-party',
    'External', 'external', 'extern', 'deps', 'Dependencies',
    'venv', 'env', '.venv', '.env', 'myenv', '__pycache__',
    'site-packages', '.tox', '.eggs',
    'Intermediate', 'Saved', 'Binaries', 'Library', 'Temp',
    '.cache', '.gradle', '.cargo',
}

MAX_FILE_SIZE = 100_000  # 100 KB — skip generated/minified files


# ---------------------------------------------------------------------------
# Memory & ingestion
# ---------------------------------------------------------------------------

def ingest_url_content(url):
    """Fetch URL content, strip HTML, and send to server memory."""
    try:
        print_colored(f"[Client] Deep-ingesting content from {url}...", COLORS['BLUE'])
        response = requests.get(url, timeout=20)
        if response.status_code != 200:
            return f"Error: Failed to fetch URL (Status {response.status_code})"

        from bs4 import BeautifulSoup
        soup = BeautifulSoup(response.text, 'html.parser')
        for script in soup(["script", "style"]):
            script.extract()
        text = soup.get_text(separator=' ', strip=True)

        payload = {"text": f"Source URL: {url}\n\n{text}"}
        mem_resp = requests.post(config.MEMORY_API_URL, json=payload, timeout=30)

        if mem_resp.status_code == 200:
            return f"Successfully ingested full content from {url} into RAG database."
        else:
            return f"Error: Server rejected memory ingestion ({mem_resp.status_code})"
    except ImportError:
        return "Error: BeautifulSoup4 is required on the client for deep-ingestion. Run 'pip install beautifulsoup4'."
    except Exception as e:
        return f"Error during deep-ingestion: {str(e)}"


def save_memory(text):
    """Send a memory/fact to the server to be saved."""
    try:
        response = requests.post(config.MEMORY_API_URL, json={"text": text}, timeout=config.REQUEST_TIMEOUT)
        if response.status_code == 200:
            print_colored(f"Memory Saved: {text[:60]}...", COLORS['GREEN'])
            return "Memory saved successfully."
        else:
            return f"Failed to save memory: {response.text}"
    except Exception as e:
        return f"Error saving memory: {str(e)}"


def ingest_codebase(directory, extensions=None):
    """Ingest a local codebase into the server's RAG database.

    Walks the directory tree, reads code files, and sends each to the server
    with the source path so the server's CodeChunker can apply tree-sitter
    AST-aware chunking.

    Args:
        directory: Root directory to ingest.
        extensions: Set of extensions to include (default: CODE_EXTENSIONS).

    Returns:
        Summary string with counts.
    """
    directory = os.path.expanduser(directory)
    if not os.path.isdir(directory):
        return f"Error: '{directory}' is not a directory."

    exts = extensions or CODE_EXTENSIONS
    files_sent = 0
    files_skipped = 0
    errors = 0

    print_colored(f"Ingesting codebase: {directory}", COLORS['CYAN'])
    print_colored(f"  Extensions: {', '.join(sorted(exts))}", COLORS['CYAN'])

    for root, dirs, files in os.walk(directory):
        # Prune ignored directories in-place
        dirs[:] = [d for d in dirs if d not in IGNORE_DIRS]

        for filename in files:
            ext = os.path.splitext(filename)[1].lower()
            if ext not in exts:
                continue

            filepath = os.path.join(root, filename)

            # Skip large files
            try:
                if os.path.getsize(filepath) > MAX_FILE_SIZE:
                    files_skipped += 1
                    continue
            except OSError:
                continue

            # Read and send
            try:
                with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
                    content = f.read()

                if not content.strip():
                    files_skipped += 1
                    continue

                # Send with source path — server uses CodeChunker for AST chunking
                response = requests.post(
                    config.MEMORY_API_URL,
                    json={"text": content, "source": filepath},
                    timeout=30,
                )
                if response.status_code == 200:
                    files_sent += 1
                    if files_sent % 50 == 0:
                        print_colored(f"  Ingested {files_sent} files...", COLORS['CYAN'])
                else:
                    errors += 1
                    logger.warning("Failed to ingest %s: %s", filepath, response.text[:100])

            except Exception as e:
                errors += 1
                logger.warning("Error ingesting %s: %s", filepath, e)

    msg = f"Ingested {files_sent} files ({files_skipped} skipped, {errors} errors) from {directory}"
    print_colored(msg, COLORS['GREEN'])
    return msg


def ingest_pdf(path):
    """Tell the server to ingest a local PDF file."""
    try:
        print_colored(f"Requesting server to ingest PDF: {path}", COLORS['CYAN'])
        response = requests.post(config.INGEST_API_URL, json={"path": path}, timeout=config.LONG_REQUEST_TIMEOUT)
        if response.status_code == 200:
            result = response.json()
            msg = f"Successfully ingested {result['filename']}: {result['chunks']} chunks from {result['pages']} pages."
            print_colored(msg, COLORS['GREEN'])
            return msg
        else:
            error_msg = f"Failed to ingest PDF: {response.text}"
            print_colored(error_msg, COLORS['FAIL'])
            return error_msg
    except Exception as e:
        return f"Error ingesting PDF: {str(e)}"


def web_search(query):
    """Send a search query to the server."""
    try:
        print_colored(f"Searching web for: {query}", COLORS['CYAN'])
        response = requests.post(config.SEARCH_API_URL, json={"query": query}, timeout=config.REQUEST_TIMEOUT)
        if response.status_code == 200:
            result = response.json().get("result", "No results")
            print_colored(f"\n{result}\n", COLORS['GREEN'])
            return f"Search Results:\n{result}"
        else:
            return f"Failed to search: {response.text}"
    except Exception as e:
        return f"Error searching: {str(e)}"


# ---------------------------------------------------------------------------
# Apple documentation (Cupertino MCP)
# ---------------------------------------------------------------------------

class CupertinoMCPClient:
    """Client for interacting with the Cupertino MCP server on macOS."""

    def __init__(self):
        self.process = None
        self.msg_id = 1
        self.lock = threading.Lock()

    def start(self):
        """Start the Cupertino MCP server process."""
        if self.process and self.process.poll() is None:
            return True
        try:
            cupertino_path = subprocess.check_output(["which", "cupertino"], text=True).strip()
            if not cupertino_path:
                return False
            self.process = subprocess.Popen(
                [cupertino_path, "serve"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
            return True
        except Exception as e:
            print_colored(f"Error starting Cupertino MCP: {e}", COLORS['FAIL'])
            return False

    def stop(self):
        """Stop the Cupertino MCP server process."""
        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=2)
            except Exception:
                try:
                    self.process.kill()
                except Exception:
                    pass
            self.process = None

    def _readline_with_timeout(self, timeout: float = 30):
        """Read a line from subprocess stdout with a timeout using select."""
        if not self.process or not self.process.stdout:
            return None
        ready, _, _ = select.select([self.process.stdout], [], [], timeout)
        if ready:
            return self.process.stdout.readline()
        print_colored(f"Cupertino MCP readline timed out after {timeout:.1f} seconds", COLORS['WARNING'])
        return None

    def _send_request(self, method, params):
        """Send a JSON-RPC request to the MCP server and wait for response."""
        if not self.start():
            return {"error": "Cupertino MCP not found or failed to start"}

        with self.lock:
            req_id = self.msg_id
            self.msg_id += 1
            request = {
                "jsonrpc": "2.0",
                "id": req_id,
                "method": method,
                "params": params,
            }
            try:
                self.process.stdin.write(json.dumps(request) + "\n")
                self.process.stdin.flush()

                for _ in range(50):
                    line = self._readline_with_timeout(timeout=30)
                    if not line:
                        if self.process.poll() is not None:
                            return {"error": "Cupertino MCP server process exited unexpectedly."}
                        return {"error": "No response from Cupertino MCP (timed out)."}
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        response = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if response.get("id") == req_id:
                        return response.get("result", {})
                return {"error": "Cupertino MCP sent too many non-matching responses"}
            except Exception as e:
                return {"error": f"Communication error: {e}"}

    def search(self, query):
        """Search Apple documentation using the MCP tool."""
        params = {"name": "search_docs", "arguments": {"query": query}}
        return self._send_request("tools/call", params)

    def read_resource(self, uri):
        """Read a specific documentation resource."""
        return self._send_request("resources/read", {"uri": uri})


cupertino_client = CupertinoMCPClient()
atexit.register(cupertino_client.stop)


def handle_cupertino_search(query):
    """Execute search via Cupertino MCP and save to server memory."""
    print_colored(f"Searching Apple Documentation for: {query}...", COLORS['BLUE'])
    result = cupertino_client.search(query)
    if "error" in result:
        error_msg = f"Cupertino Error: {result['error']}"
        print_colored(error_msg, COLORS['FAIL'])
        return error_msg

    content_list = result.get("content", [])
    text_results = []
    for item in content_list:
        if item.get("type") == "text":
            text_results.append(item.get("text", ""))

    combined_results = "\n\n".join(text_results)
    if not combined_results:
        return "No documentation found for that query."

    print_colored("Saving retrieved documentation to server memory...", COLORS['CYAN'])
    save_memory(f"Apple Documentation ({query}):\n{combined_results[:5000]}")
    return f"Retrieved Apple Documentation for '{query}':\n\n{combined_results}"


# ---------------------------------------------------------------------------
# Apple Deep Docs (server-side MCP)
# ---------------------------------------------------------------------------

def handle_apple_deep_docs(payload_str):
    """Handle Apple Deep Docs command with proper error handling."""
    try:
        payload = json.loads(payload_str)
        tool = payload.get("tool")
        args = payload.get("arguments", {})
        if not tool:
            return "Error: Missing 'tool' in Apple Deep Docs payload"
        return apple_deep_docs_search(tool, args)
    except json.JSONDecodeError as e:
        logger.error("Invalid JSON in Apple Deep Docs payload: %s", payload_str)
        return f"Error: Invalid JSON in Apple Deep Docs payload: {str(e)}"
    except Exception as e:
        logger.error("Error handling Apple Deep Docs: %s", str(e))
        return f"Error handling Apple Deep Docs: {str(e)}"


def apple_deep_docs_search(tool, args):
    """Send a deep doc search query to the server."""
    try:
        print_colored(f"Calling Apple Deep Docs ({tool}): {args}", COLORS['CYAN'])
        payload = {"tool": tool, "arguments": args}
        response = requests.post(config.DEEP_DOCS_API_URL, json=payload, timeout=config.LONG_REQUEST_TIMEOUT)

        if response.status_code == 200:
            result = response.json().get("result", "No results")
            formatted_result = ""
            if isinstance(result, (dict, list)):
                formatted_result = json.dumps(result, indent=2)
            elif isinstance(result, str):
                try:
                    parsed = json.loads(result)
                    formatted_result = json.dumps(parsed, indent=2)
                except Exception:
                    formatted_result = result
            else:
                formatted_result = str(result)

            save_memory(f"Apple Deep Doc ({tool}): {str(args)}\n{formatted_result[:10000]}")
            return f"Apple Deep Docs Result ({tool}):\n{formatted_result}"
        else:
            return f"Failed to call Deep Docs: {response.text}"
    except Exception as e:
        return f"Error in Deep Docs call: {str(e)}"
