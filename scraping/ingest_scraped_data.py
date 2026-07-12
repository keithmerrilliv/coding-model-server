#!/usr/bin/env python3
"""
Ingest Scraped Framework Documentation into Memory Service

This script reads the JSON output from the scrapers and sends the content
to the Coding Model Server's memory service for RAG.

When invoked without arguments, auto-discovers and processes all framework
directories under output/.
"""

import os
import sys
import json
import requests
from concurrent.futures import ThreadPoolExecutor

# Load environment variables
from dotenv import load_dotenv
load_dotenv()

# Server configuration (resolved once)
SERVER_IP = os.getenv('CODING_MODEL_SERVER_IP', '127.0.0.1')
SERVER_PORT = os.getenv('CODING_MODEL_SERVER_PORT', '5000')
SERVER_URL = f"http://{SERVER_IP}:{SERVER_PORT}/v1/memory"

session = requests.Session()

def load_json_file(filepath):
    """Load JSON content from a file"""
    try:
        with open(filepath, 'r') as f:
            return json.load(f)
    except Exception as e:
        print(f"Error reading {filepath}: {e}")
        return None

CHUNK_SIZE = 2000
CHUNK_OVERLAP = 200

def chunk_text(text, size=CHUNK_SIZE, overlap=CHUNK_OVERLAP):
    """Split text into overlapping chunks for better RAG retrieval."""
    chunks = []
    if not text:
        return chunks
    start = 0
    while start < len(text):
        end = start + size
        chunks.append(text[start:end])
        if end >= len(text):
            break
        start += (size - overlap)
    return chunks

def send_chunk(payload):
    """Send a single chunk to the memory server."""
    try:
        resp = session.post(SERVER_URL, json=payload, timeout=10)
        return resp.status_code == 200
    except Exception as e:
        print(f"Error sending to memory API: {e}")
        return False

def ingest_content(content, source_type, source_name):
    """Send content to memory service, chunking if necessary. Uses ThreadPool for sends."""
    if not content or not isinstance(content, str):
        return False

    chunks = chunk_text(content)
    payloads = []
    for i, chunk in enumerate(chunks):
        memory_text = f"[{source_type}] {source_name} (chunk {i+1}/{len(chunks)})\n\n{chunk}"
        payloads.append({"text": memory_text})

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(send_chunk, payloads))

    success_count = sum(results)
    if success_count < len(chunks):
        failed = len(chunks) - success_count
        print(f"  Warning: {failed}/{len(chunks)} chunks failed for {source_name}")

    return success_count > 0

def process_docs(output_dir):
    """Process API documentation files"""
    docs_dir = os.path.join(output_dir, "docs")
    if not os.path.exists(docs_dir):
        return 0

    count = 0
    for filename in os.listdir(docs_dir):
        if not filename.endswith(".json") or filename == "summary.json":
            continue

        filepath = os.path.join(docs_dir, filename)
        data = load_json_file(filepath)

        if not data:
            continue

        # Handle different formats based on the tool used (Cupertino vs Deep Docs)
        content = ""
        source = filename

        if "data" in data and isinstance(data["data"], str):
            content = data["data"]
        elif "result" in data:
            content = str(data["result"])

        if content:
            framework_name = os.path.basename(os.path.dirname(os.path.dirname(filepath)))
            if ingest_content(content, f"{framework_name.title()} API Doc", source):
                count += 1
                print(f"Ingested doc: {filename}")

    return count

def process_guides(output_dir):
    """Process guide documentation files"""
    guides_dir = os.path.join(output_dir, "guides")
    if not os.path.exists(guides_dir):
        return 0

    count = 0
    for filename in os.listdir(guides_dir):
        if not filename.endswith(".json") or "summary" in filename:
            continue

        filepath = os.path.join(guides_dir, filename)
        data = load_json_file(filepath)

        if not data:
            continue

        # Extract content from guide structure
        content = ""
        if "content" in data:
            content = data["content"]
        elif "data" in data:
            content = data["data"]

        if content:
            framework_name = os.path.basename(os.path.dirname(os.path.dirname(filepath)))
            if ingest_content(content, f"{framework_name.title()} Guide", filename):
                count += 1
                print(f"Ingested guide: {filename}")

    return count

def process_samples(output_dir):
    """Process sample code files"""
    samples_dir = os.path.join(output_dir, "samples")
    if not os.path.exists(samples_dir):
        return 0

    count = 0
    for filename in os.listdir(samples_dir):
        if not filename.endswith(".json") or "summary" in filename:
            continue

        filepath = os.path.join(samples_dir, filename)
        data = load_json_file(filepath)

        if not data:
            continue

        # Extract content from sample structure
        content = ""
        if "data" in data:
            content = data["data"]

        if content:
            framework_name = os.path.basename(os.path.dirname(os.path.dirname(filepath)))
            if ingest_content(content, f"{framework_name.title()} Sample", filename):
                count += 1
                print(f"Ingested sample: {filename}")

    return count

def process_resources(output_dir):
    """Process resource files"""
    resources_dir = os.path.join(output_dir, "resources")
    if not os.path.exists(resources_dir):
        return 0

    count = 0
    for filename in os.listdir(resources_dir):
        if not filename.endswith(".json") or "summary" in filename:
            continue

        filepath = os.path.join(resources_dir, filename)
        data = load_json_file(filepath)

        if not data:
            continue

        # Extract content from resource structure
        content = ""
        if "data" in data:
            content = data["data"]

        if content:
            framework_name = os.path.basename(os.path.dirname(os.path.dirname(filepath)))
            if ingest_content(content, f"{framework_name.title()} Resource", filename):
                count += 1
                print(f"Ingested resource: {filename}")

    return count

def process_framework(framework, base_output_dir="output"):
    """Process a single framework directory and return ingestion count."""
    output_dir = os.path.join(base_output_dir, framework)
    if not os.path.isdir(output_dir):
        print(f"Warning: {output_dir} not found, skipping")
        return 0

    print(f"\n--- Ingesting framework: {framework} ---")
    total = 0

    print(f"  Processing {framework} API Docs...")
    total += process_docs(output_dir)

    print(f"  Processing {framework} Guides...")
    total += process_guides(output_dir)

    print(f"  Processing {framework} Samples...")
    total += process_samples(output_dir)

    print(f"  Processing {framework} Resources...")
    total += process_resources(output_dir)

    print(f"  {framework}: {total} items ingested")
    return total

def main():
    print("INGESTING SCRAPED DATA INTO MEMORY")
    print("=" * 50)

    # Health check
    health_url = f"http://{SERVER_IP}:{SERVER_PORT}/health"
    try:
        requests.get(health_url, timeout=5)
    except Exception:
        print(f"Warning: Memory server might not be reachable at {SERVER_IP}:{SERVER_PORT}")

    # Determine which frameworks to process
    if len(sys.argv) > 1:
        # Explicit framework argument(s)
        frameworks = sys.argv[1:]
    else:
        # Auto-discover all framework directories under output/
        output_base = "output"
        if not os.path.isdir(output_base):
            print(f"No output/ directory found. Run scrapers first.")
            return
        frameworks = sorted([
            d for d in os.listdir(output_base)
            if os.path.isdir(os.path.join(output_base, d))
        ])
        if not frameworks:
            print("No framework directories found in output/")
            return
        print(f"Auto-discovered {len(frameworks)} frameworks: {', '.join(frameworks)}")

    total_ingested = 0
    for fw in frameworks:
        total_ingested += process_framework(fw)

    print("\n" + "=" * 50)
    print(f"Ingestion Complete. Total items added to memory: {total_ingested}")

if __name__ == "__main__":
    main()
