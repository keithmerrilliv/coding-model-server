import chromadb
import os

persist_directory = "qwen_memory_db"
client = chromadb.PersistentClient(path=persist_directory)
collection = client.get_collection(name="qwen_agent_memory")

mem_id = "mem_1770084206_0b3b0b63"
result = collection.get(ids=[mem_id], include=["documents", "metadatas"])

if result["documents"]:
    print(f"Full text for memory {mem_id}:")
    print("-" * 60)
    print(result["documents"][0])
    print("-" * 60)
    print("Metadata:", result["metadatas"][0])
else:
    print(f"Memory {mem_id} not found.")