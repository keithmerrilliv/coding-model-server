#!/usr/bin/env python3
import requests
import json
import time
import os
from bs4 import BeautifulSoup

# Server Configuration
LINUX_SERVER_IP = os.getenv("QWEN_SERVER_IP", "192.168.50.101")
MEMORY_API_URL = f"http://{LINUX_SERVER_IP}:5000/v1/memory"

# Priority Documentation Clusters for Apple
DOCS_TO_SCRAPE = [
    # Metal 4 / Blackwell / sm_120
    "https://developer.apple.com/documentation/metal/metal_4",
    "https://developer.apple.com/documentation/metal/gpu_features/understanding_gpu_family_support",
    "https://developer.apple.com/documentation/metal/compute_passes/writing_data_parallel_compute_functions",
    
    # RealityKit / Object Capture
    "https://developer.apple.com/documentation/realitykit/creating_3d_objects_from_photographs",
    "https://developer.apple.com/documentation/realitykit/objectcaptureview",
    "https://developer.apple.com/documentation/realitykit/objectcapturesession",
    
    # Vision / Face Extraction
    "https://developer.apple.com/documentation/vision/detecting_faces_in_images",
    "https://developer.apple.com/documentation/vision/extracting_facial_features_for_each_face"
]

def ingest_url(url):
    print(f"Scraping {url}...")
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.114 Safari/537.36'}
        resp = requests.get(url, headers=headers, timeout=20)
        if resp.status_code != 200:
            print(f"  ✗ Failed to fetch (Status {resp.status_code})")
            return

        soup = BeautifulSoup(resp.text, 'html.parser')
        
        # Target the main documentation content area
        main_content = soup.find('main') or soup.find('article') or soup.body
        
        # Remove navigation, footer, and scripts
        for element in main_content(["nav", "footer", "script", "style", "header"]):
            element.extract()
            
        text = main_content.get_text(separator='
', strip=True)
        
        # Push to RAG
        payload = {"text": f"Source: {url}

{text}"}
        m_resp = requests.post(MEMORY_API_URL, json=payload, timeout=30)
        
        if m_resp.status_code == 200:
            print(f"  ✓ Ingested successfully.")
        else:
            print(f"  ✗ Server rejected ingestion ({m_resp.status_code})")
            
    except Exception as e:
        print(f"  ✗ Exception: {e}")

def main():
    print(f"Starting High-Fidelity Apple Documentation Scrape...")
    for url in DOCS_TO_SCRAPE:
        ingest_url(url)
        time.sleep(2) # Politeness delay
    print("
Mission Complete.")

if __name__ == "__main__":
    main()
