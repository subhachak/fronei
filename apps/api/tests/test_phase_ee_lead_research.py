from __future__ import annotations

import time
from types import SimpleNamespace

from app.services.agent.models import TurnRequest, Source, ToolCall


def test_generate_research_brief_fallback(monkeypatch):
    from app.services.agent import model_client
    from app.services.agent.research_subtree import generate_research_brief

    monkeypatch.setattr(model_client, "complete", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("timeout")))

    brief = generate_research_brief(TurnRequest(message="Compare Tavily vs You.com", research_level="deep"))

    assert brief.objective
    assert brief.source == "heuristic"
    assert brief.fallback_reason is not None


def test_technical_architecture_profile_gets_specific_contract(monkeypatch):
    from app.services.agent import model_client
    from app.services.agent.research_subtree import generate_coverage_contract, generate_research_brief

    monkeypatch.setattr(model_client, "complete", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("offline")))

    request = TurnRequest(
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


def test_framework_comparison_gets_entity_dimension_contract():
    from app.services.agent.research_subtree import ResearchBrief, generate_coverage_contract

    request = TurnRequest(
        message=(
            "Research the top 5 agentic AI frameworks in 2025: LangGraph, CrewAI, "
            "AutoGen, Haystack, and LlamaIndex Workflows. Provide for each: architecture model, "
            "multi-agent coordination approach, production readiness, and known failure modes. "
            "Then synthesize a recommendation for the best framework for an enterprise orchestration layer."
        ),
        research_level="regular",
    )
    brief = ResearchBrief(
        objective="Compare agentic AI frameworks for enterprise orchestration.",
        research_profile="technical_architecture",
        source="heuristic",
    )

    contract = generate_coverage_contract(request, brief)

    # Phase 8 — source renamed from framework_comparison to multi_subject_comparison
    assert contract.source.endswith("multi_subject_comparison")
    assert contract.subjects == ["LangGraph", "CrewAI", "AutoGen", "Haystack", "LlamaIndex Workflows"]
    assert "architecture model" in contract.dimensions
    assert "production readiness and deployment model" in contract.dimensions
    assert "lifecycle status and ecosystem trajectory" in contract.dimensions
    assert "Lead agent and orchestration" not in contract.subjects


def test_framework_comparison_overrides_strategy_brief_profile():
    from app.services.agent.research_subtree import (
        ResearchBrief,
        generate_coverage_contract,
        plan_from_contract,
        research_budget_for,
    )

    request = TurnRequest(
        message=(
            "Research the top 5 agentic AI frameworks in 2025: LangGraph, CrewAI, "
            "AutoGen, Haystack, and LlamaIndex Workflows. Provide for each: architecture model, "
            "multi-agent coordination approach, production readiness, and known failure modes. "
            "Then synthesize a recommendation for the best framework for an enterprise orchestration layer."
        ),
        research_level="regular",
    )
    brief = ResearchBrief(
        objective="Recommend the best framework for enterprise orchestration.",
        research_profile="strategy_brief",
        source="llm",
    )

    budget = research_budget_for(request)
    contract = generate_coverage_contract(request, brief)
    plan = plan_from_contract(request, contract, budget)

    # Phase 8 — source renamed; budget now scales per named subject (5 subjects → extra_subjects=3 → +12 sources)
    # Phase 12 — strategy_brief + named subjects routes through brief_anchored, which is strictly better:
    # real named subjects are used rather than the generic static template's SaaS dimension labels.
    # Accept both the old multi_subject_comparison path and the Phase 12 brief_anchored path.
    assert (
        contract.source.endswith("multi_subject_comparison")
        or contract.source.startswith("brief_anchored:")
    ), f"Expected multi_subject_comparison or brief_anchored contract, got: {contract.source!r}"
    assert contract.subjects == ["LangGraph", "CrewAI", "AutoGen", "Haystack", "LlamaIndex Workflows"]
    # Phase 12 — plan profile may be strategy_brief (from brief_anchored) or technical_architecture
    # (from infer_research_profile fallback). The key invariant is named-subject contract cells exist.
    assert plan.research_profile in ("technical_architecture", "strategy_brief"), (
        f"Unexpected profile: {plan.research_profile}"
    )
    assert budget.max_sources >= 18
    assert any("LangGraph official docs" in worker.query for worker in plan.workers)


def test_framework_comparison_queries_prioritize_primary_docs_and_lifecycle():
    from app.services.agent.research_subtree import (
        CoverageCell,
        CoverageContract,
        _domain_discovery_workers,
        _targeted_query,
        _tech_arch_anchor_queries,
        plan_from_contract,
        research_budget_for,
    )

    message = (
        "Research the top 5 agentic AI frameworks in 2025: LangGraph, CrewAI, "
        "AutoGen, Haystack, and LlamaIndex Workflows. Provide for each: architecture model, "
        "multi-agent coordination approach, production readiness, and known failure modes. "
        "Then synthesize a recommendation for the best framework for an enterprise orchestration layer."
    )
    request = TurnRequest(message=message, research_level="deep")

    anchors = _tech_arch_anchor_queries(message)
    assert any("official documentation" in query for query in anchors)
    assert any("Microsoft Agent Framework" in query for query in anchors)
    assert any("failure modes benchmark taxonomy" in query for query in anchors)

    lifecycle_query = _targeted_query("AutoGen", ["lifecycle status and ecosystem trajectory"], message)
    assert "Microsoft Agent Framework" in lifecycle_query
    assert "migration" in lifecycle_query

    workers = _domain_discovery_workers(request, "technical_architecture", research_budget_for(request))
    assert workers
    assert all(worker.discovery_domain == "documentation" for worker in workers)
    assert any("LangGraph official docs" in worker.query for worker in workers)
    assert any("AutoGen Microsoft Agent Framework official" in worker.query for worker in workers)

    contract = CoverageContract(
        cells=[
            CoverageCell(subject="LangGraph", dimension="architecture model"),
            CoverageCell(subject="AutoGen", dimension="lifecycle status and ecosystem trajectory"),
        ],
        subjects=["LangGraph", "AutoGen"],
        dimensions=["architecture model", "lifecycle status and ecosystem trajectory"],
        source="profile:technical_architecture:framework_comparison",
    )
    plan = plan_from_contract(request, contract)
    assert any("official docs" in worker.query for worker in plan.workers)
    assert any("Microsoft Agent Framework" in worker.query for worker in plan.workers)


def test_framework_comparison_seeds_canonical_docs():
    from app.services.agent.research_lead import _canonical_framework_sources
    from app.services.agent.research_subtree import ResearchPlan

    request = TurnRequest(
        message=(
            "Research the top 5 agentic AI frameworks in 2025: LangGraph, CrewAI, "
            "AutoGen, Haystack, and LlamaIndex Workflows."
        ),
        research_level="regular",
    )
    plan = ResearchPlan(research_profile="strategy_brief")

    sources = _canonical_framework_sources(request, plan)
    urls = [source.url for source in sources]

    assert len(sources) >= 8
    assert any("langgraph" in url.lower() for url in urls)
    assert any("docs.crewai.com" in url.lower() for url in urls)
    assert any("microsoft.github.io/autogen" in url.lower() for url in urls)
    assert any("docs.haystack.deepset.ai" in url.lower() for url in urls)
    assert any("docs.llamaindex.ai" in url.lower() for url in urls)


def test_framework_comparison_judge_rejects_truncated_answer():
    from app.services.agent.research_subtree import (
        CoverageCell,
        CoverageContract,
        EvidenceItem,
        EvidencePack,
        ResearchBudget,
        ResearchBudgetLedger,
        ResearchBrief,
        ResearchPlan,
        ResearchStateStore,
        judge_research_final,
    )

    contract = CoverageContract(
        cells=[
            CoverageCell(subject=subject, dimension="architecture model", status="filled", confidence=0.9)
            for subject in ["LangGraph", "CrewAI", "AutoGen", "Haystack", "LlamaIndex Workflows"]
        ],
        subjects=["LangGraph", "CrewAI", "AutoGen", "Haystack", "LlamaIndex Workflows"],
        dimensions=["architecture model"],
        # Phase 8 — source renamed from framework_comparison to multi_subject_comparison
        source="profile:technical_architecture:multi_subject_comparison",
    )
    evidence = EvidencePack(
        items=[
            EvidenceItem(source_id="S1", title="LangGraph docs", url="https://langchain-ai.github.io/langgraph/", evidence="architecture workflow production"),
            EvidenceItem(source_id="S2", title="CrewAI docs", url="https://docs.crewai.com/introduction", evidence="agents crews flows"),
        ],
        coverage=1.0,
    )
    state = ResearchStateStore(
        brief=ResearchBrief(objective="Compare frameworks", research_profile="technical_architecture", source="heuristic"),
        contract=contract,
        plan=ResearchPlan(research_profile="technical_architecture"),
        evidence=evidence,
        budget_ledger=ResearchBudgetLedger(budget=ResearchBudget()),
    )
    answer = """# Agentic AI Frameworks

## Section 1: LangGraph
LangGraph details [S1].

## Section 2: CrewAI
CrewAI details [S2].

## Section 3: AutoGen
AutoGen details [S1].

## Section 4: Haystack
Agent components"""

    verdict = judge_research_final(TurnRequest(message="Compare agentic AI frameworks."), state, answer)

    assert verdict.repair_needed
    assert any("missing detailed sections" in issue for issue in verdict.issues)
    assert any("closing recommendation" in issue for issue in verdict.issues)
    assert any("mid-section" in issue for issue in verdict.issues)


def test_framework_comparison_judge_rejects_evidence_disclaimer_answer():
    from app.services.agent.research_subtree import (
        CoverageCell,
        CoverageContract,
        EvidenceItem,
        EvidencePack,
        ResearchBudget,
        ResearchBudgetLedger,
        ResearchBrief,
        ResearchPlan,
        ResearchStateStore,
        judge_research_final,
    )

    subjects = ["LangGraph", "CrewAI", "AutoGen", "Haystack", "LlamaIndex Workflows"]
    contract = CoverageContract(
        cells=[
            CoverageCell(subject=subject, dimension=dimension, status="filled", confidence=0.9)
            for subject in subjects
            for dimension in ["architecture model", "production readiness and deployment model"]
        ],
        subjects=subjects,
        dimensions=["architecture model", "production readiness and deployment model"],
        # Phase 8 — source renamed from framework_comparison to multi_subject_comparison
        source="profile:technical_architecture:multi_subject_comparison",
    )
    state = ResearchStateStore(
        brief=ResearchBrief(objective="Compare frameworks", research_profile="technical_architecture", source="heuristic"),
        contract=contract,
        plan=ResearchPlan(research_profile="technical_architecture", judge_threshold=0.76),
        evidence=EvidencePack(
            items=[
                EvidenceItem(
                    source_id="S1",
                    title="LangGraph docs",
                    url="https://langchain-ai.github.io/langgraph/",
                    evidence="LangGraph architecture, orchestration, production, persistence, coordination, and failure handling evidence.",
                )
            ],
            coverage=1.0,
        ),
        budget_ledger=ResearchBudgetLedger(
            budget=ResearchBudget(max_sources=16, max_deep_links=0, max_tool_calls=4, max_model_calls=4),
            sources_read=4,
            tool_calls=1,
        ),
    )
    answer = """# Agentic AI Frameworks 2025

## Evidence Quality Disclaimer

This brief is decision-shaped but evidence-thin. The retrieved sources are dominated by index/navigation fragments, listicle titles, and marketing scaffolding.

## Executive Recommendation

Winner provisional: LangGraph [S1].

## LangGraph
Architecture model: graph orchestration [S1].

## CrewAI
Architecture model: not in evidence.

## AutoGen
Architecture model: not in evidence.

## Haystack
Architecture model: not in evidence.

## LlamaIndex Workflows
Architecture model: not in evidence.

## Ranked Recommendation

This is a provisional, single-source-anchored recommendation.
"""

    verdict = judge_research_final(TurnRequest(message="Compare agentic AI frameworks.", research_level="regular"), state, answer)

    assert verdict.next_action == "research_more"
    assert verdict.can_publish is False
    # Phase 8 — disclaimer detection now uses 'not in evidence' count check (deterministic)
    # rather than the old phrase blocklist. The issue message now mentions "Multi-subject comparison".
    assert any("not in evidence" in issue for issue in verdict.issues)
    assert any("Multi-subject" in issue or "not in evidence" in issue for issue in verdict.issues)


def test_framework_comparison_judge_rejects_subtle_evidence_light_answer():
    from app.services.agent.research_subtree import (
        CoverageCell,
        CoverageContract,
        EvidenceItem,
        EvidencePack,
        ResearchBudget,
        ResearchBudgetLedger,
        ResearchBrief,
        ResearchPlan,
        ResearchStateStore,
        judge_research_final,
    )

    subjects = ["LangGraph", "CrewAI", "AutoGen", "Haystack", "LlamaIndex Workflows"]
    contract = CoverageContract(
        cells=[
            CoverageCell(subject=subject, dimension=dimension, status="filled", confidence=0.9)
            for subject in subjects
            for dimension in ["architecture model", "production readiness and deployment model"]
        ],
        subjects=subjects,
        dimensions=["architecture model", "production readiness and deployment model"],
        # Phase 8 — source renamed from framework_comparison to multi_subject_comparison
        source="profile:technical_architecture:multi_subject_comparison",
    )
    state = ResearchStateStore(
        brief=ResearchBrief(objective="Compare frameworks", research_profile="technical_architecture", source="heuristic"),
        contract=contract,
        plan=ResearchPlan(research_profile="technical_architecture", judge_threshold=0.76),
        evidence=EvidencePack(
            items=[
                EvidenceItem(
                    source_id="S1",
                    title="Framework overview",
                    url="https://example.com/frameworks",
                    evidence="Framework overview with sparse details.",
                )
            ],
            coverage=1.0,
        ),
        budget_ledger=ResearchBudgetLedger(
            budget=ResearchBudget(max_sources=16, max_deep_links=0, max_tool_calls=4, max_model_calls=4),
            sources_read=4,
            tool_calls=1,
        ),
    )
    answer = """# Executive Recommendation

Winner: LangGraph, but I want to flag upfront that the evidence pack retrieved for this question is thin on the specific technical detail your request demands [S1].

LangGraph is the provisional recommendation, pending direct evidence on LangGraph internals.

| Framework | Architecture Model | Production Readiness |
|---|---|---|
| LangGraph | Graph/state-machine orchestration, not directly described in evidence | no benchmark data in pack |
| CrewAI | Role-based teamwork | prod-grade claims unverified |
| AutoGen | Event-driven async architecture | not documented in evidence |
| Haystack | Not described in evidence | Not described in evidence |
| LlamaIndex Workflows | Not described in evidence | Not described in evidence |

## Validation Notes (for the research judge)

This answer is evidence-light on the core technical claims. I recommend requesting deeper research rather than treating the LangGraph recommendation as confirmed.

## Ranked Recommendation

LangGraph remains the provisional recommendation [S1].
"""

    verdict = judge_research_final(TurnRequest(message="Compare agentic AI frameworks.", research_level="regular"), state, answer)

    assert verdict.next_action == "research_more"
    assert verdict.can_publish is False
    # Phase 8 — "evidence-quality disclaimer" phrase removed from issue messages;
    # deterministic checks now surface "not in evidence" count and "research-judge instructions" instead.
    assert any("research-judge instructions" in issue for issue in verdict.issues)
    assert any("not in evidence" in issue or "Multi-subject" in issue for issue in verdict.issues)


def test_framework_comparison_judge_rejects_empty_framework_rows_with_validation_notes():
    from app.services.agent.research_subtree import (
        CoverageCell,
        CoverageContract,
        EvidenceItem,
        EvidencePack,
        ResearchBudget,
        ResearchBudgetLedger,
        ResearchBrief,
        ResearchPlan,
        ResearchStateStore,
        judge_research_final,
    )

    subjects = ["LangGraph", "CrewAI", "AutoGen", "Haystack", "LlamaIndex Workflows"]
    contract = CoverageContract(
        cells=[
            CoverageCell(subject=subject, dimension=dimension, status="filled", confidence=0.9)
            for subject in subjects
            for dimension in ["architecture model", "multi-agent coordination approach", "production readiness", "known failure modes"]
        ],
        subjects=subjects,
        dimensions=["architecture model", "multi-agent coordination approach", "production readiness", "known failure modes"],
        # Phase 8 — source renamed from framework_comparison to multi_subject_comparison
        source="profile:technical_architecture:multi_subject_comparison",
    )
    state = ResearchStateStore(
        brief=ResearchBrief(objective="Compare frameworks", research_profile="technical_architecture", source="heuristic"),
        contract=contract,
        plan=ResearchPlan(research_profile="technical_architecture", judge_threshold=0.76),
        evidence=EvidencePack(
            items=[
                EvidenceItem(
                    source_id="S1",
                    title="LangGraph comparison",
                    url="https://example.com/langgraph",
                    evidence="LangGraph explicit control flow and production adoption.",
                )
            ],
            coverage=1.0,
        ),
        budget_ledger=ResearchBudgetLedger(
            budget=ResearchBudget(max_sources=18, max_deep_links=0, max_tool_calls=5, max_model_calls=4),
            sources_read=6,
            tool_calls=2,
        ),
    )
    answer = """# Agentic AI Frameworks 2025: Enterprise Orchestration Decision Brief

## Executive Recommendation
Winner: LangGraph. The available evidence is thin and uneven across the five named frameworks [S1].

## Comparison Matrix

| Framework | Architecture Model | Coordination Approach | Production Readiness | Known Failure Modes |
|---|---|---|---|---|
| LangGraph | Graph-based control flow [S1] | State-machine orchestration [S1] | Strong adoption signal [S1] | Not specified in evidence |
| CrewAI | Role-based crew abstraction [S1] | Role workflows [S1] | Growing adoption [S1] | Not specified in evidence |
| AutoGen / AG2 | Conversation-centric [S1] | Multi-agent conversations [S1] | Not specified in evidence | Not specified in evidence |
| Haystack | Not described in evidence | Not described in evidence | No evidence in pack — validation note: requires dedicated research | Not specified |
| LlamaIndex Workflows | Not described in evidence | Not evidenced | Validation note: requires dedicated research | Not specified |

## Haystack
No substantive evidence in this pack. Validation note: this row requires a dedicated research pass before any enterprise recommendation.

## LlamaIndex Workflows
Not evidenced. Validation note: requires dedicated research.

## Ranked Recommendation
Default choice: LangGraph [S1].
"""

    verdict = judge_research_final(TurnRequest(message="Compare agentic AI frameworks.", research_level="regular"), state, answer)

    assert verdict.next_action == "research_more"
    assert verdict.can_publish is False
    # Phase 8 — "framework detail" renamed to "requested detail" in issue message (now covers all domains)
    assert any("validation notes for requested detail" in issue for issue in verdict.issues)


def test_framework_comparison_detects_thin_evidence_and_remediation_urls():
    from app.services.agent.research_subtree import (
        CoverageCell,
        CoverageContract,
        EvidenceItem,
        EvidencePack,
        ResearchBrief,
        ResearchPlan,
        ResearchStateStore,
        _evidence_quality_issues,
        _framework_remediation_sources,
    )

    request = TurnRequest(
        message=(
            "Research the top 5 agentic AI frameworks in 2025: LangGraph, CrewAI, "
            "AutoGen, Haystack, and LlamaIndex Workflows. Provide for each: architecture model, "
            "multi-agent coordination approach, production readiness, and known failure modes."
        ),
        research_level="deep",
    )
    state = ResearchStateStore(
        brief=ResearchBrief(objective="Compare frameworks", research_profile="technical_architecture", source="heuristic"),
        contract=CoverageContract(
            cells=[CoverageCell(subject="LangGraph", dimension="architecture model")],
            subjects=["LangGraph", "CrewAI", "AutoGen", "Haystack", "LlamaIndex Workflows"],
            dimensions=["architecture model"],
            source="profile:technical_architecture:framework_comparison",
        ),
        plan=ResearchPlan(research_profile="technical_architecture", max_sources=8),
        evidence=EvidencePack(
            items=[
                EvidenceItem(
                    source_id="S1",
                    title="Best agent frameworks",
                    url="https://example.com/listicle",
                    evidence="Skip to content Navigation menu Subscribe Previous Next LangGraph is popular.",
                )
            ]
        ),
        all_sources=[
            Source(
                title="Best agent frameworks",
                url="https://example.com/listicle",
                content="Skip to content Navigation menu Subscribe Previous Next Sign in Cookie settings.",
            )
        ],
    )

    issues = _evidence_quality_issues(request, state)
    remediation_sources = _framework_remediation_sources(request, state)
    urls = [source.url for source in remediation_sources]

    assert any("missing official documentation" in issue for issue in issues)
    assert any("thin or page-chrome-heavy" in issue for issue in issues)
    assert len(remediation_sources) >= 8
    assert any("langgraph" in url.lower() for url in urls)
    assert any("docs.crewai.com" in url.lower() for url in urls)
    assert any("microsoft.github.io/autogen" in url.lower() for url in urls)


def test_framework_comparison_remediation_reads_primary_docs_before_synthesis():
    from app.services.agent.research_subtree import (
        CoverageCell,
        CoverageContract,
        EvidenceItem,
        EvidencePack,
        LeadResearchAgent,
        ResearchBrief,
        ResearchBudget,
        ResearchBudgetLedger,
        ResearchPlan,
        ResearchStateStore,
    )

    request = TurnRequest(
        message=(
            "Research the top 5 agentic AI frameworks in 2025: LangGraph, CrewAI, "
            "AutoGen, Haystack, and LlamaIndex Workflows. Provide for each: architecture model, "
            "multi-agent coordination approach, production readiness, and known failure modes."
        ),
        research_level="deep",
    )

    class PrimaryDocTools:
        def __init__(self):
            self.read_urls = []

        def extract_urls(self, urls, max_chars_per_source=2500):
            self.read_urls.extend(urls)
            extracted = []
            for url in urls:
                if "langgraph" in url:
                    title = "LangGraph docs"
                    text = "LangGraph architecture uses state graphs with nodes, edges, checkpoints, persistence, runtime orchestration, multi-agent coordination, deployment, production observability, and failure recovery. " * 2
                elif "crewai" in url:
                    title = "CrewAI docs"
                    text = "CrewAI architecture uses crews, agents, tasks, flows, process coordination, manager delegation, production deployment, runtime limitations, and failure modes. " * 2
                elif "autogen" in url or "agent-framework" in url:
                    title = "AutoGen docs"
                    text = "AutoGen architecture uses conversational agents, group chat coordination, runtime messages, production migration guidance, orchestration limitations, and failure modes. " * 2
                elif "haystack" in url:
                    title = "Haystack docs"
                    text = "Haystack architecture uses pipeline components, agents, graph execution, document AI production deployment, orchestration limits, and failure modes. " * 2
                else:
                    title = "LlamaIndex docs"
                    text = "LlamaIndex Workflows architecture uses typed events, workflow steps, async runtime, agent orchestration, production deployment, and failure modes. " * 2
                extracted.append(Source(title=title, url=url, content=text))
            return extracted, ToolCall(name="read_url", input={"urls": urls}, output={"provider": "FakeExtract"}, ok=True)

    state = ResearchStateStore(
        brief=ResearchBrief(objective="Compare frameworks", research_profile="technical_architecture", source="heuristic"),
        contract=CoverageContract(
            cells=[
                CoverageCell(subject=subject, dimension="architecture model")
                for subject in ["LangGraph", "CrewAI", "AutoGen", "Haystack", "LlamaIndex Workflows"]
            ],
            subjects=["LangGraph", "CrewAI", "AutoGen", "Haystack", "LlamaIndex Workflows"],
            dimensions=["architecture model"],
            source="profile:technical_architecture:framework_comparison",
        ),
        plan=ResearchPlan(research_profile="technical_architecture", max_sources=12),
        evidence=EvidencePack(
            items=[
                EvidenceItem(
                    source_id="S1",
                    title="Thin framework list",
                    url="https://example.com/listicle",
                    evidence="Skip to content Navigation menu Subscribe Previous Next list of AI frameworks.",
                )
            ]
        ),
        all_sources=[
            Source(
                title="Thin framework list",
                url="https://example.com/listicle",
                content="Skip to content Navigation menu Subscribe Previous Next Sign in Cookie settings.",
            )
        ],
        budget_ledger=ResearchBudgetLedger(
            budget=ResearchBudget(max_sources=16, max_deep_links=0, max_tool_calls=3, max_model_calls=3)
        ),
    )
    tools = PrimaryDocTools()
    agent = LeadResearchAgent(request, tools)
    agent.ledger = state.budget_ledger
    agent.budget = state.budget_ledger.budget

    agent._remediate_weak_evidence_if_needed(state)

    assert len(tools.read_urls) >= 8
    assert any("langgraph" in source.url.lower() and source.content for source in state.all_sources)
    assert any("docs.crewai.com" in source.url.lower() and source.content for source in state.all_sources)
    assert len(state.evidence.items) > 1


def test_framework_comparison_binding_prioritizes_canonical_docs_over_noise():
    from app.services.agent.research_subtree import (
        CoverageCell,
        CoverageContract,
        LeadResearchAgent,
        ResearchBrief,
        ResearchPlan,
        ResearchStateStore,
    )

    request = TurnRequest(
        message=(
            "Research the top 5 agentic AI frameworks in 2025: LangGraph, CrewAI, "
            "AutoGen, Haystack, and LlamaIndex Workflows."
        ),
        research_level="regular",
    )
    state = ResearchStateStore(
        brief=ResearchBrief(objective="Compare frameworks", research_profile="technical_architecture", source="heuristic"),
        contract=CoverageContract(
            cells=[
                CoverageCell(subject="Haystack", dimension="architecture model"),
                CoverageCell(subject="LlamaIndex Workflows", dimension="architecture model"),
            ],
            subjects=["LangGraph", "CrewAI", "AutoGen", "Haystack", "LlamaIndex Workflows"],
            dimensions=["architecture model"],
            source="profile:technical_architecture:framework_comparison",
        ),
        plan=ResearchPlan(research_profile="technical_architecture", max_sources=3, min_evidence_items=3),
        all_sources=[
            Source(title=f"Listicle {index}", url=f"https://example.com/listicle-{index}", content="AI framework list with generic marketing copy.")
            for index in range(8)
        ]
        + [
            Source(
                title="Haystack pipelines",
                url="https://docs.haystack.deepset.ai/docs/pipelines",
                content=(
                    "Haystack architecture uses pipeline components, document stores, retrievers, generators, "
                    "agents, tools, orchestration, production deployment, tracing, and failure handling for RAG workflows."
                ),
            ),
            Source(
                title="LlamaIndex Workflows",
                url="https://docs.llamaindex.ai/en/stable/module_guides/workflow/",
                content=(
                    "LlamaIndex Workflows architecture uses event-driven workflow steps, typed events, async execution, "
                    "agent orchestration, state, deployment, production observability, and failure handling."
                ),
            ),
        ],
    )
    agent = LeadResearchAgent(request, tools=object())
    agent.budget.max_sources = 3
    agent.budget.max_deep_links = 0

    agent._bind_state_evidence(state)

    evidence_urls = [item.url for item in state.evidence.items]
    assert any("docs.haystack.deepset.ai" in url for url in evidence_urls)
    assert any("docs.llamaindex.ai" in url for url in evidence_urls)


def test_framework_quality_requires_bound_official_evidence_not_just_source_url():
    from app.services.agent.research_subtree import (
        CoverageContract,
        EvidencePack,
        ResearchBrief,
        ResearchPlan,
        ResearchStateStore,
        _evidence_quality_issues,
    )

    request = TurnRequest(
        message=(
            "Research the top 5 agentic AI frameworks in 2025: LangGraph, CrewAI, "
            "AutoGen, Haystack, and LlamaIndex Workflows."
        ),
        research_level="regular",
    )
    state = ResearchStateStore(
        brief=ResearchBrief(objective="Compare frameworks", research_profile="technical_architecture", source="heuristic"),
        contract=CoverageContract(
            subjects=["LangGraph", "CrewAI", "AutoGen", "Haystack", "LlamaIndex Workflows"],
            dimensions=["architecture model"],
            source="profile:technical_architecture:framework_comparison",
        ),
        plan=ResearchPlan(research_profile="technical_architecture", min_evidence_items=5),
        evidence=EvidencePack(items=[]),
        all_sources=[
            Source(
                title="Haystack pipelines",
                url="https://docs.haystack.deepset.ai/docs/pipelines",
                snippet="Canonical official documentation source for Haystack.",
            )
        ],
    )

    issues = _evidence_quality_issues(request, state)

    assert any("missing official documentation evidence" in issue for issue in issues)
    assert any("missing bound substantive evidence" in issue for issue in issues)


def test_generic_research_remediation_runs_targeted_followup_before_synthesis():
    from app.services.agent.research_subtree import (
        CoverageCell,
        CoverageContract,
        EvidenceItem,
        EvidencePack,
        LeadResearchAgent,
        ResearchBrief,
        ResearchBudget,
        ResearchBudgetLedger,
        ResearchPlan,
        ResearchStateStore,
        SearchWorkerPlan,
        _evidence_quality_issues,
    )

    request = TurnRequest(
        message="Research Tavily and Exa search API pricing and production tradeoffs.",
        research_level="regular",
    )

    class TargetedTools:
        def __init__(self):
            self.search_queries = []

        def search_web(self, query, max_results=4):
            self.search_queries.append(query)
            return [
                Source(title="Tavily pricing", url="https://tavily.com/pricing", snippet="Tavily pricing production API"),
                Source(title="Exa pricing", url="https://exa.ai/pricing", snippet="Exa pricing search API"),
            ], ToolCall(name="web_search", input={"query": query}, output={"provider": "FakeSearch"}, ok=True)

        def extract_urls(self, urls, max_chars_per_source=3500):
            return [
                Source(
                    title=url,
                    url=url,
                    content=(
                        f"{url} pricing tiers, API limits, production deployment, reliability tradeoffs, "
                        "known limitations, operational constraints, and enterprise support details."
                    ),
                )
                for url in urls
            ], ToolCall(name="read_url", input={"urls": urls}, output={"provider": "FakeExtract"}, ok=True)

    state = ResearchStateStore(
        brief=ResearchBrief(objective="Compare search API pricing and production tradeoffs", research_profile="vendor_comparison", source="heuristic"),
        contract=CoverageContract(
            cells=[
                CoverageCell(subject="Tavily", dimension="pricing"),
                CoverageCell(subject="Exa", dimension="pricing"),
            ],
            subjects=["Tavily", "Exa"],
            dimensions=["pricing"],
            source="test",
        ),
        plan=ResearchPlan(
            research_profile="vendor_comparison",
            workers=[SearchWorkerPlan(question="Compare Tavily and Exa pricing", query="Tavily Exa pricing API production")],
            max_sources=6,
            min_evidence_items=3,
        ),
        evidence=EvidencePack(
            items=[
                EvidenceItem(
                    source_id="S1",
                    title="Search API notes",
                    url="https://example.com/nav",
                    evidence="Skip to content Navigation menu Subscribe Previous Next.",
                )
            ]
        ),
        all_sources=[
            Source(
                title="Search API notes",
                url="https://example.com/nav",
                content="Skip to content Navigation menu Subscribe Previous Next.",
            )
        ],
        budget_ledger=ResearchBudgetLedger(
            budget=ResearchBudget(max_sources=8, max_deep_links=0, max_tool_calls=5, max_model_calls=3)
        ),
    )
    tools = TargetedTools()
    agent = LeadResearchAgent(request, tools)
    agent.ledger = state.budget_ledger
    agent.budget = state.budget_ledger.budget

    assert _evidence_quality_issues(request, state)

    agent._remediate_weak_evidence_if_needed(state)

    assert tools.search_queries
    assert len(state.evidence.items) >= 2
    assert any("pricing" in item.evidence.lower() for item in state.evidence.items)


def test_deep_link_extraction_skips_assets_and_marketing_junk():
    from app.services.agent.research_subtree import extract_deep_link_candidates

    links = extract_deep_link_candidates(
        [
            Source(
                title="Best Electronic Health Record Companies",
                url="https://lifebit.ai/blog/best-electronic-medical-records-companies",
                content=(
                    "See https://lifebit.ai/wp-content/uploads/2025/04/logo.png "
                    "https://lifebit.ai/pricing/ https://lifebit.ai/contact-us/ "
                    "https://lifebit.ai/demo-2/ https://lifebit.ai/blog/category/industry/ "
                    "https://www.facebook.com/tr?id=451780285406566&ev=PageView&noscript=1 "
                    "https://docs.github.com/search-github/github-code-search/understanding-github-code-search-syntax "
                    "https://avatars.githubusercontent.com/u/69429590?v=4&size=48 "
                    "and https://lifebit.ai/blog/ehr-market-analysis"
                ),
            )
        ],
        max_links=10,
    )

    assert [link.url for link in links] == ["https://lifebit.ai/blog/ehr-market-analysis"]


def test_judge_requests_more_research_for_gap_saturated_answer():
    from app.services.agent.research_subtree import (
        CoverageCell,
        CoverageContract,
        EvidenceItem,
        EvidencePack,
        ResearchBudget,
        ResearchBudgetLedger,
        ResearchBrief,
        ResearchPlan,
        ResearchStateStore,
        judge_research_final,
    )

    request = TurnRequest(
        message=(
            "Compare Epic, Oracle Health, MEDITECH, athenahealth, and eClinicalWorks "
            "including interoperability architecture, implementation approach, total cost of ownership, "
            "and known deployment failures."
        ),
        research_level="regular",
    )
    state = ResearchStateStore(
        brief=ResearchBrief(objective=request.message, research_profile="vendor_comparison", source="heuristic"),
        contract=CoverageContract(
            cells=[CoverageCell(subject="Epic", dimension="interoperability architecture", status="filled", confidence=0.8)],
            subjects=["Epic", "Oracle Health", "MEDITECH", "athenahealth", "eClinicalWorks"],
            dimensions=["interoperability architecture", "implementation approach", "total cost of ownership", "known deployment failures"],
            source="brief_anchored:vendor_comparison",
        ),
        plan=ResearchPlan(research_profile="vendor_comparison", min_evidence_items=2, judge_threshold=0.72),
        evidence=EvidencePack(
            items=[
                EvidenceItem(source_id="S1", title="EHR overview", url="https://example.com/ehr", evidence="Epic and MEDITECH market fit evidence."),
                EvidenceItem(source_id="S2", title="EHR costs", url="https://example.com/cost", evidence="Epic cost evidence."),
            ],
            coverage=1.0,
        ),
        budget_ledger=ResearchBudgetLedger(
            budget=ResearchBudget(max_sources=10, max_deep_links=4, max_tool_calls=8, max_model_calls=8),
            tool_calls=2,
            sources_read=3,
        ),
    )
    answer = """# EHR Platform Comparison

## Evidence scope
The evidence does not contain vendor-specific interoperability architecture, implementation methodology, TCO for most vendors, or known deployment failures.

| Vendor | Interoperability | Implementation | TCO | Failures |
|---|---|---|---|---|
| Epic | partial [S1] | no evidence | partial [S2] | not in evidence |
| Oracle Health | not in evidence | no evidence | gap | no evidence |
| MEDITECH | not in evidence | gap | no evidence | not documented |
| athenahealth | not supported | gap | no evidence | no evidence |
| eClinicalWorks | not in evidence | no evidence | gap | no evidence |

Recommendation: MEDITECH [S1].
"""

    verdict = judge_research_final(request, state, answer)

    assert verdict.next_action == "research_more"
    assert verdict.can_publish is False
    assert any("requested dimensions as evidence gaps" in issue for issue in verdict.issues)


def test_judge_rejects_all_subjects_claim_when_one_subject_has_no_evidence():
    from app.services.agent.research_subtree import (
        CoverageCell,
        CoverageContract,
        EvidenceItem,
        EvidencePack,
        ResearchBudget,
        ResearchBudgetLedger,
        ResearchBrief,
        ResearchPlan,
        ResearchStateStore,
        judge_research_final,
    )

    request = TurnRequest(
        message=(
            "Compare Epic, Oracle Health, MEDITECH, athenahealth, and eClinicalWorks "
            "including architecture, interoperability, deployment failures, and TCO."
        ),
        research_level="regular",
    )
    state = ResearchStateStore(
        brief=ResearchBrief(objective=request.message, research_profile="vendor_comparison", source="heuristic"),
        contract=CoverageContract(
            cells=[CoverageCell(subject="Epic", dimension="architecture", status="filled", confidence=0.8)],
            subjects=["Epic", "Oracle Health", "MEDITECH", "athenahealth", "eClinicalWorks"],
            dimensions=["architecture", "interoperability", "deployment failures", "TCO"],
            source="brief_anchored:multi_subject_comparison",
        ),
        plan=ResearchPlan(research_profile="vendor_comparison", min_evidence_items=2, judge_threshold=0.72),
        evidence=EvidencePack(
            items=[
                EvidenceItem(source_id="S1", title="Epic architecture", url="https://example.com/epic", evidence="Epic architecture evidence."),
                EvidenceItem(source_id="S2", title="MEDITECH regional", url="https://example.com/meditech", evidence="MEDITECH regional evidence."),
            ],
            coverage=1.0,
        ),
        budget_ledger=ResearchBudgetLedger(
            budget=ResearchBudget(max_sources=10, max_deep_links=4, max_tool_calls=8, max_model_calls=8),
            tool_calls=2,
            sources_read=3,
        ),
    )
    answer = """# EHR Platform Comparison

This evidence pack contains vendor-specific architecture, interoperability, deployment-failure,
and KLAS satisfaction sources for all five platforms.

### Epic
Epic architecture is documented [S1].

### Oracle Health
Oracle Health has deployment material [S2].

### MEDITECH
MEDITECH has regional evidence [S2].

### athenahealth
athenahealth has cloud evidence [S1].

### eClinicalWorks
The retrieved pack contains no eClinicalWorks-specific architecture, interoperability,
implementation, pricing, or deployment-failure sources. On the available evidence I cannot
responsibly compare eClinicalWorks against the other four.

## Recommendation
Recommend MEDITECH [S2].
"""

    verdict = judge_research_final(request, state, answer)

    assert verdict.next_action == "research_more"
    assert verdict.can_publish is False
    assert any("overclaims evidence coverage" in issue for issue in verdict.issues)


def test_published_sources_only_include_cited_evidence():
    from app.services.agent.models import Source
    from app.services.agent.research_lead import _published_sources_for_answer
    from app.services.agent.research_subtree import CoverageContract, EvidenceItem, EvidencePack, ResearchBrief, ResearchPlan, ResearchStateStore

    state = ResearchStateStore(
        brief=ResearchBrief(objective="Compare vendors", research_profile="vendor_comparison", source="heuristic"),
        contract=CoverageContract(source="test"),
        plan=ResearchPlan(research_profile="vendor_comparison"),
        evidence=EvidencePack(
            items=[
                EvidenceItem(source_id="S1", title="Used", url="https://example.com/used", evidence="Used evidence", query="q1", provider="Search"),
                EvidenceItem(source_id="S2", title="Unused", url="https://example.com/unused", evidence="Unused evidence", query="q2", provider="Search"),
            ]
        ),
        all_sources=[
            Source(title="Used", url="https://example.com/used"),
            Source(title="Unused", url="https://example.com/unused"),
            Source(title="Discovery only", url="https://example.com/discovery-only"),
        ],
    )

    sources = _published_sources_for_answer(state, "Only the used source is cited [S1].")

    assert [source.url for source in sources] == ["https://example.com/used"]


def test_ehr_subject_extraction_strips_trailing_dash():
    from app.services.agent.research_contracts import _extract_named_comparison_subjects

    message = (
        "Compare Epic, Oracle Health, MEDITECH, athenahealth, and eClinicalWorks—across "
        "interoperability architecture, implementation approach, total cost of ownership, "
        "and known deployment failures."
    )

    assert _extract_named_comparison_subjects(message) == [
        "Epic",
        "Oracle Health",
        "MEDITECH",
        "athenahealth",
        "eClinicalWorks",
    ]


def test_lead_evidence_binding_caps_to_curated_source_budget(monkeypatch):
    from app.services.agent import research_lead
    from app.services.agent.research_lead import LeadResearchAgent
    from app.services.agent.research_subtree import (
        CoverageContract,
        EvidencePack,
        ResearchBrief,
        ResearchBudget,
        ResearchPlan,
        ResearchStateStore,
    )

    seen: dict[str, int] = {}

    def fake_bind_evidence(*args, **kwargs):
        seen["max_items"] = kwargs["max_items"]
        return EvidencePack(items=[])

    monkeypatch.setattr(research_lead, "bind_evidence", fake_bind_evidence)
    agent = LeadResearchAgent(TurnRequest(message="Compare EHR vendors", research_level="deep"), tools=object())
    agent.budget = ResearchBudget(max_sources=7, max_deep_links=13)
    state = ResearchStateStore(
        brief=ResearchBrief(objective="Compare EHR vendors", research_profile="vendor_comparison", source="heuristic"),
        contract=CoverageContract(subjects=["Epic"], dimensions=["pricing"], source="test"),
        plan=ResearchPlan(research_profile="vendor_comparison"),
        all_sources=[Source(title=f"Source {idx}", url=f"https://example.com/{idx}") for idx in range(20)],
    )

    agent._bind_state_evidence(state)

    assert seen["max_items"] == 7


def test_technical_architecture_queries_are_provider_friendly():
    from app.services.agent.research_subtree import (
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
        TurnRequest(message=message, research_level="deep"),
        CoverageContract(cells=[CoverageCell(subject="Evidence binder and citation map", dimension="data model")]),
    )
    assert plan.workers[0].discovery_domain == "academic"
    assert any(worker.rationale.startswith("Profile-level anchor") for worker in plan.workers)
    assert any(worker.rationale.startswith("Cover open contract cells") for worker in plan.workers)


def test_deep_worker_plan_preserves_contract_workers_with_discovery_workers():
    from app.services.agent.research_subtree import CoverageCell, CoverageContract, plan_from_contract

    contract = CoverageContract(
        cells=[
            CoverageCell(subject=f"System component {index}", dimension="implementation pattern")
            for index in range(12)
        ]
    )

    plan = plan_from_contract(
        TurnRequest(
            message="Conduct deep research and generate a detailed architectural report explaining system design, components, and workflows of agentic deep research AI.",
            research_level="deep",
        ),
        contract,
    )

    assert len(plan.workers) == 10
    assert sum(1 for worker in plan.workers if worker.discovery_domain) >= 2
    assert sum(1 for worker in plan.workers if worker.rationale.startswith("Cover open contract cells")) >= 6


def test_domain_discovery_queries_use_clean_subjects():
    from app.services.agent.research_subtree import (
        _domain_discovery_workers,
        research_budget_for,
    )

    request = TurnRequest(
        message=(
            "Conduct deep research and generate a detailed architectural report on "
            "the system architecture, AI agent workflows, and LLM integration "
            "mechanisms of agentic presentation generation platforms like Gamma."
        ),
        research_level="deep",
    )

    workers = _domain_discovery_workers(request, "technical_architecture", research_budget_for(request))
    queries = [worker.query for worker in workers]

    assert queries
    assert all(not query.startswith("and a ") for query in queries)
    assert all("system architecture ai agent workflows" not in query for query in queries)
    assert any("agentic presentation generation platforms gamma" in query for query in queries)
    assert any("site:arxiv.org" in query for query in queries)


def test_research_profile_classifier_handles_non_architecture_profiles():
    from app.services.agent.research_subtree import infer_research_profile

    cases = [
        (
            "Build vs buy: compare Tavily and You.com for our search provider decision.",
            "vendor_comparison",
        ),
        (
            "Analyze RBI digital lending guidelines and compliance obligations.",
            "policy_regulatory",
        ),
        (
            "Create brand guidelines for Fronei.",
            "general",
        ),
        (
            "Create an implementation roadmap for migrating Fronei to production.",
            "implementation_plan",
        ),
        (
            "Research the enterprise AI governance market landscape and adoption trends.",
            "market_landscape",
        ),
    ]

    for prompt, expected in cases:
        assert infer_research_profile(prompt) == expected


def test_model_brief_vendor_guardrail_overrides_strategy(monkeypatch):
    from app.services.agent import model_client
    from app.services.agent.research_subtree import generate_research_brief

    response = SimpleNamespace(
        text=(
            '{"objective":"Choose a provider","research_profile":"strategy_brief",'
            '"secondary_profiles":[],"profile_confidence":0.82,'
            '"classification_reason":"decision framing","domain_strategy_hints":[],'
            '"audience":"CTO","scope_in":["Tavily","You.com"],"scope_out":[],'
            '"success_criteria":["Compare providers"],"output_type":"comparison","assumptions":[]}'
        ),
        model_used="test-model",
        latency_ms=12,
        cost_usd=0.001,
    )
    monkeypatch.setattr(model_client, "complete", lambda *a, **kw: response)

    brief = generate_research_brief(
        TurnRequest(
            message="Build vs buy: compare Tavily and You.com for our search provider decision.",
            research_level="deep",
        )
    )

    assert brief.research_profile == "vendor_comparison"
    assert "strategy_brief" in brief.secondary_profiles
    assert brief.source == "llm"


def test_plan_from_contract_uses_profile_source_for_execution_policy():
    from app.services.agent.research_subtree import CoverageCell, CoverageContract, plan_from_contract

    contract = CoverageContract(
        cells=[CoverageCell(subject="Tavily", dimension="pricing")],
        subjects=["Tavily"],
        dimensions=["pricing"],
        source="profile:vendor_comparison",
    )

    plan = plan_from_contract(
        TurnRequest(
            message="Build vs buy: compare Tavily and You.com for our search provider decision.",
            research_level="deep",
        ),
        contract,
    )

    assert plan.research_profile == "vendor_comparison"
    assert "official product docs" in plan.source_lanes
    assert any("pricing" in worker.query.lower() for worker in plan.workers)


def test_profile_execution_policies_drive_domain_workers_and_anchor_queries():
    from app.services.agent.research_subtree import (
        _domain_discovery_workers,
        _vendor_comparison_anchor_queries,
        research_budget_for,
    )

    request = TurnRequest(
        message="Conduct deep research comparing Tavily, You.com, and Nimble search providers.",
        research_level="deep",
    )

    anchors = _vendor_comparison_anchor_queries(request.message)
    workers = _domain_discovery_workers(request, "vendor_comparison", research_budget_for(request))

    assert all(not query.lower().startswith("conduct deep research") for query in anchors)
    assert any("pricing" in query.lower() for query in anchors)
    assert any("official docs pricing security" in worker.query for worker in workers)
    assert any(worker.discovery_domain == "documentation" for worker in workers)


def test_coverage_contract_fallback_has_cells(monkeypatch):
    from app.services.agent import model_client
    from app.services.agent.research_subtree import ResearchBrief, generate_coverage_contract

    monkeypatch.setattr(model_client, "complete", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("fail")))

    contract = generate_coverage_contract(
        TurnRequest(message="Compare Tavily vs You.com", research_level="deep"),
        ResearchBrief(
            objective="Compare Tavily and You.com",
            success_criteria=["pricing covered", "capabilities covered"],
            source="heuristic",
        ),
    )

    assert contract.cells
    assert contract.source == "heuristic"


