from __future__ import annotations

"""LangGraph research graph nodes.

CRITICAL STATE UPDATE RULE:
Each node must return ONLY the fields it is explicitly updating — never `{**state, ...}`.

For Annotated[T, operator.add] fields (sources, worker_reports, tool_calls,
provider_attempts, source_graph_expansion_results, claim_classification_results,
cost_usd_spent, tool_calls_made, model_calls_made), the returned value is the DELTA
only — LangGraph adds it to the existing accumulated value via operator.add.

Returning {**state, "sources": new_sources} would cause LangGraph to run:
  existing_sources + (existing_sources + new_sources) → exponential duplication.

For last-write-wins fields (visited_nodes, artifacts, brief, plan, etc.) it is
safe to return explicit values derived from state, but NOT via **state spread.
"""

import logging
from typing import TYPE_CHECKING, Any

from app.services.agent.langgraph_runtime.events import ProgressCallback, emit_graph_event
from app.services.agent.langgraph_runtime.state import (
    BudgetDecision,
    GraphNodeName,
    ResearchGraphState,
)

if TYPE_CHECKING:
    from app.services.agent.models import TurnRequest
    from app.services.agent.tools import Tools

logger = logging.getLogger(__name__)

# NODE_ORDER defines both the canonical visit sequence and the order in which
# sequential edges are built in graph.py.
# NOTE: dispatch_search → search_worker is a Send fan-out, not a linear edge.
#       search_worker → rank is a direct edge added separately in graph.py.
NODE_ORDER: tuple[GraphNodeName, ...] = (
    "brief",
    "subject_derivation",
    "contract",
    "plan",
    "dispatch_search",
    "search_worker",
    "rank",
    "read",
    "classify_claims",
    "expand_source_graph",
    "bind",
    "synthesize",
    "verify",
    "judge",
    "repair",
)

BUDGET_GATE_AFTER: frozenset[str] = frozenset({"bind", "judge"})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _url_priority_merge(sources: list) -> list:
    """Merge sources list by URL, preferring versions with the most content."""
    url_to_source: dict[str, Any] = {}
    for s in sources:
        if not s.url:
            continue
        existing = url_to_source.get(s.url)
        if existing is None or len(s.content or "") > len(existing.content or ""):
            url_to_source[s.url] = s
    no_url = [s for s in sources if not s.url]
    return list(url_to_source.values()) + no_url


# ---------------------------------------------------------------------------
# Slice 1 real nodes
# ---------------------------------------------------------------------------

def brief(
    state: ResearchGraphState,
    *,
    run_id: str,
    request: TurnRequest,
    tools: Tools | None = None,
    progress: ProgressCallback | None = None,
) -> dict:
    from app.services.agent.research_profiles import generate_research_brief

    visited = [*state.get("visited_nodes", []), "brief"]
    try:
        research_brief = generate_research_brief(request)
    except Exception as exc:
        logger.warning("brief node: generate_research_brief failed: %s", exc)
        from app.services.agent.research_models import ResearchBrief
        research_brief = ResearchBrief(
            objective=request.message,
            research_level=getattr(request, "research_level", "regular"),
        )

    cost = getattr(research_brief, "cost_usd", 0.0) or 0.0
    emit_graph_event(
        progress,
        run_id=run_id,
        node_name="brief",
        message=f"Research brief generated (profile={research_brief.research_profile}).",
        research_profile=research_brief.research_profile,
        cost_usd_spent=cost,
        tool_calls_made=0,
        model_calls_made=1,
    )
    return {
        "visited_nodes": visited,
        "artifacts": {**state.get("artifacts", {}), "brief": {"status": "done"}},
        "brief": research_brief,
        "cost_usd_spent": cost,     # delta → added by reducer
        "tool_calls_made": 0,
        "model_calls_made": 1,
    }


def subject_derivation(
    state: ResearchGraphState,
    *,
    run_id: str,
    request: TurnRequest,
    tools: Tools | None = None,
    progress: ProgressCallback | None = None,
) -> dict:
    from app.services.agent.research_contracts import _extract_named_comparison_subjects

    visited = [*state.get("visited_nodes", []), "subject_derivation"]
    research_brief = state.get("brief")
    named_subjects = _extract_named_comparison_subjects(request.message)

    if research_brief and research_brief.scope_in and not named_subjects:
        logger.debug(
            "subject_derivation: no named subjects from message; brief.scope_in=%r (not promoted)",
            (research_brief.scope_in or [])[:6],
        )

    emit_graph_event(
        progress,
        run_id=run_id,
        node_name="subject_derivation",
        message=f"Derived {len(named_subjects)} named subject(s): {named_subjects}.",
        named_subjects=named_subjects,
        cost_usd_spent=0.0,
        tool_calls_made=0,
        model_calls_made=0,
    )
    return {
        "visited_nodes": visited,
        "artifacts": {**state.get("artifacts", {}), "subject_derivation": {"status": "done"}},
        "named_subjects": named_subjects,
        "cost_usd_spent": 0.0,
        "tool_calls_made": 0,
        "model_calls_made": 0,
    }


