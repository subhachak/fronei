from __future__ import annotations

import logging

from app.services.agent.context_classifier import ContextDecision
from app.services.agent.context_contracts import (
    LAYER_L2,
    LAYER_L1,
    LAYER_L3,
    SCOPE_ATTACHMENT,
    SCOPE_CONVERSATION,
    SCOPE_CROSS_WORKSPACE,
    SCOPE_WORKSPACE,
    SOURCE_ATTACHMENT,
    SOURCE_FACT,
    SOURCE_PRIOR_TURN,
    SOURCE_SUMMARY,
    ContextItem,
    ContextTokenBudget,
)
from app.services.agent.models import TurnRequest
from app.services.agent.research_utils import estimate_tokens

logger = logging.getLogger(__name__)

MIN_FACT_CONFIDENCE = 0.5


def get_context_items(
    request: TurnRequest,
    decision: ContextDecision,
    *,
    db=None,
    model: str | None = None,
) -> list[ContextItem]:
    """Assemble context items for this turn, then budget-limit the result.

    `model` lets a caller who already knows which model will actually serve
    this turn pass it in for precise budgeting; otherwise this resolves a
    reasonable default from the request's own quality_mode/model_overrides.
    """
    items, _evicted_counts = get_context_items_with_eviction(request, decision, db=db, model=model)
    return items


def get_context_items_with_eviction(
    request: TurnRequest,
    decision: ContextDecision,
    *,
    db=None,
    model: str | None = None,
) -> tuple[list[ContextItem], dict[str, int]]:
    """Same as get_context_items, but also returns how many items were
    dropped per layer -- for callers (runtime.py) that persist this onto
    Turn.context_tokens_json for the /admin/context-pressure report."""
    items = _assemble_context_items(request, decision, db=db)
    return _apply_context_budget(items, request=request, model=model)


def _assemble_context_items(request: TurnRequest, decision: ContextDecision, *, db=None) -> list[ContextItem]:
    if not decision.needs_context:
        return []
    if decision.intent == "vague_unresolved_followup":
        return []

    items: list[ContextItem] = []
    if SCOPE_CONVERSATION in decision.target_scopes and request.prior_turn_context:
        items.append(
            ContextItem(
                layer=LAYER_L1,
                scope=SCOPE_CONVERSATION,
                source_type=SOURCE_PRIOR_TURN,
                content=request.prior_turn_context,
                provenance=f"L1:prior_turn:conv_{request.conversation_id or 'unknown'}",
            )
        )
    if SCOPE_ATTACHMENT in decision.target_scopes and request.attachment_context:
        items.append(
            ContextItem(
                layer=LAYER_L1,
                scope=SCOPE_ATTACHMENT,
                source_type=SOURCE_ATTACHMENT,
                content=request.attachment_context,
                provenance="L1:attachment:uploaded",
            )
        )
    l2_scope = _select_l2_scope(decision.target_scopes)
    if l2_scope:
        if db is None:
            logger.debug(
                "context_registry_no_db",
                extra={"target_scopes": decision.target_scopes, "context_intent": decision.intent},
            )
            return items
        user_id = str(getattr(request, "user_id", "") or "")
        if not user_id:
            logger.debug(
                "context_registry_no_user",
                extra={"target_scopes": decision.target_scopes, "context_intent": decision.intent},
            )
            return items
        from app.services.agent.session_memory import recall_similar_sessions

        summaries = recall_similar_sessions(user_id, request.message, db=db)
        for conversation_id, summary in summaries:
            items.append(
                ContextItem(
                    layer=LAYER_L2,
                    scope=l2_scope,
                    source_type=SOURCE_SUMMARY,
                    content=summary,
                    provenance=f"L2:summary:conv_{conversation_id}" if conversation_id else "L2:summary:unknown",
                )
            )
        if decision.intent == "same_workspace_recall" and l2_scope == SCOPE_WORKSPACE:
            items.extend(_workspace_fact_items(user_id, db=db))
    return items


def _select_l2_scope(target_scopes: list[str]) -> str | None:
    scopes = set(target_scopes)
    if SCOPE_CROSS_WORKSPACE in scopes:
        return SCOPE_CROSS_WORKSPACE
    if SCOPE_WORKSPACE in scopes:
        return SCOPE_WORKSPACE
    return None


def _format_fact_content(fact: dict) -> str:
    fact_key = str(fact.get("fact_key") or "").strip()
    fact_value = str(fact.get("fact_value") or "").strip()
    entity_id = str(fact.get("entity_id") or "").strip()
    if not fact_key or not fact_value:
        return ""
    prefix = f"{entity_id}." if entity_id else ""
    return f"{prefix}{fact_key}: {fact_value}"


