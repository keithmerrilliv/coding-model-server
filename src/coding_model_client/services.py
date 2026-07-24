"""External service clients — memory, web search, PDF ingestion, Apple docs."""
import os
import json
import subprocess
import threading
import atexit
import logging
from queue import Queue, Empty

import requests

# Module-level session: keepalive across the client's many memory/search/
# ingest calls saves TCP+TLS setup per request.
_SESSION = requests.Session()

from coding_model_client.config import config, COLORS, print_colored

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

def _is_safe_public_url(url: str) -> tuple[bool, str]:
    """SSRF guard: only allow http/https URLs to public IPs.

    Blocks LLM-driven fetches to:
      - cloud-metadata addresses (169.254.169.254 etc.)
      - loopback (127.0.0.0/8, ::1)
      - link-local (169.254.0.0/16, fe80::/10)
      - RFC1918 / private (10/8, 172.16/12, 192.168/16, fc00::/7)
      - other reserved ranges (multicast, etc.)
    Returns (is_safe, reason). reason is empty on success.
    """
    import socket
    import ipaddress
    from urllib.parse import urlparse
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False, f"unsupported scheme {parsed.scheme!r}"
    if not parsed.hostname:
        return False, "missing hostname"
    try:
        addrs = socket.getaddrinfo(parsed.hostname, None)
    except socket.gaierror as e:
        return False, f"DNS lookup failed: {e}"
    for family, _t, _p, _c, sockaddr in addrs:
        ip_str = sockaddr[0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            return False, f"invalid IP {ip_str!r}"
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
            return False, f"refusing non-public IP {ip}"
    return True, ""


_MAX_REDIRECT_HOPS = 5


def _get_revalidating_redirects(url, *, timeout):
    """GET `url`, re-running the SSRF guard on every redirect hop.

    requests follows redirects itself (up to 30) *inside* Session.get, so a URL
    that passes _is_safe_public_url on hop 0 can still 302 to
    http://169.254.169.254/ and be fetched — the guard never sees hops 1..n.
    So we disable automatic redirects and follow them by hand, validating each
    Location before we touch it. Raises ValueError if a hop fails the guard or
    the chain is too long.
    """
    from urllib.parse import urljoin

    current = url
    for _ in range(_MAX_REDIRECT_HOPS + 1):
        safe, reason = _is_safe_public_url(current)
        if not safe:
            raise ValueError(f"SSRF guard rejected URL ({reason})")
        response = _SESSION.get(current, timeout=timeout, allow_redirects=False)
        if response.status_code not in (301, 302, 303, 307, 308):
            return response
        location = response.headers.get("Location")
        if not location:
            return response  # 3xx without a target — hand it back as-is
        # Resolve relative Locations against the URL we just fetched, then loop
        # to re-validate. Also closes the intermediate response's connection.
        response.close()
        current = urljoin(current, location)
    raise ValueError(f"too many redirects (>{_MAX_REDIRECT_HOPS})")


def ingest_url_content(url):
    """Fetch URL content, strip HTML, and send to server memory."""
    safe, reason = _is_safe_public_url(url)
    if not safe:
        return f"Error: SSRF guard rejected URL ({reason})"
    try:
        print_colored(f"[Client] Deep-ingesting content from {url}...", COLORS['BLUE'])
        try:
            response = _get_revalidating_redirects(url, timeout=20)
        except ValueError as e:
            return f"Error: {e}"
        if response.status_code != 200:
            return f"Error: Failed to fetch URL (Status {response.status_code})"

        from bs4 import BeautifulSoup
        soup = BeautifulSoup(response.text, 'html.parser')
        for script in soup(["script", "style"]):
            script.extract()
        text = soup.get_text(separator=' ', strip=True)

        payload = {"text": f"Source URL: {url}\n\n{text}"}
        mem_resp = _SESSION.post(config.MEMORY_API_URL, json=payload, headers=config.auth_headers, timeout=30)

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
        response = _SESSION.post(config.MEMORY_API_URL, json={"text": text}, headers=config.auth_headers, timeout=config.REQUEST_TIMEOUT)
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
                response = _SESSION.post(
                    config.MEMORY_API_URL,
                    json={"text": content, "source": filepath},
                    headers=config.auth_headers,
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
        response = _SESSION.post(config.INGEST_API_URL, json={"path": path}, headers=config.auth_headers, timeout=config.LONG_REQUEST_TIMEOUT)
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
        response = _SESSION.post(config.SEARCH_API_URL, json={"query": query}, headers=config.auth_headers, timeout=config.REQUEST_TIMEOUT)
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
        # RLock so _send_request can hold the lock and call start() inside it
        # without deadlocking. Lock-protected start() prevents two concurrent
        # first-time callers from each spawning a subprocess. It now guards only
        # the fast operations — process lifecycle, the msg_id counter, and the
        # stdin write — NOT the response wait. A dedicated reader thread (below)
        # owns stdout and hands each response to the waiting caller's queue, so
        # concurrent requests no longer serialize behind one another for up to
        # MAX_TOTAL_WALL_TIME each.
        self.lock = threading.RLock()
        # req_id -> Queue on which the reader thread delivers that request's
        # response (or a None sentinel if the process dies). Guarded by its own
        # lock so the reader can route without contending for self.lock.
        self._pending = {}
        self._pending_lock = threading.Lock()
        self._reader = None
        self._reader_stop = False

    def start(self):
        """Start the Cupertino MCP server process."""
        with self.lock:
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
                # Unlike the server-side Apple Deep Docs client, there's no
                # synchronous handshake reading stdout here, so the reader can
                # take the pipe immediately — nothing else ever reads it.
                self._start_reader(self.process)
                return True
            except Exception as e:
                print_colored(f"Error starting Cupertino MCP: {e}", COLORS['FAIL'])
                return False

    def _start_reader(self, proc):
        """Spawn the stdout demux thread for `proc` (caller holds self.lock)."""
        self._reader_stop = False
        self._reader = threading.Thread(
            target=self._reader_loop, args=(proc,),
            name="cupertino-mcp-reader", daemon=True,
        )
        self._reader.start()

    def _reader_loop(self, proc):
        """Own `proc`'s stdout: parse each JSON-RPC line and deliver it to the
        queue registered for its id.

        This is what lets _send_request wait off-lock. A single reader means one
        consumer of the shared stdout pipe, so responses can't be swallowed by
        the wrong caller — the old design had every caller read the pipe and
        discard lines whose id didn't match, so two at once ate each other's
        replies.
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
            self._fail_all_pending()

    def _fail_all_pending(self):
        with self._pending_lock:
            waiters = list(self._pending.values())
        for q in waiters:
            q.put(None)  # None = connection lost; _send_request maps it to an error

    def stop(self):
        """Stop the Cupertino MCP server process."""
        if self.process:
            self._reader_stop = True
            try:
                self.process.terminate()
                self.process.wait(timeout=2)
            except Exception:
                try:
                    self.process.kill()
                except Exception:
                    pass
            self.process = None
            # readline() returns EOF once the child is gone, so the reader loop
            # falls out on its own; join briefly so a restart gets a clean slate.
            if self._reader is not None:
                self._reader.join(timeout=5)
                self._reader = None
            self._fail_all_pending()

    # 2 min. This is now a per-request wait, not a lock hold, so a slow request
    # no longer blocks every other caller for its duration.
    MAX_TOTAL_WALL_TIME = 120

    def _send_request(self, method, params):
        """Send a JSON-RPC request to the MCP server and wait for response."""
        if not self.start():
            return {"error": "Cupertino MCP not found or failed to start"}

        response_q = Queue()

        # Short critical section: allocate the id, register the waiter, and write
        # the request. The response wait happens AFTER releasing the lock, so
        # concurrent callers overlap instead of queueing for up to 2 min each.
        with self.lock:
            req_id = self.msg_id
            self.msg_id += 1
            with self._pending_lock:
                self._pending[req_id] = response_q
            request = {
                "jsonrpc": "2.0",
                "id": req_id,
                "method": method,
                "params": params,
            }
            try:
                self.process.stdin.write(json.dumps(request) + "\n")
                self.process.stdin.flush()
            except Exception as e:
                with self._pending_lock:
                    self._pending.pop(req_id, None)
                return {"error": f"Communication error: {e}"}

        try:
            try:
                response = response_q.get(timeout=self.MAX_TOTAL_WALL_TIME)
            except Empty:
                return {
                    "error": "Cupertino MCP response not received within "
                             f"{self.MAX_TOTAL_WALL_TIME}s"
                }
            if response is None:  # sentinel from _fail_all_pending
                return {"error": "Cupertino MCP server process exited unexpectedly."}
            return response.get("result", {})
        finally:
            # Always deregister, so a late/never response can't leak the queue.
            with self._pending_lock:
                self._pending.pop(req_id, None)

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
        response = _SESSION.post(config.DEEP_DOCS_API_URL, json=payload, headers=config.auth_headers, timeout=config.LONG_REQUEST_TIMEOUT)

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
