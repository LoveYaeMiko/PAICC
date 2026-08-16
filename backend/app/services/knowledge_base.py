"""Deep-research knowledge base service.

Local-first retrieval augmented generation (RAG):

* Documents (PDF / Markdown / TXT / raw text) are chunked and stored in a local
  Chroma vector collection when available, otherwise a keyword-overlap fallback is
  used.
* ``search`` prefers local results and, when confidence falls below a configurable
  threshold, appends web results from a configured search provider.
* ``generate_report`` synthesises a Markdown report from local + web snippets via the
  LLM client (with a pure-assembly fallback when the LLM is unavailable).

Heavy dependencies (``chromadb``, ``sentence-transformers``, ``pypdf``/``PyPDF2``)
are imported lazily so the core backend runs without them.
"""
from __future__ import annotations

import html
import ipaddress
import logging
import os
import re
import socket
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

from app import config, db
from app.config import settings

logger = logging.getLogger(__name__)

CHUNK_SIZE = 500
CHUNK_OVERLAP = 50
SNIPPET_WIDTH = 200

#: Module-level store: ``{"client": <chroma client>, "collection": <collection>}``
#: when Chroma is available, else ``None`` (keyword fallback mode).
_store: dict[str, Any] | None = None
_init_attempted = False


# --------------------------------------------------------------------------- #
# Initialisation
# --------------------------------------------------------------------------- #
def init() -> dict[str, Any] | None:
    """Initialise the Chroma store once (idempotent, non-blocking).

    Returns the store dict, or ``None`` when Chroma is unavailable (fallback mode).
    """
    global _store, _init_attempted
    if _init_attempted:
        return _store
    _init_attempted = True
    try:
        import chromadb  # noqa: F401  (lazy heavy dependency)

        embedding_fn = _make_embedding_function()
        client = chromadb.PersistentClient(path=str(config.CHROMA_DIR))
        if embedding_fn is not None:
            collection = client.get_or_create_collection("kb", embedding_function=embedding_fn)
        else:
            collection = client.get_or_create_collection("kb")
        _store = {"client": client, "collection": collection}
        logger.info("knowledge base initialised with Chroma at %s", config.CHROMA_DIR)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Chroma unavailable, using keyword fallback: %s", exc)
        _store = None
    return _store


def _ensure_store() -> dict[str, Any] | None:
    """Return the active store, initialising it on first use."""
    if not _init_attempted:
        return init()
    return _store


def _make_embedding_function() -> Any:
    """Build a sentence-transformers embedding function, or ``None`` for Chroma default.

    The model is configurable via the ``embedding_model`` setting (env
    ``PAICC_EMBEDDING_MODEL`` or the settings table). It defaults to a compact
    Chinese-friendly model (``BAAI/bge-small-zh-v1.5``) per the blueprint.
    """
    try:
        import sentence_transformers  # noqa: F401  (lazy heavy dependency)
        from chromadb.utils import embedding_functions

        model = str(settings.get("embedding_model", "BAAI/bge-small-zh-v1.5")).strip()
        if not model:
            model = "BAAI/bge-small-zh-v1.5"
        return embedding_functions.SentenceTransformerEmbeddingFunction(model_name=model)
    except Exception as exc:  # noqa: BLE001
        logger.info("sentence-transformers embedding unavailable, using Chroma default: %s", exc)
        return None


