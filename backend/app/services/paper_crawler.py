"""Paper crawling and scoring for the daily AI literature review.

Fetches papers from the arXiv API (Atom XML) and the Semantic Scholar Graph API,
normalises them into a common dict shape, scores them on relevance/popularity/
recency/quality, and selects the two daily recommendation sets:

* ``frontier`` — 10 papers from the current month (recency-dominant).
* ``top``      — 10 papers from the past year (full composite score).

This module is pure: it does no persistence, scheduling, or email. See
:mod:`app.services.paper_service` for the orchestration layer.
"""
from __future__ import annotations

import logging
import math
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

ARXIV_API = "http://export.arxiv.org/api/query"
S2_API = "https://api.semanticscholar.org/graph/v1/paper/search"
OPENALEX_API = "https://api.openalex.org/works"
ATOM = "http://www.w3.org/2005/Atom"

_USER_AGENT = "PAICC/0.1 (https://github.com/LoveYaeMiko/PAICC; contact 3555452607@qq.com)"

#: AI sub-fields used for relevance scoring (arXiv categories + S2 fields-of-study).
AI_CATEGORIES = {"cs.AI", "cs.LG", "cs.CL", "cs.CV", "cs.NE", "cs.RO", "stat.ML"}
AI_KEYWORDS = (
    "llm", "large language model", "language model", "transformer", "diffusion",
    "reinforcement learning", "deep learning", "neural network", "generative",
    "agent", "multi-agent", "vision", "multimodal", "retrieval-augmented", "rag",
    "foundation model", "representation learning", "self-supervised", "graph neural",
)
TOP_VENUES = (
    "neurips", "nips", "icml", "iclr", "cvpr", "iccv", "eccv", "acl", "emnlp",
    "naacl", "aaai", "ijcai", "kdd", "sigir", "nature", "science", "tmlr", "jmlr",
    "tpami", "pami",
)

_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


def _proxy() -> str | None:
    proxy = str(settings.get("papers_proxy", "")).strip()
    return proxy or None


def _headers() -> dict[str, str]:
    headers = {"User-Agent": _USER_AGENT}
    key = str(settings.get("paper_s2_api_key", "")).strip()
    if key:
        headers["x-api-key"] = key
    return headers


def _parse_date(value: str | None) -> tuple[str, float]:
    """Return ``(iso_date, epoch)`` for a source date string; ``("", 0.0)`` on failure."""
    if not value:
        return "", 0.0
    text = value.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%Y-%m"):
        try:
            dt = datetime.strptime(text, fmt)
            return dt.strftime("%Y-%m-%d"), dt.timestamp()
        except ValueError:
            continue
    return "", 0.0


def _extract_arxiv_id(id_text: str) -> str | None:
    if not id_text:
        return None
    raw = id_text.rsplit("/", 1)[-1]
    return re.sub(r"v\d+$", "", raw) or None


def _reconstruct_abstract(inverted: Any) -> str:
    """Reconstruct a plain-text abstract from OpenAlex's ``abstract_inverted_index``."""
    if not isinstance(inverted, dict) or not inverted:
        return ""
    positions: dict[int, str] = {}
    for word, idxs in inverted.items():
        if isinstance(idxs, list):
            for i in idxs:
                try:
                    positions[int(i)] = word
                except (TypeError, ValueError):
                    continue
    return " ".join(positions[i] for i in sorted(positions))


def _arxiv_id_from_url(url: str | None) -> str | None:
    if not url:
        return None
    m = re.search(r"arxiv\.org/(?:abs|pdf)/([^/\s?#]+)", url)
    if m:
        return re.sub(r"v\d+$", "", m.group(1))
    return None


def _ai_concept_score(work: dict[str, Any]) -> float:
    """Highest OpenAlex score among the work's field-level AI concepts."""
    best = 0.0
    for concept in work.get("concepts") or []:
        if (concept.get("display_name") or "").lower() in _CORE_AI_CONCEPTS:
            try:
                best = max(best, float(concept.get("score") or 0.0))
            except (TypeError, ValueError):
                continue
    return best


