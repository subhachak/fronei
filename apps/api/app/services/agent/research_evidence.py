"""research_evidence.py — Evidence binding, claim extraction, and passage scoring.

Responsibilities:
  - bind_evidence: wraps raw sources into a typed EvidencePack
  - extract_evidence_claims: typed claim extraction with scoring
  - extract_architecture_cards: AgentDeck card extraction from evidence
  - Passage selection and scoring helpers
  - detect_contradictions: simple contradiction signal

Extracted from research_subtree.py (TD-01).
"""
from __future__ import annotations

import logging
import re
from typing import Literal
from urllib.parse import urlparse

from app.services.agent.models import Source
from app.services.agent.research_models import (
    ArchitectureExtractionCard,
    CoverageContract,
    EvidenceClaim,
    EvidenceItem,
    EvidencePack,
    ResearchPlan,
)
from app.services.agent.research_planner import (
    _meaningful_tokens,
    _text_supports_cell,
)
from app.services.agent.research_utils import (
    _dedupe,
    _estimate_relevance,
    _looks_like_substantive_claim,
    classify_source_type,
    score_source_authority,
    score_technical_density,
)

logger = logging.getLogger(__name__)

def bind_evidence(
    sources: list[Source],
    plan: ResearchPlan | None = None,
    max_items: int = 8,
    contract: CoverageContract | None = None,
) -> EvidencePack:
    seen: set[str] = set()
    items: list[EvidenceItem] = []
    questions = plan.questions if plan else []
    profile = plan.research_profile if plan else "general"
    for source in sources:
        if not source.url or source.url in seen:
            continue
        seen.add(source.url)
        body = (source.content or source.snippet or "").strip()
        if not body:
            continue
        source_type = classify_source_type(url=source.url)
        # Evidence body cap: academic papers and repos are the richest technical
        # sources — give them more room so synthesis has dense material to work with.
        # Generic web pages are capped lower to avoid diluting the context.
        if profile == "technical_architecture" and source_type in {"academic", "pdf"}:
            body_cap = 7000
        elif profile == "technical_architecture" and source_type in {"repository", "documentation"}:
            body_cap = 5600
        elif profile == "technical_architecture":
            body_cap = 3800
        elif source_type in {"academic", "repository"}:
            body_cap = 3200
        elif source_type in {"documentation", "pdf"}:
            body_cap = 2400
        else:
            body_cap = 900
        passages = _select_evidence_passages(
            source,
            body,
            plan=plan,
            contract=contract,
            body_cap=body_cap,
            max_passages=3 if profile == "technical_architecture" else 1,
        )
        for passage in passages:
            source_id = f"S{len(items) + 1}"
            items.append(
                EvidenceItem(
                    source_id=source_id,
                    question=questions[(len(items) % len(questions))] if questions else "",
                    title=source.title,
                    url=source.url,
                    source_type=source_type,
                    evidence=passage["text"],
                    relevance=max(_estimate_relevance(source, questions), float(passage["score"])),
                    confidence=_passage_confidence(source, passage_score=float(passage["score"])),
                    authority=score_source_authority(source.url),
                    supports_cells=list(passage["cell_ids"]),
                    quoted_text=str(passage["text"])[:500],
                    query=source.query,
                    provider=source.provider,
                )
            )
            if len(items) >= max_items:
                break
        if len(items) >= max_items:
            break
    min_items = plan.min_evidence_items if plan else 1
    coverage = min(1.0, len(items) / max(1, min_items))
    gaps = [] if len(items) >= min_items else [f"Only {len(items)} usable evidence item(s); target is {min_items}."]
    contradictions = detect_contradictions(items)
    pack = EvidencePack(items=items, coverage=coverage, gaps=gaps, contradictions=contradictions)
    pack.claims = extract_evidence_claims(pack, plan=plan)
    pack.architecture_cards = extract_architecture_cards(pack, plan=plan)
    return pack


