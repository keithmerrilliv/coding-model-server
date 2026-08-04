#!/usr/bin/env python3
"""Ingest scrape_apple_docs_json.py output into the RAG via POST /v1/memory.

Each record carries its own provenance (source URL, framework, symbol kind,
availability, beta flag), which the server now stores because MemoryRequest
accepts a metadata dict. That is the whole point of this path: the previous
ingest route dropped metadata, so ~89% of the old collection was stamped
source="manual" and could never be selectively refreshed.

Deliberately NOT sending `source` in the request body: that field is a
language hint that routes to the tree-sitter CODE chunker, which is wrong for
prose. The real source URL travels in metadata instead.

Usage:
    python3 ingest_apple_docs_json.py --dry-run  <dir>
    python3 ingest_apple_docs_json.py --server 192.168.1.3 --admin-key ... <dir>
"""

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

CHUNK_SIZE = 2000
CHUNK_OVERLAP = 200


def chunk_text(text, size=CHUNK_SIZE, overlap=CHUNK_OVERLAP):
    """Same windowing as the existing ingest path, so retrieval behaviour is
    comparable to what the collection already contains."""
    if not text:
        return []
    if len(text) <= size:
        return [text]
    out, start = [], 0
    while start < len(text):
        out.append(text[start:start + size])
        if start + size >= len(text):
            break
        start += size - overlap
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("indir")
    ap.add_argument("--server", default=os.getenv("CODING_MODEL_SERVER_IP", "192.168.1.3"))
    ap.add_argument("--port", default=os.getenv("CODING_MODEL_SERVER_PORT", "5000"))
    ap.add_argument("--admin-key", default=os.getenv("ADMIN_API_KEY", ""))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    url = "http://%s:%s/v1/memory" % (args.server, args.port)
    files = sorted(Path(args.indir).glob("*.jsonl"))
    if not files:
        print("no .jsonl files in %s" % args.indir)
        return 1

    jobs, bad_lines = [], 0
    for f in files:
        for line in f.open():
            # Tolerate a truncated trailing line: this runs unattended after a
            # multi-hour crawl, and one malformed record must not cost the
            # whole ingest.
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                bad_lines += 1
                continue
            text, meta = rec["text"], rec["metadata"]
            parts = chunk_text(text)
            for i, part in enumerate(parts):
                m = dict(meta)
                m["chunk_index"] = i
                m["chunk_total"] = len(parts)
                jobs.append({"text": part, "metadata": m})

    print("%d records -> %d chunks from %d frameworks%s" % (
        sum(1 for f in files for _ in f.open()), len(jobs), len(files),
        "  (%d malformed lines skipped)" % bad_lines if bad_lines else ""))
    if args.dry_run:
        print("DRY RUN — nothing sent. Sample payload:")
        print(json.dumps(jobs[0], indent=2)[:700])
        return 0

    if not args.admin_key:
        print("ERROR: --admin-key required (or ADMIN_API_KEY env)")
        return 2

    sess = requests.Session()
    sess.headers.update({"X-Admin-Key": args.admin_key, "Content-Type": "application/json"})
    ok = fail = dup = 0
    t0 = time.time()

    def send(p):
        try:
            r = sess.post(url, json=p, timeout=30)
            if r.status_code != 200:
                return ("fail", r.status_code, r.text[:120])
            return ("dup" if r.json().get("status") == "duplicate" else "ok", 200, "")
        except Exception as e:  # noqa: BLE001
            return ("fail", 0, str(e)[:120])

    errors = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for i, (kind, code, msg) in enumerate(ex.map(send, jobs), 1):
            if kind == "ok":
                ok += 1
            elif kind == "dup":
                dup += 1
            else:
                fail += 1
                if len(errors) < 10:
                    errors.append("%s %s" % (code, msg))
            if i % 500 == 0:
                print("  %d/%d  ok=%d dup=%d fail=%d  %.0fs" % (
                    i, len(jobs), ok, dup, fail, time.time() - t0), flush=True)

    print("\nDONE ok=%d duplicate=%d failed=%d in %.0fs" % (ok, dup, fail, time.time() - t0))
    for e in errors:
        print("  ERROR %s" % e)
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