def contract(
    state: ResearchGraphState,
    *,
    run_id: str,
    request: TurnRequest,
    tools: Tools | None = None,
    progress: ProgressCallback | None = None,
) -> dict:
    from app.services.agent.research_contracts import generate_coverage_contract

    visited = [*state.get("visited_nodes", []), "contract"]
    research_brief = state.get("brief")
    named_subjects = state.get("named_subjects") or []

    if research_brief is None:
        from app.services.agent.research_models import ResearchBrief
        research_brief = ResearchBrief(
            objective=request.message,
            research_level=getattr(request, "research_level", "regular"),
        )

    coverage_contract = generate_coverage_contract(
        request, research_brief, named_subjects=named_subjects or None
    )

    emit_graph_event(
        progress,
        run_id=run_id,
        node_name="contract",
        message=f"Coverage contract built: {len(coverage_contract.subjects)} subject(s), {len(coverage_contract.cells)} cell(s).",
        subjects=coverage_contract.subjects,
        cell_count=len(coverage_contract.cells),
        cost_usd_spent=0.0,
        tool_calls_made=0,
        model_calls_made=0,
    )
    return {
        "visited_nodes": visited,
        "artifacts": {**state.get("artifacts", {}), "contract": {"status": "done"}},
        "contract": coverage_contract,
        "named_subjects": coverage_contract.subjects,
        "cost_usd_spent": 0.0,
        "tool_calls_made": 0,
        "model_calls_made": 0,
    }


def plan(
    state: ResearchGraphState,
    *,
    run_id: str,
    request: TurnRequest,
    tools: Tools | None = None,
    progress: ProgressCallback | None = None,
) -> dict:
    from app.services.agent.research_planner import plan_from_contract

    visited = [*state.get("visited_nodes", []), "plan"]
    coverage_contract = state.get("contract")

    if coverage_contract is None:
        from app.services.agent.research_planner import plan_research
        research_plan = plan_research(request)
    else:
        research_plan = plan_from_contract(request, coverage_contract)

    from app.services.agent.research_planner import flag_untargeted_worker_queries
    flag_untargeted_worker_queries(research_plan, request)

    emit_graph_event(
        progress,
        run_id=run_id,
        node_name="plan",
        message=f"Research plan built: {len(research_plan.workers)} worker(s).",
        worker_count=len(research_plan.workers),
        research_profile=research_plan.research_profile,
        cost_usd_spent=0.0,
        tool_calls_made=0,
        model_calls_made=0,
    )
    return {
        "visited_nodes": visited,
        "artifacts": {**state.get("artifacts", {}), "plan": {"status": "done"}},
        "plan": research_plan,
        "cost_usd_spent": 0.0,
        "tool_calls_made": 0,
        "model_calls_made": 0,
    }


# ---------------------------------------------------------------------------
# Slice 2 real nodes — search fan-out
# ---------------------------------------------------------------------------

def dispatch_search(
    state: ResearchGraphState,
    *,
    run_id: str,
    request: TurnRequest,
    tools: Tools | None = None,
    progress: ProgressCallback | None = None,
) -> dict:
    """Record search fan-out dispatch; emit progress event.

    This is a regular node (returns dict update).
    The actual Send fan-out is handled by dispatch_search_router in graph.py,
    which is wired as add_conditional_edges after this node.
    """
    research_plan = state.get("plan")
    workers = research_plan.workers if research_plan else []

    visited = [*state.get("visited_nodes", []), "dispatch_search"]

    emit_graph_event(
        progress,
        run_id=run_id,
        node_name="dispatch_search",
        message=f"Dispatching {len(workers)} search worker(s).",
        worker_count=len(workers),
        cost_usd_spent=0.0,
        tool_calls_made=0,
        model_calls_made=0,
    )
    return {
        "visited_nodes": visited,
        "artifacts": {**state.get("artifacts", {}), "dispatch_search": {"status": "done"}},
        "cost_usd_spent": 0.0,
        "tool_calls_made": 0,
        "model_calls_made": 0,
    }


def dispatch_search_router(
    state: ResearchGraphState,
    *,
    run_id: str,
    request: TurnRequest,
    tools: Tools | None = None,
    progress: ProgressCallback | None = None,
) -> list:
    """Routing function for add_conditional_edges after dispatch_search.

    Returns either:
    - list[Send]: fan out to search_worker (one per worker in the plan)
    - ["rank"]: shortcircuit directly to rank when there are no workers
    """
    from langgraph.types import Send

    research_plan = state.get("plan")
    workers = research_plan.workers if research_plan else []

    if not workers or tools is None:
        return ["rank"]

    plan_dict = research_plan.model_dump(mode="json")
    return [
        Send(
            "search_worker",
            {
                "worker_index": i,
                "worker_plan": worker.model_dump(mode="json"),
                "plan_dict": plan_dict,
                # Pass through last-write-wins fields needed by search_worker
                "visited_nodes": [*state.get("visited_nodes", []), "search_worker"],
                "artifacts": state.get("artifacts", {}),
                # Accumulated fields: send 0/[] deltas (not the accumulated state)
                "cost_usd_spent": 0.0,
                "tool_calls_made": 0,
                "model_calls_made": 0,
                "sources": [],
                "worker_reports": [],
                "tool_calls": [],
                "provider_attempts": [],
            },
        )
        for i, worker in enumerate(workers)
    ]


