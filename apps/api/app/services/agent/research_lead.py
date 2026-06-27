"""research_lead.py — Lead orchestration, worker execution, and the main research loop.

Responsibilities:
  - _chunk_urls, _max_parallel_read_batches_for, _read_cap_for_batch: batching helpers
  - _assigned_cell_for_worker, _retry_query_for_worker: worker→contract mapping
  - _source_relevance_for_worker, _worker_confidence, _worker_missing_evidence: worker scoring
  - _worker_claim_pack, _worker_report_from_sources, _worker_report_message: worker reporting
  - verify_claims, _specificity_rewrite_issues: citation verification
  - LeadResearchAgent: full multi-agent orchestration class
  - lead_research_loop: entry point for the research pipeline
  - _ensure_source_provenance, _apply_source_provenance: URL provenance helpers

Extracted from research_subtree.py (TD-01).
"""
from __future__ import annotations

import logging
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Literal

from app.services.agent import model_client
from app.services.agent.models import Source, TurnRequest, ToolCall
from app.services.agent.research_models import (
    ClaimVerification,
    CoverageCell,
    CoverageContract,
    EvidenceClaim,
    EvidencePack,
    ReflectionDecision,
    ResearchBudgetLedger,
    ResearchFeedbackLoop,
    ResearchJudgeResult,
    ResearchPlan,
    ResearchStateStore,
    SearchWorkerPlan,
    SearchWorkerReport,
    PROFILE_POLICIES,
)
from app.services.agent.research_planner import (
    _cell_terms,
    _extract_named_framework_subjects,
    _max_iterations_for,
    _targeted_query,
    _tech_arch_grounding_term,
    _text_supports_cell,
    judge_research_final,
    plan_from_contract,
    plan_from_targeted_queries,
    reflect,
    update_contract_from_evidence,
    verify_citations_semantically,
)
from app.services.agent.research_profiles import (
    _request_for_research_objective,
    create_research_goal,
    generate_research_brief,
    get_research_registry,
    research_budget_for,
)
from app.services.agent.research_contracts import generate_coverage_contract
from app.services.agent.research_evidence import bind_evidence
from app.services.agent.research_synthesis import (
    _select_diverse_ranked_sources,
    _source_inventory_summary,
    extract_deep_link_candidates,
    is_public_source_url,
    rank_sources,
    repair_research_answer,
    synthesize_answer,
)
from app.services.agent.research_utils import (
    _estimate_relevance,
    _looks_like_substantive_claim,
    classify_source_type,
    score_source_authority,
    score_technical_density,
)

logger = logging.getLogger(__name__)

MAX_PARALLEL_READ_BATCHES = 4
MAX_PARALLEL_READ_BATCHES_DEEP = 6
MAX_URLS_PER_READ_BATCH = 6

FRAMEWORK_CANONICAL_DOCS: dict[str, list[tuple[str, str]]] = {
    "LangGraph": [
        ("LangGraph overview", "https://langchain-ai.github.io/langgraph/"),
        ("LangGraph concepts", "https://langchain-ai.github.io/langgraph/concepts/"),
        ("LangGraph agents", "https://langchain-ai.github.io/langgraph/agents/agents/"),
    ],
    "CrewAI": [
        ("CrewAI introduction", "https://docs.crewai.com/introduction"),
        ("CrewAI crews", "https://docs.crewai.com/concepts/crews"),
        ("CrewAI flows", "https://docs.crewai.com/concepts/flows"),
    ],
    "AutoGen": [
        ("AutoGen documentation", "https://microsoft.github.io/autogen/stable/"),
        ("AutoGen AgentChat", "https://microsoft.github.io/autogen/stable/user-guide/agentchat-user-guide/index.html"),
        ("Microsoft Agent Framework", "https://learn.microsoft.com/en-us/agent-framework/"),
    ],
    "Haystack": [
        ("Haystack documentation", "https://docs.haystack.deepset.ai/docs/intro"),
        ("Haystack pipelines", "https://docs.haystack.deepset.ai/docs/pipelines"),
        ("Haystack agents", "https://docs.haystack.deepset.ai/docs/agents"),
    ],
    "LlamaIndex Workflows": [
        ("LlamaIndex Workflows", "https://docs.llamaindex.ai/en/stable/module_guides/workflow/"),
        ("LlamaIndex agents", "https://docs.llamaindex.ai/en/stable/module_guides/deploying/agents/"),
        ("LlamaIndex multi-agent workflows", "https://docs.llamaindex.ai/en/stable/understanding/agent/multi_agent/"),
    ],
}

def _chunk_urls(urls: list[str], *, size: int) -> list[list[str]]:
    chunks: list[list[str]] = []
    for index in range(0, len(urls), max(1, size)):
        chunk = [url for url in urls[index : index + size] if url]
        if chunk:
            chunks.append(chunk)
    return chunks


def _max_parallel_read_batches_for(research_level: str) -> int:
    return MAX_PARALLEL_READ_BATCHES_DEEP if research_level == "deep" else MAX_PARALLEL_READ_BATCHES


def _read_cap_for_batch(urls: list[str], plan: ResearchPlan | None) -> int:
    if plan and plan.research_profile == "technical_architecture":
        if any(classify_source_type(url) in {"academic", "pdf"} for url in urls):
            return 14000
        if any(classify_source_type(url) in {"repository", "documentation"} for url in urls):
            return 10000
        return 6500
    if any(classify_source_type(url) in {"academic", "pdf", "documentation"} for url in urls):
        return 7000
    return 3500


