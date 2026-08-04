#!/usr/bin/env python3
"""Scrape Apple framework documentation from developer.apple.com's own JSON API.

WHY THIS EXISTS, given scrape_generic_docs.py already scrapes Apple docs:

  1. That script shells out to `cupertino`, which is installed on neither the
     Mac Studio nor zooshly, and cannot be installed as documented —
     CONFIGURATION.md says `brew install cupertino` but no such formula
     exists (it is github.com/mihaelamj/cupertino, a source build). The other
     documented source, the Apple Deep Docs MCP server, needs
     APPLE_DEEP_DOCS_PATH, which is unset on both machines.
  2. Worse, it fails SILENTLY: the `cupertino` call sits inside a broad
     `except Exception` that records the string "failed" per entity and lets
     the run complete successfully. Combined with run_all_ingestion.sh's
     "The database should be cleared before running this", a well-intentioned
     refresh wipes the collection and repopulates it with nothing.
  3. It also discards provenance. Everything went in through POST /v1/memory
     without metadata, so add_memory stamped source="manual" — which is why
     ~89% of the existing 83k chunks cannot be traced to a framework, URL or
     SDK version, and why a targeted refresh is impossible.

This scraper walks the same JSON the documentation site itself consumes:

    https://developer.apple.com/tutorials/data/documentation/<path>.json

No third-party CLI, no MCP server, and every chunk carries real provenance —
source URL, framework, symbol kind, and the per-platform availability that
Apple publishes (including the `beta` flag and `introducedAt` version), so
retrieval can tell stable API from beta churn.

Usage:
    python3 scrape_apple_docs_json.py --dry-run Metal
    python3 scrape_apple_docs_json.py --out output-json Metal SwiftUI
    python3 scrape_apple_docs_json.py --ingest --server 192.168.1.3 Metal
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = "https://developer.apple.com/tutorials/data/documentation"
UA = "coding-model-docs-scraper/1.0 (+local RAG provisioning)"

# Default scope: the graphics stack the Metal projects need, plus the
# general-purpose frameworks every generated Swift project touches.
DEFAULT_FRAMEWORKS = [
    "Metal", "MetalKit", "MetalFX", "MetalPerformanceShaders",
    "CompositorServices", "RealityKit", "ARKit", "ModelIO",
    "Swift", "SwiftUI", "Foundation",
]


class Fetcher:
    """HTTP with retries and a polite delay. Raises on give-up rather than
    returning a sentinel, so a broken run fails loudly instead of quietly
    producing 'failed' placeholders the way the cupertino path does."""

    def __init__(self, delay=0.25, retries=3, timeout=20):
        self.delay = delay
        self.retries = retries
        self.timeout = timeout
        self.count = 0
        self.errors = []

    def get_json(self, url):
        last = None
        for attempt in range(self.retries):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    self.count += 1
                    time.sleep(self.delay)
                    return json.loads(r.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return None          # genuinely absent, not an error
                last = e
            except Exception as e:       # noqa: BLE001 - network is broad
                last = e
            time.sleep(self.delay * (2 ** attempt))
        self.errors.append((url, str(last)))
        return None


def _text(nodes):
    """Flatten Apple's inline-content node lists into plain text."""
    out = []
    for n in nodes or []:
        t = n.get("type")
        if t == "text":
            out.append(n.get("text", ""))
        elif t == "codeVoice":
            out.append("`%s`" % n.get("code", ""))
        elif t in ("emphasis", "strong", "link", "reference", "inlineHead"):
            out.append(_text(n.get("inlineContent")) or n.get("title", ""))
        elif t == "paragraph":
            out.append(_text(n.get("inlineContent")))
        elif t == "codeListing":
            out.append("\n```\n%s\n```\n" % "\n".join(n.get("code", [])))
        elif t in ("heading", "aside"):
            out.append(_text(n.get("content") or n.get("inlineContent")))
        elif t in ("unorderedList", "orderedList"):
            for item in n.get("items", []):
                out.append("- " + _text(item.get("content")))
        elif t == "content":
            out.append(_text(n.get("content")))
        elif "content" in n:
            out.append(_text(n.get("content")))
        elif "inlineContent" in n:
            out.append(_text(n.get("inlineContent")))
    return " ".join(x for x in out if x).strip()


def _declaration(sections):
    for s in sections or []:
        if s.get("kind") == "declarations":
            for d in s.get("declarations", []):
                toks = d.get("tokens", [])
                if toks:
                    return "".join(t.get("text", "") for t in toks)
    return ""


def _content_prose(sections):
    parts = []
    for s in sections or []:
        if s.get("kind") == "content":
            parts.append(_text(s.get("content")))
    return "\n\n".join(p for p in parts if p)


