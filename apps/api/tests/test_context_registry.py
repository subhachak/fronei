from __future__ import annotations

from app.services.agent.context_classifier import ContextDecision
from app.services.agent.context_contracts import (
    LAYER_L1,
    LAYER_L2,
    SCOPE_ATTACHMENT,
    SCOPE_CONVERSATION,
    SCOPE_CROSS_WORKSPACE,
    SCOPE_WORKSPACE,
    SOURCE_ATTACHMENT,
    SOURCE_PRIOR_TURN,
    SOURCE_SUMMARY,
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
    assert item.provenance == "L1:prior_turn:conv_unknown"


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
    assert item.provenance == "L1:attachment:uploaded"


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


def test_workspace_scope_without_db_returns_no_items():
    decision = ContextDecision(
        intent="same_workspace_recall",
        needs_context=True,
        target_scopes=[SCOPE_WORKSPACE],
        reason="test",
    )

    assert get_context_items(TurnRequest(message="Use the project facts."), decision, db=None) == []


def test_workspace_scope_with_db_wraps_l2_summaries(monkeypatch):
    class RequestWithUser(TurnRequest):
        user_id: str = "user_1"

    def fake_recall(user_id, query, *, db=None, limit=3):
        assert user_id == "user_1"
        assert db == "db"
        return [("conv_1", "Prior session summary")]

    monkeypatch.setattr("app.services.agent.session_memory.recall_similar_sessions", fake_recall)
    decision = ContextDecision(
        intent="same_workspace_recall",
        needs_context=True,
        target_scopes=[SCOPE_WORKSPACE],
        reason="test",
    )

    items = get_context_items(RequestWithUser(message="Use the project facts."), decision, db="db")

    assert len(items) == 1
    assert items[0].layer == LAYER_L2
    assert items[0].scope == SCOPE_WORKSPACE
    assert items[0].source_type == SOURCE_SUMMARY
    assert items[0].content == "Prior session summary"
    assert items[0].provenance == "L2:summary:conv_conv_1"


def test_workspace_and_cross_workspace_scopes_do_not_duplicate_l2_summaries(monkeypatch):
    class RequestWithUser(TurnRequest):
        user_id: str = "user_1"

    monkeypatch.setattr(
        "app.services.agent.session_memory.recall_similar_sessions",
        lambda *_args, **_kwargs: [("conv_1", "Prior session summary")],
    )
    decision = ContextDecision(
        intent="explicit_cross_workspace_recall",
        needs_context=True,
        target_scopes=[SCOPE_WORKSPACE, SCOPE_CROSS_WORKSPACE],
        reason="test",
    )

    items = get_context_items(RequestWithUser(message="Use all known facts."), decision, db="db")

    assert len(items) == 1
    assert items[0].scope == SCOPE_CROSS_WORKSPACE