def _canonical_framework_sources(request: TurnRequest, plan: ResearchPlan) -> list[Source]:
    if plan.research_profile != "technical_architecture":
        return []
    frameworks = _extract_named_framework_subjects(request.message)
    if len(frameworks) < 3:
        return []
    sources: list[Source] = []
    for framework in frameworks:
        for title, url in FRAMEWORK_CANONICAL_DOCS.get(framework, [])[:2]:
            sources.append(
                Source(
                    title=title,
                    url=url,
                    snippet=(
                        f"Canonical official documentation source for {framework}; read this page for "
                        "architecture, coordination, production, lifecycle, and limitations evidence."
                    ),
                    query=f"{framework} official documentation",
                    provider="canonical_docs",
                )
            )
    return sources


def _assigned_cell_for_worker(worker: SearchWorkerPlan, contract: CoverageContract) -> CoverageCell | None:
    if not contract.cells:
        return None
    haystack = f"{worker.question} {worker.query} {worker.rationale}".lower()
    scored: list[tuple[int, CoverageCell]] = []
    for cell in contract.cells:
        terms = _cell_terms(cell.subject) + _cell_terms(cell.dimension)
        hits = sum(1 for term in terms if term in haystack)
        if hits:
            scored.append((hits, cell))
    if not scored:
        return None
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return scored[0][1]


def _retry_query_for_worker(worker: SearchWorkerPlan, assigned_cell: CoverageCell | None, request: TurnRequest) -> str:
    if assigned_cell:
        return _targeted_query(assigned_cell.subject, [assigned_cell.dimension], request.message)
    grounding = _tech_arch_grounding_term(worker.question) if "architecture" in request.message.lower() else ""
    return " ".join(part for part in [worker.question, grounding or "primary sources implementation evidence"] if part).strip()


def _source_relevance_for_worker(source: Source, worker: SearchWorkerPlan) -> float:
    question_score = _estimate_relevance(source, [worker.question])
    query_score = _estimate_relevance(source, [worker.query])
    density_bonus = min(0.18, score_technical_density(source) * 0.18)
    return max(question_score, query_score) + density_bonus


def _worker_confidence(
    worker: SearchWorkerPlan,
    sources: list[Source],
    claims: list[EvidenceClaim],
    assigned_cell: CoverageCell | None,
) -> float:
    if not sources:
        return 0.0
    relevance = sum(_source_relevance_for_worker(source, worker) for source in sources) / max(1, len(sources))
    claim_bonus = min(0.28, len(claims) * 0.045)
    authority = max((score_source_authority(source.url) for source in sources if source.url), default=0.0) * 0.14
    assignment_bonus = 0.08 if assigned_cell else 0.0
    return max(0.0, min(1.0, relevance * 0.55 + claim_bonus + authority + assignment_bonus))


def _worker_missing_evidence(
    worker: SearchWorkerPlan,
    sources: list[Source],
    claims: list[EvidenceClaim],
    confidence: float,
    assigned_cell: CoverageCell | None,
) -> list[str]:
    missing: list[str] = []
    if not sources:
        missing.append("No usable public sources found.")
    if confidence < 0.45:
        missing.append("Search results appear weak for the assigned question.")
    if not claims:
        missing.append("No typed evidence claims extracted from selected sources.")
    if assigned_cell and not any(
        _text_supports_cell(f"{claim.text} {claim.source_title} {claim.source_url}", assigned_cell)
        for claim in claims
    ):
        missing.append(f"No claim clearly supports {assigned_cell.subject}/{assigned_cell.dimension}.")
    return missing


def _worker_claim_pack(worker: SearchWorkerPlan, assigned_cell: CoverageCell | None, sources: list[Source], plan: ResearchPlan) -> EvidencePack:
    contract = None
    if assigned_cell:
        contract = CoverageContract(cells=[assigned_cell], subjects=[assigned_cell.subject], dimensions=[assigned_cell.dimension])
    worker_max_sources = 8 if plan.research_profile == "technical_architecture" else 6
    worker_max_items = 8 if plan.research_profile == "technical_architecture" else 6
    worker_plan = ResearchPlan(
        research_profile=plan.research_profile,
        questions=[worker.question],
        search_queries=[worker.query],
        workers=[worker],
        max_sources=min(worker_max_sources, plan.max_sources),
        min_evidence_items=1,
    )
    return bind_evidence(
        sources,
        plan=worker_plan,
        contract=contract,
        max_items=min(worker_max_items, max(1, len(sources) * 2)),
    )


def _worker_report_from_sources(
    worker: SearchWorkerPlan,
    *,
    assigned_cell: CoverageCell | None,
    sources: list[Source],
    plan: ResearchPlan,
    provider_attempts: list[dict[str, Any]],
    retry_queries: list[str] | None = None,
) -> SearchWorkerReport:
    pack = _worker_claim_pack(worker, assigned_cell, sources, plan)
    confidence = _worker_confidence(worker, sources, pack.claims, assigned_cell)
    return SearchWorkerReport(
        worker_id=worker.worker_id,
        question=worker.question,
        query=worker.query,
        assigned_subject=assigned_cell.subject if assigned_cell else "",
        assigned_dimension=assigned_cell.dimension if assigned_cell else "",
        sources=sources,
        claims=pack.claims,
        self_assessed_confidence=confidence,
        missing_evidence=_worker_missing_evidence(worker, sources, pack.claims, confidence, assigned_cell),
        retry_queries=retry_queries or [],
        provider_attempts=provider_attempts,
    )


