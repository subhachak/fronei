from __future__ import annotations

import pytest

from app.services.agent.context_classifier import ContextDecision
from app.services.agent.context_contracts import (
    LAYER_L1,
    SCOPE_ATTACHMENT,
    SCOPE_CONVERSATION,
    SOURCE_ATTACHMENT,
    SOURCE_PRIOR_TURN,
)
from app.services.agent.context_registry import get_context_items
from app.services.agent.models import TurnRequest


def test_vague_unresolved_followup_returns_no_items():
    decision = ContextDecision(
        intent="vague_unresolved_followup",
        needs_context=True,
        target_scopes=[],
        reason="test",
    )
    items = get_context_items(TurnRequest(message="What did we decide?"), decision)
    assert items == []


def test_same_conversation_followup_wraps_prior_turn_context():
    request = TurnRequest(
        message="Can you summarize that?",
        prior_turn_context="User: discuss PgBouncer\nAssistant: It pools connections.",
    )
    decision = ContextDecision(
        intent="same_conversation_followup",
        needs_context=True,
        target_scopes=[SCOPE_CONVERSATION],
        reason="test",
    )

    items = get_context_items(request, decision)

    assert len(items) == 1
    item = items[0]
    assert item.layer == LAYER_L1
    assert item.scope == SCOPE_CONVERSATION
    assert item.source_type == SOURCE_PRIOR_TURN
    assert item.content == request.prior_turn_context


def test_attachment_context_wraps_attachment_context():
    request = TurnRequest(
        message="Summarize the attachment.",
        attachment_context="Attached PDF text...",
    )
    decision = ContextDecision(
        intent="attachment_context",
        needs_context=True,
        target_scopes=[SCOPE_ATTACHMENT],
        reason="test",
    )

    items = get_context_items(request, decision)

    assert len(items) == 1
    item = items[0]
    assert item.layer == LAYER_L1
    assert item.scope == SCOPE_ATTACHMENT
    assert item.source_type == SOURCE_ATTACHMENT
    assert item.content == request.attachment_context


def test_needs_context_false_returns_no_items_even_when_fields_are_populated():
    request = TurnRequest(
        message="Explain caching.",
        prior_turn_context="User: prior\nAssistant: prior",
        attachment_context="Attached content",
    )
    decision = ContextDecision(
        intent="standalone",
        needs_context=False,
        target_scopes=[SCOPE_CONVERSATION, SCOPE_ATTACHMENT],
        reason="test",
    )

    assert get_context_items(request, decision) == []


def test_unsupported_scope_raises_not_implemented():
    # workspace and cross_workspace are L2/L3 — not yet wired; must raise so
    # EPIC-03 is forced to implement before these paths go live.
    from app.services.agent.context_contracts import SCOPE_WORKSPACE
    decision = ContextDecision(
        intent="same_workspace_recall",
        needs_context=True,
        target_scopes=[SCOPE_WORKSPACE],
        reason="test",
    )

    with pytest.raises(NotImplementedError, match="EPIC-03"):
        get_context_items(TurnRequest(message="Use the project facts."), decision)