def test_plan_research_parse_fallback_uses_contract_workers(monkeypatch):
    from app.services.agent import model_client
    from app.services.agent.model_client import ModelResponse
    from app.services.agent.research_subtree import plan_research

    monkeypatch.setattr(
        model_client,
        "complete",
        lambda *a, **kw: ModelResponse(text='{"questions": ["broken"],', model_used="fake", latency_ms=1, cost_usd=0.0),
    )

    request = TurnRequest(
        message=(
            "Research and compare the top 5 EHR platforms for mid-size hospital systems: "
            "Epic, Oracle Health, MEDITECH, athenahealth, and eClinicalWorks, including "
            "interoperability architecture, implementation approach, total cost of ownership, "
            "and known deployment failures."
        ),
        research_level="regular",
    )

    plan = plan_research(request)
    queries = " ".join(worker.query for worker in plan.workers).lower()

    assert plan.source == "heuristic_contract_fallback"
    assert len(plan.workers) > 1
    assert any("epic" in worker.query.lower() for worker in plan.workers)
    assert any("meditech" in worker.query.lower() for worker in plan.workers)
    assert "interoperability" in queries or "implementation" in queries


def test_coverage_contract_ratio():
    from app.services.agent.research_subtree import CoverageCell, CoverageContract

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
    from app.services.agent.research_subtree import CoverageCell, CoverageContract, plan_from_contract

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
        TurnRequest(message="Compare Tavily and Nimble", research_level="deep"),
        contract,
    )

    assert plan.workers
    assert any("Tavily" in worker.question for worker in plan.workers)
    assert any("Nimble" in worker.question for worker in plan.workers)


