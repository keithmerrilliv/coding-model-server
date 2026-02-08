#!/usr/bin/env python3
"""
Apple Documentation Scraper — All Frameworks

Discovers every public framework from installed Xcode SDKs (macOS, iOS, visionOS)
and scrapes docs, guides, samples, and resources for each one, then ingests the
results into the RAG database.

Usage:
    python3 scrape_all_apple_frameworks.py              # scrape all discovered frameworks
    python3 scrape_all_apple_frameworks.py metal vision  # scrape only these two
"""

import os
import sys
import subprocess
import json
import time
from pathlib import Path


def discover_frameworks():
    """Scan installed Xcode SDKs for all public framework names (deduplicated)."""
    apps_dir = Path("/Applications")
    frameworks = set()

    platforms = [
        "MacOSX.platform",
        "iPhoneOS.platform",
        "XROS.platform",
        "XRSimulator.platform",
    ]

    for xcode_app in apps_dir.glob("Xcode*.app"):
        for platform in platforms:
            sdks_dir = xcode_app / f"Contents/Developer/Platforms/{platform}/Developer/SDKs"
            if not sdks_dir.exists():
                continue
            for sdk in sdks_dir.glob("*.sdk"):
                fw_dir = sdk / "System/Library/Frameworks"
                if not fw_dir.exists():
                    continue
                for fw_bundle in fw_dir.glob("*.framework"):
                    name = fw_bundle.stem
                    # Skip underscore-prefixed internal integration modules
                    if name.startswith("_"):
                        continue
                    frameworks.add(name)

    return sorted(frameworks, key=str.lower)


def scrape_framework(framework):
    """Run all four scraper modules for a single framework. Returns modules succeeded (0-4)."""
    from scrape_generic_docs import GenericDocScraper
    from scrape_generic_guides import GenericGuidesScraper
    from scrape_generic_samples import GenericSampleScraper
    from scrape_generic_resources import GenericResourcesScraper

    os.makedirs(f'output/{framework}', exist_ok=True)

    scrapers = [
        ("docs",      GenericDocScraper),
        ("guides",    GenericGuidesScraper),
        ("samples",   GenericSampleScraper),
        ("resources", GenericResourcesScraper),
    ]

    success_count = 0
    for name, cls in scrapers:
        try:
            print(f"\n{'='*60}")
            print(f"  [{framework}] Running {name.upper()}")
            print('='*60)
            cls(framework).run()
            print(f"  [{framework}] {name} completed")
            success_count += 1
        except Exception as e:
            print(f"  [{framework}] {name} failed: {e}")
            import traceback
            traceback.print_exc()

    if success_count > 0:
        create_master_summary(framework)
        print(f"\n{'='*60}")
        print(f"  [{framework}] RUNNING MEMORY INGESTION")
        print('='*60)
        subprocess.run([sys.executable, 'ingest_scraped_data.py', framework])

    return success_count


def create_master_summary(framework):
    """Create a master summary from individual module summaries."""
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
                module_name = summary_file.split('/')[-2]
                master_data['modules'][module_name] = data
        except Exception as e:
            print(f"Could not read {summary_file}: {e}")

    with open(f'output/{framework}/master_summary.json', 'w') as f:
        json.dump(master_data, f, indent=2)

    return master_data


def main():
    """Scrape all frameworks (or a subset from the command line)."""
    print("APPLE DOCUMENTATION SCRAPER — ALL FRAMEWORKS")
    print("Using MCP Tools (Cupertino, Apple Deep Docs)")
    print("="*60)

    if len(sys.argv) > 1:
        frameworks = [f for f in sys.argv[1:]]
        print(f"Target frameworks: {', '.join(frameworks)}")
    else:
        frameworks = discover_frameworks()
        if not frameworks:
            print("ERROR: No Xcode installation found in /Applications.")
            print("Install Xcode or pass framework names on the command line.")
            sys.exit(1)
        print(f"Discovered {len(frameworks)} frameworks from Xcode SDKs:")
        # Print in columns
        col_width = max(len(f) for f in frameworks) + 2
        cols = 80 // col_width
        for i in range(0, len(frameworks), cols):
            row = frameworks[i:i+cols]
            print("  " + "".join(f.ljust(col_width) for f in row))

    print("="*60)

    total_start = time.time()
    results = {}

    for i, framework in enumerate(frameworks, 1):
        print(f"\n{'#'*60}")
        print(f"# [{i}/{len(frameworks)}] {framework}")
        print(f"{'#'*60}")

        fw_start = time.time()
        succeeded = scrape_framework(framework)
        elapsed = time.time() - fw_start

        results[framework] = succeeded
        print(f"\n  {framework}: {succeeded}/4 modules in {elapsed:.0f}s")

    total_elapsed = time.time() - total_start
    total_min = int(total_elapsed) // 60
    total_sec = int(total_elapsed) % 60

    fw_width = max(len(f) for f in results) + 2 if results else 20

    print(f"\n{'='*60}")
    print("SCRAPING COMPLETE")
    print(f"{'='*60}")
    print(f"  Total time: {total_min}m {total_sec}s")
    print(f"  Frameworks: {len(results)}")
    ok = sum(1 for c in results.values() if c > 0)
    print(f"  Succeeded:  {ok}")
    if ok < len(results):
        print(f"  Failed:     {len(results) - ok}")
    print()
    for fw, count in results.items():
        status = "ok" if count > 0 else "FAILED"
        print(f"  {fw:{fw_width}s} {count}/4 modules  [{status}]")
    print()


if __name__ == "__main__":
    main()
