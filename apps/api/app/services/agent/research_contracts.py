"""research_contracts.py — Coverage contract generation for the research pipeline.

Responsibilities:
  - Profile-specific coverage contracts (one per ResearchProfile variant)
  - LLM-driven generic contract generation with heuristic fallback
  - Fallback helpers: _derive_fallback_subjects, _derive_fallback_dimensions

Extracted from research_subtree.py (TD-01).
"""
from __future__ import annotations

import json
import logging
import re

from app.services.agent import model_client
from app.services.agent.models import TurnRequest
from app.services.agent.prompt_library import resolve_prompt
from app.services.agent.research_models import (
    CoverageCell,
    CoverageContract,
    ResearchBrief,
)
from app.services.agent.research_utils import _dedupe, _parse_json

logger = logging.getLogger(__name__)

COVERAGE_CONTRACT_PROMPT = """You are the Fronei coverage contract agent.

Given a research brief, generate the evidence matrix that defines when research is complete.
For comparison or vendor research, subjects are the entities being compared and dimensions are the attributes.
For topic research, subjects are major subtopics and dimensions are analytical angles.

Return only JSON:
{
  "subjects": ["2-6 subjects"],
  "dimensions": ["3-7 dimensions"],
  "cells": [
    {"dimension": "dimension name", "subject": "subject name", "required": true}
  ]
}
Generate one cell per dimension × subject combination. Mark required=false only when obviously not applicable.
"""


# ---------------------------------------------------------------------------
# Profile-specific contract factories
# ---------------------------------------------------------------------------

def _technical_architecture_contract() -> CoverageContract:
    subjects = [
        "Lead agent and orchestration",
        "Research planning and coverage contract",
        "Search workers and provider strategy",
        "Source reading and deep-link crawling",
        "Evidence binder and citation map",
        "Reflection, gap detection, and repair loop",
        "Synthesis, judge, and quality gates",
        "Runtime durability, budget ledger, and observability",
        "Guardrails and security controls",
    ]
    dimensions = [
        "responsibility",
        "implementation pattern",
        "data model",
        "workflow",
        "failure handling",
        "trade-offs",
    ]
    cells = [
        CoverageCell(subject=subject, dimension=dimension, required=True)
        for subject in subjects
        for dimension in dimensions
    ]
    return CoverageContract(
        cells=cells,
        subjects=subjects,
        dimensions=dimensions,
        source="profile:technical_architecture",
    )


def _framework_comparison_contract(message: str) -> CoverageContract:
    subjects = _extract_named_framework_subjects(message)
    dimensions = [
        "architecture model",
        "multi-agent coordination approach",
        "production readiness and deployment model",
        "known failure modes and limitations",
        "lifecycle status and ecosystem trajectory",
        "enterprise fit and recommendation rationale",
    ]
    cells = [
        CoverageCell(subject=subject, dimension=dimension, required=True)
        for subject in subjects
        for dimension in dimensions
    ]
    return CoverageContract(
        cells=cells,
        subjects=subjects,
        dimensions=dimensions,
        source="profile:technical_architecture:multi_subject_comparison",
    )


def _vendor_comparison_contract() -> CoverageContract:
    subjects = [
        "Pricing and licensing models",
        "API capabilities and integration",
        "Security and compliance posture",
        "SLAs, reliability, and support",
        "Use-case fit and feature gaps",
        "Vendor stability and lock-in risk",
        "Migration and switching costs",
    ]
    dimensions = ["current state", "specifics / evidence", "strengths", "weaknesses / risks"]
    cells = [
        CoverageCell(subject=subject, dimension=dimension, required=True)
        for subject in subjects
        for dimension in dimensions
    ]
    return CoverageContract(
        cells=cells, subjects=subjects, dimensions=dimensions,
        source="profile:vendor_comparison",
    )


