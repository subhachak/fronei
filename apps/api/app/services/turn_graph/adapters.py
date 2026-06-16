from __future__ import annotations

import json
from typing import Any

from app.db.models import Conversation, ConversationTurn
from app.services.turn_graph.state import TurnGraphState


def state_from_turn(
    *,
    conversation: Conversation | None,
    turn: ConversationTurn | None,
    user_message: str,
    history: list[dict[str, Any]] | None = None,
    user_memory: str = "",
    profile: str | None = None,
) -> TurnGraphState:
    """Build graph state from today's conversation/turn records.

    This is intentionally a small adapter rather than direct router wiring. It
    gives the future graph runtime a stable state shape while the existing chat
    pipeline remains the source of truth during shadow-mode rollout.
    """

    active_task: dict[str, Any] | None = None
    if conversation and conversation.active_task_json:
        try:
            parsed = json.loads(conversation.active_task_json)
            active_task = parsed if isinstance(parsed, dict) else None
        except (TypeError, ValueError):
            active_task = None

    user_id = getattr(turn, "user_id", None) or getattr(conversation, "user_id", None)

    return TurnGraphState(
        conversation_id=getattr(conversation, "public_id", None),
        turn_id=getattr(turn, "public_id", None),
        user_id=user_id,
        tenant_id=user_id,
        user_message=user_message,
        profile=profile or getattr(conversation, "profile", None) or "balanced",
        history=history or [],
        user_memory=user_memory or "",
        running_summary=getattr(conversation, "running_summary", None) or "",
        active_task=active_task,
    )


def graph_trace_payload(state: TurnGraphState) -> dict[str, Any]:
    """Return a JSON-serializable graph trace for lifecycle/admin views."""

    return {
        "status": state.status,
        "error": state.error,
        "selected_tools": state.selected_tools,
        "accepted_tools": state.accepted_tools,
        "declined_tools": state.declined_tools,
        "triage_decision": state.triage_decision,
        "gate": state.gate,
        "events": [
            {
                "node": event.node,
                "event": event.event,
                "message": event.message,
                "ts": event.ts.isoformat(),
                "data": event.data,
            }
            for event in state.events
        ],
        "node_timings": [
            {
                "node": timing.node,
                "status": timing.status,
                "latency_ms": timing.latency_ms,
                "meta": timing.meta,
                "started_at": timing.started_at.isoformat() if timing.started_at else None,
                "completed_at": timing.completed_at.isoformat() if timing.completed_at else None,
            }
            for timing in state.node_timings
        ],
    }