def search_worker(
    state: ResearchGraphState,
    *,
    run_id: str,
    request: TurnRequest,
    tools: Tools,
    progress: ProgressCallback | None = None,
) -> dict:
    """Execute one search worker: calls tools.search_web and returns sources.

    This node is invoked once per Send from dispatch_search_router.
    Results accumulate in main state via operator.add reducers.

    CRITICAL: Do NOT return visited_nodes here — multiple parallel search_worker
    invocations cannot write the same last-write-wins field at the same step
    (LangGraph raises InvalidUpdateError). "search_worker" is added to
    visited_nodes by the rank node after the fan-out joins.
    """
    from app.services.agent.research_models import SearchWorkerPlan, SearchWorkerReport

    worker_index = state.get("worker_index", 0)
    worker_dict = state.get("worker_plan", {})

    try:
        worker = SearchWorkerPlan.model_validate(worker_dict)
    except Exception as exc:
        logger.warning("search_worker: invalid worker_plan: %s", exc)
        return {"sources": [], "worker_reports": [], "tool_calls": [], "provider_attempts": [],
                "tool_calls_made": 0, "cost_usd_spent": 0.0, "model_calls_made": 0}

    sources = []
    tool_calls_count = 0

    if tools is not None:
        try:
            sources, call = tools.search_web(worker.query, worker.max_results)
            tool_calls_count = 1
            emit_graph_event(
                progress,
                run_id=run_id,
                node_name="search_worker",
                message=f"Worker {worker_index}: '{worker.query[:60]}' → {len(sources)} result(s).",
                worker_index=worker_index,
                query=worker.query,
                source_count=len(sources),
                ok=call.ok,
                cost_usd_spent=0.0,
                tool_calls_made=tool_calls_count,
                model_calls_made=0,
            )
        except Exception as exc:
            logger.warning("search_worker %d failed: %s", worker_index, exc)
            from app.services.agent.models import ToolCall
            call = ToolCall(name="web_search", input={"query": worker.query}, ok=False, error=str(exc))
    else:
        from app.services.agent.models import ToolCall
        call = ToolCall(name="web_search", input={"query": worker.query}, ok=False, error="no tools")

    report = SearchWorkerReport(
        worker_id=worker.worker_id if hasattr(worker, "worker_id") else f"w{worker_index}",
        question=worker.question,
        query=worker.query,
        sources=sources,
        source_count=len(sources),
        ok=len(sources) > 0,
    )

    # Return only the deltas for accumulated fields.
    # visited_nodes intentionally omitted (parallel write restriction).
    return {
        "sources": sources,
        "worker_reports": [report],
        "tool_calls": [call],
        "provider_attempts": [],
        "tool_calls_made": tool_calls_count,
        "cost_usd_spent": 0.0,
        "model_calls_made": 0,
    }


# ---------------------------------------------------------------------------
# Slice 2 real nodes — post-fan-out sequential
# ---------------------------------------------------------------------------

def rank(
    state: ResearchGraphState,
    *,
    run_id: str,
    request: TurnRequest,
    tools: Tools | None = None,
    progress: ProgressCallback | None = None,
) -> dict:
    """Rank aggregated sources and store ranked URLs for the read node."""
    from app.services.agent.research_synthesis import rank_sources

    # search_worker cannot write visited_nodes (parallel write restriction).
    # Inject "search_worker" here if any workers ran.
    prior = state.get("visited_nodes") or []
    if (state.get("worker_reports") or []) and "search_worker" not in prior:
        prior = [*prior, "search_worker"]
    visited = [*prior, "rank"]

    all_sources = state.get("sources") or []
    research_plan = state.get("plan")

    ranked = rank_sources(all_sources, research_plan) if all_sources else []
    max_sources = (research_plan.max_sources if research_plan else 6) or 6
    # RankedSource wraps source in .source field; url is at .source.url
    ranked_urls = [r.source.url for r in ranked[:max_sources] if r.source.url]

    emit_graph_event(
        progress,
        run_id=run_id,
        node_name="rank",
        message=f"Ranked {len(all_sources)} source(s); selected top {len(ranked_urls)} for reading.",
        total_sources=len(all_sources),
        selected_count=len(ranked_urls),
        cost_usd_spent=0.0,
        tool_calls_made=0,
        model_calls_made=0,
    )
    return {
        "visited_nodes": visited,
        "artifacts": {**state.get("artifacts", {}), "rank": {"status": "done"}},
        "ranked_source_urls": ranked_urls,
        "source_inventory": [s.url for s in all_sources if s.url],
        "cost_usd_spent": 0.0,
        "tool_calls_made": 0,
        "model_calls_made": 0,
    }


