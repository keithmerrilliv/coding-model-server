from llama_cpp import Llama
import sys
import os

print("Testing GPU model load...")

model_path = "/home/keith-merrill/.lmstudio/models/zhangfeng026/Qwen2.5-Coder-32B-Instruct-Q4_K_M-GGUF/qwen2.5-coder-32b-instruct-q4_k_m.gguf"

if not os.path.exists(model_path):
    print(f"Model not found at {model_path}")
    sys.exit(1)

try:
    test = Llama(
        model_path=model_path,
        n_ctx=512,
        n_gpu_layers=16, 
        verbose=True
    )
    print("✓ Model test load successful")
except Exception as e:
    print(f"✗ Model test failed: {e}")
    sys.exit(1)
