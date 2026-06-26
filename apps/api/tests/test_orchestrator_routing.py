import json
from types import SimpleNamespace

from app.services.agent.models import TurnRequest
from app.services.agent.orchestrator import decide_with_options, heuristic_decide


GITHUB_PPT_RESEARCH_PROMPT = (
    "Look in GitHub repos to see if there are recent projects to generate ppts "
    "from a brief and preconfigured templates"
)


def test_ppt_generator_repo_lookup_routes_to_chat_research_in_heuristic():
    decision = heuristic_decide(TurnRequest(message=GITHUB_PPT_RESEARCH_PROMPT, output_format="chat"))

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
