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
    scoped = [item for item in brief.scope_in if len(item.strip()) > 1]
    if scoped:
        return _dedupe(scoped)[:4]
    candidates = re.split(r"\b(?:vs\.?|versus|and|,|/)\b", message, flags=re.IGNORECASE)
    subjects = [candidate.strip(" .:-") for candidate in candidates if 2 <= len(candidate.strip()) <= 80]
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


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def generate_coverage_contract(request: TurnRequest, brief: ResearchBrief) -> CoverageContract:
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
    "_derive_fallback_dimensions",
    "_derive_fallback_subjects",
    "_implementation_plan_contract",
    "_market_landscape_contract",
    "_policy_regulatory_contract",
    "_strategy_brief_contract",
    "_technical_architecture_contract",
    "_vendor_comparison_contract",
    "generate_coverage_contract",
]
