"""Web search for Agent 2/3 via the Tavily Search API (official `tavily-python` SDK), packaged in the mcp image.

TAVILY_API_KEY is given to the mcp service only. Queries leave the host: agents must not paste whole contracts.
"""
import os

from common.exceptions import IntegrationNotConfigured


class TavilySearch:
    def __init__(self, api_key=None, *, client=None):
        if client is None:
            key = (api_key or os.getenv("TAVILY_API_KEY", "")).strip()
            if not key:
                raise IntegrationNotConfigured("TODO: set TAVILY_API_KEY for web_search")
            from tavily import TavilyClient
            client = TavilyClient(api_key=key)
        self.client = client
        self.depth = os.getenv("TAVILY_SEARCH_DEPTH", "basic")  # basic = 1 credit, advanced = 2
        self.country = os.getenv("TAVILY_COUNTRY", "vietnam") or None  # boosts results from this country

    def search(self, query, max_results=5):
        if not isinstance(query, str) or not query.strip() or len(query) > 400:
            raise ValueError("query must be 1-400 characters")  # Tavily rejects queries over 400 characters
        response = self.client.search(query.strip(), search_depth=self.depth, topic="general", country=self.country,
                                      max_results=max(1, min(int(max_results), 20)))
        return [{"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("content", ""),
                 "score": r.get("score")} for r in response.get("results", [])]