def read(
    state: ResearchGraphState,
    *,
    run_id: str,
    request: TurnRequest,
    tools: Tools | None = None,
    progress: ProgressCallback | None = None,
) -> dict:
    """Extract full content from ranked source URLs."""
    visited = [*state.get("visited_nodes", []), "read"]
    ranked_urls = state.get("ranked_source_urls") or []

    if not ranked_urls or tools is None:
        emit_graph_event(
            progress, run_id=run_id, node_name="read",
            message="Read: no URLs to extract.",
            cost_usd_spent=0.0, tool_calls_made=0, model_calls_made=0,
        )
        return {
            "visited_nodes": visited,
            "artifacts": {**state.get("artifacts", {}), "read": {"status": "skipped"}},
            "tool_calls": [],   # delta
            "cost_usd_spent": 0.0,
            "tool_calls_made": 0,
            "model_calls_made": 0,
        }

    batch_size = 4
    # ranked_urls is already bounded by research_plan.max_sources (set in rank node).
    # No batch cap here — [:2] was written when max_sources was always 6, but
    # multi-subject queries now correctly budget max_sources=18+ and the old cap
    # silently discards the majority of ranked sources for 5-subject comparisons.
    batches = [ranked_urls[i:i + batch_size] for i in range(0, len(ranked_urls), batch_size)]

    extracted_sources = []
    new_tool_calls = []

    for batch in batches:
        try:
            sources, call = tools.extract_urls(batch, max_chars_per_source=3000)
            extracted_sources.extend(sources)
            new_tool_calls.append(call)
        except Exception as exc:
            logger.warning("read node: extract_urls failed for batch %s: %s", batch, exc)
            from app.services.agent.models import ToolCall
            new_tool_calls.append(ToolCall(name="read_url", input={"urls": batch}, ok=False, error=str(exc)))

    emit_graph_event(
        progress,
        run_id=run_id,
        node_name="read",
        message=f"Read {len(extracted_sources)} source(s) from {len(batches)} batch(es).",
        extracted_count=len(extracted_sources),
        batch_count=len(batches),
        cost_usd_spent=0.0,
        tool_calls_made=len(new_tool_calls),
        model_calls_made=0,
    )
    return {
        "visited_nodes": visited,
        "artifacts": {**state.get("artifacts", {}), "read": {"status": "done"}},
        "sources": extracted_sources,   # delta → appended via operator.add reducer
        "tool_calls": new_tool_calls,   # delta
        "cost_usd_spent": 0.0,
        "tool_calls_made": len(new_tool_calls),
        "model_calls_made": 0,
    }


def classify_claims(
    state: ResearchGraphState,
    *,
    run_id: str,
    request: TurnRequest,
    tools: Tools | None = None,
    progress: ProgressCallback | None = None,
) -> dict:
    """Pre-classify claim sentences from extracted sources before binding (Slice 4).

    Calls classify_claims_llm per source using the extracted content, building a
    minimal EvidenceItem from each Source so the LLM classifier has the source_type,
    url, and title context it needs.

    Results are stored as claim_classification_results (Annotated[list, operator.add])
    so they accumulate across any future parallel classification paths.

    Results flow into bind via state field claim_classification_results.  bind reads
    them as pre_classified_by_url and passes to bind_evidence/extract_evidence_claims,
    which skips the LLM call for already-classified sources (P2 fix, Slice 4).

    Falls back to empty results gracefully (regex fallback fires inside
    classify_claims_llm when the LLM is unavailable).
    """
    from app.services.agent.research_evidence import (
        classify_claims_llm,
        _claim_candidate_sentences,  # type: ignore[attr-defined]
    )
    from app.services.agent.research_models import EvidenceItem, ResearchBudgetLedger
    from app.services.agent.research_profiles import research_budget_for
    from app.services.agent.research_utils import classify_source_type

    node_name: GraphNodeName = "classify_claims"
    visited = [*state.get("visited_nodes", []), node_name]
    sources = state.get("sources") or []
    overrides = getattr(request, "model_overrides", None)

    # One ledger for the whole node — records each classify_claims_llm LLM call.
    # ledger.model_calls delta distinguishes a real LLM call from a regex fallback
    # (fallback returns results without calling record_model_call).
    ledger = ResearchBudgetLedger(budget=research_budget_for(request))

    emit_graph_event(
        progress,
        run_id=run_id,
        node_name=node_name,
        message=f"Classifying claim sentences for {len(sources)} source(s).",
        source_count=len(sources),
    )

    classification_records: list[dict[str, Any]] = []

    for idx, source in enumerate(sources):
        content = source.content or source.snippet or ""
        if not content:
            continue

        # Build a minimal EvidenceItem — classify_claims_llm only needs
        # source_type, url, and title from it.
        item = EvidenceItem(
            source_id=f"S{idx + 1}",
            title=source.title or "",
            url=source.url or "",
            source_type=classify_source_type(source.url or ""),
            evidence=content,
        )

        sentences = _claim_candidate_sentences(content)
        if not sentences:
            continue

        calls_before = ledger.model_calls
        # Batch per source — one LLM call covers all sentences for this source.
        llm_results = classify_claims_llm(
            sentences[:5],  # cap at 5 sentences per source for budget control
            item,
            overrides=overrides,
            ledger=ledger,
        )
        # Only append a record when results are present (regex fallback may also
        # return results, but ledger.model_calls won't advance for those — that's
        # intentional: regex fallback is free and should not count as a model call).
        if llm_results:
            record: dict[str, Any] = {
                "url": source.url,
                "title": source.title or "",
                "source_type": item.source_type,
                "sentence_count": len(sentences[:5]),
                "classifications": llm_results,
                # Flag whether this came from a real LLM call or regex fallback.
                "llm_classified": ledger.model_calls > calls_before,
            }
            classification_records.append(record)

    total_model_calls = ledger.model_calls
    total_cost = ledger.cost_usd

    emit_graph_event(
        progress,
        run_id=run_id,
        node_name=node_name,
        message=(
            f"Classified claims for {len(classification_records)}/{len(sources)} source(s); "
            f"{total_model_calls} LLM call(s), ${total_cost:.5f}."
        ),
        classified_source_count=len(classification_records),
        model_calls=total_model_calls,
    )

    return {
        "visited_nodes": visited,
        "artifacts": {**state.get("artifacts", {}), node_name: {"status": "real"}},
        "claim_classification_results": classification_records,
        "cost_usd_spent": total_cost,
        "tool_calls_made": 0,
        "model_calls_made": total_model_calls,
    }