def extract_evidence_claims(
    evidence: EvidencePack,
    *,
    plan: ResearchPlan | None = None,
    max_claims_per_item: int = 3,
) -> list[EvidenceClaim]:
    claims: list[EvidenceClaim] = []
    query_terms = _claim_query_terms(plan)
    for item in evidence.items:
        item_claim_limit = _max_claims_for_item(item, plan, default=max_claims_per_item)
        candidates: list[tuple[float, str]] = []
        for sentence in _claim_candidate_sentences(item.evidence):
            score = _score_claim_sentence(sentence, item, query_terms=query_terms, plan=plan)
            if score <= 0:
                continue
            candidates.append((score, sentence))
        candidates.sort(key=lambda pair: pair[0], reverse=True)
        for score, sentence in candidates[:item_claim_limit]:
            claims.append(
                EvidenceClaim(
                    source_id=item.source_id,
                    text=sentence[:650],
                    quote=sentence[:500],
                    claim_type=_claim_type_for_text(sentence),
                    claim_role=_claim_role_for_text(sentence, item),
                    freshness_risk=_freshness_risk_for_text(sentence),
                    confidence=max(0.35, min(0.94, item.confidence + min(0.20, score * 0.05))),
                    source_title=item.title,
                    source_url=item.url,
                )
            )
    claims.sort(key=lambda claim: (claim.confidence, _claim_type_priority(claim.claim_type)), reverse=True)
    max_claims = 80 if plan and plan.research_profile == "technical_architecture" else 32
    return claims[:max_claims]


def _max_claims_for_item(item: EvidenceItem, plan: ResearchPlan | None, *, default: int) -> int:
    if not plan:
        return default
    if plan.research_profile == "technical_architecture":
        if item.source_type in {"academic", "repository"}:
            return 7
        if item.source_type in {"documentation", "pdf"}:
            return 6
        return 5
    if item.source_type in {"academic", "repository", "documentation", "pdf"}:
        return max(default, 5)
    return default


def extract_architecture_cards(
    evidence: EvidencePack,
    *,
    plan: ResearchPlan | None = None,
    max_cards: int = 18,
) -> list[ArchitectureExtractionCard]:
    if not plan or plan.research_profile != "technical_architecture":
        return []
    cards: list[ArchitectureExtractionCard] = []
    for item in evidence.items:
        text = item.evidence or ""
        system = _architecture_system_name(item, text)
        card = ArchitectureExtractionCard(
            system=system,
            source_id=item.source_id,
            source_title=item.title,
            source_url=item.url,
            architecture_pattern=_extract_architecture_pattern(text),
            agent_roles=_extract_architecture_terms(text, _AGENT_ROLE_TERMS, limit=8),
            state_objects=_extract_architecture_terms(text, _STATE_OBJECT_TERMS, limit=8),
            tools_or_renderers=_extract_architecture_terms(text, _TOOL_RENDERER_TERMS, limit=8),
            validation_loop=_extract_validation_loop(text),
            failure_modes=_extract_architecture_terms(text, _FAILURE_MODE_TERMS, limit=8),
            metrics=_extract_metric_snippets(text),
            lesson_for_agentdeck=_lesson_for_agentdeck(item, text),
            quote=_best_architecture_quote(text),
            confidence=_architecture_card_confidence(item, text),
        )
        if _architecture_card_has_signal(card):
            cards.append(card)
    cards.sort(key=lambda card: card.confidence, reverse=True)
    return cards[:max_cards]


_AGENT_ROLE_TERMS = [
    "orchestrator",
    "lead agent",
    "planner",
    "researcher",
    "worker",
    "subagent",
    "writer",
    "critic",
    "reviewer",
    "verifier",
    "citation agent",
    "formatter",
    "layout agent",
    "executor",
]

_STATE_OBJECT_TERMS = [
    "outline",
    "research brief",
    "coverage contract",
    "state graph",
    "memory",
    "scratchpad",
    "evidence pack",
    "citation map",
    "schema",
    "json",
    "slide spec",
    "render plan",
    "theme",
    "design tokens",
]

_TOOL_RENDERER_TERMS = [
    "pptxgenjs",
    "python-pptx",
    "python-docx",
    "openpyxl",
    "html",
    "css",
    "soffice",
    "pdftoppm",
    "vlm",
    "vision model",
    "mcp",
    "langgraph",
    "rag",
    "github",
]

