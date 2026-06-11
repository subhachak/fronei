"""Iterative deep research pipeline.

This is intentionally separate from the normal chat worker path.  It performs
bounded research runs: plan questions, search/fetch sources, extract claims,
check gaps/contradictions, synthesize, and optionally verify.
"""
from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Iterable
from urllib.parse import urlparse

from litellm import completion

from app.config import get_settings
from app.db.models import (
    ResearchClaim,
    ResearchFinding,
    ResearchQuestion,
    ResearchRun,
    ResearchSource,
)
from app.schemas import Profile, RouteDecision
from app.services.llm_gateway import LLMResult, invoke_llm
from app.services.router import choose_route
from app.services.web_context import (
    WebSource,
    brave_search,
    crawl_url,
    ddg_search,
    find_urls,
    tavily_search,
)

MAX_ITERATIONS         = 3     # up to 3 gap-filling passes
HARD_MAX_SOURCES       = 40    # emergency brake — never exceed
MIN_TOTAL_SOURCES      = 6     # don't stop before this many sources
MIN_SOURCES_PER_QUESTION = 2   # each question needs this many sources before skipping
MIN_PRIMARY_SOURCES_DEEP = 1
MIN_PRIMARY_SOURCES_EXPERT = 2
MIN_CREDIBILITY_SCORE  = 0.25  # discard sources below this threshold
MAX_CLAIMS_PER_SOURCE  = 4
SOURCE_EXCERPT_CHARS   = 6000
PRIMARY_SOURCE_TYPES = {"government", "academic", "documentation", "pricing", "release_notes", "repository", "pdf"}
MAX_SOURCES_PER_HOST_DEEP = 4
MAX_SOURCES_PER_HOST_EXPERT = 3
CLAIM_EXTRACTOR_MODEL = "claude-haiku-4-5-20251001"
MAX_CLAIM_EXTRACT_WORKERS = 6
MAX_QUESTION_WORKERS = 4
MAX_CANDIDATES_PER_QUESTION = 8


@dataclass
class ResearchPipelineResult:
    run: ResearchRun
    result: LLMResult
    route: RouteDecision
    source_logs: list[dict]
    questions: list[str]
    gaps: list[str]
    contradictions: list[str]
    verifier_notes: str | None
    claim_logs: list[dict] = field(default_factory=list)


@dataclass
class PlannedQuestion:
    question: str
    search_query: str
    priority: str = "medium"
    required_source_types: list[str] | None = None


@dataclass
class ClaimRecord:
    claim: str
    score: float
    quote: str | None = None
    confidence: str = "medium"


@dataclass
class SourceQuality:
    source_type: str
    credibility: float
    freshness: float
    relevance: float
    quality: float
    published_year: int | None = None


@dataclass
class ResearchDomainStrategy:
    domain: str
    preferred_source_types: set[str]
    query_suffixes: list[str]
    primary_source_hint: str


@dataclass
class ResearchEvaluation:
    gaps: list[str]
    follow_up_queries: list[str]
    contradictions: list[str]
    confidence: str
    enough_evidence: bool


@dataclass
class CitationVerification:
    verifier_notes: str
    unsupported_claims: list[str]
    citation_issues: list[str]
    stale_or_overconfident_claims: list[str]
    verified_answer: str


@dataclass
class QuestionWorkerResult:
    question_id: int | None
    candidates: list[tuple[str, WebSource, int | None]]


Progress = Callable[[str, str, dict], None]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _tokens(text: str) -> set[str]:
    stop = {
        "about", "after", "again", "against", "also", "and", "are", "from",
        "have", "into", "latest", "more", "need", "should", "that", "the",
        "their", "there", "this", "what", "when", "where", "which", "with",
        "would", "your",
    }
    return {
        w.lower()
        for w in re.findall(r"[A-Za-z0-9][A-Za-z0-9\-]{2,}", text)
        if w.lower() not in stop
    }


def _split_sentences(text: str) -> list[str]:
    chunks = re.split(r"(?<=[.!?])\s+", text.strip())
    return [c.strip() for c in chunks if 45 <= len(c.strip()) <= 360]


def _host(url: str) -> str:
    return urlparse(url).netloc.lower().removeprefix("www.")


def _max_sources_per_host(mode: str) -> int:
    return MAX_SOURCES_PER_HOST_EXPERT if mode == "expert" else MAX_SOURCES_PER_HOST_DEEP


def _source_type(url: str) -> str:
    host = _host(url)
    path = urlparse(url).path.lower()
    if path.endswith(".pdf"):
        return "pdf"
    if any(x in path for x in ["/pricing", "/price", "/plans"]):
        return "pricing"
    if any(x in path for x in ["/release", "/changelog", "/updates", "/whats-new"]):
        return "release_notes"
    if any(x in host for x in ["docs.", "developer.", "learn.", "support.", "help."]):
        return "documentation"
    if host.endswith(".gov"):
        return "government"
    if host.endswith(".edu"):
        return "academic"
    if "github.com" in host:
        return "repository"
    if any(x in host for x in ["medium.", "substack.", "blog", "wordpress.", "blogspot."]):
        return "commentary"
    if any(x in host for x in ["reddit.", "quora.", "news.ycombinator."]):
        return "forum"
    if any(x in host for x in ["forbes.", "businessinsider.", "zdnet.", "techcrunch.", "theverge."]):
        return "news"
    return "web"


def _research_domain_strategy(query: str) -> ResearchDomainStrategy:
    lower = query.lower()
    legal_markers = {
        "visa", "immigration", "uscis", "h-1b", "h1b", "h-4", "h4", "ead",
        "i-765", "i-539", "law", "legal", "regulation", "compliance", "policy",
    }
    medical_markers = {
        "medical", "clinical", "disease", "drug", "fda", "treatment", "diagnosis",
        "patient", "trial", "therapy", "healthcare", "medication",
    }
    finance_markers = {
        "financial", "finance", "stock", "earnings", "sec", "10-k", "10q", "10-q",
        "market", "revenue", "pricing", "cost", "budget", "roi",
    }
    enterprise_markers = {
        "api", "platform", "cloud", "rag", "architecture", "enterprise", "governance",
        "security", "azure", "aws", "bedrock", "snowflake", "google cloud", "gcp",
        "databricks", "salesforce", "oracle", "microsoft", "model", "llm",
    }
    academic_markers = {"paper", "study", "research", "benchmark", "arxiv", "dataset", "evaluation"}

    tokens = _tokens(lower)
    if tokens & legal_markers:
        return ResearchDomainStrategy(
            domain="legal_regulatory",
            preferred_source_types={"government", "pdf", "documentation"},
            query_suffixes=["official site:.gov", "policy guidance", "regulation"],
            primary_source_hint="government/legal primary sources",
        )
    if tokens & medical_markers:
        return ResearchDomainStrategy(
            domain="medical",
            preferred_source_types={"government", "academic", "pdf"},
            query_suffixes=["site:nih.gov", "site:fda.gov", "clinical study"],
            primary_source_hint="government, institutional, or peer-reviewed sources",
        )
    if tokens & finance_markers and not (tokens & enterprise_markers):
        return ResearchDomainStrategy(
            domain="financial",
            preferred_source_types={"government", "pdf", "news"},
            query_suffixes=["official filing", "site:sec.gov", "investor relations"],
            primary_source_hint="regulatory filings and official company sources",
        )
    if tokens & enterprise_markers:
        return ResearchDomainStrategy(
            domain="enterprise_technology",
            preferred_source_types={"documentation", "pricing", "release_notes", "repository"},
            query_suffixes=["official docs", "pricing", "release notes"],
            primary_source_hint="vendor docs, pricing pages, release notes, and repositories",
        )
    if tokens & academic_markers:
        return ResearchDomainStrategy(
            domain="academic",
            preferred_source_types={"academic", "pdf", "repository"},
            query_suffixes=["paper", "arxiv", "benchmark"],
            primary_source_hint="papers, datasets, and repositories",
        )
    return ResearchDomainStrategy(
        domain="general",
        preferred_source_types=PRIMARY_SOURCE_TYPES,
        query_suffixes=["official source", "2026", "analysis"],
        primary_source_hint="primary or official sources",
    )


