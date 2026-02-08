#!/usr/bin/env python3
"""Inspect recent RAG database entries.

NOTE: This script must be run on the Linux server (192.168.50.101) where
the ChromaDB database lives, NOT on the macOS client.
"""
import chromadb
import os

persist_directory = "qwen_memory_db"
client = chromadb.PersistentClient(path=persist_directory)
collection = client.get_collection(name="qwen_agent_memory")

# Fetch recent memories
results = collection.get(
    include=["documents", "metadatas"],
    limit=100
)

memories = []
for doc, meta, mem_id in zip(results["documents"], results["metadatas"], results["ids"]):
    memories.append({
        "id": mem_id,
        "content": doc,
        "source": meta.get("source", "unknown"),
        "timestamp": meta.get("timestamp", 0)
    })

# Sort by timestamp descending
memories.sort(key=lambda x: x['timestamp'], reverse=True)

print("Inspecting the latest 30 additions to the RAG database:\n")
for i, mem in enumerate(memories[:30], 1):
    source = mem['source']
    content = mem['content'][:300].replace('\n', ' ')
    print(f"{i}. ID: {mem['id']} | Source: {source}")
    print(f"   {content}...")
    print("-" * 40)