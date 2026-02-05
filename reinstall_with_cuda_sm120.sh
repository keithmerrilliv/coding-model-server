#!/bin/bash
# Script to reinstall llama-cpp-python with CUDA support for RTX 5080 (sm_120)
# Compute capability 12.0 requires CUDA 13.0+ with proper architecture flags

set -e

echo "============================================================"
echo "Reinstalling llama-cpp-python with RTX 5080 Support (sm_120)"
echo "============================================================"
echo ""

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

if [ ! -f "server.py" ]; then
    echo -e "${RED}Error: Run from /home/keith-merrill/Dev/qwen-server${NC}"
    exit 1
fi

# Explicitly set CUDA 13.1 paths for RTX 5080 compatibility
export CUDA_HOME="/usr/local/cuda-13.1"
export PATH="${CUDA_HOME}/bin:${PATH}"
export LD_LIBRARY_PATH="${CUDA_HOME}/lib64:${LD_LIBRARY_PATH}"

if ! command -v nvcc &> /dev/null; then
    echo -e "${RED}Error: CUDA compiler (nvcc) not found at ${CUDA_HOME}/bin/nvcc${NC}"
    exit 1
fi

echo -e "${YELLOW}Step 1/6: Stopping service...${NC}"
sudo systemctl stop qwen-server || true
echo -e "${GREEN}✓ Stopped${NC}"
echo ""

echo -e "${YELLOW}Step 2/6: Activating venv and upgrading core tools...${NC}"
source venv/bin/activate
pip install --upgrade pip setuptools wheel
pip install -r requirements.txt
echo -e "${GREEN}✓ Environment updated${NC}"
echo ""

echo -e "${YELLOW}Step 3/6: Uninstalling current llama-cpp-python...${NC}"
pip uninstall -y llama-cpp-python
echo -e "${GREEN}✓ Uninstalled${NC}"
echo ""

echo -e "${YELLOW}Step 4/6: Rebuilding llama-cpp-python with RTX 5080 support (sm_120)...${NC}"
echo "This will compile for sm_120 (Blackwell architecture)"
echo "Compilation takes 3-5 minutes..."
echo ""

# Set CMAKE arguments for CUDA with explicit architecture support
# sm_120 is for RTX 5080 (Blackwell)
export CMAKE_ARGS="-DGGML_CUDA=on -DCMAKE_CUDA_ARCHITECTURES=120"
export FORCE_CMAKE=1

pip install llama-cpp-python>=0.3.16 --force-reinstall --no-cache-dir --verbose

echo ""
echo -e "${GREEN}✓ Compilation complete${NC}"
echo ""

echo -e "${YELLOW}Step 5/6: Verifying GPU support...${NC}"
GPU_SUPPORT=$(python3 -c "from llama_cpp import llama_supports_gpu_offload; print(llama_supports_gpu_offload())" 2>&1)

if [ "$GPU_SUPPORT" = "True" ]; then
    echo -e "${GREEN}✓ GPU Offload Supported: $GPU_SUPPORT${NC}"
else
    echo -e "${RED}✗ GPU Offload Supported: $GPU_SUPPORT${NC}"
fi

# Test actual GPU layer offloading
echo ""
echo "Testing GPU layer assignment..."
python3 << 'TESTEOF'
from llama_cpp import Llama
import sys

try:
    test = Llama(
        model_path="/home/keith-merrill/.lmstudio/models/zhangfeng026/Qwen2.5-Coder-32B-Instruct-Q4_K_M-GGUF/qwen2.5-coder-32b-instruct-q4_k_m.gguf",
        n_ctx=512,
        n_gpu_layers=10,  # Test with 10 layers
        verbose=False
    )
    print("✓ Model test load successful")
    del test
except Exception as e:
    print(f"✗ Model test failed: {e}")
    sys.exit(1)
TESTEOF

echo ""

echo -e "${YELLOW}Step 6/6: Restarting service...${NC}"
sudo systemctl start qwen-server
sleep 3
echo -e "${GREEN}✓ Service restarted${NC}"
echo ""

echo "============================================================"
echo "Next Steps:"
echo "============================================================"
echo "1. Make a test API request to trigger model loading"
echo "2. Check VRAM with: nvidia-smi"
echo "   - Should see ~19-20 GB VRAM (not ~400 MB)"
echo ""
echo "To verify layers on GPU:"
echo '  journalctl -u qwen-server -n 100 | grep "offloaded"'
echo ""
echo "============================================================"
