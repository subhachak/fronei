"""research_planner.py — Research planning, reflection, and judge logic.

Responsibilities:
  - plan_from_contract / plan_from_brief_contract / plan_research
  - update_contract_from_evidence, plan_from_targeted_queries
  - build_research_plan_preview
  - reflect (LLM + heuristic)
  - verify_citations_semantically
  - judge_research_final (deterministic quality gate)
  - All query-construction helpers: anchor queries, domain discovery, _targeted_query

Extracted from research_subtree.py (TD-01).
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Literal

from app.config import get_settings
from app.services.agent import model_client
from app.services.agent.models import TurnRequest
from app.services.agent.prompt_library import resolve_prompt
from app.services.agent.research_models import (
    _RESEARCH_PROFILES,
    PROFILE_POLICIES,
    CitationVerification,
    CoverageCell,
    CoverageContract,
    EvidenceItem,
    JudgeVerdict,
    ReflectionDecision,
    ResearchBrief,
    ResearchBudget,
    ResearchGoal,
    ResearchPlan,
    ResearchProfile,
    ResearchStateStore,
    SearchWorkerPlan,
)
from app.services.agent.research_profiles import (
    _secondary_profiles_for,
    create_research_goal,
    generate_research_brief,
    infer_research_profile,
    research_budget_for,
)
from app.services.agent.research_contracts import (
    generate_coverage_contract,
    _extract_named_comparison_subjects,
    _is_multi_subject_comparison,
    _is_tech_entity_comparison,
)
from app.services.agent.research_utils import (
    _dedupe,
    _parse_json,
    resolve_relative_date_phrases,
    score_technical_density,
    temporal_context,
)
from app.services.agent.models import Source

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Prompt constants
# ---------------------------------------------------------------------------

REFLECTION_PROMPT = """You are the Fronei lead research agent.

Review the current research state and decide whether to continue or terminate.
Return only JSON:
{
  "sufficient": true|false,
  "open_dimensions": ["dimensions not yet covered"],
  "open_subjects": ["subjects not yet covered"],
  "targeted_queries": ["2-5 specific search queries to close remaining gaps; empty if sufficient"],
  "terminate_reason": "reason to stop if sufficient=true or budget exhausted",
  "coverage_ratio": 0.0-1.0,
  "next_action": "continue|publish|stop_with_gaps"
}
Be specific in targeted_queries. Prefer site-specific queries for vendor docs, pricing, compliance, APIs, and official pages.
If a cell has repeated targeted attempts and still has no public evidence, stop with an explicit gap rather than hallucinating.
"""

CITATION_VERIFICATION_PROMPT = """You are the Fronei citation verification agent.

You will be given a synthesized answer, an evidence pack, and optionally the expected primary claim role.