_FAILURE_MODE_TERMS = [
    "hallucination",
    "overflow",
    "overlap",
    "truncation",
    "invalid json",
    "invalid code",
    "corrupt",
    "latency",
    "cost",
    "context",
    "incoherent",
    "disjoint",
    "security",
    "sandbox",
]


def _architecture_system_name(item: EvidenceItem, text: str) -> str:
    haystack = f"{item.title} {item.url} {text}".lower()
    known = [
        "AgentDeck",
        "PPTAgent",
        "AutoPresent",
        "STORM",
        "LongWriter",
        "AgentWrite",
        "SlideBot",
        "PPTEval",
        "PaperFit",
        "LangGraph",
        "Open Deep Research",
        "Gamma",
        "Microsoft Copilot",
        "Google Gemini",
        "Anthropic",
        "PptxGenJS",
        "Presenton",
        "MASFactory",
    ]
    for name in known:
        if name.lower() in haystack:
            return name
    host = urlparse(item.url or "").netloc.lower().replace("www.", "")
    return host or item.title[:80] or "Unknown system"


def _extract_architecture_pattern(text: str) -> str:
    candidates = _claim_candidate_sentences(text)
    pattern_terms = ("architecture", "orchestr", "workflow", "pipeline", "plan", "render", "critique", "revise", "agent")
    for sentence in candidates:
        lower = sentence.lower()
        if any(term in lower for term in pattern_terms):
            return sentence[:500]
    return candidates[0][:500] if candidates else ""


def _extract_architecture_terms(text: str, terms: list[str], *, limit: int) -> list[str]:
    lower = (text or "").lower()
    found = [term for term in terms if term in lower]
    return _dedupe(found)[:limit]


def _extract_validation_loop(text: str) -> str:
    candidates = _claim_candidate_sentences(text)
    validation_terms = ("validate", "verification", "verify", "judge", "critic", "render", "inspect", "qa", "feedback", "repair")
    for sentence in candidates:
        if any(term in sentence.lower() for term in validation_terms):
            return sentence[:500]
    return ""


def _extract_metric_snippets(text: str) -> list[str]:
    snippets: list[str] = []
    for sentence in _claim_candidate_sentences(text):
        lower = sentence.lower()
        if re.search(r"\b\d+(?:\.\d+)?\s*(?:%|percent|x|×|tokens|pearson|score|seconds|minutes|ms|calls)\b", lower):
            snippets.append(sentence[:300])
        elif any(term in lower for term in ("benchmark", "correlation", "evaluation", "outperform", "preferred by humans")):
            snippets.append(sentence[:300])
        if len(snippets) >= 5:
            break
    return snippets


def _lesson_for_agentdeck(item: EvidenceItem, text: str) -> str:
    lower = f"{item.title} {item.url} {text}".lower()
    if any(term in lower for term in ("overflow", "overlap", "render", "vision", "vlm", "pdftoppm", "soffice")):
        return "Use render-then-inspect QA with element-level repair before publishing."
    if any(term in lower for term in ("schema", "json", "structured output", "grammar", "validation")):
        return "Keep a schema-validated content contract before rendering."
    if any(term in lower for term in ("orchestrator", "subagent", "worker", "parallel")):
        return "Use a lead-agent plan with bounded specialist workers and a shared spine."
    if any(term in lower for term in ("theme", "brand", "design token", "template")):
        return "Treat the design system as a versioned contract, not a prompt hint."
    if any(term in lower for term in ("citation", "ground", "source", "rag")):
        return "Bind claims to source evidence before synthesis and verify citations after drafting."
    return "Extract the reusable architectural mechanism and map it to AgentDeck's pipeline."