def expand_source_graph(
    state: ResearchGraphState,
    *,
    run_id: str,
    request: TurnRequest,
    tools: Tools | None = None,
    progress: ProgressCallback | None = None,
) -> dict:
    """Follow deep links from extracted sources."""
    from app.services.agent.research_synthesis import extract_deep_link_candidates
    from app.services.agent.research_synthesis import is_public_source_url
    from app.services.agent.research_synthesis import balance_sources_for_deep_links, subjects_for_deep_link_balance

    visited = [*state.get("visited_nodes", []), "expand_source_graph"]
    all_sources = state.get("sources") or []
    source_inventory = set(state.get("source_inventory") or [])

    if not all_sources or tools is None:
        emit_graph_event(
            progress, run_id=run_id, node_name="expand_source_graph",
            message="Source graph expansion: no sources or tools.",
            cost_usd_spent=0.0, tool_calls_made=0, model_calls_made=0,
        )
        return {
            "visited_nodes": visited,
            "artifacts": {**state.get("artifacts", {}), "expand_source_graph": {"status": "skipped"}},
            "source_graph_expansion_results": [],  # delta
            "cost_usd_spent": 0.0,
            "tool_calls_made": 0,
            "model_calls_made": 0,
        }

    subjects = subjects_for_deep_link_balance(getattr(request, "message", "") or "")
    candidates = extract_deep_link_candidates(
        balance_sources_for_deep_links(all_sources, subjects), max_links=4
    )
    urls = [
        c.url for c in candidates
        if c.url and c.url not in source_inventory and is_public_source_url(c.url)
    ][:4]

    expanded_sources = []
    deep_tool_calls = []

    if urls:
        try:
            expanded_sources, call = tools.extract_urls(urls, max_chars_per_source=2500)
            deep_tool_calls.append(call)
        except Exception as exc:
            logger.warning("expand_source_graph: extract_urls failed: %s", exc)
            from app.services.agent.models import ToolCall
            deep_tool_calls.append(ToolCall(name="read_url", input={"urls": urls}, ok=False, error=str(exc)))

    emit_graph_event(
        progress,
        run_id=run_id,
        node_name="expand_source_graph",
        message=f"Source graph expansion: {len(expanded_sources)} deep-link source(s).",
        candidate_count=len(candidates),
        expanded_count=len(expanded_sources),
        cost_usd_spent=0.0,
        tool_calls_made=len(deep_tool_calls),
        model_calls_made=0,
    )
    return {
        "visited_nodes": visited,
        "artifacts": {**state.get("artifacts", {}), "expand_source_graph": {"status": "done"}},
        "sources": expanded_sources,                                        # delta
        "tool_calls": deep_tool_calls,                                      # delta
        "source_graph_expansion_results": [{"url": c.url} for c in candidates[:len(urls)]],  # delta
        "cost_usd_spent": 0.0,
        "tool_calls_made": len(deep_tool_calls),
        "model_calls_made": 0,
    }


def bind(
    state: ResearchGraphState,
    *,
    run_id: str,
    request: TurnRequest,
    tools: Tools | None = None,
    progress: ProgressCallback | None = None,
) -> dict:
    """Merge sources by URL priority and call bind_evidence to produce EvidencePack."""
    from app.services.agent.research_evidence import bind_evidence
    from app.services.agent.research_models import ResearchBudgetLedger
    from app.services.agent.research_profiles import research_budget_for

    visited = [*state.get("visited_nodes", []), "bind"]
    all_sources = state.get("sources") or []
    research_plan = state.get("plan")
    coverage_contract = state.get("contract")

    # URL-priority merge: prefer content-rich (extracted) over snippet-only (search result)
    merged_sources = _url_priority_merge(all_sources)

    # Ledger tracks classify_claims_llm calls made inside bind_evidence.
    # Note: classify_claims ran a pre-pass over raw source.content sentences and
    # stored results in state as claim_classification_results — those results are
    # intentionally NOT forwarded here.  classify_claims operates on source.content
    # while bind operates on _select_evidence_passages passage text; the sentence
    # sets differ, so index-based reuse would corrupt claim_type/claim_role/
    # freshness_risk.  bind always classifies its own passage sentences fresh.
    bind_ledger = ResearchBudgetLedger(budget=research_budget_for(request))

    evidence = bind_evidence(
        merged_sources,
        research_plan,
        max_items=bind_ledger.budget.max_sources,
        contract=coverage_contract,
        overrides=getattr(request, "model_overrides", None),
        ledger=bind_ledger,
    )

    emit_graph_event(
        progress,
        run_id=run_id,
        node_name="bind",
        message=(
            f"Evidence bound: {len(evidence.items)} item(s) from {len(merged_sources)} source(s); "
            f"{bind_ledger.model_calls} classify call(s), ${bind_ledger.cost_usd:.5f}."
        ),
        evidence_item_count=len(evidence.items),
        source_count=len(merged_sources),
        cost_usd_spent=bind_ledger.cost_usd,
        tool_calls_made=0,
        model_calls_made=bind_ledger.model_calls,
    )
    return {
        "visited_nodes": visited,
        "artifacts": {**state.get("artifacts", {}), "bind": {"status": "done"}},
        "evidence": evidence,
        "cost_usd_spent": bind_ledger.cost_usd,
        "tool_calls_made": 0,
        "model_calls_made": bind_ledger.model_calls,
    }


