"""External service clients — memory, web search, PDF ingestion, Apple docs."""
import os
import json
import logging

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

def _validate_public_url(url: str) -> tuple[bool, str, "list[str]"]:
    """SSRF guard: only allow http/https URLs to public IPs.

    Blocks LLM-driven fetches to:
      - cloud-metadata addresses (169.254.169.254 etc.)
      - loopback (127.0.0.0/8, ::1)
      - link-local (169.254.0.0/16, fe80::/10)
      - RFC1918 / private (10/8, 172.16/12, 192.168/16, fc00::/7)
      - other reserved ranges (multicast, etc.)

    Returns (is_safe, reason, validated_ips). The IP list is the point: the
    caller CONNECTS to one of these instead of letting requests re-resolve
    the hostname, closing the DNS-rebinding window between check and fetch
    (DEV-163) — a rebinding domain could answer public here and
    169.254.169.254 microseconds later.
    """
    import socket
    import ipaddress
    from urllib.parse import urlparse
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False, f"unsupported scheme {parsed.scheme!r}", []
    if not parsed.hostname:
        return False, "missing hostname", []
    try:
        addrs = socket.getaddrinfo(parsed.hostname, None)
    except socket.gaierror as e:
        return False, f"DNS lookup failed: {e}", []
    validated: list[str] = []
    for family, _t, _p, _c, sockaddr in addrs:
        ip_str = sockaddr[0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            return False, f"invalid IP {ip_str!r}", []
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
            return False, f"refusing non-public IP {ip}", []
        validated.append(ip_str)
    if not validated:
        return False, "hostname resolved to no addresses", []
    return True, "", validated


def _is_safe_public_url(url: str) -> tuple[bool, str]:
    """Boolean-only view of the guard, for callers that just gate."""
    safe, reason, _ = _validate_public_url(url)
    return safe, reason


_MAX_REDIRECT_HOPS = 5


def _pinned_get(url: str, ip: str, *, timeout):
    """GET *url* connecting to the already-validated *ip* (DEV-163).

    The URL's host is swapped for the literal IP and the original hostname
    rides in the Host header, so requests never re-resolves the name — a
    rebinding domain cannot answer public to the guard and private to the
    fetch. Redirects stay disabled; the caller validates each hop.

    HTTPS is exempt: pinning the IP breaks SNI/certificate validation, and
    trading TLS identity for rebinding protection is a bad swap. TLS already
    binds the response to the certified hostname, so a rebind lands on a
    host that cannot present a valid cert for it.
    """
    from urllib.parse import urlparse, urlunparse
    parsed = urlparse(url)
    if parsed.scheme != "http":
        return _SESSION.get(url, timeout=timeout, allow_redirects=False)
    host_header = parsed.netloc
    literal = f"[{ip}]" if ":" in ip else ip
    netloc = f"{literal}:{parsed.port}" if parsed.port else literal
    pinned_url = urlunparse(parsed._replace(netloc=netloc))
    return _SESSION.get(pinned_url, timeout=timeout, allow_redirects=False,
                        headers={"Host": host_header})


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
        safe, reason, ips = _validate_public_url(current)
        if not safe:
            raise ValueError(f"SSRF guard rejected URL ({reason})")
        response = _pinned_get(current, ips[0], timeout=timeout)
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


def apple_deep_docs_list():
    """List the MCP server's available tools (GET on the same endpoint).

    Exists because the tool names were undiscoverable: POST requires a `tool`
    and nothing published the valid values, so the whole service looked dead
    (DEV-480).
    """
    try:
        response = _SESSION.get(config.DEEP_DOCS_API_URL,
                                headers=config.auth_headers,
                                timeout=config.LONG_REQUEST_TIMEOUT)
        if response.status_code == 200:
            return response.json().get("result", "No tools reported")
        return f"Error listing tools: HTTP {response.status_code}"
    except Exception as e:
        return f"Error listing tools: {e}"


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