def _best_architecture_quote(text: str) -> str:
    scored: list[tuple[float, str]] = []
    for sentence in _claim_candidate_sentences(text):
        score = 0.0
        lower = sentence.lower()
        if any(term in lower for term in ("architecture", "workflow", "orchestr", "schema", "render", "verify", "agent")):
            score += 2.0
        if re.search(r"\b\d+(?:\.\d+)?\s*(?:%|percent|x|×|tokens|pearson|score)\b", lower):
            score += 2.0
        if any(term in lower for term in ("implementation", "component", "state", "tool", "validation")):
            score += 1.0
        scored.append((score, sentence[:500]))
    scored.sort(key=lambda item: item[0], reverse=True)
    return scored[0][1] if scored and scored[0][0] > 0 else ""


def _architecture_card_confidence(item: EvidenceItem, text: str) -> float:
    signals = 0
    lower = text.lower()
    for terms in (_AGENT_ROLE_TERMS, _STATE_OBJECT_TERMS, _TOOL_RENDERER_TERMS, _FAILURE_MODE_TERMS):
        if any(term in lower for term in terms):
            signals += 1
    if _extract_metric_snippets(text):
        signals += 1
    return max(0.35, min(0.94, item.confidence + signals * 0.06 + score_technical_density(Source(title=item.title, url=item.url, content=text)) * 0.12))


def _architecture_card_has_signal(card: ArchitectureExtractionCard) -> bool:
    return bool(
        card.architecture_pattern
        or card.agent_roles
        or card.state_objects
        or card.tools_or_renderers
        or card.validation_loop
        or card.metrics
    )


def _claim_candidate_sentences(text: str) -> list[str]:
    normalized = re.sub(r"\s+", " ", text or "").strip()
    if not normalized:
        return []
    parts = [part.strip() for part in re.split(r"(?<=[.!?])\s+", normalized) if part.strip()]
    candidates: list[str] = []
    for part in parts:
        if 45 <= len(part) <= 520 and _looks_like_substantive_claim(part):
            candidates.append(part)
    if candidates:
        return candidates
    return [part for part in parts if 45 <= len(part) <= 520][:4]


def _claim_query_terms(plan: ResearchPlan | None) -> set[str]:
    if not plan:
        return set()
    text = " ".join(
        [
            plan.research_profile,
            *plan.questions,
            *plan.search_queries,
            *[worker.question for worker in plan.workers],
            *[worker.query for worker in plan.workers],
        ]
    )
    return set(_meaningful_tokens(text))


def _score_claim_sentence(
    sentence: str,
    item: EvidenceItem,
    *,
    query_terms: set[str],
    plan: ResearchPlan | None,
) -> float:
    lower = sentence.lower()
    query_hits = sum(1 for term in query_terms if term in lower)
    score = min(10.0, query_hits * 0.65)
    if re.search(r"\b\d+(?:\.\d+)?\s*(?:%|percent|ms|seconds|minutes|hours|days|tokens|calls|usd|\$)\b", lower):
        score += 2.2
    if any(term in lower for term in ("architecture", "orchestr", "workflow", "pipeline", "runtime", "state", "memory", "tool", "agent")):
        score += 1.8
    if any(term in lower for term in ("implementation", "data model", "schema", "queue", "trace", "event", "budget", "guardrail")):
        score += 1.8
    if any(term in lower for term in ("trade-off", "tradeoff", "latency", "cost", "failure", "risk", "limitation", "recovery")):
        score += 1.4
    if item.source_type in {"academic", "repository", "documentation", "pdf"}:
        score += 0.8
    if plan and plan.research_profile == "technical_architecture":
        score += score_technical_density(Source(title=item.title, url=item.url, content=sentence)) * 3.0
    return score


def _claim_type_for_text(text: str) -> str:
    lower = text.lower()
    if any(term in lower for term in ("architecture", "orchestr", "workflow", "pipeline", "component", "topology")):
        return "architecture"
    if any(term in lower for term in ("implementation", "schema", "data model", "queue", "state", "trace", "runtime")):
        return "implementation"
    if any(term in lower for term in ("trade-off", "tradeoff", "latency", "cost", "overhead", "performance")):
        return "tradeoff"
    if any(term in lower for term in ("fail", "failure", "risk", "limitation", "error", "recover", "timeout")):
        return "failure"
    if re.search(r"\b\d+(?:\.\d+)?\s*(?:%|percent|ms|seconds|minutes|hours|days|tokens|calls|\$|usd)\b", lower):
        return "statistic"
    if any(term in lower for term in ("price", "pricing", "costs", "plan", "$")):
        return "price"
    if any(term in lower for term in ("supports", "provides", "offers", "enables", "can ")):
        return "capability"
    if any(term in lower for term in ("according to", "argues", "suggests", "proposes", "observes")):
        return "interpretation"
    if any(term in lower for term in ("must", "required", "policy", "compliance", "shall")):
        return "policy"
    return "unknown"


