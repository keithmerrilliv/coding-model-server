# Apple Documentation Scraping & RAG Ingestion

Scrapes Apple framework documentation and loads it — plus the local code and
sample-code corpora — into the server's RAG memory (`POST /v1/memory`).

The scraper is first-party and stdlib-only: it walks the same JSON API the
documentation site itself consumes
(`developer.apple.com/tutorials/data/documentation/<path>.json`), so every
chunk carries provenance — source URL, framework, symbol kind, and the
per-platform availability Apple publishes (including the `beta` flag). Two
earlier third-party integrations (the `cupertino` CLI and an MCP-driven
scraper) rotted to unusable while still exiting zero and were removed; the
docstring at the top of `scrape_apple_docs_json.py` is the post-mortem.

## What's here

| File | Purpose |
|---|---|
| `scrape_apple_docs_json.py` | The scraper. `python3 scrape_apple_docs_json.py [framework ...] [--out DIR] [--max-pages N] [--ingest]` — with `--ingest` it POSTs chunks straight to the server. |
| `ingest_apple_docs_json.py` | Standalone ingester for scraper output: `python3 ingest_apple_docs_json.py <indir> [--server IP] [--port N] [--admin-key KEY] [--dry-run]`. |
| `refresh_apple_docs.sh` | Scheduled weekly refresh (`coding-model-apple-docs-refresh.timer`). Replaces per framework instead of appending, and **refuses to touch the RAG if the harvest collapses** relative to the last success — the guard that keeps an unattended job honest. |
| `overnight_refresh.sh` | Unattended overnight crawl + provision, with per-framework page caps derived from Apple's own index sizes (see its header for why Swift/Foundation are deliberately under-capped). |
| `run_all_ingestion.sh` | Parallel repopulation of the **non-Apple** sources (local code, sample code, Xcode docs). Do *not* clear the database first — the header comment explains why. |
| `ingest_intelligent.py`, `ingest_apple_sample_code.py`, `ingest_xcode_docs.py`, `ingest_scraped_data.py` | The individual ingesters `run_all_ingestion.sh` drives. |
| `requirements.txt` | `requests` + `beautifulsoup4` for the ingesters. The scraper itself needs nothing beyond the standard library. |

Output directories (`output/`, `output-json/`) are not tracked.

## From the client

`/scrape [framework]` runs the scraper server-side. **No argument means ALL
frameworks** — roughly 30k pages, hours of crawling — so name a framework
(`/scrape Metal`) unless you mean it.

## Configuration

| Variable | Default | Used for |
|---|---|---|
| `CODING_MODEL_SERVER_IP` | `192.0.2.11` (placeholder — set your server's IP) | Where the scraper/ingesters POST chunks |
| `CODING_MODEL_SERVER_PORT` | `5000` | Server port |
| `ADMIN_API_KEY` | *(required by the server)* | Auth for `POST /v1/memory` |
| `INGEST_BATCH` | *(optional)* | Ingestion batch-size override |

See [docs/CONFIGURATION.md](../docs/CONFIGURATION.md) for the full server-side
environment reference, including the Apple Deep Docs MCP service
(`APPLE_DEEP_DOCS_PATH` — a directory, not a script).