def _market_landscape_contract() -> CoverageContract:
    subjects = [
        "Market categories and segmentation",
        "Key players and competitive positioning",
        "Market size, growth, and adoption metrics",
        "Technology and product trends",
        "Buyer behavior and use-case patterns",
        "Business model and monetization dynamics",
        "Barriers to entry and competitive moat",
    ]
    dimensions = ["current state", "quantitative data", "key examples", "trend direction", "business implication"]
    cells = [
        CoverageCell(subject=subject, dimension=dimension, required=True)
        for subject in subjects
        for dimension in dimensions
    ]
    return CoverageContract(
        cells=cells, subjects=subjects, dimensions=dimensions,
        source="profile:market_landscape",
    )


def _policy_regulatory_contract() -> CoverageContract:
    subjects = [
        "Primary regulations and legislative source",
        "Regulatory authority and enforcement body",
        "Jurisdiction scope and applicability",
        "Specific compliance requirements and obligations",
        "Penalties, enforcement history, and precedents",
        "Industry guidance and safe-harbor interpretations",
        "Pending changes and regulatory direction",
    ]
    dimensions = ["source / authority", "effective date / jurisdiction", "specific requirement", "compliance impact", "recent developments"]
    cells = [
        CoverageCell(subject=subject, dimension=dimension, required=True)
        for subject in subjects
        for dimension in dimensions
    ]
    return CoverageContract(
        cells=cells, subjects=subjects, dimensions=dimensions,
        source="profile:policy_regulatory",
    )


def _strategy_brief_contract() -> CoverageContract:
    subjects = [
        "Business context and problem statement",
        "Strategic options and alternatives",
        "Recommended course of action",
        "Key risks and mitigations",
        "Resource, cost, and timeline implications",
        "Success metrics and decision criteria",
        "Immediate next steps and owners",
    ]
    dimensions = ["current state / evidence", "analysis", "recommendation", "risks"]
    cells = [
        CoverageCell(subject=subject, dimension=dimension, required=True)
        for subject in subjects
        for dimension in dimensions
    ]
    return CoverageContract(
        cells=cells, subjects=subjects, dimensions=dimensions,
        source="profile:strategy_brief",
    )


def _implementation_plan_contract() -> CoverageContract:
    subjects = [
        "Scope, objectives, and success criteria",
        "Workstream breakdown and task dependencies",
        "Milestone timeline and phasing",
        "Resource requirements and ownership model",
        "Risk register and mitigation actions",
        "Governance, change management, and communication",
        "Rollback and contingency planning",
    ]
    dimensions = ["deliverable / definition", "dependencies", "owner / team", "timeline", "risk / blocker"]
    cells = [
        CoverageCell(subject=subject, dimension=dimension, required=True)
        for subject in subjects
        for dimension in dimensions
    ]
    return CoverageContract(
        cells=cells, subjects=subjects, dimensions=dimensions,
        source="profile:implementation_plan",
    )


# ---------------------------------------------------------------------------
# Fallback subject/dimension derivation
# ---------------------------------------------------------------------------

def _derive_fallback_subjects(message: str, brief: ResearchBrief) -> list[str]:
    framework_subjects = _extract_named_framework_subjects(message)
    if framework_subjects:
        return framework_subjects
    scoped = [item for item in brief.scope_in if len(item.strip()) > 1]
    if scoped:
        return _dedupe(scoped)[:4]
    candidates = re.split(r"\b(?:vs\.?|versus|and|,|/)\b", message, flags=re.IGNORECASE)
    subjects = [_clean_subject_name(candidate) for candidate in candidates if 2 <= len(candidate.strip()) <= 80]
    subjects = [subject for subject in subjects if subject]
    if len(subjects) >= 2 and any(token in message.lower() for token in ("compare", " vs", "versus")):
        return _dedupe(subjects)[:4]
    return [brief.objective[:80] or message[:80]]


def _derive_fallback_dimensions(criteria: list[str]) -> list[str]:
    text = " ".join(criteria).lower()
    standard = []
    for dimension in ("capabilities", "pricing", "security", "data quality", "risks", "recent developments"):
        if dimension.split()[0] in text:
            standard.append(dimension)
    if standard:
        return _dedupe(standard)[:5]
    return ["capabilities", "evidence", "risks"]