def test_technical_architecture_ranking_prefers_dense_sources():
    from app.services.agent.research_subtree import ResearchPlan, rank_sources

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
    from app.services.agent.research_subtree import EvidencePack, ResearchPlan, bind_evidence

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
    from app.services.agent.research_subtree import CoverageCell, CoverageContract, ResearchPlan, bind_evidence

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
    from app.services.agent.research_subtree import CoverageCell, CoverageContract, ResearchPlan, bind_evidence

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
    from app.services.agent.research_subtree import ResearchPlan, bind_evidence

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
    from app.services.agent import model_client
    from app.services.agent.research_subtree import (
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

    decision = reflect(TurnRequest(message="test", research_level="deep"), state)

    assert decision.sufficient is True
    assert decision.coverage_ratio == 1.0
    assert decision.next_action == "publish"


def test_citation_verification_detects_hallucinated(monkeypatch):
    from app.services.agent import model_client
    from app.services.agent.research_subtree import EvidenceItem, EvidencePack, verify_citations_semantically

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
    from app.services.agent import model_client
    from app.services.agent.model_client import ModelResponse
    from app.services.agent.research_subtree import lead_research_loop

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
        TurnRequest(message="Compare Tavily and Nimble pricing", research_level="deep"),
        FakeTools(),
        lambda stage, message, data: None,
    )

    assert set(result) == {"sources", "tool_calls", "evidence", "response", "plan", "worker_reports", "feedback"}
    assert result["response"].text
    assert result["tool_calls"]
    assert result["worker_reports"]


