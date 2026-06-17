from __future__ import annotations

import time
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


def test_technical_architecture_profile_gets_specific_contract(monkeypatch):
    from app.services.agent_v3 import model_client
    from app.services.agent_v3.research_subtree import generate_coverage_contract, generate_research_brief

    monkeypatch.setattr(model_client, "complete", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("offline")))

    request = AgentV3Request(
        message="Conduct deep research and generate a detailed architectural report explaining system design, components, and workflows of agentic deep research AI.",
        research_level="deep",
    )
    brief = generate_research_brief(request)
    contract = generate_coverage_contract(request, brief)

    assert brief.research_profile == "technical_architecture"
    assert "Lead agent and orchestration" in contract.subjects
    assert "Guardrails and security controls" in contract.subjects
    assert "data model" in contract.dimensions
    assert len(contract.cells) >= 50


def test_technical_architecture_queries_are_provider_friendly():
    from app.services.agent_v3.research_subtree import (
        CoverageCell,
        CoverageContract,
        _targeted_query,
        _tech_arch_anchor_queries,
        plan_from_contract,
    )

    message = "Conduct deep research on agentic deep research AI system architecture."
    anchors = _tech_arch_anchor_queries(message)
    assert anchors
    assert all(" OR " not in query for query in anchors)

    query = _targeted_query("Evidence binder and citation map", ["data model"], message)
    assert "Evidence binder" not in query
    assert "citation verification" in query

    plan = plan_from_contract(
        AgentV3Request(message=message, research_level="deep"),
        CoverageContract(cells=[CoverageCell(subject="Evidence binder and citation map", dimension="data model")]),
    )
    assert plan.workers[0].discovery_domain == "academic"
    assert any(worker.rationale.startswith("Profile-level anchor") for worker in plan.workers)


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


def test_technical_architecture_ranking_prefers_dense_sources():
    from app.services.agent_v3.research_subtree import ResearchPlan, rank_sources

    plan = ResearchPlan(
        research_profile="technical_architecture",
        questions=["agentic deep research architecture implementation"],
    )
    sources = [
        Source(
            title="What is Agentic AI?",
            url="https://example.com/agentic-ai-overview",
            snippet="Agentic AI transforms passive LLMs into autonomous agents.",
        ),
        Source(
            title="Agent Research System README",
            url="https://github.com/example/agent-research-system",
            snippet="Architecture components workflow orchestrator evidence schema judge guardrails trace runtime.",
            content="Planner executor critic evidence binder coverage contract MCP tools retries queues budget ledger.",
        ),
    ]

    ranked = rank_sources(sources, plan)

    assert ranked[0].source.url.startswith("https://github.com")
    assert ranked[0].score > ranked[1].score


def test_evidence_preserves_source_provenance():
    from app.services.agent_v3.research_subtree import EvidencePack, ResearchPlan, bind_evidence

    evidence = bind_evidence(
        [
            Source(
                title="Agent runtime docs",
                url="https://github.com/example/agent-runtime",
                snippet="planner executor evidence guardrails runtime trace",
                query="agent runtime tracing budget ledger observability",
                provider="Tavily",
            )
        ],
        ResearchPlan(research_profile="technical_architecture", questions=["runtime"]),
    )

    assert isinstance(evidence, EvidencePack)
    assert evidence.items[0].query == "agent runtime tracing budget ledger observability"
    assert evidence.items[0].provider == "Tavily"


def test_bind_evidence_selects_relevant_passage_not_intro():
    from app.services.agent_v3.research_subtree import CoverageCell, CoverageContract, ResearchPlan, bind_evidence

    intro = " ".join(["This introductory section defines general artificial intelligence concepts."] * 35)
    relevant = (
        "The lead agent orchestrator owns the planning loop, dispatches search workers, "
        "tracks workflow state, records telemetry traces, manages budget ledger limits, "
        "and retries failed source readers through recovery policies."
    )
    evidence = bind_evidence(
        [
            Source(
                title="Agent runtime paper",
                url="https://arxiv.org/html/2506.18959v1",
                content=f"{intro}\n\n{relevant}",
            )
        ],
        ResearchPlan(
            research_profile="technical_architecture",
            questions=["lead agent orchestration workflow state budget telemetry"],
        ),
        contract=CoverageContract(
            cells=[
                CoverageCell(subject="Lead agent and orchestration", dimension="workflow"),
                CoverageCell(subject="Runtime durability, budget ledger, and observability", dimension="data model"),
            ]
        ),
        max_items=2,
    )

    combined = "\n".join(item.evidence for item in evidence.items)
    assert "lead agent orchestrator" in combined.lower()
    assert "budget ledger" in combined.lower()
    assert "general artificial intelligence concepts" not in evidence.items[0].evidence.lower()