def _extract_named_framework_subjects(message: str) -> list[str]:
    text = message or ""
    lower = text.lower()
    if not any(term in lower for term in ("framework", "frameworks", "agentic", "multi-agent", "multi agent", "orchestration")):
        return []
    region = text
    if ":" in region:
        region = region.split(":", 1)[1]
    stop_match = re.search(r"\bprovide for each\b|\bthen synthesize\b|\bexplain why\b|\brecommend", region, flags=re.IGNORECASE)
    if stop_match:
        region = region[:stop_match.start()]
    raw_candidates = re.split(r",|;|\band\b", region)
    subjects: list[str] = []
    for raw in raw_candidates:
        value = raw.strip(" .:-()[]")
        value = re.sub(r"^(?:and|or|the|a|an)\s+", "", value, flags=re.IGNORECASE).strip()
        if not value or len(value) > 60:
            continue
        if re.search(r"[A-Z][A-Za-z0-9]*(?:[A-Z][A-Za-z0-9]*)?", value):
            subjects.append(value)
    known = [
        ("LangGraph", r"\blanggraph\b"),
        ("CrewAI", r"\bcrewai\b"),
        ("AutoGen", r"\bautogen\b"),
        ("Haystack", r"\bhaystack\b"),
        ("LlamaIndex Workflows", r"\bllamaindex(?:\s+workflows)?\b"),
        ("Microsoft Agent Framework", r"\bmicrosoft agent framework\b"),
    ]
    for label, pattern in known:
        if re.search(pattern, lower) and label not in subjects:
            subjects.append(label)
    return _dedupe(subjects)[:6]


def _is_framework_comparison_request(message: str) -> bool:
    subjects = _extract_named_framework_subjects(message)
    if len(subjects) < 3:
        return False
    lower = (message or "").lower()
    return any(term in lower for term in ("compare", "top ", "for each", "recommend", "best", "enterprise", "production"))


# ---------------------------------------------------------------------------
# Phase 6 — Generalized multi-subject comparison detection
# ---------------------------------------------------------------------------

# Keywords that signal a software/product/tool entity (triggers status check queries)
_TECH_ENTITY_SIGNALS = frozenset({
    "framework", "library", "platform", "tool", "sdk", "api", "product",
    "service", "software", "database", "runtime", "engine", "protocol",
})


_COMPARISON_LEAD_VERBS = frozenset({
    "compare", "comparing", "evaluate", "evaluating", "assess", "assessing",
    "research", "review", "benchmark", "rank", "ranking", "contrast",
    "analyze", "analyse", "study", "pick", "choose", "select",
})

# Words that indicate a candidate is a sentence fragment rather than a name
_FRAGMENT_SIGNALS = re.compile(
    r"\b(?:for|with|as|at|in|on|from|to|by|of|that|which|who|how|when|where|"
    r"the|a|an|this|these|those|globally|distributed|production|enterprise)\b",
    re.IGNORECASE,
)