def _worker_report_message(index: int, report: SearchWorkerReport) -> str:
    if report.self_assessed_confidence >= 0.70:
        strength = "strong"
    elif report.self_assessed_confidence >= 0.45:
        strength = "usable"
    else:
        strength = "weak"
    target = ""
    if report.assigned_subject or report.assigned_dimension:
        target = f" for {report.assigned_subject}/{report.assigned_dimension}".strip()
    retry_note = " after a retry" if report.retry_queries else ""
    return (
        f"Search worker {index} found {strength} evidence{target}{retry_note}: "
        f"{len(report.claims)} claim(s), {len(report.sources)} source(s)."
    )


def verify_claims(answer: str, evidence: EvidencePack) -> ClaimVerification:
    sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", answer or "") if len(part.strip()) > 30]
    checked = min(12, len(sentences))
    unsupported: list[str] = []
    cited = 0
    valid_source_ids = {item.source_id for item in evidence.items}
    for sentence in sentences[:checked]:
        markers = set(re.findall(r"\[(S\d+)\]", sentence))
        if markers & valid_source_ids:
            cited += 1
        elif _looks_like_substantive_claim(sentence):
            unsupported.append(sentence[:180])
    status: Literal["pass", "repair"] = "pass" if not unsupported else "repair"
    notes = [] if not unsupported else ["Some substantive claims lack [S#] citations."]
    return ClaimVerification(
        status=status,
        checked_claims=checked,
        cited_claims=cited,
        unsupported_claims=unsupported,
        notes=notes,
    )


def _specificity_rewrite_issues(answer: str) -> list[str]:
    text = answer or ""
    issues: list[str] = []
    hedged_patterns = [
        r"\b(?:many|some|several|various)\s+(?:systems|sources|providers|teams|organizations)\b",
        r"\b(?:may|might|could|can)\s+(?:help|support|enable|improve|reduce)\b",
        r"\b(?:it is important|it is crucial|it should be noted)\b",
        r"\b(?:generally|typically|often|commonly)\b",
    ]
    for pattern in hedged_patterns:
        if re.search(pattern, text, flags=re.IGNORECASE):
            issues.append(f"Hedged language matched: {pattern}")
    uncited_substantive = 0
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        if _looks_like_substantive_claim(sentence) and "[S" not in sentence:
            uncited_substantive += 1
    if uncited_substantive >= 3:
        issues.append(f"{uncited_substantive} substantive sentence(s) lack direct source citations.")
    return issues[:10]


