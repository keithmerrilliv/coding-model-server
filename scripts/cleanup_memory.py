#!/usr/bin/env python3
"""Clean up the RAG memory database by removing low-quality entries.

Scans SQLite directly for speed (842K docs in seconds vs hours via ChromaDB API).

Targets:
  1. PDF TOC noise (dotted lines, page headers with no content)
  2. Leaked model thinking (reasoning saved as memory)
  3. Near-empty documents (whitespace, short fragments)

Usage:
  python3 cleanup_memory.py --dry-run   # Preview what would be deleted
  python3 cleanup_memory.py             # Execute cleanup
  python3 cleanup_memory.py --vacuum    # Execute cleanup + VACUUM SQLite
"""
import re
import sys
import argparse
import sqlite3

DB_PATH = "memory_db/chroma.sqlite3"
BATCH_SIZE = 500


def classify(text):
    """Return a reason string if the document should be deleted, else None."""
    if not text or len(text.strip()) < 20:
        return "too_short"

    stripped = text.strip()

    # PDF TOC noise: mostly dots/periods
    dot_count = stripped.count('.')
    if dot_count > 20 and dot_count / len(stripped) > 0.3:
        return "toc_noise"

    # Page header/footer only
    if re.match(r'^(\s*---\s*Page\s+\d+\s*---\s*)+$', stripped):
        return "page_header_only"

    # Leaked thinking: model reasoning that was saved as memory
    thinking_patterns = [
        r'^(?:Let me|I need to|I should|We need to|Let\'s|Thus final|The content can)',
        r'^(?:fact\s*\.\s*Just provide|Not possible due to|Could just store)',
        r'^(?:Maybe user expects|Simpler:|Steps:\s*$)',
        r'audit_report_content\\n\$\(cat',
    ]
    lines = [l for l in stripped.split('\n') if l.strip()]
    if lines:
        matching_lines = sum(
            1 for line in lines
            if any(re.search(p, line.strip(), re.IGNORECASE) for p in thinking_patterns)
        )
        # Flag only if >50% of lines match thinking patterns
        if matching_lines / len(lines) > 0.5:
            return "leaked_thinking"

    return None


def cleanup(dry_run=True, vacuum=False):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # Get total count
    cur.execute("SELECT count(*) FROM embedding_fulltext_search")
    total = cur.fetchone()[0]
    print(f"Database: {total:,} documents")

    # Scan fulltext table — has rowid that maps to embeddings.id
    # The fulltext search table's rowid corresponds to embeddings.id
    cur.execute("SELECT rowid, * FROM embedding_fulltext_search")

    reasons = {}
    delete_rowids = []
    scanned = 0

    for row in cur:
        rowid = row[0]
        text = row[1] if len(row) > 1 else ""
        scanned += 1

        reason = classify(text)
        if reason:
            delete_rowids.append(rowid)
            reasons[reason] = reasons.get(reason, 0) + 1

        if scanned % 100000 == 0:
            print(f"  Scanned {scanned:,}/{total:,}... flagged {len(delete_rowids):,}")

    print(f"\nScan complete: {scanned:,} documents")
    print(f"Flagged for deletion: {len(delete_rowids):,} ({100*len(delete_rowids)//max(total,1)}%)")
    for reason, count in sorted(reasons.items(), key=lambda x: -x[1]):
        print(f"  {reason}: {count:,}")

    if dry_run:
        print(f"\nDry run — no changes made. Run without --dry-run to delete.")
        conn.close()
        return

    if not delete_rowids:
        print("Nothing to delete.")
        conn.close()
        return

    # Map rowids to embedding_ids for ChromaDB deletion
    print(f"\nLooking up embedding IDs...")
    embedding_ids = []
    for i in range(0, len(delete_rowids), BATCH_SIZE):
        batch = delete_rowids[i:i + BATCH_SIZE]
        placeholders = ",".join("?" * len(batch))
        cur.execute(f"SELECT embedding_id FROM embeddings WHERE id IN ({placeholders})", batch)
        embedding_ids.extend([r[0] for r in cur.fetchall()])

    conn.close()

    print(f"Deleting {len(embedding_ids):,} documents via ChromaDB API...")
    import chromadb
    client = chromadb.PersistentClient(path="memory_db")
    col = client.get_collection("qwen_agent_memory")

    for i in range(0, len(embedding_ids), BATCH_SIZE):
        batch = embedding_ids[i:i + BATCH_SIZE]
        try:
            col.delete(ids=batch)
        except Exception as e:
            print(f"  Warning: batch delete failed: {e}")
        if (i // BATCH_SIZE) % 50 == 0:
            print(f"  Deleted {min(i + BATCH_SIZE, len(embedding_ids)):,}/{len(embedding_ids):,}")

    remaining = col.count()
    print(f"\nDone. {total:,} → {remaining:,} documents ({total - remaining:,} removed)")

    if vacuum:
        print("\nVACUUMing SQLite database...")
        conn = sqlite3.connect(DB_PATH)
        conn.execute("VACUUM")
        conn.close()
        import os
        size_mb = os.path.getsize(DB_PATH) / (1024 * 1024)
        print(f"VACUUM complete. Database size: {size_mb:,.0f} MB")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Clean up RAG memory database")
    parser.add_argument("--dry-run", action="store_true", help="Preview without deleting")
    parser.add_argument("--vacuum", action="store_true", help="VACUUM SQLite after cleanup")
    args = parser.parse_args()

    cleanup(dry_run=args.dry_run, vacuum=args.vacuum)