def _normalize_required_source_type(value: str) -> set[str]:
    key = value.lower().strip().replace("-", "_").replace(" ", "_")
    aliases = {
        "official": {"government", "documentation", "pricing", "release_notes"},
        "official_docs": {"documentation", "pricing", "release_notes"},
        "primary": PRIMARY_SOURCE_TYPES,
        "primary_sources": PRIMARY_SOURCE_TYPES,
        "pricing": {"pricing"},
        "price": {"pricing"},
        "release_notes": {"release_notes"},
        "changelog": {"release_notes"},
        "government": {"government"},
        "gov": {"government"},
        "papers": {"academic", "pdf"},
        "research_papers": {"academic", "pdf"},
        "academic": {"academic"},
        "repository": {"repository"},
        "repos": {"repository"},
    }
    return aliases.get(key, {key})


def _preferred_source_types(
    strategy: ResearchDomainStrategy,
    required_source_types: list[str] | None = None,
) -> set[str]:
    preferred = set(strategy.preferred_source_types)
    for source_type in required_source_types or []:
        preferred.update(_normalize_required_source_type(source_type))
    return preferred


def _credibility(url: str, title: str = "", content: str = "") -> float:
    host = _host(url)
    path = urlparse(url).path.lower()
    source_type = _source_type(url)
    haystack = f"{title} {content[:1200]}".lower()
    score = 0.45
    if host.endswith(".gov"):
        score += 0.42
    elif host.endswith(".edu"):
        score += 0.35
    if source_type in {"documentation", "pricing", "release_notes"}:
        score += 0.28
    if "github.com" in host:
        score += 0.25
    if source_type == "pdf":
        score += 0.08
    if any(x in path for x in ["/docs", "/developer", "/learn", "/reference", "/api", "/pricing", "/release-notes"]):
        score += 0.08
    if any(p in haystack for p in ["official documentation", "api reference", "release notes", "pricing", "service limits"]):
        score += 0.08
    if any(x in host for x in ["medium.", "substack.", "reddit.", "quora.", "wordpress.", "blogspot."]):
        score -= 0.15
    if any(p in haystack for p in ["sponsored", "affiliate", "coupon", "top 10", "best tools", "ultimate guide"]):
        score -= 0.12
    return max(0.05, min(1.0, score))


def _extract_published_year(content: str) -> int | None:
    patterns = [
        r"(?:published|updated|last updated|modified|date)[:\s]+(?:[A-Za-z]{3,9}\s+\d{1,2},\s+)?(20[1-3][0-9])",
        r"\b(20[1-3][0-9])[-/]\d{1,2}[-/]\d{1,2}\b",
        r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\s+\d{1,2},\s+(20[1-3][0-9])\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, content, re.IGNORECASE)
        if match:
            return int(match.group(1))
    years = [int(y) for y in re.findall(r"\b20[1-3][0-9]\b", content)]
    return max(years) if years else None


def _freshness(content: str) -> float:
    newest = _extract_published_year(content)
    if newest is None:
        return 0.45
    current = _now().year
    if newest >= current:
        return 1.0
    if newest == current - 1:
        return 0.85
    if newest == current - 2:
        return 0.65
    return 0.35


def _relevance(query: str, content: str) -> float:
    q = _tokens(query)
    if not q:
        return 0.5
    c = _tokens(content[:4000])
    return min(1.0, len(q & c) / max(1, len(q)) + 0.15)


def _source_quality(
    query: str,
    title: str,
    url: str,
    content: str,
    strategy: ResearchDomainStrategy | None = None,
    required_source_types: list[str] | None = None,
) -> SourceQuality:
    source_type = _source_type(url)
    cred = _credibility(url, title, content)
    fresh = _freshness(content)
    relev = _relevance(query, f"{title}\n{content}")
    primary_bonus = 0.0
    if source_type in {"government", "academic", "documentation", "pricing", "release_notes", "repository"}:
        primary_bonus = 0.08
    if source_type in {"forum", "commentary"}:
        primary_bonus = -0.08
    if strategy:
        preferred = _preferred_source_types(strategy, required_source_types)
        if source_type in preferred:
            primary_bonus += 0.08
            cred = min(1.0, cred + 0.06)
        elif source_type in {"forum", "commentary", "news"} and strategy.domain in {"legal_regulatory", "medical"}:
            primary_bonus -= 0.10
    quality = (cred * 0.45) + (relev * 0.35) + (fresh * 0.20) + primary_bonus
    return SourceQuality(
        source_type=source_type,
        credibility=max(0.05, min(1.0, cred)),
        freshness=max(0.05, min(1.0, fresh)),
        relevance=max(0.05, min(1.0, relev)),
        quality=max(0.0, min(1.0, quality)),
        published_year=_extract_published_year(content),
    )


def _parse_json_object(raw: str) -> dict | None:
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group())
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


def _json_model(
    system: str,
    user: str,
    *,
    max_tokens: int = 1400,
    model: str | None = None,
) -> dict | None:
    """Call a structured-output model. Returns None on any failure."""
    model = model or get_settings().planner_model
    try:
        resp = completion(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.1,
            max_tokens=max_tokens,
        )
        raw = (resp.choices[0].message.content or "").strip()
        return _parse_json_object(raw)
    except Exception:
        return None


def _planned_questions_from_data(data: dict | None, fallback: list[tuple[str, str]]) -> list[PlannedQuestion]:
    if not data:
        return [PlannedQuestion(question=q, search_query=s) for q, s in fallback]
    rows = data.get("subquestions")
    if not isinstance(rows, list):
        rows = data.get("questions")
    if not isinstance(rows, list):
        return [PlannedQuestion(question=q, search_query=s) for q, s in fallback]
    planned: list[PlannedQuestion] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        question = str(row.get("question") or "").strip()
        search_query = str(row.get("search_query") or row.get("query") or "").strip()
        if not question:
            continue
        if not search_query:
            search_query = _distill_search_query(question)
        source_types = row.get("required_source_types")
        planned.append(PlannedQuestion(
            question=question[:600],
            search_query=search_query[:140],
            priority=str(row.get("priority") or "medium")[:16],
            required_source_types=source_types if isinstance(source_types, list) else None,
        ))
    return planned[:6] or [PlannedQuestion(question=q, search_query=s) for q, s in fallback]


def _llm_plan_research(query: str, mode: str) -> list[PlannedQuestion]:
    fallback = _make_questions(query, mode)
    system = """You are a senior research planner for Fronei.
Return ONLY valid JSON. No markdown.

Schema:
{
  "objective": "string",
  "domain": "string",
  "freshness_required": true,
  "subquestions": [
    {
      "question": "clear research subquestion",
      "search_query": "short keyword search query, no prose framing",
      "priority": "high|medium|low",
      "required_source_types": ["official_docs", "pricing", "release_notes", "primary_sources"]
    }
  ],
  "stopping_criteria": {
    "min_primary_sources": 2,
    "max_sources": 40,
    "max_iterations": 3
  }
}

Create 3-5 subquestions. Prefer primary/official sources for current, legal,
technical, pricing, medical, financial, immigration, or enterprise architecture topics."""
    user = f"Mode: {mode}\nResearch request:\n{query}"
    return _planned_questions_from_data(_json_model(system, user), fallback)