# ---------------------------------------------------------------------------
# arXiv
# ---------------------------------------------------------------------------
def fetch_arxiv(categories: str | None = None, since_days: int = 365) -> list[dict[str, Any]]:
    """Fetch papers from arXiv within ``since_days``, newest first, limited to 200."""
    cats = [c.strip() for c in (categories or "").split(",") if c.strip()]
    if not cats:
        cats = sorted(AI_CATEGORIES)

    now = datetime.now()
    start = datetime.fromtimestamp(now.timestamp() - since_days * 86400)
    cat_query = " OR ".join(f"cat:{c}" for c in cats)
    search_query = (
        f"({cat_query}) AND submittedDate:[{start.strftime('%Y%m%d0000')} "
        f"TO {now.strftime('%Y%m%d2359')}]"
    )
    params = {
        "search_query": search_query,
        "start": 0,
        "max_results": 200,
        "sortBy": "submittedDate",
        "sortOrder": "descending",
    }
    url = f"{ARXIV_API}?{urlencode(params)}"

    resp = None
    for attempt in range(3):
        try:
            resp = httpx.get(url, proxy=_proxy(), timeout=_TIMEOUT, follow_redirects=True)
            if resp.status_code == 429:
                wait = 5 * (attempt + 1)
                logger.warning("arXiv 429 (retry in %ds)", wait)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            break
        except Exception as exc:  # noqa: BLE001
            logger.exception("arXiv fetch failed")
            time.sleep(1)
    if resp is None or resp.status_code >= 400:
        return []

    papers: list[dict[str, Any]] = []
    try:
        root = ET.fromstring(resp.content)
        for entry in root.findall(f"{{{ATOM}}}entry"):
            arxiv_id = _extract_arxiv_id((entry.findtext(f"{{{ATOM}}}id") or ""))
            title = " ".join((entry.findtext(f"{{{ATOM}}}title") or "").split())
            abstract = " ".join((entry.findtext(f"{{{ATOM}}}summary") or "").split())
            published_at, published_ts = _parse_date(entry.findtext(f"{{{ATOM}}}published"))
            authors = ", ".join(
                a.findtext(f"{{{ATOM}}}name") or "" for a in entry.findall(f"{{{ATOM}}}author")
            )
            cats_list = [c.get("term", "") for c in entry.findall(f"{{{ATOM}}}category")]
            papers.append(
                {
                    "arxiv_id": arxiv_id,
                    "title": title,
                    "authors": authors,
                    "abstract": abstract,
                    "categories": ", ".join(cats_list),
                    "fields": "",
                    "published_at": published_at,
                    "published_ts": published_ts,
                    "url": f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else "",
                    "source": "arxiv",
                    "citation_count": 0,
                    "venue": "",
                }
            )
    except Exception:  # noqa: BLE001
        logger.exception("arXiv parse failed")
    return papers


# ---------------------------------------------------------------------------
# Semantic Scholar
# ---------------------------------------------------------------------------
_S2_QUERIES = (
    "artificial intelligence",
    "large language model",
    "deep learning",
    "reinforcement learning",
    "computer vision",
    "natural language processing",
)
_S2_FIELDS = "title,abstract,authors,venue,year,publicationDate,citationCount,externalIds,url,fieldsOfStudy"

#: OpenAlex concept IDs scoping the fetch to AI sub-fields only. The broad
#: "Computer science" concept (C41008148) was deliberately dropped — it also
#: covers databases, programming languages, and systems, letting non-AI papers
#: (PL/logic, graph databases, materials-science journals) through. IDs:
#:   Artificial intelligence C154945302, Machine learning C119857082,
#:   Computer vision C31972630, NLP C204321447, Deep learning C108583219,
#:   Reinforcement learning C97541855, Speech recognition C28490314,
#:   Recommender system C557471498, Artificial neural network C50644808,
#:   Robot C90509273, Machine translation C203005215, Knowledge graph C2987255567,
#:   Object detection C2776151529, Generative model C167966045.
_OPENALEX_CONCEPTS = (
    "C154945302|C119857082|C31972630|C204321447|C108583219|C97541855|C28490314|"
    "C557471498|C50644808|C90509273|C203005215|C2987255567|C2776151529|C167966045"
)

