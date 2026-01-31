#!/usr/bin/env python3
"""
Metal Documentation Scraper using Apple MCP Tools

This script uses Cupertino and Apple Deep Docs commands instead of raw HTML scraping.
"""

import os
import json
import subprocess
import time

class MetalDocScraper:
    def __init__(self):
        self.output_dir = "output/metal/docs"
        os.makedirs(self.output_dir, exist_ok=True)
        
    def scrape_api_docs_with_cupertino(self):
        """Use Cupertino tool to get core API documentation"""
        print("Scraping Metal framework APIs with Cupertino...")
        
        # Common Metal classes and protocols
        metal_entities = [
            "MTLDevice", "MTLCommandQueue", "MTLBuffer", "MTLTexture",
            "MTLRenderPipeline", "MTLComputePipeline", 
            "Metal framework documentation"
        ]
        
        results = {}
        for entity in metal_entities:
            try:
                # This would normally be executed via command line
                print(f"  Querying Cupertino for '{entity}'...")
                
                # Simulate the cupertino call - actual implementation depends on how client executes it
                result_file = f"{self.output_dir}/{entity.replace(' ', '_')}_cupertino.json"
                
                # Execute the actual cupertino command
                result = subprocess.run(['cupertino', entity], capture_output=True, text=True)
                if result.returncode == 0:
                    mock_result = {
                        'entity': entity,
                        'status': 'found',
                        'data': result.stdout,
                        'timestamp': time.time()
                    }
                else:
                    mock_result = {
                        'entity': entity,
                        'status': 'error',
                        'error': result.stderr,
                        'timestamp': time.time()
                    }
                
                with open(result_file, 'w') as f:
                    json.dump(mock_result, f, indent=2)
                    
                results[entity] = "success"
            except Exception as e:
                print(f"    Error querying '{entity}': {e}")
                results[entity] = "failed"
        
        return results
    
    def scrape_with_apple_deep_docs(self):
        """Use Apple Deep Docs to get structured documentation"""
        print("Scraping with Apple Deep Docs...")
        
        queries = [
            {"tool": "search_apple_online", "arguments": {"query": "Metal framework"}},
            {"tool": "search_apple_online", "arguments": {"query": "MTLDevice protocol"}}, 
            {"tool": "search_apple_online", "arguments": {"query": "Metal rendering pipeline"}}
        ]
        
        results = {}
        
        for query in queries:
            try:
                # This simulates calling the Apple Deep Docs tool
                print(f"  Querying deep docs for '{query['arguments']['query']}'...")
                
                result_file = f"{self.output_dir}/deepdocs_{query['arguments']['query'].replace(' ', '_')}.json"
                
                # First, extract the actual query from the arguments
                search_query = query['arguments']['query']

                # Try to use apple-deep-docs if available, otherwise fall back to cupertino
                try:
                    result = subprocess.run(['apple-deep-docs', '--query', search_query], capture_output=True, text=True, timeout=30)
                    tool_used = 'apple-deep-docs'
                except FileNotFoundError:
                    # apple-deep-docs not available, fall back to cupertino
                    result = subprocess.run(['cupertino', 'search', search_query], capture_output=True, text=True)
                    tool_used = 'cupertino'

                if result.returncode == 0:
                    mock_result = {
                        'query': query,
                        'found_results': True,
                        'data': result.stdout,
                        'tool_used': tool_used,
                        'timestamp': time.time()
                    }
                else:
                    mock_result = {
                        'query': query,
                        'found_results': False,
                        'error': result.stderr,
                        'tool_used': tool_used,
                        'timestamp': time.time()
                    }
                
                with open(result_file, 'w') as f:
                    json.dump(mock_result, f, indent=2)
                    
                results[query['arguments']['query']] = "success"
            except Exception as e:
                print(f"    Error querying deep docs: {e}")
                results[query['arguments']['query']] = "failed"
                
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