def _extract_named_comparison_subjects(message: str) -> list[str]:
    """Extract N≥3 named entities in any multi-subject comparison request.

    Generalizes _extract_named_framework_subjects() to cover any domain —
    tech products, medical treatments, companies, etc. Falls back to the
    framework-specific extractor for AI framework comparisons.
    """
    # Try framework-specific path first (it has hand-tuned known-entity list)
    framework_subjects = _extract_named_framework_subjects(message)
    if len(framework_subjects) >= 3:
        return framework_subjects

    text = message or ""

    # Look for explicit list structure: "top N X: A, B, C, D, E"
    top_match = re.search(r"\btop\s+\d+\s+[^:]{0,180}:\s*(.+?)(?:\.|$)", text, re.IGNORECASE)
    region = top_match.group(1) if top_match else text

    # Strip a leading comparison verb from the region
    region = re.sub(
        r"^\s*(?:" + "|".join(_COMPARISON_LEAD_VERBS) + r")\s+",
        "",
        region,
        flags=re.IGNORECASE,
    )

    # Phase 10 — extended sentence-boundary truncation.
    # Added: "covering", "across", "spanning" (dimension-list lead-ins);
    #        "as <Category> platforms/tools/systems/..." (role/class descriptors);
    #        "on" followed by what looks like a dimension list ("on durability, performance...").
    # These patterns dominate real comparison queries and were causing candidates like
    # "eClinicalWorks as EHR platforms" and "Azure Blob Storage on durability" to
    # survive as oversized 4-word fragments that the > 3 words filter then discarded.
    stop_match = re.search(
        r"\bprovide for each\b|\bthen synthesize\b|\bexplain why\b|\brecommend\b"
        r"|\bincluding\b|\bfor (?:a|an|the|use|each|globally|enterprise|production)\b"
        r"|\bas (?:a|an|the)\b|\bto (?:determine|decide|select|choose)\b"
        r"|\bcovering\b|\bacross\b|\bspanning\b"
        r"|\bon\b(?=\s+\w+(?:,|\s+and\b))"
        r"|\bas (?:[A-Z][A-Za-z]*\s+)?(?:platforms?|tools?|systems?|solutions?|vendors?|products?|providers?|frameworks?|services?|options?|databases?|stacks?)\b",
        region,
        flags=re.IGNORECASE,
    )
    if stop_match:
        region = region[: stop_match.start()]

    # Split on list delimiters
    raw_candidates = re.split(r",|;|\bvs\.?\b|\bversus\b|\band\b", region)

    # Phase 10 — first pass: collect all structurally valid candidates, note which have capitals.
    # We use this to infer proper-noun-hood from list position for intentionally-lowercase
    # product names like "athenahealth" or "eClinicalWorks" (which has an interior capital,
    # but genuinely lowercase product names like "athenahealth" have none).
    pre_subjects: list[str] = []
    for raw in raw_candidates:
        value = _clean_subject_name(raw)
        value = re.sub(r"^(?:and|or|the|a|an|also)\s+", "", value, flags=re.IGNORECASE).strip()
        if not value or len(value) < 2:
            continue
        if len(value) > 40:
            continue
        words = value.split()
        if len(words) > 3:
            continue
        if len(words) > 1 and _FRAGMENT_SIGNALS.search(value):
            continue
        if words[0].lower() in _COMPARISON_LEAD_VERBS and len(words) == 1:
            continue
        pre_subjects.append(value)

    # Phase 10 — if ≥2 siblings have a capital letter, this is a proper-noun list context.
    # Accept all candidates that pass structural checks, not just those with a capital —
    # intentionally-lowercase product names (e.g. athenahealth) appear in proper-noun lists.
    capital_count = sum(1 for v in pre_subjects if re.search(r"[A-Z]", v))
    list_is_proper_noun_context = capital_count >= 2

    subjects: list[str] = []
    for value in pre_subjects:
        has_capital = bool(re.search(r"[A-Z]", value))
        if has_capital or list_is_proper_noun_context:
            subjects.append(_clean_subject_name(value))

    return _dedupe(subjects)[:8]


def _clean_subject_name(value: str) -> str:
    value = re.sub(r"[\u2010-\u2015]+$", "", (value or "").strip())
    value = value.strip(" .:-()[]\"'\u2010\u2011\u2012\u2013\u2014\u2015")
    return re.sub(r"\s+", " ", value).strip()


