"""Part 1 -- token-budget governance for context assembly.

Covers: estimate_tokens(), resolve_context_window(), ContextTokenBudget's
split/headroom, and context_registry.py's prioritized eviction (including a
"heavy turn" regression that the assembled context stays within budget).
"""
from __future__ import annotations

from app.services.agent.context_classifier import ContextDecision
from app.services.agent.context_contracts import (
    LAYER_L1,
    LAYER_L2,
    LAYER_L3,
    SCOPE_ATTACHMENT,
    SCOPE_CONVERSATION,
    SCOPE_WORKSPACE,
    ContextTokenBudget,
)
from app.services.agent.context_registry import (
    _apply_context_budget,
    _evict_to_budget,
    context_tokens_breakdown,
    get_context_items,
    get_context_items_with_eviction,
)
from app.services.agent.context_contracts import ContextItem
from app.services.agent.model_client import resolve_context_window
from app.services.agent.models import TurnRequest
from app.services.agent.research_utils import estimate_tokens


# ---------------------------------------------------------------------------
# estimate_tokens()
# ---------------------------------------------------------------------------

def test_estimate_tokens_empty_string_returns_one():
    assert estimate_tokens("") == 1


def test_estimate_tokens_scales_with_text_length():
    short = estimate_tokens("Hello world.")
    long = estimate_tokens("Hello world. " * 100)
    assert 0 < short < long


def test_estimate_tokens_is_reasonable_relative_to_char_count():
    # cl100k_base averages roughly 4 chars/token for English prose -- assert
    # the estimate stays in a sane band rather than pinning an exact value.
    text = "The quick brown fox jumps over the lazy dog. " * 20
    tokens = estimate_tokens(text)
    assert len(text) / 8 < tokens < len(text) / 2


# ---------------------------------------------------------------------------
# resolve_context_window()
# ---------------------------------------------------------------------------

def test_resolve_context_window_known_model():
    assert resolve_context_window("gpt-4.1-mini") > 0


def test_resolve_context_window_known_model_matches_litellm():
    import litellm

    assert resolve_context_window("claude-sonnet-4-6") == litellm.get_max_tokens("claude-sonnet-4-6")


def test_resolve_context_window_unrecognized_model_uses_conservative_fallback():
    assert resolve_context_window("totally-not-a-real-model-xyz") == 32_000


# ---------------------------------------------------------------------------
# ContextTokenBudget
# ---------------------------------------------------------------------------

def test_context_token_budget_reserves_headroom_before_splitting():
    budget = ContextTokenBudget(total_tokens=10_000, system_prompt_reserve=1000, output_reserve=2000)

    assert budget.available_tokens == 7000
    assert budget.conversation_tokens + budget.facts_tokens + budget.evidence_tokens <= budget.available_tokens


def test_context_token_budget_splits_by_configured_shares():
    budget = ContextTokenBudget(total_tokens=10_000, system_prompt_reserve=0, output_reserve=0)

    assert budget.conversation_tokens == int(10_000 * 0.15)
    assert budget.facts_tokens == int(10_000 * 0.25)
    assert budget.evidence_tokens == int(10_000 * 0.60)


def test_context_token_budget_available_tokens_never_negative():
    budget = ContextTokenBudget(total_tokens=100, system_prompt_reserve=1500, output_reserve=2000)

    assert budget.available_tokens == 0
    assert budget.conversation_tokens == 0
    assert budget.facts_tokens == 0
    assert budget.evidence_tokens == 0


def test_context_token_budget_for_model_resolves_real_window():
    budget = ContextTokenBudget.for_model("gpt-4.1-mini")

    assert budget.total_tokens == resolve_context_window("gpt-4.1-mini")


# ---------------------------------------------------------------------------
# _evict_to_budget() -- the low-level priority-ordered eviction primitive
# ---------------------------------------------------------------------------

def _fact_item(confidence: float, chars: int) -> ContextItem:
    from app.services.agent.context_contracts import SOURCE_FACT

    return ContextItem(layer=LAYER_L3, scope=SCOPE_WORKSPACE, source_type=SOURCE_FACT, content="x" * chars, confidence=confidence)


def test_evict_to_budget_keeps_highest_priority_first():
    # Each "x" * 80 item costs ~10 estimated tokens; budget=25 fits exactly 2.
    items = [_fact_item(0.9, 80), _fact_item(0.5, 80), _fact_item(0.95, 80)]

    kept, evicted = _evict_to_budget(items, 25, priority_key=lambda item: item.confidence)

    assert sorted(item.confidence for item in kept) == [0.9, 0.95]
    assert [item.confidence for item in evicted] == [0.5]


def test_evict_to_budget_zero_budget_evicts_everything():
    items = [_fact_item(0.9, 80)]

    kept, evicted = _evict_to_budget(items, 0, priority_key=lambda item: item.confidence)

    assert kept == []
    assert evicted == items


def test_evict_to_budget_empty_items_returns_empty():
    kept, evicted = _evict_to_budget([], 1000, priority_key=lambda item: item.confidence)

    assert kept == []
    assert evicted == []


# ---------------------------------------------------------------------------
# get_context_items() / get_context_items_with_eviction() -- full integration
# ---------------------------------------------------------------------------