# ---------------------------------------------------------------------------
# Synthesis half: synthesize → verify → judge → repair  (Slice 3)
# ---------------------------------------------------------------------------

def _langgraph_stream_writer():
    try:
        from langgraph.config import get_stream_writer

        return get_stream_writer()
    except RuntimeError:
        return None


def synthesize(
    state: ResearchGraphState,
    *,
    run_id: str,
    request: TurnRequest,
    tools: Tools | None = None,
    progress: ProgressCallback | None = None,
) -> dict:
    """Call synthesize_answer(request, plan, evidence) → answer text + model metadata.

    Returns delta-only: answer, model_used, latency_ms, cost_usd_spent, model_calls_made.
    """
    from app.services.agent.research_synthesis import synthesize_answer_stream

    node_name: GraphNodeName = "synthesize"
    visited = [*state.get("visited_nodes", []), node_name]
    artifacts = {**state.get("artifacts", {}), node_name: {"status": "real"}}

    plan = state.get("plan")
    evidence = state.get("evidence")

    emit_graph_event(
        progress,
        run_id=run_id,
        node_name=node_name,
        message="Writing one coherent answer from the evidence.",
    )

    if plan is None or evidence is None:
        logger.warning("synthesize: plan or evidence missing — returning empty answer")
        return {
            "visited_nodes": visited,
            "artifacts": artifacts,
            "answer": "",
            "model_used": "synthesize-no-plan",
            "latency_ms": 0,
            "cost_usd_spent": 0.0,
            "model_calls_made": 0,
        }

    writer = _langgraph_stream_writer()

    def _on_delta(text: str) -> None:
        if writer is not None:
            writer({"answer_delta": text, "source_node": node_name})

    response = synthesize_answer_stream(request, plan, evidence, on_delta=_on_delta)

    emit_graph_event(
        progress,
        run_id=run_id,
        node_name=node_name,
        message=f"Synthesis used {response.model_used or 'the configured synthesis model'}.",
        model_used=response.model_used,
        latency_ms=response.latency_ms,
        cost_usd=response.cost_usd,
    )

    return {
        "visited_nodes": visited,
        "artifacts": artifacts,
        "answer": response.text or "",
        "model_used": response.model_used or "",
        "latency_ms": response.latency_ms or 0,
        "cost_usd_spent": response.cost_usd or 0.0,
        "model_calls_made": 1,
    }


def verify(
    state: ResearchGraphState,
    *,
    run_id: str,
    request: TurnRequest,
    tools: Tools | None = None,
    progress: ProgressCallback | None = None,
) -> dict:
    """Call verify_citations_semantically(answer, evidence) → CitationVerification in state.

    Returns delta-only: last_citation_verification, cost_usd_spent, model_calls_made.
    LLM call is skipped (heuristic path) when answer has no [S#] citations.
    """
    from app.services.agent.research_planner import verify_citations_semantically

    node_name: GraphNodeName = "verify"
    visited = [*state.get("visited_nodes", []), node_name]
    artifacts = {**state.get("artifacts", {}), node_name: {"status": "real"}}

    answer = state.get("answer", "")
    evidence = state.get("evidence")
    plan = state.get("plan")

    emit_graph_event(
        progress,
        run_id=run_id,
        node_name=node_name,
        message="Checking citations and source support before publishing.",
    )

    if not evidence:
        return {
            "visited_nodes": visited,
            "artifacts": artifacts,
            "last_citation_verification": None,
            "cost_usd_spent": 0.0,
            "model_calls_made": 0,
        }

    expected_primary_role = (
        plan.expected_primary_role if plan and hasattr(plan, "expected_primary_role") else None
    )
    result = verify_citations_semantically(
        answer,
        evidence,
        overrides=getattr(request, "model_overrides", None),
        expected_primary_role=expected_primary_role,
    )

    emit_graph_event(
        progress,
        run_id=run_id,
        node_name=node_name,
        message="Citation verification complete.",
        repair_needed=result.repair_needed,
        source=result.source,
    )

    return {
        "visited_nodes": visited,
        "artifacts": artifacts,
        "last_citation_verification": result,
        "cost_usd_spent": result.cost_usd or 0.0,
        "model_calls_made": 1 if result.model_used else 0,
    }


