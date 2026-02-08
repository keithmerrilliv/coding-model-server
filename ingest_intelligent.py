#!/usr/bin/env python3
import os
import requests
import hashlib
import time
import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from code_chunker import CodeChunker

# Server Configuration
LINUX_SERVER_IP = os.getenv("QWEN_SERVER_IP", "192.168.50.101")
MEMORY_API_URL = f"http://{LINUX_SERVER_IP}:5000/v1/memory"

DEV_ROOT = os.path.expanduser("~/Dev")
PROGRESS_FILE = "ingest_intelligent_progress.json"

# Directories to ignore
IGNORE_DIRS = {'.git', 'node_modules', 'venv', 'env', 'build', 'dist', 'DerivedData', '.xcodeproj', '.xcassets', '__pycache__', '.idea', '.vscode', 'myenv', 'ingest_venv'}

session = requests.Session()
chunker = CodeChunker()

def load_progress():
    if os.path.exists(PROGRESS_FILE):
        try:
            with open(PROGRESS_FILE, 'r') as f:
                return json.load(f)
        except:
            pass
    return {"processed_hashes": {}}

def save_progress(progress):
    with open(PROGRESS_FILE, 'w') as f:
        json.dump(progress, f)

def get_file_hash(file_path):
    hasher = hashlib.md5()
    with open(file_path, 'rb') as f:
        buf = f.read()
        hasher.update(buf)
    return hasher.hexdigest()

def send_chunk(payload):
    try:
        session.post(MEMORY_API_URL, json=payload, timeout=30)
    except:
        pass

def main():
    progress = load_progress()
    processed_hashes = progress["processed_hashes"]
    
    print(f"🚀 Starting Intelligent Ingestion of {DEV_ROOT}...")
    
    files_to_process = []
    
    # 1. Discover files
    for root, dirs, files in os.walk(DEV_ROOT):
        dirs[:] = [d for d in dirs if d not in IGNORE_DIRS]
        
        try:
            rel_root = os.path.relpath(root, DEV_ROOT)
            project_name = rel_root.split(os.sep)[0] if rel_root != "." else "Root"
        except:
            project_name = "Root"
            
        for file in files:
            ext = os.path.splitext(file)[1].lower()
            if ext in chunker.extension_map:
                file_path = os.path.join(root, file)
                try:
                    f_hash = get_file_hash(file_path)
                    if file_path not in processed_hashes or processed_hashes[file_path] != f_hash:
                        files_to_process.append((file_path, project_name, f_hash))
                except:
                    continue

    print(f"Found {len(files_to_process)} new or modified files for intelligent chunking.")
    
    # 2. Process files in parallel
    with ThreadPoolExecutor(max_workers=10) as pool:
        for i, (file_path, project_name, f_hash) in enumerate(files_to_process):
            if (i + 1) % 10 == 0 or i == 0:
                print(f"[{i+1}/{len(files_to_process)}] Parsing: {file_path}")
            
            # Use Tree-sitter to get logical chunks
            chunks = chunker.chunk_file(file_path)
            
            # Prepare payloads
            payloads = []
            for c in chunks:
                ctx = c['metadata'].get('context', '')
                source = c['metadata']['source']
                node_type = c['metadata']['type']
                
                payloads.append({
                    "text": f"Project: {project_name}\nSource: {source}\nContext: {ctx}\nType: {node_type}\n\n{c['text']}"
                })
            
            # Send chunks to server
            pool.map(send_chunk, payloads)
            
            # Record progress
            processed_hashes[file_path] = f_hash
            if (i + 1) % 100 == 0:
                save_progress(progress)

    save_progress(progress)
    print("\nIntelligent Ingestion Complete.")

if __name__ == "__main__":
    main()