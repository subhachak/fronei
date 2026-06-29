import json
from types import SimpleNamespace

from app.services.agent.models import TurnRequest
from app.services.agent.orchestrator import decide_with_options, heuristic_decide


GITHUB_PPT_RESEARCH_PROMPT = (
    "Look in GitHub repos to see if there are recent projects to generate ppts "
    "from a brief and preconfigured templates"
)

SEARCH_GITHUB_PPT_RESEARCH_PROMPT = (
    "Search GitHub for recent open-source projects that generate PPTX slide decks from short briefs "
    "and preconfigured templates. Summarize in chat with a concise comparison including project name, "
    "GitHub link, generated outputs, template support, maturity signals, and which project looks most promising."
)


def test_ppt_generator_repo_lookup_routes_to_chat_research_in_heuristic():
    decision = heuristic_decide(TurnRequest(message=GITHUB_PPT_RESEARCH_PROMPT, output_format="chat"))

    assert decision.route == "research"
    assert decision.output_format == "chat"


def test_search_github_ppt_generator_lookup_routes_to_chat_research_in_heuristic():
    decision = heuristic_decide(TurnRequest(message=SEARCH_GITHUB_PPT_RESEARCH_PROMPT, output_format="chat"))

    assert decision.route == "research"
    assert decision.output_format == "chat"


def test_ppt_generator_repo_lookup_normalizes_llm_document_route(monkeypatch):
    from app.services.agent import model_client

    def fake_complete(messages, **_kwargs):
        return SimpleNamespace(
            text=json.dumps(
                {
                    "route": "research_document",
                    "confidence": 0.9,
                    "reason": "Mistakenly treated PPT generators as a PPT deliverable.",
                    "output_format": "pptx",
                    "research_level": "regular",
                    "requires_confirmation": False,
                }
            ),
            model_used="fake-orchestrator",
            latency_ms=1,
            cost_usd=0.0,
        )

    monkeypatch.setattr(model_client, "complete", fake_complete)

    decision = decide_with_options(
        TurnRequest(message=GITHUB_PPT_RESEARCH_PROMPT, output_format="chat"),
        available_routes=["direct", "clarify", "research", "document", "research_document"],
        available_tools=[],
    )

    assert decision.route == "research"
    assert decision.output_format == "chat"


def test_search_github_ppt_generator_lookup_normalizes_llm_document_route(monkeypatch):
    from app.services.agent import model_client

    def fake_complete(messages, **_kwargs):
        return SimpleNamespace(
            text=json.dumps(
                {
                    "route": "research_document",
                    "confidence": 0.9,
                    "reason": "Mistakenly treated PPT generators as a PPT deliverable.",
                    "output_format": "pptx",
                    "research_level": "regular",
                    "requires_confirmation": False,
                }
            ),
            model_used="fake-orchestrator",
            latency_ms=1,
            cost_usd=0.0,
        )

    monkeypatch.setattr(model_client, "complete", fake_complete)

    decision = decide_with_options(
        TurnRequest(message=SEARCH_GITHUB_PPT_RESEARCH_PROMPT, output_format="chat"),
        available_routes=["direct", "clarify", "research", "document", "research_document"],
        available_tools=[],
    )

    assert decision.route == "research"
    assert decision.output_format == "chat"


def test_medical_supplement_safety_normalizes_llm_direct_route(monkeypatch):
    from app.services.agent import model_client

    def fake_complete(messages, **_kwargs):
        return SimpleNamespace(
            text=json.dumps(
                {
                    "route": "direct",
                    "confidence": 0.95,
                    "reason": "Well-established clinical knowledge.",
                    "output_format": "chat",
                    "research_level": "regular",
                    "requires_confirmation": False,
                }
            ),
            model_used="fake-orchestrator",
            latency_ms=1,
            cost_usd=0.0,
        )

    monkeypatch.setattr(model_client, "complete", fake_complete)

    decision = decide_with_options(
        TurnRequest(message="Is long-term creatine supplementation safe for kidney health?"),
        available_routes=["direct", "clarify", "research", "document", "research_document"],
        available_tools=[],
    )

    assert decision.route == "research"
    assert decision.research_level == "regular"
    assert decision.requires_confirmation is False
    assert "Routing signals" in decision.reason


