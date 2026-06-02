#!/usr/bin/env python3
"""Purge bulk-ingested code samples from the RAG database.

Removes all <text>.* source entries EXCEPT:
  - manual (agent-saved memories)
  - *.pdf (documentation)
  - <text>.md (markdown docs)

Keeps these intact. After purging, relevant Swift/Metal files can be
re-ingested with better chunking via reingest_code.py.

Usage:
  python3 purge_bulk_code.py --dry-run   # Preview
  python3 purge_bulk_code.py             # Execute
  python3 purge_bulk_code.py --vacuum    # Execute + reclaim disk
"""
import argparse
import sqlite3

import chromadb

DB_PATH = "memory_db"
SQLITE_PATH = f"{DB_PATH}/chroma.sqlite3"
COLLECTION = "qwen_agent_memory"
BATCH_SIZE = 500

# Sources to KEEP
KEEP_SOURCES = {'manual', '<text>.md'}
KEEP_SUFFIXES = {'.pdf'}


def should_purge(source):
    if source in KEEP_SOURCES:
        return False
    for suffix in KEEP_SUFFIXES:
        if source.endswith(suffix):
            return False
    return True


def purge(dry_run=True, vacuum=False):
    conn = sqlite3.connect(SQLITE_PATH)
    cur = conn.cursor()

    # Get sources to purge
    cur.execute('''
        SELECT string_value, count(*)
        FROM embedding_metadata
        WHERE key = 'source'
        GROUP BY string_value
        ORDER BY count(*) DESC
    ''')

    purge_sources = []
    keep_count = 0
    purge_count = 0

    print(f"{'Source':<45} {'Count':>8}  Action")
    print("-" * 70)
    for source, count in cur.fetchall():
        if should_purge(source):
            purge_sources.append(source)
            purge_count += count
            print(f"{source:<45} {count:>8,}  PURGE")
        else:
            keep_count += count
            print(f"{source:<45} {count:>8,}  keep")

    print(f"\nKeep: {keep_count:,} | Purge: {purge_count:,}")

    if dry_run:
        print("\nDry run — no changes. Run without --dry-run to delete.")
        conn.close()
        return

    if not purge_sources:
        print("Nothing to purge.")
        conn.close()
        return

    # Get embedding IDs for purge sources
    print(f"\nCollecting IDs for {len(purge_sources)} sources...")
    all_ids = []
    for source in purge_sources:
        cur.execute('''
            SELECT e.embedding_id
            FROM embedding_metadata em
            JOIN embeddings e ON em.id = e.id
            WHERE em.key = 'source' AND em.string_value = ?
        ''', (source,))
        ids = [r[0] for r in cur.fetchall()]
        all_ids.extend(ids)

    conn.close()

    print(f"Deleting {len(all_ids):,} documents...")
    client = chromadb.PersistentClient(path=DB_PATH)
    col = client.get_collection(COLLECTION)

    deleted = 0
    for i in range(0, len(all_ids), BATCH_SIZE):
        batch = all_ids[i:i + BATCH_SIZE]
        try:
            col.delete(ids=batch)
            deleted += len(batch)
        except Exception as e:
            print(f"  Warning: batch failed: {e}")
        if deleted % 10000 == 0:
            print(f"  Deleted {deleted:,}/{len(all_ids):,}")

    remaining = col.count()
    print(f"\nDone. Purged {deleted:,} documents. Remaining: {remaining:,}")

    if vacuum:
        import os
        before = os.path.getsize(SQLITE_PATH)
        print(f"\nVACUUMing ({before / 1024**3:.1f} GB)...")
        conn = sqlite3.connect(SQLITE_PATH)
        conn.execute("VACUUM")
        conn.close()
        after = os.path.getsize(SQLITE_PATH)
        print(f"Done. {before / 1024**3:.1f} GB → {after / 1024**3:.1f} GB")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--vacuum", action="store_true")
    args = parser.parse_args()
    purge(dry_run=args.dry_run, vacuum=args.vacuum)
