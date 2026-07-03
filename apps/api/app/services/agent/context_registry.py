from __future__ import annotations

import logging

from app.services.agent.context_classifier import ContextDecision
from app.services.agent.context_contracts import (
    LAYER_L2,
    LAYER_L1,
    SCOPE_ATTACHMENT,
    SCOPE_CONVERSATION,
    SCOPE_CROSS_WORKSPACE,
    SCOPE_WORKSPACE,
    SOURCE_ATTACHMENT,
    SOURCE_PRIOR_TURN,
    SOURCE_SUMMARY,
    ContextItem,
)
from app.services.agent.models import TurnRequest

logger = logging.getLogger(__name__)


def get_context_items(request: TurnRequest, decision: ContextDecision, *, db=None) -> list[ContextItem]:
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
                provenance="TurnRequest.prior_turn_context",
            )
        )
    if SCOPE_ATTACHMENT in decision.target_scopes and request.attachment_context:
        items.append(
            ContextItem(
                layer=LAYER_L1,
                scope=SCOPE_ATTACHMENT,
                source_type=SOURCE_ATTACHMENT,
                content=request.attachment_context,
                provenance="TurnRequest.attachment_context",
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
        for summary in summaries:
            items.append(
                ContextItem(
                    layer=LAYER_L2,
                    scope=l2_scope,
                    source_type=SOURCE_SUMMARY,
                    content=summary,
                    provenance="session_summaries",
                )
            )
    return items


def _select_l2_scope(target_scopes: list[str]) -> str | None:
    scopes = set(target_scopes)
    if SCOPE_CROSS_WORKSPACE in scopes:
        return SCOPE_CROSS_WORKSPACE
    if SCOPE_WORKSPACE in scopes:
        return SCOPE_WORKSPACE
    return None
