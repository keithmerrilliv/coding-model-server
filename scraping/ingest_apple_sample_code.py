#!/usr/bin/env python3
import requests
import json
import time
import os
import sys
import re
import zipfile
import io
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

from coding_model_server.code_chunker import CodeChunker

# Server Configuration
LINUX_SERVER_IP = os.getenv("CODING_MODEL_SERVER_IP", "192.168.50.101")
MEMORY_API_URL = f"http://{LINUX_SERVER_IP}:5000/v1/memory"

INDEX_URLS = [
    "https://developer.apple.com/tutorials/data/documentation/samplecode.json",
    "https://developer.apple.com/tutorials/data/documentation/metal.json"
]
BASE_DATA_URL = "https://developer.apple.com/tutorials/data"
ASSET_URL_ROOT = "https://docs-assets.developer.apple.com/published"

# Use a session for better performance (connection pooling) and a real User-Agent
session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
})

chunker = CodeChunker()

# Track progress to allow resuming
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROGRESS_FILE = os.path.join(SCRIPT_DIR, "ingest_progress.json")
progress_lock = threading.Lock()

def load_progress():
    if os.path.exists(PROGRESS_FILE):
        try:
            with open(PROGRESS_FILE, 'r') as f:
                return json.load(f)
        except:
            pass
    return {"processed_identifiers": []}

def save_progress(progress):
    with open(PROGRESS_FILE, 'w') as f:
        json.dump(progress, f)

def extract_text_from_json(content_list):
    text_parts = []
    if not isinstance(content_list, list): return ""
    for item in content_list:
        if isinstance(item, dict):
            if item.get('type') == 'heading':
                text_parts.append(f"\n## {item.get('text', '')}\n")
            elif 'inlineContent' in item:
                text_parts.append(" ".join([c.get('text', '') for c in item['inlineContent'] if 'text' in c]))
            elif item.get('type') == 'codeListing':
                code = "\n".join(item.get('code', []))
                syntax = item.get('syntax', '')
                text_parts.append(f"\n```{syntax}\n{code}\n```\n")
            elif item.get('type') in ['unorderedList', 'orderedList']:
                for li in item.get('items', []):
                    if 'content' in li:
                        text_parts.append("- " + extract_text_from_json(li['content']))
            elif 'content' in item and isinstance(item['content'], list):
                text_parts.append(extract_text_from_json(item['content']))
        elif isinstance(item, list):
            text_parts.append(extract_text_from_json(item))
    return "\n".join(text_parts)

def send_chunk(payload):
    try:
        session.post(MEMORY_API_URL, json=payload, timeout=30)
    except:
        pass

def ingest_content(text, source, sample_title, chunk_size=3000, pool=None):
    """Chunk and send text to server using optional thread pool."""
    overlap = 300
    start = 0
    payloads = []
    while start < len(text):
        end = start + chunk_size
        chunk = text[start:end]
        payloads.append({
            "text": f"Source: {source}\nSample: {sample_title}\n\n{chunk}"
        })
        if end >= len(text): break
        start += (chunk_size - overlap)

    if pool:
        pool.map(send_chunk, payloads)
    else:
        for p in payloads: send_chunk(p)

