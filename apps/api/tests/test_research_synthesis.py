from app.services.agent.models import TurnRequest
from app.services.agent.research_models import EvidencePack, ResearchPlan
from app.services.agent.research_synthesis import build_synthesis_prompt


def test_chat_research_synthesis_contract_is_not_report_mode():
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

    assert "Produce a concise chat answer, not a report or artifact." in user_prompt
    assert "Prefer a short ranked list or compact bullets over large tables." in user_prompt