def _workspace_fact_items(user_id: str, *, db) -> list[ContextItem]:
    from app.services.agent.known_facts import get_facts_for_type

    items: list[ContextItem] = []
    for fact in get_facts_for_type(user_id, "workspace", db=db):
        confidence = float(fact.get("confidence") or 1.0)
        if confidence < MIN_FACT_CONFIDENCE:
            logger.debug(
                "context_l3_low_confidence_fact_skipped",
                extra={"fact_key": fact.get("fact_key")},
            )
            continue
        content = _format_fact_content(fact)
        if not content:
            continue
        items.append(
            ContextItem(
                layer=LAYER_L3,
                scope=SCOPE_WORKSPACE,
                source_type=SOURCE_FACT,
                content=content,
                confidence=confidence,
                provenance=f"L3:fact:{fact.get('entity_id')}:{fact.get('fact_key')}",
            )
        )
    return items


def context_tokens_breakdown(items: list[ContextItem]) -> dict[str, int]:
    """Group already-budgeted context items into the same conversation/facts
    split ContextTokenBudget uses, for Turn.context_tokens_json reporting.
    ("evidence" -- the research pipeline's own EvidencePack -- isn't part of
    this registry's L1-L3 items, so callers with a research evidence pack
    add that key separately.)"""
    conversation_tokens = sum(
        estimate_tokens(item.content) for item in items if item.layer in (LAYER_L1, LAYER_L2)
    )
    facts_tokens = sum(estimate_tokens(item.content) for item in items if item.layer == LAYER_L3)
    return {"conversation": conversation_tokens, "facts": facts_tokens}


def _apply_context_budget(
    items: list[ContextItem],
    *,
    request: TurnRequest,
    model: str | None = None,
) -> tuple[list[ContextItem], dict[str, int]]:
    """Budget-limit the assembled context set instead of returning it
    unbounded. Returns (kept_items, evicted_count_by_layer) -- the latter is
    what /admin/context-pressure reports on.

    L1 (prior-turn/attachment) is never evicted before L2/L3 -- it's the
    current turn's own conversation context, protected outright. L2
    (cross-session recall) and L1 share the "conversation" budget slice;
    L1's token cost is subtracted first so L2 only gets what's left. L2
    items are evicted in their existing similarity-rank assembly order --
    ContextItem has no timestamp field (and this task doesn't add one), so
    that's the closest available proxy for "more recent wins". L3 (known
    facts) evicts lowest-confidence items first.
    """
    if not items:
        return items, {}

    from app.services.agent.model_client import model_for_role

    resolved_model = (
        model
        or model_for_role(None, quality_mode=request.quality_mode, overrides=request.model_overrides)
        or "gpt-4.1-mini"
    )
    budget = ContextTokenBudget.for_model(resolved_model)

    l1_items = [item for item in items if item.layer == LAYER_L1]
    l2_items = [item for item in items if item.layer == LAYER_L2]
    l3_items = [item for item in items if item.layer == LAYER_L3]

    l1_tokens = sum(estimate_tokens(item.content) for item in l1_items)
    l2_budget = max(0, budget.conversation_tokens - l1_tokens)

    kept_l2, evicted_l2 = _evict_to_budget(l2_items, l2_budget, priority_key=lambda _item: 0)
    kept_l3, evicted_l3 = _evict_to_budget(l3_items, budget.facts_tokens, priority_key=lambda item: item.confidence)

    for item in evicted_l2 + evicted_l3:
        logger.debug(
            "context_budget_item_evicted",
            extra={
                "layer": item.layer,
                "scope": item.scope,
                "source_type": item.source_type,
                "provenance": item.provenance,
                "confidence": item.confidence,
                "estimated_tokens": estimate_tokens(item.content),
            },
        )

    evicted_counts = {}
    if evicted_l2:
        evicted_counts["conversation"] = len(evicted_l2)
    if evicted_l3:
        evicted_counts["facts"] = len(evicted_l3)

    return l1_items + kept_l2 + kept_l3, evicted_counts


def _evict_to_budget(
    items: list[ContextItem],
    token_budget: int,
    *,
    priority_key,
) -> tuple[list[ContextItem], list[ContextItem]]:
    """Keep items (highest priority_key first) until token_budget would be
    exceeded, then drop the rest. Index-based so ties preserve original
    relative order (stable sort) rather than relying on ContextItem equality."""
    if not items or token_budget <= 0:
        return [], list(items)
    order = sorted(range(len(items)), key=lambda i: priority_key(items[i]), reverse=True)
    keep_indices: set[int] = set()
    used = 0
    for i in order:
        cost = estimate_tokens(items[i].content)
        if used + cost > token_budget:
            break
        keep_indices.add(i)
        used += cost
    kept = [items[i] for i in range(len(items)) if i in keep_indices]
    evicted = [items[i] for i in range(len(items)) if i not in keep_indices]
    return kept, evicted
