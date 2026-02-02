# Apple Documentation Scraper Configuration

## Overview

The Apple documentation scraper is a comprehensive system for collecting documentation from multiple sources including Cupertino, Apple Deep Docs MCP server, and web resources.

## Required Setup for Open-Source Tools

The scripts expect the following open-source tools to be available in your environment:

- **Cupertino**: Open-source tool for accessing Apple documentation (https://github.com/mihaelamj/cupertino)
- **Apple Deep Docs**: Open-source documentation search tool (https://github.com/Ahrentlov/appledeepdoc-mcp)

## Environment Configuration

### Environment Variables

The system uses environment variables for configuration. Create a `.env` file in the project root with the following variables:

```env
# Scraping Configuration
APPLE_DEEP_DOCS_PATH=/Users/km4/Dev/Qwen/appledeepdoc-mcp/run.sh

# Server Configuration
QWEN_SERVER_IP=192.168.50.101
QWEN_SERVER_PORT=5000
```

### Installation Instructions

1. **Install Cupertino** (if not already installed):
   ```bash
   # Installation varies by system
   # On macOS with Homebrew:
   brew install cupertino
   ```

2. **Install Apple Deep Docs MCP Server**:
   ```bash
   git clone https://github.com/Ahrentlov/appledeepdoc-mcp.git
   cd appledeepdoc-mcp
   python3 -m venv venv
   source venv/bin/activate
   pip install fastmcp
   ```

3. **Install Python dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

## Tool Integration

The scripts are configured to use multiple tools with fallback capabilities:

- If `apple-deep-docs` MCP server is available, it will be used preferentially
- If MCP server is not available, the scripts will automatically fall back to using `cupertino`
- Both tools can coexist and complement each other

## Framework Support

The system supports scraping documentation for any Apple framework:

- **Generic Framework Support**: Works with any Apple framework (Metal, UIKit, AVFoundation, CoreData, Photos, etc.)
- **Dynamic Class Patterns**: Automatically generates common class patterns for the specified framework
- **Comprehensive Search**: Searches for APIs, guides, samples, and resources

## Output Structure

The system generates structured output in `scraping/output/{framework}/`:

- `docs/` - API documentation and class references
- `guides/` - Programming guides and tutorials  
- `samples/` - Sample code and example projects
- `resources/` - WWDC videos, related frameworks, and tools
- `master_summary.json` - Aggregated summary of all modules

## Memory Ingestion

Scraped documentation is automatically ingested into the RAG database:

- All JSON output files are processed
- Content is sent to the configured server endpoint
- Framework-specific categorization is maintained
- Ready for AI retrieval and knowledge enhancement

## Running the Scraper

### Basic Usage

```bash
cd scraping
python main.py <framework_name>
```

Examples:
```bash
python main.py metal      # Metal framework
python main.py uikit      # UIKit framework
python main.py coredata   # CoreData framework
python main.py avfoundation # AVFoundation framework
python main.py photos     # Photos framework
```

### Standalone Ingestion

To run memory ingestion separately:

```bash
python ingest_scraped_data.py <framework_name>
```

### MCP Server Mode

To start the Apple Deep Docs MCP server and run scraping:

```bash
python start_scraping_with_server.py <framework_name>
```

## Current Status

The scripts are configured to work with the open-source tools. When both tools are available:
- `apple-deep-docs` MCP server will be used for deeper documentation access
- `cupertino` will be used as a fallback and for general searches
- Output files will contain actual data retrieved from Apple's public documentation

## Troubleshooting

### MCP Server Issues
- Ensure Xcode is installed for local documentation access
- Verify the MCP server path in environment variables
- Check that the server is properly configured for your AI tools

### Server Connection Issues
- Verify that the Qwen server is running at the configured IP and port
- Check firewall settings if connecting to remote server
- Ensure network connectivity to the server

### Scraping Problems
- Ensure Cupertino tool is installed and accessible
- Verify internet connectivity for web-based searches
- Check that the framework name is spelled correctly