#: Field-level AI concepts (as opposed to method-level ones like "decision tree",
#: "transformer", or "language model"). OpenAlex tags papers from other domains with
#: low-score "artificial intelligence" / "machine learning" concepts when they only
#: *use* an ML method (a land-cover study using decision trees, a PID-control paper),
#: so a paper is kept only when one of these field-level concepts clears the score
#: threshold below — the strict AI gate for the citation/venue source.
_CORE_AI_CONCEPTS = {
    "artificial intelligence",
    "machine learning",
    "deep learning",
    "computer vision",
    "natural language processing",
    "reinforcement learning",
    "generative model",
    "speech recognition",
    "language model",
    "robotics",
    "robot",
}

#: Minimum OpenAlex concept score for a field-level AI concept to count. Genuine AI
#: papers score ~0.5–0.9; tangential users of ML methods (a psycholinguistics study
#: tagged "natural language processing", a land-cover study using decision trees) sit
#: at ~0.0–0.42.
_AI_SCORE_THRESHOLD = 0.45


def fetch_semantic_scholar(since_days: int = 365) -> list[dict[str, Any]]:
    """Fetch papers from Semantic Scholar via AI-topic keyword searches."""
    cutoff = time.time() - since_days * 86400
    seen: dict[str, dict[str, Any]] = {}

    for query in _S2_QUERIES:
        params = {"query": query, "fields": _S2_FIELDS, "limit": 50}
        data: list[dict[str, Any]] = []
        for attempt in range(3):
            try:
                resp = httpx.get(
                    S2_API,
                    params=params,
                    proxy=_proxy(),
                    headers=_headers(),
                    timeout=_TIMEOUT,
                    follow_redirects=True,
                )
                if resp.status_code == 429:
                    wait = 5 * (attempt + 1)
                    logger.warning("Semantic Scholar 429 for %r (retry in %ds)", query, wait)
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                data = resp.json().get("data") or []
                break
            except Exception as exc:  # noqa: BLE001
                logger.exception("Semantic Scholar fetch failed for %r", query)
                time.sleep(1)
                break

        for p in data:
            ext = p.get("externalIds") or {}
            arxiv_id = ext.get("ArXiv")
            title = " ".join((p.get("title") or "").split())
            key = arxiv_id or title.lower()
            if not title or key in seen:
                continue
            authors = ", ".join(a.get("name", "") for a in (p.get("authors") or []))
            fields = ", ".join(p.get("fieldsOfStudy") or [])
            published_at, published_ts = _parse_date(p.get("publicationDate"))
            if published_ts and published_ts < cutoff:
                continue
            seen[key] = {
                "arxiv_id": arxiv_id,
                "title": title,
                "authors": authors,
                "abstract": " ".join((p.get("abstract") or "").split()),
                "categories": "",
                "fields": fields,
                "published_at": published_at,
                "published_ts": published_ts,
                "url": p.get("url") or (f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else ""),
                "source": "semantic_scholar",
                "citation_count": int(p.get("citationCount") or 0),
                "venue": p.get("venue") or "",
            }
        time.sleep(1)

    return list(seen.values())


