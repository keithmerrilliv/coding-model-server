#!/usr/bin/env python3
"""RAG Database Utilities

Consolidated utility for inspecting, searching, sanitizing, and testing
the ChromaDB memory database.

NOTE: This script must be run on the Linux server where the ChromaDB database lives.

Usage:
    python rag_utils.py count              - Show record count and sample data
    python rag_utils.py recent [N]         - Show N most recent entries (default: 30)
    python rag_utils.py inspect <mem_id>   - Show full content of a specific memory
    python rag_utils.py sanitize           - Remove known junk entries
    python rag_utils.py test-pdf <path>    - Test PDF ingestion
"""

import sys
import os
from pathlib import Path

# Allow lazy `from memory_service import MemoryService` (cmd_test_pdf) to
# resolve the repo-root module when this script is invoked from scripts/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import chromadb

PERSIST_DIR = "memory_db"
COLLECTION_NAME = "qwen_agent_memory"


def get_collection():
    client = chromadb.PersistentClient(path=PERSIST_DIR)
    return client.get_collection(name=COLLECTION_NAME)


def cmd_count():
    """Show record count and sample data."""
    collection = get_collection()
    record_count = collection.count()
    print(f"Number of records in the RAG database: {record_count}")

    try:
        sample_data = collection.get(limit=5)
        print(f"Sample record IDs: {sample_data['ids'][:3]}...")
        if sample_data['metadatas'] and sample_data['metadatas'][0]:
            print(f"Sample metadata keys: {list(sample_data['metadatas'][0].keys())}")
    except Exception as e:
        print(f"Could not retrieve sample data: {e}")


def cmd_recent(n=30):
    """Show the N most recent entries."""
    collection = get_collection()
    results = collection.get(include=["documents", "metadatas"], limit=100)

    memories = []
    for doc, meta, mem_id in zip(results["documents"], results["metadatas"], results["ids"]):
        memories.append({
            "id": mem_id,
            "content": doc,
            "source": meta.get("source", "unknown"),
            "timestamp": meta.get("timestamp", 0)
        })

    memories.sort(key=lambda x: x['timestamp'], reverse=True)

    print(f"Showing the latest {min(n, len(memories))} additions to the RAG database:\n")
    for i, mem in enumerate(memories[:n], 1):
        content = mem['content'][:300].replace('\n', ' ')
        print(f"{i}. ID: {mem['id']} | Source: {mem['source']}")
        print(f"   {content}...")
        print("-" * 40)


def cmd_inspect(mem_id):
    """Show full content of a specific memory."""
    collection = get_collection()
    result = collection.get(ids=[mem_id], include=["documents", "metadatas"])

    if result["documents"]:
        print(f"Full text for memory {mem_id}:")
        print("-" * 60)
        print(result["documents"][0])
        print("-" * 60)
        print("Metadata:", result["metadatas"][0])
    else:
        print(f"Memory {mem_id} not found.")


def cmd_sanitize():
    """Remove known junk entries from the database."""
    collection = get_collection()
    total_before = collection.count()
    print(f"Total memories before sanitization: {total_before}")

    results = collection.get(include=["documents"])
    ids_to_delete = []

    patterns_to_remove = [
        "Apple Deep Doc (search_apple_online):",
        "Apple Deep Doc (fetch_apple_documentation):",
        "Apple Deep Doc (search_wwdc_notes):",
        "Apple Deep Doc (search_metal4):",
        "1 validation error for call",
        "2 validation errors for call",
        "Unknown tool: search_metal4",
        '{"query":',
    ]

    for doc, mem_id in zip(results["documents"], results["ids"]):
        if any(pattern in doc for pattern in patterns_to_remove):
            ids_to_delete.append(mem_id)

    if ids_to_delete:
        print(f"Found {len(ids_to_delete)} redundant search logs/errors for deletion.")
        batch_size = 100
        for i in range(0, len(ids_to_delete), batch_size):
            batch = ids_to_delete[i:i + batch_size]
            collection.delete(ids=batch)
        print(f"\nSanitization complete. Remaining: {collection.count()}")
    else:
        print("No redundant search logs found.")


def cmd_test_pdf(pdf_path):
    """Test PDF ingestion."""
    import logging
    import warnings
    from memory_service import MemoryService

    logging.basicConfig(level=logging.DEBUG)
    logger = logging.getLogger('test_pdf')

    def warn_with_log(message, category, filename, lineno, file=None, line=None):
        logger.warning(f"Warning: {category.__name__}: {message} ({filename}:{lineno})")
    warnings.showwarning = warn_with_log

    ms = MemoryService()
    abs_path = os.path.abspath(pdf_path)
    print(f"Testing ingestion of {abs_path}")
    result = ms.ingest_pdf(abs_path)
    print(f"Result: {result}")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "count":
        cmd_count()
    elif cmd == "recent":
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 30
        cmd_recent(n)
    elif cmd == "inspect":
        if len(sys.argv) < 3:
            print("Usage: python rag_utils.py inspect <mem_id>")
            sys.exit(1)
        cmd_inspect(sys.argv[2])
    elif cmd == "sanitize":
        cmd_sanitize()
    elif cmd == "test-pdf":
        if len(sys.argv) < 3:
            print("Usage: python rag_utils.py test-pdf <path>")
            sys.exit(1)
        cmd_test_pdf(sys.argv[2])
    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
