#!/usr/bin/env python3
"""Script to check the number of records in the RAG database."""

import os
from memory_service import MemoryService

def main():
    # Initialize the memory service
    memory_service = MemoryService()
    
    # Get the collection
    collection = memory_service._collection
    
    # Get the count of records
    record_count = collection.count()
    
    print(f"Number of records in the RAG database: {record_count}")
    
    # Also get some additional info about the collection
    try:
        # Get a sample of the data to see what types of records exist
        sample_data = collection.get(limit=5)
        print(f"Sample record IDs: {sample_data['ids'][:3]}...")
        if sample_data['metadatas']:
            print(f"Sample metadata keys: {list(sample_data['metadatas'][0].keys()) if sample_data['metadatas'][0] else 'None'}")
    except Exception as e:
        print(f"Could not retrieve sample data: {e}")

if __name__ == "__main__":
    main()