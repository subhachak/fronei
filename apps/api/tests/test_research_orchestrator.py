import threading
import time

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.models import Base, ResearchClaim, ResearchFinding, ResearchQuestion, ResearchRun, ResearchSource
from app.services.research_metadata import research_meta_for_run
from app.services.research_orchestrator import (
    CLAIM_EXTRACTOR_MODEL,
    ClaimRecord,
    _claim_records_from_data,
    _confidence,
    _llm_extract_claim_records,
    _llm_extract_claim_records_parallel,
    _credibility,
    _extract_published_year,
    _evaluation_from_data,
    _find_gaps,
    _freshness,
    _host_counts,
    _make_questions,
    _max_sources_per_host,
    _parse_json_object,
    _persist_research_findings,
    _planned_questions_from_data,
    _primary_source_gaps,
    _query_variants,
    _question_needs_more_sources,
    _question_primary_source_counts,
    _question_source_counts,
    _relevance,
    _research_domain_strategy,
    _run_question_source_workers,
    _select_diverse_candidates,
    _should_verify_research,
    _is_primary_source,
    _source_quality,
    _source_type,
    _verification_from_data,
)
from app.services.web_context import WebSource


def test_make_questions_adds_expert_contradiction_question():
    pairs = _make_questions("compare AI research tools", "expert")
    assert len(pairs) >= 4
    questions = [q for q, _ in pairs]
    search_queries = [s for _, s in pairs]
    assert any("conflicting evidence" in q for q in questions)
    # search queries must not contain question-framing words that mislead search engines
    assert not any("strongest" in s or "official" in s.lower() for s in search_queries)
    # search queries must be shorter than the questions
    for q, s in pairs:
        assert len(s) <= len(q)


def test_source_scoring_prefers_official_sources():
    official = _credibility("https://docs.example.com/product/release-notes")
    forum = _credibility("https://reddit.com/r/example/comments/1")
    assert official > forum


def test_source_type_detects_primary_source_pages():
    assert _source_type("https://vendor.com/pricing") == "pricing"
    assert _source_type("https://vendor.com/changelog") == "release_notes"
    assert _source_type("https://docs.vendor.com/guide") == "documentation"
    assert _source_type("https://example.gov/policy") == "government"


def test_source_quality_penalizes_seo_commentary():
    query = "vendor ai platform pricing governance"
    official = _source_quality(
        query,
        "Vendor pricing",
        "https://docs.vendor.com/pricing",
        "Official documentation. Last updated 2026-04-01. Pricing governance service limits.",
    )
    seo = _source_quality(
        query,
        "Top 10 best AI platforms",
        "https://example-blog.com/top-10-ai-platforms",
        "Sponsored affiliate ultimate guide mentioning vendor ai platform pricing governance.",
    )
    assert official.quality > seo.quality
    assert official.credibility > seo.credibility
    assert official.source_type == "pricing"


def test_research_domain_strategy_detects_enterprise_technology():
    strategy = _research_domain_strategy("Compare Azure AI Foundry and Amazon Bedrock for enterprise RAG governance")
    assert strategy.domain == "enterprise_technology"
    assert "documentation" in strategy.preferred_source_types
    assert "pricing" in strategy.preferred_source_types


def test_research_domain_strategy_detects_immigration_government_sources():
    strategy = _research_domain_strategy("Current H4 EAD USCIS processing timeline and policy")
    assert strategy.domain == "legal_regulatory"
    assert "government" in strategy.preferred_source_types
    assert "official" in " ".join(strategy.query_suffixes)


def test_query_variants_include_domain_specific_source_terms():
    strategy = _research_domain_strategy("Compare Azure AI Foundry pricing and release notes")
    variants = _query_variants("Azure AI Foundry", 0, strategy, ["pricing", "release_notes"])
    assert any("pricing" in v.lower() for v in variants)
    assert any("release notes" in v.lower() for v in variants)
    assert any("official docs" in v.lower() for v in variants)


def test_source_quality_boosts_domain_preferred_source_type():
    tech = _research_domain_strategy("enterprise API pricing")
    general = _research_domain_strategy("general overview")
    content = "Pricing API governance service limits Last updated 2026-04-01"
    tech_score = _source_quality("API pricing governance", "Pricing", "https://vendor.com/pricing", content, tech)
    general_score = _source_quality("API pricing governance", "Pricing", "https://vendor.com/pricing", content, general)
    assert tech_score.quality >= general_score.quality


