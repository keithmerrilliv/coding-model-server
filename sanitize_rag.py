#!/usr/bin/env python3
"""Clean up ChromaDB RAG database from invalid/unwanted entries.

NOTE: This script must be run on the Linux server (192.168.50.101) where
the ChromaDB database lives, NOT on the macOS client.
"""
import chromadb
import os

persist_directory = "qwen_memory_db"
client = chromadb.PersistentClient(path=persist_directory)
collection = client.get_collection(name="qwen_agent_memory")

# Initial count
total_before = collection.count()
print(f"Total memories before sanitization: {total_before}")

# Fetch all memories to filter them
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
    '{"query":', # Likely search metadata JSON
]

for doc, mem_id in zip(results["documents"], results["ids"]):
    # Check if any pattern matches
    if any(pattern in doc for pattern in patterns_to_remove):
        ids_to_delete.append(mem_id)

if ids_to_delete:
    print(f"Found {len(ids_to_delete)} redundant search logs/errors for deletion.")
    
    # ChromaDB delete works best in smaller batches
    batch_size = 100
    for i in range(0, len(ids_to_delete), batch_size):
        batch = ids_to_delete[i:i + batch_size]
        collection.delete(ids=batch)
        
    print("\nSanitization complete.")
    print(f"Total memories remaining: {collection.count()}")
else:
    print("No redundant search logs found.")