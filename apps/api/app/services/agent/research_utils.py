"""research_utils.py — Pure utility functions for the research pipeline.

All functions here are stateless and can be called from any other
research_*.py module without risk of circular imports.

Extracted from research_subtree.py (TD-01).
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from functools import lru_cache
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from app.services.agent.models import Source

# Deliberate single-owner-usage default while Fronei has one real user: falls
# back to the owner's timezone rather than UTC so "today"/"tomorrow" resolve
# correctly even when a request has no user_timezone. Revisit (switch to a
# UTC fallback, make frontend timezone capture mandatory) once Fronei has
# broader multi-user traffic.
_DEFAULT_TZ = "America/New_York"


def temporal_context(tz: str | None = None) -> dict:
    """Current date/time context injected into every model-facing payload."""
    try:
        zone = ZoneInfo(tz) if tz else ZoneInfo(_DEFAULT_TZ)
    except Exception:
        zone = ZoneInfo(_DEFAULT_TZ)
    now = datetime.now(zone)
    return {
        "current_date": now.strftime("%A, %B %d, %Y"),
        "current_datetime_iso": now.isoformat(),
    }


@lru_cache(maxsize=1)
def _tiktoken_encoding():
    import tiktoken

    return tiktoken.get_encoding("cl100k_base")


def estimate_tokens(text: str) -> int:
    """Rough token estimate for context-budget governance.

    tiktoken is already installed transitively via litellm, so this uses its
    cl100k_base encoding as a cheap, provider-agnostic approximation --
    exact for OpenAI models, close enough for Anthropic/Gemini for budgeting
    purposes. Falls back to a chars/4 heuristic if tiktoken is unavailable
    for any reason. Deliberately cheap since this runs on every
    context-assembly pass, not meant to match provider-billed token counts.
    """
    if not text:
        return 1
    try:
        return max(1, len(_tiktoken_encoding().encode(text)))
    except Exception:
        return max(1, len(text) // 4)


def _dedupe(values: list[str]) -> list[str]:
    """Return a deduplicated list of values preserving insertion order.

    Normalises whitespace and compares case-insensitively.
    """
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        cleaned = " ".join(str(value).split())
        key = cleaned.lower()
        if cleaned and key not in seen:
            seen.add(key)
            result.append(cleaned)
    return result


def _parse_json(raw: str) -> dict:
    """Parse a JSON string that may be wrapped in a markdown code fence."""
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?", "", cleaned).strip()
        cleaned = re.sub(r"```$", "", cleaned).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if not match:
            raise
        return json.loads(match.group(0))


def _estimate_relevance(source: Source, questions: list[str]) -> float:
    """Estimate keyword-overlap relevance of a source against a list of questions."""
    haystack = f"{source.title} {source.snippet} {source.content}".lower()
    if not haystack or not questions:
        return 0.5
    tokens = {token for question in questions for token in re.findall(r"[a-z0-9]{4,}", question.lower())}
    if not tokens:
        return 0.5
    hits = sum(1 for token in tokens if token in haystack)
    return max(0.25, min(0.95, 0.35 + hits / max(4, len(tokens))))


def _extract_urls_from_text(text: str) -> list[str]:
    """Extract all HTTP/HTTPS URLs from markdown or plain text."""
    markdown_urls = re.findall(r"\[[^\]]+\]\((https?://[^)\s]+)\)", text or "")
    plain_urls = re.findall(r"https?://[^\s)>\]\"']+", text or "")
    return _dedupe([*_clean_urls(markdown_urls), *_clean_urls(plain_urls)])


def _clean_urls(urls: list[str]) -> list[str]:
    """Strip trailing punctuation from URLs."""
    return [url.rstrip(".,;:") for url in urls if url]


def _looks_like_substantive_claim(sentence: str) -> bool:
    """Return True if a sentence contains tokens associated with verifiable claims."""
    lowered = sentence.lower()
    return any(
        token in lowered
        for token in (
            "%",
            "$",
            "million",
            "billion",
            "increase",
            "decrease",
            "growth",
            "decline",
            "market",
            "revenue",
            "cost",
            "risk",
            "announced",
            "reported",
        )
    )


# ---------------------------------------------------------------------------
# Source classification and scoring
# ---------------------------------------------------------------------------

def classify_source_type(url: str) -> str:
    """Classify a URL into a broad source category."""
    parsed = urlparse(url or "")
    path = parsed.path.lower()
    host = (parsed.hostname or "").lower()
    if path.endswith(".pdf"):
        return "pdf"
    if "arxiv.org" in host or "papers.ssrn.com" in host or "aclanthology.org" in host:
        return "academic"
    if "github.com" in host or "gitlab.com" in host:
        return "repository"
    if host.endswith(".gov") or ".gov." in host:
        return "government"
    if host.endswith(".edu") or ".edu." in host:
        return "academic"
    if any(token in host for token in ("sec.gov", "who.int", "oecd.org", "worldbank.org", "imf.org")):
        return "primary"
    if any(token in host for token in ("docs.", "developer.", "support.", "help.", "readthedocs", "langchain", "llamaindex")):
        return "documentation"
    if any(token in host for token in ("reuters.com", "apnews.com", "bloomberg.com", "ft.com", "wsj.com")):
        return "news"
    return "web"


def score_source_authority(url: str) -> float:
    """Return an authority score [0, 1] for a URL based on its source type."""
    scores = {
        "government": 0.95,
        "primary": 0.92,
        "academic": 0.88,
        "repository": 0.86,
        "documentation": 0.84,
        "pdf": 0.76,
        "news": 0.68,
        "web": 0.52,
    }
    return scores.get(classify_source_type(url), 0.5)


def score_technical_density(source: Source) -> float:
    """Return a technical density score [0, 1] based on signal term frequency and source type."""
    text = f"{source.title} {source.url} {source.snippet} {source.content}".lower()
    signals = [
        "architecture", "component", "workflow", "orchestrator", "planner", "executor",
        "critic", "judge", "guardrail", "retrieval", "citation", "evidence", "schema",
        "state", "memory", "tool", "mcp", "api", "latency", "cost", "evaluation",
        "benchmark", "failure", "retry", "queue", "event", "trace", "github", "arxiv",
        "implementation",
    ]
    hits = sum(1 for signal in signals if signal in text)
    type_bonus = {
        "academic": 0.28,
        "repository": 0.26,
        "documentation": 0.22,
        "pdf": 0.16,
        "primary": 0.12,
    }.get(classify_source_type(source.url), 0.0)
    content_bonus = 0.12 if len(source.content or "") > 1200 else 0.0
    return max(0.0, min(1.0, type_bonus + min(0.60, hits * 0.035) + content_bonus))


__all__ = [
    "_clean_urls",
    "_dedupe",
    "_estimate_relevance",
    "_extract_urls_from_text",
    "_looks_like_substantive_claim",
    "_parse_json",
    "classify_source_type",
    "estimate_tokens",
    "score_source_authority",
    "score_technical_density",
    "temporal_context",
]