def test_freshness_extracts_dates_and_scores_recent_content():
    assert _extract_published_year("Last updated: April 4, 2026") == 2026
    assert _freshness("Last updated: April 4, 2026") >= 0.85
    assert _freshness("Published in 2020") < _freshness("Published in 2026")


def test_relevance_uses_query_overlap():
    high = _relevance("vector database retrieval architecture", "This architecture uses retrieval with a vector database.")
    low = _relevance("vector database retrieval architecture", "The quarterly earnings report was published yesterday.")
    assert high > low


def test_gap_detection_finds_questions_without_claims():
    q1 = ResearchQuestion(id=1, run_id=1, question="covered")
    q2 = ResearchQuestion(id=2, run_id=1, question="missing")
    assert _find_gaps([q1, q2], {1: 2}) == ["missing"]


def test_primary_source_detection_prefers_official_sources():
    official = ResearchSource(
        id=1, run_id=1, question_id=1, title="Docs", url="https://docs.vendor.com/pricing",
        provider="test", source_type="pricing", credibility_score=0.8,
        relevance_score=0.8, freshness_score=1.0,
    )
    commentary = ResearchSource(
        id=2, run_id=1, question_id=1, title="Blog", url="https://example-blog.com/post",
        provider="test", source_type="commentary", credibility_score=0.4,
        relevance_score=0.9, freshness_score=1.0,
    )
    assert _is_primary_source(official)
    assert not _is_primary_source(commentary)


def test_question_needs_more_sources_until_primary_minimum_met():
    q = ResearchQuestion(id=1, run_id=1, question="pricing")
    sources = [
        ResearchSource(
            id=1, run_id=1, question_id=1, title="Blog", url="https://example-blog.com/post",
            provider="test", source_type="commentary", credibility_score=0.4,
            relevance_score=0.9, freshness_score=1.0,
        ),
        ResearchSource(
            id=2, run_id=1, question_id=1, title="News", url="https://news.example.com/post",
            provider="test", source_type="news", credibility_score=0.5,
            relevance_score=0.9, freshness_score=1.0,
        ),
    ]
    source_counts = _question_source_counts(sources, [q])
    primary_counts = _question_primary_source_counts(sources, [q])
    assert _question_needs_more_sources(q, source_counts, primary_counts, total_sources=6, mode="deep")

    sources.append(ResearchSource(
        id=3, run_id=1, question_id=1, title="Docs", url="https://docs.vendor.com/pricing",
        provider="test", source_type="pricing", credibility_score=0.8,
        relevance_score=0.9, freshness_score=1.0,
    ))
    source_counts = _question_source_counts(sources, [q])
    primary_counts = _question_primary_source_counts(sources, [q])
    assert not _question_needs_more_sources(q, source_counts, primary_counts, total_sources=6, mode="deep")
    assert _question_needs_more_sources(q, source_counts, primary_counts, total_sources=6, mode="expert")


def test_primary_source_gaps_name_uncovered_questions():
    q1 = ResearchQuestion(id=1, run_id=1, question="pricing")
    q2 = ResearchQuestion(id=2, run_id=1, question="governance")
    gaps = _primary_source_gaps([q1, q2], {1: 1, 2: 0}, "deep")
    assert gaps == ["governance (needs 1 primary/official source(s))"]


def test_host_counts_group_existing_sources():
    sources = [
        ResearchSource(id=1, run_id=1, title="A", url="https://docs.vendor.com/a", provider="test"),
        ResearchSource(id=2, run_id=1, title="B", url="https://www.docs.vendor.com/b", provider="test"),
        ResearchSource(id=3, run_id=1, title="C", url="https://other.com/c", provider="test"),
    ]
    assert _host_counts(sources) == {"docs.vendor.com": 2, "other.com": 1}


