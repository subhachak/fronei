from app.services.agent.models import TurnRequest
from app.services.agent.research_models import EvidencePack, ResearchPlan
from app.services.agent.research_synthesis import build_synthesis_prompt, _synthesis_token_budget


def test_chat_research_synthesis_contract_is_elaborative_by_default():
    _, user_prompt = build_synthesis_prompt(
        TurnRequest(
            message=(
                "Look in GitHub repos to see if there are recent open-source projects "
                "that generate PPTX slide decks from a short brief and preconfigured templates."
            ),
            output_format="chat",
        ),
        ResearchPlan(research_profile="general", questions=["Which repos fit?"]),
        EvidencePack(),
    )

    assert "Produce an elaborative, source-grounded chat answer by default" in user_prompt
    assert "enough detail that the answer can stand alone" in user_prompt
    assert "Only be brief when the user explicitly asks for brevity." in user_prompt
    assert "Produce a concise chat answer" not in user_prompt


def test_chat_research_synthesis_goes_brief_when_user_asks():
    _, user_prompt = build_synthesis_prompt(
        TurnRequest(
            message="Briefly check recent open-source PPTX generation repos and give me the short version.",
            output_format="chat",
        ),
        ResearchPlan(research_profile="general", questions=["Which repos fit?"]),
        EvidencePack(),
    )

    assert "Produce a concise chat answer, not a report or artifact." in user_prompt
    assert "Prefer a short ranked list or compact bullets over large tables." in user_prompt


def test_chat_research_budget_is_elaborative_by_default():
    request = TurnRequest(
        message="Look for recent open-source projects that generate PPTX slide decks from short briefs.",
        output_format="chat",
    )
    plan = ResearchPlan(research_profile="general", questions=["Which repos fit?"])

    assert _synthesis_token_budget(request, plan) >= 4000


def test_chat_research_budget_stays_small_when_user_asks_for_brief():
    request = TurnRequest(
        message="Briefly check recent open-source PPTX generation repos.",
        output_format="chat",
    )
    plan = ResearchPlan(research_profile="general", questions=["Which repos fit?"])

    assert _synthesis_token_budget(request, plan) <= 1800


def test_framework_comparison_chat_gets_decision_grade_contract():
    _, user_prompt = build_synthesis_prompt(
        TurnRequest(
            message=(
                "Research the top 5 agentic AI frameworks in 2025: LangGraph, CrewAI, "
                "AutoGen, Haystack, and LlamaIndex Workflows. Provide for each: architecture model, "
                "multi-agent coordination approach, production readiness, and known failure modes. "
                "Then synthesize a recommendation for the best framework for an enterprise orchestration "
                "layer and explain why."
            ),
            output_format="chat",
        ),
        ResearchPlan(research_profile="technical_architecture", questions=["Compare frameworks"]),
        EvidencePack(),
    )

    assert "Produce a decision-grade research answer in chat" in user_prompt
    assert "architecture model, coordination approach, production readiness, known failure modes" in user_prompt
    assert "cross-cutting failure taxonomy or governance lens" in user_prompt
    assert "lifecycle, maintenance, successor-framework, or ecosystem shifts" in user_prompt
    assert "ranked recommendation and conditional overrides" in user_prompt
    assert "Do not open with an evidence-quality disclaimer" in user_prompt
    assert "Produce a concise chat answer" not in user_prompt


def test_framework_comparison_chat_gets_room_to_answer():
    request = TurnRequest(
        message=(
            "Research the top 5 agentic AI frameworks in 2025: LangGraph, CrewAI, "
            "AutoGen, Haystack, and LlamaIndex Workflows. Provide for each: architecture model, "
            "multi-agent coordination approach, production readiness, and known failure modes. "
            "Then synthesize a recommendation for the best framework for an enterprise orchestration layer."
        ),
        output_format="chat",
    )
    plan = ResearchPlan(research_profile="technical_architecture", questions=["Compare frameworks"])

    assert _synthesis_token_budget(request, plan) >= 8000
