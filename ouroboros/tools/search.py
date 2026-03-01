"""Web search tool — OpenAI Responses API with DuckDuckGo fallback."""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List

from ouroboros.tools.registry import ToolContext, ToolEntry


def _search_openai(query: str) -> str | None:
    """Try OpenAI Responses API search. Returns JSON string or None on failure."""
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        return None
    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key)
        resp = client.responses.create(
            model=os.environ.get("OUROBOROS_WEBSEARCH_MODEL", "gpt-5"),
            tools=[{"type": "web_search"}],
            tool_choice="auto",
            input=query,
        )
        d = resp.model_dump()
        text = ""
        for item in d.get("output", []) or []:
            if item.get("type") == "message":
                for block in item.get("content", []) or []:
                    if block.get("type") in ("output_text", "text"):
                        text += block.get("text", "")
        if text:
            return json.dumps({"answer": text, "source": "openai"}, ensure_ascii=False, indent=2)
        return None
    except Exception:
        return None


def _search_ddg(query: str) -> str:
    """DuckDuckGo fallback search. Always returns a JSON string."""
    try:
        from ddgs import DDGS
        results = list(DDGS().text(query, max_results=8))
        if not results:
            return json.dumps({"error": "No results from DuckDuckGo."}, ensure_ascii=False)
        # Format into a readable answer string + sources list
        answer_parts = []
        sources = []
        for r in results:
            title = r.get("title", "")
            body = r.get("body", "")
            href = r.get("href", "")
            answer_parts.append(f"**{title}**\n{body}")
            sources.append({"title": title, "url": href})
        answer = "\n\n".join(answer_parts)
        return json.dumps(
            {"answer": answer, "sources": sources, "source": "duckduckgo"},
            ensure_ascii=False,
            indent=2,
        )
    except ImportError:
        return json.dumps({"error": "ddgs package not installed; run: pip install ddgs"}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": f"DuckDuckGo search failed: {repr(e)}"}, ensure_ascii=False)


def _web_search(ctx: ToolContext, query: str) -> str:
    # Try OpenAI first (richer answers, cited sources)
    result = _search_openai(query)
    if result is not None:
        return result
    # Fallback: DuckDuckGo (no API key needed)
    return _search_ddg(query)


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry("web_search", {
            "name": "web_search",
            "description": "Search the web via OpenAI Responses API. Returns JSON with answer + sources.",
            "parameters": {"type": "object", "properties": {
                "query": {"type": "string"},
            }, "required": ["query"]},
        }, _web_search),
    ]