def test_select_diverse_candidates_enforces_host_cap():
    strategy = _research_domain_strategy("Compare Azure AI Foundry and Amazon Bedrock pricing")
    existing = [
        ResearchSource(
            id=i, run_id=1, title=f"Existing {i}", url=f"https://docs.vendor.com/{i}",
            provider="test", source_type="documentation", credibility_score=0.8,
            relevance_score=0.8, freshness_score=1.0,
        )
        for i in range(_max_sources_per_host("expert"))
    ]
    candidates = [
        (
            "test",
            WebSource(
                title="Vendor extra docs",
                url="https://docs.vendor.com/extra",
                content="Official documentation pricing governance Last updated 2026-04-01",
            ),
            1,
        ),
        (
            "test",
            WebSource(
                title="Other vendor docs",
                url="https://docs.other-vendor.com/pricing",
                content="Official documentation pricing governance Last updated 2026-04-01",
            ),
            1,
        ),
    ]
    selected = _select_diverse_candidates(
        "enterprise pricing governance",
        candidates,
        existing,
        strategy,
        "expert",
        {1: ["official_docs", "pricing"]},
        remaining_slots=5,
    )
    assert [s.url for _, s, _ in selected] == ["https://docs.other-vendor.com/pricing"]


def test_confidence_drops_with_gaps():
    assert _confidence(8, 8, [], []) == "high"
    assert _confidence(8, 8, ["missing"], []) == "medium"
    assert _confidence(1, 1, ["missing"], []) == "low"


def test_parse_json_object_strips_markdown_fence():
    assert _parse_json_object('```json\n{"ok": true}\n```') == {"ok": True}
    assert _parse_json_object("not json") is None


def test_planned_questions_from_data_normalizes_plan():
    data = {
        "subquestions": [
            {
                "question": "What are the official capabilities?",
                "search_query": "official capabilities",
                "priority": "high",
                "required_source_types": ["official_docs"],
            }
        ]
    }
    planned = _planned_questions_from_data(data, [("fallback question", "fallback query")])
    assert planned[0].question == "What are the official capabilities?"
    assert planned[0].search_query == "official capabilities"
    assert planned[0].required_source_types == ["official_docs"]


def test_claim_records_from_data_falls_back_on_bad_shape():
    records = _claim_records_from_data({"claims": "bad"}, [("fallback claim", 0.4)])
    assert records[0].claim == "fallback claim"
    assert records[0].score == 0.4


def test_claim_extractor_uses_cheap_model(monkeypatch):
    seen = {}

    def fake_json_model(_system, _user, *, max_tokens=1400, model=None):
        seen["model"] = model
        return {"claims": [{"claim": "Supported fact", "quote": "Supported fact", "relevance_score": 0.8}]}

    monkeypatch.setattr("app.services.research_orchestrator._json_model", fake_json_model)
    source = ResearchSource(
        id=1, run_id=1, title="Docs", url="https://docs.vendor.com/a",
        provider="test", excerpt="Supported fact", source_type="documentation",
    )

    records = _llm_extract_claim_records("research request", source)

    assert records[0].claim == "Supported fact"
    assert seen["model"] == CLAIM_EXTRACTOR_MODEL


def test_claim_extraction_parallelizes_sources(monkeypatch):
    lock = threading.Lock()
    active = 0
    max_active = 0

    def fake_extract(_query, source):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.02)
        with lock:
            active -= 1
        return [ClaimRecord(claim=f"claim {source.id}", score=0.7)]

    monkeypatch.setattr("app.services.research_orchestrator._llm_extract_claim_records", fake_extract)
    sources = [
        ResearchSource(id=i, run_id=1, title=f"S{i}", url=f"https://example.com/{i}", provider="test")
        for i in range(1, 5)
    ]

    records = _llm_extract_claim_records_parallel("query", sources)

    assert set(records) == {1, 2, 3, 4}
    assert max_active > 1


def test_research_verifier_only_runs_for_expert_mode():
    assert _should_verify_research("expert") is True
    assert _should_verify_research("deep") is False


def test_question_source_workers_run_in_parallel(monkeypatch):
    lock = threading.Lock()
    active = 0
    max_active = 0

    def fake_search(query):
        nonlocal active, max_active
        key = query.split()[0]
        with lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.02)
        with lock:
            active -= 1
        return "test", [WebSource(title=query, url=f"https://example.com/{key}", content="official documentation pricing governance")]

    monkeypatch.setattr("app.services.research_orchestrator._search", fake_search)
    monkeypatch.setattr("app.services.research_orchestrator.crawl_url", lambda _url: None)
    questions = [
        ResearchQuestion(id=i, run_id=1, question=f"question {i}", search_query=f"query-{i}")
        for i in range(1, 5)
    ]

    candidates = _run_question_source_workers(
        questions=questions,
        iteration=0,
        strategy=_research_domain_strategy("enterprise pricing governance"),
        required_by_question={},
        seen_urls=set(),
        progress=lambda *_args: None,
    )

    assert len(candidates) == 4
    assert max_active > 1


