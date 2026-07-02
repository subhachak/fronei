from app.services.agent.models import TurnRequest
from app.services.agent.research_models import CoverageContract, EvidenceItem, EvidencePack, ResearchPlan
from app.services.agent.research_synthesis import build_gap_followup_workers, build_synthesis_prompt, judge_research, _synthesis_token_budget


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


def test_owner_reliability_gap_followup_uses_forum_queries():
    request = TurnRequest(
        message=(
            "Research real-world reliability and failure rates of Anker SOLIX home battery "
            "systems after 1-2 years based on owner reviews."
        )
    )
    plan = ResearchPlan(questions=["Find owner evidence"], max_sources=14)
    evidence = EvidencePack(
        gaps=[
            "Missing actual owner/community/forum evidence; policy pages do not answer owner reliability.",
            "Missing quantified or outcome-based evidence for failure rate, degradation, or claim outcomes.",
        ]
    )

    workers = build_gap_followup_workers(request, plan, evidence)
    queries = [worker.query for worker in workers]

    assert len(workers) == 4
    assert any("site:reddit.com" in query for query in queries)
    assert any("site:diysolarforum.com" in query for query in queries)
    assert any("F3800" in query and "12 months" in query for query in queries)
    assert all("Missing actual owner" not in query for query in queries)


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
    assert "Choose whatever structure best serves this request" in user_prompt
    assert "Let evidence density per subject guide section depth" in user_prompt
    assert "cross-cutting failure taxonomy or governance lens" in user_prompt
    assert "ranked recommendation and conditional overrides" in user_prompt
    assert "LIFECYCLE FLAGS" in user_prompt
    assert "NO LEADING DISCLAIMER" in user_prompt
    assert "BEST EFFORT OVER REFUSAL" in user_prompt
    assert "Produce a concise chat answer" not in user_prompt


def test_comparison_mode_gets_strict_matrix_contract():
    _, user_prompt = build_synthesis_prompt(
        TurnRequest(
            message="Compare AWS Bedrock, Snowflake Cortex, and Azure OpenAI on governance, cost, and model catalog.",
            output_format="markdown",
            comparison_mode=True,
        ),
        ResearchPlan(research_profile="general", questions=["Compare providers"]),
        EvidencePack(),
    )

    assert "Produce a strict comparison matrix" in user_prompt
    assert "exactly one Markdown table" in user_prompt
    assert "Insufficient evidence" in user_prompt
    assert "Produce a detailed architectural report" not in user_prompt
    assert "Produce an elaborative, source-grounded chat answer by default" not in user_prompt


def test_comparison_mode_judge_repairs_answer_without_table():
    request = TurnRequest(
        message="Compare AWS Bedrock, Snowflake Cortex, and Azure OpenAI on governance.",
        comparison_mode=True,
    )
    plan = ResearchPlan(questions=["Compare providers"], judge_threshold=0.72)
    evidence = EvidencePack(
        coverage=1.0,
        items=[EvidenceItem(source_id=f"S{i}", evidence="provider evidence") for i in range(1, 7)],
    )
    answer = (
        "AWS Bedrock has governance controls [S1]. Snowflake Cortex has governance integrations [S2]. "
        "Azure OpenAI has enterprise governance controls [S3]. The best choice depends on cloud estate [S4]. "
        "AWS Bedrock has governance controls [S1]. Snowflake Cortex has governance integrations [S2]. "
        "Azure OpenAI has enterprise governance controls [S3]. The best choice depends on cloud estate [S4]."
    )

    result = judge_research(
        request,
        plan,
        evidence,
        answer,
        CoverageContract(subjects=["AWS Bedrock", "Snowflake Cortex", "Azure OpenAI"]),
    )

    assert result.status == "repair"
    assert not result.can_publish
    assert any("requires a Markdown table" in issue for issue in result.issues)


def test_comparison_mode_judge_repairs_table_missing_subject_column():
    request = TurnRequest(
        message="Compare AWS Bedrock, Snowflake Cortex, and Azure OpenAI on governance.",
        comparison_mode=True,
    )
    plan = ResearchPlan(questions=["Compare providers"], judge_threshold=0.72)
    evidence = EvidencePack(
        coverage=1.0,
        items=[EvidenceItem(source_id=f"S{i}", evidence="provider evidence") for i in range(1, 7)],
    )
    answer = (
        "Framing sentence [S1].\n\n"
        "| Dimension | AWS Bedrock | Snowflake Cortex |\n"
        "| --- | --- | --- |\n"
        "| Governance | Controls [S1] | Integrations [S2] |\n\n"
        "Recommendation: AWS wins by cloud fit [S3]."
    )

    result = judge_research(
        request,
        plan,
        evidence,
        answer,
        CoverageContract(subjects=["AWS Bedrock", "Snowflake Cortex", "Azure OpenAI"]),
    )

    assert result.status == "repair"
    assert any("fewer subject columns" in issue for issue in result.issues)


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
