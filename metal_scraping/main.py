#!/usr/bin/env python3
"""
Main Metal Documentation Scraper using Apple MCP Tools

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

def create_master_summary():
    """Create a master summary from individual module summaries"""
    
    # Collect all the summary files
    summary_files = [
        'output/metal/docs/summary.json',
        'output/metal/guides/guides_summary.json',
        'output/metal/samples/samples_summary.json',
        'output/metal/resources/resources_summary.json'
    ]
    
    master_data = {
        'timestamp': time.time(),
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
    
    with open('output/master_summary.json', 'w') as f:
        json.dump(master_data, f, indent=2)
        
    return master_data

def main():
    """Main execution function"""
    print("APPLE METAL DOCUMENTATION SCRAPER")
    print("Using MCP Tools (Cupertino, Apple Deep Docs)")
    
    # Check for framework argument
    framework_filter = None
    if len(sys.argv) > 1:
        framework_filter = sys.argv[1]
        print(f"Target Framework: {framework_filter}")
    
    print("="*50)
    
    # Create output directory
    os.makedirs('metal_scraping/output', exist_ok=True)
    
    # List of modules to run in order
    modules = [
        'scrape_metal_docs',
        'scrape_metal_guides', 
        'scrape_metal_samples',
        'scrape_metal_resources'
    ]
    
    success_count = 0
    
    for module in modules:
        if run_module(module):
            success_count += 1
        else:
            print(f"Failed to complete {module}")
            
    # Create master summary after all modules have run
    if success_count > 0:
        master_summary = create_master_summary()

        total_api_lookups = sum(
            mod.get('successful_api_lookups', 0) for mod in [
                master_summary['modules'].get('docs', {}),
                master_summary['modules'].get('guides', {}),
                master_summary['modules'].get('samples', {}),
                master_summary['modules'].get('resources', {})
            ]
        )
    
    print("\n" + "="*60)
    print("METAL DOCUMENTATION SCRAPING COMPLETED")
    print("="*60)  
    print(f"Successfully ran {success_count}/{len(modules)} modules")
        
    # Write completion file
    with open('SCRAPING_COMPLETED.txt', 'w') as f:
        f.write("Metal Documentation Scraping Completed Successfully\n")
        f.write("="*50 + "\n")
        f.write(f"Timestamp: {time.ctime()}\n")
        f.write(f"Modules executed: {success_count}/{len(modules)}\n")

if __name__ == "__main__":
    main()
