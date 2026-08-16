"""Function-Calling tools for the deep-research knowledge base."""
from __future__ import annotations

from typing import Any

from app.services import knowledge_base
from app.tools.registry import tool


@tool(
    name="search_knowledge_base",
    description="Search the local knowledge base for documents relevant to a query. "
    "Returns matching snippets with scores; falls back to web search when local "
    "confidence is low.",
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query"},
            "top_k": {"type": "integer", "description": "Number of results to return"},
        },
        "required": ["query"],
    },
    category="research",
)
def search_knowledge_base(query: str, top_k: int = 5) -> dict[str, Any]:
    """Search the knowledge base."""
    return knowledge_base.search(query, top_k)


@tool(
    name="ingest_document",
    description="Import a document into the knowledge base. Accepts a file path "
    "(PDF, Markdown, TXT) or raw text, which is chunked and indexed.",
    parameters={
        "type": "object",
        "properties": {
            "source": {"type": "string", "description": "File path or raw text to ingest"},
        },
        "required": ["source"],
    },
    category="research",
)
def ingest_document(source: str) -> dict[str, Any]:
    """Ingest a document."""
    return knowledge_base.ingest(source)


@tool(
    name="list_documents",
    description="List all documents currently stored in the knowledge base.",
    parameters={"type": "object", "properties": {}},
    category="research",
)
def list_documents() -> dict[str, Any]:
    """List knowledge base documents."""
    return {"ok": True, "documents": knowledge_base.list_documents()}


@tool(
    name="save_to_knowledge_base",
    description="Save a titled piece of text (e.g. a report, note, or summary) into "
    "the knowledge base as a searchable document.",
    parameters={
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Document title"},
            "text": {"type": "string", "description": "Text content to save"},
        },
        "required": ["title", "text"],
    },
    category="research",
)
def save_to_knowledge_base(title: str, text: str) -> dict[str, Any]:
    """Save a titled document into the knowledge base."""
    return knowledge_base.ingest_text(title, text)


@tool(
    name="generate_research_report",
    description="Generate a structured Markdown research report on a topic using the "
    "local knowledge base and, when needed, web search.",
    parameters={
        "type": "object",
        "properties": {
            "topic": {"type": "string", "description": "The report topic"},
        },
        "required": ["topic"],
    },
    category="research",
)
async def generate_research_report(topic: str) -> dict[str, Any]:
    """Generate a research report."""
    return await knowledge_base.generate_report(topic, use_web=True)
