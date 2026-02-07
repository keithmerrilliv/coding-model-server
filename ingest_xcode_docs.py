#!/usr/bin/env python3
import os
import requests
from pathlib import Path

# Server configuration (matching your qwen_remote.py setup)
LINUX_SERVER_IP = os.getenv("QWEN_SERVER_IP", "192.168.50.101")
MEMORY_API_URL = f"http://{LINUX_SERVER_IP}:5000/v1/memory"

# Local Xcode Documentation paths
APPLICATIONS_DIR = Path("/Applications")
DOC_SUBPATH = "Contents/PlugIns/IDEIntelligenceChat.framework/Versions/A/Resources/AdditionalDocumentation"

def find_docs():
    """Find all markdown documentation in Xcode installations."""
    docs = []
    for xcode_app in APPLICATIONS_DIR.glob("Xcode*.app"):
        doc_dir = xcode_app / DOC_SUBPATH
        if doc_dir.exists():
            for md_file in doc_dir.glob("*.md"):
                docs.append(md_file)
    return docs

def ingest_file(file_path):
    """Read a file and send it to the server's RAG memory."""
    try:
        content = file_path.read_text(encoding='utf-8')
        filename = file_path.name
        
        # Prepare payload
        payload = {
            "text": f"Source: {filename}

{content}"
        }
        
        # Send to server
        response = requests.post(MEMORY_API_URL, json=payload, timeout=30)
        
        if response.status_code == 200:
            print(f"  ✓ {filename} (Ingested)")
            return True
        else:
            print(f"  ✗ {filename} (Error: {response.status_code})")
            return False
    except Exception as e:
        print(f"  ✗ {file_path.name} (Exception: {e})")
        return False

def main():
    print(f"Connecting to Qwen Server at {LINUX_SERVER_IP}...")
    
    all_docs = find_docs()
    print(f"Found {len(all_docs)} local documentation files.")
    
    # Filter for high-priority topics
    priority_keywords = ["Metal", "Reality", "Vision", "Glass", "Shader", "Compute", "PBR", "Blackwell"]
    
    to_ingest = []
    for doc in all_docs:
        if any(kw.lower() in doc.name.lower() for kw in priority_keywords):
            to_ingest.append(doc)
            
    print(f"Filtering for high-priority topics ({', '.join(priority_keywords)})...")
    print(f"Selected {len(to_ingest)} files for ingestion.")
    
    count = 0
    for doc in to_ingest:
        if ingest_file(doc):
            count += 1
            
    print(f"
Finished! Ingested {count} high-priority documentation files into RAG.")

if __name__ == "__main__":
    main()
