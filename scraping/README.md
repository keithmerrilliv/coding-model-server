# Apple Documentation Scraper

A comprehensive system for scraping Apple framework documentation using multiple tools and techniques.

## Features

- **Generic Framework Support**: Works with any Apple framework (Metal, UIKit, AVFoundation, CoreData, Photos, etc.)
- **Dual Tool Integration**: Uses both Cupertino and Apple Deep Docs MCP server for comprehensive documentation access
- **Enhanced Web Scraping**: Improved search patterns for better documentation discovery
- **Memory Ingestion**: Automatically sends scraped data to RAG database for AI knowledge enhancement
- **Environment Configuration**: Flexible configuration via environment variables

## Architecture

```
scraping/
├── main.py                    # Main orchestrator
├── scrape_generic_docs.py     # API documentation scraper
├── scrape_generic_guides.py   # Programming guides scraper
├── scrape_generic_samples.py  # Sample code scraper
├── scrape_generic_resources.py # Additional resources scraper
├── ingest_scraped_data.py    # Memory ingestion module
├── mcp_client.py             # MCP protocol client
├── start_scraping_with_server.py # Server startup script
├── output/                   # Generated output (gitignored)
│   └── {framework}/          # Framework-specific output
│       ├── docs/             # API documentation
│       ├── guides/           # Programming guides
│       ├── samples/          # Sample code
│       └── resources/        # Additional resources
└── ...
```

## Prerequisites

- Python 3.10+
- Cupertino tool installed
- Xcode (for local documentation access with Apple Deep Docs)

## Installation

1. Clone the repository
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Install Cupertino tool if not already installed
4. Set up environment variables in `.env` file

## Configuration

Create a `.env` file in the root directory with the following variables:

```env
# Scraping Configuration
APPLE_DEEP_DOCS_PATH=/path/to/appledeepdoc-mcp/run.sh

# Server Configuration
CODING_MODEL_SERVER_IP=192.0.2.10
CODING_MODEL_SERVER_PORT=5000
```

## Usage

### Basic Scraping

To scrape documentation for a specific framework:

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

## Scraping Modules

### 1. API Documentation Scraper (`scrape_generic_docs.py`)
- Searches for framework-specific classes and protocols
- Uses Cupertino for public documentation
- Integrates with Apple Deep Docs MCP server when available
- Generates comprehensive API documentation

### 2. Guides Scraper (`scrape_generic_guides.py`)
- Retrieves programming guides and tutorials
- Finds best practices and fundamentals
- Locates framework references and specifications

### 3. Samples Scraper (`scrape_generic_samples.py`)
- Searches for sample projects on GitHub
- Finds example applications and code snippets
- Discovers tutorial projects and demos

### 4. Resources Scraper (`scrape_generic_resources.py`)
- Locates WWDC videos and sessions
- Identifies related frameworks and integrations
- Retrieves tool documentation and profiling guides

## MCP Server Integration

The system integrates with the Apple Deep Docs MCP server for access to:

- Hidden Xcode documentation
- Apple Developer API documentation
- Swift Evolution Proposals
- Swift Open Source Repositories
- WWDC Session Notes
- Human Interface Guidelines

When the MCP server is not available, the system gracefully falls back to Cupertino-based scraping.

## Memory Ingestion

Scraped documentation is automatically ingested into the RAG database:

- All JSON output files are processed
- Content is sent to the configured server endpoint
- Framework-specific categorization is maintained
- Ready for AI retrieval and knowledge enhancement

## Output Structure

The system generates structured output in `scraping/output/{framework}/`:

- `docs/` - API documentation and class references
- `guides/` - Programming guides and tutorials
- `samples/` - Sample code and example projects
- `resources/` - WWDC videos, related frameworks, and tools
- `master_summary.json` - Aggregated summary of all modules

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `APPLE_DEEP_DOCS_PATH` | Path to Apple Deep Docs MCP server | - |
| `CODING_MODEL_SERVER_IP` | IP address of Coding Model server | 127.0.0.1 |
| `CODING_MODEL_SERVER_PORT` | Port of Coding Model server | 5000 |

## Troubleshooting

### Server Connection Issues
- Verify that the Coding Model server is running at the configured IP and port
- Check firewall settings if connecting to remote server
- Ensure network connectivity to the server

### MCP Server Issues
- Confirm Xcode is installed for local documentation access
- Verify the MCP server path in environment variables
- Check that the server is properly configured for your AI tools

### Scraping Problems
- Ensure Cupertino tool is installed and accessible
- Verify internet connectivity for web-based searches
- Check that the framework name is spelled correctly

## Development

### Adding New Scraping Modules
1. Create a new scraper class following the generic pattern
2. Implement the required interface methods
3. Add to the main orchestrator in `main.py`

### Extending Search Patterns
- Modify search queries in each scraper module
- Add new framework-specific patterns
- Enhance result parsing for better extraction

## License

MIT