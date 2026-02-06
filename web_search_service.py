import importlib.util

# Validate that ddgs is available
if importlib.util.find_spec("ddgs") is None:
    raise ImportError("ddgs package is not installed. Please install it with: pip install duckduckgo_search")

import logging
from typing import List, Dict, Any, Optional
from ddgs import DDGS

logger = logging.getLogger(__name__)

class WebSearchService:
    def __init__(self):
        # No longer store a persistent DDGS instance - create fresh one per search
        pass

    def search(self, query: str, max_results: int = 5) -> str:
        """
        Perform a web search using DuckDuckGo and return a formatted string of results.
        """
        if not query or not query.strip():
            return "Error: Empty search query."

        try:
            logger.info(f"Searching web for: {query}")
            ddgs = DDGS()
            results = ddgs.text(query, max_results=max_results)
            
            if not results:
                return f"No results found for query: {query}"

            formatted_results = f"## WEB SEARCH RESULTS FOR: '{query}'\n\n"
            for i, res in enumerate(results, 1):
                title = res.get('title', 'No Title')
                link = res.get('href', '#')
                body = res.get('body', 'No description available.')
                formatted_results += f"### {i}. {title}\n**Source:** {link}\n**Snippet:** {body}\n\n"
            
            return formatted_results

        except Exception as e:
            logger.error(f"Web search failed: {e}")
            return f"Error performing web search: {str(e)}"