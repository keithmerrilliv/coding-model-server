import requests
import json
import time

url = "http://localhost:5000/v1/chat/completions"
headers = {"Content-Type": "application/json"}
data = {
    "model": "implementer",
    "messages": [{"role": "user", "content": "Hello, are you running on GPU?"}],
    "max_tokens": 50,
    "stream": False
}

print("Sending request to trigger model load...")
start = time.time()
try:
    response = requests.post(url, headers=headers, json=data)
    print(f"Status Code: {response.status_code}")
    if response.status_code == 200:
        print("Response:", json.dumps(response.json(), indent=2))
    else:
        print("Error:", response.text)
except Exception as e:
    print(f"Request failed: {e}")
print(f"Time taken: {time.time() - start:.2f}s")