def judge(
    state: ResearchGraphState,
    *,
    run_id: str,
    request: TurnRequest,
    tools: Tools | None = None,
    progress: ProgressCallback | None = None,
) -> dict:
    """Call judge_research(request, plan, evidence, answer) + enforce citation verification.

    Two signal sources feed the final decision:

    1. judge_research() — heuristic score: coverage, citation density, length.
       next_action:
         "publish"        — can_publish=True (score ≥ threshold, no signals)
         "research_more"  — status="repair" (fixable by repair node)
         "stop_with_gaps" — status="fail" (needs full redo, repair won't help)

    2. last_citation_verification — signals from verify node:
         repair_needed, role_mismatch_issues, unresolved_conflicts,
         asks_permission_to_continue.
       If ANY of these are present, next_action is forced to "research_more"
       even if judge_research alone would have said "publish".
       The repair instruction from the verifier is added to judge_result.issues
       so repair node can act on it.

    No LLM call — pure heuristic + stored verifier output.
    """
    from app.services.agent.research_synthesis import judge_research
    from app.services.agent.research_models import ResearchJudgeResult

    node_name: GraphNodeName = "judge"
    visited = [*state.get("visited_nodes", []), node_name]
    artifacts = {**state.get("artifacts", {}), node_name: {"status": "real"}}

    answer = state.get("answer", "")
    evidence = state.get("evidence")
    plan = state.get("plan")
    coverage_contract = state.get("contract")
    citation_result = state.get("last_citation_verification")

    emit_graph_event(
        progress,
        run_id=run_id,
        node_name=node_name,
        message="Checking whether the answer is ready to publish.",
    )

    if plan is None or evidence is None:
        judge_result = ResearchJudgeResult(
            status="pass", score=1.0, issues=[], can_publish=True
        )
        emit_graph_event(
            progress, run_id=run_id, node_name=node_name,
            message="Judge skipped (no plan/evidence) — marking as publish.",
        )
        return {
            "visited_nodes": visited,
            "artifacts": artifacts,
            "judge_result": judge_result,
            "next_action": "publish",
            "model_calls_made": 0,
        }

    judge_result = judge_research(request, plan, evidence, answer, coverage_contract)

    # --- Enforce citation verification signals --------------------------------
    # Mirrors the legacy check in _synthesize_verify_and_judge:
    #   needs_repair = (
    #     citation_result.repair_needed
    #     or bool(citation_result.role_mismatch_issues)
    #     or bool(citation_result.unresolved_conflicts)
    #     or citation_result.asks_permission_to_continue
    #   )
    citation_repair_signals: list[str] = []
    if citation_result is not None:
        if citation_result.repair_needed and citation_result.repair_instruction:
            citation_repair_signals.append(citation_result.repair_instruction)
        for issue in (citation_result.role_mismatch_issues or []):
            citation_repair_signals.append(f"Role mismatch: {issue}")
        for conflict in (citation_result.unresolved_conflicts or []):
            citation_repair_signals.append(f"Unresolved conflict: {conflict}")
        if citation_result.asks_permission_to_continue:
            citation_repair_signals.append(
                "Answer asks permission to continue research — rewrite to deliver findings directly."
            )

    if citation_repair_signals:
        # Merge verifier issues into judge_result so repair node has full context.
        merged_issues = list(judge_result.issues) + citation_repair_signals
        repair_instruction = (
            judge_result.repair_instruction
            or citation_repair_signals[0]
        )
        judge_result = ResearchJudgeResult(
            status="repair",
            score=judge_result.score,
            issues=merged_issues,
            repair_instruction=repair_instruction,
            can_publish=False,
        )

    if judge_result.can_publish:
        next_action: str = "publish"
    elif judge_result.status == "repair":
        next_action = "research_more"
    else:  # "fail" — repair cannot recover; full redo would be needed
        next_action = "stop_with_gaps"

    emit_graph_event(
        progress,
        run_id=run_id,
        node_name=node_name,
        message=f"Judge recommends {next_action} (score={judge_result.score:.2f}).",
        score=judge_result.score,
        can_publish=judge_result.can_publish,
        next_action=next_action,
        issues=judge_result.issues,
    )

    return {
        "visited_nodes": visited,
        "artifacts": artifacts,
        "judge_result": judge_result,
        "next_action": next_action,
        "model_calls_made": 0,  # pure heuristic — no LLM call
    }


def repair(
    state: ResearchGraphState,
    *,
    run_id: str,
    request: TurnRequest,
    tools: Tools | None = None,
    progress: ProgressCallback | None = None,
) -> dict:
    """Repair the answer when judge requests it.

    Pass-through (no LLM call) when:
      - judge_result is None (no judge ran)
      - judge_result.can_publish is True (already approved)
      - judge_result.status != "repair" (e.g. "fail" — repair cannot recover)

    When repair runs: calls repair_research_answer(request, plan, evidence, answer, judge_result).
    Updates answer, model_used, latency_ms, cost_usd_spent, repair_history.
    """
    from app.services.agent.research_synthesis import repair_research_answer_stream

    node_name: GraphNodeName = "repair"
    visited = [*state.get("visited_nodes", []), node_name]
    artifacts = {**state.get("artifacts", {}), node_name: {"status": "real"}}

    judge_result = state.get("judge_result")

    # ── Pass-through paths ───────────────────────────────────────────────────
    skip = (
        judge_result is None
        or judge_result.can_publish
        or judge_result.status != "repair"
    )
    if skip:
        emit_graph_event(
            progress,
            run_id=run_id,
            node_name=node_name,
            message="Repair skipped — answer already publishable or cannot be repaired.",
        )
        return {
            "visited_nodes": visited,
            "artifacts": artifacts,
            "repair_history": list(state.get("repair_history") or []),
            "cost_usd_spent": 0.0,
            "tool_calls_made": 0,
            "model_calls_made": 0,
        }

    # ── Actual repair ────────────────────────────────────────────────────────
    answer = state.get("answer", "")
    evidence = state.get("evidence")
    plan = state.get("plan")
    instruction = judge_result.repair_instruction or "Improve the answer with better citations."

    emit_graph_event(
        progress,
        run_id=run_id,
        node_name=node_name,
        message=f"Repairing answer: {instruction[:120]}",
        repair_instruction=instruction,
    )

    if plan is None or evidence is None:
        logger.warning("repair: plan or evidence missing — cannot repair, returning as-is")
        return {
            "visited_nodes": visited,
            "artifacts": artifacts,
            "repair_history": list(state.get("repair_history") or []),
            "cost_usd_spent": 0.0,
            "tool_calls_made": 0,
            "model_calls_made": 0,
        }

    writer = _langgraph_stream_writer()

    def _on_delta(text: str) -> None:
        if writer is not None:
            writer({"answer_delta": text, "source_node": node_name})

    response = repair_research_answer_stream(request, plan, evidence, answer, judge_result, on_delta=_on_delta)
    history = [*(state.get("repair_history") or []), instruction]

    emit_graph_event(
        progress,
        run_id=run_id,
        node_name=node_name,
        message=f"Repair used {response.model_used or 'the configured repair model'}.",
        model_used=response.model_used,
        latency_ms=response.latency_ms,
        cost_usd=response.cost_usd,
    )

    return {
        "visited_nodes": visited,
        "artifacts": artifacts,
        "answer": response.text or answer,
        "model_used": response.model_used or "",
        "latency_ms": response.latency_ms or 0,
        "cost_usd_spent": response.cost_usd or 0.0,
        "model_calls_made": 1,
        "repair_history": history,
    }