def _claim_role_for_text(text: str, item: EvidenceItem) -> str:
    lower = text.lower()
    if item.source_type in {"academic", "repository", "documentation"} and any(
        term in lower for term in ("implementation", "schema", "workflow", "runtime", "architecture", "pipeline")
    ):
        return "technical_design"
    if any(term in lower for term in ("implementation", "code", "schema", "api", "runtime", "trace")):
        return "implementation_detail"
    if any(term in lower for term in ("benchmark", "study", "%", "percent", "measured", "dataset")):
        return "statistical_data"
    if any(term in lower for term in ("according to", "argues", "suggests", "we propose")):
        return "expert_interpretation"
    if item.source_type in {"primary", "documentation"}:
        return "official_policy"
    return "background_context"


def _freshness_risk_for_text(text: str) -> Literal["low", "medium", "high", "unknown"]:
    lower = text.lower()
    if re.search(r"\b20(?:2[4-9]|3\d)\b", lower) or any(term in lower for term in ("latest", "current", "recent")):
        return "low"
    if re.search(r"\b20(?:1\d|2[0-3])\b", lower):
        return "medium"
    return "unknown"


def _claim_type_priority(claim_type: str) -> int:
    return {
        "implementation": 7,
        "architecture": 7,
        "tradeoff": 6,
        "failure": 6,
        "statistic": 5,
        "policy": 4,
        "capability": 3,
        "interpretation": 2,
    }.get(claim_type, 1)


def _select_evidence_passages(
    source: Source,
    body: str,
    *,
    plan: ResearchPlan | None,
    contract: CoverageContract | None,
    body_cap: int,
    max_passages: int,
) -> list[dict[str, object]]:
    passages = _candidate_passages(body, max_chars=body_cap)
    if not passages:
        return [{"text": body[:body_cap], "score": 0.5, "cell_ids": []}]
    scored: list[dict[str, object]] = []
    for index, passage in enumerate(passages):
        score = _score_passage(source, passage, plan=plan, contract=contract)
        cell_ids = [
            cell.cell_id
            for cell in (contract.cells if contract else [])
            if _text_supports_cell(f"{source.title} {source.url} {passage}", cell)
        ]
        scored.append({"text": passage[:body_cap], "score": score, "cell_ids": cell_ids, "index": index})
    scored.sort(key=lambda item: (float(item["score"]), -int(item["index"])), reverse=True)
    selected: list[dict[str, object]] = []
    selected_signatures: set[str] = set()
    for passage in scored:
        signature = _passage_signature(str(passage["text"]))
        if signature in selected_signatures:
            continue
        selected.append(passage)
        selected_signatures.add(signature)
        if len(selected) >= max_passages:
            break
    if not selected:
        selected = [scored[0]]
    return selected


def _candidate_passages(body: str, *, max_chars: int) -> list[str]:
    text = re.sub(r"\s+", " ", body or "").strip()
    if not text:
        return []
    raw_parts = [part.strip() for part in re.split(r"(?:\n\s*){2,}", body) if part.strip()]
    if len(raw_parts) >= 2:
        passages: list[str] = []
        for part in raw_parts:
            normalized = re.sub(r"\s+", " ", part).strip()
            if len(normalized) > max_chars:
                passages.extend(_chunk_long_passage(normalized, max_chars=max_chars))
            elif normalized:
                passages.append(normalized)
        return [passage for passage in passages if len(passage) >= 60] or [text[:max_chars]]
    if len(raw_parts) <= 1:
        raw_parts = [part.strip() for part in re.split(r"(?<=[.!?])\s+", text) if part.strip()]
    passages: list[str] = []
    current = ""
    target_chars = max(650, min(max_chars, 1400))
    for part in raw_parts:
        normalized = re.sub(r"\s+", " ", part).strip()
        if not normalized:
            continue
        if len(normalized) > max_chars:
            for chunk in _chunk_long_passage(normalized, max_chars=max_chars):
                passages.append(chunk)
            current = ""
            continue
        if current and len(current) + len(normalized) + 1 > target_chars:
            passages.append(current)
            current = normalized
        else:
            current = f"{current} {normalized}".strip()
    if current:
        passages.append(current)
    return [passage for passage in passages if len(passage) >= 80] or [text[:max_chars]]