def fetch_openalex(since_days: int = 365) -> list[dict[str, Any]]:
    """Fetch recent, highly-cited AI papers from OpenAlex (free, key-free).

    Used as a reliable source of citation counts and venue metadata so the
    popularity/quality scoring keeps working even when Semantic Scholar is
    rate-limiting the client's IP. Scoped to AI/ML/CS via concept filters,
    restricted to the past ``since_days``, and sorted by citation count so the
    "top" recommendation set favours high-impact work.
    """
    cutoff = time.time() - since_days * 86400
    from_date = (datetime.now() - timedelta(days=since_days)).strftime("%Y-%m-%d")
    seen: dict[str, dict[str, Any]] = {}

    params = {
        "filter": f"concepts.id:{_OPENALEX_CONCEPTS},from_publication_date:{from_date}",
        "sort": "cited_by_count:desc",
        "per_page": 200,
        "mailto": "3555452607@qq.com",
    }
    try:
        resp = httpx.get(
            OPENALEX_API,
            params=params,
            proxy=_proxy(),
            headers=_headers(),
            timeout=_TIMEOUT,
            follow_redirects=True,
        )
        resp.raise_for_status()
        results = resp.json().get("results") or []
    except Exception as exc:  # noqa: BLE001
        logger.exception("OpenAlex fetch failed")
        return []

    for w in results:
        title = " ".join((w.get("title") or "").split())
        if not title:
            continue
        published_at, published_ts = _parse_date(w.get("publication_date"))
        if published_ts and published_ts < cutoff:
            continue
        loc = w.get("primary_location") or {}
        landing = loc.get("landing_page_url") or loc.get("pdf_url") or ""
        arxiv_id = _arxiv_id_from_url(landing)
        if not arxiv_id:
            for loc2 in w.get("locations") or []:
                arxiv_id = _arxiv_id_from_url(
                    loc2.get("landing_page_url") or loc2.get("pdf_url") or ""
                )
                if arxiv_id:
                    break
        key = arxiv_id or title.lower()
        if key in seen:
            continue
        authors = ", ".join(
            (a.get("author") or {}).get("display_name") or ""
            for a in (w.get("authorships") or [])
        )
        source = loc.get("source") or {}
        concepts = ", ".join(
            (c.get("display_name") or "") for c in (w.get("concepts") or [])
        )
        abstract = _reconstruct_abstract(w.get("abstract_inverted_index"))
        if _ai_concept_score(w) < _AI_SCORE_THRESHOLD:
            continue
        seen[key] = {
            "arxiv_id": arxiv_id,
            "title": title,
            "authors": authors,
            "abstract": abstract,
            "categories": "",
            "fields": concepts,
            "published_at": published_at,
            "published_ts": published_ts,
            "url": landing or (f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else ""),
            "source": "openalex",
            "citation_count": int(w.get("cited_by_count") or 0),
            "venue": source.get("display_name") or "",
        }

    return list(seen.values())


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
def _category_set(p: dict[str, Any]) -> set[str]:
    cats = (p.get("categories") or "").split(",") + (p.get("fields") or "").split(",")
    return {c.strip() for c in cats if c.strip()}


def _relevance(p: dict[str, Any]) -> float:
    cats = _category_set(p)
    text = f"{p.get('title', '')} {p.get('abstract', '')}".lower()
    score = 0.0
    if cats & AI_CATEGORIES:
        score += 0.6
    hits = sum(1 for kw in AI_KEYWORDS if kw in text)
    score += min(hits * 0.08, 0.4)
    return min(score, 1.0)


def _popularity(p: dict[str, Any]) -> float:
    citations = max(0, int(p.get("citation_count") or 0))
    return min(1.0, math.log1p(citations) / math.log1p(500))


def _recency(p: dict[str, Any]) -> float:
    ts = p.get("published_ts") or 0.0
    if ts <= 0:
        return 0.0
    days = max(0.0, (time.time() - ts) / 86400)
    return max(0.0, 1.0 - days / 365.0)


def _quality(p: dict[str, Any]) -> float:
    venue = (p.get("venue") or "").lower()
    score = 0.0
    if any(v in venue for v in TOP_VENUES):
        score += 0.7
    cats = _category_set(p)
    if cats & AI_CATEGORIES:
        score += 0.3
    return min(score, 1.0)