def _distill_search_query(text: str, max_len: int = 72) -> str:
    """Strip question preamble words to produce a concise search query.

    Input:  'What are the current pricing tiers for Claude API as of 2026? ...'
    Output: 'pricing tiers for Claude API 2026'
    """
    t = text.strip()
    # Remove leading question/instruction words
    for prefix in [
        "what are ", "what is ", "what's ", "how does ", "how do ", "how is ",
        "can you ", "please ", "i need ", "i want ", "tell me ", "explain ",
        "describe ", "compare ", "give me ", "list ", "find ",
    ]:
        if t.lower().startswith(prefix):
            t = t[len(prefix):].lstrip()
            break
    # Keep only up to the first sentence/clause break
    for sep in ["\n", "? ", ". ", "! "]:
        if sep in t:
            t = t.split(sep)[0]
            break
    return t[:max_len].rstrip(" ,;:")


_COMPLEX_MARKERS = {
    "compare", "comparison", "versus", "vs.", " vs ", "evaluate", "assessment",
    "trade-off", "trade off", "tradeoff", "pros and cons", "pros cons",
    "differences between", "which is better", "benchmark", "review",
    "enterprise", "production", "architecture", "framework", "strategy",
    "migration", "modernization", "platform", "ecosystem",
}


def _is_complex_query(query: str) -> bool:
    """Heuristic: multi-part or comparative queries need more sub-questions."""
    lower = query.lower()
    return len(query) > 200 or any(m in lower for m in _COMPLEX_MARKERS)


def _make_questions(query: str, mode: str) -> list[tuple[str, str]]:
    """Return (llm_question, search_query) pairs.

    Simple factual queries get 2 questions (direct + trade-offs).
    Complex/comparative queries get 3 questions (adds limitations angle).
    Expert mode adds a contradictions question.

    Each search_query is a short keyword phrase — no prose question framing.
    """
    cleaned = " ".join(query.split())
    core = _distill_search_query(cleaned)
    complex_query = _is_complex_query(cleaned)

    base: list[tuple[str, str]] = [
        (cleaned,
         core),
        (f"What are the main considerations, trade-offs, and caveats for: {cleaned}",
         f"{core} considerations comparison review"),
    ]

    if complex_query:
        base.append((
            f"What are the key limitations and risks for: {cleaned}",
            f"{core} limitations risks problems drawbacks",
        ))

    if _should_verify_research(mode):
        base.append((
            f"What conflicting evidence or alternatives exist for: {cleaned}",
            f"{core} alternatives criticism controversy",
        ))

    return base


def _query_variants(
    search_query: str,
    iteration: int,
    strategy: ResearchDomainStrategy | None = None,
    required_source_types: list[str] | None = None,
) -> list[str]:
    """Generate variant search queries from a concise search_query string."""
    base = search_query.strip()
    variants = [base]
    if iteration == 0:
        variants.append(f"{base} review")
        variants.append(f"{base} 2026")
    else:
        variants.append(f"{base} issues caveats")
        variants.append(f"{base} enterprise guide")

    if strategy:
        variants.extend(f"{base} {suffix}" for suffix in strategy.query_suffixes[:3])
        preferred = _preferred_source_types(strategy, required_source_types)
        if "pricing" in preferred:
            variants.append(f"{base} pricing")
        if "release_notes" in preferred:
            variants.append(f"{base} release notes")
        if "documentation" in preferred:
            variants.append(f"{base} official docs")
        if "government" in preferred:
            variants.append(f"{base} site:.gov")
        if "academic" in preferred:
            variants.append(f"{base} paper study")
        if "repository" in preferred:
            variants.append(f"{base} github")

    deduped: list[str] = []
    seen: set[str] = set()
    for variant in variants:
        key = variant.lower().strip()
        if key and key not in seen:
            seen.add(key)
            deduped.append(variant[:140])
    return deduped[:8]


def _search(query: str) -> tuple[str, list[WebSource]]:
    sources = tavily_search(query)
    if sources:
        return "Tavily", sources
    sources = brave_search(query)
    if sources:
        return "Brave", sources
    sources = ddg_search(query)
    return ("DuckDuckGo" if sources else "", sources)


def _collect_sources(query: str, direct_urls: Iterable[str], progress: Progress) -> list[tuple[str, WebSource]]:
    collected: list[tuple[str, WebSource]] = []
    seen: set[str] = set()
    for url in direct_urls:
        source = crawl_url(url)
        if source and source.url not in seen:
            seen.add(source.url)
            collected.append(("URL", source))
            progress("source_read", f"Read direct URL: {source_title(source)}", {"url": source.url})
    return collected


def source_title(source: WebSource) -> str:
    return source.title or urlparse(source.url).netloc or source.url


def _dedupe_append(target: list[tuple[str, WebSource]], provider: str, source: WebSource, seen: set[str]) -> None:
    key = source.url.split("#")[0].rstrip("/")
    if key in seen:
        return
    seen.add(key)
    target.append((provider, source))


