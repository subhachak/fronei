from __future__ import annotations

from app.services.agent.context_classifier import ContextDecision
from app.services.agent.context_contracts import (
    LAYER_L1,
    SCOPE_ATTACHMENT,
    SCOPE_CONVERSATION,
    SOURCE_ATTACHMENT,
    SOURCE_PRIOR_TURN,
    ContextItem,
)
from app.services.agent.models import TurnRequest


def get_context_items(request: TurnRequest, decision: ContextDecision) -> list[ContextItem]:
    if not decision.needs_context:
        return []
    if decision.intent == "vague_unresolved_followup":
        return []
    _SUPPORTED_SCOPES = frozenset({SCOPE_CONVERSATION, SCOPE_ATTACHMENT})
    unsupported_scopes = [s for s in decision.target_scopes if s not in _SUPPORTED_SCOPES]
    if unsupported_scopes:
        raise NotImplementedError(
            f"Context registry only supports {set(_SUPPORTED_SCOPES)} in this slice; "
            f"unsupported scopes: {unsupported_scopes} — wire L2/L3 in EPIC-03"
        )

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
    return items