def top_score(p: dict[str, Any]) -> float:
    """Composite score (relevance + popularity + recency + quality)."""
    return 0.35 * _relevance(p) + 0.25 * _popularity(p) + 0.20 * _recency(p) + 0.20 * _quality(p)


def frontier_score(p: dict[str, Any]) -> float:
    """Recency-dominant score for the current-month 'frontier' set."""
    return 0.5 * _recency(p) + 0.3 * _relevance(p) + 0.2 * _quality(p)


# ---------------------------------------------------------------------------
# Merge + select
# ---------------------------------------------------------------------------
def paper_key(p: dict[str, Any]) -> str:
    """Stable identity for a paper: arXiv id when present, else lower-cased title.

    This is the same key ``merge_dedupe`` collapses on, so ``filter_seen`` can
    reliably drop papers that were already recommended on a previous day.
    """
    return p.get("arxiv_id") or (p.get("title") or "").strip().lower()


def filter_seen(pool: list[dict[str, Any]], seen: set[str]) -> list[dict[str, Any]]:
    """Drop papers already recommended before (never repeat a recommendation)."""
    if not seen:
        return pool
    return [p for p in pool if paper_key(p) not in seen]


def merge_dedupe(*source_lists: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge paper lists from multiple sources, enriching earlier records with the
    richer citation/venue metadata of later ones (matched by arXiv id or title)."""
    by_arxiv: dict[str, dict[str, Any]] = {}
    by_title: dict[str, dict[str, Any]] = {}
    merged: list[dict[str, Any]] = []

    def add(p: dict[str, Any]) -> None:
        if p.get("arxiv_id"):
            existing = by_arxiv.get(p["arxiv_id"])
            if existing is None:
                by_arxiv[p["arxiv_id"]] = p
                merged.append(p)
            else:
                _enrich(existing, p)
            return
        tkey = p["title"].lower()
        existing = by_title.get(tkey)
        if existing is None:
            by_title[tkey] = p
            merged.append(p)
        else:
            _enrich(existing, p)

    for papers in source_lists:
        for p in papers:
            add(p)
    return merged


def _enrich(target: dict[str, Any], incoming: dict[str, Any]) -> None:
    """Fill empty target fields from a duplicate's richer metadata."""
    for key in ("abstract", "authors", "venue", "fields", "url", "published_at", "published_ts"):
        if not target.get(key) and incoming.get(key):
            target[key] = incoming[key]
    if not target.get("arxiv_id") and incoming.get("arxiv_id"):
        target["arxiv_id"] = incoming["arxiv_id"]
    if incoming.get("citation_count", 0) > target.get("citation_count", 0):
        target["citation_count"] = incoming["citation_count"]
    inc_src = incoming.get("source") or ""
    tgt_src = target.get("source") or ""
    if inc_src != tgt_src and inc_src in ("semantic_scholar", "openalex"):
        target["source"] = f"{tgt_src}+{inc_src}" if tgt_src else inc_src


def select_recommendations(
    pool: list[dict[str, Any]], frontier_n: int = 10, top_n: int = 10
) -> dict[str, list[dict[str, Any]]]:
    """Select the two daily sets. Frontier papers come from the current month and
    are excluded from the past-year 'top' set (no overlap)."""
    month_prefix = datetime.now().strftime("%Y-%m")

    frontier_candidates = [p for p in pool if (p.get("published_at") or "").startswith(month_prefix)]
    frontier_candidates.sort(key=frontier_score, reverse=True)
    frontier = frontier_candidates[:frontier_n]

    # The "top" set covers the past year *excluding* the current month, which is
    # what the "frontier" set already represents (new papers have no citations yet,
    # so recency would otherwise drown out the high-impact work we want here).
    rest = [
        p
        for p in pool
        if not (p.get("published_at") or "").startswith(month_prefix)
        and (p.get("published_ts") or 0) > time.time() - 365 * 86400
    ]
    rest.sort(key=top_score, reverse=True)
    top = rest[:top_n]

    return {"frontier": frontier, "top": top}