def test_question_source_workers_dedupe_parallel_results(monkeypatch):
    def fake_search(_query):
        return "test", [WebSource(title="Same", url="https://example.com/same", content="official documentation pricing governance")]

    monkeypatch.setattr("app.services.research_orchestrator._search", fake_search)
    monkeypatch.setattr("app.services.research_orchestrator.crawl_url", lambda _url: None)
    questions = [
        ResearchQuestion(id=1, run_id=1, question="q1", search_query="q1"),
        ResearchQuestion(id=2, run_id=1, question="q2", search_query="q2"),
    ]

    candidates = _run_question_source_workers(
        questions=questions,
        iteration=0,
        strategy=_research_domain_strategy("enterprise pricing governance"),
        required_by_question={},
        seen_urls=set(),
        progress=lambda *_args: None,
    )

    assert len(candidates) == 1


def test_research_findings_are_persisted_and_serialized():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine)()
    try:
        run = ResearchRun(user_id="u1", query="compare platforms", mode="deep", status="running")
        db.add(run)
        db.commit()
        db.refresh(run)

        question = ResearchQuestion(run_id=run.id, question="What is strongest?", search_query="strongest")
        db.add(question)
        db.commit()
        db.refresh(question)

        source = ResearchSource(
            run_id=run.id,
            question_id=question.id,
            title="Official docs",
            url="https://docs.vendor.com/a",
            provider="test",
            source_type="documentation",
            credibility_score=0.9,
            relevance_score=0.8,
            freshness_score=0.9,
        )
        db.add(source)
        db.commit()
        db.refresh(source)

        claim = ResearchClaim(
            run_id=run.id,
            source_id=source.id,
            claim="The platform supports governed RAG workflows.",
            quote="governed RAG workflows",
            confidence="high",
            relevance_score=0.95,
        )
        db.add(claim)
        db.commit()

        _persist_research_findings(db, run, [question], [source], [claim], "high")

        findings = db.query(ResearchFinding).filter(ResearchFinding.run_id == run.id).all()
        assert len(findings) == 1
        assert findings[0].finding == "The platform supports governed RAG workflows."

        meta = research_meta_for_run(db, run)
        assert len(meta.findings) == 1
        assert meta.findings[0].evidence[0]["source_ref"] == "S1"
    finally:
        db.close()


def test_evaluation_from_data_normalizes_followups():
    evaluation = _evaluation_from_data(
        {
            "enough_evidence": False,
            "confidence": "medium",
            "gaps": ["Need official pricing"],
            "follow_up_queries": ["vendor pricing docs"],
            "contradictions": ["Marketing conflicts with docs"],
        },
        [],
        [],
        "low",
    )
    assert evaluation.gaps == ["Need official pricing"]
    assert evaluation.follow_up_queries == ["vendor pricing docs"]
    assert evaluation.contradictions == ["Marketing conflicts with docs"]
    assert evaluation.confidence == "medium"
    assert evaluation.enough_evidence is False


def test_verification_from_data_uses_repaired_answer_without_notes():
    verification = _verification_from_data(
        {
            "verifier_notes": "Removed one unsupported claim.",
            "unsupported_claims": ["Unsupported market-share number"],
            "citation_issues": ["[S1] did not support the pricing claim"],
            "stale_or_overconfident_claims": ["Availability needs date caveat"],
            "verified_answer": "Final repaired answer [S1].",
        },
        "Draft answer.",
    )
    assert verification.verified_answer == "Final repaired answer [S1]."
    assert verification.verifier_notes == "Removed one unsupported claim."
    assert verification.unsupported_claims == ["Unsupported market-share number"]
    assert verification.citation_issues == ["[S1] did not support the pricing claim"]


def test_verification_from_data_falls_back_to_draft():
    verification = _verification_from_data(None, "Draft answer.")
    assert verification.verified_answer == "Draft answer."
    assert "structured output" in verification.verifier_notes
