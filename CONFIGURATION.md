# Qwen Multi-Agent Server Configuration

## Environment Variables

The server uses several environment variables for configuration. Copy `.env.example` to `.env` and customize as needed:

### Server Configuration
- `QWEN_SERVER_IP` - IP address of the Qwen server (default: 127.0.0.1)
- `QWEN_SERVER_PORT` - Port of the Qwen server (default: 5000)

### Model Configuration
- `MODEL_PATH` - Path to the model file
- `MODEL_N_CTX` - Context length (default: 4096)
- `MODEL_N_THREADS` - Number of CPU threads to use (0 = auto-detect)
- `MODEL_N_BATCH` - Batch size for processing
- `MODEL_FLASH_ATTENTION` - Enable flash attention (true/false)
- `MODEL_USE_MMAP` - Use memory mapping (true/false)
- `MODEL_USE_MLOCK` - Lock model in RAM (true/false)

### Security Settings
- `ALLOW_SHELL_MODE` - Enable shell features (true/false)
- `COMMAND_WHITELIST` - Comma-separated list of allowed commands

### Scraping Configuration
- `APPLE_DEEP_DOCS_PATH` - Path to Apple Deep Docs MCP server
- `QWEN_SERVER_IP` - IP address for memory ingestion (when scraping)
- `QWEN_SERVER_PORT` - Port for memory ingestion (when scraping)

## Apple Documentation Scraper

The system includes a comprehensive Apple documentation scraping feature:

### Prerequisites
- Cupertino tool installed
- Xcode (for local documentation access with Apple Deep Docs)

### Usage
- Run `cd scraping && python main.py <framework_name>` to scrape documentation
- Output is stored in `scraping/output/<framework_name>/`
- Scraped data is automatically ingested into the RAG database

### Supported Frameworks
- Any Apple framework (Metal, UIKit, AVFoundation, CoreData, Photos, etc.)
- Dynamic class pattern generation for framework-specific APIs
- Comprehensive search for guides, samples, and resources

### MCP Server Integration
- Integrates with Apple Deep Docs MCP server for deeper documentation access
- Falls back to Cupertino when MCP server is unavailable
- Access to hidden Xcode documentation, Swift Evolution proposals, and more

## Memory Service

The memory service stores information for RAG (Retrieval Augmented Generation):

- Stores scraped documentation from the Apple documentation scraper
- Maintains conversation history
- Provides context for AI responses
- Connects to the configured server endpoint for ingestion

## Web Search Service

Provides web search capabilities:

- DDG Search integration via ddgs
- Used for external information retrieval
- Configured through environment variables

## Client Configuration

The client supports various commands and features:

- `/scrape <framework>` - Scrape documentation for a specific Apple framework
- `/cupertino <query>` - Search Apple documentation (macOS only)
- `/ingest <path>` - Ingest files into memory
- `/model <name>` - Switch between different agent models