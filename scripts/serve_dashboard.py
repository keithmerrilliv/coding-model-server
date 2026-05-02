#!/usr/bin/env python3
"""Static-file server for the dashboard SPA.

Serves dashboard/dist/ on a configurable host:port. Falls back to
index.html on any 404 so React Router (BrowserRouter) deep links like
/specs/abc123 resolve correctly when the user types or refreshes them.

No external dependencies — stdlib only. Run as a systemd service
(`qwen-dashboard.service`).
"""
from __future__ import annotations

import argparse
import logging
import os
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

logger = logging.getLogger("qwen-dashboard")


class SPAFallbackHandler(SimpleHTTPRequestHandler):
    """SimpleHTTPRequestHandler with SPA-style 404 → index.html fallback.

    Standard SimpleHTTPRequestHandler returns 404 for /specs/abc, since
    that path doesn't exist on disk. React Router would have handled it
    client-side, but only if the browser receives index.html first. So
    we intercept the 404 and serve index.html instead, leaving the
    original URL in place — React Router reads window.location and
    routes to the right component.
    """

    # Class-level so the handler instance keeps the same root across requests.
    serve_root: Path = Path()

    def translate_path(self, path: str) -> str:
        # Resolve relative to serve_root, NOT the cwd of the process.
        rel = path.split("?", 1)[0].split("#", 1)[0].lstrip("/")
        candidate = (self.serve_root / rel).resolve()
        try:
            candidate.relative_to(self.serve_root.resolve())
        except ValueError:
            # Path traversal attempt — just return root index path so
            # send_head() will 404 it cleanly.
            return str(self.serve_root / "index.html")
        return str(candidate)

    def send_head(self):
        """Override to fall through to index.html for unknown HTML routes.

        We only fall through when the missing path looks like a route
        (no file extension) — never for /assets/missing.js, since that's
        a real bug we want to surface as 404.
        """
        path = self.translate_path(self.path)
        if not os.path.exists(path) and self._is_spa_route(self.path):
            self.path = "/index.html"
        return super().send_head()

    @staticmethod
    def _is_spa_route(url_path: str) -> bool:
        # Strip query/hash, then check the basename for a file extension.
        clean = url_path.split("?", 1)[0].split("#", 1)[0]
        basename = clean.rsplit("/", 1)[-1]
        return "." not in basename

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        logger.info("%s - %s", self.address_string(), fmt % args)


def main() -> int:
    parser = argparse.ArgumentParser(description="Serve dashboard/dist/ as a SPA")
    parser.add_argument(
        "--root",
        default=os.getenv(
            "QWEN_DASHBOARD_ROOT",
            str(Path(__file__).resolve().parent.parent / "dashboard" / "dist"),
        ),
        help="Path to the built dashboard directory (default: ../dashboard/dist)",
    )
    parser.add_argument(
        "--host",
        default=os.getenv("QWEN_DASHBOARD_HOST", "0.0.0.0"),
        help="Bind host (default: 0.0.0.0; set to 127.0.0.1 for loopback only)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("QWEN_DASHBOARD_PORT", "3001")),
        help="Bind port (default: 3001)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    root = Path(args.root).resolve()
    if not (root / "index.html").is_file():
        logger.error(
            "dashboard build not found at %s. Run `npm run build` in dashboard/ first.",
            root,
        )
        return 1

    SPAFallbackHandler.serve_root = root
    httpd = HTTPServer((args.host, args.port), SPAFallbackHandler)
    logger.info("serving %s at http://%s:%d (SPA fallback enabled)", root, args.host, args.port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("shutdown requested")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
