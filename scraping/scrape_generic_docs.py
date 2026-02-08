#!/usr/bin/env python3
"""
Generic Documentation Scraper using Apple MCP Tools

This script uses Cupertino and Apple Deep Docs commands to scrape documentation for any framework.
"""

import os
import json
import subprocess
import time

# Load environment variables
from dotenv import load_dotenv
load_dotenv()

# Import MCP client
try:
    import sys
    import os
    sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from mcp_client import call_mcp_tool, search_docs, search_apple_online
    MCP_AVAILABLE = True
except ImportError:
    MCP_AVAILABLE = False
    print("MCP client not available, will use Cupertino only")

class GenericDocScraper:
    def __init__(self, framework):
        self.framework = framework
        self.output_dir = f"output/{framework}/docs"
        os.makedirs(self.output_dir, exist_ok=True)

    def scrape_api_docs_with_cupertino(self):
        """Use Cupertino tool to get core API documentation"""
        print(f"Scraping {self.framework} framework APIs with Cupertino...")

        # Generate common class patterns for the framework
        framework_patterns = [
            f"{self.framework.title()}Device",
            f"{self.framework.title()}Manager",
            f"{self.framework.title()}Controller",
            f"{self.framework.title()}View",
            f"{self.framework.title()}Delegate",
            f"{self.framework.title()}Protocol",
            f"{self.framework.title()}Session",
            f"{self.framework.title()}Configuration",
            f"{self.framework.title()}Request",
            f"{self.framework.title()}Response",
            f"{self.framework.title()}Object",
            f"{self.framework.title()}Helper"
        ]

        results = {}
        for entity in framework_patterns:
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

        # Also search for the framework itself
        framework_query = self.framework
        try:
            print(f"  Querying Cupertino for framework '{framework_query}'...")

            result = subprocess.run(['cupertino', 'search', framework_query], capture_output=True, text=True, timeout=30)

            if result.returncode == 0:
                data = result.stdout

                actual_result = {
                    'entity': framework_query,
                    'status': 'found',
                    'data': data,
                    'timestamp': time.time()
                }

                result_file = f"{self.output_dir}/{framework_query.replace(' ', '_')}_cupertino.json"
                with open(result_file, 'w') as f:
                    json.dump(actual_result, f, indent=2)

                results[framework_query] = "success"
            else:
                error_result = {
                    'entity': framework_query,
                    'status': 'not found',
                    'error': result.stderr,
                    'timestamp': time.time()
                }

                result_file = f"{self.output_dir}/{framework_query.replace(' ', '_')}_cupertino.json"
                with open(result_file, 'w') as f:
                    json.dump(error_result, f, indent=2)

                results[framework_query] = "not found"
        except subprocess.TimeoutExpired:
            print(f"    Timeout querying framework '{framework_query}' with Cupertino")
            results[framework_query] = "timeout"
        except Exception as e:
            print(f"    Error querying framework '{framework_query}' with Cupertino: {e}")
            results[framework_query] = "failed"

        return results

    def _run_cupertino(self, query):
        """Run a cupertino search query and return (data, success) tuple."""
        result = subprocess.run(['cupertino', 'search', query], capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            return result.stdout, True
        return result.stderr, False

    def scrape_with_apple_deep_docs(self):
        """Use Apple Deep Docs / Cupertino to get structured documentation"""
        print(f"Scraping {self.framework} documentation with Apple Deep Docs...")

        queries = [
            f"{self.framework} framework",
            f"{self.framework.title()}Device protocol",
            f"{self.framework} rendering pipeline"
        ]

        results = {}

        for query in queries:
            tool_used = 'cupertino'
            try:
                print(f"  Querying deep docs for '{query}'...")

                actual_result = None

                if MCP_AVAILABLE:
                    try:
                        if "protocol" in query.lower():
                            mcp_result = call_mcp_tool("search_docs", query=query)
                        else:
                            mcp_result = search_apple_online(query)

                        if mcp_result:
                            tool_used = 'mcp-apple-deep-docs'
                            actual_result = {
                                'query': query,
                                'tool_used': tool_used,
                                'data': json.dumps(mcp_result),
                                'found_results': True,
                                'timestamp': time.time()
                            }
                    except Exception as e:
                        print(f"    MCP client error for '{query}': {e}, falling back to cupertino")

                if actual_result is None:
                    data, success = self._run_cupertino(query)
                    actual_result = {
                        'query': query,
                        'tool_used': tool_used,
                        'found_results': success,
                        'timestamp': time.time()
                    }
                    if success:
                        actual_result['data'] = data
                    else:
                        actual_result['error'] = data

                result_file = f"{self.output_dir}/deepdocs_{query.replace(' ', '_')}.json"
                with open(result_file, 'w') as f:
                    json.dump(actual_result, f, indent=2)

                results[query] = "success" if actual_result.get('found_results', False) else "failed"
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

        print(f"\n{self.framework} API Documentation Results:")
        for entity, status in api_results.items():
            print(f"  {entity}: {status}")

        print(f"\n{self.framework} Deep Docs Query Results:")
        for query, status in deep_doc_results.items():
            print(f"  {query}: {status}")

        return summary

if __name__ == "__main__":
    import sys
    framework = sys.argv[1] if len(sys.argv) > 1 else "metal"
    scraper = GenericDocScraper(framework)
    results = scraper.run()
    print(f"\nCompleted with {results['successful_api_lookups']}/{results['total_api_entities']} API lookups successful")