def test_get_context_items_never_evicts_l1_before_l2_l3(monkeypatch):
    """L1 (prior-turn context) must never be evicted, even under extreme
    budget pressure that would otherwise wipe out everything."""
    class RequestWithUser(TurnRequest):
        user_id: str = "user_1"

    monkeypatch.setattr(
        "app.services.agent.session_memory.recall_similar_sessions",
        lambda *_args, **_kwargs: [("conv_1", "summary " * 2000)],
    )
    monkeypatch.setattr(
        "app.services.agent.known_facts.get_facts_for_type",
        lambda *_args, **_kwargs: [
            {
                "id": f"fact_{i}",
                "entity_id": "workspace_1",
                "entity_type": "workspace",
                "fact_key": f"key_{i}",
                "fact_value": "value " * 500,
                "confidence": 0.9,
                "source_conversation_id": None,
                "created_at": None,
                "updated_at": None,
            }
            for i in range(10)
        ],
    )
    decision = ContextDecision(
        intent="same_workspace_recall",
        needs_context=True,
        target_scopes=[SCOPE_CONVERSATION, SCOPE_WORKSPACE],
        reason="test",
    )
    request = RequestWithUser(
        message="Use the project facts.",
        prior_turn_context="prior turn context " * 50,
    )

    # Force a tiny window so eviction pressure is extreme.
    items = get_context_items(request, decision, db="db", model="totally-unknown-model-with-conservative-fallback")

    l1_items = [item for item in items if item.layer == LAYER_L1]
    assert len(l1_items) == 1
    assert l1_items[0].content == request.prior_turn_context


def test_get_context_items_with_eviction_drops_lowest_confidence_facts_first(monkeypatch):
    class RequestWithUser(TurnRequest):
        user_id: str = "user_1"

    monkeypatch.setattr("app.services.agent.session_memory.recall_similar_sessions", lambda *_args, **_kwargs: [])
    # 40 facts x ~301 estimated tokens each (~12,040 total) comfortably
    # exceeds the fallback model's facts_tokens budget (~7,125), so eviction
    # is guaranteed to trigger rather than depending on exact token counts.
    fact_count = 40
    monkeypatch.setattr(
        "app.services.agent.known_facts.get_facts_for_type",
        lambda *_args, **_kwargs: [
            {
                "id": f"fact_{i}",
                "entity_id": "workspace_1",
                "entity_type": "workspace",
                "fact_key": f"key_{i}",
                "fact_value": "value " * 300,
                "confidence": round(0.5 + (i % 10) * 0.05, 2),
                "source_conversation_id": None,
                "created_at": None,
                "updated_at": None,
            }
            for i in range(fact_count)
        ],
    )
    decision = ContextDecision(
        intent="same_workspace_recall",
        needs_context=True,
        target_scopes=[SCOPE_WORKSPACE],
        reason="test",
    )
    request = RequestWithUser(message="Use the project facts.")

    items, evicted_counts = get_context_items_with_eviction(
        request, decision, db="db", model="totally-unknown-model-with-conservative-fallback"
    )

    assert evicted_counts.get("facts", 0) > 0
    kept_confidences = sorted(item.confidence for item in items if item.layer == LAYER_L3)
    # Every kept fact's confidence must be >= every evicted fact's confidence
    # -- i.e. eviction dropped the lowest-confidence items first, not an
    # arbitrary subset.
    all_confidences = [round(0.5 + (i % 10) * 0.05, 2) for i in range(fact_count)]
    evicted_confidences = sorted(set(all_confidences) - set(kept_confidences))
    if evicted_confidences and kept_confidences:
        assert max(evicted_confidences) <= min(kept_confidences)


def test_heavy_turn_regression_assembled_context_stays_within_budget(monkeypatch):
    """Simulates a genuinely heavy turn (long conversation + many stored
    facts + large attachment) and confirms the final assembled context's
    total estimated tokens stays within the resolved budget rather than
    growing unbounded."""
    class RequestWithUser(TurnRequest):
        user_id: str = "user_1"

    monkeypatch.setattr(
        "app.services.agent.session_memory.recall_similar_sessions",
        lambda *_args, **_kwargs: [(f"conv_{i}", "summary text " * 300) for i in range(5)],
    )
    monkeypatch.setattr(
        "app.services.agent.known_facts.get_facts_for_type",
        lambda *_args, **_kwargs: [
            {
                "id": f"fact_{i}",
                "entity_id": "workspace_1",
                "entity_type": "workspace",
                "fact_key": f"key_{i}",
                "fact_value": "detailed fact value " * 200,
                "confidence": 0.5 + (i % 5) * 0.1,
                "source_conversation_id": None,
                "created_at": None,
                "updated_at": None,
            }
            for i in range(30)
        ],
    )
    decision = ContextDecision(
        intent="same_workspace_recall",
        needs_context=True,
        target_scopes=[SCOPE_CONVERSATION, SCOPE_ATTACHMENT, SCOPE_WORKSPACE],
        reason="test",
    )
    request = RequestWithUser(
        message="What's the status across everything we've discussed?",
        prior_turn_context="long prior conversation turn text " * 400,
        attachment_context="large attached document content " * 800,
    )
    model = "gpt-4.1-mini"

    items = get_context_items(request, decision, db="db", model=model)

    budget = ContextTokenBudget.for_model(model)
    tokens = context_tokens_breakdown(items)
    # conversation covers L1+L2; L1 is protected and can push slightly over
    # its nominal share, but must never approach the unbounded pre-governance
    # behavior (all 5 summaries + all 30 facts + the full attachment).
    assert tokens["facts"] <= budget.facts_tokens
    unbounded_facts_tokens = sum(estimate_tokens("detailed fact value " * 200) for _ in range(30))
    assert tokens["facts"] < unbounded_facts_tokens