def test_workplace_policy_evidence_normalizes_llm_direct_route(monkeypatch):
    from app.services.agent import model_client

    def fake_complete(messages, **_kwargs):
        return SimpleNamespace(
            text=json.dumps(
                {
                    "route": "direct",
                    "confidence": 0.9,
                    "reason": "Well-established workplace policy pattern.",
                    "output_format": "chat",
                    "research_level": "regular",
                    "requires_confirmation": False,
                }
            ),
            model_used="fake-orchestrator",
            latency_ms=1,
            cost_usd=0.0,
        )

    monkeypatch.setattr(model_client, "complete", fake_complete)

    decision = decide_with_options(
        TurnRequest(
            message=(
                "Give a concise overview for a mid-size manufacturing company considering a four-day work week, "
                "based on evidence about productivity and retention."
            )
        ),
        available_routes=["direct", "clarify", "research", "document", "research_document"],
        available_tools=[],
    )

    assert decision.route == "research"
    assert decision.research_level == "regular"
    assert decision.requires_confirmation is False
    assert "Routing signals" in decision.reason


def test_medical_supplement_safety_heuristic_routes_to_research():
    decision = heuristic_decide(
        TurnRequest(message="Is long-term creatine supplementation safe for kidney health?")
    )

    assert decision.route == "research"
    assert decision.research_level == "regular"


# ---------------------------------------------------------------------------
# Phase 13a — time_sensitive_factual signal group routing
# ---------------------------------------------------------------------------

def test_cardiology_wait_time_heuristic_routes_to_research():
    """Phase 13a anchor case: 'in practice' + 'how long does' must route to research."""
    decision = heuristic_decide(
        TurnRequest(message="How long does a cardiology referral actually take in practice?")
    )

    assert decision.route == "research", (
        f"Expected research, got {decision.route!r}. "
        "The time_sensitive_factual signal group must promote this to research."
    )


def test_passport_processing_wait_time_heuristic_routes_to_research():
    """Phase 13a: 'currently taking' must route to research."""
    decision = heuristic_decide(
        TurnRequest(message="How long is passport processing currently taking?")
    )

    assert decision.route == "research", (
        f"Expected research, got {decision.route!r}. "
        "The time_sensitive_factual signal group must promote this to research."
    )


def test_small_claims_wait_time_heuristic_routes_to_research():
    """Phase 13a: 'wait time' must route to research."""
    decision = heuristic_decide(
        TurnRequest(message="What is the typical wait time to get a small claims court hearing scheduled?")
    )

    assert decision.route == "research", (
        f"Expected research, got {decision.route!r}. "
        "The time_sensitive_factual signal group must promote this to research."
    )


def test_time_sensitive_signal_group_is_registered():
    """Phase 13a: time_sensitive_factual group must exist in BOOTSTRAP_SIGNAL_GROUPS."""
    from app.services.agent.routing_policy import BOOTSTRAP_SIGNAL_GROUPS

    ids = {g.id for g in BOOTSTRAP_SIGNAL_GROUPS}
    assert "time_sensitive_factual" in ids, (
        "BOOTSTRAP_SIGNAL_GROUPS must contain a 'time_sensitive_factual' group (Phase 13a)."
    )


def test_golden_set_has_phase13a_cases():
    """Phase 13a — research_golden_set.json must contain at least 3 time_sensitive_factual entries."""
    import json
    import os

    golden_path = os.path.join(
        os.path.dirname(__file__), "..", "evals", "research_golden_set.json"
    )
    with open(golden_path) as f:
        cases = json.load(f)

    phase13a = [c for c in cases if c.get("category") == "time_sensitive_factual_routing"]
    assert len(phase13a) >= 3, (
        f"Golden set must include at least 3 Phase 13a time_sensitive_factual_routing cases; found {len(phase13a)}."
    )
    ids = {c["id"] for c in phase13a}
    assert "cardiology_referral_wait_time" in ids, "Must include cardiology anchor case."