def _chunk_long_passage(text: str, *, max_chars: int) -> list[str]:
    chunks: list[str] = []
    start = 0
    stride = max(500, max_chars - 250)
    while start < len(text):
        chunk = text[start : start + max_chars].strip()
        if chunk:
            chunks.append(chunk)
        start += stride
    return chunks


def _score_passage(
    source: Source,
    passage: str,
    *,
    plan: ResearchPlan | None,
    contract: CoverageContract | None,
) -> float:
    haystack = f"{source.title} {source.url} {passage}".lower()
    query_text = " ".join(
        [
            *(plan.questions if plan else []),
            *(plan.search_queries if plan else []),
            *([worker.question + " " + worker.query for worker in plan.workers] if plan else []),
        ]
    )
    query_tokens = set(_meaningful_tokens(query_text))
    query_hits = sum(1 for token in query_tokens if token in haystack)
    query_score = min(0.30, query_hits * 0.018)
    cell_matches = 0
    if contract:
        cell_matches = sum(1 for cell in contract.cells if _text_supports_cell(haystack, cell))
    cell_score = min(0.30, cell_matches * 0.04)
    technical_score = 0.0
    if plan and plan.research_profile == "technical_architecture":
        technical_score = score_technical_density(Source(title=source.title, url=source.url, content=passage)) * 0.32
    type_score = {
        "academic": 0.12,
        "repository": 0.11,
        "documentation": 0.10,
        "pdf": 0.08,
        "primary": 0.08,
    }.get(classify_source_type(source.url), 0.03)
    length_score = 0.08 if len(passage) > 900 else 0.04 if len(passage) > 350 else 0.0
    return max(0.0, min(1.0, 0.16 + query_score + cell_score + technical_score + type_score + length_score))


def _passage_confidence(source: Source, *, passage_score: float) -> float:
    base = 0.66 if source.content else 0.50
    return max(0.45, min(0.9, base + min(0.18, passage_score * 0.18)))


def _passage_signature(text: str) -> str:
    tokens = _meaningful_tokens(text)[:28]
    return " ".join(tokens)


def detect_contradictions(items: list[EvidenceItem]) -> list[str]:
    text = " ".join(item.evidence.lower() for item in items)
    pairs = [("increase", "decrease"), ("growth", "decline"), ("approved", "rejected"), ("profit", "loss")]
    found: list[str] = []
    for left, right in pairs:
        if left in text and right in text:
            found.append(f"Evidence contains both '{left}' and '{right}' signals; synthesis should avoid overclaiming.")
    return found[:3]



__all__ = [
    "bind_evidence",
    "detect_contradictions",
    "extract_architecture_cards",
    "extract_evidence_claims",
    "_claim_candidate_sentences",
    "_claim_query_terms",
    "_claim_role_for_text",
    "_claim_type_for_text",
    "_claim_type_priority",
    "_freshness_risk_for_text",
    "_max_claims_for_item",
    "_score_claim_sentence",
    "_select_evidence_passages",
    "_candidate_passages",
    "_chunk_long_passage",
    "_score_passage",
    "_passage_confidence",
    "_passage_signature",
    "_architecture_system_name",
    "_architecture_card_confidence",
    "_architecture_card_has_signal",
    "_best_architecture_quote",
    "_extract_architecture_pattern",
    "_extract_architecture_terms",
    "_extract_metric_snippets",
    "_extract_validation_loop",
    "_lesson_for_agentdeck",
]
