import requests
import json
import time
import sys

# Configuration
SERVER_URL = "http://localhost:5000/v1/chat/completions"
MODEL = "architect"

def test_architect():
    print(f"Testing Architect Agent with model: {MODEL}")
    print("-" * 60)

    payload = {
        "model": MODEL,
        "messages": [
            {"role": "user", "content": "Design a high-level architecture for a distributed task queue system using Redis and Python."}
        ],
        "max_tokens": 512,
        "temperature": 0.7,
        "stream": True
    }

    start_time = time.time()
    try:
        response = requests.post(SERVER_URL, json=payload, stream=True, timeout=7200) # Long timeout for large model load and inference
        
        if response.status_code != 200:
            print(f"Error: {response.status_code} - {response.text}")
            sys.exit(1)

        print("Response stream:")
        full_content = ""
        for line in response.iter_lines():
            if line:
                line = line.decode('utf-8')
                if line.startswith("data: "):
                    data_str = line[6:]
                    if data_str == "[DONE]":
                        break
                    try:
                        data = json.loads(data_str)
                        if "choices" in data and len(data["choices"]) > 0:
                            content = data["choices"][0]["delta"].get("content", "")
                            if content:
                                print(content, end="", flush=True)
                                full_content += content
                    except json.JSONDecodeError:
                        pass
        
        print("\n" + "-" * 60)
        duration = time.time() - start_time
        print(f"Total time: {duration:.2f}s")
        print(f"Tokens generated: {len(full_content.split())} (approx)")
        
    except Exception as e:
        print(f"\nRequest failed: {e}")
        sys.exit(1)

if __name__ == "__main__":
    test_architect()
