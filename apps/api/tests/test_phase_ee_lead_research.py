from __future__ import annotations

from types import SimpleNamespace

from app.services.agent_v3.models import AgentV3Request, Source, ToolCall


def test_generate_research_brief_fallback(monkeypatch):
    from app.services.agent_v3 import model_client
    from app.services.agent_v3.research_subtree import generate_research_brief

    monkeypatch.setattr(model_client, "complete", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("timeout")))

    brief = generate_research_brief(AgentV3Request(message="Compare Tavily vs You.com", research_level="deep"))

    assert brief.objective
    assert brief.source == "heuristic"
    assert brief.fallback_reason is not None


def test_coverage_contract_fallback_has_cells(monkeypatch):
    from app.services.agent_v3 import model_client
    from app.services.agent_v3.research_subtree import ResearchBrief, generate_coverage_contract

    monkeypatch.setattr(model_client, "complete", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("fail")))

    contract = generate_coverage_contract(
        AgentV3Request(message="Compare Tavily vs You.com", research_level="deep"),
        ResearchBrief(
            objective="Compare Tavily and You.com",
            success_criteria=["pricing covered", "capabilities covered"],
            source="heuristic",
        ),
    )

    assert contract.cells
    assert contract.source == "heuristic"


def test_coverage_contract_ratio():
    from app.services.agent_v3.research_subtree import CoverageCell, CoverageContract

    contract = CoverageContract(
        subjects=["A", "B"],
        dimensions=["price", "features"],
        cells=[
            CoverageCell(dimension="price", subject="A", required=True, status="filled"),
            CoverageCell(dimension="price", subject="B", required=True, status="empty"),
            CoverageCell(dimension="features", subject="A", required=True, status="partial"),
            CoverageCell(dimension="features", subject="B", required=True, status="empty"),
        ],
    )

    assert contract.coverage_ratio() == 0.5
    assert len(contract.open_cells()) == 2
    assert len(contract.partial_cells()) == 1


def test_plan_from_contract_generates_workers():
    from app.services.agent_v3.research_subtree import CoverageCell, CoverageContract, plan_from_contract

    contract = CoverageContract(
        subjects=["Tavily", "Nimble"],
        dimensions=["pricing", "security"],
        cells=[
            CoverageCell(dimension="pricing", subject="Tavily"),
            CoverageCell(dimension="pricing", subject="Nimble"),
            CoverageCell(dimension="security", subject="Tavily"),
            CoverageCell(dimension="security", subject="Nimble"),
        ],
    )

    plan = plan_from_contract(
        AgentV3Request(message="Compare Tavily and Nimble", research_level="deep"),
        contract,
    )

    assert plan.workers
    assert any("Tavily" in worker.question for worker in plan.workers)
    assert any("Nimble" in worker.question for worker in plan.workers)


def test_reflect_sufficient_when_fully_covered(monkeypatch):
    from app.services.agent_v3 import model_client
    from app.services.agent_v3.research_subtree import (
        CoverageCell,
        CoverageContract,
        ResearchBrief,
        ResearchBudget,
        ResearchBudgetLedger,
        ResearchPlan,
        ResearchStateStore,
        reflect,
    )

    monkeypatch.setattr(model_client, "complete", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("no llm call")))
    state = ResearchStateStore(
        brief=ResearchBrief(objective="test", source="heuristic"),
        contract=CoverageContract(cells=[CoverageCell(dimension="x", subject="A", status="filled")]),
        plan=ResearchPlan(source="heuristic"),
        budget_ledger=ResearchBudgetLedger(budget=ResearchBudget()),
    )

    decision = reflect(AgentV3Request(message="test", research_level="deep"), state)

    assert decision.sufficient is True
    assert decision.coverage_ratio == 1.0
    assert decision.next_action == "publish"


