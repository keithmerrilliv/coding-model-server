#!/usr/bin/env python3
import os
import requests
import hashlib
from pathlib import Path

# Server configuration
LINUX_SERVER_IP = os.getenv("QWEN_SERVER_IP", "192.168.50.101")
MEMORY_API_URL = f"http://{LINUX_SERVER_IP}:5000/v1/memory"

# Local Xcode Documentation paths
APPLICATIONS_DIR = Path("/Applications")

# Chunking settings for better RAG quality
CHUNK_SIZE = 2000  # Characters
CHUNK_OVERLAP = 200

def get_file_hash(file_path):
    """Calculate a simple hash to de-duplicate content."""
    try:
        return hashlib.md5(file_path.read_bytes()).hexdigest()
    except:
        return None

def find_docs():
    """Find all markdown and header documentation in Xcode installations, de-duplicated."""
    docs_map = {} # Hash -> Path
    
    for xcode_app in APPLICATIONS_DIR.glob("Xcode*.app"):
        print(f"Searching {xcode_app.name}...")
        
        # 1. Markdown files throughout the bundle
        for md_file in xcode_app.rglob("*.md"):
            if any(part in md_file.parts for part in ["node_modules", ".git", "DerivedData"]):
                continue
            f_hash = get_file_hash(md_file)
            if f_hash and f_hash not in docs_map:
                docs_map[f_hash] = md_file
            
        # 2. ALL Framework headers in SDKs across platforms
        platforms = ["MacOSX.platform", "iPhoneOS.platform", "XROS.platform", "XRSimulator.platform"]
        
        for platform in platforms:
            platform_dir = xcode_app / f"Contents/Developer/Platforms/{platform}/Developer/SDKs"
            if not platform_dir.exists(): continue
            
            # Find the primary SDK (latest or non-versioned)
            for sdk_dir in platform_dir.glob("*.sdk"):
                sdks_fw_dir = sdk_dir / "System/Library/Frameworks"
                if sdks_fw_dir.exists():
                    print(f"  Scanning frameworks in {platform} ({sdk_dir.name})...")
                    # Glob all frameworks
                    for fw_bundle in sdks_fw_dir.glob("*.framework"):
                        headers_dir = fw_bundle / "Headers"
                        if headers_dir.exists():
                            for h_file in headers_dir.glob("*.h"):
                                f_hash = get_file_hash(h_file)
                                if f_hash and f_hash not in docs_map:
                                    docs_map[f_hash] = h_file
    
    return list(docs_map.values())

def chunk_text(text, size, overlap):
    """Split text into overlapping chunks."""
    chunks = []
    if not text: return chunks
    
    start = 0
    while start < len(text):
        end = start + size
        chunks.append(text[start:end])
        if end >= len(text):
            break
        start += (size - overlap)
    return chunks

def ingest_file(file_path):
    """Read a file, chunk it, and send to the server's RAG memory."""
    try:
        content = file_path.read_text(encoding='utf-8', errors='replace')
        filename = file_path.name
        
        # Determine context (Framework name or folder)
        parts = file_path.parts
        if ".framework" in str(file_path):
            fw_index = next(i for i, part in enumerate(parts) if ".framework" in part)
            context = "/".join(parts[fw_index:])
        else:
            context = "/".join(parts[-min(len(parts), 3):])
            
        # Chunking
        chunks = chunk_text(content, CHUNK_SIZE, CHUNK_OVERLAP)
        
        success_count = 0
        for i, chunk in enumerate(chunks):
            # Prepare payload with source context in EVERY chunk
            payload = {
                "text": f"Source: {context}\nChunk: {i+1}/{len(chunks)}\nFile: {filename}\n\n{chunk}"
            }
            
            # Send to server
            response = requests.post(MEMORY_API_URL, json=payload, timeout=30)
            if response.status_code == 200:
                success_count += 1
            else:
                print(f"    ✗ Chunk {i+1} failed: {response.status_code}")
        
        if success_count > 0:
            print(f"  ✓ {context} ({success_count}/{len(chunks)} chunks ingested)")
            return True
        return False
        
    except Exception as e:
        print(f"  ✗ {file_path.name} (Exception: {e})")
        return False

def main():
    print(f"Connecting to Qwen Server at {LINUX_SERVER_IP}...")
    
    all_docs = find_docs()
    print(f"Found {len(all_docs)} unique documentation files after de-duplication.")
    
    if len(all_docs) > 1000:
        confirm = input(f"Ingesting {len(all_docs)} files will create many chunks. Continue? [y/N] ")
        if confirm.lower() != 'y':
            print("Aborted.")
            return

    count = 0
    for doc in all_docs:
        if ingest_file(doc):
            count += 1
            
    print(f"Finished! Ingested {count} files (in chunks) into RAG.")

if __name__ == "__main__":
    main()