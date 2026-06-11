"""Hybrid deep-research recommendation helper.

Deterministic rules raise predictable signals; the planner contributes nuanced
judgment. The result is advisory only: the user decides whether to run research.
"""
from dataclasses import dataclass
import re

from app.services.planner import Plan


@dataclass
class ResearchRecommendation:
    recommend: bool
    confidence: str
    reason: str
    risk_factors: list[str]
    suggested_mode: str = "deep"
    source: str = "hybrid"


_HIGH_STAKES = (
    "legal", "law", "regulation", "regulatory", "compliance", "privacy",
    "hipaa", "gdpr", "sox", "sec", "fda", "medical", "clinical",
    "financial", "investment", "tax", "immigration", "visa", "uscis",
)

_CURRENT_TERMS = (
    "latest", "current", "today", "recent", "newest", "now", "2025", "2026",
    "as of", "release notes", "roadmap", "pricing", "price", "cost",
    "processing time", "timeline", "availability", "ga", "preview",
)

_EXTERNAL_DECISION_TERMS = (
    "compare", "comparison", "evaluate", "recommend", "best", "choose",
    "vendor", "platform", "market", "competitor", "benchmark", "analyst",
    "state of", "maturity", "adoption", "enterprise adoption",
)

_SOURCE_TERMS = (
    "source", "sources", "citation", "citations", "evidence", "research",
    "deep research", "investigate", "verify", "fact check", "fact-check",
)

_PURCHASE_TERMS = (
    "buy", "purchase", "shop", "shortlist", "suitable for me", "right for me",
    "find one", "which one", "pick one", "recommend one",
)

_CONSUMER_PRODUCT_TERMS = (
    "appliance", "washer", "dryer", "refrigerator", "fridge", "dishwasher",
    "microwave", "oven", "range", "cooktop", "vacuum", "air purifier",
    "dehumidifier", "water heater", "ac unit", "air conditioner",
)


def _has_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(term in text for term in terms)


def _deterministic_signals(message: str) -> tuple[list[str], str]:
    text = message.lower()
    factors: list[str] = []

    if _has_any(text, _CURRENT_TERMS):
        factors.append("current_facts")
    if _has_any(text, _HIGH_STAKES):
        factors.append("high_stakes")
    if _has_any(text, _EXTERNAL_DECISION_TERMS):
        factors.append("external_decision")
    if _has_any(text, _SOURCE_TERMS):
        factors.append("requires_citations")
    if _has_any(text, _PURCHASE_TERMS):
        factors.append("purchase_decision")
    if _has_any(text, _CONSUMER_PRODUCT_TERMS):
        factors.append("consumer_product")
    if re.search(r"\b(aws|azure|google|gemini|openai|anthropic|claude|snowflake|databricks|bedrock|cortex)\b", text):
        factors.append("vendor_context")

    if "pricing" in factors or "price" in text or "cost" in text:
        factors.append("pricing")

    # Preserve order while deduplicating.
    deduped = list(dict.fromkeys(factors))
    if not deduped:
        return [], ""

    if "high_stakes" in deduped and ("current_facts" in deduped or "requires_citations" in deduped):
        reason = "This touches high-stakes external facts where a quick answer may be risky."
    elif "vendor_context" in deduped and ("external_decision" in deduped or "current_facts" in deduped):
        reason = "This looks like a vendor or platform decision that depends on current external evidence."
    elif "requires_citations" in deduped:
        reason = "You appear to need sourced evidence rather than an unsupported quick answer."
    elif "purchase_decision" in deduped and "consumer_product" in deduped:
        reason = "This looks like a product recommendation where current models, reviews, and constraints matter."
    else:
        reason = "This may depend on external facts that are better handled with research."
    return deduped, reason


def advise_research(message: str, plan: Plan, has_attached_documents: bool = False) -> ResearchRecommendation:
    """Return an advisory deep-research recommendation."""
    rule_factors, rule_reason = _deterministic_signals(message)
    planner_factors = list(dict.fromkeys(plan.research_risk_factors))
    factors = list(dict.fromkeys([*rule_factors, *planner_factors]))

    planner_recommends = bool(plan.recommend_deep_research)
    rules_recommend = (
        "requires_citations" in rule_factors
        or ("high_stakes" in rule_factors and ("current_facts" in rule_factors or "external_decision" in rule_factors))
        or ("vendor_context" in rule_factors and ("external_decision" in rule_factors or "current_facts" in rule_factors))
        or ("purchase_decision" in rule_factors and ("consumer_product" in rule_factors or "current_facts" in rule_factors))
        or len(rule_factors) >= 3
    )

    # If the user uploaded context and there are no freshness/external signals,
    # prefer normal document QA over interrupting with research.
    if has_attached_documents and not {"current_facts", "vendor_context", "high_stakes", "requires_citations"} & set(factors):
        return ResearchRecommendation(False, "low", "", factors, source="rules")

    if not planner_recommends and not rules_recommend:
        return ResearchRecommendation(False, "low", "", factors, source="hybrid")

    confidence = "medium"
    if plan.research_confidence == "high" or "high_stakes" in factors or len(factors) >= 3:
        confidence = "high"

    source = "planner" if planner_recommends and not rules_recommend else "rules" if rules_recommend and not planner_recommends else "hybrid"
    reason = plan.research_reason or rule_reason or "This looks better suited to a source-grounded research pass."

    return ResearchRecommendation(True, confidence, reason, factors, source=source)