def test_citation_verification_detects_hallucinated(monkeypatch):
    from app.services.agent_v3 import model_client
    from app.services.agent_v3.research_subtree import EvidenceItem, EvidencePack, verify_citations_semantically

    monkeypatch.setattr(model_client, "complete", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("skip llm")))
    evidence = EvidencePack(
        items=[
            EvidenceItem(
                source_id="S1",
                title="Tavily pricing",
                url="https://tavily.com/pricing",
                evidence="$30/month for 4000 credits",
            )
        ]
    )

    result = verify_citations_semantically("Tavily costs $30/month [S1]. Nimble costs $500 [S9].", evidence)

    assert "S9" in result.hallucinated_citations
    assert result.repair_needed is True


def test_lead_research_loop_returns_expected_shape(monkeypatch):
    from app.services.agent_v3 import model_client
    from app.services.agent_v3.model_client import ModelResponse
    from app.services.agent_v3.research_subtree import lead_research_loop

    responses = iter(
        [
            '{"objective": "Compare Tavily and Nimble", "scope_in": ["Tavily", "Nimble"], "success_criteria": ["pricing covered"], "output_type": "comparison"}',
            '{"subjects": ["Tavily", "Nimble"], "dimensions": ["pricing"], "cells": [{"dimension": "pricing", "subject": "Tavily", "required": true}, {"dimension": "pricing", "subject": "Nimble", "required": true}]}',
            '{"sufficient": true, "targeted_queries": [], "terminate_reason": "enough", "coverage_ratio": 0.5, "next_action": "stop_with_gaps"}',
            '{"verified_claims": 1, "unsupported_claims": [], "hallucinated_citations": [], "repair_needed": false}',
        ]
    )

    def fake_complete(messages, **kwargs):
        return ModelResponse(text=next(responses), model_used="test-model", latency_ms=10, cost_usd=0.001)

    monkeypatch.setattr(model_client, "complete", fake_complete)
    monkeypatch.setattr(
        model_client,
        "simple_completion",
        lambda *a, **kw: ModelResponse(
            text="Tavily pricing is public [S1]. Nimble pricing is not clearly public [S2].",
            model_used="test-model",
            latency_ms=10,
            cost_usd=0.001,
        ),
    )

    class FakeTools:
        def search_web(self, query, max_results=6):
            return [
                Source(title="Tavily pricing", url="https://tavily.com/pricing", snippet="$30/month"),
                Source(title="Nimble docs", url="https://docs.nimbleway.com/search", snippet="Nimble search API"),
            ], ToolCall(name="web_search", input={"query": query}, output={"provider": "FakeSearch"}, ok=True)

        def extract_urls(self, urls, max_chars_per_source=2500):
            return [
                Source(title="Page", url=url, content=f"{url} pricing and API details")
                for url in urls
            ], ToolCall(name="read_url", input={"urls": urls}, output={"source_count": len(urls)}, ok=True)

    result = lead_research_loop(
        AgentV3Request(message="Compare Tavily and Nimble pricing", research_level="deep"),
        FakeTools(),
        lambda stage, message, data: None,
    )

    assert set(result) == {"sources", "tool_calls", "evidence", "response", "plan", "feedback"}
    assert result["response"].text
    assert result["tool_calls"]


def test_runtime_routes_deep_to_lead_loop(monkeypatch):
    from app.services.agent_v3 import research_subtree
    from app.services.agent_v3.runtime import AgentV3Runtime

    called = {"loop": False}

    def fake_lead_loop(request, tools, progress):
        called["loop"] = True
        progress("complete", "deep complete", {})
        return {
            "sources": [],
            "tool_calls": [],
            "evidence": None,
            "response": SimpleNamespace(text="deep answer", model_used="fake", latency_ms=1, cost_usd=0.0),
            "plan": None,
            "feedback": None,
        }

    monkeypatch.setattr(research_subtree, "lead_research_loop", fake_lead_loop)

    result = list(
        AgentV3Runtime()._run_research_subtree(
            AgentV3Request(message="deep question", research_level="deep"),
            lambda stage, message, **data: SimpleNamespace(model_dump=lambda mode=None: {"stage": stage, "message": message, "data": data}),
        )
    )

    assert called["loop"] is True
    assert result[-1].data["stage"] == "complete"
