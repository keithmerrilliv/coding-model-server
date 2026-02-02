#!/usr/bin/env python3
"""
Ingest Scraped Framework Documentation into Memory Service

This script reads the JSON output from the scrapers and sends the content
to the Qwen Server's memory service for RAG.
"""

import os
import sys
import json
import requests
import time

def load_json_file(filepath):
    """Load JSON content from a file"""
    try:
        with open(filepath, 'r') as f:
            return json.load(f)
    except Exception as e:
        print(f"Error reading {filepath}: {e}")
        return None

def ingest_content(content, source_type, source_name):
    """Send content to memory service"""
    if not content or not isinstance(content, str):
        return False

    # Create a structured memory entry
    memory_text = f"[{source_type}] {source_name}\n\n{content}"

    try:
        response = requests.post("http://127.0.0.1:5000/v1/memory", json={"text": memory_text}, timeout=10)
        if response.status_code == 200:
            return True
        else:
            print(f"Failed to ingest: {response.text}")
            return False
    except Exception as e:
        print(f"Error sending to memory API: {e}")
        return False

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

def main():
    # Get framework from command line argument, default to 'metal'
    framework = sys.argv[1] if len(sys.argv) > 1 else "metal"
    output_dir = f"output/{framework}"
    
    print("INGESTING SCRAPED DATA INTO MEMORY")
    print("="*50)

    # Wait for server to be potentially ready if this is running in a chain
    try:
        requests.get("http://127.0.0.1:5000/health", timeout=5)
    except Exception:
        print("Warning: Memory server might not be reachable at localhost:5000")

    total_ingested = 0

    print(f"\nProcessing {framework} API Docs...")
    docs_count = process_docs(output_dir)
    total_ingested += docs_count

    print(f"\nProcessing {framework} Guides...")
    guides_count = process_guides(output_dir)
    total_ingested += guides_count

    print(f"\nProcessing {framework} Samples...")
    samples_count = process_samples(output_dir)
    total_ingested += samples_count

    print(f"\nProcessing {framework} Resources...")
    resources_count = process_resources(output_dir)
    total_ingested += resources_count

    print("\n" + "="*50)
    print(f"Ingestion Complete. Total items added to memory: {total_ingested}")

if __name__ == "__main__":
    main()