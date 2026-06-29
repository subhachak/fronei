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
    batches = [ranked_urls[i:i + batch_size] for i in range(0, len(ranked_urls), batch_size)][:2]

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
    """Pre-classify claims from extracted sources before binding.

    Slice 2 status: PARTIAL STUB.
    `classify_claims_llm` is called per EvidenceItem inside `bind_evidence`, so
    claim classification already happens implicitly in the bind node. This node
    exists as the explicit pipeline stage so it can be upgraded in Slice 3 to:
      - run `classify_claims_llm` independently per source (not per evidence item)
      - emit per-source classification events and budget deltas
      - write `claim_classification_results` with per-sentence classifications

    Until Slice 3, this node is a pass-through: it records itself as visited,
    emits a progress event marking it as stub, and returns 0-delta budget counters.
    The actual LLM calls are counted against the model budget when bind fires.
    """
    visited = [*state.get("visited_nodes", []), "classify_claims"]
    sources = state.get("sources") or []
    emit_graph_event(
        progress,
        run_id=run_id,
        node_name="classify_claims",
        message=(
            f"classify_claims: {len(sources)} source(s) ready; "
            "per-source LLM classification fires inside bind_evidence (Slice 3 will extract it here)."
        ),
        source_count=len(sources),
        stub=True,
        cost_usd_spent=0.0,
        tool_calls_made=0,
        model_calls_made=0,
    )
    return {
        "visited_nodes": visited,
        "artifacts": {**state.get("artifacts", {}), "classify_claims": {"status": "stub_slice2"}},
        "claim_classification_results": [],  # delta — Slice 3 will populate this
        "cost_usd_spent": 0.0,
        "tool_calls_made": 0,
        "model_calls_made": 0,
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

    candidates = extract_deep_link_candidates(all_sources, max_links=4)
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

    visited = [*state.get("visited_nodes", []), "bind"]
    all_sources = state.get("sources") or []
    research_plan = state.get("plan")
    coverage_contract = state.get("contract")

    # URL-priority merge: prefer content-rich (extracted) over snippet-only (search result)
    merged_sources = _url_priority_merge(all_sources)

    evidence = bind_evidence(
        merged_sources,
        research_plan,
        contract=coverage_contract,
        overrides=getattr(request, "model_overrides", None),
    )

    emit_graph_event(
        progress,
        run_id=run_id,
        node_name="bind",
        message=f"Evidence bound: {len(evidence.items)} item(s) from {len(merged_sources)} source(s).",
        evidence_item_count=len(evidence.items),
        source_count=len(merged_sources),
        cost_usd_spent=0.0,
        tool_calls_made=0,
        model_calls_made=0,
    )
    return {
        "visited_nodes": visited,
        "artifacts": {**state.get("artifacts", {}), "bind": {"status": "done"}},
        "evidence": evidence,
        "cost_usd_spent": 0.0,
        "tool_calls_made": 0,
        "model_calls_made": 0,
    }


# ---------------------------------------------------------------------------
# Remaining stub nodes (Slice 3+)
# ---------------------------------------------------------------------------

def _stub_node(
    state: ResearchGraphState,
    *,
    run_id: str,
    node_name: GraphNodeName,
    progress: ProgressCallback | None,
) -> dict:
    visited = [*state.get("visited_nodes", []), node_name]
    artifacts = {**state.get("artifacts", {}), node_name: {"status": "stubbed"}}
    emit_graph_event(
        progress,
        run_id=run_id,
        node_name=node_name,
        message=f"LangGraph {node_name} node stubbed.",
        placeholder=True,
    )
    return {
        "visited_nodes": visited,
        "artifacts": artifacts,
        "cost_usd_spent": 0.0,
        "tool_calls_made": 0,
        "model_calls_made": 0,
    }


def synthesize(
    state: ResearchGraphState,
    *,
    run_id: str,
    request: TurnRequest,
    tools: Tools | None = None,
    progress: ProgressCallback | None = None,
) -> dict:
    result = _stub_node(state, run_id=run_id, node_name="synthesize", progress=progress)
    return {**result, "answer": "", "model_used": "langgraph-slice-2-stub", "latency_ms": 0, "model_calls_made": 1}


def verify(
    state: ResearchGraphState,
    *,
    run_id: str,
    request: TurnRequest,
    tools: Tools | None = None,
    progress: ProgressCallback | None = None,
) -> dict:
    return _stub_node(state, run_id=run_id, node_name="verify", progress=progress)


def judge(
    state: ResearchGraphState,
    *,
    run_id: str,
    request: TurnRequest,
    tools: Tools | None = None,
    progress: ProgressCallback | None = None,
) -> dict:
    result = _stub_node(state, run_id=run_id, node_name="judge", progress=progress)
    return {**result, "next_action": "publish", "model_calls_made": 1}


def repair(
    state: ResearchGraphState,
    *,
    run_id: str,
    request: TurnRequest,
    tools: Tools | None = None,
    progress: ProgressCallback | None = None,
) -> dict:
    result = _stub_node(state, run_id=run_id, node_name="repair", progress=progress)
    return {**result, "model_calls_made": 1}


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
        updates["pause_contract"] = {
            "pause_reason": (
                f"Cost ceiling reached: ${cost:.4f} spent against "
                f"${budget.max_cost_usd:.4f} limit."
            ),
            "required_additional_budget_usd": continuation_budget,
            "resume_checkpoint_id": "",
            "audit_event_id": new_id("lgpause"),
            "paused_at": datetime.datetime.utcnow().isoformat() + "Z",
        }
    return updates
