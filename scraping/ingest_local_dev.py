#!/usr/bin/env python3
import os
import requests
import hashlib
import time
import json
import logging
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

# Server Configuration
LINUX_SERVER_IP = os.getenv("QWEN_SERVER_IP", "192.168.50.101")
MEMORY_API_URL = f"http://{LINUX_SERVER_IP}:5000/v1/memory"

DEV_ROOT = os.path.expanduser("~/Dev")
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROGRESS_FILE = os.path.join(SCRIPT_DIR, "ingest_local_dev_progress.json")

# Extensions to ingest
EXTENSIONS = {'.swift', '.metal', '.h', '.m', '.cpp', '.mm', '.py', '.c', '.txt', '.md', '.json', '.yaml', '.yml', '.sh'}

# Directories to ignore
IGNORE_DIRS = {'.git', 'node_modules', 'venv', 'env', 'build', 'dist', 'DerivedData', '.xcodeproj', '.xcassets', '__pycache__', '.idea', '.vscode', 'myenv', 'ingest_venv', '.xcworkspace', 'Pods', '.build'}

session = requests.Session()

def load_progress():
    if os.path.exists(PROGRESS_FILE):
        try:
            with open(PROGRESS_FILE, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logger.warning(f"Failed to load progress file, starting fresh: {e}")
    return {"processed_hashes": {}}

def save_progress(progress):
    tmp = PROGRESS_FILE + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(progress, f)
    os.replace(tmp, PROGRESS_FILE)

def get_file_hash(file_path):
    """Calculate MD5 hash of file content."""
    hasher = hashlib.md5()
    with open(file_path, 'rb') as f:
        buf = f.read()
        hasher.update(buf)
    return hasher.hexdigest()

def send_chunk(payload):
    try:
        resp = session.post(MEMORY_API_URL, json=payload, timeout=30)
        return resp.status_code == 200
    except Exception as e:
        logger.debug(f"Failed to send chunk: {e}")
        return False

def ingest_file(file_path, project_name, pool):
    """Chunk and send file content to server. Returns True only if at least one chunk was accepted."""
    try:
        with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read()

        if len(content.strip()) < 10:
            return True

        source_label = f"LocalDev: {file_path}"
        chunk_size = 2000
        overlap = 200

        start = 0
        payloads = []
        while start < len(content):
            end = start + chunk_size
            chunk = content[start:end]
            payloads.append({
                "text": f"Source: {source_label}\nProject: {project_name}\n\n{chunk}"
            })
            if end >= len(content): break
            start += (chunk_size - overlap)

        results = list(pool.map(send_chunk, payloads))
        if not any(results):
            logger.warning(f"All {len(payloads)} chunks failed for {file_path}")
            return False
        return True
    except Exception as e:
        logger.error(f"Error ingesting {file_path}: {e}")
        return False

def main():
    progress = load_progress()
    processed_hashes = progress["processed_hashes"]
    
    print(f"🚀 Starting ingestion of {DEV_ROOT}...")
    
    files_to_process = []
    
    for root, dirs, files in os.walk(DEV_ROOT):
        # Filter out ignored directories
        dirs[:] = [d for d in dirs if d not in IGNORE_DIRS]
        
        rel_root = os.path.relpath(root, DEV_ROOT)
        project_name = rel_root.split(os.sep)[0] if rel_root != "." else "Root"
        
        for file in files:
            ext = os.path.splitext(file)[1].lower()
            if ext in EXTENSIONS:
                file_path = os.path.join(root, file)
                try:
                    f_hash = get_file_hash(file_path)
                    if file_path not in processed_hashes or processed_hashes[file_path] != f_hash:
                        files_to_process.append((file_path, project_name, f_hash))
                except OSError as e:
                    logger.debug(f"Skipping {file_path}: {e}")
                    continue

    print(f"Found {len(files_to_process)} new or modified files to ingest.")
    
    with ThreadPoolExecutor(max_workers=10) as pool:
        for i, (file_path, project_name, f_hash) in enumerate(files_to_process):
            if (i + 1) % 10 == 0 or i == 0:
                print(f"[{i+1}/{len(files_to_process)}] Processing: {file_path}")
            
            if ingest_file(file_path, project_name, pool):
                processed_hashes[file_path] = f_hash
                if (i + 1) % 100 == 0:
                    save_progress(progress)

    save_progress(progress)
    print("\nMission Complete. Local Dev folder ingested.")

if __name__ == "__main__":
    main()