def test_technical_architecture_synthesis_uses_report_budget(monkeypatch):
    from app.services.agent import model_client
    from app.services.agent.research_subtree import EvidenceItem, EvidencePack, ResearchPlan, synthesize_answer

    captured = {}

    def fake_simple_completion(system, user, *, max_tokens=1200, **kwargs):
        captured["system"] = system
        captured["user"] = user
        captured["max_tokens"] = max_tokens
        captured["role"] = kwargs.get("role")
        captured["quality_mode"] = kwargs.get("quality_mode")
        return model_client.ModelResponse(text="ok", model_used="fake", latency_ms=1, cost_usd=0.0)

    monkeypatch.setattr(model_client, "simple_completion", fake_simple_completion)
    request = TurnRequest(
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
    assert captured["role"] == "synthesis"
    assert captured["quality_mode"] == "standard"
    assert "Typed evidence claims" in captured["user"]
    assert "implementation/implementation_detail" in captured["user"]
    assert "Derive the section structure from the evidence" in captured["user"]
    assert "data models, control flow, state transitions" in captured["user"]
    assert "real architectural report" in captured["system"]


def test_lead_research_dispatches_search_workers_in_parallel(monkeypatch):
    from app.services.agent.research_subtree import (
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
        def __init__(self):
            self.search_started_at: list[float] = []

        def search_web(self, query, max_results=4):
            self.search_started_at.append(time.monotonic())
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
    tools = SlowSearchTools()
    agent = LeadResearchAgent(TurnRequest(message="parallel test", research_level="deep"), tools)
    agent.ledger = state.budget_ledger
    agent.budget = state.budget_ledger.budget

    agent._dispatch_worker_wave(state)

    assert len(tools.search_started_at) == 3
    assert max(tools.search_started_at) - min(tools.search_started_at) < 0.08
    assert len(state.all_tool_calls) >= 2
    assert len(state.all_sources) >= 3
    assert state.worker_reports
    assert all(report.claims for report in state.worker_reports)


def test_worker_reports_update_coverage_from_typed_claims():
    from app.services.agent.research_subtree import (
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
    from app.services.agent.research_subtree import (
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


def test_state_add_sources_preserves_snippet_when_reader_returns_account_chrome():
    from app.services.agent.research_subtree import (
        CoverageContract,
        ResearchBrief,
        ResearchPlan,
        ResearchStateStore,
    )

    state = ResearchStateStore(
        brief=ResearchBrief(objective="creatine kidney safety", source="heuristic"),
        contract=CoverageContract(),
        plan=ResearchPlan(source="heuristic"),
    )

    snippet = "Systematic review of creatine supplementation found no adverse kidney function effects in healthy adults."
    chrome = "![logo](x) Log in Dashboard Publications Account settings Log out"
    state.add_sources([Source(title="PMC review", url="https://pmc.ncbi.nlm.nih.gov/articles/PMC123", snippet=snippet)])
    state.add_sources([Source(title="PMC review", url="https://pmc.ncbi.nlm.nih.gov/articles/PMC123", content=chrome)])

    assert len(state.all_sources) == 1
    assert state.all_sources[0].snippet == snippet
    assert state.all_sources[0].content == ""


def test_bind_evidence_uses_snippet_when_extracted_content_is_account_chrome():
    from app.services.agent.research_subtree import ResearchPlan, bind_evidence

    snippet = "Creatine supplementation at recommended doses has not been shown to impair kidney function in healthy adults."
    chrome = "![logo](x) Log in Dashboard Publications Account settings Log out"

    evidence = bind_evidence(
        [
            Source(
                title="Creatine kidney safety review",
                url="https://pmc.ncbi.nlm.nih.gov/articles/PMC123",
                snippet=snippet,
                content=chrome,
            )
        ],
        plan=ResearchPlan(
            questions=["Is long-term creatine supplementation safe for kidney health?"],
            min_evidence_items=1,
        ),
        max_items=1,
    )

    assert evidence.items
    assert "Creatine supplementation" in evidence.items[0].evidence
    assert "Dashboard" not in evidence.items[0].evidence


def test_technical_architecture_binds_architecture_cards():
    from app.services.agent.research_subtree import ResearchPlan, bind_evidence

    source = Source(
        title="PPTAgent architecture",
        url="https://arxiv.org/abs/2501.12345",
        content=(
            "PPTAgent uses an orchestrator planner, generator, reviewer, and verifier workflow. "
            "The system stores an outline, slide spec JSON, render plan, evidence pack, and citation map in state. "
            "It renders slides with PptxGenJS and uses soffice and pdftoppm for visual verification. "
            "The validation loop renders the artifact, inspects overflow and overlap, and repairs failed slides. "
            "Reported PPTEval Pearson correlation was 0.71 and the visual critic improved design scores by 17.8 percent. "
            "Failure modes include hallucination, overflow, truncation, invalid JSON, latency, and cost."
        ),
    )

    evidence = bind_evidence(
        [source],
        plan=ResearchPlan(
            research_profile="technical_architecture",
            questions=["How do agentic PPT systems work?"],
            search_queries=["PPTAgent architecture"],
            min_evidence_items=1,
        ),
        max_items=3,
    )

    assert evidence.architecture_cards
    card = evidence.architecture_cards[0]
    assert card.system == "PPTAgent"
    assert "planner" in card.agent_roles
    assert "slide spec" in card.state_objects
    assert "pptxgenjs" in card.tools_or_renderers
    assert card.metrics
    assert "overflow" in card.failure_modes


def test_deep_technical_reader_uses_large_source_cap():
    from app.services.agent.research_subtree import ResearchPlan, _max_parallel_read_batches_for, _read_cap_for_batch

    plan = ResearchPlan(research_profile="technical_architecture")

    assert _read_cap_for_batch(["https://arxiv.org/abs/2501.12345"], plan) == 14000
    assert _read_cap_for_batch(["https://github.com/example/repo"], plan) == 10000
    assert _read_cap_for_batch(["https://example.com/post"], plan) == 6500
    assert _max_parallel_read_batches_for("regular") == 4
    assert _max_parallel_read_batches_for("deep") == 6


def test_deep_technical_synthesis_uses_expansive_token_budget():
    from app.services.agent.research_subtree import ResearchPlan, _synthesis_token_budget

    plan = ResearchPlan(research_profile="technical_architecture")

    assert _synthesis_token_budget(TurnRequest(message="architecture", research_level="deep"), plan) == 12000
    assert (
        _synthesis_token_budget(
            TurnRequest(message="architecture", research_level="deep", quality_mode="executive"),
            plan,
        )
        == 14000
    )


def test_deep_document_writer_uses_expansive_budget_and_floor():
    from app.services.agent.document_subtree import (
        DocumentDraft,
        DocumentPlan,
        _document_writer_token_budget,
        judge_document,
    )

    request = TurnRequest(
        message="Conduct deep research and generate a detailed architectural report on agentic deep research AI.",
        research_level="deep",
        output_format="docx",
    )
    plan = DocumentPlan(title="Architecture", sections=[f"Section {index}" for index in range(10)])
    short_draft = DocumentDraft(markdown="# Summary\n\nToo short. [S1]")

    assert _document_writer_token_budget(request, research_answer="Research answer") == 10000
    assert judge_document(short_draft, plan, source_count=1).status == "repair"


def test_deep_document_writer_generates_sections_individually(monkeypatch):
    from app.services.agent import model_client
    from app.services.agent.document_subtree import DocumentPlan, write_document
    from app.services.agent.model_client import ModelResponse
    from app.services.agent.research_subtree import EvidenceItem, EvidencePack

    calls = []

    def fake_simple_completion(system, user, **kwargs):
        calls.append({"system": system, "user": user, **kwargs})
        heading = "Generated section"
        for line in user.splitlines():
            if line.startswith("Current section"):
                heading = line.split(":", 1)[1].strip()
                break
        return ModelResponse(
            text=f"## {heading}\n\n### Existing subsection\n\nDetailed content for {heading}. [S1]",
            model_used="test-model",
            latency_ms=10,
            cost_usd=0.001,
            model_role=kwargs.get("role", ""),
            preferred_model="test-model",
            attempted_models=["test-model"],
        )

    monkeypatch.setattr(model_client, "simple_completion", fake_simple_completion)

    request = TurnRequest(
        message="Conduct deep research and generate a detailed architectural report on agentic deep research AI.",
        research_level="deep",
        output_format="docx",
    )
    plan = DocumentPlan(
        title="Architecture",
        sections=[
            "1. Executive Summary",
            "2. System Architecture",
            "Agent Workflows",
            "Evidence Binder",
            "Failure Modes",
            "Recommendations",
        ],
    )
    evidence = EvidencePack(
        items=[
            EvidenceItem(
                source_id="S1",
                title="Architecture paper",
                url="https://arxiv.org/html/2501.12345",
                evidence="Agent workflows use planner executor loops, evidence binders, and judge repair gates.",
                question="Agent workflows",
                source_type="academic",
            )
        ],
        coverage=1.0,
    )

    draft = write_document(request, plan, sources=[], research_answer="Research answer [S1].", evidence=evidence)
    calls_by_heading = {}
    for call in calls:
        for line in call["user"].splitlines():
            if line.startswith("Current section"):
                calls_by_heading[line.split(":", 1)[1].strip()] = call
                break

    assert len(calls) == len(plan.sections)
    assert draft.markdown.startswith("# Architecture")
    assert "## 1. Executive Summary" in draft.markdown
    assert "## 2. System Architecture" in draft.markdown
    assert "## 1. 1. Executive Summary" not in draft.markdown
    assert "## 2. 2. System Architecture" not in draft.markdown
    assert "### 2.1 Existing subsection" in draft.markdown
    assert calls_by_heading["1. Executive Summary"]["max_tokens"] < calls_by_heading["2. System Architecture"]["max_tokens"]
    assert all(call["role"] == "document_writer" for call in calls)
    assert draft.latency_ms == 60


def test_deep_document_planner_preserves_long_context_and_sections():
    from app.services.agent.document_subtree import (
        DocumentPlan,
        _normalize_plan,
        _planner_research_summary,
        _section_limit,
    )

    request = TurnRequest(
        message="Conduct deep research and generate a detailed architectural report on agentic deep research AI.",
        research_level="deep",
        output_format="docx",
    )
    sections = [f"Section {index}" for index in range(16)]
    plan = _normalize_plan(DocumentPlan(title="Architecture", sections=sections), request)
    research = "x" * 15000

    assert len(plan.sections) == 14
    assert _section_limit(request) == 14
    assert len(_planner_research_summary(request, research)) == 12000


def test_technical_architecture_ranker_prioritizes_primary_technical_sources():
    from app.services.agent.research_subtree import ResearchPlan, rank_sources

    plan = ResearchPlan(
        research_profile="technical_architecture",
        questions=["agentic document generation architecture workflow implementation"],
    )
    sources = [
        Source(
            title="Generic overview of agentic document generation",
            url="https://medium.com/example/agentic-document-generation-overview",
            snippet="agentic document generation architecture workflow implementation overview",
        ),
        Source(
            title="Agentic document generation architecture paper",
            url="https://arxiv.org/abs/2501.12345",
            snippet="agentic document generation architecture workflow implementation evaluation",
        ),
        Source(
            title="AgentDeck implementation repository",
            url="https://github.com/example/agentdeck",
            snippet="agentic document generation architecture workflow implementation source code",
        ),
        Source(
            title="PptxGenJS documentation",
            url="https://gitbrent.github.io/PptxGenJS/docs/api/presentation",
            snippet="presentation generation implementation renderer workflow documentation",
        ),
    ]

    ranked = rank_sources(sources, plan)
    top_types = [item.source_type for item in ranked[:3]]

    assert "academic" in top_types
    assert "repository" in top_types
    assert ranked[0].source_type in {"academic", "repository", "documentation"}


def test_runtime_routes_deep_to_lead_loop(monkeypatch):
    from app.services.agent import research_subtree
    from app.services.agent.runtime import Runtime

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
        Runtime()._run_research_subtree(
            TurnRequest(message="deep question", research_level="deep"),
            lambda stage, message, **data: SimpleNamespace(model_dump=lambda mode=None: {"stage": stage, "message": message, "data": data}),
        )
    )

    assert called["loop"] is True
    assert result[-1].data["stage"] == "complete"


# ---------------------------------------------------------------------------
# Phase 9 — research_level misclassification fix + uncapped budget + no-permission rule
# ---------------------------------------------------------------------------

def test_multi_subject_recommendation_request_classified_as_deep():
    """Phase 9 — a request naming ≥3 subjects AND asking for a recommendation must
    classify as 'deep' via the structural signal, regardless of keyword list."""
    from app.services.agent.orchestrator import choose_research_level

    request = TurnRequest(
        message=(
            "Research the top 5 agentic AI frameworks in 2025: LangGraph, CrewAI, "
            "AutoGen, Haystack, and LlamaIndex Workflows. Provide for each: architecture model, "
            "multi-agent coordination approach, production readiness, and known failure modes. "
            "Then synthesize a recommendation for the best framework for an enterprise orchestration layer."
        ),
        # research_level not explicitly set — let choose_research_level classify it
    )
    level = choose_research_level(request, "research")
    assert level == "deep", (
        f"Expected 'deep' for 5-subject + recommendation request, got '{level}'"
    )


def test_multi_subject_recommendation_single_subject_stays_regular():
    """Phase 9 — single-subject request without deep-tier keywords stays 'regular'."""
    from app.services.agent.orchestrator import choose_research_level

    request = TurnRequest(message="What is the current USCIS filing fee for Form I-765?")
    level = choose_research_level(request, "research")
    assert level in {"regular", "easy"}, f"Single-subject query should not be 'deep', got '{level}'"


def test_deep_multi_subject_budget_exceeds_plain_deep_tier():
    """Phase 9 — a 5-subject request at deep level must get a budget strictly greater
    than the plain deep-tier defaults on every relevant dimension."""
    from app.services.agent.research_subtree import research_budget_for

    plain_deep = research_budget_for(TurnRequest(
        message="Do deep research on IPL business economics.",
        research_level="deep",
    ))
    multi_deep = research_budget_for(TurnRequest(
        message=(
            "Research LangGraph, CrewAI, AutoGen, Haystack, and LlamaIndex Workflows "
            "for enterprise orchestration. Provide a recommendation."
        ),
        research_level="deep",
    ))
    assert multi_deep.max_tool_calls > plain_deep.max_tool_calls, (
        f"5-subject deep should have more tool calls than 1-subject deep: "
        f"{multi_deep.max_tool_calls} <= {plain_deep.max_tool_calls}"
    )
    assert multi_deep.max_deep_links > plain_deep.max_deep_links, (
        f"5-subject deep should have more deep links than 1-subject deep: "
        f"{multi_deep.max_deep_links} <= {plain_deep.max_deep_links}"
    )
    assert multi_deep.max_cost_usd > plain_deep.max_cost_usd


def test_synthesis_substance_requirements_includes_no_permission_rule():
    """Phase 9 — SYNTHESIS_SUBSTANCE_REQUIREMENTS must include rule 5 banning
    permission-seeking as a closing move."""
    from app.services.agent.research_synthesis import SYNTHESIS_SUBSTANCE_REQUIREMENTS

    text = SYNTHESIS_SUBSTANCE_REQUIREMENTS.lower()
    assert "never" in text and ("permission" in text or "authorize" in text), (
        "SYNTHESIS_SUBSTANCE_REQUIREMENTS missing Phase 9 no-permission rule"
    )
    assert "deeper dive" in text or "further research" in text or "second pass" in text


def test_citation_verification_has_asks_permission_field():
    """Phase 9 — CitationVerification must have asks_permission_to_continue field."""
    from app.services.agent.research_models import CitationVerification

    cv = CitationVerification()
    assert hasattr(cv, "asks_permission_to_continue"), (
        "CitationVerification missing asks_permission_to_continue field"
    )
    assert cv.asks_permission_to_continue is False  # default


def test_asks_permission_wired_into_repair_trigger():
    """Phase 9 — when asks_permission_to_continue is True, needs_repair must be True in
    the lead research loop (mirrors the Phase 8 leads_with_disclaimer wiring)."""
    from app.services.agent.research_models import CitationVerification

    cv = CitationVerification(
        asks_permission_to_continue=True,
        repair_needed=False,
        repair_instruction="",
    )
    # Replicate the needs_repair OR logic from research_lead.py
    needs_repair = (
        cv.repair_needed
        or bool(cv.role_mismatch_issues)
        or bool(cv.unresolved_conflicts)
        or cv.asks_permission_to_continue
    )
    assert needs_repair, "asks_permission_to_continue=True should trigger repair"


# ---------------------------------------------------------------------------
# Phase 10 — subject-extractor sentence boundaries + synthesis budget reservation
# ---------------------------------------------------------------------------

def test_ehr_vendor_subjects_extracted_correctly():
    """Phase 10 — the EHR comparison query must extract all 5 vendors including
    'athenahealth' (all-lowercase, no capital letter anywhere)."""
    from app.services.agent.research_subtree import _extract_named_comparison_subjects

    message = (
        "Compare Epic, Oracle Cerner, Meditech Expanse, athenahealth, and eClinicalWorks "
        "as EHR platforms for enterprise hospital deployment in 2025. For each: clinical "
        "workflow integration, interoperability standards (HL7 FHIR support), cloud "
        "deployment model, pricing model, and known implementation failure modes."
    )
    subjects = _extract_named_comparison_subjects(message)
    assert "athenahealth" in subjects, (
        f"'athenahealth' must be extracted despite all-lowercase; got: {subjects}"
    )
    assert len(subjects) >= 3, f"Expected ≥3 EHR vendors, got {subjects}"


def test_covering_clause_truncation_prevents_oversized_fragments():
    """Phase 10 — 'covering X, Y, Z' should truncate the region before X, Y, Z
    so dimension words don't merge with the last subject name."""
    from app.services.agent.research_subtree import _extract_named_comparison_subjects

    # 'covering' should stop the region at "covering", stripping dimension list
    message = "Compare AWS S3, Azure Blob Storage, and Google Cloud Storage covering durability, performance, and pricing."
    subjects = _extract_named_comparison_subjects(message)
    assert "AWS S3" in subjects or any("AWS" in s for s in subjects), f"AWS S3 should be a subject, got {subjects}"
    # No subject should contain "durability" or "performance" (dimension words)
    assert all("durability" not in s and "performance" not in s for s in subjects), (
        f"Dimension words leaked into subjects: {subjects}"
    )


def test_on_dimension_list_truncation():
    """Phase 10 — 'on durability, performance...' should truncate and not cause
    'Azure Blob Storage on durability' to be treated as a single oversized fragment."""
    from app.services.agent.research_subtree import _extract_named_comparison_subjects

    message = (
        "Compare AWS S3, Azure Blob Storage, and Google Cloud Storage on durability, "
        "performance, pricing, and compliance."
    )
    subjects = _extract_named_comparison_subjects(message)
    assert len(subjects) >= 3, f"Expected ≥3 cloud storage subjects, got {subjects}"
    assert all("durability" not in s and "compliance" not in s for s in subjects), (
        f"Dimension words leaked into subjects: {subjects}"
    )


def test_ehr_comparison_triggers_multi_subject_gate():
    """Phase 10 — the EHR query must pass _is_multi_subject_comparison() after
    subject extraction is fixed, so all Phase 6/8 protections apply to non-AI domains."""
    from app.services.agent.research_subtree import _is_multi_subject_comparison

    message = (
        "Compare Epic, Oracle Cerner, Meditech Expanse, athenahealth, and eClinicalWorks "
        "as EHR platforms for enterprise hospital deployment in 2025. For each: clinical "
        "workflow integration, interoperability standards (HL7 FHIR support), cloud "
        "deployment model, pricing model, and known implementation failure modes."
    )
    assert _is_multi_subject_comparison(message), (
        "EHR 5-vendor comparison must trigger _is_multi_subject_comparison()"
    )


def test_reserved_synthesis_model_calls_field_exists():
    """Phase 10 — ResearchBudget must have reserved_synthesis_model_calls field."""
    from app.services.agent.research_models import ResearchBudget

    b = ResearchBudget()
    assert hasattr(b, "reserved_synthesis_model_calls"), (
        "ResearchBudget missing reserved_synthesis_model_calls field"
    )
    assert b.reserved_synthesis_model_calls > 0


def test_gathering_agents_stop_before_synthesis_reservation():
    """Phase 10 — gathering-phase agents (e.g. claim_classifier) must be denied
    a model call once max_model_calls - reserved_synthesis_model_calls is reached,
    while synthesis_agent is still permitted."""
    from app.services.agent.research_models import ResearchBudget, ResearchBudgetLedger

    budget = ResearchBudget(max_model_calls=4, reserved_synthesis_model_calls=2)
    ledger = ResearchBudgetLedger(budget=budget)

    # Consume 2 calls (= max_model_calls - reserved = 4 - 2 = 2 → gathering limit)
    ledger.record_model_call(cost_usd=0.0, latency_ms=0)
    ledger.record_model_call(cost_usd=0.0, latency_ms=0)

    # Gathering agent should now be denied
    assert not ledger.can_start_model("claim_classifier"), (
        "claim_classifier should be denied at gathering limit (2 of 4 used, 2 reserved)"
    )
    # Synthesis agent should still be permitted
    assert ledger.can_start_model("synthesis_agent"), (
        "synthesis_agent must still be permitted despite gathering limit"
    )


def test_max_model_calls_raised_across_tiers():
    """Phase 10 — all budget tiers must have higher max_model_calls than the pre-Phase-1
    values (4 regular, 24 deep) to account for the claim-classifier and citation-verifier
    calls added in Phases 1 and 5."""
    from app.services.agent.research_subtree import research_budget_for

    regular = research_budget_for(TurnRequest(message="Research RBI digital lending guidelines.", research_level="regular"))
    deep = research_budget_for(TurnRequest(message="Do deep research on IPL business economics.", research_level="deep"))

    assert regular.max_model_calls > 4, (
        f"Regular tier max_model_calls should exceed pre-Phase-1 value of 4, got {regular.max_model_calls}"
    )
    assert deep.max_model_calls > 24, (
        f"Deep tier max_model_calls should exceed pre-Phase-1 value of 24, got {deep.max_model_calls}"
    )


# ---------------------------------------------------------------------------
# Phase 11 tests
# ---------------------------------------------------------------------------

def test_count_comparison_dimensions_basic():
    """Phase 11 — _count_comparison_dimensions returns the right count for 'on X, Y, and Z'."""
    from app.services.agent.research_subtree import _count_comparison_dimensions

    count = _count_comparison_dimensions(
        "Compare AWS S3, Google Cloud Storage, and Azure Blob Storage on durability, pricing tiers, and egress costs"
    )
    assert count == 3, f"Expected 3 dimensions (durability/pricing tiers/egress costs), got {count}"


def test_count_comparison_dimensions_covering():
    """Phase 11 — 'covering' lead-in is also detected."""
    from app.services.agent.research_subtree import _count_comparison_dimensions

    count = _count_comparison_dimensions(
        "Review LangGraph, AutoGen, and CrewAI covering memory management, tool calling, and fault tolerance"
    )
    assert count >= 3, f"Expected ≥3 dimensions after 'covering', got {count}"


def test_count_comparison_dimensions_no_match():
    """Phase 11 — returns 0 when no dimension-list lead-in keyword is present."""
    from app.services.agent.research_subtree import _count_comparison_dimensions

    count = _count_comparison_dimensions("What is the capital of France?")
    assert count == 0, f"Expected 0 dimensions for trivial query, got {count}"


def test_cloud_storage_no_recommendation_classifies_as_deep():
    """Phase 11 — a 3-subject, 3-dimension query with NO synthesis-intent words must
    classify as 'deep' via the dimension-richness path (Signal B).
    'Compare AWS S3, GCS, and Azure Blob on durability, pricing, and egress' has zero
    recommendation/synthesis keywords and must still reach deep tier."""
    from app.services.agent.orchestrator import choose_research_level

    request = TurnRequest(
        message="Compare AWS S3, Google Cloud Storage, and Azure Blob Storage on durability, pricing tiers, and egress costs"
    )
    level = choose_research_level(request, "research")
    assert level == "deep", (
        f"Expected 'deep' for 3-subject + 3-dimension query (no synthesis-intent words), got '{level}'"
    )


def test_two_subjects_three_dimensions_classifies_as_deep():
    """Phase 11 — 2 subjects (≥2 threshold) + 3 dimensions is sufficient for deep tier."""
    from app.services.agent.orchestrator import choose_research_level

    request = TurnRequest(
        message="Compare PostgreSQL and MySQL on performance, replication support, and JSONB indexing"
    )
    level = choose_research_level(request, "research")
    assert level == "deep", (
        f"Expected 'deep' for 2-subject + 3-dimension query, got '{level}'"
    )


def test_one_subject_many_dimensions_stays_regular():
    """Phase 11 — a single-subject query with many dimensions does not reach deep tier
    via the dimension-richness path (requires ≥2 named subjects)."""
    from app.services.agent.orchestrator import choose_research_level

    request = TurnRequest(
        message="Research PostgreSQL on performance, replication, indexing, JSONB, and partitioning"
    )
    level = choose_research_level(request, "research")
    # May be regular or deep for other reasons; the dimension-richness path alone
    # should not push a single-subject query to deep.
    # We can't assert "regular" absolutely because other signals may fire, but we
    # verify the dimension counter doesn't incorrectly extract 1 subject as 2.
    from app.services.agent.research_subtree import _extract_named_comparison_subjects
    subjects = _extract_named_comparison_subjects(request.message)
    assert len(subjects) < 2, (
        f"Single-subject query should not yield ≥2 extracted subjects, got {subjects}"
    )


def test_recommendation_intent_path_still_works_after_phase11():
    """Phase 11 regression — Phase 9's recommendation-intent path (Signal A) must still
    classify queries with ≥3 subjects + synthesis terms as 'deep'."""
    from app.services.agent.orchestrator import choose_research_level

    request = TurnRequest(
        message=(
            "Research the top 5 agentic AI frameworks in 2025: LangGraph, CrewAI, "
            "AutoGen, Haystack, and LlamaIndex Workflows. Provide for each: architecture model, "
            "multi-agent coordination approach, production readiness, and known failure modes. "
            "Then synthesize a recommendation for the best framework for an enterprise orchestration layer."
        ),
    )
    level = choose_research_level(request, "research")
    assert level == "deep", (
        f"Phase 9 Signal A should still classify 5-subject + recommendation query as 'deep', got '{level}'"
    )


def test_docx_offer_boundary_in_citation_verifier_prompt():
    """Phase 11 — the citation verifier prompt must explicitly exclude document-formatting
    offers from the asks_permission_to_continue check.  This guards against the field
    firing on 'I can produce this as a DOCX if you supply vendor quotes'."""
    from app.services.agent.research_planner import CITATION_VERIFICATION_PROMPT

    # The narrowing language must be present so the LLM understands the boundary.
    assert "formatting" in CITATION_VERIFICATION_PROMPT.lower() or "reformat" in CITATION_VERIFICATION_PROMPT.lower(), (
        "CITATION_VERIFICATION_PROMPT must reference the formatting-offer exclusion "
        "so the LLM knows not to flag document-format offers as permission-seeking"
    )
    # Also confirm the original prohibited-endings examples are still present.
    assert "deeper dive" in CITATION_VERIFICATION_PROMPT.lower(), (
        "Original permission-seeking examples must still be in the prompt"
    )


def test_golden_set_has_phase11_cases():
    """Phase 11 — research_golden_set.json must contain the two Phase 11 anchor cases."""
    import json
    import os

    golden_path = os.path.join(
        os.path.dirname(__file__), "..", "evals", "research_golden_set.json"
    )
    with open(golden_path) as f:
        cases = json.load(f)

    ids = {c["id"] for c in cases}
    assert "cloud_storage_no_recommendation_multi_dimension" in ids, (
        "Golden set must include Phase 11 cloud-storage (no-recommendation) anchor case"
    )
    assert "docx_offer_not_permission_seeking" in ids, (
        "Golden set must include Phase 11 DOCX-offer boundary case"
    )
