"""Deep-research knowledge base endpoints."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services import knowledge_base

router = APIRouter(prefix="/research", tags=["research"])


class IngestRequest(BaseModel):
    source: str


class ReportRequest(BaseModel):
    topic: str
    use_web: bool = True


@router.post("/ingest")
def ingest(req: IngestRequest) -> dict[str, Any]:
    """Ingest a file path or raw text into the knowledge base."""
    result = knowledge_base.ingest(req.source)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "ingest failed"))
    return result


@router.get("/search")
def search(query: str, top_k: int = 5) -> dict[str, Any]:
    """Search the knowledge base; falls back to web search on low confidence."""
    return knowledge_base.search(query, top_k)


@router.get("/documents")
def list_documents() -> list[dict[str, Any]]:
    """List all ingested documents."""
    return knowledge_base.list_documents()


@router.delete("/documents/{doc_id}")
def delete_document(doc_id: int) -> dict[str, Any]:
    """Delete a document from the knowledge base."""
    result = knowledge_base.delete_document(doc_id)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("error", "document not found"))
    return result


@router.post("/report")
async def generate_report(req: ReportRequest) -> dict[str, Any]:
    """Generate a Markdown research report from local and (optionally) web sources."""
    return await knowledge_base.generate_report(req.topic, req.use_web)