def test_bind_evidence_creates_passage_level_items_for_technical_sources():
    from app.services.agent_v3.research_subtree import CoverageCell, CoverageContract, ResearchPlan, bind_evidence

    content = "\n\n".join(
        [
            "The search worker layer handles provider routing, query planning, source retrieval, and crawl scheduling.",
            "The evidence binder stores citation provenance, source identifiers, quoted passages, and coverage-cell mappings.",
            "The judge loop checks synthesis quality, detects gaps, requests repair, and enforces termination rules.",
        ]
    )
    evidence = bind_evidence(
        [Source(title="Research agent implementation", url="https://github.com/example/research-agent", content=content)],
        ResearchPlan(research_profile="technical_architecture", questions=["provider strategy evidence binder judge loop"]),
        contract=CoverageContract(
            cells=[
                CoverageCell(subject="Search workers and provider strategy", dimension="implementation pattern"),
                CoverageCell(subject="Evidence binder and citation map", dimension="data model"),
                CoverageCell(subject="Synthesis, judge, and quality gates", dimension="workflow"),
            ]
        ),
        max_items=3,
    )

    assert len(evidence.items) == 3
    assert all(item.supports_cells for item in evidence.items)
    assert "citation provenance" in "\n".join(item.evidence for item in evidence.items)


def test_bind_evidence_extracts_typed_technical_claims():
    from app.services.agent_v3.research_subtree import ResearchPlan, bind_evidence

    evidence = bind_evidence(
        [
            Source(
                title="Research runtime implementation",
                url="https://github.com/example/research-runtime",
                content=(
                    "The orchestrator stores workflow state in a durable runtime trace and dispatches search "
                    "workers through a bounded concurrency queue. The implementation records tool call latency, "
                    "provider choice, source provenance, and budget ledger decisions for every stage. "
                    "A failure recovery policy retries transient source-reader failures and stops when the "
                    "elapsed-time budget is exhausted."
                ),
            )
        ],
        ResearchPlan(
            research_profile="technical_architecture",
            questions=["orchestrator workflow state implementation budget ledger failure recovery"],
        ),
        max_items=3,
    )

    assert evidence.claims
    assert {claim.claim_type for claim in evidence.claims} & {"architecture", "implementation", "failure"}
    assert any(claim.claim_role in {"technical_design", "implementation_detail"} for claim in evidence.claims)
    assert all(claim.source_id.startswith("S") for claim in evidence.claims)


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

    assert set(result) == {"sources", "tool_calls", "evidence", "response", "plan", "worker_reports", "feedback"}
    assert result["response"].text
    assert result["tool_calls"]
    assert result["worker_reports"]


def test_technical_architecture_synthesis_uses_report_budget(monkeypatch):
    from app.services.agent_v3 import model_client
    from app.services.agent_v3.research_subtree import EvidenceItem, EvidencePack, ResearchPlan, synthesize_answer

    captured = {}

    def fake_simple_completion(system, user, *, max_tokens=1200):
        captured["system"] = system
        captured["user"] = user
        captured["max_tokens"] = max_tokens
        return model_client.ModelResponse(text="ok", model_used="fake", latency_ms=1, cost_usd=0.0)

    monkeypatch.setattr(model_client, "simple_completion", fake_simple_completion)
    request = AgentV3Request(
        message="Conduct deep research and generate a detailed architectural report explaining system design, components, and workflows of agentic deep research AI.",
        research_level="deep",
    )
    plan = ResearchPlan(
        research_profile="technical_architecture",
        questions=["architecture"],
    )
    evidence = EvidencePack(
        items=[
            EvidenceItem(
                source_id="S1",
                title="Agent architecture",
                url="https://github.com/example/agent-research",
                evidence="orchestrator planner workflow evidence schema guardrails runtime trace",
            )
        ],
        claims=[
            {
                "source_id": "S1",
                "text": "The implementation records runtime trace events for each worker and tool call.",
                "claim_type": "implementation",
                "claim_role": "implementation_detail",
                "confidence": 0.82,
            }
        ],
    )

    synthesize_answer(request, plan, evidence)

    assert captured["max_tokens"] >= 5000
    assert "Typed evidence claims" in captured["user"]
    assert "implementation/implementation_detail" in captured["user"]
    assert "Derive the section structure from the evidence" in captured["user"]
    assert "data models, control flow, state transitions" in captured["user"]
    assert "real architectural report" in captured["system"]


