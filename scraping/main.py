#!/usr/bin/env python3
"""
Main Apple Documentation Scraper using Apple MCP Tools

This script orchestrates all the individual scraping modules.
"""

import os
import sys
import subprocess
import json
import time

def run_module(module_name):
    """Run a scraper module and return results"""
    print(f"\n{'='*60}")
    print(f"Running {module_name.upper()}")
    print('='*60)

    try:
        # Run the script in the current directory
        result = subprocess.run([
            sys.executable,
            f'{module_name}.py'
        ], capture_output=True, text=True, timeout=120)

        if result.returncode == 0:
            print("✓ Completed successfully")
            return True
        else:
            print(f"✗ Failed with exit code {result.returncode}")
            if result.stderr:
                print(f"STDERR: {result.stderr[:200]}...")
            return False

    except subprocess.TimeoutExpired:
        print("✗ Timeout expired")
        return False
    except Exception as e:
        print(f"✗ Error running module: {e}")
        return False

def create_master_summary(framework):
    """Create a master summary from individual module summaries"""

    # Collect all the summary files
    summary_files = [
        f'output/{framework}/docs/summary.json',
        f'output/{framework}/guides/guides_summary.json',
        f'output/{framework}/samples/samples_summary.json',
        f'output/{framework}/resources/resources_summary.json'
    ]

    master_data = {
        'timestamp': time.time(),
        'framework': framework,
        'modules': {}
    }

    for summary_file in summary_files:
        try:
            if os.path.exists(summary_file):
                with open(summary_file, 'r') as f:
                    data = json.load(f)

                # Extract just the filename without path and extension
                module_name = summary_file.split('/')[-2]  # Gets docs, guides, etc.
                master_data['modules'][module_name] = data

        except Exception as e:
            print(f"Could not read {summary_file}: {e}")

    with open(f'output/{framework}/master_summary.json', 'w') as f:
        json.dump(master_data, f, indent=2)

    return master_data

def main():
    """Main execution function"""
    print("APPLE DOCUMENTATION SCRAPER")
    print("Using MCP Tools (Cupertino, Apple Deep Docs)")

    # Check for framework argument - default to metal for backward compatibility
    framework_filter = "metal"
    if len(sys.argv) > 1:
        framework_filter = sys.argv[1].lower()
        print(f"Target Framework: {framework_filter}")

    print("="*50)

    # Create output directory
    os.makedirs(f'output/{framework_filter}', exist_ok=True)

    # Import the generic scraper classes and run them directly
    from scrape_generic_docs import GenericDocScraper
    from scrape_generic_guides import GenericGuidesScraper
    from scrape_generic_samples import GenericSampleScraper
    from scrape_generic_resources import GenericResourcesScraper

    success_count = 0

    # Run docs scraper
    try:
        print(f"\n{'='*60}")
        print("Running SCRAPE_GENERIC_DOCS")
        print('='*60)
        docs_scraper = GenericDocScraper(framework_filter)
        docs_results = docs_scraper.run()
        print("✓ Completed successfully")
        success_count += 1
    except Exception as e:
        print(f"✗ Failed with error: {e}")
        import traceback
        traceback.print_exc()

    # Run guides scraper
    try:
        print(f"\n{'='*60}")
        print("Running SCRAPE_GENERIC_GUIDES")
        print('='*60)
        guides_scraper = GenericGuidesScraper(framework_filter)
        guides_results = guides_scraper.run()
        print("✓ Completed successfully")
        success_count += 1
    except Exception as e:
        print(f"✗ Failed with error: {e}")
        import traceback
        traceback.print_exc()

    # Run samples scraper
    try:
        print(f"\n{'='*60}")
        print("Running SCRAPE_GENERIC_SAMPLES")
        print('='*60)
        samples_scraper = GenericSampleScraper(framework_filter)
        samples_results = samples_scraper.run()
        print("✓ Completed successfully")
        success_count += 1
    except Exception as e:
        print(f"✗ Failed with error: {e}")
        import traceback
        traceback.print_exc()

    # Run resources scraper
    try:
        print(f"\n{'='*60}")
        print("Running SCRAPE_GENERIC_RESOURCES")
        print('='*60)
        resources_scraper = GenericResourcesScraper(framework_filter)
        resources_results = resources_scraper.run()
        print("✓ Completed successfully")
        success_count += 1
    except Exception as e:
        print(f"✗ Failed with error: {e}")
        import traceback
        traceback.print_exc()

    # Create master summary after all modules have run
    if success_count > 0:
        master_summary = create_master_summary(framework_filter)

        total_api_lookups = sum(
            mod.get('successful_api_lookups', 0) for mod in [
                master_summary['modules'].get('docs', {}),
                master_summary['modules'].get('guides', {}),
                master_summary['modules'].get('samples', {}),
                master_summary['modules'].get('resources', {})
            ]
        )

        # Run ingestion step
        print("\n" + "="*60)
        print("RUNNING MEMORY INGESTION")
        print('='*60)
        subprocess.run([sys.executable, 'ingest_scraped_data.py'])

    print("\n" + "="*60)
    print(f"{framework_filter.upper()} DOCUMENTATION SCRAPING COMPLETED")
    print("="*60)
    print(f"Successfully ran {success_count}/4 modules")

    # Write completion file
    with open(f'SCRAPING_COMPLETED_{framework_filter.upper()}.txt', 'w') as f:
        f.write(f"{framework_filter.capitalize()} Documentation Scraping Completed Successfully\n")
        f.write("="*50 + "\n")
        f.write(f"Timestamp: {time.ctime()}\n")
        f.write(f"Modules executed: {success_count}/4\n")

if __name__ == "__main__":
    main()