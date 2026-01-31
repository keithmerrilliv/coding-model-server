# Metal Documentation Scraper

This project contains a suite of tools for scraping Apple's Metal framework documentation using MCP tools (Cupertino and Apple Deep Docs).

## Project Structure

- `main.py` - Main orchestrator script that runs all scrapers
- `scrape_metal_docs.py` - Scrapes Metal API documentation
- `scrape_metal_guides.py` - Retrieves Metal programming guides
- `scrape_metal_samples.py` - Finds Metal sample code and examples
- `scrape_metal_resources.py` - Gathers additional resources like WWDC videos
- `scrape_metal_docs_updated.py` - Updated version of the docs scraper
- `check_config.py` - Checks environment configuration
- `CONFIGURATION.md` - Detailed setup instructions
- `output/` - Directory for scraped documentation output

## Prerequisites

To run these scripts successfully, you need to install the following open-source tools:

1. **Cupertino** - Open-source tool for Apple documentation access ([GitHub](https://github.com/mihaelamj/cupertino))
2. **Apple Deep Docs** - Open-source documentation search tool ([GitHub](https://github.com/Ahrentlov/appledeepdoc-mcp))

These tools provide access to Apple's public documentation without requiring internal Apple access.

## Configuration

1. First, install the required tools:

   **Cupertino:**
   ```bash
   # Follow installation instructions from https://github.com/mihaelamj/cupertino
   # Typically involves cloning the repo and installing dependencies
   git clone https://github.com/mihaelamj/cupertino.git
   cd cupertino
   pip install -r requirements.txt
   # Make sure the cupertino command is available in your PATH
   ```

   **Apple Deep Docs:**
   ```bash
   # Follow installation instructions from https://github.com/Ahrentlov/appledeepdoc-mcp
   git clone https://github.com/Ahrentlov/appledeepdoc-mcp.git
   cd appledeepdoc-mcp
   # Follow the setup instructions in the repository
   # Make sure the apple-deep-docs command is available in your PATH
   ```

2. Check if your environment has the required tools:
   ```bash
   python check_config.py
   ```

3. Install required Python dependencies:
   ```bash
   pip install -r requirements.txt
   ```

## Running the Scraper

### Individual Modules
Run individual scraping modules:
```bash
python scrape_metal_docs.py
python scrape_metal_guides.py
python scrape_metal_samples.py
python scrape_metal_resources.py
```

### Full Suite
Run all scrapers using the main orchestrator:
```bash
python main.py
```

## Output

The scraped documentation will be saved in the `output/metal/` directory:
- `docs/` - Metal API documentation
- `guides/` - Programming guides and tutorials
- `samples/` - Sample code and examples
- `resources/` - Additional resources like WWDC videos

A master summary will be created at `output/master_summary.json`.

## Important Notes

- The scripts currently contain placeholder code for MCP tools since they simulate API calls
- When MCP tools are available, replace the placeholder sections with actual tool calls
- The output directory is ignored by Git (see `.gitignore`) to avoid committing large amounts of scraped data
- These tools are intended for internal Apple use with proper authorization to access internal documentation

## Troubleshooting

If the scripts don't work as expected:

1. Run `python check_config.py` to verify tool availability
2. Check that both `cupertino` and `apple-deep-docs` commands are accessible in your PATH
3. If `apple-deep-docs` is not available, the scripts will automatically fall back to using `cupertino` for all searches
4. Review the installation instructions above to ensure both tools are properly installed
5. Check that you have internet connectivity as these tools require online access to Apple's documentation