def test_lead_research_dispatches_search_workers_in_parallel(monkeypatch):
    from app.services.agent_v3.research_subtree import (
        CoverageContract,
        LeadResearchAgent,
        ResearchBrief,
        ResearchBudget,
        ResearchBudgetLedger,
        ResearchPlan,
        ResearchStateStore,
        SearchWorkerPlan,
    )

    class SlowSearchTools:
        def search_web(self, query, max_results=4):
            time.sleep(0.12)
            return [Source(title=query, url=f"https://example.com/{query}", snippet=query)], ToolCall(
                name="web_search",
                input={"query": query},
                output={"provider": "SlowFake"},
                ok=True,
            )

        def extract_urls(self, urls, max_chars_per_source=3500):
            return [Source(title=url, url=url, content=f"{url} implementation workflow trace budget") for url in urls], ToolCall(
                name="read_url",
                input={"urls": urls},
                output={"provider": "FakeExtract"},
                ok=True,
            )

    plan = ResearchPlan(
        research_profile="technical_architecture",
        workers=[
            SearchWorkerPlan(question="q1", query="alpha"),
            SearchWorkerPlan(question="q2", query="beta"),
            SearchWorkerPlan(question="q3", query="gamma"),
        ],
        max_sources=3,
    )
    state = ResearchStateStore(
        brief=ResearchBrief(objective="parallel test", source="heuristic"),
        contract=CoverageContract(cells=[]),
        plan=plan,
        budget_ledger=ResearchBudgetLedger(
            budget=ResearchBudget(max_search_workers=3, max_sources=3, max_tool_calls=6, max_deep_links=0)
        ),
    )
    agent = LeadResearchAgent(AgentV3Request(message="parallel test", research_level="deep"), SlowSearchTools())
    agent.ledger = state.budget_ledger

    started = time.monotonic()
    agent._dispatch_worker_wave(state)
    elapsed = time.monotonic() - started

    assert elapsed < 0.30
    assert len(state.all_tool_calls) >= 2
    assert len(state.all_sources) >= 3
    assert state.worker_reports
    assert all(report.claims for report in state.worker_reports)


def test_worker_reports_update_coverage_from_typed_claims():
    from app.services.agent_v3.research_subtree import (
        CoverageCell,
        CoverageContract,
        EvidenceClaim,
        ResearchBrief,
        ResearchBudget,
        ResearchBudgetLedger,
        ResearchPlan,
        ResearchStateStore,
        SearchWorkerReport,
        update_contract_from_evidence,
    )

    cell = CoverageCell(subject="Evidence binder and citation map", dimension="data model")
    state = ResearchStateStore(
        brief=ResearchBrief(objective="coverage test", source="heuristic"),
        contract=CoverageContract(cells=[cell]),
        plan=ResearchPlan(source="heuristic"),
        budget_ledger=ResearchBudgetLedger(budget=ResearchBudget()),
        worker_reports=[
            SearchWorkerReport(
                worker_id="worker-1",
                question="How is evidence modeled?",
                query="citation evidence schema",
                assigned_subject="Evidence binder and citation map",
                assigned_dimension="data model",
                claims=[
                    EvidenceClaim(
                        source_id="S1",
                        text="The evidence binder stores citation provenance and source identifiers in a schema.",
                        claim_type="implementation",
                        claim_role="implementation_detail",
                        confidence=0.8,
                    )
                ],
                self_assessed_confidence=0.72,
            )
        ],
    )

    update_contract_from_evidence(state)

    assert state.contract.cells[0].status == "partial"
    assert state.contract.cells[0].evidence_ids == ["S1"]
    assert "typed claim" in state.contract.cells[0].notes


def test_state_add_sources_upgrades_candidate_with_read_content():
    from app.services.agent_v3.research_subtree import (
        CoverageContract,
        ResearchBrief,
        ResearchPlan,
        ResearchStateStore,
    )

    state = ResearchStateStore(
        brief=ResearchBrief(objective="source merge test", source="heuristic"),
        contract=CoverageContract(),
        plan=ResearchPlan(source="heuristic"),
    )

    state.add_sources([Source(title="Snippet title", url="https://example.com/source", snippet="Short snippet")])
    state.add_sources(
        [
            Source(
                title="Full page title",
                url="https://example.com/source",
                content="Full page implementation detail with architecture, workflow, trace, and evidence.",
                provider="Reader",
            )
        ]
    )

    assert len(state.all_sources) == 1
    assert state.all_sources[0].title == "Full page title"
    assert "Full page implementation detail" in state.all_sources[0].content
    assert state.all_sources[0].provider == "Reader"


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