def _count_comparison_dimensions(message: str) -> int:
    """Count distinct comparison dimensions/criteria stated after a list-lead-in keyword.

    Phase 11 — reuses the same boundary patterns Phase 10 added as truncation stops in
    _extract_named_comparison_subjects(), but now counts the items ON THE DIMENSION SIDE
    of that boundary rather than discarding them.

    Example: "Compare AWS S3, Google Cloud Storage, and Azure Blob Storage on durability,
    pricing tiers, and egress costs" → 3 dimensions (durability / pricing tiers / egress costs).

    Returns 0 if no dimension-list pattern is found.
    """
    text = message or ""
    # Find the first dimension-list lead-in.  Anchors match the same boundary keywords
    # used in the stop_match inside _extract_named_comparison_subjects().
    dim_match = re.search(
        r"\b(?:on|covering|across|spanning|in\s+terms\s+of)\b\s*(.+?)(?:\.|;|$)",
        text,
        flags=re.IGNORECASE,
    )
    if not dim_match:
        return 0
    dim_region = dim_match.group(1).strip()
    # Split on comma/and — same delimiters as the subject-extraction pass.
    raw_dims = re.split(r",|\band\b", dim_region, flags=re.IGNORECASE)
    dims: list[str] = []
    for raw in raw_dims:
        value = raw.strip(" .:-()[]\"'")
        if not value or len(value) < 2:
            continue
        words = value.split()
        if len(words) > 6:
            # Likely prose rather than a dimension label — stop counting here.
            break
        dims.append(value)
    return len(dims)


def _is_multi_subject_comparison(message: str) -> bool:
    """True when the request names N≥3 comparable entities in a comparison context."""
    subjects = _extract_named_comparison_subjects(message)
    if len(subjects) < 3:
        return False
    lower = (message or "").lower()
    comparison_signals = (
        "compare", "comparison", "top ", "for each", "recommend", "best",
        "vs", "versus", "evaluate", "assessment", "side by side", "which",
        "review", "rank", "ranking",
    )
    return any(term in lower for term in comparison_signals)


def _is_tech_entity_comparison(message: str) -> bool:
    """True when _is_multi_subject_comparison() fires AND the entities are software/tools.

    Used to gate named-entity status check queries (Phase 6.4) — we only want to
    run deprecation/successor queries for software products, not medical treatments
    or financial instruments.
    """
    if not _is_multi_subject_comparison(message):
        return False
    lower = (message or "").lower()
    # Has explicit tech-entity signals OR is already detected as framework comparison
    return (
        _is_framework_comparison_request(message)
        or any(term in lower for term in _TECH_ENTITY_SIGNALS)
    )


def _multi_subject_comparison_contract(message: str) -> CoverageContract:
    """Generic comparison contract: one CoverageCell per (named_subject, dimension).

    Used when _is_multi_subject_comparison() fires but _is_framework_comparison_request()
    does not — i.e. any non-AI-framework comparison with N≥3 named entities.
    """
    subjects = _extract_named_comparison_subjects(message)
    lower = (message or "").lower()

    # Derive dimensions from the query's explicit "for each" / "across" clauses
    for_each_match = re.search(
        r"\bfor each[^:]*?:\s*(.+?)(?:\.|then|$)", lower, re.IGNORECASE
    )
    if for_each_match:
        dim_text = for_each_match.group(1)
        raw_dims = [d.strip(" .,;") for d in re.split(r",|;", dim_text)]
        dimensions = [d for d in raw_dims if 3 <= len(d) <= 60][:6]
    else:
        dimensions = []

    if not dimensions:
        # Generic cross-domain comparison dimensions
        dimensions = [
            "overview and core capability",
            "current status and maturity",
            "key strengths",
            "known limitations and risks",
            "best-fit use cases",
        ]

    cells = [
        CoverageCell(subject=subject, dimension=dimension, required=True)
        for subject in subjects
        for dimension in dimensions
    ]
    return CoverageContract(
        cells=cells,
        subjects=subjects,
        dimensions=dimensions,
        source="profile:multi_subject_comparison",
    )


# ---------------------------------------------------------------------------
# Phase 12 — Brief-anchored contract: uses subjects from brief/message, not static template
# ---------------------------------------------------------------------------