# --------------------------------------------------------------------------- #
# Text helpers
# --------------------------------------------------------------------------- #
def _chunk_text(text: str, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Split ``text`` into ~``chunk_size``-char chunks with ``overlap`` chars of overlap."""
    text = (text or "").strip()
    if not text:
        return []
    chunks: list[str] = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + chunk_size, n)
        chunks.append(text[start:end])
        if end >= n:
            break
        start = end - overlap
    return chunks


def _extract_pdf_text(path: str) -> str:
    """Extract text from a PDF using pypdf (or PyPDF2 as a fallback)."""
    try:
        from pypdf import PdfReader  # lazy heavy dependency
    except ImportError:
        try:
            from PyPDF2 import PdfReader  # legacy name
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("PDF parsing requires pypdf or PyPDF2") from exc
    reader = PdfReader(path)
    pages = [page.extract_text() or "" for page in reader.pages]
    return "\n".join(pages)


def _read_file_text(path: str) -> str:
    """Read a file as text; PDFs are parsed, other files are decoded with fallbacks."""
    if path.lower().endswith(".pdf"):
        return _extract_pdf_text(path)
    for encoding in ("utf-8", "gbk", "latin-1"):
        try:
            with open(path, "r", encoding=encoding) as fh:
                return fh.read()
        except (UnicodeDecodeError, OSError):
            continue
    return ""


def _title_from_text(text: str) -> str:
    """Derive a title from the first non-empty line of ``text``."""
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            return line[:120]
    return "Untitled"


def _strip_html(raw: str) -> str:
    """Strip ``<script>``/``<style>`` blocks and remaining tags, then unescape entities."""
    raw = raw or ""
    raw = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw)
    raw = re.sub(r"(?s)<[^>]+>", " ", raw)
    text = html.unescape(raw)
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r"[ \t]*\n[ \t]*", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _title_from_html(raw: str) -> str:
    """Extract a title from the ``<title>`` tag, if present."""
    match = re.search(r"(?is)<title[^>]*>(.*?)</title>", raw or "")
    if not match:
        return ""
    title = html.unescape(re.sub(r"(?s)<[^>]+>", "", match.group(1))).strip()
    return title[:200] if title else ""


#: Cap on the number of bytes read from a fetched URL (SSRF/DoS guard).
MAX_FETCH_BYTES = 2 * 1024 * 1024
_MAX_REDIRECTS = 5


def _is_private_host(host: str) -> bool:
    """Resolve ``host`` and reject loopback/private/link-local/reserved addresses."""
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return True
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if (
            ip.is_loopback
            or ip.is_private
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            return True
    return False


def _validate_public_url(url: str) -> str:
    """Return an error string if ``url`` is not an http(s) public URL, else ``""``."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return "invalid URL"
    if parsed.scheme not in ("http", "https"):
        return "only http/https URLs are allowed"
    host = parsed.hostname
    if not host:
        return "invalid URL"
    if _is_private_host(host):
        return "private/internal hosts are not allowed"
    return ""


def _fetch_url_text(url: str) -> dict[str, Any]:
    """Fetch a web page and extract its plain text plus a derived title.

    SSRF guard: only public http(s) hosts are allowed (loopback/private/link-local/
    reserved addresses are rejected), every redirect hop is re-validated, and the
    response body is streamed with a byte cap.
    """
    err = _validate_public_url(url)
    if err:
        return {"ok": False, "error": err}
    try:
        import httpx
    except ImportError as exc:  # pragma: no cover
        return {"ok": False, "error": f"httpx unavailable: {exc}"}

    resp: Any = None
    current = url
    for _ in range(_MAX_REDIRECTS):
        err = _validate_public_url(current)
        if err:
            return {"ok": False, "error": err}
        try:
            resp = httpx.stream("GET", current, timeout=15.0, follow_redirects=False)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"failed to fetch URL: {exc}"}
        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("location")
            resp.close()
            if not location:
                return {"ok": False, "error": "redirect without location"}
            current = urljoin(current, location)
            continue
        try:
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            resp.close()
            return {"ok": False, "error": f"failed to fetch URL: {exc}"}
        break
    else:
        return {"ok": False, "error": "too many redirects"}

    chunks: list[bytes] = []
    total = 0
    try:
        with resp:
            for chunk in resp.iter_bytes():
                if total >= MAX_FETCH_BYTES:
                    break
                chunks.append(chunk)
                total += len(chunk)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"failed to read response: {exc}"}

    raw = b"".join(chunks).decode("utf-8", errors="replace")
    text = _strip_html(raw)
    if not text:
        return {"ok": False, "error": "no extractable content from URL"}
    title = _title_from_html(raw) or _title_from_text(text)
    return {"ok": True, "title": title, "text": text}