class LeadResearchAgent:
    """Lead-agent controller for deep research.

    Workers collect and read sources, but this lead owns the research state,
    coverage contract, budget ledger, reflection loop, and publish decision.
    """

    def __init__(
        self,
        request: TurnRequest,
        tools: Any,
        progress: Callable[[str, str, dict[str, Any]], None] | None = None,
    ):
        self.request = request
        self.tools = tools
        self.progress = progress or (lambda _stage, _message, _data: None)
        self.budget = research_budget_for(request)
        self.ledger = ResearchBudgetLedger(budget=self.budget)

    def run(self) -> dict[str, Any]:
        registry = get_research_registry()
        self._progress(
            "research_registry",
            "Research team is ready.",
            {"registry": registry.public_summary(), "agent_count": len(registry.agents), "mode": "lead_loop"},
        )
        self._progress(
            "research_brief",
            "Scoping the research objective.",
            {
                "agent_id": "research_lead",
                **model_client.telemetry_for_role("research_brief", quality_mode=self.request.quality_mode, overrides=self.request.model_overrides),
            },
        )
        brief = generate_research_brief(self.request)
        self.ledger.record_model_call(cost_usd=brief.cost_usd, latency_ms=brief.latency_ms)
        self._progress(
            "research_brief_result",
            f"Research brief used {brief.model_used or 'the configured brief model'}.",
            {
                "agent_id": "research_lead",
                **model_client.telemetry_for_role(
                    "research_brief",
                    quality_mode=self.request.quality_mode,
                    model_used=brief.model_used,
                    overrides=self.request.model_overrides,
                ),
                "source": brief.source,
                "research_profile": brief.research_profile,
                "secondary_profiles": brief.secondary_profiles,
                "profile_confidence": brief.profile_confidence,
                "classification_reason": brief.classification_reason,
                "source_lanes": PROFILE_POLICIES[brief.research_profile].source_lanes,
                "domain_strategy_hints": brief.domain_strategy_hints,
                "latency_ms": brief.latency_ms,
                "cost_usd": brief.cost_usd,
            },
        )

        self._progress(
            "coverage_contract",
            "Building the evidence coverage matrix.",
            {
                "agent_id": "research_lead",
                **model_client.telemetry_for_role("coverage_contract", quality_mode=self.request.quality_mode, overrides=self.request.model_overrides),
            },
        )
        contract = generate_coverage_contract(self.request, brief)
        self.ledger.record_model_call(cost_usd=contract.cost_usd, latency_ms=contract.latency_ms)
        self._progress(
            "coverage_contract_result",
            f"Coverage contract used {contract.model_used or 'the configured contract model'}.",
            {
                "agent_id": "research_lead",
                **model_client.telemetry_for_role(
                    "coverage_contract",
                    quality_mode=self.request.quality_mode,
                    model_used=contract.model_used,
                    overrides=self.request.model_overrides,
                ),
                "source": contract.source,
                "latency_ms": contract.latency_ms,
                "cost_usd": contract.cost_usd,
            },
        )

        planning_request = _request_for_research_objective(self.request, brief)
        plan = plan_from_contract(planning_request, contract, self.budget)
        goal = create_research_goal(planning_request)
        state = ResearchStateStore(brief=brief, contract=contract, plan=plan, budget_ledger=self.ledger)
        self._progress(
            "research_goal",
            "Lead research goal and safety limits are set.",
            {
                "goal": goal.model_dump(mode="json"),
                "brief": brief.model_dump(mode="json"),
                "contract": contract.model_dump(mode="json"),
                "budget_ledger": self.ledger.model_dump(mode="json"),
            },
        )

        for iteration in range(1, _max_iterations_for(self.request) + 1):
            if self.ledger.stopped:
                break
            state.iteration = iteration
            self._dispatch_worker_wave(state)
            state.evidence = bind_evidence(
                state.all_sources,
                plan=state.plan,
                max_items=self.budget.max_sources + self.budget.max_deep_links,
                contract=state.contract,
            )
            update_contract_from_evidence(state)
            self._progress(
                "coverage_check",
                f"Coverage is {state.contract.coverage_ratio():.0%}; {len(state.contract.open_cells())} cell(s) remain open.",
                {
                    "coverage_ratio": state.contract.coverage_ratio(),
                    "open_cells": [cell.model_dump(mode="json") for cell in state.contract.open_cells()[:10]],
                    "partial_cells": [cell.model_dump(mode="json") for cell in state.contract.partial_cells()[:8]],
                    "worker_reports": [report.model_dump(mode="json") for report in state.worker_reports[-8:]],
                    "iteration": iteration,
                    "budget_ledger": self.ledger.model_dump(mode="json"),
                },
            )
            decision = reflect(self.request, state)
            if decision.model_used:
                self.ledger.record_model_call(cost_usd=decision.cost_usd, latency_ms=decision.latency_ms)
            self._progress(
                "lead_reflection",
                self._reflection_message(decision),
                {
                    "decision": decision.model_dump(mode="json"),
                    "targeted_queries": decision.targeted_queries,
                    **model_client.telemetry_for_role(
                        "reflection",
                        quality_mode=self.request.quality_mode,
                        model_used=decision.model_used,
                        overrides=self.request.model_overrides,
                    ),
                    "budget_ledger": self.ledger.model_dump(mode="json"),
                },
            )
            if decision.next_action != "continue" or decision.sufficient or not decision.targeted_queries:
                break
            self._mark_attempts_for_open_cells(state)
            state.plan = plan_from_targeted_queries(decision.targeted_queries, state)

        response = self._synthesize_verify_and_judge(state)
        feedback = ResearchFeedbackLoop(
            judge=ResearchJudgeResult(
                status="pass" if response["verdict"].can_publish else "repair",
                score=response["verdict"].score,
                issues=response["verdict"].issues,
                repair_instruction=response["verdict"].repair_instruction,
                can_publish=response["verdict"].can_publish,
            ),
            repaired=response["repaired"],
            repair_attempts=response["repair_attempts"],
            final_score=response["verdict"].score,
        )
        self._progress(
            "research_budget",
            "Lead research budget ledger closed.",
            {
                "stop_reason": self.ledger.stop_reason,
                "coverage_ratio": state.contract.coverage_ratio(),
                "open_cells": len(state.contract.open_cells()),
                "evidence_items": len(state.evidence.items),
                "worker_reports": len(state.worker_reports),
                "iterations": state.iteration,
                "budget_ledger": self.ledger.model_dump(mode="json"),
            },
        )
        return {
            "sources": state.all_sources,
            "tool_calls": state.all_tool_calls,
            "evidence": state.evidence,
            "response": response["model_response"],
            "plan": state.plan,
            "worker_reports": state.worker_reports,
            "feedback": feedback,
        }

    def _dispatch_worker_wave(self, state: ResearchStateStore) -> None:
        self._progress(
            "lead_research_dispatch",
            f"Dispatching worker wave {state.iteration} with {len(state.plan.workers)} worker(s).",
            {
                "iteration": state.iteration,
                "workers": [worker.model_dump(mode="json") for worker in state.plan.workers],
                "agent_id": "research_lead",
            },
        )
        wave_sources: list[Source] = []
        worker_sources: dict[str, list[Source]] = {}
        provider_attempts_by_worker: dict[str, list[dict[str, Any]]] = {}
        retry_queries_by_worker: dict[str, list[str]] = {}
        pending_workers: list[tuple[int, SearchWorkerPlan]] = []
        for index, worker in enumerate(state.plan.workers, start=1):
            if worker.query in state.query_history:
                continue
            pending_workers.append((index, worker))

        search_slots = min(
            max(1, state.budget_ledger.budget.max_search_workers),
            len(pending_workers),
            self.ledger.remaining_tool_calls(),
        )
        if search_slots <= 0 or not self.ledger.can_start_tool("web_search"):
            pending_workers = []
        else:
            pending_workers = pending_workers[:search_slots]

        for index, worker in pending_workers:
            state.add_queries([worker.query])
            self._progress(
                "search_worker",
                f"Search worker {index} is looking for evidence.",
                {
                    "agent_id": worker.agent_id,
                    "worker_id": worker.worker_id,
                    "worker_index": index,
                    "query": worker.query,
                    "question": worker.question,
                    "rationale": worker.rationale,
                },
            )
        if pending_workers:
            max_workers = max(1, min(state.budget_ledger.budget.max_search_workers, len(pending_workers)))
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {
                    executor.submit(self.tools.search_web, worker.query, max_results=worker.max_results): (index, worker)
                    for index, worker in pending_workers
                }
                for future in as_completed(futures):
                    index, worker = futures[future]
                    try:
                        sources, call = future.result()
                    except Exception as exc:
                        logger.warning("agent search worker failed for query=%r: %s", worker.query, exc)
                        sources = []
                        call = ToolCall(
                            name="web_search",
                            input={"query": worker.query, "max_results": worker.max_results},
                            output={},
                            ok=False,
                            error=str(exc),
                        )
                    self.ledger.record_tool_call(latency_ms=call.latency_ms, sources_seen=len(sources))
                    state.all_tool_calls.append(call)
                    provider = call.output.get("provider") if isinstance(call.output, dict) else ""
                    _ensure_source_provenance(sources, query=worker.query, provider=str(provider or ""))
                    public_sources = [source for source in sources if is_public_source_url(source.url)]
                    provider_attempts_by_worker.setdefault(worker.worker_id, []).append(
                        {
                            "query": worker.query,
                            "provider": provider,
                            "ok": call.ok,
                            "error": call.error,
                            "source_count": len(sources),
                            "public_source_count": len(public_sources),
                        }
                    )
                    assigned_cell = _assigned_cell_for_worker(worker, state.contract)
                    avg_relevance = (
                        sum(_source_relevance_for_worker(source, worker) for source in public_sources) / max(1, len(public_sources))
                    )
                    if (
                        self.request.research_level != "easy"
                        and avg_relevance < 0.35
                        and self.ledger.can_start_tool("web_search")
                    ):
                        retry_query = _retry_query_for_worker(
                            worker,
                            assigned_cell,
                            _request_for_research_objective(self.request, state.brief),
                        )
                        if retry_query and retry_query not in state.query_history:
                            state.add_queries([retry_query])
                            retry_queries_by_worker.setdefault(worker.worker_id, []).append(retry_query)
                            self._progress(
                                "search_worker_retry",
                                f"Search worker {index} is refining a weak result set.",
                                {
                                    "worker_index": index,
                                    "worker_id": worker.worker_id,
                                    "retry_query": retry_query,
                                    "initial_relevance": avg_relevance,
                                },
                            )
                            try:
                                retry_sources, retry_call = self.tools.search_web(retry_query, max_results=worker.max_results)
                            except Exception as exc:
                                logger.warning("agent retry search failed for query=%r: %s", retry_query, exc)
                                retry_sources = []
                                retry_call = ToolCall(
                                    name="web_search",
                                    input={"query": retry_query, "max_results": worker.max_results},
                                    output={},
                                    ok=False,
                                    error=str(exc),
                                )
                            self.ledger.record_tool_call(latency_ms=retry_call.latency_ms, sources_seen=len(retry_sources))
                            state.all_tool_calls.append(retry_call)
                            retry_provider = retry_call.output.get("provider") if isinstance(retry_call.output, dict) else ""
                            _ensure_source_provenance(retry_sources, query=retry_query, provider=str(retry_provider or provider or ""))
                            retry_public = [source for source in retry_sources if is_public_source_url(source.url)]
                            provider_attempts_by_worker.setdefault(worker.worker_id, []).append(
                                {
                                    "query": retry_query,
                                    "provider": retry_provider,
                                    "ok": retry_call.ok,
                                    "error": retry_call.error,
                                    "source_count": len(retry_sources),
                                    "public_source_count": len(retry_public),
                                }
                            )
                            public_sources.extend(retry_public)
                    added = state.add_sources(public_sources)
                    worker_sources.setdefault(worker.worker_id, []).extend(added)
                    wave_sources.extend(added)
                    self._progress(
                        "search_worker_provider",
                        f"Search worker {index} used {provider or 'the configured search provider chain'}.",
                        {
                            "worker_index": index,
                            "provider": provider,
                            "ok": call.ok,
                            "error": call.error,
                            "source_count": len(sources),
                            "public_source_count": len(public_sources),
                            "self_assessed_relevance": avg_relevance,
                            "retry_queries": retry_queries_by_worker.get(worker.worker_id, []),
                            "parallel_workers": len(pending_workers),
                            "budget_ledger": self.ledger.model_dump(mode="json"),
                        },
                    )

        canonical_sources = state.add_sources(_canonical_framework_sources(self.request, state.plan))
        if canonical_sources:
            wave_sources.extend(canonical_sources)
            self._progress(
                "canonical_source_seed",
                f"Seeded {len(canonical_sources)} official documentation source(s).",
                {
                    "source_count": len(canonical_sources),
                    "urls": [source.url for source in canonical_sources if source.url],
                },
            )

        ranked = rank_sources(wave_sources, state.plan)
        selected = _select_diverse_ranked_sources(
            ranked,
            limit=self.ledger.remaining_source_reads(),
            research_level=self.request.research_level,
        )
        inventory = _source_inventory_summary(state.all_sources)
        self._progress(
            "source_inventory",
            f"Built a candidate inventory with {inventory['total']} source candidate(s).",
            {
                "inventory": inventory,
                "candidate_count": inventory["total"],
                "read_budget_remaining": self.ledger.remaining_source_reads(),
                "tool_budget_remaining": self.ledger.remaining_tool_calls(),
            },
        )
        self._progress(
            "source_ranker",
            f"Ranked {len(wave_sources)} candidate source(s).",
            {
                "agent_id": "source_ranker",
                "ranked_sources": [item.model_dump(mode="json") for item in ranked[: state.plan.max_sources]],
                "selected_source_provenance": [
                    {
                        "title": source.title,
                        "url": source.url,
                        "query": source.query,
                        "provider": source.provider,
                    }
                    for source in selected
                ],
            },
        )
        if selected and self.ledger.can_start_tool("read_url") and self.ledger.can_read_more_sources():
            read_urls = [source.url for source in selected if source.url][: self.ledger.remaining_source_reads()]
            provenance_by_url = {
                source.url: {"query": source.query, "provider": source.provider}
                for source in selected
                if source.url
            }
            extracted_by_url: dict[str, Source] = {}
            read_batches = _chunk_urls(read_urls, size=MAX_URLS_PER_READ_BATCH)
            max_read_batches = _max_parallel_read_batches_for(self.request.research_level)
            read_batches = read_batches[: min(max_read_batches, self.ledger.remaining_tool_calls())]
            self._progress(
                "source_reader",
                f"Reading {sum(len(batch) for batch in read_batches)} selected source page(s).",
                {
                    "urls": read_urls,
                    "batch_count": len(read_batches),
                    "max_parallel_read_batches": max_read_batches,
                    "max_urls_per_read_batch": MAX_URLS_PER_READ_BATCH,
                    "read_ceiling": max_read_batches * MAX_URLS_PER_READ_BATCH,
                },
            )
            if read_batches:
                max_read_workers = max(1, min(max_read_batches, len(read_batches)))
                with ThreadPoolExecutor(max_workers=max_read_workers) as executor:
                    futures = {
                        executor.submit(self.tools.extract_urls, batch, max_chars_per_source=_read_cap_for_batch(batch, state.plan)): batch
                        for batch in read_batches
                    }
                    for future in as_completed(futures):
                        batch = futures[future]
                        try:
                            extracted, call = future.result()
                        except Exception as exc:
                            logger.warning("agent source reader failed for %d url(s): %s", len(batch), exc)
                            extracted = []
                            call = ToolCall(
                                name="read_url",
                                input={"urls": batch},
                                output={},
                                ok=False,
                                error=str(exc),
                            )
                        _apply_source_provenance(extracted, provenance_by_url)
                        self.ledger.record_tool_call(latency_ms=call.latency_ms, sources_read=len(batch))
                        state.all_tool_calls.append(call)
                        state.add_sources(extracted)
                        for source in extracted:
                            if source.url:
                                extracted_by_url[source.url] = source
                        provider = call.output.get("provider") if isinstance(call.output, dict) else None
                        self._progress(
                            "source_reader_result",
                            "Source reader finished extracting source text.",
                            {
                                "ok": call.ok,
                                "error": call.error,
                                "provider": provider,
                                "source_count": len(extracted),
                                "batch_size": len(batch),
                                "budget_ledger": self.ledger.model_dump(mode="json"),
                            },
                        )
            for index, worker in pending_workers:
                assigned_cell = _assigned_cell_for_worker(worker, state.contract)
                report_sources = [
                    extracted_by_url.get(source.url, source)
                    for source in worker_sources.get(worker.worker_id, [])
                    if source.url
                ]
                report = _worker_report_from_sources(
                    worker,
                    assigned_cell=assigned_cell,
                    sources=report_sources,
                    plan=state.plan,
                    provider_attempts=provider_attempts_by_worker.get(worker.worker_id, []),
                    retry_queries=retry_queries_by_worker.get(worker.worker_id, []),
                )
                state.worker_reports.append(report)
                self._progress(
                    "search_worker_report",
                    _worker_report_message(index, report),
                    {
                        "worker_index": index,
                        "report": report.model_dump(mode="json"),
                    },
                )
        self._expand_source_graph(state, selected)

    def _follow_deep_links(self, state: ResearchStateStore, sources: list[Source]) -> None:
        if self.budget.max_deep_links <= 0 or not self.ledger.can_read_more_sources():
            return
        candidates = extract_deep_link_candidates(sources, max_links=self.budget.max_deep_links)
        urls = [
            candidate.url
            for candidate in candidates
            if candidate.url not in state.source_inventory and is_public_source_url(candidate.url)
        ][: self.ledger.remaining_source_reads()]
        self._progress(
            "deep_link_agent",
            f"Found {len(urls)} useful deep link(s) to inspect.",
            {"links": [candidate.model_dump(mode="json") for candidate in candidates[: len(urls)]]},
        )
        if not urls or not self.ledger.can_start_tool("read_url"):
            return
        extracted, call = self.tools.extract_urls(urls, max_chars_per_source=2500)
        _apply_source_provenance(
            extracted,
            {
                candidate.url: {"query": "deep-link follow-up", "provider": "Tavily Extract", "parent_url": candidate.parent_url}
                for candidate in candidates
            },
        )
        self.ledger.record_tool_call(latency_ms=call.latency_ms, sources_read=len(urls))
        state.all_tool_calls.append(call)
        state.add_sources(extracted)

    def _expand_source_graph(self, state: ResearchStateStore, seeds: list[Source]) -> None:
        if self.budget.max_deep_links <= 0 or not self.ledger.can_read_more_sources():
            return
        max_depth = 2 if self.request.research_level == "deep" else 1
        frontier = [source for source in seeds if source.url]
        followed: set[str] = set()
        for depth in range(1, max_depth + 1):
            if not frontier or not self.ledger.can_read_more_sources() or not self.ledger.can_start_tool("read_url"):
                break
            candidate_limit = min(self.ledger.remaining_source_reads(), max(1, self.budget.max_deep_links - len(followed)))
            candidates = extract_deep_link_candidates(frontier, max_links=candidate_limit)
            urls = [
                candidate.url
                for candidate in candidates
                if candidate.url not in followed
                and candidate.url not in state.source_inventory
                and is_public_source_url(candidate.url)
            ][: self.ledger.remaining_source_reads()]
            self._progress(
                "source_graph_expansion",
                f"Following source graph layer {depth}: {len(urls)} reference link(s).",
                {
                    "depth": depth,
                    "candidate_count": len(candidates),
                    "selected_count": len(urls),
                    "links": [candidate.model_dump(mode="json") for candidate in candidates[: len(urls)]],
                    "inventory": _source_inventory_summary(state.all_sources),
                },
            )
            if not urls:
                break
            provenance_by_url = {
                candidate.url: {
                    "query": f"source graph layer {depth}",
                    "provider": "source_graph",
                    "parent_url": candidate.parent_url,
                }
                for candidate in candidates
            }
            try:
                extracted, call = self.tools.extract_urls(urls, max_chars_per_source=_read_cap_for_batch(urls, state.plan))
            except Exception as exc:
                logger.warning("agent source graph expansion failed at depth=%d: %s", depth, exc)
                extracted = []
                call = ToolCall(name="read_url", input={"urls": urls}, output={}, ok=False, error=str(exc))
            _apply_source_provenance(extracted, provenance_by_url)
            self.ledger.record_tool_call(latency_ms=call.latency_ms, sources_read=len(urls))
            state.all_tool_calls.append(call)
            state.add_sources(extracted)
            followed.update(urls)
            frontier = extracted
            self._progress(
                "source_graph_result",
                f"Source graph layer {depth} added {len(extracted)} readable source(s).",
                {
                    "depth": depth,
                    "ok": call.ok,
                    "error": call.error,
                    "source_count": len(extracted),
                    "inventory": _source_inventory_summary(state.all_sources),
                    "budget_ledger": self.ledger.model_dump(mode="json"),
                },
            )

    def _synthesize_verify_and_judge(self, state: ResearchStateStore) -> dict[str, Any]:
        self._progress(
            "evidence_binder",
            f"Bound {len(state.evidence.items)} evidence item(s).",
            {
                "agent_id": "evidence_binder",
                "coverage": state.evidence.coverage,
                "coverage_contract_ratio": state.contract.coverage_ratio(),
                "claim_count": len(state.evidence.claims),
                "gaps": state.evidence.gaps,
                "contradictions": state.evidence.contradictions,
                "evidence_items": [item.model_dump(mode="json") for item in state.evidence.items],
                "claims": [claim.model_dump(mode="json") for claim in state.evidence.claims[:20]],
                "architecture_cards": [card.model_dump(mode="json") for card in state.evidence.architecture_cards[:16]],
            },
        )
        if not self.ledger.can_start_model("synthesis_agent"):
            answer = self._budget_stopped_answer(state)
            return {
                "model_response": model_client.ModelResponse(
                    text=answer,
                    model_used="budget-ledger",
                    latency_ms=0,
                    cost_usd=0.0,
                ),
                "verdict": judge_research_final(self.request, state, answer),
                "repaired": False,
                "repair_attempts": 0,
            }
        self._progress(
            "synthesis",
            "Writing one coherent answer from the evidence.",
            {
                "agent_id": "synthesis_agent",
                **model_client.telemetry_for_role("synthesis", quality_mode=self.request.quality_mode, overrides=self.request.model_overrides),
            },
        )
        model_response = synthesize_answer(self.request, state.plan, state.evidence)
        self.ledger.record_model_call(cost_usd=model_response.cost_usd, latency_ms=model_response.latency_ms)
        self._progress(
            "synthesis_result",
            f"Synthesis used {model_response.model_used or 'the configured synthesis model'}.",
            {
                "agent_id": "synthesis_agent",
                **model_client.telemetry_for_response(model_response, overrides=self.request.model_overrides),
                "latency_ms": model_response.latency_ms,
                "cost_usd": model_response.cost_usd,
                "budget_ledger": self.ledger.model_dump(mode="json"),
            },
        )
        answer = model_response.text

        citation_result = verify_citations_semantically(answer, state.evidence, overrides=self.request.model_overrides)
        if citation_result.model_used:
            self.ledger.record_model_call(cost_usd=citation_result.cost_usd, latency_ms=citation_result.latency_ms)
        self._progress(
            "citation_verification",
            "Verified answer citations against source text.",
            {
                "agent_id": "claim_verifier",
                "verification": citation_result.model_dump(mode="json"),
                **model_client.telemetry_for_role(
                    "citation_verifier",
                    quality_mode="standard",
                    model_used=citation_result.model_used,
                    overrides=self.request.model_overrides,
                ),
            },
        )
        repaired = False
        repair_attempts = 0
        if citation_result.repair_needed and self.ledger.can_start_model("repair_agent"):
            model_response = self._repair_answer(state, answer, citation_result.repair_instruction)
            answer = model_response.text
            repaired = True
            repair_attempts += 1

        specificity_issues = _specificity_rewrite_issues(answer)
        self._progress(
            "fact_check_rewrite",
            "Fact-checking the draft and tightening vague claims.",
            {
                "agent_id": "claim_verifier",
                "issue_count": len(specificity_issues),
                "issues": specificity_issues[:8],
            },
        )
        if specificity_issues and self.ledger.can_start_model("repair_agent"):
            instruction = (
                "Replace vague or hedged claims with named-source specifics using [S#] citations. "
                "If the evidence does not support a specific version, disclose the gap plainly. "
                "Issues to fix: " + "; ".join(specificity_issues[:8])
            )
            model_response = self._repair_answer(state, answer, instruction)
            answer = model_response.text
            repaired = True
            repair_attempts += 1

        verdict = judge_research_final(self.request, state, answer)
        self._progress(
            "research_judge_result",
            f"Research judge recommends {verdict.next_action}.",
            {"agent_id": "research_judge", "verdict": verdict.model_dump(mode="json")},
        )
        if verdict.next_action == "research_more" and not self.ledger.stopped:
            state.plan = plan_from_targeted_queries(
                [_targeted_query(cell.subject, [cell.dimension], state.brief.objective) for cell in state.contract.open_cells()[:4]],
                state,
            )
            self._mark_attempts_for_open_cells(state)
            self._dispatch_worker_wave(state)
            state.evidence = bind_evidence(
                state.all_sources,
                plan=state.plan,
                max_items=self.budget.max_sources + self.budget.max_deep_links,
                contract=state.contract,
            )
            update_contract_from_evidence(state)
            model_response = synthesize_answer(self.request, state.plan, state.evidence)
            self.ledger.record_model_call(cost_usd=model_response.cost_usd, latency_ms=model_response.latency_ms)
            answer = model_response.text
            verdict = judge_research_final(self.request, state, answer)
        if verdict.repair_needed and state.plan.repair_iterations > repair_attempts and self.ledger.can_start_model("repair_agent"):
            model_response = self._repair_answer(state, answer, verdict.repair_instruction)
            repaired = True
            repair_attempts += 1
            verdict = judge_research_final(self.request, state, model_response.text)
        return {
            "model_response": model_response,
            "verdict": verdict,
            "repaired": repaired,
            "repair_attempts": repair_attempts,
        }

    def _repair_answer(self, state: ResearchStateStore, answer: str, instruction: str):
        self._progress(
            "research_repair",
            "Repairing the answer before publishing.",
            {
                "repair_instruction": instruction,
                **model_client.telemetry_for_role("repair", quality_mode=self.request.quality_mode, overrides=self.request.model_overrides),
            },
        )
        fake_judge = ResearchJudgeResult(
            status="repair",
            score=0.6,
            repair_instruction=instruction,
            can_publish=False,
        )
        repaired = repair_research_answer(self.request, state.plan, state.evidence, answer, fake_judge)
        self.ledger.record_model_call(cost_usd=repaired.cost_usd, latency_ms=repaired.latency_ms)
        self._progress(
            "research_repair_model",
            f"Repair used {repaired.model_used or 'the configured repair model'}.",
            {
                **model_client.telemetry_for_response(repaired, overrides=self.request.model_overrides),
                "latency_ms": repaired.latency_ms,
                "cost_usd": repaired.cost_usd,
                "budget_ledger": self.ledger.model_dump(mode="json"),
            },
        )
        return repaired

    def _mark_attempts_for_open_cells(self, state: ResearchStateStore) -> None:
        for cell in state.contract.open_cells():
            cell.attempts += 1

    def _budget_stopped_answer(self, state: ResearchStateStore) -> str:
        open_cells = state.contract.open_cells()
        gap_text = ""
        if open_cells:
            gap_text = "\n\nUnresolved public-evidence gaps:\n" + "\n".join(
                f"- {cell.subject} / {cell.dimension}" for cell in open_cells[:10]
            )
        return (
            "I gathered evidence but stopped before synthesis because the research budget was exhausted"
            f" ({self.ledger.stop_reason or 'budget stopped'}).{gap_text}"
        )

    def _reflection_message(self, decision: ReflectionDecision) -> str:
        if decision.next_action == "publish":
            return "Lead researcher judged the evidence sufficient."
        if decision.next_action == "stop_with_gaps":
            return "Lead researcher stopped with explicit unresolved gaps."
        return "Lead researcher found gaps and prepared targeted follow-up searches."

    def _progress(self, stage: str, message: str, data: dict[str, Any] | None = None) -> None:
        self.progress(stage, message, data or {})


def lead_research_loop(
    request: TurnRequest,
    tools: Any,
    progress: Callable[[str, str, dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    return LeadResearchAgent(request, tools, progress).run()


def _ensure_source_provenance(sources: list[Source], *, query: str, provider: str) -> None:
    for source in sources:
        if not source.query:
            source.query = query
        if provider and not source.provider:
            source.provider = provider


def _apply_source_provenance(sources: list[Source], provenance_by_url: dict[str, dict[str, str]]) -> None:
    for source in sources:
        provenance = provenance_by_url.get(source.url) or {}
        if not source.query:
            source.query = provenance.get("query", "")
        if not source.provider:
            source.provider = provenance.get("provider", "")




__all__ = [
    "LeadResearchAgent",
    "lead_research_loop",
    "verify_claims",
    "_apply_source_provenance",
    "_assigned_cell_for_worker",
    "_chunk_urls",
    "_ensure_source_provenance",
    "_max_parallel_read_batches_for",
    "_read_cap_for_batch",
    "_retry_query_for_worker",
    "_source_relevance_for_worker",
    "_specificity_rewrite_issues",
    "_worker_claim_pack",
    "_worker_confidence",
    "_worker_missing_evidence",
    "_worker_report_from_sources",
    "_worker_report_message",
]