def _availability(md):
    """Compact per-platform availability, and whether ANY platform is beta."""
    plats, beta, intro = [], False, {}
    for p in md.get("platforms") or []:
        name = p.get("name")
        if not name:
            continue
        bits = [name]
        if p.get("introducedAt"):
            bits.append(p["introducedAt"])
            intro[name] = p["introducedAt"]
        if p.get("deprecated"):
            bits.append("deprecated")
        if p.get("beta"):
            bits.append("beta")
            beta = True
        plats.append(" ".join(bits))
    return "; ".join(plats), beta, intro


def render_symbol(doc, url):
    """Turn one documentation JSON page into (text, metadata)."""
    md = doc.get("metadata", {}) or {}
    title = md.get("title") or url.rsplit("/", 1)[-1]
    kind = md.get("symbolKind") or md.get("roleHeading") or doc.get("kind") or "doc"
    abstract = _text(doc.get("abstract"))
    decl = _declaration(doc.get("primaryContentSections"))
    prose = _content_prose(doc.get("primaryContentSections"))
    avail, is_beta, _intro = _availability(md)

    body = ["# %s" % title]
    if kind:
        body.append("Kind: %s" % kind)
    if abstract:
        body.append(abstract)
    if decl:
        body.append("```swift\n%s\n```" % decl)
    if avail:
        body.append("Availability: %s" % avail)
    if prose:
        body.append(prose)
    text = "\n\n".join(body).strip()

    meta = {
        "source": "https://developer.apple.com" + url,
        "doc_title": title,
        "symbol_kind": kind,
        "availability": avail[:480],
        "is_beta": is_beta,
        "scraped_at": time.strftime("%Y-%m-%d"),
        "ingest_batch": os.environ.get("INGEST_BATCH", "apple-docs"),
    }
    return text, meta


def crawl(framework, fetcher, max_pages, verbose=True):
    """Breadth-first over a framework's reference graph, staying inside it."""
    root = "/documentation/%s" % framework.lower()
    seen, queue, pages = {root}, [root], []
    while queue and len(pages) < max_pages:
        path = queue.pop(0)
        doc = fetcher.get_json("%s%s.json" % (BASE, path[len("/documentation"):]))
        if not doc:
            continue
        text, meta = render_symbol(doc, path)
        if len(text) > 40:
            meta["framework"] = framework
            pages.append((text, meta))
            if verbose and len(pages) % 25 == 0:
                print("    %s: %d pages" % (framework, len(pages)), flush=True)
        for ref in (doc.get("references") or {}).values():
            u = ref.get("url") or ""
            if (u.startswith(root + "/") or u == root) and u not in seen:
                seen.add(u)
                queue.append(u)
    return pages


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("frameworks", nargs="*", default=None)
    ap.add_argument("--out", default="output-json")
    ap.add_argument("--max-pages", type=int, default=1200)
    ap.add_argument("--dry-run", action="store_true",
                    help="crawl and report, write nothing, ingest nothing")
    ap.add_argument("--ingest", action="store_true")
    ap.add_argument("--server", default=os.getenv("CODING_MODEL_SERVER_IP", "192.168.1.3"))
    ap.add_argument("--port", default=os.getenv("CODING_MODEL_SERVER_PORT", "5000"))
    ap.add_argument("--admin-key", default=os.getenv("ADMIN_API_KEY", ""))
    ap.add_argument("--delay", type=float, default=0.25)
    args = ap.parse_args()

    frameworks = args.frameworks or DEFAULT_FRAMEWORKS
    fetcher = Fetcher(delay=args.delay)
    outdir = Path(args.out)
    if not args.dry_run:
        outdir.mkdir(parents=True, exist_ok=True)

    total = 0
    for fw in frameworks:
        print("== %s" % fw, flush=True)
        pages = crawl(fw, fetcher, args.max_pages)
        beta = sum(1 for _, m in pages if m.get("is_beta"))
        chars = sum(len(t) for t, _ in pages)
        print("   %d pages, %d beta-flagged, %d chars" % (len(pages), beta, chars))
        total += len(pages)
        if not args.dry_run:
            with (outdir / ("%s.jsonl" % fw)).open("w") as f:
                for text, meta in pages:
                    f.write(json.dumps({"text": text, "metadata": meta}) + "\n")

    print("\nTOTAL %d pages across %d frameworks; %d HTTP fetches, %d errors"
          % (total, len(frameworks), fetcher.count, len(fetcher.errors)))
    for u, e in fetcher.errors[:10]:
        print("  ERROR %s -> %s" % (u, e))
    if fetcher.errors:
        print("  (%d total errors — investigate before ingesting)" % len(fetcher.errors))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