# ---------------------------------------------------------------------------
# Budget gate node
# ---------------------------------------------------------------------------

def budget_gate(
    state: ResearchGraphState,
    *,
    run_id: str,
    request: TurnRequest,
    tools: Tools | None = None,
    progress: ProgressCallback | None = None,
) -> dict:
    """Evaluate accumulated spend against the request's actual ResearchBudget.

    Thresholds come from research_budget_for(request) — NOT hard-coded constants —
    so easy/regular/deep tiers and multi-subject scaling are all respected.

    Pause contract reports the CONTINUATION budget: the amount the user would need
    to authorise to finish the pipeline from the current checkpoint.  This is always
    positive (one full budget ceiling worth of additional spend) rather than the
    deficit to the already-exceeded threshold.
    """
    import datetime
    from langgraph.types import interrupt

    from app.services.agent.models import new_id
    from app.services.agent.research_profiles import research_budget_for

    budget = research_budget_for(request)
    cost = state.get("cost_usd_spent", 0.0)
    tool_calls = state.get("tool_calls_made", 0)
    model_calls = state.get("model_calls_made", 0)

    # synthesis + repair reserve from the request's budget
    synthesis_reserve = (
        budget.reserved_synthesis_model_calls + budget.reserved_repair_model_calls
    )

    if cost >= budget.max_cost_usd:
        decision = BudgetDecision.REQUIRE_HUMAN_APPROVAL
    elif model_calls + synthesis_reserve > budget.max_model_calls:
        decision = BudgetDecision.RESERVE_FOR_SYNTHESIS
    elif tool_calls > budget.max_tool_calls:
        decision = BudgetDecision.CONTINUE_WITH_REDUCED_SEARCH
    else:
        decision = BudgetDecision.CONTINUE

    emit_graph_event(
        progress,
        run_id=run_id,
        node_name="budget_gate",
        message=f"Budget gate: {decision.value} (spent ${cost:.4f} of ${budget.max_cost_usd}).",
        cost_usd_spent=cost,
        tool_calls_made=tool_calls,
        model_calls_made=model_calls,
        budget_max_cost_usd=budget.max_cost_usd,
        budget_max_tool_calls=budget.max_tool_calls,
        budget_max_model_calls=budget.max_model_calls,
        decision=decision.value,
    )

    updates: dict = {"budget_decision": decision}
    if decision == BudgetDecision.REQUIRE_HUMAN_APPROVAL:
        # required_additional_budget_usd is the CONTINUATION budget — how much the user
        # must authorise to run the remainder of the pipeline.  It is always positive
        # and equals one full budget ceiling (same as what was originally granted).
        continuation_budget = budget.max_cost_usd
        pause_contract = {
            "pause_reason": (
                f"Cost ceiling reached: ${cost:.4f} spent against "
                f"${budget.max_cost_usd:.4f} limit."
            ),
            "required_additional_budget_usd": continuation_budget,
            "resume_checkpoint_id": run_id,
            "audit_event_id": new_id("lgpause"),
            "paused_at": datetime.datetime.utcnow().isoformat() + "Z",
        }
        try:
            approval = interrupt(pause_contract)
        except RuntimeError as exc:
            if "outside of a runnable context" not in str(exc):
                raise
            updates["pause_contract"] = pause_contract
            return updates
        if isinstance(approval, dict):
            approval_contract = dict(approval)
        else:
            approval_contract = {"approved": bool(approval)}
        approval_contract.setdefault("approved_at", datetime.datetime.utcnow().isoformat() + "Z")
        approval_contract.setdefault("updated_budget_ceiling_usd", cost + continuation_budget)
        approval_contract.setdefault("approval_audit_event_id", new_id("lgapprove"))
        return {
            "budget_decision": BudgetDecision.CONTINUE,
            "pause_contract": pause_contract,
            "approval_contract": approval_contract,
        }
    return updates
