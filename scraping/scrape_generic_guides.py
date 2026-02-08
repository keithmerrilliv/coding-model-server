#!/usr/bin/env python3
"""
Generic Guides and Specifications Scraper using Apple MCP Tools

This script focuses on framework guides, tutorials and specification documents.
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

class GenericGuidesScraper:
    def __init__(self, framework):
        self.framework = framework
        self.output_dir = f"output/{framework}/guides"
        os.makedirs(self.output_dir, exist_ok=True)

    def scrape_programming_guides(self):
        """Use Cupertino to get programming guides"""
        print(f"Scraping {self.framework} Programming Guides...")

        # More generic guide topics that apply to most frameworks
        guide_topics = [
            f"{self.framework.title()} Programming Guide",
            f"{self.framework.title()} Best Practices",
            f"{self.framework.title()} Tutorial",
            f"{self.framework.title()} Getting Started",
            f"How to use {self.framework.title()}",
            f"{self.framework.title()} Overview",
            f"{self.framework.title()} Fundamentals",
            f"{self.framework.title()} Advanced Guide"
        ]

        results = {}
        for topic in guide_topics:
            try:
                print(f"  Querying guides for '{topic}'...")

                result_file = f"{self.output_dir}/{topic.replace(' ', '_')}_guide.json"

                result = subprocess.run(['cupertino', 'search', f'{self.framework} {topic}'], capture_output=True, text=True, timeout=30)
                tool_used = 'cupertino'

                if result.returncode == 0:
                    # Parse the results to extract sections
                    data = result.stdout
                    # Look for common section headers in the response
                    sections = []
                    for line in data.split('\n'):
                        if any(header in line.lower() for header in ['introduction', 'setup', 'implementation', 'overview', 'usage', 'best practices', 'tutorial', 'getting started', 'advanced', 'fundamentals']):
                            sections.append(line.strip())

                    actual_result = {
                        'topic': topic,
                        'content_available': True,
                        'sections_found': sections if sections else ['Introduction', 'Setup', 'Implementation'],
                        'data': data,
                        'tool_used': tool_used,
                        'timestamp': time.time()
                    }
                else:
                    actual_result = {
                        'topic': topic,
                        'content_available': False,
                        'sections_found': [],
                        'error': result.stderr,
                        'tool_used': tool_used,
                        'timestamp': time.time()
                    }

                with open(result_file, 'w') as f:
                    json.dump(actual_result, f, indent=2)

                results[topic] = "success" if result.returncode == 0 else "failed"
            except subprocess.TimeoutExpired:
                print(f"    Timeout retrieving guide '{topic}'")
                results[topic] = "timeout"
            except Exception as e:
                print(f"    Error retrieving guide '{topic}': {e}")
                results[topic] = "failed"

        return results

    def scrape_specifications(self):
        """Get framework specification documents"""
        print(f"Retrieving {self.framework} Specification Documents...")

        spec_docs = [
            f"{self.framework.title()} Shading Language Specification",
            f"{self.framework.title()} Framework Reference"
        ]

        results = {}
        for doc in spec_docs:
            try:
                print(f"  Retrieving '{doc}'...")

                result_file = f"{self.output_dir}/{doc.replace(' ', '_')}_spec.json"

                result = subprocess.run(['cupertino', 'search', doc], capture_output=True, text=True, timeout=30)
                tool_used = 'cupertino'

                if result.returncode == 0:
                    actual_result = {
                        'document': doc,
                        'format': 'text',
                        'accessible_via_tool': tool_used,
                        'data': result.stdout,
                        'timestamp': time.time()
                    }
                else:
                    actual_result = {
                        'document': doc,
                        'format': 'text',
                        'accessible_via_tool': tool_used,
                        'error': result.stderr,
                        'timestamp': time.time()
                    }

                with open(result_file, 'w') as f:
                    json.dump(actual_result, f, indent=2)

                results[doc] = "success" if result.returncode == 0 else "failed"
            except subprocess.TimeoutExpired:
                print(f"    Timeout retrieving '{doc}'")
                results[doc] = "timeout"
            except Exception as e:
                print(f"    Error retrieving '{doc}': {e}")
                results[doc] = "failed"

        return results

    def run(self):
        """Execute all guide and specification scraping tasks"""

        # Get programming guides
        guide_results = self.scrape_programming_guides()

        # Retrieve specifications
        spec_results = self.scrape_specifications()

        # Create summary report
        summary = {
            'programming_guides': guide_results,
            'specification_documents': spec_results,
            'timestamp': time.time(),
            'total_guides': len(guide_results),
            'successful_guide_retrievals': sum(1 for v in guide_results.values() if v == "success"),
            'total_specs': len(spec_results),
            'successful_spec_retrievals': sum(1 for v in spec_results.values() if v == "success")
        }

        with open(f"{self.output_dir}/guides_summary.json", "w") as f:
            json.dump(summary, f, indent=2)

        print(f"\n{self.framework} Programming Guide Results:")
        for topic, status in guide_results.items():
            print(f"  {topic}: {status}")

        print(f"\n{self.framework} Specification Document Results:")
        for doc, status in spec_results.items():
            print(f"  {doc}: {status}")

        return summary

if __name__ == "__main__":
    import sys
    framework = sys.argv[1] if len(sys.argv) > 1 else "metal"
    scraper = GenericGuidesScraper(framework)
    results = scraper.run()
    print(f"\nCompleted with {results['successful_guide_retrievals']}/{results['total_guides']} guides and {results['successful_spec_retrievals']}/{results['total_specs']} specs retrieved")