from __future__ import annotations

import logging
from typing import Any

from app.observability import log_event
from app.services.agent.models import TurnRequest


CONTEXT_CLAIM_PHRASES: tuple[str, ...] = (
    "from context",
    "context contains",
    "conversation context",
    "current conversation context",
    "prior context",
    "prior conversation",
    "prior turn",
    "previous turn",
    "already answered",
    "context has",
    "context provides",
    "based on context",
    "what we discussed",
)


def prompt_token_estimate(text: str) -> int:
    return max(1, len(text or "") // 4)


def prior_turn_grounded(request: TurnRequest) -> bool:
    """Return True only when the turn has evidence of prior conversation.

    `conversation_context` can contain attachment text, workspace background,
    profile/preferences, or user-pinned facts. Those are useful context, but
    they are not prior-turn grounding. `last_turn_route` is the strongest
    server-populated signal. The textual fallback exists for deterministic
    eval fixtures and older tests that pass formatted prior turns directly.
    """
    if request.last_turn_route:
        return True
    prior_context = (getattr(request, "prior_turn_context", "") or "").strip()
    if prior_context:
        return True
    context = (request.conversation_context or "").strip()
    if not context:
        return False
    lowered = context.lower()
    if lowered.startswith("attached file context:"):
        return False
    if "workspace background only" in lowered:
        return False
    prior_markers = (
        "current conversation context",
        "current conversation topic",
        "recent turns",
        "user:",
        "assistant:",
        "previous turn",
        "prior turn",
        "running summary",
    )
    return any(marker in lowered for marker in prior_markers)


def reason_claims_context(reason: str) -> bool:
    lowered = (reason or "").lower()
    return any(phrase in lowered for phrase in CONTEXT_CLAIM_PHRASES)


def raw_message_count(request: TurnRequest) -> int:
    return 2 if prior_turn_grounded(request) else 1


def state_preview(request: TurnRequest) -> dict[str, Any]:
    context = request.conversation_context or ""
    return {
        "conversation_id": request.conversation_id,
        "raw_message_count": raw_message_count(request),
        "raw_messages_preview": [
            {
                "role": "user",
                "content_len": len(request.message or ""),
                "content_head": (request.message or "")[:120],
            }
        ],
        "context_keys_present": [
            key
            for key, value in {
                "conversation_id": request.conversation_id,
                "conversation_context": request.conversation_context,
                "prior_turn_context": getattr(request, "prior_turn_context", ""),
                "attachment_context": request.attachment_context,
                "last_turn_route": request.last_turn_route,
                "quality_mode": request.quality_mode,
                "research_level": request.research_level,
                "output_format": request.output_format,
            }.items()
            if value not in (None, "", {}, [])
        ],
        "conversation_context_len": len(context),
        "grounded": prior_turn_grounded(request),
        "last_turn_route": request.last_turn_route,
    }


def log_router_pre_decision(
    logger: logging.Logger,
    *,
    request: TurnRequest,
    prompt: str,
    router_name: str,
    level: int = logging.DEBUG,
    **fields: Any,
) -> None:
    log_event(
        logger,
        level,
        "router_pre_decision_state",
        router_name=router_name,
        prompt_token_estimate=prompt_token_estimate(prompt),
        **state_preview(request),
        **fields,
    )


def log_context_entry_state(
    logger: logging.Logger,
    *,
    request: TurnRequest | None,
    entry_point: str,
    level: int = logging.DEBUG,
    **fields: Any,
) -> None:
    if request is None:
        log_event(logger, level, "context_entry_state", entry_point=entry_point, **fields)
        return
    log_event(
        logger,
        level,
        "context_entry_state",
        entry_point=entry_point,
        **state_preview(request),
        **fields,
    )


def log_grounding_check(
    logger: logging.Logger,
    *,
    request: TurnRequest,
    router_name: str,
    decision: str,
    reason: str,
    level: int | None = None,
    **fields: Any,
) -> bool:
    grounded = prior_turn_grounded(request)
    fabricated = not grounded and reason_claims_context(reason)
    log_event(
        logger,
        logging.WARNING if fabricated else (level or logging.DEBUG),
        "router_decision_grounding_check",
        router_name=router_name,
        conversation_id=request.conversation_id,
        decision=decision,
        reason_given=reason,
        state_message_count_at_decision=raw_message_count(request),
        grounded=grounded,
        fabricated_context_claim=fabricated,
        **fields,
    )
    return fabricated