def _persist_raw_text(title: str, text: str) -> str:
    """Persist raw (non-file) text so keyword fallback can re-read it later."""
    try:
        docs_dir = Path(config.DATA_DIR) / "research_docs"
        docs_dir.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^\w一-鿿-]+", "_", title)[:60] or "doc"
        path = docs_dir / f"{safe}_{int(time.time() * 1000)}.txt"
        path.write_text(text, encoding="utf-8")
        return str(path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to persist raw text: %s", exc)
        return ""


def _tokenize(text: str) -> set[str]:
    """Tokenise into lowercase alphanumeric words plus individual CJK characters."""
    text = (text or "").lower()
    terms = set(re.findall(r"[a-z0-9]+", text))
    terms |= set(re.findall(r"[一-鿿]", text))
    return terms


def _snip(text: str, width: int = SNIPPET_WIDTH) -> str:
    return (text or "").strip()[:width]


def _make_snippet(text: str, query: str, width: int = SNIPPET_WIDTH) -> str:
    """Return a snippet window centred on the first matching query term."""
    text = (text or "").strip()
    if not text:
        return ""
    low = text.lower()
    pos = -1
    for term in _tokenize(query):
        idx = low.find(term)
        if idx != -1:
            pos = idx
            break
    if pos == -1:
        return text[:width]
    start = max(0, pos - width // 2)
    return text[start : start + width]


# --------------------------------------------------------------------------- #
# Ingest / list / delete
# --------------------------------------------------------------------------- #
def ingest_text(title: str, text: str, file_path: str | None = None) -> dict[str, Any]:
    """Persist and index a titled raw-text document into the knowledge base.

    This is the reusable core of :func:`ingest` (and is also called by the
    ``save_to_knowledge_base`` tool and ``quant_manager.save_report_to_kb``).
    When ``file_path`` is ``None`` the text is persisted to a copy under
    ``research_docs`` so the keyword fallback can re-read it later.
    """
    title = (title or "").strip() or _title_from_text(text)
    text = (text or "").strip()
    if not text:
        return {"ok": False, "error": "no extractable content"}

    if file_path is None:
        file_path = _persist_raw_text(title, text)

    chunks = _chunk_text(text)
    doc_id = db.execute(
        "INSERT INTO research_documents(title, file_path, ingested_at, chunk_count) VALUES (?, ?, ?, ?)",
        (title, file_path, time.time(), len(chunks)),
    )

    store = _ensure_store()
    if store is not None and chunks:
        try:
            store["collection"].add(
                ids=[f"{doc_id}_{i}" for i in range(len(chunks))],
                documents=chunks,
                metadatas=[
                    {"doc_id": str(doc_id), "title": title, "file_path": file_path}
                    for _ in chunks
                ],
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("chroma add failed for doc %s: %s", doc_id, exc)

    return {
        "ok": True,
        "title": title,
        "chunk_count": len(chunks),
        "doc_id": doc_id,
        "file_path": file_path,
    }


def ingest(source: str) -> dict[str, Any]:
    """Ingest a file path, URL, or raw text into the knowledge base."""
    if not isinstance(source, str) or not source.strip():
        return {"ok": False, "error": "empty source"}

    source = source.strip()
    is_file = False
    file_path: str | None = None

    if source.startswith(("http://", "https://")):
        fetched = _fetch_url_text(source)
        if not fetched.get("ok"):
            return fetched
        title = fetched["title"]
        text = fetched["text"]
    elif os.path.isfile(source):
        is_file = True
        title = os.path.splitext(os.path.basename(source))[0] or "Untitled"
        try:
            text = _read_file_text(str(Path(source).resolve()))
        except Exception as exc:  # noqa: BLE001 — e.g. pypdf not installed
            return {"ok": False, "error": f"failed to read document: {exc}"}
        file_path = str(Path(source).resolve())
    else:
        title = _title_from_text(source)
        text = source

    result = ingest_text(title, text, file_path)
    if not result.get("ok"):
        return result

    db.log_operation(
        "research_ingest",
        {"source": source, "is_file": is_file},
        {
            "doc_id": result.get("doc_id"),
            "title": result.get("title"),
            "chunk_count": result.get("chunk_count"),
        },
    )
    return {
        "ok": True,
        "title": result.get("title"),
        "chunk_count": result.get("chunk_count"),
    }


def list_documents() -> list[dict[str, Any]]:
    """Return all ingested documents (most recent first)."""
    return db.query("SELECT * FROM research_documents ORDER BY id DESC")


def delete_document(doc_id: int) -> dict[str, Any]:
    """Remove a document row and any associated Chroma chunks."""
    row = db.query_one("SELECT * FROM research_documents WHERE id = ?", (doc_id,))
    if row is None:
        return {"ok": False, "error": "document not found"}

    store = _ensure_store()
    if store is not None:
        try:
            store["collection"].delete(where={"doc_id": str(doc_id)})
        except Exception as exc:  # noqa: BLE001
            logger.warning("chroma delete failed for doc %s: %s", doc_id, exc)

    db.execute("DELETE FROM research_documents WHERE id = ?", (doc_id,))
    db.log_operation("research_delete_document", {"doc_id": doc_id}, {"title": row["title"]})
    return {"ok": True, "deleted": doc_id}


# --------------------------------------------------------------------------- #
# Search
# --------------------------------------------------------------------------- #
def _keyword_search(query: str, top_k: int) -> list[dict[str, Any]]:
    """Fallback: score each document's file content by query-term overlap."""
    q_terms = _tokenize(query)
    if not q_terms:
        return []
    scored: list[dict[str, Any]] = []
    for doc in list_documents():
        file_path = doc.get("file_path") or ""
        if not file_path or not os.path.isfile(file_path):
            continue
        try:
            text = _read_file_text(file_path)
        except Exception:  # noqa: BLE001
            continue
        doc_terms = _tokenize(text)
        if not doc_terms:
            continue
        overlap = len(q_terms & doc_terms)
        if overlap == 0:
            continue
        score = overlap / len(q_terms)
        scored.append(
            {
                "title": doc.get("title", ""),
                "file_path": file_path,
                "snippet": _make_snippet(text, query),
                "score": round(score, 4),
            }
        )
    scored.sort(key=lambda r: -float(r["score"]))
    return scored[:top_k]


def search(query: str, top_k: int = 5) -> dict[str, Any]:
    """Search the knowledge base; append web results when confidence is low."""
    query = (query or "").strip()
    if not query:
        return {"ok": True, "results": []}
    try:
        top_k = max(1, int(top_k))
    except (TypeError, ValueError):
        top_k = 5

    results: list[dict[str, Any]] = []
    store = _ensure_store()
    if store is not None:
        try:
            res = store["collection"].query(
                query_texts=[query],
                n_results=top_k,
                include=["documents", "metadatas", "distances"],
            )
            docs = (res.get("documents") or [[]])[0]
            metas = (res.get("metadatas") or [[]])[0]
            dists = (res.get("distances") or [[]])[0]
            for doc, meta, dist in zip(docs, metas, dists):
                meta = meta or {}
                score = 1.0 / (1.0 + float(dist)) if dist is not None else 0.0
                results.append(
                    {
                        "title": meta.get("title", ""),
                        "file_path": meta.get("file_path", ""),
                        "snippet": _snip(doc),
                        "score": round(score, 4),
                    }
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("chroma query failed, using keyword fallback: %s", exc)
            results = []

    if not results:
        results = _keyword_search(query, top_k)

    best = max((float(r.get("score", 0.0)) for r in results), default=0.0)
    threshold = settings.get_float("knowledge_threshold", 0.5)
    if best < threshold:
        for w in web_search(query):
            results.append(
                {
                    "title": w.get("title", ""),
                    "file_path": w.get("url", ""),
                    "url": w.get("url", ""),
                    "snippet": w.get("snippet", ""),
                    "score": 0.0,
                    "source": "web",
                }
            )

    return {"ok": True, "results": results}


# --------------------------------------------------------------------------- #
# Web search
# --------------------------------------------------------------------------- #
def web_search(query: str) -> list[dict[str, Any]]:
    """Search the configured web provider (serpapi or bing). Returns [] on any error."""
    provider = str(settings.get("search_api_provider", "")).strip().lower()
    api_key = str(settings.get("search_api_key", "")).strip()
    if not provider or not api_key or not query:
        return []
    try:
        import httpx  # noqa: F401  (lazy import, already in core requirements)

        if provider == "serpapi":
            resp = httpx.get(
                "https://serpapi.com/search.json",
                params={"q": query, "api_key": api_key},
                timeout=10.0,
            )
            resp.raise_for_status()
            data = resp.json()
            return [
                {
                    "title": item.get("title", ""),
                    "url": item.get("link", ""),
                    "snippet": item.get("snippet", ""),
                }
                for item in data.get("organic_results", [])
            ]
        if provider == "bing":
            resp = httpx.get(
                "https://api.bing.microsoft.com/v7.0/search",
                params={"q": query},
                headers={"Ocp-Apim-Subscription-Key": api_key},
                timeout=10.0,
            )
            resp.raise_for_status()
            data = resp.json()
            return [
                {
                    "title": item.get("name", ""),
                    "url": item.get("url", ""),
                    "snippet": item.get("snippet", ""),
                }
                for item in data.get("webPages", {}).get("value", [])
            ]
    except Exception as exc:  # noqa: BLE001
        logger.warning("web search via %s failed: %s", provider, exc)
    return []


# --------------------------------------------------------------------------- #
# Report generation
# --------------------------------------------------------------------------- #
async def _synthesize_report(topic: str, context: str) -> str:
    """Ask the LLM to synthesise a report, or fall back to assembly if unavailable."""
    try:
        from app.services.llm_client import LLMClient

        client = LLMClient()
        prompt = (
            f"你是一名研究分析师。请基于以下参考资料，就主题「{topic}」撰写一份结构化的 "
            f"Markdown 研究报告。报告需包含：概述、关键发现、详细分析、结论与建议。"
            f"若参考资料不足，请明确说明。\n\n参考资料：\n{context}"
        )
        reply = await client.chat([{"role": "user", "content": prompt}])
        content = reply.get("content") if isinstance(reply, dict) else None
        content = str(content or "").strip()
        if content:
            return content
    except Exception as exc:  # noqa: BLE001
        logger.warning("LLM report synthesis unavailable: %s", exc)
    return _fallback_report(topic, context)


def _fallback_report(topic: str, context: str) -> str:
    """Assemble a simple report directly from retrieved snippets."""
    lines = [
        f"# 研究报告：{topic}",
        "",
        "## 概述",
        f"本报告基于本地知识库与联网检索结果自动生成，主题为「{topic}」。",
        "",
        "## 检索到的资料",
    ]
    if context and context != "(no sources found)":
        lines.append(context)
    else:
        lines.append("- 未检索到相关资料。")
    lines += ["", "## 说明", "（LLM 不可用，本报告由检索片段自动拼装，仅供参考。）"]
    return "\n".join(lines)


async def generate_report(topic: str, use_web: bool = True) -> dict[str, Any]:
    """Generate a Markdown research report from local (+ optional web) sources."""
    topic = (topic or "").strip()
    if not topic:
        return {"content": "", "sources": []}

    local = search(topic, top_k=5).get("results", [])
    web: list[dict[str, Any]] = web_search(topic) if use_web else []

    sources: list[dict[str, Any]] = []
    snippets: list[str] = []
    for r in local:
        snippets.append(f"- {r.get('title', '')}: {r.get('snippet', '')}")
        sources.append({"title": r.get("title", ""), "file_path": r.get("file_path", "")})
    for w in web:
        snippets.append(f"- {w.get('title', '')}: {w.get('snippet', '')}")
        sources.append({"title": w.get("title", ""), "url": w.get("url", "")})

    context = "\n".join(snippets) if snippets else "(no sources found)"
    content = await _synthesize_report(topic, context)

    db.log_operation(
        "research_generate_report",
        {"topic": topic, "use_web": use_web},
        {"sources": len(sources), "content_len": len(content)},
    )
    return {"content": content, "sources": sources}