def _host_counts(sources: Iterable[ResearchSource]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for source in sources:
        host = _host(source.url)
        if not host:
            continue
        counts[host] = counts.get(host, 0) + 1
    return counts


def _select_diverse_candidates(
    query: str,
    candidates: list[tuple[str, WebSource, int | None]],
    existing_sources: list[ResearchSource],
    strategy: ResearchDomainStrategy,
    mode: str,
    required_by_question: dict[int, list[str] | None],
    remaining_slots: int,
) -> list[tuple[str, WebSource, int | None]]:
    """Choose high-quality candidates while preventing one host from dominating."""
    if remaining_slots <= 0:
        return []
    host_counts = _host_counts(existing_sources)
    host_cap = _max_sources_per_host(mode)

    scored: list[tuple[float, bool, str, tuple[str, WebSource, int | None]]] = []
    for item in candidates:
        _provider, source, question_id = item
        required = required_by_question.get(question_id or -1)
        quality = _source_quality(
            query,
            source_title(source),
            source.url,
            source.content[:SOURCE_EXCERPT_CHARS],
            strategy,
            required,
        )
        preferred = quality.source_type in _preferred_source_types(strategy, required)
        scored.append((quality.quality, preferred, _host(source.url), item))

    scored.sort(key=lambda row: (row[1], row[0]), reverse=True)

    selected: list[tuple[str, WebSource, int | None]] = []
    for _quality, _preferred, host, item in scored:
        if len(selected) >= remaining_slots:
            break
        if host_counts.get(host, 0) >= host_cap:
            continue
        selected.append(item)
        host_counts[host] = host_counts.get(host, 0) + 1
    return selected


def _run_question_source_worker(
    *,
    question: ResearchQuestion,
    iteration: int,
    strategy: ResearchDomainStrategy,
    required_source_types: list[str] | None,
    seen_urls: set[str],
    progress: Progress | None = None,
) -> QuestionWorkerResult:
    """Search and read candidate sources for one research question."""
    question_id = question.id
    variants = _query_variants(question.search_query or question.question, iteration, strategy, required_source_types)
    if progress:
        progress("searching", f"Searching: {(question.search_query or question.question)[:70]}…", {
            "question_id": question_id,
            "variants": len(variants),
            "domain": strategy.domain,
            "preferred_sources": sorted(_preferred_source_types(strategy, required_source_types)),
        })

    raw_candidates: list[tuple[float, str, WebSource, int | None]] = []
    local_seen: set[str] = set()
    for variant in variants:
        provider, found = _search(variant)
        for source in found:
            key = source.url.split("#")[0].rstrip("/")
            if key in seen_urls or key in local_seen:
                continue
            local_seen.add(key)
            quality = _source_quality(
                question.search_query or question.question,
                source_title(source),
                source.url,
                source.content[:SOURCE_EXCERPT_CHARS],
                strategy,
                required_source_types,
            )
            raw_candidates.append((quality.quality, provider, source, question_id))

    raw_candidates.sort(key=lambda item: item[0], reverse=True)
    candidates: list[tuple[str, WebSource, int | None]] = []
    for _quality, provider, source, qid in raw_candidates[:MAX_CANDIDATES_PER_QUESTION]:
        if progress:
            progress("reading", f"Reading {source_title(source)[:80]}…", {
                "question_id": question_id,
                "url": source.url,
            })
        crawled = crawl_url(source.url)
        if crawled and len(crawled.content) > len(source.content):
            source = crawled
        candidates.append((provider, source, qid))

    return QuestionWorkerResult(question_id=question_id, candidates=candidates)


def _run_question_source_workers(
    *,
    questions: list[ResearchQuestion],
    iteration: int,
    strategy: ResearchDomainStrategy,
    required_by_question: dict[int, list[str] | None],
    seen_urls: set[str],
    progress: Progress,
) -> list[tuple[str, WebSource, int | None]]:
    if not questions:
        return []
    max_workers = min(MAX_QUESTION_WORKERS, len(questions))
    merged: list[tuple[str, WebSource, int | None]] = []
    merged_seen: set[str] = set()
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [
            pool.submit(
                _run_question_source_worker,
                question=q,
                iteration=iteration,
                strategy=strategy,
                required_source_types=required_by_question.get(q.id or -1),
                seen_urls=set(seen_urls),
                progress=progress,
            )
            for q in questions
        ]
        for future in as_completed(futures):
            try:
                result = future.result()
            except Exception:
                continue
            progress("searching", f"Question search complete.", {
                "question_id": result.question_id,
                "candidates": len(result.candidates),
            })
            for provider, source, question_id in result.candidates:
                key = source.url.split("#")[0].rstrip("/")
                if key in seen_urls or key in merged_seen:
                    continue
                merged_seen.add(key)
                merged.append((provider, source, question_id))
    return merged


def _extract_claims(query: str, source: ResearchSource) -> list[tuple[str, float]]:
    excerpt = source.excerpt or ""
    query_terms = _tokens(query)
    scored: list[tuple[str, float]] = []
    for sentence in _split_sentences(excerpt):
        terms = _tokens(sentence)
        overlap = len(query_terms & terms)
        score = min(1.0, (overlap / max(1, len(query_terms))) + source.credibility_score * 0.35)
        if overlap > 0 or score >= 0.35:
            scored.append((sentence, score))
    scored.sort(key=lambda item: item[1], reverse=True)
    return scored[:MAX_CLAIMS_PER_SOURCE]


def _claim_records_from_data(data: dict | None, fallback: list[tuple[str, float]]) -> list[ClaimRecord]:
    if not data:
        return [ClaimRecord(claim=c, score=s, quote=c[:260], confidence="medium") for c, s in fallback]
    rows = data.get("claims")
    if not isinstance(rows, list):
        return [ClaimRecord(claim=c, score=s, quote=c[:260], confidence="medium") for c, s in fallback]
    claims: list[ClaimRecord] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        claim = str(row.get("claim") or "").strip()
        if not claim:
            continue
        try:
            score = float(row.get("relevance_score", row.get("confidence_score", 0.55)))
        except (TypeError, ValueError):
            score = 0.55
        confidence = str(row.get("confidence") or "medium").lower()
        if confidence not in {"low", "medium", "high"}:
            confidence = "medium"
        quote = str(row.get("quote") or claim[:260]).strip()
        claims.append(ClaimRecord(
            claim=claim[:600],
            score=max(0.0, min(1.0, score)),
            quote=quote[:500] if quote else None,
            confidence=confidence,
        ))
    return claims[:MAX_CLAIMS_PER_SOURCE] or [
        ClaimRecord(claim=c, score=s, quote=c[:260], confidence="medium") for c, s in fallback
    ]


def _llm_extract_claim_records(query: str, source: ResearchSource) -> list[ClaimRecord]:
    fallback = _extract_claims(query, source)
    if not source.excerpt:
        return _claim_records_from_data(None, fallback)
    system = """You extract citation-grade evidence for Fronei research.
Return ONLY valid JSON. No markdown.

Schema:
{
  "claims": [
    {
      "claim": "atomic factual claim supported by the source excerpt",
      "quote": "short direct supporting excerpt",
      "confidence": "high|medium|low",
      "relevance_score": 0.0
    }
  ]
}

Rules:
- Extract at most 4 claims.
- Claims must be directly supported by the source excerpt.
- Prefer specific facts, dates, limits, capabilities, pricing, availability, caveats, or trade-offs.
- Do not invent facts not present in the excerpt."""
    user = f"""Research question:
{query}

Source title:
{source.title}

Source URL:
{source.url}

Source excerpt:
{source.excerpt[:SOURCE_EXCERPT_CHARS]}"""
    return _claim_records_from_data(
        _json_model(system, user, max_tokens=1600, model=CLAIM_EXTRACTOR_MODEL),
        fallback,
    )


def _llm_extract_claim_records_parallel(
    query: str,
    sources: list[ResearchSource],
) -> dict[int, list[ClaimRecord]]:
    """Extract claim records concurrently; DB writes stay on the caller thread."""
    sources_with_ids = [s for s in sources if s.id is not None]
    if not sources_with_ids:
        return {}
    if len(sources_with_ids) == 1:
        s = sources_with_ids[0]
        return {s.id: _llm_extract_claim_records(query, s)}

    results: dict[int, list[ClaimRecord]] = {}
    max_workers = min(MAX_CLAIM_EXTRACT_WORKERS, len(sources_with_ids))
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_to_source = {
            pool.submit(_llm_extract_claim_records, query, source): source
            for source in sources_with_ids
        }
        for future in as_completed(future_to_source):
            source = future_to_source[future]
            try:
                records = future.result()
            except Exception:
                records = _claim_records_from_data(None, _extract_claims(query, source))
            if source.id is not None:
                results[source.id] = records
    return results


def _find_gaps(questions: list[ResearchQuestion], claims_by_question: dict[int, int]) -> list[str]:
    gaps = []
    for q in questions:
        if claims_by_question.get(q.id, 0) == 0:
            gaps.append(q.question)
    return gaps[:4]


def _find_contradictions(claims: list[ResearchClaim]) -> list[str]:
    """Cheap fallback used when the LLM evaluator cannot identify conflicts."""
    contradictions: list[str] = []
    lowers = [(c.claim.lower(), c.claim) for c in claims]
    opposing = [
        ("increase", "decrease"),
        ("higher", "lower"),
        ("recommended", "not recommended"),
        ("supported", "unsupported"),
        ("secure", "insecure"),
        ("required", "optional"),
    ]
    for a, b in opposing:
        left = [raw for low, raw in lowers if a in low]
        right = [raw for low, raw in lowers if b in low]
        if left and right:
            contradictions.append(f"Potential conflict: '{a}' vs '{b}' appears across extracted claims.")
    return contradictions[:4]


def _evaluation_from_data(
    data: dict | None,
    fallback_gaps: list[str],
    fallback_contradictions: list[str],
    fallback_confidence: str,
) -> ResearchEvaluation:
    if not data:
        return ResearchEvaluation(
            gaps=fallback_gaps,
            follow_up_queries=[f"{g[:100]} 2026" for g in fallback_gaps],
            contradictions=fallback_contradictions,
            confidence=fallback_confidence,
            enough_evidence=fallback_confidence == "high",
        )
    gaps = data.get("gaps")
    if not isinstance(gaps, list):
        gaps = fallback_gaps
    followups = data.get("follow_up_queries")
    if not isinstance(followups, list):
        followups = [f"{str(g)[:100]} 2026" for g in gaps]
    contradictions = data.get("contradictions")
    if not isinstance(contradictions, list):
        contradictions = fallback_contradictions
    confidence = str(data.get("confidence") or fallback_confidence).lower()
    if confidence not in {"low", "medium", "high"}:
        confidence = fallback_confidence
    return ResearchEvaluation(
        gaps=[str(g)[:300] for g in gaps[:5]],
        follow_up_queries=[str(q)[:160] for q in followups[:6]],
        contradictions=[str(c)[:300] for c in contradictions[:5]],
        confidence=confidence,
        enough_evidence=bool(data.get("enough_evidence", confidence == "high")),
    )


def _llm_evaluate_research(
    query: str,
    questions: list[ResearchQuestion],
    sources: list[ResearchSource],
    claims: list[ResearchClaim],
    fallback_gaps: list[str],
    fallback_contradictions: list[str],
) -> ResearchEvaluation:
    fallback_confidence = _confidence(len(sources), len(claims), fallback_gaps, fallback_contradictions)
    source_rows = [
        f"- {s.title} ({s.source_type}, credibility={s.credibility_score:.2f}, relevance={s.relevance_score:.2f})"
        for s in sources[:24]
    ]
    claim_rows = [f"- {c.claim}" for c in claims[:40]]
    system = """You are a research quality evaluator for Fronei.
Return ONLY valid JSON. No markdown.

Schema:
{
  "enough_evidence": true,
  "confidence": "high|medium|low",
  "gaps": ["missing evidence or weakly supported subquestion"],
  "follow_up_queries": ["short keyword query to close a gap"],
  "contradictions": ["specific conflict or caveat across evidence"]
}

Evaluate whether the gathered evidence is enough for a citation-backed answer.
Prefer primary and official sources. Penalize stale, low-authority, or thin evidence."""
    user = f"""Original research request:
{query}

Subquestions:
{chr(10).join(f"- {q.question}" for q in questions)}

Sources:
{chr(10).join(source_rows) if source_rows else "- none"}

Extracted claims:
{chr(10).join(claim_rows) if claim_rows else "- none"}"""
    return _evaluation_from_data(
        _json_model(system, user, max_tokens=1600),
        fallback_gaps,
        fallback_contradictions,
        fallback_confidence,
    )


def _confidence(source_count: int, claim_count: int, gaps: list[str], contradictions: list[str]) -> str:
    if source_count >= 8 and claim_count >= 8 and not gaps and not contradictions:
        return "high"
    if source_count >= 4 and claim_count >= 4:
        return "medium"
    return "low"


def _question_source_counts(
    sources: list[ResearchSource],
    questions: list[ResearchQuestion],
) -> dict[int, int]:
    """Map question_id → number of quality sources covering it."""
    counts: dict[int, int] = {q.id: 0 for q in questions if q.id is not None}
    for s in sources:
        if s.question_id in counts and s.credibility_score >= MIN_CREDIBILITY_SCORE:
            counts[s.question_id] += 1
    return counts


def _is_primary_source(source: ResearchSource) -> bool:
    """Return True for official/primary-ish sources useful for grounded research."""
    source_type = source.source_type or ""
    if source_type in PRIMARY_SOURCE_TYPES:
        return source.credibility_score >= 0.55
    return source.credibility_score >= 0.78


def _question_primary_source_counts(
    sources: list[ResearchSource],
    questions: list[ResearchQuestion],
) -> dict[int, int]:
    """Map question_id → number of primary/official sources covering it."""
    counts: dict[int, int] = {q.id: 0 for q in questions if q.id is not None}
    for source in sources:
        if source.question_id in counts and _is_primary_source(source):
            counts[source.question_id] += 1
    return counts


def _min_primary_sources(mode: str) -> int:
    return MIN_PRIMARY_SOURCES_EXPERT if mode == "expert" else MIN_PRIMARY_SOURCES_DEEP


def _primary_source_gaps(
    questions: list[ResearchQuestion],
    primary_counts: dict[int, int],
    mode: str,
) -> list[str]:
    required = _min_primary_sources(mode)
    gaps: list[str] = []
    for question in questions:
        if question.id is None:
            continue
        if primary_counts.get(question.id, 0) < required:
            gaps.append(f"{question.question} (needs {required} primary/official source(s))")
    return gaps[:5]


def _question_needs_more_sources(
    q: ResearchQuestion,
    source_counts: dict[int, int],
    primary_counts: dict[int, int],
    total_sources: int,
    mode: str,
) -> bool:
    """Return True when this question still needs more sources."""
    if total_sources < MIN_TOTAL_SOURCES:
        return True
    if primary_counts.get(q.id or -1, 0) < _min_primary_sources(mode):
        return True
    return source_counts.get(q.id or -1, 0) < MIN_SOURCES_PER_QUESTION


def _build_followup_synthesis_prompt(
    original_query: str,
    follow_up_question: str,
    sources: list[ResearchSource],
    claims: list[ResearchClaim],
) -> str:
    source_lines = [
        f"[S{i}] {s.title} ({s.source_type})\nURL: {s.url}"
        for i, s in enumerate(sources[:20], 1)
    ]
    source_label = {s.id: f"S{i}" for i, s in enumerate(sources[:20], 1)}
    claim_lines = [
        f"- [{source_label.get(c.source_id, 'S?')}] {c.claim}"
        for c in claims[:40]
    ]
    return f"""You are Fronei answering a follow-up question using prior research evidence.

Prior research question:
{original_query}

Follow-up question:
{follow_up_question}

Available sources:
{chr(10).join(source_lines) if source_lines else "- None"}

Extracted evidence:
{chr(10).join(claim_lines) if claim_lines else "- None"}

Instructions:
- Answer the follow-up question specifically and concisely using the evidence above.
- Cite sources inline using [S1], [S2] etc. where the evidence supports the claim.
- If the follow-up asks about something not covered by the available evidence, clearly
  state what additional research would be needed rather than guessing.
- Do not reproduce the full prior research brief — focus only on what was asked.
- Do not fabricate numbers, capabilities, or sources not present in the evidence."""


@dataclass
class ResearchFollowupResult:
    result: LLMResult
    route: RouteDecision
    run: ResearchRun
    source_logs: list[dict]
    questions: list[str]


def run_research_followup(
    db,
    *,
    user_id: str,
    run_id: int,
    follow_up_question: str,
    profile: Profile | None,
    force_model: str | None,
    progress: Progress,
) -> ResearchFollowupResult:
    """Synthesize a follow-up answer from an existing research run's evidence.

    No new web searching. Loads already-gathered sources and claims and
    produces a focused answer. Takes 10-20 seconds instead of 3-7 minutes.
    """
    run = db.get(ResearchRun, run_id)
    if run is None or run.user_id != user_id:
        raise ValueError(f"Research run {run_id} not found or access denied")

    sources = (
        db.query(ResearchSource)
        .filter(ResearchSource.run_id == run_id)
        .order_by(
            ResearchSource.credibility_score.desc(),
            ResearchSource.relevance_score.desc(),
        )
        .limit(20)
        .all()
    )
    claims = (
        db.query(ResearchClaim)
        .filter(ResearchClaim.run_id == run_id)
        .order_by(ResearchClaim.relevance_score.desc())
        .limit(40)
        .all()
    )

    progress("planning", "Using existing research evidence…", {
        "research_run_id": run_id,
        "source_count": len(sources),
        "claim_count": len(claims),
    })

    route = choose_route(
        follow_up_question,
        profile=profile,
        force_model=force_model,
        deep_research=False,
        web_search=False,
        task_override="writing",
        complexity_override="medium",
    )

    progress("synthesising", "Synthesising follow-up from existing evidence…", {
        "research_run_id": run_id,
    })

    result = invoke_llm(
        _build_followup_synthesis_prompt(run.query, follow_up_question, sources, claims),
        route,
        deep_research=False,
        web_context=None,
        enable_native_search=False,
    )

    source_logs = [
        {
            "id": s.id, "title": s.title, "url": s.url,
            "provider": s.provider,
            "credibility_score": s.credibility_score,
            "relevance_score": s.relevance_score,
            "freshness_score": s.freshness_score,
            "source_type": s.source_type,
        }
        for s in sources
    ]

    progress("complete", "Follow-up complete.", {
        "research_run_id": run_id,
        "source_count": len(sources),
    })

    return ResearchFollowupResult(
        result=result,
        route=route,
        run=run,
        source_logs=source_logs,
        questions=[run.query],
    )


def _build_synthesis_prompt(
    query: str,
    questions: list[ResearchQuestion],
    sources: list[ResearchSource],
    claims: list[ResearchClaim],
    gaps: list[str],
    contradictions: list[str],
    mode: str,
) -> str:
    source_lines = [
        f"[S{i}] {s.title}\nURL: {s.url}\nType: {s.source_type}; credibility={s.credibility_score:.2f}; relevance={s.relevance_score:.2f}"
        for i, s in enumerate(sources, 1)
    ]
    source_label = {s.id: f"S{i}" for i, s in enumerate(sources, 1)}
    claim_lines = [
        f"- [{source_label.get(c.source_id, 'S?')}] {c.claim}"
        for c in claims[:36]
    ]
    return f"""You are Fronei running a frontier-style research synthesis.

Research mode: {mode}
Original question:
{query}

Research questions:
{chr(10).join(f"- {q.question}" for q in questions)}

Sources:
{chr(10).join(source_lines) if source_lines else "- No external sources were successfully retrieved."}

Extracted evidence:
{chr(10).join(claim_lines) if claim_lines else "- No citation-grade claims were extracted."}

Known gaps:
{chr(10).join(f"- {g}" for g in gaps) if gaps else "- None identified."}

Potential contradictions:
{chr(10).join(f"- {c}" for c in contradictions) if contradictions else "- None identified."}

Write the final answer as an executive research brief with:
1. Bottom line
2. Key findings with inline citations like [S1]
3. Trade-offs / risks / caveats
4. What is still uncertain
5. Recommended next steps
6. Source table

Do not cite unsupported claims. If evidence is thin, say so plainly."""


def _build_verifier_prompt(draft: str, claims: list[ResearchClaim], gaps: list[str], contradictions: list[str]) -> str:
    evidence = "\n".join(f"- {c.claim}" for c in claims[:40]) or "- No extracted claims."
    return f"""Review this research draft for unsupported claims, stale/currentness risk, citation mismatch, and overconfidence.

Draft:
{draft}

Evidence:
{evidence}

Known gaps:
{json.dumps(gaps)}

Potential contradictions:
{json.dumps(contradictions)}

Return concise verifier notes followed by a corrected final answer. Keep the final answer citation-backed."""


def _verification_from_data(data: dict | None, draft: str) -> CitationVerification:
    if not data:
        return CitationVerification(
            verifier_notes="Verifier could not return structured output; using synthesis draft unchanged.",
            unsupported_claims=[],
            citation_issues=[],
            stale_or_overconfident_claims=[],
            verified_answer=draft,
        )

    def _list(name: str) -> list[str]:
        value = data.get(name)
        if not isinstance(value, list):
            return []
        return [str(v)[:400] for v in value if str(v).strip()][:12]

    answer = str(data.get("verified_answer") or "").strip()
    if not answer:
        answer = draft
    notes = str(data.get("verifier_notes") or "").strip()
    unsupported = _list("unsupported_claims")
    citation_issues = _list("citation_issues")
    stale = _list("stale_or_overconfident_claims")
    if not notes:
        issue_count = len(unsupported) + len(citation_issues) + len(stale)
        notes = "Citation verifier passed with no material issues." if issue_count == 0 else (
            f"Citation verifier repaired {issue_count} issue(s)."
        )
    return CitationVerification(
        verifier_notes=notes[:2000],
        unsupported_claims=unsupported,
        citation_issues=citation_issues,
        stale_or_overconfident_claims=stale,
        verified_answer=answer,
    )


def _build_citation_verifier_prompt(
    query: str,
    draft: str,
    sources: list[ResearchSource],
    claims: list[ResearchClaim],
    gaps: list[str],
    contradictions: list[str],
    mode: str,
) -> str:
    source_label = {s.id: f"S{i}" for i, s in enumerate(sources, 1)}
    source_rows = [
        f"[S{i}] {s.title}\nURL: {s.url}\nType: {s.source_type}; credibility={s.credibility_score:.2f}; freshness={s.freshness_score:.2f}"
        for i, s in enumerate(sources, 1)
    ]
    evidence_rows = [
        f"- [{source_label.get(c.source_id, 'S?')}] claim: {c.claim}\n  quote: {c.quote or '(no quote)'}"
        for c in claims[:60]
    ]
    strictness = (
        "Use strict expert scrutiny. Remove or qualify any claim that is not directly supported."
        if mode == "expert"
        else "Use practical scrutiny. Remove or qualify material unsupported claims."
    )
    return f"""You are Fronei's citation verifier. Check whether the draft's factual
claims are directly supported by the evidence store.

Return ONLY valid JSON. No markdown.

Schema:
{{
  "verifier_notes": "short plain-English summary of what was checked and repaired",
  "unsupported_claims": ["claims in the draft not supported by evidence"],
  "citation_issues": ["citations that do not support the nearby claim or are missing"],
  "stale_or_overconfident_claims": ["claims needing date caveats, lower confidence, or uncertainty"],
  "verified_answer": "the final answer only, repaired so every non-obvious factual claim is evidence-backed"
}}

Rules:
- {strictness}
- A citation like [S1] must support the sentence it appears in.
- Do not invent new sources, URLs, numbers, dates, or capabilities.
- Preserve useful analysis, but label it as analysis when it goes beyond evidence.
- If evidence is thin or gaps remain, say so in the answer.
- Do not include verifier notes in verified_answer.

Original research request:
{query}

Known gaps:
{json.dumps(gaps)}

Potential contradictions:
{json.dumps(contradictions)}

Sources:
{chr(10).join(source_rows) if source_rows else "- none"}

Evidence store:
{chr(10).join(evidence_rows) if evidence_rows else "- none"}

Draft to verify:
{draft}"""


def _llm_verify_and_repair(
    query: str,
    draft: str,
    sources: list[ResearchSource],
    claims: list[ResearchClaim],
    gaps: list[str],
    contradictions: list[str],
    route: RouteDecision,
    mode: str,
) -> tuple[CitationVerification, LLMResult | None]:
    prompt = _build_citation_verifier_prompt(query, draft, sources, claims, gaps, contradictions, mode)
    try:
        verified = invoke_llm(
            prompt,
            route,
            deep_research=False,
            web_context=None,
            enable_native_search=False,
        )
        return _verification_from_data(_parse_json_object(verified.answer), draft), verified
    except Exception:
        return _verification_from_data(None, draft), None


def _should_verify_research(mode: str) -> bool:
    return mode == "expert"


def _claim_confidence_rank(confidence: str | None) -> int:
    return {"high": 3, "medium": 2, "low": 1}.get((confidence or "").lower(), 2)


def _persist_research_findings(
    db,
    run: ResearchRun,
    questions: list[ResearchQuestion],
    sources: list[ResearchSource],
    claims: list[ResearchClaim],
    confidence: str,
) -> None:
    """Persist compact key findings derived from the strongest extracted claims."""
    db.query(ResearchFinding).filter(ResearchFinding.run_id == run.id).delete()
    source_by_id = {s.id: s for s in sources}
    source_index = {s.id: i for i, s in enumerate(sources, 1)}
    source_question = {s.id: s.question_id for s in sources}
    used_claim_ids: set[int] = set()

    def ranked(rows: list[ResearchClaim]) -> list[ResearchClaim]:
        return sorted(
            rows,
            key=lambda c: (_claim_confidence_rank(c.confidence), c.relevance_score or 0.0),
            reverse=True,
        )

    def evidence_for(rows: list[ResearchClaim]) -> list[dict]:
        evidence: list[dict] = []
        for claim in rows[:3]:
            source = source_by_id.get(claim.source_id)
            evidence.append({
                "claim_id": claim.id,
                "source_id": claim.source_id,
                "source_ref": f"S{source_index.get(claim.source_id, '?')}",
                "source_title": source.title if source else None,
                "source_url": source.url if source else None,
                "quote": claim.quote,
            })
            if claim.id is not None:
                used_claim_ids.add(claim.id)
        return evidence

    findings: list[ResearchFinding] = []
    for question in questions:
        q_claims = ranked([c for c in claims if source_question.get(c.source_id) == question.id])
        if not q_claims:
            continue
        top = q_claims[0]
        findings.append(ResearchFinding(
            run_id=run.id,
            finding=top.claim,
            evidence_json=json.dumps(evidence_for(q_claims)),
            confidence=top.confidence or confidence,
            created_at=_now(),
        ))
        if len(findings) >= 8:
            break

    if len(findings) < 5:
        for claim in ranked(claims):
            if claim.id in used_claim_ids:
                continue
            findings.append(ResearchFinding(
                run_id=run.id,
                finding=claim.claim,
                evidence_json=json.dumps(evidence_for([claim])),
                confidence=claim.confidence or confidence,
                created_at=_now(),
            ))
            if len(findings) >= 8:
                break

    for finding in findings:
        db.add(finding)
    db.commit()


def run_research(
    db,
    *,
    user_id: str,
    conversation_id: int | None,
    query: str,
    profile: Profile | None,
    force_model: str | None,
    mode: str,
    progress: Progress,
) -> ResearchPipelineResult:
    started = time.perf_counter()
    mode = "expert" if mode == "expert" else "deep"
    run = ResearchRun(
        user_id=user_id,
        conversation_id=conversation_id,
        query=query,
        mode=mode,
        status="running",
        max_sources=HARD_MAX_SOURCES,
        created_at=_now(),
        updated_at=_now(),
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    try:
        return _run_pipeline(db, run=run, query=query, profile=profile,
                             force_model=force_model, mode=mode,
                             planned_questions=_llm_plan_research(query, mode),
                             started=started, progress=progress)
    except BaseException:
        try:
            run.status = "failed"
            run.updated_at = _now()
            db.commit()
        except Exception:
            pass
        raise


def _run_pipeline(
    db,
    *,
    run: ResearchRun,
    query: str,
    profile,
    force_model,
    mode: str,
    planned_questions: list[PlannedQuestion],
    started: float,
    progress: Progress,
) -> "ResearchPipelineResult":
    strategy = _research_domain_strategy(query)

    progress("planning", "Planning research questions…", {
        "research_run_id": run.id,
        "mode": mode,
        "domain": strategy.domain,
        "source_strategy": strategy.primary_source_hint,
        "questions": [p.question for p in planned_questions],
    })
    questions: list[ResearchQuestion] = []
    for planned in planned_questions:
        q = ResearchQuestion(
            run_id=run.id,
            question=planned.question,
            search_query=planned.search_query,   # concise keyword query, not the prose question
            created_at=_now(),
        )
        db.add(q)
        questions.append(q)
    db.commit()
    for q in questions:
        db.refresh(q)
    question_required_source_types: dict[int, list[str] | None] = {
        q.id: planned.required_source_types
        for q, planned in zip(questions, planned_questions)
        if q.id is not None
    }

    all_sources: list[ResearchSource] = []
    seen_urls: set[str] = set()
    direct = _collect_sources(query, find_urls(query), progress)
    for provider, source in direct:
        key = source.url.split("#")[0].rstrip("/")
        if key in seen_urls:
            continue
        seen_urls.add(key)
        excerpt = source.content[:SOURCE_EXCERPT_CHARS]
        quality = _source_quality(query, source_title(source), source.url, excerpt, strategy)
        db.add(ResearchSource(
            run_id=run.id,
            question_id=None,
            title=source_title(source),
            url=source.url,
            provider=provider,
            excerpt=excerpt,
            credibility_score=quality.credibility,
            relevance_score=quality.relevance,
            freshness_score=quality.freshness,
            source_type=quality.source_type,
            created_at=_now(),
        ))
    db.commit()
    all_sources = db.query(ResearchSource).filter(ResearchSource.run_id == run.id).all()

    for iteration in range(MAX_ITERATIONS):
        run.iterations = iteration + 1
        db.commit()
        progress("searching", f"Search pass {iteration + 1}…", {"iteration": iteration + 1})

        # Rule: compute per-question source coverage before deciding what to search
        q_source_counts = _question_source_counts(all_sources, questions)
        q_primary_counts = _question_primary_source_counts(all_sources, questions)
        required_by_question = question_required_source_types

        remaining_slots = HARD_MAX_SOURCES - len(all_sources)
        if remaining_slots <= 0:
            break
        eligible_questions = [
            q for q in questions
            if _question_needs_more_sources(q, q_source_counts, q_primary_counts, len(all_sources), mode)
        ]
        if eligible_questions:
            progress("searching", f"Running {len(eligible_questions)} question worker(s) in parallel…", {
                "iteration": iteration + 1,
                "workers": min(MAX_QUESTION_WORKERS, len(eligible_questions)),
            })
        candidate_sources = _run_question_source_workers(
            questions=eligible_questions,
            iteration=iteration,
            strategy=strategy,
            required_by_question=required_by_question,
            seen_urls=seen_urls,
            progress=progress,
        )

        candidate_sources = _select_diverse_candidates(
            query,
            candidate_sources,
            all_sources,
            strategy,
            mode,
            required_by_question,
            remaining_slots,
        )
        for _provider, selected_source, _question_id in candidate_sources:
            seen_urls.add(selected_source.url.split("#")[0].rstrip("/"))

        for provider, web_source, question_id in candidate_sources:
            excerpt = web_source.content[:SOURCE_EXCERPT_CHARS]
            required_source_types = question_required_source_types.get(question_id or -1)
            quality = _source_quality(
                query, source_title(web_source), web_source.url, excerpt,
                strategy, required_source_types,
            )
            # Rule 3 — quality floor: discard low-signal sources
            if quality.quality < MIN_CREDIBILITY_SCORE:
                continue
            source = ResearchSource(
                run_id=run.id,
                question_id=question_id,
                title=source_title(web_source),
                url=web_source.url,
                provider=provider,
                excerpt=excerpt,
                credibility_score=quality.credibility,
                relevance_score=quality.relevance,
                freshness_score=quality.freshness,
                source_type=quality.source_type,
                created_at=_now(),
            )
            db.add(source)
            all_sources.append(source)
        db.commit()
        for source in all_sources:
            if source.id is None:
                db.refresh(source)

        progress("extracting", f"Extracting claims from {len(all_sources)} sources…", {"source_count": len(all_sources)})
        existing_source_ids = {c.source_id for c in db.query(ResearchClaim).filter(ResearchClaim.run_id == run.id).all()}
        pending_extract_sources = [source for source in all_sources if source.id not in existing_source_ids]
        claim_records_by_source = _llm_extract_claim_records_parallel(query, pending_extract_sources)
        for source in pending_extract_sources:
            if source.id is None:
                continue
            for record in claim_records_by_source.get(source.id, []):
                db.add(ResearchClaim(
                    run_id=run.id,
                    source_id=source.id,
                    claim=record.claim,
                    quote=record.quote or record.claim[:260],
                    confidence=record.confidence,
                    relevance_score=record.score,
                    created_at=_now(),
                ))
        db.commit()

        claims = db.query(ResearchClaim).filter(ResearchClaim.run_id == run.id).all()
        claims_by_question: dict[int, int] = {}
        for source in all_sources:
            if source.question_id:
                source_claims = [c for c in claims if c.source_id == source.id]
                claims_by_question[source.question_id] = claims_by_question.get(source.question_id, 0) + len(source_claims)
        fallback_gaps = _find_gaps(questions, claims_by_question)
        primary_gaps = _primary_source_gaps(questions, _question_primary_source_counts(all_sources, questions), mode)
        fallback_gaps = [*fallback_gaps, *[g for g in primary_gaps if g not in fallback_gaps]][:5]
        fallback_contradictions = _find_contradictions(claims)
        evaluation = _llm_evaluate_research(
            query, questions, all_sources, claims,
            fallback_gaps=fallback_gaps,
            fallback_contradictions=fallback_contradictions,
        )
        gaps = [*evaluation.gaps, *[g for g in primary_gaps if g not in evaluation.gaps]][:5]
        progress("checking", f"Checking gaps after pass {iteration + 1}…", {
            "gaps": gaps,
            "confidence": evaluation.confidence,
        })

        # Rule 4 — confidence-based early exit: stop if coverage is already high
        if not gaps and (evaluation.enough_evidence or evaluation.confidence == "high"):
            progress("checking", "Confidence is high — stopping early.", {})
            break
        if not gaps or iteration == MAX_ITERATIONS - 1:
            break
        followups = evaluation.follow_up_queries or [f"{gap[:100]} 2026" for gap in gaps]
        for gap, followup in zip(gaps, followups):
            questions.append(ResearchQuestion(
                run_id=run.id,
                question=gap,
                search_query=followup,
                status="follow_up",
                created_at=_now(),
            ))
            db.add(questions[-1])
        db.commit()
        for q in questions:
            if q.id is None:
                db.refresh(q)
            if q.id is not None and q.id not in question_required_source_types:
                question_required_source_types[q.id] = strategy.preferred_source_types

    sources = db.query(ResearchSource).filter(ResearchSource.run_id == run.id).order_by(
        ResearchSource.credibility_score.desc(), ResearchSource.relevance_score.desc()
    ).all()
    claims = db.query(ResearchClaim).filter(ResearchClaim.run_id == run.id).order_by(
        ResearchClaim.relevance_score.desc()
    ).all()
    claims_by_question = {}
    for source in sources:
        if source.question_id:
            claims_by_question[source.question_id] = claims_by_question.get(source.question_id, 0) + len([c for c in claims if c.source_id == source.id])
    fallback_gaps = _find_gaps(questions, claims_by_question)
    primary_gaps = _primary_source_gaps(questions, _question_primary_source_counts(sources, questions), mode)
    fallback_gaps = [*fallback_gaps, *[g for g in primary_gaps if g not in fallback_gaps]][:5]
    fallback_contradictions = _find_contradictions(claims)
    final_evaluation = _llm_evaluate_research(
        query, questions, sources, claims,
        fallback_gaps=fallback_gaps,
        fallback_contradictions=fallback_contradictions,
    )
    gaps = [*final_evaluation.gaps, *[g for g in primary_gaps if g not in final_evaluation.gaps]][:5]
    contradictions = final_evaluation.contradictions
    confidence = final_evaluation.confidence
    _persist_research_findings(db, run, questions, sources, claims, confidence)
    run.source_count = len(sources)
    run.claim_count = len(claims)
    run.gaps_json = json.dumps(gaps)
    run.contradictions_json = json.dumps(contradictions)
    run.confidence = confidence
    run.updated_at = _now()
    db.commit()

    progress("synthesising", f"Synthesising {len(sources)} sources and {len(claims)} claims…", {
        "source_count": len(sources),
        "claim_count": len(claims),
        "confidence": confidence,
    })

    # Synthesis is a writing/reasoning task — pick a model capable of
    # producing a long structured brief, not a live-search model.
    route = choose_route(
        query,
        profile=profile,
        force_model=force_model,
        deep_research=False,
        web_search=False,
        task_override="writing",
        complexity_override="high",
        preferred_model="claude-sonnet-4-6",
    )
    draft = invoke_llm(
        _build_synthesis_prompt(query, questions, sources, claims, gaps, contradictions, mode),
        route,
        deep_research=False,          # avoid DEEP_RESEARCH_SYSTEM_PROMPT which warns about
        web_context=None,             # "no live web access" — the evidence is already in the prompt
        enable_native_search=False,
    )
    result = draft
    verifier_notes = None
    final_answer = draft.answer

    if mode == "expert":
        progress("verifying", "Verifying citation support…", {"research_run_id": run.id})
        verification, verified = _llm_verify_and_repair(
            query, draft.answer, sources, claims, gaps, contradictions, route, mode,
        )
        verifier_notes = json.dumps({
            "notes": verification.verifier_notes,
            "unsupported_claims": verification.unsupported_claims,
            "citation_issues": verification.citation_issues,
            "stale_or_overconfident_claims": verification.stale_or_overconfident_claims,
        })
        final_answer = verification.verified_answer
        if verified is not None:
            result = LLMResult(
                answer=final_answer,
                model_used=verified.model_used,
                latency_ms=draft.latency_ms + verified.latency_ms,
                prompt_tokens=(draft.prompt_tokens or 0) + (verified.prompt_tokens or 0),
                completion_tokens=(draft.completion_tokens or 0) + (verified.completion_tokens or 0),
                estimated_cost_usd=(draft.estimated_cost_usd or 0.0) + (verified.estimated_cost_usd or 0.0),
            )
        else:
            result = LLMResult(
                answer=final_answer,
                model_used=draft.model_used,
                latency_ms=draft.latency_ms,
                prompt_tokens=draft.prompt_tokens,
                completion_tokens=draft.completion_tokens,
                estimated_cost_usd=draft.estimated_cost_usd,
                fallback_errors=draft.fallback_errors,
            )
    else:
        progress("verifying", "Skipping verifier for deep mode.", {"research_run_id": run.id})

    run.status = "complete"
    run.verifier_notes = verifier_notes
    run.final_answer = final_answer
    run.updated_at = _now()
    db.commit()
    db.refresh(run)

    source_logs = [
        {
            "id": s.id,
            "title": s.title,
            "url": s.url,
            "provider": s.provider,
            "credibility_score": s.credibility_score,
            "relevance_score": s.relevance_score,
            "freshness_score": s.freshness_score,
            "source_type": s.source_type,
        }
        for s in sources
    ]
    source_by_id = {s.id: s for s in sources}
    source_index = {s.id: i for i, s in enumerate(sources, 1)}
    claim_logs = [
        {
            "id": c.id,
            "claim": c.claim,
            "quote": c.quote,
            "confidence": c.confidence,
            "relevance_score": c.relevance_score,
            "source_id": c.source_id,
            "source_ref": f"S{source_index.get(c.source_id, '?')}",
            "source_title": source_by_id.get(c.source_id).title if source_by_id.get(c.source_id) else None,
            "source_url": source_by_id.get(c.source_id).url if source_by_id.get(c.source_id) else None,
        }
        for c in claims[:80]
    ]
    progress("complete", "Research complete.", {
        "research_run_id": run.id,
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
        "sources": source_logs,
        "claims": claim_logs,
    })
    return ResearchPipelineResult(
        run=run,
        result=result,
        route=route,
        source_logs=source_logs,
        questions=[q.question for q in questions],
        gaps=gaps,
        contradictions=contradictions,
        verifier_notes=verifier_notes,
        claim_logs=claim_logs,
    )