def process_zip(zip_id, title):
    """Download and ingest all source files in the project zip."""
    zip_url = f"{ASSET_URL_ROOT}/{zip_id}"
    print(f"    Downloading Source: {zip_url}")

    try:
        start_time = time.time()
        with session.get(zip_url, timeout=60, stream=True) as r:
            if r.status_code != 200: return
            content = io.BytesIO()
            for chunk in r.iter_content(chunk_size=8192): content.write(chunk)

            elapsed = time.time() - start_time
            size_mb = content.tell() / (1024 * 1024)
            print(f"      Downloaded {size_mb:.2f} MB in {elapsed:.1f}s ({(size_mb/elapsed):.2f} MB/s)")

            content.seek(0)
            with zipfile.ZipFile(content) as z:
                with ThreadPoolExecutor(max_workers=20) as pool:
                    for file_info in z.infolist():
                        if file_info.is_dir(): continue
                        ext = os.path.splitext(file_info.filename)[1].lower()

                        if ext in chunker.extension_map:
                            with z.open(file_info) as f:
                                try:
                                    file_text = f.read().decode('utf-8', errors='replace')
                                    if len(file_text.strip()) < 10: continue

                                    # Use chunk_text() directly — no temp files needed
                                    chunks = chunker.chunk_text(file_text, ext)

                                    payloads = []
                                    for c in chunks:
                                        ctx = c['metadata'].get('context', '')
                                        node_type = c['metadata']['type']
                                        payloads.append({
                                            "text": f"Sample: {title}\nSource: {zip_url} -> {file_info.filename}\nContext: {ctx}\nType: {node_type}\n\n{c['text']}"
                                        })
                                    pool.map(send_chunk, payloads)
                                except: continue
        print(f"      Processed all source files in zip.")
    except Exception as e:
        print(f"      Zip processing failed: {e}")

def ingest_sample(identifier):
    """Process a single sample — returns the identifier on success, None on failure."""
    path = identifier.replace("doc://com.apple.documentation", "").replace("doc://com.apple.metal", "")
    data_url = f"{BASE_DATA_URL}{path}.json"

    try:
        resp = session.get(data_url, timeout=20)
        if resp.status_code in [301, 302]:
            data_url = resp.headers['Location']
            if not data_url.startswith('http'): data_url = f"https://developer.apple.com{data_url}"
            resp = session.get(data_url, timeout=20)

        if resp.status_code != 200: return identifier
        data = resp.json()
        if data.get('metadata', {}).get('role', '') != 'sampleCode': return identifier

        title = data.get('metadata', {}).get('title', 'Unknown Sample')
        print(f"\nProcessing: {title}")

        # 1. Ingest Prose
        abstract = " ".join([item.get('text', '') for item in data.get('abstract', []) if 'text' in item])
        prose = extract_text_from_json(data.get('primaryContentSections', []))
        ingest_content(f"Title: {title}\nAbstract: {abstract}\n\n{prose}", data_url, title)
        print(f"    Ingested documentation prose.")

        # 2. Ingest Source (Intelligent Chunking)
        zip_id = data.get('sampleCodeDownload', {}).get('action', {}).get('identifier')
        if zip_id: process_zip(zip_id, title)

        # Politeness delay — still respect Apple's CDN
        time.sleep(0.5)
        return identifier

    except Exception as e:
        print(f"    Error processing {identifier}: {e}")
        return identifier

def find_all_identifiers(obj, identifiers):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, str) and v.startswith("doc://"): identifiers.add(v)
            else: find_all_identifiers(v, identifiers)
    elif isinstance(obj, list):
        for item in obj: find_all_identifiers(item, identifiers)

def main():
    progress = load_progress()
    all_identifiers = set()

    for index_url in INDEX_URLS:
        try:
            resp = session.get(index_url, timeout=30)
            if resp.status_code == 200: find_all_identifiers(resp.json(), all_identifiers)
        except Exception as e: print(f"Error: {e}")

    unique_ids = sorted(list(all_identifiers))
    to_process = [i for i in unique_ids if i not in progress["processed_identifiers"]]

    print(f"Found {len(unique_ids)} samples. {len(to_process)} remaining.")

    # Process samples concurrently (4 workers — moderate to avoid hammering Apple's CDN)
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(ingest_sample, ident): ident for ident in to_process}

        for future in as_completed(futures):
            ident = futures[future]
            try:
                result = future.result()
                if result:
                    with progress_lock:
                        progress["processed_identifiers"].append(result)
                        save_progress(progress)
            except Exception as e:
                print(f"    Error: {e}")

    print("\nMission Complete.")

if __name__ == "__main__":
    main()
