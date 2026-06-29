from __future__ import annotations

import operator
from enum import Enum
from typing import Annotated, Any, Literal, TypedDict

from app.services.agent.models import Source, ToolCall
from app.services.agent.research_models import (
    CoverageContract,
    EvidencePack,
    ResearchBrief,
    ResearchJudgeResult,
    ResearchPlan,
    SearchWorkerReport,
)


# ---------------------------------------------------------------------------
# Node name literal — used in nodes.py and graph.py.
# ---------------------------------------------------------------------------
GraphNodeName = Literal[
    "brief",
    "subject_derivation",
    "contract",
    "plan",
    "search",
    "rank",
    "read",
    "classify_claims",
    "expand_source_graph",
    "bind",
    "synthesize",
    "verify",
    "judge",
    "repair",
]


# ---------------------------------------------------------------------------
# BudgetDecision
#
# CONTINUE, RESERVE_FOR_SYNTHESIS, STOP_WITH_GAPS match legacy
# ResearchBudgetLedger behaviour and are oracle-comparable.
# CONTINUE_WITH_REDUCED_SEARCH and REQUIRE_HUMAN_APPROVAL are NEW
# functionality — test them directly, never via the parity comparator.
# ---------------------------------------------------------------------------
class BudgetDecision(str, Enum):
    CONTINUE = "continue"
    CONTINUE_WITH_REDUCED_SEARCH = "continue_with_reduced_search"
    RESERVE_FOR_SYNTHESIS = "reserve_for_synthesis"
    STOP_WITH_GAPS = "stop_with_gaps"
    REQUIRE_HUMAN_APPROVAL = "require_human_approval"


# ---------------------------------------------------------------------------
# Pause / Approval contracts
#
# These are two separate contracts populated at two different moments:
#   - PauseContract: exists the instant the graph reaches REQUIRE_HUMAN_APPROVAL
#   - ApprovalContract: exists only after a human has approved continuation
#
# Asserting ApprovalContract fields before approval has occurred is a test
# error, not a valid state.
# ---------------------------------------------------------------------------
class PauseContract(TypedDict, total=False):
    """Populated when budget_decision == REQUIRE_HUMAN_APPROVAL."""
    pause_reason: str
    required_additional_budget_usd: float
    resume_checkpoint_id: str        # LangGraph checkpoint ID to resume from
    audit_event_id: str              # Logged at pause; immutable
    paused_at: str                   # ISO-8601 timestamp


class ApprovalContract(TypedDict, total=False):
    """Populated only after a human approves — never at pause time."""
    approved_by: str
    approved_at: str                 # ISO-8601 timestamp
    updated_budget_ceiling_usd: float
    approval_audit_event_id: str     # Distinct event from PauseContract.audit_event_id


# ---------------------------------------------------------------------------
# ResearchGraphState
#
# Field list derived by reading actual trace events emitted in
# research_lead.py:  source_inventory, source_ranker, source_reader,
# search_worker_report, source_graph_expansion, budget_ledger,
# worker_reports, all_tool_calls, all_sources.
#
# Annotated[T, operator.add] fields use LangGraph's reducer protocol to
# accumulate values across nodes.  All other fields are last-write-wins.
# ---------------------------------------------------------------------------
class ResearchGraphState(TypedDict, total=False):
    # ---- Request context (written once at graph entry) --------------------
    request_message: str
    research_level: str              # "easy" | "regular" | "deep"
    run_id: str

    # ---- Pipeline products (each node writes its own output field) --------
    brief: ResearchBrief | None
    named_subjects: list[str]        # output of subject_derivation node
    contract: CoverageContract | None
    plan: ResearchPlan | None
    evidence: EvidencePack | None
    last_citation_verification: Any | None

    # ---- Accumulated search / read results (LangGraph list reducers) ------
    sources: Annotated[list[Source], operator.add]
    worker_reports: Annotated[list[SearchWorkerReport], operator.add]
    tool_calls: Annotated[list[ToolCall], operator.add]
    provider_attempts: Annotated[list[dict[str, Any]], operator.add]
    source_graph_expansion_results: Annotated[list[dict[str, Any]], operator.add]
    claim_classification_results: Annotated[list[dict[str, Any]], operator.add]

    # ---- Dedup inventories (last-write-wins; nodes reconstruct from sources) -
    source_inventory: list[str]      # canonical URL set
    query_history: list[str]         # search queries issued

    # ---- Budget counters (LangGraph add reducers) -------------------------
    cost_usd_spent: Annotated[float, operator.add]
    tool_calls_made: Annotated[int, operator.add]
    model_calls_made: Annotated[int, operator.add]

    # ---- Budget gate output -----------------------------------------------
    budget_decision: BudgetDecision | None
    iteration: int

    # ---- Synthesis / judge / repair ---------------------------------------
    judge_result: ResearchJudgeResult | None
    next_action: Literal["publish", "research_more", "stop_with_gaps", "requires_approval"] | None
    repair_history: list[str]        # repair instructions applied (audit trail)
    answer: str
    model_used: str
    latency_ms: int

    # ---- Human-in-the-loop contracts (Slice 0B new functionality) ---------
    pause_contract: PauseContract | None
    approval_contract: ApprovalContract | None

    # ---- Slice 0A compatibility fields ------------------------------------
    visited_nodes: list[str]
    artifacts: dict[str, Any]