def _brief_anchored_contract(
    subjects: list[str],
    message: str,
    brief: "ResearchBrief",
    *,
    profile_source: str = "brief_anchored",
) -> CoverageContract:
    """Build a CoverageContract whose subjects come from the brief or message extraction.

    Dimensions are derived from the query (explicit "for each" / "across" / "covering"
    clauses, or scope_in terms, or a sensible generic fallback for the profile).
    This contract is always preferred over a static profile template when real named
    subjects are available, so every dispatched search query can be anchored to an
    actual entity from the request rather than a generic SaaS evaluation dimension.
    """
    # Try to derive dimensions from the message's explicit dimension-list phrases.
    dimensions: list[str] = []

    # Check for "for each: dim1, dim2, ..." or "covering dim1, dim2, ..."
    for pattern in (
        r"\bfor each[^:]*?:\s*(.+?)(?:\.|then|$)",
        r"\b(?:including|covering|across|spanning|in\s+terms\s+of|on)\b\s*(.+?)(?:\.|;|$)",
    ):
        m = re.search(pattern, message, re.IGNORECASE)
        if m:
            raw_dims = [d.strip(" .,;") for d in re.split(r",|\band\b", m.group(1), flags=re.IGNORECASE)]
            candidate_dims = [d for d in raw_dims if 2 <= len(d) <= 80][:6]
            if candidate_dims:
                dimensions = candidate_dims
                break

    # Fall back to brief.scope_in terms as dimensions (they're often topic facets)
    if not dimensions and brief.scope_in:
        subject_keys = {subject.lower() for subject in subjects}
        dimensions = [s for s in brief.scope_in if len(s.strip()) > 1 and s.lower() not in subject_keys][:6]

    # Final fallback: profile-aware generic dimensions
    if not dimensions:
        _profile_dimensions: dict[str, list[str]] = {
            "vendor_comparison": [
                "capabilities and core features",
                "pricing and licensing",
                "integration and API",
                "security and compliance",
                "support and SLA",
            ],
            "market_landscape": [
                "market position",
                "product offering",
                "strengths",
                "limitations",
                "ecosystem and partnerships",
            ],
            "strategy_brief": [
                "current state",
                "strategic fit",
                "risks",
                "recommendation rationale",
            ],
        }
        dimensions = _profile_dimensions.get(
            profile_source,
            ["overview", "capabilities", "strengths", "limitations", "recommendation"],
        )

    cells = [
        CoverageCell(subject=subject, dimension=dimension, required=True)
        for subject in subjects
        for dimension in dimensions
    ]
    return CoverageContract(
        cells=cells,
        subjects=subjects,
        dimensions=dimensions,
        source=f"brief_anchored:{profile_source}",
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def generate_coverage_contract(request: TurnRequest, brief: ResearchBrief) -> CoverageContract:
    # Phase 12 — prefer subjects extracted from the brief / message over any static template.
    # The brief is built by an LLM with full query context; static templates are domain-generic
    # and silently override the brief's correctly-extracted scope_in list.
    # Strategy: extract named subjects; if ≥3 are found, use the multi-subject path so
    # cells are anchored to real entities.  Profile-specific static templates remain as the
    # fallback for genuinely un-structured, single-subject queries.
    named_subjects = _extract_named_comparison_subjects(request.message)
    brief_subjects = [s for s in brief.scope_in if len(s.strip()) > 1] if brief.scope_in else []

    # For comparison-flavoured profiles, try to build a subject-anchored contract rather than
    # handing off to the generic static factory that carries no named-entity information.
    _COMPARISON_PROFILES = frozenset({"vendor_comparison", "market_landscape", "strategy_brief"})
    if brief.research_profile in _COMPARISON_PROFILES and (named_subjects or brief_subjects):
        subjects_to_use = named_subjects if named_subjects else _dedupe(brief_subjects)[:6]
        if subjects_to_use:
            return _brief_anchored_contract(
                subjects_to_use,
                request.message,
                brief,
                profile_source=brief.research_profile,
            )

    if _is_framework_comparison_request(request.message):
        return _framework_comparison_contract(request.message)
    # Phase 6 — general multi-subject comparison: create per-(entity, dimension) cells
    if _is_multi_subject_comparison(request.message) and brief.research_profile not in (
        "technical_architecture", "vendor_comparison",
    ):
        return _multi_subject_comparison_contract(request.message)
    if brief.research_profile == "technical_architecture":
        return _technical_architecture_contract()
    if brief.research_profile == "vendor_comparison":
        return _vendor_comparison_contract()
    if brief.research_profile == "market_landscape":
        return _market_landscape_contract()
    if brief.research_profile == "policy_regulatory":
        return _policy_regulatory_contract()
    if brief.research_profile == "strategy_brief":
        return _strategy_brief_contract()
    if brief.research_profile == "implementation_plan":
        return _implementation_plan_contract()
    try:
        prompt = resolve_prompt(
            "agent.research.coverage_contract.default",
            agent_id="coverage_contract",
            fallback_system_prompt=COVERAGE_CONTRACT_PROMPT,
            variables=["message", "brief"],
            profile=brief.research_profile,
        )
        response = model_client.complete(
            [
                {"role": "system", "content": prompt.system_prompt},
                {
                    "role": "user",
                    "content": json.dumps(
                        {"message": request.message, "brief": brief.model_dump(mode="json")},
                        ensure_ascii=False,
                    ),
                },
            ],
            role="coverage_contract",
            quality_mode=request.quality_mode,
            overrides=request.model_overrides,
            max_tokens=1000,
            timeout_s=20,
        )
        payload = _parse_json(response.text)
        subjects = [str(item) for item in (payload.get("subjects") or []) if str(item).strip()][:6]
        dimensions = [str(item) for item in (payload.get("dimensions") or []) if str(item).strip()][:7]
        cells = [
            CoverageCell.model_validate(cell)
            for cell in payload.get("cells", [])
            if isinstance(cell, dict)
        ]
        if subjects:
            cells = [cell for cell in cells if cell.subject in subjects]
        if dimensions:
            cells = [cell for cell in cells if cell.dimension in dimensions]
        cells = cells[:42]
        if not cells:
            raise ValueError("empty coverage contract")
        if not subjects:
            subjects = _dedupe([cell.subject for cell in cells])[:6]
        if not dimensions:
            dimensions = _dedupe([cell.dimension for cell in cells])[:7]
        return CoverageContract(
            cells=cells,
            subjects=subjects,
            dimensions=dimensions,
            model_used=response.model_used,
            latency_ms=response.latency_ms,
            cost_usd=response.cost_usd,
            source="llm",
        )
    except Exception as exc:
        logger.warning("agent coverage contract failed; using fallback: %s", exc)
        criteria = brief.success_criteria or [brief.objective]
        subjects = _derive_fallback_subjects(request.message, brief)
        dimensions = _derive_fallback_dimensions(criteria)
        cells = [
            CoverageCell(subject=subject, dimension=dimension, required=True)
            for subject in subjects
            for dimension in dimensions
        ][:24]
        return CoverageContract(
            cells=cells or [CoverageCell(subject=brief.objective[:80], dimension="coverage")],
            subjects=subjects or [brief.objective[:80]],
            dimensions=dimensions or ["coverage"],
            source="heuristic",
            fallback_reason=str(exc),
        )


__all__ = [
    "COVERAGE_CONTRACT_PROMPT",
    "_brief_anchored_contract",
    "_count_comparison_dimensions",
    "_derive_fallback_dimensions",
    "_derive_fallback_subjects",
    "_extract_named_comparison_subjects",
    "_framework_comparison_contract",
    "_implementation_plan_contract",
    "_is_multi_subject_comparison",
    "_is_tech_entity_comparison",
    "_market_landscape_contract",
    "_multi_subject_comparison_contract",
    "_policy_regulatory_contract",
    "_strategy_brief_contract",
    "_technical_architecture_contract",
    "_vendor_comparison_contract",
    "generate_coverage_contract",
]
