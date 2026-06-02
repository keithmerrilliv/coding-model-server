#!/bin/bash
# Setup script for Qwen Multi-Agent Server (FastAPI)

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "Setting up Qwen Multi-Agent Server (FastAPI)..."
echo "================================================"

# Check Python version
echo "Checking Python version..."
if ! command -v python3 &> /dev/null; then
    echo "Error: python3 not found. Please install Python 3.8 or higher."
    exit 1
fi

PYTHON_VERSION=$(python3 --version | cut -d' ' -f2 | cut -d'.' -f1,2)
echo "Found Python $PYTHON_VERSION"

# Create virtual environment
if [ -d "venv" ]; then
    echo "Virtual environment already exists. Skipping creation."
else
    echo "Creating virtual environment..."
    python3 -m venv venv
    echo "Virtual environment created."
fi

# Activate virtual environment
echo "Activating virtual environment..."
source venv/bin/activate

# Upgrade pip
echo "Upgrading pip..."
pip install --upgrade pip

# Install dependencies + the three src/ packages in one step. pyproject.toml
# is the single source of truth for dependencies; `-e .` installs the core
# deps and makes qwen_server / qwen_client / qwen_autonomous importable plus
# wires the qwen-client / qwen-autonomous console scripts. Add the client
# extras (rich, beautifulsoup4) with `pip install -e '.[client]'`.
echo "Installing dependencies and packages (pip install -e .)..."
pip install -e .

# Setup external tools (MCPs)
echo ""
echo "Setting up external tools..."
mkdir -p tools

# Apple Deep Docs MCP
if [ ! -d "tools/appledeepdoc-mcp" ]; then
    echo "Cloning Apple Deep Docs MCP..."
    git clone https://github.com/Ahrentlov/appledeepdoc-mcp.git tools/appledeepdoc-mcp
fi

echo "Setting up Apple Deep Docs MCP environment..."
cd tools/appledeepdoc-mcp
if [ ! -d "venv" ]; then
    python3 -m venv venv
fi
source venv/bin/activate
pip install --upgrade pip
pip install hatchling fastmcp requests beautifulsoup4
deactivate
cd ../..

# Create .env if it doesn't exist
if [ ! -f ".env" ]; then
    echo "Creating .env from .env.example..."
    cp .env.example .env
    echo ".env file created. Please edit it with your configuration."
else
    echo ".env file already exists."
fi

echo ""
echo "================================================"
echo "Setup complete!"
echo "================================================"
echo ""
echo "Next steps:"
echo "1. Edit .env file with your configuration"
echo "2. Start the server with: ./start.sh"
echo "3. Or install as systemd service (see below)"
echo ""
echo "To install as systemd service:"
echo "  sudo cp systemd/qwen-server.service /etc/systemd/system/"
echo "  sudo systemctl daemon-reload"
echo "  sudo systemctl enable qwen-server"
echo "  sudo systemctl start qwen-server"
echo ""