For each factual claim with a [S#] citation, verify:
1. The source [S#] exists in the evidence pack.
2. The quoted source text supports the specific claim.
3. Whether the answer correctly handles role conflicts: if official_policy and operational_reality
   sources disagree (e.g. official SLA vs. practitioner wait times), the answer MUST name both
   positions explicitly — not blend them silently.
4. Whether the answer suppresses relevant operational_reality or anecdotal_case evidence solely
   because its source authority is lower than official sources.
5. Phase 8 — DISCLAIMER CHECK (judgment, not phrase matching):
   Does this answer open with an evidence-quality caveat or disclaimer block BEFORE delivering
   substance? Examples of prohibited patterns (regardless of exact wording):
   - Opening with a multi-paragraph block explaining what evidence is missing before the answer starts
   - Leading with "⚠️ Critical Evidence Constraint" or "Evidence Quality Disclaimer" or any heading
     whose purpose is to front-load doubt rather than answer the question
   - Front-loading "There is no retrieved evidence containing..." before any substantive response
   - Opening with "I cannot provide a complete answer because..." and then hedging
   Allowed: brief inline disclosures WITHIN the answer ("no evidence was found for X [see coverage note]")
   Set leads_with_disclaimer: true ONLY if the answer's dominant opening content is a disclaimer block
   rather than a direct response to the question. If the answer leads with substance and discloses
   gaps inline, set to false even if disclaimers appear later.
6. Phase 9 — PERMISSION-SEEKING CHECK (judgment, not phrase matching):
   Does this answer end by asking the user to authorize, approve, or proceed with further research,
   a deeper dive, a second pass, or additional detail — regardless of exact wording? Examples of
   prohibited endings:
   - "Let me know if you'd like me to research X further"
   - "Would you like a deeper dive into..."
   - "I can do a second pass on... if you'd like"
   - "Should I look into... in more detail?"
   - "If you want, I could explore... further"
   If the answer states remaining gaps plainly and moves on without soliciting permission, set
   asks_permission_to_continue: false. Only set true if the closing explicitly invites the user
   to authorize continuation as if their approval is needed.
   Phase 11 — IMPORTANT BOUNDARY: Do NOT flag the following as asks_permission_to_continue:
   - Offers to reformat or export the already-delivered answer (e.g. "I can produce this as a
     DOCX or slide deck if you'd like") — this is a formatting offer, not a research request.
   - Offers to incorporate data the system has no way to retrieve automatically (vendor quotes
     from RFP calls, live demo outcomes, custom negotiated pricing, internal documents) — the
     system cannot fetch these; offering to include them once supplied is appropriate, not
     permission-seeking research solicitation.
   Only flag asks_permission_to_continue when the system is offering to do more automated
   research/information-gathering that it could have already done within the current run.
7. STALENESS CHECK: if a cited claim's evidence is marked staleness="stale" in claim_staleness_summary,
   the answer MUST flag that explicitly (e.g. "as of a source from over a year ago" / "may be outdated")
   rather than presenting it as current. Treat an unflagged stale claim like an unsupported claim.

Return only JSON:
{
  "verified_claims": 0,
  "unsupported_claims": ["claims where citation does not support the claim"],
  "hallucinated_citations": ["S# references that appear in the answer but are not in the evidence pack"],
  "role_mismatch_issues": ["description of any official_policy vs operational_reality conflicts not named in the answer"],
  "unresolved_conflicts": ["description of any evidence conflicts silently blended rather than explicitly named"],
  "unflagged_stale_claims": ["S# citations to staleness=\"stale\" evidence that the answer presents as current without flagging it"],
  "leads_with_disclaimer": false,
  "asks_permission_to_continue": false,
  "repair_needed": true|false,
  "repair_instruction": "specific repair instruction if needed"
}
"""


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------

def plan_from_contract(
    request: TurnRequest,
    contract: CoverageContract,
    budget: ResearchBudget | None = None,
) -> ResearchPlan:
    budget = budget or research_budget_for(request)
    open_cells = contract.open_cells()
    workers: list[SearchWorkerPlan] = []
    for subject in _dedupe([cell.subject for cell in open_cells]):
        subject_cells = [cell for cell in open_cells if cell.subject == subject]
        dimensions = _dedupe([cell.dimension for cell in subject_cells])[:4]
        query = _targeted_query(subject, dimensions, request.message, tz=request.user_timezone)
        workers.append(
            SearchWorkerPlan(
                question=f"Research {subject}: {', '.join(dimensions)}",
                query=query,
                rationale=f"Cover open contract cells for {subject}.",
                max_results=budget.max_results_per_worker,
            )
        )
        if len(workers) >= budget.max_search_workers:
            break
    if not workers:
        workers = _fallback_plan(request, create_research_goal(request)).workers

    profile = _profile_from_contract(contract) or infer_research_profile(request.message)
    anchor_workers: list[SearchWorkerPlan] = []
    _anchor_query_fns: dict[str, Any] = {
        "technical_architecture": _tech_arch_anchor_queries,
        "vendor_comparison": _vendor_comparison_anchor_queries,
        "market_landscape": _market_landscape_anchor_queries,
        "policy_regulatory": _policy_regulatory_anchor_queries,
        "strategy_brief": _strategy_brief_anchor_queries,
        "implementation_plan": _implementation_plan_anchor_queries,
    }
    if profile in _anchor_query_fns and request.research_level == "deep":
        anchor_queries = _anchor_query_fns[profile](request.message)
        existing_queries = {w.query for w in workers}
        anchor_workers = [
            SearchWorkerPlan(
                question=f"Anchor: {q}",
                query=q,
                rationale="Profile-level anchor to seed high-quality sources.",
                max_results=budget.max_results_per_worker,
                discovery_domain=_domain_for_query(q),
            )
            for q in anchor_queries
            if q not in existing_queries
        ]

    domain_workers: list[SearchWorkerPlan] = []
    if request.research_level == "deep":
        existing_queries = {w.query for w in workers} | {w.query for w in anchor_workers}
        domain_workers = [
            worker for worker in _domain_discovery_workers(request, profile, budget)
            if worker.query not in existing_queries
        ]

    # Phase 6 — per-entity anchor queries + status check queries for multi-subject comparisons.
    # These are separate from profile anchor_workers so they fire across profiles.
    phase6_workers: list[SearchWorkerPlan] = []
    if _is_multi_subject_comparison(request.message):
        existing_queries_all = {w.query for w in workers} | {w.query for w in anchor_workers} | {w.query for w in domain_workers}
        phase6_workers = [
            w for w in _per_entity_anchor_queries(request.message, budget)
            if w.query not in existing_queries_all
        ]
        if _is_tech_entity_comparison(request.message):
            status_workers = [
                w for w in _status_check_queries(request.message, budget)
                if w.query not in existing_queries_all
            ]
            phase6_workers.extend(status_workers)

    if request.research_level == "deep":
        workers = _compose_deep_worker_wave(
            contract_workers=workers,
            anchor_workers=phase6_workers + anchor_workers,
            domain_workers=domain_workers,
            max_workers=budget.max_search_workers,
        )
    elif anchor_workers or phase6_workers:
        # Phase 6 fix: do NOT cap phase6_workers at [:2] here — each named subject gets
        # one per-entity anchor query, so capping at 2 starves all subjects beyond the
        # first two of domain-targeted queries.  budget.max_search_workers is the correct
        # ceiling (already applied by the final slice).
        workers = _dedupe_workers((anchor_workers + phase6_workers) + workers)[:budget.max_search_workers]

    return ResearchPlan(
        research_profile=profile,
        secondary_profiles=_secondary_profiles_for(request.message, profile),
        source_lanes=PROFILE_POLICIES[profile].source_lanes,
        questions=[worker.question for worker in workers],
        search_queries=[worker.query for worker in workers],
        workers=workers,
        max_sources=budget.max_sources,
        min_evidence_items=budget.min_evidence_items,
        judge_threshold=budget.judge_threshold,
        repair_iterations=budget.repair_iterations,
        guardrails=create_research_goal(request).guardrails,
        source="contract",
        expected_primary_role=_infer_primary_role_hint(request.message),
    )


def plan_from_brief_contract(
    request: TurnRequest,
    brief: ResearchBrief,
    contract: CoverageContract,
    budget: ResearchBudget | None = None,
) -> ResearchPlan:
    from app.services.agent.research_profiles import _request_for_research_objective
    return plan_from_contract(_request_for_research_objective(request, brief), contract, budget)


def _profile_from_contract(contract: CoverageContract) -> ResearchProfile | None:
    # Phase 12 — also handle brief_anchored:{profile} sources produced by _brief_anchored_contract().
    for prefix in ("profile:", "brief_anchored:"):
        if contract.source.startswith(prefix):
            candidate = contract.source[len(prefix):].split(":", 1)[0]
            if candidate in _RESEARCH_PROFILES:
                return candidate  # type: ignore[return-value]
    return None


def _dedupe_workers(workers: list[SearchWorkerPlan]) -> list[SearchWorkerPlan]:
    seen: set[str] = set()
    result: list[SearchWorkerPlan] = []
    for worker in workers:
        key = " ".join((worker.query or worker.question).lower().split())
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(worker)
    return result


def _compose_deep_worker_wave(
    *,
    contract_workers: list[SearchWorkerPlan],
    anchor_workers: list[SearchWorkerPlan],
    domain_workers: list[SearchWorkerPlan],
    max_workers: int,
) -> list[SearchWorkerPlan]:
    """Mix broad discovery with contract-targeted work without starving either."""
    if max_workers <= 0:
        return []
    if not contract_workers:
        return _dedupe_workers(domain_workers + anchor_workers)[:max_workers]
    discovery_cap = min(4, max(2, max_workers // 3))
    domain_cap = min(2, discovery_cap)
    anchor_cap = max(0, discovery_cap - domain_cap)
    selected = _dedupe_workers(
        domain_workers[:domain_cap]
        + anchor_workers[:anchor_cap]
        + contract_workers
        + domain_workers[domain_cap:]
        + anchor_workers[anchor_cap:]
    )
    return selected[:max_workers]


def plan_from_targeted_queries(targeted_queries: list[str], state: ResearchStateStore) -> ResearchPlan:
    new_queries = [
        " ".join(query.split())
        for query in targeted_queries
        if query and " ".join(query.split()) not in state.query_history
    ][: state.budget_ledger.budget.max_search_workers]
    if not new_queries:
        return state.plan
    workers = [
        SearchWorkerPlan(
            question=f"Follow-up: {query}",
            query=query[:220],
            rationale="Lead agent targeted follow-up to fill coverage gaps.",
            max_results=4,
        )
        for query in new_queries
    ]
    return ResearchPlan(
        research_profile=state.plan.research_profile,
        questions=[worker.question for worker in workers],
        search_queries=[worker.query for worker in workers],
        workers=workers,
        max_sources=state.plan.max_sources,
        min_evidence_items=state.plan.min_evidence_items,
        judge_threshold=state.plan.judge_threshold,
        repair_iterations=state.plan.repair_iterations,
        guardrails=state.plan.guardrails,
        source="reflection",
    )


def update_contract_from_evidence(state: ResearchStateStore) -> None:
    for cell in state.contract.cells:
        if not cell.required or cell.status == "not_applicable":
            continue
        worker_matches = [
            report
            for report in state.worker_reports
            if report.assigned_subject == cell.subject
            and report.assigned_dimension == cell.dimension
            and report.claims
            and report.self_assessed_confidence >= 0.42
        ]
        if worker_matches:
            best = max(worker_matches, key=lambda report: report.self_assessed_confidence)
            claim_source_ids = _dedupe([claim.source_id for claim in best.claims])
            cell.evidence_ids = claim_source_ids
            cell.confidence = best.self_assessed_confidence
            cell.status = "filled" if best.self_assessed_confidence >= 0.68 and len(best.claims) >= 2 else "partial"
            cell.notes = (
                f"Worker {best.worker_id} reported {len(best.claims)} typed claim(s); "
                f"confidence={best.self_assessed_confidence:.2f}."
            )
            continue
        matches: list[EvidenceItem] = []
        for item in state.evidence.items:
            if _evidence_supports_cell(item, cell):
                matches.append(item)
        if not matches:
            if cell.status not in {"partial", "filled"}:
                cell.status = "empty"
                cell.evidence_ids = []
                cell.confidence = 0.0
            continue
        cell.evidence_ids = [item.source_id for item in matches]
        cell.confidence = max(item.confidence for item in matches)
        cell.status = "filled" if len(matches) >= 2 or cell.confidence >= 0.72 else "partial"
        for item in matches:
            if cell.cell_id not in item.supports_cells:
                item.supports_cells.append(cell.cell_id)


def plan_research(request: TurnRequest) -> ResearchPlan:
    goal = create_research_goal(request)
    try:
        registry = _get_registry()
        prompt = registry.prompt_for("research_lead")
        resolved_prompt = resolve_prompt(
            "agent.research.lead.default",
            agent_id="research_lead",
            fallback_system_prompt=prompt.system_prompt,
            variables=["message", "conversation_context", "quality_mode", "research_level", "output_format", "budget"],
            profile=infer_research_profile(request.message),
        )
        response = model_client.complete(
            [
                {"role": "system", "content": resolved_prompt.system_prompt},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "message": request.message,
                            **temporal_context(request.user_timezone),
                            "conversation_context": request.conversation_context[-5000:] if request.conversation_context else "",
                            "quality_mode": request.quality_mode,
                            "research_level": request.research_level,
                            "output_format": request.output_format,
                            "budget": goal.budget.model_dump(mode="json"),
                            "guardrails": goal.guardrails,
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            role="research_planner",
            quality_mode=request.quality_mode,
            overrides=request.model_overrides,
            max_tokens=1000 if request.research_level == "deep" else 600,
            timeout_s=20,
        )
        payload = _parse_json(response.text)
        plan = ResearchPlan.model_validate(payload)
        plan.max_sources = min(plan.max_sources, goal.budget.max_sources)
        plan.min_evidence_items = min(plan.min_evidence_items, goal.budget.min_evidence_items)
        plan.judge_threshold = goal.budget.judge_threshold
        plan.repair_iterations = goal.budget.repair_iterations
        plan.workers = plan.workers[: goal.budget.max_search_workers]
        plan.model_used = response.model_used
        plan.latency_ms = response.latency_ms
        plan.cost_usd = response.cost_usd
        plan.source = "llm"
        return _normalize_plan(plan, request, goal)
    except Exception as exc:
        logger.warning("agent research planning failed; using fallback plan: %s", exc)
        plan = _fallback_plan(request, goal)
        plan.fallback_reason = str(exc)
        return plan


def _get_registry():
    from app.services.agent.research_profiles import get_research_registry
    return get_research_registry()


def build_research_plan_preview(request: TurnRequest) -> dict[str, Any]:
    """Build a human-reviewable research plan without executing web tools."""
    started = time.perf_counter()
    preview_request = request.model_copy(update={"research_level": "deep"})
    brief = generate_research_brief(preview_request)
    contract = generate_coverage_contract(preview_request, brief)
    budget = research_budget_for(preview_request)
    plan = plan_from_brief_contract(preview_request, brief, contract, budget)
    investigate = _plan_preview_investigation_items(brief, contract, plan)
    source_strategy = _plan_preview_source_strategy(brief.research_profile, plan)
    return {
        "title": _plan_preview_title(brief, request),
        "goal": brief.objective,
        "audience": brief.audience,
        "research_profile": brief.research_profile,
        "research_level": "deep",
        "output_format": request.output_format,
        "estimated_duration": "Ready in a few minutes",
        "workflow": [
            {"label": "Research websites", "description": "Run domain-specific discovery lanes and build a broad source candidate inventory."},
            {"label": "Analyze results", "description": "Rank sources, follow reference links, bind typed evidence, and retry weak worker results."},
            {"label": "Create report", "description": "Synthesize, fact-check, and tighten the final answer from verified evidence."},
        ],
        "investigate": investigate,
        "source_strategy": source_strategy,
        "workers": [worker.model_dump(mode="json") for worker in plan.workers],
        "coverage": {
            "subjects": contract.subjects,
            "dimensions": contract.dimensions,
            "required_cells": len([cell for cell in contract.cells if cell.required]),
        },
        "budget": budget.model_dump(mode="json"),
        "model_used": _dedupe([v for v in [brief.model_used, contract.model_used, plan.model_used] if v]),
        "latency_ms": int((time.perf_counter() - started) * 1000) + brief.latency_ms + contract.latency_ms + plan.latency_ms,
        "fallback_reasons": _dedupe(
            [v for v in [brief.fallback_reason, contract.fallback_reason, plan.fallback_reason] if v]
        ),
    }


def _plan_preview_title(brief: ResearchBrief, request: TurnRequest) -> str:
    if brief.scope_in:
        return " ".join(brief.scope_in[:2])[:90]
    objective = re.sub(r"^(conduct|do|perform)\s+(deep\s+)?research\s+(on|about)?\s*", "", brief.objective, flags=re.I)
    return objective.strip(" .")[:90] or request.message[:90] or "Research plan"


def _plan_preview_investigation_items(
    brief: ResearchBrief,
    contract: CoverageContract,
    plan: ResearchPlan,
) -> list[str]:
    items: list[str] = []
    for criterion in brief.success_criteria:
        cleaned = " ".join(str(criterion).split()).strip()
        if cleaned:
            items.append(cleaned.rstrip(".") + ".")
    if len(items) < 5:
        for worker in plan.workers:
            cleaned = " ".join(worker.question.split()).strip()
            if cleaned and cleaned not in items:
                items.append(cleaned.rstrip(".") + ".")
    if len(items) < 5:
        for subject in contract.subjects:
            for dimension in contract.dimensions[:2]:
                items.append(f"Examine {subject} through the lens of {dimension}.")
                if len(items) >= 7:
                    break
            if len(items) >= 7:
                break
    return _dedupe(items)[:8]


def _plan_preview_source_strategy(profile: ResearchProfile, plan: ResearchPlan) -> list[str]:
    policy = PROFILE_POLICIES.get(profile, PROFILE_POLICIES["general"])
    base = [
        "Profile-specific discovery lanes: " + ", ".join(policy.source_lanes[:4]),
        "Web search across the configured provider chain",
        "Candidate source inventory before expensive page reading",
        "Source reading for high-value pages and documents",
        "Source graph expansion from high-value references and repository/docs links",
        "Worker self-evaluation and retry for weak result sets",
        "Typed claim extraction with citation provenance",
        "Fact-check/rewrite pass to replace vague claims with named-source specifics",
    ]
    if policy.allowed_gaps:
        base.extend(policy.allowed_gaps)
    return _dedupe(base)


# ---------------------------------------------------------------------------
# Reflection and citation verification
# ---------------------------------------------------------------------------

def _topical_relevance_check(state: ResearchStateStore) -> str | None:
    """Phase 12.2 — before judging evidence sufficient, verify it actually mentions
    the named subjects from the brief.

    Returns a human-readable failure string if relevance is below threshold, or None
    when evidence is topically on-target.  A failure here returns a 'research_more'
    signal so the lead loop dispatches per-subject targeted queries before synthesis.
    """
    # Only fire when named subjects exist (multi-subject comparisons, vendor evals, etc.)
    named_subjects = [s for s in state.contract.subjects if s.strip()]
    if len(named_subjects) < 2:
        return None  # single-subject queries: no per-entity relevance check needed

    # Collect searchable text from every bound evidence item.
    evidence_text = " ".join(
        " ".join([item.title or "", item.url or "", item.evidence or "", item.quoted_text or ""])
        for item in state.evidence.items
    ).lower()

    if not evidence_text.strip():
        return f"No evidence has been bound yet; {len(named_subjects)} named subjects have no coverage."

    # For each subject, check whether any evidence item mentions it (case-insensitive token
    # presence).  We accept a partial word match (e.g. "athenahealth" in a URL slug).
    missing_subjects: list[str] = []
    for subject in named_subjects:
        subject_tokens = re.split(r"\s+", subject.lower().strip())
        # Any token ≥4 chars is enough to confirm the subject was retrieved (avoids
        # false positives from short tokens like "aws" matching "lawsuits").
        significant_tokens = [t for t in subject_tokens if len(t) >= 4]
        if not significant_tokens:
            significant_tokens = subject_tokens  # keep even short ones if no other choice
        if not any(token in evidence_text for token in significant_tokens):
            missing_subjects.append(subject)

    if not missing_subjects:
        return None

    threshold = max(1, int(len(named_subjects) * 0.4))  # allow up to 40% absent before flagging
    if len(missing_subjects) > threshold:
        return (
            f"Evidence has no mention of {len(missing_subjects)} named subject(s): "
            f"{', '.join(missing_subjects[:5])}. "
            "Per-subject targeted queries are needed before synthesis."
        )
    return None


def reflect(request: TurnRequest, state: ResearchStateStore) -> ReflectionDecision:
    open_cells = state.contract.open_cells()
    partial_cells = state.contract.partial_cells()
    coverage = state.contract.coverage_ratio()
    if not open_cells and coverage >= 1.0:
        # Phase 12.2 — even with 100% cell fill, reject if evidence doesn't mention the subjects.
        relevance_issue = _topical_relevance_check(state)
        if relevance_issue:
            targeted_queries = [
                _targeted_query(subject, [state.contract.dimensions[0] if state.contract.dimensions else "overview"], request.message, tz=request.user_timezone)
                for subject in state.contract.subjects
                if not any(
                    (token := re.split(r"\s+", subject.lower())[0]) and token in (
                        " ".join([i.title or "", i.url or "", i.evidence or ""]).lower()
                    )
                    for i in state.evidence.items
                )
            ][:4]
            return ReflectionDecision(
                sufficient=False,
                open_subjects=state.contract.subjects,
                targeted_queries=targeted_queries,
                terminate_reason=None,
                coverage_ratio=coverage,
                next_action="continue",
                source="heuristic",
            )
        return ReflectionDecision(sufficient=True, terminate_reason="Coverage contract fully satisfied.", coverage_ratio=coverage, next_action="publish", source="heuristic")
    if state.budget_ledger.stopped:
        return ReflectionDecision(sufficient=True, terminate_reason=state.budget_ledger.stop_reason or "budget exhausted", coverage_ratio=coverage, next_action="stop_with_gaps" if open_cells else "publish", source="heuristic")
    exhausted_cells = [cell for cell in open_cells if cell.attempts >= _max_attempts_per_cell(request)]
    if open_cells and len(exhausted_cells) == len(open_cells):
        return ReflectionDecision(sufficient=True, open_dimensions=_dedupe([cell.dimension for cell in open_cells]), open_subjects=_dedupe([cell.subject for cell in open_cells]), terminate_reason="Remaining cells have already had targeted follow-up attempts.", coverage_ratio=coverage, next_action="stop_with_gaps", source="heuristic")
    try:
        prompt = resolve_prompt(
            "agent.research.reflection.default",
            agent_id="reflection",
            fallback_system_prompt=REFLECTION_PROMPT,
            variables=["state"],
            profile=state.plan.research_profile,
        )
        response = model_client.complete(
            [
                {"role": "system", "content": prompt.system_prompt},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "objective": state.brief.objective,
                            **temporal_context(request.user_timezone),
                            "coverage_ratio": coverage,
                            "open_cells": [{"subject": cell.subject, "dimension": cell.dimension, "attempts": cell.attempts} for cell in open_cells[:14]],
                            "partial_cells": [{"subject": cell.subject, "dimension": cell.dimension, "notes": cell.notes} for cell in partial_cells[:8]],
                            "queries_already_tried": state.query_history[-10:],
                            "worker_reports": [
                                {
                                    "question": report.question,
                                    "query": report.query,
                                    "assigned_subject": report.assigned_subject,
                                    "assigned_dimension": report.assigned_dimension,
                                    "confidence": report.self_assessed_confidence,
                                    "claim_count": len(report.claims),
                                    "missing_evidence": report.missing_evidence,
                                    "retry_queries": report.retry_queries,
                                }
                                for report in state.worker_reports[-10:]
                            ],
                            "source_count": len(state.source_inventory),
                            "iteration": state.iteration,
                            "budget_remaining": {"tool_calls": state.budget_ledger.remaining_tool_calls(), "source_reads": state.budget_ledger.remaining_source_reads()},
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            role="reflection",
            quality_mode=request.quality_mode,
            overrides=request.model_overrides,
            max_tokens=900 if request.research_level == "deep" else 500,
            timeout_s=15,
        )
        payload = _parse_json(response.text)
        decision = ReflectionDecision.model_validate(payload)
        decision.coverage_ratio = coverage
        decision.model_used = response.model_used
        decision.latency_ms = response.latency_ms
        decision.cost_usd = response.cost_usd
        decision.source = "llm"
        if decision.sufficient and decision.next_action == "continue":
            decision.next_action = "publish" if not open_cells else "stop_with_gaps"
        # Phase 12.2 — override LLM "sufficient" if evidence doesn't actually mention subjects
        if decision.sufficient and decision.next_action == "publish":
            relevance_issue = _topical_relevance_check(state)
            if relevance_issue:
                decision.sufficient = False
                decision.next_action = "continue"
                decision.terminate_reason = None
                if not decision.targeted_queries:
                    decision.targeted_queries = [
                        _targeted_query(subject, [state.contract.dimensions[0] if state.contract.dimensions else "overview"], request.message, tz=request.user_timezone)
                        for subject in (decision.open_subjects or state.contract.subjects)[:4]
                    ]
                if not decision.open_subjects:
                    decision.open_subjects = state.contract.subjects
        return decision
    except Exception as exc:
        logger.warning("agent reflection failed; using deterministic follow-up: %s", exc)
        queries = [_targeted_query(cell.subject, [cell.dimension], request.message, tz=request.user_timezone) for cell in open_cells[:4]]
        return ReflectionDecision(sufficient=not queries, open_dimensions=_dedupe([cell.dimension for cell in open_cells]), open_subjects=_dedupe([cell.subject for cell in open_cells]), targeted_queries=queries, terminate_reason=f"Reflection agent failed: {exc}" if not queries else None, coverage_ratio=coverage, next_action="continue" if queries else "stop_with_gaps", source="heuristic")


def verify_citations_semantically(
    answer: str,
    evidence,
    *,
    overrides: dict[str, str] | None = None,
    expected_primary_role: str | None = None,
) -> CitationVerification:
    if not answer or not evidence.items:
        return CitationVerification(source="skipped")
    evidence_index = {
        item.source_id: f"[{item.source_id}] {item.title}\nURL: {item.url}\nEvidence: {item.evidence[:1600]}"
        for item in evidence.items
    }
    cited_ids = set(re.findall(r"\[(S\d+)\]", answer))
    hallucinated = sorted(cited_ids - set(evidence_index))
    if not cited_ids:
        return CitationVerification(repair_needed=True, repair_instruction="The answer contains no [S#] citations. Add citations for factual claims.", source="heuristic")
    try:
        prompt = resolve_prompt(
            "agent.research.citation_verifier.default",
            agent_id="citation_verifier",
            fallback_system_prompt=CITATION_VERIFICATION_PROMPT,
            variables=["answer", "evidence_pack"],
        )
        user_payload: dict = {
            "answer": answer[:10000],
            "evidence_pack": list(evidence_index.values()),
            "hallucinated_citations_detected": hallucinated,
        }
        # Phase 5 — pass role context so the verifier can check role conflicts.
        if expected_primary_role:
            user_payload["expected_primary_role"] = expected_primary_role
        # Include a summary of claim roles present to help the verifier spot conflicts.
        claim_roles = sorted({c.claim_role for c in (evidence.claims or [])})
        if claim_roles:
            user_payload["claim_roles_in_evidence"] = claim_roles
        # Phase 2c — stale-evidence claims the verifier must check are flagged, not
        # presented as current (see compute_staleness in research_evidence.py).
        stale_claims = [
            {"source_id": c.source_id, "text": c.text[:200]}
            for c in (evidence.claims or [])
            if c.staleness == "stale"
        ]
        if stale_claims:
            user_payload["claim_staleness_summary"] = stale_claims
        response = model_client.complete(
            [
                {"role": "system", "content": prompt.system_prompt},
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
            ],
            role="citation_verifier",
            quality_mode="standard",
            overrides=overrides,
            max_tokens=900,
            timeout_s=20,
        )
        payload = _parse_json(response.text)
        result = CitationVerification.model_validate(payload)
        result.hallucinated_citations = sorted(set(result.hallucinated_citations) | set(hallucinated))
        # Phase 5 — role_mismatch_issues and unresolved_conflicts trigger repair.
        # Phase 8 — leads_with_disclaimer triggers repair (LLM judgment, not phrase list).
        # Phase 9 — asks_permission_to_continue triggers repair.
        result.repair_needed = bool(
            result.repair_needed
            or result.unsupported_claims
            or result.hallucinated_citations
            or result.role_mismatch_issues
            or result.unresolved_conflicts
            or result.leads_with_disclaimer
            or result.asks_permission_to_continue
            or result.unflagged_stale_claims
        )
        if result.repair_needed and not result.repair_instruction:
            result.repair_instruction = _citation_repair_instruction(result)
        result.model_used = response.model_used
        result.latency_ms = response.latency_ms
        result.cost_usd = response.cost_usd
        result.source = "llm"
        return result
    except Exception as exc:
        logger.warning("agent citation verification failed; using heuristic fallback: %s", exc)
        return CitationVerification(hallucinated_citations=hallucinated, repair_needed=bool(hallucinated), repair_instruction="Remove or fix hallucinated citation markers." if hallucinated else "", source="heuristic")


def judge_research_final(request: TurnRequest, state: ResearchStateStore, answer: str) -> JudgeVerdict:
    issues: list[str] = []
    score = 0.35
    if state.evidence.items:
        score += min(0.20, 0.035 * len(state.evidence.items))
    coverage = state.contract.coverage_ratio()
    score += coverage * 0.25
    citation_count = len(re.findall(r"\[S\d+\]", answer or ""))
    if citation_count:
        score += min(0.15, 0.035 * citation_count)
    else:
        issues.append("No source citations in answer.")
    if len(answer or "") < 250:
        score -= 0.10
        issues.append("Answer too short for deep research.")
    if state.plan.research_profile == "technical_architecture" and request.research_level == "deep":
        section_count = len(re.findall(r"(?m)^(?:#{1,3}\s+|\d+\.\s+)[A-Z0-9][^\n]{3,}", answer or ""))
        required_terms = ["orchestr", "workflow", "evidence", "guardrail", "judge", "runtime", "failure", "budget", "trace", "source"]
        missing_terms = [term for term in required_terms if term not in (answer or "").lower()]
        technical_sources = [
            item for item in state.evidence.items
            if score_technical_density(Source(title=item.title, url=item.url, content=item.evidence)) >= 0.35
        ]
        technical_claims = [
            claim for claim in state.evidence.claims
            if claim.claim_type in {"architecture", "implementation", "tradeoff", "failure", "statistic"}
        ]
        if len(answer or "") < 4500:
            score -= 0.28
            issues.append("Deep technical architecture report is far too short; expected a detailed multi-section report.")
        elif len(answer or "") < 9000:
            score -= 0.16
            issues.append("Deep technical architecture report is still short for deep mode; expand with concrete implementation detail.")
        if section_count < 10:
            score -= 0.12
            issues.append("Technical architecture report lacks enough concrete sections for deep mode.")
        if missing_terms:
            score -= min(0.16, 0.025 * len(missing_terms))
            issues.append("Technical architecture report misses required implementation concepts: " + ", ".join(missing_terms[:6]))
        if len(technical_sources) < 4:
            score -= 0.12
            issues.append("Evidence pack has too few technically dense sources.")
        if len(technical_claims) < max(8, state.plan.min_evidence_items):
            score -= 0.12
            issues.append("Evidence pack has too few typed technical claims for a deep architecture report.")
    completion_issues = _framework_comparison_completion_issues(state, answer)
    if completion_issues:
        score -= min(0.35, 0.12 * len(completion_issues))
        issues.extend(completion_issues)
    disclaimer_issues = _evidence_disclaimer_issues(state, answer)
    if disclaimer_issues:
        score -= min(0.42, 0.16 * len(disclaimer_issues))
        issues.extend(disclaimer_issues)
    gap_saturation_issues = _answer_gap_saturation_issues(request, state, answer)
    if gap_saturation_issues:
        score -= min(0.35, 0.14 * len(gap_saturation_issues))
        issues.extend(gap_saturation_issues)
    consistency_issues = _answer_internal_consistency_issues(state, answer)
    if consistency_issues:
        score -= min(0.30, 0.15 * len(consistency_issues))
        issues.extend(consistency_issues)
    open_cells = state.contract.open_cells()
    if open_cells:
        score -= min(0.18, 0.025 * len(open_cells))
        issues.append(f"{len(open_cells)} required coverage cell(s) remain empty: " + ", ".join(f"{cell.subject}/{cell.dimension}" for cell in open_cells[:5]))
    if state.evidence.contradictions:
        score -= 0.05
        issues.append("Contradictions in evidence should be surfaced.")
    # Phase 5 — consume last citation verification's role/conflict signals.
    # Phase 8 — consume leads_with_disclaimer LLM judgment field.
    citation_result = getattr(state, "last_citation_verification", None)
    if citation_result is not None:
        if citation_result.role_mismatch_issues:
            score -= min(0.12, 0.06 * len(citation_result.role_mismatch_issues))
            issues.extend(citation_result.role_mismatch_issues)
        if citation_result.unresolved_conflicts:
            score -= min(0.10, 0.05 * len(citation_result.unresolved_conflicts))
            issues.extend(citation_result.unresolved_conflicts)
        if citation_result.leads_with_disclaimer:
            score -= 0.20
            issues.append("Answer leads with a disclaimer/caveat block before delivering substance (LLM judgment). Repair to lead with substance and disclose gaps inline.")
        # Phase 9 — consume asks_permission_to_continue LLM judgment field.
        if citation_result.asks_permission_to_continue:
            score -= 0.15
            issues.append("Answer ends by soliciting user permission to do more research (LLM judgment). Repair: state remaining gaps plainly and do not ask for authorization to continue.")
    score = max(0.0, min(1.0, score))
    threshold = state.plan.judge_threshold or 0.78
    if (disclaimer_issues or gap_saturation_issues or consistency_issues) and not state.budget_ledger.stopped and state.budget_ledger.remaining_tool_calls() > 0 and state.budget_ledger.remaining_source_reads() > 0:
        return JudgeVerdict(
            can_publish=False,
            repair_needed=False,
            score=score,
            issues=issues,
            specific_gaps=[f"{cell.subject}/{cell.dimension}" for cell in open_cells[:8]],
            next_action="research_more",
        )
    if score >= threshold and not open_cells:
        return JudgeVerdict(can_publish=True, repair_needed=False, score=score, issues=issues, next_action="publish")
    if open_cells and not state.budget_ledger.stopped and state.iteration < _max_iterations_for(request):
        return JudgeVerdict(can_publish=False, repair_needed=False, score=score, issues=issues, specific_gaps=[f"{cell.subject}/{cell.dimension}" for cell in open_cells[:8]], next_action="research_more")
    repair_instruction = " ".join(issues) or "Improve the answer with better citation use and explicit caveats."
    if open_cells:
        repair_instruction += " Explicitly disclose unresolved public-evidence gaps: " + "; ".join(f"{cell.subject} {cell.dimension}" for cell in open_cells[:6])
    return JudgeVerdict(can_publish=score >= max(0.55, threshold - 0.20), repair_needed=True, repair_instruction=repair_instruction, specific_gaps=[f"{cell.subject}/{cell.dimension}" for cell in open_cells[:8]], score=score, issues=issues, next_action="repair_answer" if score >= 0.45 else "stop_with_gaps")


def _framework_comparison_completion_issues(state: ResearchStateStore, answer: str) -> list[str]:
    # Phase 8 — renamed source suffix: framework_comparison → multi_subject_comparison.
    # This function now covers any multi-subject comparison, not just AI frameworks.
    if not state.contract.source.endswith("multi_subject_comparison"):
        return []
    text = answer or ""
    lower = text.lower()
    issues: list[str] = []
    missing_sections: list[str] = []
    for subject in state.contract.subjects:
        subject_pattern = re.escape(subject)
        if not re.search(rf"(?mi)^#+\s+(?:section\s+\d+[:.\s-]+)?{subject_pattern}\b", text):
            missing_sections.append(subject)
    if missing_sections:
        issues.append("Multi-subject comparison answer is incomplete; missing detailed sections for: " + ", ".join(missing_sections[:5]))
    tail = lower[-1800:]
    if not any(term in tail for term in ("final recommendation", "ranked recommendation", "recommendation matrix", "bottom line", "decision logic")):
        issues.append("Multi-subject comparison answer lacks a closing recommendation section near the end.")
    if re.search(r"(?m)(?:^|\n)\s*[-*]\s*$|(?:\bcomponents|\bagents|\bpipelines|\bcoordination)\s*$", text.strip(), flags=re.IGNORECASE):
        issues.append("Multi-subject comparison answer appears to end mid-section or mid-sentence.")
    empty_subjects = _framework_subjects_with_empty_sections(state, text)
    if empty_subjects:
        issues.append(
            "Multi-subject comparison substitutes validation notes for requested detail: "
            + ", ".join(empty_subjects[:5])
        )
    return issues


def _framework_subjects_with_empty_sections(state: ResearchStateStore, answer: str) -> list[str]:
    # Phase 8 — now covers any multi_subject_comparison contract, not just AI frameworks.
    if not state.contract.source.endswith("multi_subject_comparison"):
        return []
    empty_subjects: list[str] = []
    empty_patterns = (
        r"\bnot\s+(?:directly\s+)?described in evidence\b",
        r"\bnot documented in evidence\b",
        r"\bnot specified in evidence\b",
        r"\bnot evidenced\b",
        r"\bno substantive evidence\b",
        r"\bno evidence in (?:this )?pack\b",
        r"\brequires? dedicated research\b",
        r"\bvalidation note\b",
    )
    dimensions = ["architecture", "coordination", "production", "failure"]
    for subject in state.contract.subjects:
        subject_pattern = re.escape(subject)
        heading_match = re.search(rf"(?mi)^#{1,3}\s+{subject_pattern}\b", answer)
        if heading_match:
            following = answer[heading_match.end() : heading_match.end() + 1200]
            next_heading = re.search(r"(?m)^#{1,3}\s+", following)
            section = following[: next_heading.start()] if next_heading else following
        else:
            row_match = re.search(rf"(?mi)^\|\s*\*?\*?{subject_pattern}\*?\*?\s*\|(.+)$", answer)
            section = row_match.group(0) if row_match else ""
        section_lower = section.lower()
        empty_hits = sum(1 for pattern in empty_patterns if re.search(pattern, section_lower))
        dimension_hits = sum(1 for term in dimensions if term in section_lower)
        if empty_hits >= 2 or (empty_hits >= 1 and dimension_hits < 2):
            empty_subjects.append(subject)
    return empty_subjects


def _evidence_disclaimer_issues(state: ResearchStateStore, answer: str) -> list[str]:
    """Phase 8 — keyword phrase list REMOVED; disclaimer detection now delegated to LLM judgment
    in verify_citations_semantically() via CitationVerification.leads_with_disclaimer.

    This function retains only the structural/multi-subject checks that can be computed
    deterministically from the answer text and contract state — specifically:
    1. The model exposing internal research-judge instructions in the output (a different bug).
    2. Multi-subject comparison contracts leaving too many cells as 'not in evidence'.
    3. Provisional-recommendation signal in multi-subject comparison context.

    The original phrase list (hard_disclaimer_terms + soft_disclaimer_terms) is deleted because
    it is a keyword blocklist that doesn't generalize — the model can paraphrase around any term.
    The LLM judgment call (leads_with_disclaimer) handles this correctly for any phrasing.
    """
    text = answer or ""
    lower = text.lower()
    issues: list[str] = []

    # Structural check: model exposing internal judge instructions
    if "research judge" in lower and any(term in lower for term in ("deeper research", "deeper retrieval", "treat as a trigger")):
        issues.append("Answer exposes internal research-judge instructions instead of completing the research.")

    # Structural check: multi-subject comparison contract with too many cells left empty
    not_in_evidence_count = len(
        re.findall(
            r"\bnot in evidence\b|\bnot supported by this evidence pack\b|\bno usable evidence\b|"
            r"\bnot directly described in evidence\b|\bnot described in evidence\b|\bnot documented in evidence\b|"
            r"\bunverified from this evidence pack\b|\bnot specified in evidence\b|\bnot evidenced\b|"
            r"\bno substantive evidence in this pack\b|\bno evidence in pack\b|\brequires? dedicated research\b",
            lower,
        )
    )
    if state.contract.source.endswith("multi_subject_comparison") and not_in_evidence_count >= 4:
        issues.append("Multi-subject comparison leaves too many requested cells as 'not in evidence'.")
    if state.contract.source.endswith("multi_subject_comparison") and (
        "provisional, single-source" in lower or "provisional recommendation" in lower
    ):
        issues.append("Multi-subject recommendation is explicitly single-source/provisional rather than decision-grade.")
    return issues


def _answer_gap_saturation_issues(request: TurnRequest, state: ResearchStateStore, answer: str) -> list[str]:
    lower = (answer or "").lower()
    if not lower:
        return []
    gap_count = len(
        re.findall(
            r"\bnot in evidence\b|\bno evidence\b|\bnot specified in evidence\b|\bnot documented\b|"
            r"\bnot supported\b|\bgap\b|\bgaps\b|\bdoes not contain\b|\babsence of evidence\b",
            lower,
        )
    )
    requested_gap_dimensions = [
        term for term in (
            "interoperability",
            "implementation",
            "total cost",
            "tco",
            "failure",
            "failures",
            "deployment failure",
            "architecture",
        )
        if term in (request.message or "").lower() and term in lower
    ]
    if gap_count >= 6 and len(requested_gap_dimensions) >= 2:
        return [
            "Answer repeatedly marks requested dimensions as evidence gaps while research budget remains; run targeted follow-up before publishing."
        ]
    if len(state.evidence.items) >= state.plan.min_evidence_items and gap_count >= 10:
        return [
            "Answer has enough bound items but still saturates the response with evidence gaps, indicating the source set is off-target."
        ]
    return []


def _answer_internal_consistency_issues(state: ResearchStateStore, answer: str) -> list[str]:
    text = answer or ""
    lower = text.lower()
    if not lower or not state.contract.subjects:
        return []

    all_subjects_claim = bool(
        re.search(
            r"\b(?:all\s+\d+|all\s+(?:five|four|three)|all\s+(?:platforms|vendors|frameworks|products|subjects))\b"
            r".{0,140}\b(?:evidence|source|document|architecture|interoperability|failure|deployment|coverage)",
            lower,
            flags=re.DOTALL,
        )
        or re.search(
            r"\b(?:evidence|source|document|architecture|interoperability|failure|deployment|coverage)\b"
            r".{0,140}\b(?:for|across)\s+all\s+(?:\d+|five|four|three|platforms|vendors|frameworks|products|subjects)\b",
            lower,
            flags=re.DOTALL,
        )
    )
    if not all_subjects_claim:
        return []

    empty_subjects = _framework_subjects_with_empty_sections(state, text)
    if not empty_subjects:
        no_evidence_subjects: list[str] = []
        for subject in state.contract.subjects:
            pattern = re.escape(subject.lower())
            subject_pos = lower.find(subject.lower())
            if subject_pos < 0:
                continue
            section = lower[subject_pos : subject_pos + 1200]
            if re.search(
                rf"\bno\s+{pattern}[a-z0-9\s-]{{0,80}}\b(?:evidence|source|architecture|interoperability|implementation|pricing|failure)"
                r"|\binsufficient evidence\b|\bcannot responsibly compare\b|\bno evidence in (?:the )?pack\b",
                section,
            ):
                no_evidence_subjects.append(subject)
        empty_subjects = no_evidence_subjects

    if empty_subjects:
        return [
            "Answer overclaims evidence coverage for all subjects while later saying evidence is absent for: "
            + ", ".join(empty_subjects[:5])
            + "."
        ]
    return []


# ---------------------------------------------------------------------------
# Query construction helpers
# ---------------------------------------------------------------------------

def _tech_arch_anchor_queries(original_message: str) -> list[str]:
    msg_lower = (original_message or "").lower()
    if _is_framework_comparison_request(original_message):
        return [
            "LangGraph CrewAI AutoGen Haystack LlamaIndex Workflows official documentation architecture agents workflow",
            "LangGraph CrewAI AutoGen Haystack LlamaIndex production deployment observability checkpointing",
            "AutoGen Microsoft Agent Framework migration maintenance successor official",
            "multi-agent framework failure modes benchmark taxonomy MAST agentic AI",
        ]
    if "deep research" in msg_lower or "deep_research" in msg_lower:
        return ["agentic deep research multi-agent architecture implementation", "LLM research agent planning loop evidence retrieval site:arxiv.org", "LLM research agent planning loop evidence retrieval site:github.com", "autonomous research agent orchestration evidence synthesis 2024"]
    if "multi-agent" in msg_lower or "multi agent" in msg_lower:
        return ["multi-agent LLM orchestration architecture patterns", "multi-agent AI system design orchestrator planner executor", "agentic workflow multi-agent framework implementation site:github.com", "agentic workflow multi-agent framework implementation site:arxiv.org"]
    if "rag" in msg_lower or "retrieval" in msg_lower:
        return ["RAG architecture retrieval augmented generation production implementation", "agentic RAG planning retrieval evidence grounding 2024", "retrieval augmented generation system design components site:arxiv.org"]
    return [f"{original_message[:80]} architecture implementation", f"{original_message[:60]} system design components site:arxiv.org", f"{original_message[:60]} system design components site:github.com"]


def _vendor_comparison_anchor_queries(original_message: str) -> list[str]:
    if _llm_vendor_comparison_subject(original_message):
        return ["OpenAI API models pricing official docs GPT chatbot", "Anthropic Claude API models pricing official docs chatbot", "Google Gemini API models pricing official docs chatbot", "LLM API model pricing comparison OpenAI Anthropic Google Gemini Claude GPT"]
    entities = _extract_named_comparison_subjects(original_message)
    focus = _comparison_focus_terms(original_message)
    if len(entities) >= 2:
        subject = " ".join(entities[:5])
        return [
            f"{subject} {focus} official documentation",
            f"{subject} {focus} pricing official docs",
            f"{subject} {focus} comparison limitations",
        ]
    subject = _compact_search_subject(original_message)
    return [f"{subject} pricing comparison official docs", f"{subject} vs alternatives review site:g2.com OR site:capterra.com", f"{subject} analyst comparison 2024 2025"]


def _market_landscape_anchor_queries(original_message: str) -> list[str]:
    subject = _compact_search_subject(original_message)
    return [f"{subject} market size growth forecast 2024 2025", f"{subject} competitive landscape report site:gartner.com OR site:forrester.com OR site:idc.com", f"{subject} industry analysis market share"]


def _policy_regulatory_anchor_queries(original_message: str) -> list[str]:
    subject = _compact_search_subject(original_message)
    return [f"{subject} regulation official guidance site:gov OR site:europa.eu", f"{subject} enforcement action penalty compliance requirement", f"{subject} regulatory update 2024 2025"]


def _strategy_brief_anchor_queries(original_message: str) -> list[str]:
    subject = _compact_search_subject(original_message)
    return [f"{subject} strategic analysis business case", f"{subject} case study ROI outcomes"]


def _implementation_plan_anchor_queries(original_message: str) -> list[str]:
    subject = _compact_search_subject(original_message)
    return [f"{subject} implementation guide best practices", f"{subject} rollout plan milestones lessons learned"]


def _per_entity_anchor_queries(message: str, budget: ResearchBudget) -> list[SearchWorkerPlan]:
    """Phase 6.1 — One targeted primary-source query per named entity in a multi-subject
    comparison request.

    Guarantees each named entity gets its own dedicated search, not just a slot in a broad
    "top N frameworks" query that may only surface 2–3 of the N by name.
    Added alongside (not replacing) the existing anchor queries.
    """
    entities = _extract_named_comparison_subjects(message)
    if len(entities) < 3:
        return []
    workers: list[SearchWorkerPlan] = []
    focus = _comparison_focus_terms(message)
    for entity in entities:
        # For known tech entities, prefer official docs/GitHub
        entity_lower = entity.lower().replace(" ", "")
        if "autogen" in entity_lower:
            query = "AutoGen Microsoft Agent Framework official docs architecture agentchat 2025"
        elif "haystack" in entity_lower:
            query = f"Haystack deepset official documentation architecture pipeline agents 2025"
        elif "llamaindex" in entity_lower or "llama" in entity_lower:
            query = f"LlamaIndex Workflows official docs event-driven agents 2025"
        elif "langgraph" in entity_lower:
            query = f"LangGraph official docs architecture state graph agents site:langchain.com"
        elif "crewai" in entity_lower:
            query = f"CrewAI official docs role-based crew agent coordination site:docs.crewai.com"
        elif entity_lower in {"awss3", "amazons3", "s3"}:
            query = "site:docs.aws.amazon.com/AmazonS3/latest/userguide OR site:aws.amazon.com/s3/pricing AWS S3 durability storage classes egress"
        elif "googlecloudstorage" in entity_lower or entity_lower == "gcs":
            query = "site:cloud.google.com/storage/docs OR site:cloud.google.com/storage/pricing Google Cloud Storage durability storage classes egress"
        elif "azureblobstorage" in entity_lower or entity_lower == "azureblob":
            query = "site:learn.microsoft.com/azure/storage OR site:azure.microsoft.com/en-us/pricing/details/storage/blobs Azure Blob Storage redundancy durability access tiers egress"
        else:
            query = f"{entity} official documentation {focus}"
        workers.append(
            SearchWorkerPlan(
                question=f"Per-entity anchor: {entity} primary source",
                query=query[:220],
                rationale=f"Phase 6 breadth guarantee: one dedicated primary-source query for {entity}.",
                max_results=budget.max_results_per_worker,
                discovery_domain="documentation",
            )
        )
    # Cap to avoid blowing the worker budget — one per entity up to 5
    return workers[:5]


def _status_check_queries(message: str, budget: ResearchBudget) -> list[SearchWorkerPlan]:
    """Phase 6.4 — One deprecation/successor/maintenance-mode query per named tech entity.

    Triggered only for software/product/framework comparisons. These queries surface
    whether any named entity has been superseded, renamed, or moved to maintenance mode
    — the failure mode that caused AutoGen to be evaluated as actively developed when
    Microsoft had already announced the successor.
    """
    if not _is_tech_entity_comparison(message):
        return []
    entities = _extract_named_comparison_subjects(message)
    if len(entities) < 2:
        return []
    workers: list[SearchWorkerPlan] = []
    for entity in entities[:6]:  # Cap at 6 to stay within budget
        query = (
            f"{entity} maintenance mode OR deprecated OR successor OR discontinued "
            f"OR end-of-life OR EOL OR renamed OR archived 2024 2025"
        )
        workers.append(
            SearchWorkerPlan(
                question=f"Status check: {entity} deprecation / successor / lifecycle",
                query=query[:220],
                rationale=(
                    f"Phase 6 status-volatility check: surface any deprecation, rename, "
                    f"successor, or maintenance-mode event for {entity}."
                ),
                max_results=min(3, budget.max_results_per_worker),
                discovery_domain="news",
            )
        )
    return workers


def _domain_discovery_workers(request: TurnRequest, profile: ResearchProfile, budget: ResearchBudget) -> list[SearchWorkerPlan]:
    subject = _compact_search_subject(request.message)
    # Phase 8 — generalized: use _is_multi_subject_comparison() (via _is_framework_comparison_request alias)
    # so this path fires for any N≥3 named-entity comparison in technical_architecture context,
    # not just the hardcoded AI framework list.
    if profile == "technical_architecture" and _is_multi_subject_comparison(request.message):
        workers: list[SearchWorkerPlan] = []
        # Use generic subject extraction first; fall back to framework-specific extractor if that yields more
        subjects = _extract_named_comparison_subjects(request.message)
        for framework in subjects[:5]:
            query = f"{framework} official docs architecture workflow production deployment 2025"
            # Retain the AutoGen-specific query since it's high-signal for the status-volatility case
            if framework.lower() in ("autogen", "ag2"):
                query = "AutoGen Microsoft Agent Framework official migration maintenance agentchat core group chat"
            workers.append(
                SearchWorkerPlan(
                    question=f"Domain lane: official architecture and production evidence for {framework}",
                    query=query,
                    rationale="Prioritize primary documentation and lifecycle evidence for multi-subject comparison.",
                    max_results=budget.max_results_per_worker,
                    discovery_domain="documentation",
                )
            )
        return workers[: max(0, min(5, budget.max_search_workers))]
    if profile == "vendor_comparison" and _llm_vendor_comparison_subject(request.message):
        return [
            SearchWorkerPlan(question="Domain lane: OpenAI official model and pricing evidence", query="site:platform.openai.com/docs/models OR site:openai.com/api/pricing OpenAI GPT API models pricing chatbot", rationale="Find OpenAI-owned model catalog and pricing evidence.", max_results=budget.max_results_per_worker, discovery_domain="primary"),
            SearchWorkerPlan(question="Domain lane: Anthropic official Claude model and pricing evidence", query="site:docs.anthropic.com OR site:anthropic.com/pricing Claude API models pricing chatbot", rationale="Find Anthropic-owned model catalog and pricing evidence.", max_results=budget.max_results_per_worker, discovery_domain="primary"),
            SearchWorkerPlan(question="Domain lane: Google Gemini official model and pricing evidence", query="site:ai.google.dev/gemini-api/docs/models OR site:ai.google.dev/gemini-api/docs/pricing Gemini API models pricing chatbot", rationale="Find Google-owned Gemini model catalog and pricing evidence.", max_results=budget.max_results_per_worker, discovery_domain="primary"),
            SearchWorkerPlan(question="Domain lane: neutral LLM API comparison evidence", query="LLM API model pricing benchmark comparison OpenAI Anthropic Google Gemini Claude GPT chatbot", rationale="Find credible cross-provider comparisons and caveats.", max_results=budget.max_results_per_worker, discovery_domain="general"),
        ][: max(0, min(4, budget.max_search_workers))]
    workers: list[SearchWorkerPlan] = []
    policy = PROFILE_POLICIES.get(profile, PROFILE_POLICIES["general"])
    for domain, query_template, rationale in policy.domain_specs:
        query = query_template.format(subject=subject)
        workers.append(SearchWorkerPlan(question=f"Domain lane: {domain} evidence for {subject}", query=query[:220], rationale=rationale, max_results=budget.max_results_per_worker, discovery_domain=domain))  # type: ignore[arg-type]
    return workers[: max(0, min(4, budget.max_search_workers))]


def _compact_search_subject(message: str) -> str:
    if _llm_vendor_comparison_subject(message):
        return "LLM API models OpenAI Anthropic Google Gemini Claude GPT chatbot"
    subject_phrase = _extract_search_subject_phrase(message)
    if subject_phrase:
        return _strip_meta_instruction_terms(subject_phrase)
    stop = {"a", "an", "and", "are", "as", "be", "by", "conduct", "create", "deep", "detailed", "do", "easy", "explaining", "explain", "for", "from", "generate", "give", "in", "into", "latest", "like", "me", "of", "on", "perform", "regular", "report", "research", "the", "to", "with"}
    tokens = [token for token in re.findall(r"[a-z0-9.]{2,}", (message or "").lower()) if token not in stop]
    cleaned = " ".join(_dedupe(tokens)[:16])
    result = cleaned[:140] or (message or "")[:110] or "research topic"
    # _extract_search_subject_phrase's stop-marker list and this function's own
    # stopword set both predate the meta-instruction-leak fix -- neither
    # excludes words like "bullets"/"numbered"/"supporting detail", so a
    # trailing answer-formatting instruction in the user's message can survive
    # into the "subject" this function returns, which feeds directly into
    # _domain_discovery_workers and every *_anchor_queries() function. Route
    # through the same filter _targeted_query() uses rather than duplicating it.
    return _strip_meta_instruction_terms(result)


# Generic output-formatting/meta-instruction vocabulary -- describes how the
# ANSWER should be structured, not what to research. Same category as the
# existing meta-vocabulary _compact_search_subject() already filters out
# ("detailed", "report", "explain", "conduct", ...); not a proper-noun/
# collision blocklist.
_QUERY_META_INSTRUCTION_TERMS = frozenset({
    "bullets", "bullet", "numbered", "numbering", "format", "formatted",
    "structure", "structured", "detail", "details", "supporting",
    "above", "below", "max", "maximum", "section", "sections",
    "outline", "outlined",
})


def _strip_meta_instruction_terms(text: str) -> str:
    """Drop output-formatting/meta-instruction words (e.g. "3-5 bullets max
    then supporting detail by numbered above") before a message fragment is
    echoed into a search query.

    Confirmed root cause of a live failure: a user's answer-formatting
    preferences, embedded in the same message as the substantive research
    ask, got echoed verbatim by _targeted_query()'s default branch and
    literally matched the JDK `javah` tool in web search -- the same
    collision-with-an-unrelated-proper-noun failure mode as unresolved date
    idioms, just a different category of text that shouldn't be searched
    literally. Splits on whitespace rather than research_utils-style
    character-class tokenization so it doesn't fragment hyphenated tokens
    (e.g. a resolved ISO date like "2026-07-10").
    """
    if not text:
        return text
    tokens = [token for token in text.split() if token.strip(".,;:!?()[]{}\"'").lower() not in _QUERY_META_INSTRUCTION_TERMS]
    return " ".join(tokens)


def _comparison_focus_terms(message: str) -> str:
    text = (message or "").lower()
    focus_terms: list[str] = []
    term_map = [
        ("durability", ("durability", "reliability", "data loss")),
        ("availability SLA", ("availability", "sla", "uptime")),
        ("pricing", ("pricing", "price", "cost", "costs", "tco", "licensing")),
        ("tiers", ("tier", "tiers", "class", "classes", "storage class", "access tier")),
        ("egress", ("egress", "data transfer", "transfer out", "bandwidth")),
        ("redundancy", ("redundancy", "replication", "multi-region", "zone")),
        ("security compliance", ("security", "compliance", "soc 2", "hipaa")),
        ("API integration", ("api", "integration", "interoperability")),
        ("implementation", ("implementation", "deployment", "migration")),
        ("limitations", ("limitation", "limitations", "tradeoff", "tradeoffs", "failure")),
    ]
    for label, needles in term_map:
        if any(needle in text for needle in needles):
            focus_terms.append(label)
    if focus_terms:
        return " ".join(_dedupe(focus_terms)[:8])
    return "pricing capabilities limitations"


def _extract_search_subject_phrase(message: str) -> str:
    text = " ".join((message or "").lower().split())
    if not text:
        return ""
    text = re.sub(r"[""\"'`]", "", text)
    text = re.sub(r"\b(ai|llm)\b", lambda m: m.group(1), text)
    candidates: list[str] = []
    for match in re.finditer(r"\b(?:of|about|on|into|for)\s+(.+?)(?=\b(?:including|covering|include|ensure|output|final|create|generate|write|produce)\b|[.;:]|$)", text):
        phrase = match.group(1).strip()
        if phrase:
            candidates.append(phrase)
    for phrase in reversed(candidates):
        cleaned = _clean_search_subject_phrase(phrase)
        if _subject_phrase_is_useful(cleaned):
            return cleaned[:140]
    return ""


def _clean_search_subject_phrase(phrase: str) -> str:
    phrase = re.sub(r"\b(the|a|an)\b", " ", phrase)
    phrase = re.sub(r"\b(like|such as|including)\b", " ", phrase)
    phrase = re.sub(r"\b(system architecture|architecture|system design|components|workflows?|workflow|mechanisms?|integration|explaining|explaining)\b", " ", phrase)
    phrase = re.sub(r"[^a-z0-9.+#/-]+", " ", phrase)
    # "by"/"then" are already excluded from _compact_search_subject's tokenizer-fallback
    # stop set; this phrase-cleaning path lacked them, so a trailing connective fragment
    # left over after _strip_meta_instruction_terms removes the formatting-instruction
    # words around it (e.g. "javah 3-5 bullets max then supporting detail by numbered
    # above" -> "javah 3-5 then by") would survive into the search query. Not a
    # proper-noun/collision blocklist -- generic connective words, same category as the
    # rest of this stop2 set.
    stop2 = {"and", "or", "of", "for", "to", "in", "with", "by", "then", "platforms" if len(phrase.split()) <= 2 else ""}
    tokens = [token for token in phrase.split() if token and token not in stop2]
    return " ".join(_dedupe(tokens)).strip()


def _subject_phrase_is_useful(phrase: str) -> bool:
    tokens = phrase.split()
    if len(tokens) < 2:
        return False
    generic = {"system", "architecture", "design", "components", "workflows", "workflow", "mechanisms", "integration"}
    return any(token not in generic for token in tokens)


def _longform_timeout_s() -> int:
    return max(30, int(get_settings().longform_timeout_s or 180))


def _domain_for_query(query: str) -> Literal["general", "academic", "repository", "documentation", "news", "primary"]:
    lower = (query or "").lower()
    if "arxiv" in lower or "semanticscholar" in lower or "paper" in lower:
        return "academic"
    if "github" in lower or "repo" in lower:
        return "repository"
    if "docs" in lower or "documentation" in lower or "api" in lower:
        return "documentation"
    if "official" in lower or "regulator" in lower or "government" in lower:
        return "primary"
    if "latest" in lower or "news" in lower or "recent" in lower:
        return "news"
    return "general"


def _targeted_query(subject: str, dimensions: list[str], original: str, tz: str | None = None) -> str:
    raw_subject = " ".join(str(subject or "").split())
    if _is_owner_reliability_query(original):
        focus = " ".join(str(dim) for dim in dimensions if dim).lower()
        terms = "owner review reliability failure degradation warranty 12 months 18 months forum reddit"
        if "software" in focus or "firmware" in focus:
            terms = "owner forum firmware app bluetooth reliability failure warranty 12 months"
        elif "support" in focus or "warranty" in focus:
            terms = "owner review warranty claim support replacement failure forum reddit"
        return f"{raw_subject} {terms}".strip()[:220]
    if len(_extract_named_framework_subjects(original)) >= 3:
        return _framework_comparison_query(raw_subject, dimensions)
    inferred_profile = infer_research_profile(original)
    original_lower = (original or "").lower()
    dimension_text = " ".join(str(dim) for dim in dimensions if dim)
    vendor_signal = inferred_profile == "vendor_comparison" or any(
        term in original_lower
        for term in ("ehr", "total cost", "tco", "pricing", "licensing", "vendors", "platforms")
    )
    subject = _public_technical_subject(raw_subject) if inferred_profile == "technical_architecture" and not vendor_signal else raw_subject
    primary_dim = " ".join(str(dimensions[0] if dimensions else "").split())
    if subject and any(token in subject.lower() for token in ("tavily", "nimble", "you.com", "youcom")):
        return f"{subject} {primary_dim} official docs pricing security API enterprise".strip()[:180]
    if vendor_signal:
        vendor_subject = _compact_search_subject(original)
        if _llm_vendor_comparison_subject(original):
            vendor_subject = "OpenAI Anthropic Google Gemini Claude GPT LLM API chatbot"
        focus = dimension_text or primary_dim
        return f"{vendor_subject} {subject} {focus} pricing implementation interoperability failures official".strip()[:220]
    if inferred_profile == "technical_architecture":
        grounding = _tech_arch_grounding_term(raw_subject)
        return f"{subject} {primary_dim} {grounding}".strip()[:180]
    base = f"{subject} {primary_dim}".strip()
    # Deterministic counterpart to PLAN_PROMPT's query-hygiene/date-resolution
    # rules -- this code path has no LLM call to instruct, so a literal idiom
    # like "the day after" would otherwise echo straight into the query and
    # collide with an unrelated proper noun (e.g. the movie The Day After
    # Tomorrow). See research_utils.resolve_relative_date_phrases.
    resolved_original = resolve_relative_date_phrases(original, tz)
    # A user's answer-formatting preferences ("3-5 bullets max then
    # supporting detail by numbered above") can be embedded in the same
    # message as the substantive ask. Left in, they echo verbatim into the
    # query and can literally collide with an unrelated proper noun (e.g.
    # the JDK `javah` tool) -- confirmed root cause of a live failure.
    resolved_original = _strip_meta_instruction_terms(resolved_original)
    return f"{base} {resolved_original}".strip()[:220]


def _is_owner_reliability_query(message: str) -> bool:
    text = (message or "").lower()
    owner_terms = (
        "owner review",
        "owner reviews",
        "owner report",
        "owner reports",
        "owner experience",
        "owner experiences",
        "owners say",
        "user reviews",
        "customer reviews",
        "reddit",
        "forum",
        "community",
        "real-world",
        "real world",
    )
    reliability_terms = ("reliability", "failure rate", "failure rates", "failures", "degradation", "capacity retention", "long-term", "long term", "1-2 years", "1–2 years", "warranty claim")
    return any(term in text for term in owner_terms) and any(term in text for term in reliability_terms)


def _framework_comparison_query(subject: str, dimensions: list[str]) -> str:
    dimension_text = " ".join(dimensions).lower()
    if "lifecycle" in dimension_text or "ecosystem" in dimension_text:
        focus = "release notes roadmap maintenance migration successor ecosystem"
    elif "production" in dimension_text or "deployment" in dimension_text:
        focus = "production deployment observability persistence checkpointing enterprise"
    elif "failure" in dimension_text or "limitation" in dimension_text:
        focus = "failure modes limitations issues troubleshooting production"
    elif "coordination" in dimension_text or "multi-agent" in dimension_text:
        focus = "multi-agent coordination supervisor handoff group chat workflow"
    else:
        focus = "official docs architecture workflow agents state"
    if subject.lower() == "autogen":
        return f"AutoGen Microsoft Agent Framework official docs {focus}".strip()[:220]
    if subject.lower() == "haystack":
        return f"Haystack deepset official docs agents pipelines {focus}".strip()[:220]
    if subject.lower() == "llamaindex workflows":
        return f"LlamaIndex Workflows official docs agents event workflow {focus}".strip()[:220]
    return f"{subject} official docs {focus}".strip()[:220]


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
    """Phase 8 — now delegates to the generic _is_multi_subject_comparison() from Phase 6.
    The framework-name allowlist is no longer the primary gate; it's one optional input
    to subject extraction. This function is kept as an alias so call sites throughout
    the planner continue to work without renaming every caller.
    """
    return _is_multi_subject_comparison(message)


def _llm_vendor_comparison_subject(message: str) -> bool:
    text = (message or "").lower()
    if any(
        term in text
        for term in (
            "aws s3",
            "amazon s3",
            "google cloud storage",
            "azure blob",
            "blob storage",
            "object storage",
            "storage bucket",
            "egress",
            "durability",
        )
    ) and not any(term in text for term in ("openai", "anthropic", "claude", "gemini", "gpt", "llm")):
        return False

    explicit_llm_terms = (
        "llm",
        "language model",
        "large language model",
        "gpt",
        "openai",
        "anthropic",
        "claude",
        "gemini",
    )
    model_use_terms = ("model", "models", "chatbot", "chat bot", "ai assistant")
    decision_terms = ("api", "provider", "vendor", "pricing", "cost", "cheap", "accuracy", "fast", "fidelity", "affordable")
    if any(term in text for term in explicit_llm_terms) and any(term in text for term in decision_terms + model_use_terms):
        return True
    return any(term in text for term in model_use_terms) and any(term in text for term in decision_terms) and "storage" not in text


def _public_technical_subject(subject: str) -> str:
    text = subject.lower()
    mappings = [
        (("lead agent", "orchestration"), "multi-agent research orchestrator planner executor"),
        (("research planning", "coverage contract"), "research agent query planning coverage evaluation"),
        (("search worker", "provider"), "LLM search worker web retrieval provider routing"),
        (("source reading", "deep-link"), "web research agent source extraction crawling"),
        (("evidence binder", "citation"), "evidence grounding citation verification research agent"),
        (("reflection", "gap", "repair"), "agent reflection gap detection repair loop"),
        (("synthesis", "judge", "quality"), "LLM judge synthesis critic quality gate"),
        (("runtime", "durability", "budget", "observability"), "agent runtime tracing budget ledger observability"),
        (("guardrail", "security"), "LLM agent guardrails tool security policy"),
    ]
    for needles, replacement in mappings:
        if any(needle in text for needle in needles):
            return replacement
    return subject


def _tech_arch_grounding_term(subject: str) -> str:
    s = subject.lower()
    if any(t in s for t in ("orchestrat", "lead agent", "planner")):
        return "implementation"
    if any(t in s for t in ("evidence", "citation", "binder")):
        return "architecture"
    if any(t in s for t in ("search", "worker", "provider", "retrieval")):
        return "multi-agent"
    if any(t in s for t in ("reflect", "gap", "repair", "judge", "critic")):
        return "agentic loop"
    if any(t in s for t in ("guardrail", "security", "safety")):
        return "LLM guardrails"
    if any(t in s for t in ("budget", "ledger", "observ", "latency", "cost")):
        return "production"
    if any(t in s for t in ("memory", "state", "stateful")):
        return "stateful agent"
    if any(t in s for t in ("synthesis", "synthesiz")):
        return "RAG synthesis"
    return "agentic AI"


# ---------------------------------------------------------------------------
# Cell support helpers (used by update_contract_from_evidence)
# ---------------------------------------------------------------------------

def _evidence_supports_cell(item: EvidenceItem, cell: CoverageCell) -> bool:
    if cell.cell_id in item.supports_cells:
        return True
    return _text_supports_cell(f"{item.title} {item.url} {item.evidence}", cell)


def _text_supports_cell(text: str, cell: CoverageCell) -> bool:
    haystack = text.lower()
    subject_terms = _cell_terms(cell.subject)
    dimension_terms = _cell_terms(cell.dimension)
    subject_hit = any(term in haystack for term in subject_terms) if subject_terms else False
    dimension_hit = any(term in haystack for term in dimension_terms) if dimension_terms else False
    if subject_hit and dimension_hit:
        return True
    if subject_hit and cell.dimension.lower() in {"evidence", "coverage", "capabilities"}:
        return True
    if dimension_hit and any(term in haystack for term in ("architecture", "agent", "research", "system")):
        return True
    return False


def _cell_terms(value: str) -> list[str]:
    tokens = _meaningful_tokens(value)
    lowered = value.lower()
    aliases: list[str] = []
    alias_groups = [
        (("lead", "orchestrat"), ["orchestrator", "supervisor", "controller", "coordinator", "planner"]),
        (("planning", "coverage"), ["plan", "planning", "decomposition", "coverage", "evaluation", "query"]),
        (("search", "provider"), ["search", "retrieval", "provider", "browser", "crawl", "query"]),
        (("source", "reading", "deep-link"), ["source", "extract", "crawl", "read", "parse", "document"]),
        (("evidence", "citation"), ["evidence", "citation", "grounding", "provenance", "attribution", "source"]),
        (("reflection", "gap", "repair"), ["reflection", "critic", "judge", "repair", "gap", "feedback"]),
        (("synthesis", "judge", "quality"), ["synthesis", "generate", "judge", "critic", "quality", "evaluation"]),
        (("runtime", "budget", "observability"), ["runtime", "state", "trace", "telemetry", "budget", "cost", "latency", "durable"]),
        (("guardrail", "security"), ["guardrail", "security", "safety", "policy", "permission", "validation"]),
        (("responsibility",), ["role", "responsibility", "function", "owns", "manage"]),
        (("implementation", "pattern"), ["implementation", "architecture", "design", "pattern", "component"]),
        (("data", "model"), ["schema", "state", "data", "model", "store", "object"]),
        (("workflow",), ["workflow", "flow", "pipeline", "process", "loop", "sequence"]),
        (("failure", "handling"), ["failure", "error", "retry", "fallback", "timeout", "recovery"]),
        (("trade",), ["trade-off", "tradeoff", "latency", "cost", "quality", "accuracy", "complexity"]),
    ]
    for triggers, terms in alias_groups:
        if any(trigger in lowered for trigger in triggers):
            aliases.extend(terms)
    return _dedupe([*tokens, *aliases])


def _meaningful_tokens(value: str) -> list[str]:
    return [token for token in re.findall(r"[a-z0-9.]{3,}", value.lower()) if token not in {"the", "and", "for", "with", "from", "about", "latest", "research"}]


def _max_attempts_per_cell(request: TurnRequest) -> int:
    if request.quality_mode == "executive":
        return 4
    return 3 if request.research_level == "deep" else 1


def _max_iterations_for(request: TurnRequest) -> int:
    if request.quality_mode == "executive" and request.research_level == "deep":
        return 5
    if request.research_level == "deep":
        return 4
    return 3


def _citation_repair_instruction(result: CitationVerification) -> str:
    parts: list[str] = []
    if result.unsupported_claims:
        parts.append("Correct or remove unsupported claims: " + "; ".join(result.unsupported_claims[:3]))
    if result.hallucinated_citations:
        parts.append("Remove or fix hallucinated citation markers: " + ", ".join(result.hallucinated_citations[:6]))
    if result.unflagged_stale_claims:
        parts.append("Flag these citations as potentially outdated instead of presenting them as current: " + ", ".join(result.unflagged_stale_claims[:6]))
    return " ".join(parts) or "Repair citation support."


# ---------------------------------------------------------------------------
# Plan construction helpers
# ---------------------------------------------------------------------------

def _infer_primary_role_hint(message: str) -> str | None:
    """Heuristic: return the expected dominant claim role for this query.

    Used by Phase 4 (diversity backfill) and Phase 5 (citation verifier context).
    Returns None when no strong signal is present — callers must treat None as
    "no preference", not as background_context.
    """
    lower = message.lower()

    # Operational/experiential signals: "how long", "actually", "in practice", etc.
    operational_patterns = [
        r"\bhow long\b", r"\bactual(ly)?\b", r"\bin practice\b",
        r"\breal.world\b", r"\bexperience\b", r"\bprocessing time\b",
        r"\bwait time\b", r"\bbacklog\b", r"\bcurrently taking\b",
        r"\bpeople (are|have been) reporting\b",
        r"\bpractitioner\b",
    ]
    anecdotal_patterns = [
        r"\bforum\b", r"\breddit\b", r"\bcommunity (reports?|experience)\b",
        r"\breal.world experience\b", r"\bpeople (report|say|claim)\b",
        r"\bwhat (do|are) (people|users|developers|applicants)\b",
        r"\breally happening\b",
    ]
    policy_patterns = [
        r"\beligibilit(y|ies)\b", r"\brequirement\b", r"\bpolic(y|ies)\b",
        r"\bregulat(ion|ory)\b", r"\bofficial\b", r"\bUSCIS\b", r"\blaw\b",
        r"\bstatut(e|ory)\b", r"\bfiling fee\b", r"\bform [A-Z0-9\-]+\b",
    ]
    expert_patterns = [
        r"\banalyst\b", r"\bexpect(ation|ed)?\b", r"\bconsensus\b",
        r"\bforecast\b", r"\bprediction\b",
    ]

    op_hits = sum(1 for p in operational_patterns if re.search(p, lower))
    anec_hits = sum(1 for p in anecdotal_patterns if re.search(p, lower))
    policy_hits = sum(1 for p in policy_patterns if re.search(p, lower))
    expert_hits = sum(1 for p in expert_patterns if re.search(p, lower))

    # Anecdotal wins if explicit forum/community signals present and no strong policy signal.
    if anec_hits >= 2 and policy_hits == 0:
        return "anecdotal_case"
    # Operational wins if timing/practice signals dominate.
    if op_hits >= 2:
        return "operational_reality"
    if op_hits >= 1 and policy_hits == 0:
        return "operational_reality"
    # Policy wins if eligibility/requirements language dominates.
    if policy_hits >= 2 and op_hits == 0:
        return "official_policy"
    # Expert interpretation.
    if expert_hits >= 2 and op_hits <= 1:
        return "expert_interpretation"

    return None


def _fallback_plan(request: TurnRequest, goal: ResearchGoal | None = None) -> ResearchPlan:
    goal = goal or create_research_goal(request)
    profile = infer_research_profile(request.message)
    try:
        heuristic_brief = ResearchBrief(
            objective=request.message,
            research_profile=profile,
            scope_in=_extract_named_comparison_subjects(request.message),
            success_criteria=[],
            source="heuristic",
            fallback_reason="planner fallback from malformed/failed LLM plan",
        )
        contract = generate_coverage_contract(request, heuristic_brief)
        if contract.cells:
            plan = plan_from_contract(request, contract, goal.budget)
            plan.goal_id = goal.id
            plan.source = "heuristic_contract_fallback"
            plan.fallback_reason = "LLM research plan failed; generated deterministic contract-based worker plan."
            return plan
    except Exception as exc:
        logger.warning("contract-based fallback planning failed; using single-worker fallback: %s", exc)
    rationale = "Easy research uses one narrow source-grounding search." if request.research_level == "easy" else "Fallback worker from the original request."
    worker = SearchWorkerPlan(question=request.message, query=request.message, rationale=rationale, max_results=goal.budget.max_results_per_worker)
    return ResearchPlan(
        goal_id=goal.id,
        research_profile=profile,
        secondary_profiles=_secondary_profiles_for(request.message, profile),
        source_lanes=PROFILE_POLICIES[profile].source_lanes,
        questions=[request.message],
        search_queries=[request.message],
        workers=[worker],
        max_sources=goal.budget.max_sources,
        min_evidence_items=goal.budget.min_evidence_items,
        judge_threshold=goal.budget.judge_threshold,
        repair_iterations=goal.budget.repair_iterations,
        guardrails=goal.guardrails,
        source="heuristic",
        expected_primary_role=_infer_primary_role_hint(request.message),
    )


def flag_untargeted_worker_queries(plan: ResearchPlan, request: TurnRequest) -> None:
    """Query-hygiene self-check: log if a worker's query is just the user's
    raw message verbatim, with no subject/dimension targeting applied.

    Not a hardcoded proper-noun/collision blocklist -- a generic signal that
    a worker never got properly targeted, which is the anti-pattern that
    produced the original "the day after" / movie-title collision bug (a
    literal, untargeted echo of the user's message into a search query).
    Called once on the final plan from the LangGraph "plan" node
    (langgraph_runtime/nodes.py) so it covers both plan_research() (LLM-direct)
    and plan_from_contract() (contract-driven) output, since only the plan
    node is a choke point both paths pass through -- _normalize_plan() is not
    called by plan_from_contract().
    """
    raw_message = " ".join((request.message or "").strip().lower().split())
    if not raw_message:
        return
    for worker in plan.workers:
        query_normalized = " ".join((worker.query or "").strip().lower().split())
        if query_normalized and query_normalized == raw_message:
            logger.debug(
                "research_plan_untargeted_worker_query",
                extra={"worker_id": worker.worker_id, "question": worker.question, "query": worker.query},
            )


def _normalize_plan(plan: ResearchPlan, request: TurnRequest, goal: ResearchGoal | None = None) -> ResearchPlan:
    goal = goal or create_research_goal(request)
    if plan.research_profile == "general":
        plan.research_profile = infer_research_profile(request.message)
    plan.secondary_profiles = [
        item for item in _dedupe([*plan.secondary_profiles, *_secondary_profiles_for(request.message, plan.research_profile)])
        if item != plan.research_profile
    ][:2]
    plan.source_lanes = plan.source_lanes or PROFILE_POLICIES[plan.research_profile].source_lanes
    if not plan.questions:
        plan.questions = [request.message]
    plan.questions = _dedupe(plan.questions)[:4]
    if not plan.workers:
        queries = _dedupe(plan.search_queries or plan.questions)[: goal.budget.max_search_workers]
        plan.workers = [SearchWorkerPlan(question=plan.questions[idx] if idx < len(plan.questions) else query, query=query, rationale="LLM-selected focused research worker.", max_results=goal.budget.max_results_per_worker) for idx, query in enumerate(queries)]
    else:
        plan.workers = [worker for worker in plan.workers if worker.query][: goal.budget.max_search_workers]
    if not plan.workers:
        plan.workers = _fallback_plan(request, goal).workers
    plan.search_queries = [worker.query for worker in plan.workers]
    plan.questions = _dedupe([worker.question for worker in plan.workers] or plan.questions)[:4]
    minimum_sources = 1 if request.research_level == "easy" else 2
    plan.max_sources = max(minimum_sources, min(goal.budget.max_sources, int(plan.max_sources or goal.budget.max_sources)))
    plan.min_evidence_items = max(1, min(plan.max_sources, int(plan.min_evidence_items or goal.budget.min_evidence_items)))
    plan.judge_threshold = max(0.45, min(0.9, float(plan.judge_threshold or goal.budget.judge_threshold)))
    plan.repair_iterations = max(0, min(goal.budget.repair_iterations, int(plan.repair_iterations or 0)))
    plan.guardrails = plan.guardrails or goal.guardrails
    plan.goal_id = plan.goal_id or goal.id
    # Phase 4 — preserve an LLM-assigned role if present; fall back to heuristic.
    if not plan.expected_primary_role:
        plan.expected_primary_role = _infer_primary_role_hint(request.message)
    for worker in plan.workers:
        worker.max_results = max(1, min(goal.budget.max_results_per_worker, int(worker.max_results or goal.budget.max_results_per_worker)))
    return plan


__all__ = [
    "CITATION_VERIFICATION_PROMPT",
    "REFLECTION_PROMPT",
    "_cell_terms",
    "_citation_repair_instruction",
    "_compact_search_subject",
    "_compose_deep_worker_wave",
    "_dedupe_workers",
    "_domain_discovery_workers",
    "_domain_for_query",
    "_evidence_supports_cell",
    "_fallback_plan",
    "_implementation_plan_anchor_queries",
    "_llm_vendor_comparison_subject",
    "_longform_timeout_s",
    "_market_landscape_anchor_queries",
    "_max_attempts_per_cell",
    "_max_iterations_for",
    "_meaningful_tokens",
    "_normalize_plan",
    "_plan_preview_investigation_items",
    "_plan_preview_source_strategy",
    "_plan_preview_title",
    "_policy_regulatory_anchor_queries",
    "_profile_from_contract",
    "_public_technical_subject",
    "_strategy_brief_anchor_queries",
    "_strip_meta_instruction_terms",
    "_subject_phrase_is_useful",
    "_targeted_query",
    "_tech_arch_anchor_queries",
    "_tech_arch_grounding_term",
    "_text_supports_cell",
    "_vendor_comparison_anchor_queries",
    "build_research_plan_preview",
    "flag_untargeted_worker_queries",
    "judge_research_final",
    "plan_from_brief_contract",
    "plan_from_contract",
    "plan_from_targeted_queries",
    "plan_research",
    "reflect",
    "update_contract_from_evidence",
    "verify_citations_semantically",
]
