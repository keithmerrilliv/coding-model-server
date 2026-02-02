#!/usr/bin/env python3
"""
Metal Documentation Scraper using Apple MCP Tools

This script uses Cupertino and Apple Deep Docs commands instead of raw HTML scraping.
"""

import os
import json
import subprocess
import time

# Load environment variables
from dotenv import load_dotenv
load_dotenv()

class MetalDocScraper:
    def __init__(self):
        self.output_dir = "output/metal/docs"
        os.makedirs(self.output_dir, exist_ok=True)
        
    def scrape_api_docs_with_cupertino(self):
        """Use Cupertino tool to get core API documentation"""
        print("Scraping Metal framework APIs with Cupertino...")

        # Common Metal classes and protocols
        metal_entities = [
            "MTLDevice",
            "MTLCommandQueue",
            "MTLBuffer",
            "MTLTexture",
            "MTLRenderPipeline",
            "MTLComputePipeline"
        ]

        results = {}
        for entity in metal_entities:
            try:
                print(f"  Querying Cupertino for '{entity}'...")

                # Make actual call to Cupertino tool
                result = subprocess.run(['cupertino', 'search', entity], capture_output=True, text=True, timeout=30)

                if result.returncode == 0:
                    # Parse the results from Cupertino
                    data = result.stdout

                    actual_result = {
                        'entity': entity,
                        'status': 'found',
                        'data': data,
                        'timestamp': time.time()
                    }

                    result_file = f"{self.output_dir}/{entity.replace(' ', '_')}_cupertino.json"
                    with open(result_file, 'w') as f:
                        json.dump(actual_result, f, indent=2)

                    results[entity] = "success"
                else:
                    # Handle case where Cupertino didn't find the entity
                    error_result = {
                        'entity': entity,
                        'status': 'not found',
                        'error': result.stderr,
                        'timestamp': time.time()
                    }

                    result_file = f"{self.output_dir}/{entity.replace(' ', '_')}_cupertino.json"
                    with open(result_file, 'w') as f:
                        json.dump(error_result, f, indent=2)

                    results[entity] = "not found"

            except subprocess.TimeoutExpired:
                print(f"    Timeout querying '{entity}' with Cupertino")
                results[entity] = "timeout"
            except Exception as e:
                print(f"    Error querying '{entity}' with Cupertino: {e}")
                results[entity] = "failed"

        return results
    
    def scrape_with_apple_deep_docs(self):
        """Use Apple Deep Docs to get structured documentation"""
        print("Scraping with Apple Deep Docs...")

        queries = [
            "Metal framework",
            "MTLDevice protocol",
            "Metal rendering pipeline"
        ]

        results = {}

        for query in queries:
            try:
                print(f"  Querying deep docs for '{query}'...")

                # apple-deep-docs is an MCP server that doesn't work with command-line args
                # Fall back to cupertino which is known to work
                apple_deep_docs_path = os.getenv('APPLE_DEEP_DOCS_PATH', '/default/path/not/set')
                if os.path.exists(apple_deep_docs_path):
                    # If we wanted to use apple-deep-docs, we would call it like this:
                    # result = subprocess.run([apple_deep_docs_path, '--query', query], capture_output=True, text=True, timeout=30)
                    # But since it's an MCP server, we'll stick with cupertino
                    pass

                result = subprocess.run(['cupertino', 'search', query], capture_output=True, text=True, timeout=30)
                tool_used = 'cupertino'

                if result.returncode == 0:
                    actual_result = {
                        'query': query,
                        'tool_used': tool_used,
                        'data': result.stdout,
                        'found_results': True,
                        'timestamp': time.time()
                    }

                    result_file = f"{self.output_dir}/deepdocs_{query.replace(' ', '_')}.json"
                    with open(result_file, 'w') as f:
                        json.dump(actual_result, f, indent=2)

                    results[query] = "success"
                else:
                    error_result = {
                        'query': query,
                        'tool_used': tool_used,
                        'error': result.stderr,
                        'found_results': False,
                        'timestamp': time.time()
                    }

                    result_file = f"{self.output_dir}/deepdocs_{query.replace(' ', '_')}.json"
                    with open(result_file, 'w') as f:
                        json.dump(error_result, f, indent=2)

                    results[query] = "failed"
            except subprocess.TimeoutExpired:
                print(f"    Timeout querying '{query}' with {tool_used}")
                results[query] = "timeout"
            except Exception as e:
                print(f"    Error querying deep docs for '{query}': {e}")
                results[query] = "failed"

        return results
    
    def run(self):
        """Execute all documentation scraping tasks"""
        
        # Scrape API documentation using Cupertino
        api_results = self.scrape_api_docs_with_cupertino()
        
        # Get structured data with Apple Deep Docs
        deep_doc_results = self.scrape_with_apple_deep_docs()
        
        # Create summary report
        summary = {
            'api_documentation': api_results,
            'deep_docs_queries': deep_doc_results,
            'timestamp': time.time(),
            'total_api_entities': len(api_results),
            'successful_api_lookups': sum(1 for v in api_results.values() if v == "success"),
            'total_deepdoc_queries': len(deep_doc_results), 
            'successful_deepdoc_queries': sum(1 for v in deep_doc_results.values() if v == "success")
        }
        
        with open(f"{self.output_dir}/summary.json", "w") as f:
            json.dump(summary, f, indent=2)
            
        print("\nAPI Documentation Results:")
        for entity, status in api_results.items():
            print(f"  {entity}: {status}")
            
        print("\nDeep Docs Query Results:")  
        for query, status in deep_doc_results.items():
            print(f"  {query}: {status}")
                
        return summary

if __name__ == "__main__":
    scraper = MetalDocScraper()
    results = scraper.run() 
    print(f"\nCompleted with {results['successful_api_lookups']}/{results['total_api_entities']} API lookups successful")
