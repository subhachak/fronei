from __future__ import annotations

from app.services.agent.context_classifier import ContextDecision
from app.services.agent.context_contracts import (
    LAYER_L1,
    LAYER_L2,
    LAYER_L3,
    SCOPE_ATTACHMENT,
    SCOPE_CONVERSATION,
    SCOPE_CROSS_WORKSPACE,
    SCOPE_WORKSPACE,
    SOURCE_ATTACHMENT,
    SOURCE_FACT,
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


def test_l3_facts_pinned_appear_before_auto_extracted(monkeypatch):
    """Pinned facts (source_conversation_id=None) come first in context items."""
    class RequestWithUser(TurnRequest):
        user_id: str = "user_1"

    monkeypatch.setattr("app.services.agent.session_memory.recall_similar_sessions", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        "app.services.agent.known_facts.get_facts_for_type",
        lambda *_args, **_kwargs: [
            {
                "id": "fact_pinned",
                "entity_id": "workspace_1",
                "entity_type": "workspace",
                "fact_key": "priority",
                "fact_value": "Pinned value",
                "confidence": 1.0,
                "source_conversation_id": None,
                "created_at": None,
                "updated_at": None,
            },
            {
                "id": "fact_auto",
                "entity_id": "workspace_1",
                "entity_type": "workspace",
                "fact_key": "status",
                "fact_value": "Auto-extracted value",
                "confidence": 0.9,
                "source_conversation_id": "conv_1",
                "created_at": None,
                "updated_at": None,
            },
        ],
    )
    decision = ContextDecision(
        intent="same_workspace_recall",
        needs_context=True,
        target_scopes=[SCOPE_WORKSPACE],
        reason="test",
    )

    items = get_context_items(RequestWithUser(message="Use the project facts."), decision, db="db")

    assert [item.content for item in items] == [
        "workspace_1.priority: Pinned value",
        "workspace_1.status: Auto-extracted value",
    ]
    assert all(item.layer == LAYER_L3 for item in items)
    assert all(item.source_type == SOURCE_FACT for item in items)


def test_l3_facts_are_included_with_l2_summaries(monkeypatch):
    """Workspace facts should not be suppressed just because L2 summaries exist."""
    class RequestWithUser(TurnRequest):
        user_id: str = "user_1"

    monkeypatch.setattr(
        "app.services.agent.session_memory.recall_similar_sessions",
        lambda *_args, **_kwargs: [("conv_1", "Prior workspace summary")],
    )
    monkeypatch.setattr(
        "app.services.agent.known_facts.get_facts_for_type",
        lambda *_args, **_kwargs: [
            {
                "id": "fact_pinned",
                "entity_id": "h2-test",
                "entity_type": "workspace",
                "fact_key": "preferred-language",
                "fact_value": "Rust",
                "confidence": 1.0,
                "source_conversation_id": None,
                "created_at": None,
                "updated_at": None,
            },
        ],
    )
    decision = ContextDecision(
        intent="same_workspace_recall",
        needs_context=True,
        target_scopes=[SCOPE_WORKSPACE],
        reason="test",
    )

    items = get_context_items(RequestWithUser(message="Use the workspace facts."), decision, db="db")

    assert [item.layer for item in items] == [LAYER_L2, LAYER_L3]
    assert [item.source_type for item in items] == [SOURCE_SUMMARY, SOURCE_FACT]
    assert items[1].content == "h2-test.preferred-language: Rust"


def test_l3_low_confidence_facts_excluded(monkeypatch):
    """Facts below 0.5 confidence are not included in context items."""
    class RequestWithUser(TurnRequest):
        user_id: str = "user_1"

    monkeypatch.setattr("app.services.agent.session_memory.recall_similar_sessions", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        "app.services.agent.known_facts.get_facts_for_type",
        lambda *_args, **_kwargs: [
            {
                "id": "fact_low",
                "entity_id": "workspace_1",
                "entity_type": "workspace",
                "fact_key": "low",
                "fact_value": "Excluded",
                "confidence": 0.3,
                "source_conversation_id": None,
                "created_at": None,
                "updated_at": None,
            },
            {
                "id": "fact_boundary",
                "entity_id": "workspace_1",
                "entity_type": "workspace",
                "fact_key": "boundary",
                "fact_value": "Included",
                "confidence": 0.5,
                "source_conversation_id": None,
                "created_at": None,
                "updated_at": None,
            },
        ],
    )
    decision = ContextDecision(
        intent="same_workspace_recall",
        needs_context=True,
        target_scopes=[SCOPE_WORKSPACE],
        reason="test",
    )

    items = get_context_items(RequestWithUser(message="Use the project facts."), decision, db="db")

    assert len(items) == 1
    assert items[0].content == "workspace_1.boundary: Included"
    assert items[0].confidence == 0.5
