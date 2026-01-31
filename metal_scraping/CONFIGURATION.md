# Metal Documentation Scraper Configuration

## Required Setup for Open-Source Tools

The Metal documentation scraper scripts are designed to use open-source tools for accessing Metal documentation. To make these scripts work properly, you need to:

### 1. Open-Source Tools Access

The scripts expect the following open-source tools to be available in your environment:

- **Cupertino**: Open-source tool for accessing Apple documentation (https://github.com/mihaelamj/cupertino)
- **Apple Deep Docs**: Open-source documentation search tool (https://github.com/Ahrentlov/appledeepdoc-mcp)

### 2. Environment Configuration

#### Installation Instructions

1. **Install Cupertino**:
   ```bash
   git clone https://github.com/mihaelamj/cupertino.git
   cd cupertino
   pip install -r requirements.txt
   # Make sure the cupertino command is available in your PATH
   ```

2. **Install Apple Deep Docs**:
   ```bash
   git clone https://github.com/Ahrentlov/appledeepdoc-mcp.git
   cd appledeepdoc-mcp
   # Follow the setup instructions in the repository
   # Make sure the apple-deep-docs command is available in your PATH
   ```

3. **Install Python dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

### 3. Tool Integration

The scripts are already configured to use both tools with fallback capabilities:

- If `apple-deep-docs` is available, it will be used preferentially
- If `apple-deep-docs` is not available, the scripts will automatically fall back to using `cupertino`
- Both tools can coexist and complement each other

### 4. Running the Scripts

Once the tools are installed, you can run:

```bash
python main.py
```

Or run individual modules:
```bash
python scrape_metal_docs.py
python scrape_metal_guides.py
python scrape_metal_samples.py
python scrape_metal_resources.py
```

### 5. Current Status

The scripts are now configured to work with the open-source tools. When both tools are available:
- `apple-deep-docs` will be used for detailed documentation searches
- `cupertino` will be used as a fallback and for general searches
- Output files will contain actual data retrieved from Apple's public documentation

### 6. Troubleshooting

- If you get "command not found" errors, the tools are not installed or not in your PATH
- Run `python check_config.py` to verify tool availability
- Check that you have internet connectivity as these tools require online access
- If `apple-deep-docs` is not available, the scripts will still work using `cupertino` as fallback