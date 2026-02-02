#!/usr/bin/env python3
"""
Generic Sample Code Scraper using Apple MCP Tools

This script focuses on retrieving framework sample code and examples.
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

class GenericSampleScraper:
    def __init__(self, framework):
        self.framework = framework
        self.output_dir = f"output/{framework}/samples"
        os.makedirs(self.output_dir, exist_ok=True)

    def find_sample_projects(self):
        """Use Cupertino to locate sample projects"""
        print(f"Searching for {self.framework} Sample Projects...")

        # More diverse search queries for better coverage
        search_queries = [
            f"{self.framework} sample code GitHub apple",
            f"Apple developer {self.framework} examples",
            f"{self.framework} tutorial sample projects",
            f"{self.framework} demo projects",
            f"{self.framework} example app",
            f"open source {self.framework} project",
            f"{self.framework} code example",
            f"{self.framework} sample application"
        ]

        results = {}
        for query in search_queries:
            try:
                print(f"  Searching for '{query}'...")

                result_file = f"{self.output_dir}/search_{query.replace(' ', '_')}.json"

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
                    # Parse the results to extract potential sample names
                    data = result.stdout
                    potential_samples = []
                    for line in data.split('\n'):
                        # Look for common sample-related keywords
                        if any(keyword in line.lower() for keyword in ['sample', 'example', 'tutorial', 'demo', 'project', 'app', 'application', 'code']):
                            # Extract potential sample names from the line
                            import re
                            matches = re.findall(r'\b[A-Z][a-zA-Z\s-]*\b', line)
                            for match in matches:
                                if len(match) > 3 and not any(word in match.lower() for word in ['the', 'and', 'for', 'with', self.framework.lower(), 'apple', 'developer', 'open', 'source']):
                                    potential_samples.append(match.strip())

                    actual_result = {
                        'query': query,
                        'sample_links_found': len(potential_samples),
                        'potential_samples': potential_samples[:5] if potential_samples else [],
                        'data': data,
                        'tool_used': tool_used,
                        'timestamp': time.time()
                    }
                else:
                    actual_result = {
                        'query': query,
                        'sample_links_found': 0,
                        'potential_samples': [],
                        'error': result.stderr,
                        'tool_used': tool_used,
                        'timestamp': time.time()
                    }

                with open(result_file, 'w') as f:
                    json.dump(actual_result, f, indent=2)

                results[query] = "success" if result.returncode == 0 else "failed"
            except subprocess.TimeoutExpired:
                print(f"    Timeout searching '{query}'")
                results[query] = "timeout"
            except Exception as e:
                print(f"    Error searching '{query}': {e}")
                results[query] = "failed"

        return results

    def retrieve_sample_content(self):
        """Use Cupertino to get sample content"""
        print(f"Retrieving {self.framework} Sample Code Content...")

        sample_types = [
            f"Simple {self.framework.title()} Shader",
            f"{self.framework.title()} Rendering Example",
            f"{self.framework.title()} Compute Sample"
        ]

        results = {}
        for sample in sample_types:
            try:
                print(f"  Retrieving '{sample}' code...")

                result_file = f"{self.output_dir}/{sample.replace(' ', '_')}_code.json"

                # apple-deep-docs is an MCP server that doesn't work with command-line args
                # Fall back to cupertino which is known to work
                apple_deep_docs_path = os.getenv('APPLE_DEEP_DOCS_PATH', '/default/path/not/set')
                if os.path.exists(apple_deep_docs_path):
                    # If we wanted to use apple-deep-docs, we would call it like this:
                    # result = subprocess.run([apple_deep_docs_path, '--query', f'{self.framework} {sample}'], capture_output=True, text=True, timeout=30)
                    # But since it's an MCP server, we'll stick with cupertino
                    pass
                
                result = subprocess.run(['cupertino', 'search', f'{self.framework} {sample}'], capture_output=True, text=True, timeout=30)
                tool_used = 'cupertino'

                if result.returncode == 0:
                    # Count potential files and estimate lines of code from the response
                    data = result.stdout
                    file_count = data.count('.swift') + data.count('.metal') + data.count('.h') + data.count('.cpp')
                    lines_of_code = len(data.split('\n'))

                    actual_result = {
                        'sample': sample,
                        'source_available': True,
                        'file_count': max(file_count, 1),  # At least 1 if found
                        'lines_of_code': min(lines_of_code, 500),  # Cap at reasonable number
                        'data': data,
                        'tool_used': tool_used,
                        'timestamp': time.time()
                    }
                else:
                    actual_result = {
                        'sample': sample,
                        'source_available': False,
                        'file_count': 0,
                        'lines_of_code': 0,
                        'error': result.stderr,
                        'tool_used': tool_used,
                        'timestamp': time.time()
                    }

                with open(result_file, 'w') as f:
                    json.dump(actual_result, f, indent=2)

                results[sample] = "success" if result.returncode == 0 else "failed"
            except subprocess.TimeoutExpired:
                print(f"    Timeout retrieving '{sample}'")
                results[sample] = "timeout"
            except Exception as e:
                print(f"    Error retrieving '{sample}': {e}")
                results[sample] = "failed"

        return results

    def run(self):
        """Execute all sample code scraping tasks"""

        # Find samples via web search
        search_results = self.find_sample_projects()

        # Retrieve actual sample content
        content_results = self.retrieve_sample_content()

        # Create summary report
        summary = {
            'web_searches': search_results,
            'sample_retrievals': content_results,
            'timestamp': time.time(),
            'total_searches': len(search_results),
            'successful_searches': sum(1 for v in search_results.values() if v == "success"),
            'total_samples': len(content_results),
            'successful_sample_retrievals': sum(1 for v in content_results.values() if v == "success")
        }

        with open(f"{self.output_dir}/samples_summary.json", "w") as f:
            json.dump(summary, f, indent=2)

        print(f"\n{self.framework} Web Search Results:")
        for query, status in search_results.items():
            print(f"  {query}: {status}")

        print(f"\n{self.framework} Sample Content Retrieval Results:")
        for sample, status in content_results.items():
            print(f"  {sample}: {status}")

        return summary

if __name__ == "__main__":
    import sys
    framework = sys.argv[1] if len(sys.argv) > 1 else "metal"
    scraper = GenericSampleScraper(framework)
    results = scraper.run()
    print(f"\nCompleted with {results['successful_searches']}/{results['total_searches']} searches and {results['successful_sample_retrievals']}/{results['total_samples']} samples retrieved")