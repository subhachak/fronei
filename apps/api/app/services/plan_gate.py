"""Plan gate — deterministic, config-driven decision of whether a turn can
execute immediately ("auto") or needs one bundled confirmation popup
("confirm") before execution.

This is pure logic (no LLM calls, no I/O beyond reading the policy YAML once)
so it's cheap to unit test. See docs/unified-plan-architecture.md for the
overall design and app/policies/plan_gate_rules.yaml for the tunables.
"""
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

from app.services.planner import Plan

POLICY_PATH = Path(__file__).resolve().parents[1] / "policies" / "plan_gate_rules.yaml"


@lru_cache(maxsize=1)
def load_policy() -> dict:
    with POLICY_PATH.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


@dataclass
class CapabilityState:
    enabled: bool
    recommended: bool
    reason: str = ""
    # Free-form extra fields per capability: risk_factors for deep_research,
    # brief/format_options/format_recommendation for document.
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"enabled": self.enabled, "recommended": self.recommended, "reason": self.reason, **self.extra}


@dataclass
class PlanGateResult:
    mode: str  # "auto" | "confirm"
    capabilities: dict[str, CapabilityState]
    open_questions: list[str]
    plan_confidence: str

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "plan_confidence": self.plan_confidence,
            "open_questions": self.open_questions,
            "capabilities": {k: v.to_dict() for k, v in self.capabilities.items()},
        }


_EMPTY_BRIEF_VALUES = (None, "", [])


def evaluate(plan: Plan) -> PlanGateResult:
    policy = load_policy()
    open_questions = list(plan.open_questions or [])
    gate_reasons: list[str] = []

    # ── Web search ───────────────────────────────────────────────────────
    web_cfg = policy.get("web_search", {})
    criticality = plan.web_search_criticality or web_cfg.get("default_criticality", "material")
    web_gates = bool(plan.needs_web_search) and criticality in web_cfg.get("gating_criticalities", ["material"])
    web_state = CapabilityState(
        enabled=bool(plan.needs_web_search),
        recommended=web_gates,
        reason=(
            "This may need information beyond what you've supplied — Fronei would search the web."
            if plan.needs_web_search else ""
        ),
    )
    if web_gates:
        gate_reasons.append("web_search")

    # ── Deep research ────────────────────────────────────────────────────
    research_cfg = policy.get("deep_research", {})
    research_gates = bool(plan.recommend_deep_research) and bool(research_cfg.get("always_gate", True))
    research_state = CapabilityState(
        enabled=bool(plan.recommend_deep_research),
        recommended=bool(plan.recommend_deep_research),
        reason=plan.research_reason or "",
        extra={"risk_factors": list(plan.research_risk_factors or [])},
    )
    if research_gates:
        gate_reasons.append("deep_research")

    # ── Document ─────────────────────────────────────────────────────────
    doc_cfg = policy.get("document", {})
    brief = dict(plan.document_brief or {})
    required_fields = doc_cfg.get("required_brief_fields", [])
    missing_fields = [f for f in required_fields if brief.get(f) in _EMPTY_BRIEF_VALUES]
    format_options = list(plan.document_format_options or (["markdown"] if plan.wants_document_output else []))
    if not format_options and plan.wants_document_output:
        format_options = ["markdown"]
    max_silent_formats = doc_cfg.get("max_silent_format_options", 1)

    doc_gates = bool(plan.wants_document_output) and (
        bool(missing_fields) or len(format_options) > max_silent_formats
    )
    if doc_gates:
        gate_reasons.append("document")
        for f in missing_fields:
            open_questions.append(f"Document {f.replace('_', ' ')} isn't specified.")
        if len(format_options) > max_silent_formats:
            open_questions.append("Multiple document formats could work for this — which do you want?")

    supported_formats = set(policy.get("supported_document_formats", ["markdown", "docx"]))
    document_state = CapabilityState(
        enabled=bool(plan.wants_document_output),
        recommended=doc_gates,
        reason=(
            "This looks like it should produce a document rather than a chat reply."
            if plan.wants_document_output else ""
        ),
        extra={
            "brief": brief,
            "format_options": format_options,
            "format_recommendation": plan.document_format_recommendation or (format_options[0] if format_options else "markdown"),
            "supported_formats": sorted(supported_formats),
        },
    )

    # ── Overall confidence ───────────────────────────────────────────────
    confidence_cfg = policy.get("plan_confidence", {})
    confidence_gates = plan.plan_confidence in confidence_cfg.get("gating_levels", ["low"])
    if confidence_gates:
        gate_reasons.append("plan_confidence")

    mode = "confirm" if gate_reasons else "auto"

    return PlanGateResult(
        mode=mode,
        capabilities={
            "web_search": web_state,
            "deep_research": research_state,
            "document": document_state,
        },
        open_questions=open_questions,
        plan_confidence=plan.plan_confidence,
    )
