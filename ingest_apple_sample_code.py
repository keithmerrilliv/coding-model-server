#!/usr/bin/env python3
import requests
import json
import time
import os
import re
import zipfile
import io
import shutil
from pathlib import Path

# Server Configuration
LINUX_SERVER_IP = os.getenv("QWEN_SERVER_IP", "192.168.50.101")
MEMORY_API_URL = f"http://{LINUX_SERVER_IP}:5000/v1/memory"

INDEX_URLS = [
    "https://developer.apple.com/tutorials/data/documentation/samplecode.json",
    "https://developer.apple.com/tutorials/data/documentation/metal.json"
]
BASE_DATA_URL = "https://developer.apple.com/tutorials/data"
ASSET_URL_ROOT = "https://docs-assets.developer.apple.com/published"

# Track progress to allow resuming
PROGRESS_FILE = "ingest_progress.json"

def load_progress():
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE, 'r') as f:
            return json.load(f)
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

def ingest_content(text, source, sample_title, chunk_size=3000):
    """General helper to chunk and send text to server."""
    overlap = 300
    start = 0
    while start < len(text):
        end = start + chunk_size
        chunk = text[start:end]
        payload = {
            "text": f"Source: {source}\nSample: {sample_title}\n\n{chunk}"
        }
        try:
            requests.post(MEMORY_API_URL, json=payload, timeout=15)
        except:
            pass # Skip failed chunks to keep moving
        if end >= len(text): break
        start += (chunk_size - overlap)

def process_zip(zip_id, title):
    """Download and ingest all source files in the project zip."""
    zip_url = f"{ASSET_URL_ROOT}/{zip_id}"
    print(f"    Downloading Source: {zip_url}")
    
    try:
        r = requests.get(zip_url, timeout=60)
        if r.status_code != 200: return
        
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            for file_info in z.infolist():
                if file_info.is_dir(): continue
                
                ext = os.path.splitext(file_info.filename)[1].lower()
                if ext in ['.swift', '.metal', '.h', '.m', '.cpp', '.mm', '.c', '.txt', '.md']:
                    with z.open(file_info) as f:
                        try:
                            content = f.read().decode('utf-8', errors='replace')
                            if len(content.strip()) < 10: continue
                            
                            source_label = f"{zip_url} -> {file_info.filename}"
                            # Smaller chunks for actual code files for precision
                            ingest_content(content, source_label, title, chunk_size=2000)
                        except:
                            continue
        print(f"      ✓ Processed all source files in zip.")
    except Exception as e:
        print(f"      ✗ Zip processing failed: {e}")

def ingest_sample(identifier):
    path = identifier.replace("doc://com.apple.documentation", "").replace("doc://com.apple.metal", "")
    data_url = f"{BASE_DATA_URL}{path}.json"
    
    try:
        resp = requests.get(data_url, timeout=20)
        if resp.status_code in [301, 302]:
            data_url = resp.headers['Location']
            if not data_url.startswith('http'): data_url = f"https://developer.apple.com{data_url}"
            resp = requests.get(data_url, timeout=20)
            
        if resp.status_code != 200: return
        
        data = resp.json()
        role = data.get('metadata', {}).get('role', '')
        if role != 'sampleCode': return
            
        title = data.get('metadata', {}).get('title', 'Unknown Sample')
        print(f"\n🚀 Processing: {title}")
        
        # 1. Ingest Prose/Snippets from JSON
        abstract = " ".join([item.get('text', '') for item in data.get('abstract', []) if 'text' in item])
        prose = extract_text_from_json(data.get('primaryContentSections', []))
        ingest_content(f"Title: {title}\nAbstract: {abstract}\n\n{prose}", data_url, title)
        print(f"    ✓ Ingested documentation prose.")
        
        # 2. Ingest Full Source Code from Zip if available
        zip_id = data.get('sampleCodeDownload', {}).get('action', {}).get('identifier')
        if zip_id:
            process_zip(zip_id, title)
            
    except Exception as e:
        print(f"    ✗ Error: {e}")

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
        print(f"Fetching Index: {index_url}")
        try:
            resp = requests.get(index_url, timeout=30)
            if resp.status_code == 200: find_all_identifiers(resp.json(), all_identifiers)
        except Exception as e: print(f"Error: {e}")
            
    unique_ids = sorted(list(all_identifiers))
    to_process = [i for i in unique_ids if i not in progress["processed_identifiers"]]
    
    print(f"Found {len(unique_ids)} samples. {len(to_process)} remaining to be deep-scraped.")
    
    for i, ident in enumerate(to_process):
        print(f"[{i+1}/{len(to_process)}] {ident}")
        ingest_sample(ident)
        
        progress["processed_identifiers"].append(ident)
        save_progress(progress)
        time.sleep(0.5)
            
    print("\nMission Complete.")

if __name__ == "__main__":
    main()