import threading
import time
import json
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from datetime import timedelta

from app.db.models import Base, ResearchClaim, ResearchFinding, ResearchQuestion, ResearchRun, ResearchSource, ResearchSourceCache
from app.services.research_metadata import research_meta_for_run
from app.services.research_orchestrator import (
    CLAIM_EXTRACTOR_MODEL,
    ClaimRecord,
    _cache_category_and_ttl,
    _claim_records_from_cache,
    _claim_records_from_data,
    _confidence,
    _get_cached_source,
    _hard_max_sources,
    _max_iterations,
    _now,
    _planned_questions_cap,
    _store_source_cache,
    _llm_extract_claim_records,
    _llm_extract_claim_records_parallel,
    _credibility,
    _extract_published_year,
    _evaluation_from_data,
    _find_gaps,
    _find_contradictions,
    _freshness,
    _host_counts,
    _make_questions,
    _max_sources_per_host,
    _parse_json_object,
    _persist_research_findings,
    _planned_questions_from_data,
    _apply_thread_contract_status,
    _freshness_is_explicit,
    _primary_source_gaps,
    _query_variants,
    _question_needs_more_sources,
    _thread_source_budget_reached,
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
from app.services.research_evidence import (
    ROLE_ANECDOTAL_CASE,
    ROLE_OPERATIONAL_REALITY,
    ROLE_OFFICIAL_POLICY,
    SOURCE_TIER_ANECDOTAL,
    SOURCE_TIER_EXPERT,
    SOURCE_TIER_OFFICIAL,
    build_source_evidence_metadata,
    classify_source_role_prior,
    classify_source_tier,
    extract_source_dates,
    source_family_for_url,
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


def test_research_evidence_classifies_tier_family_and_role_prior():
    assert source_family_for_url("https://www.uscis.gov/forms/all-forms/how-do-i-request-premium-processing") == "uscis.gov"
    assert classify_source_tier("https://www.uscis.gov/policy", "USCIS policy", "", "government") == SOURCE_TIER_OFFICIAL
    assert classify_source_tier("https://www.lawfully.com/community/posts/abc", "Timeline report", "", "forum") == SOURCE_TIER_ANECDOTAL
    assert classify_source_role_prior(
        "https://www.uscis.gov/processing-times",
        "Processing times",
        "Check current processing time and service center wait time.",
        "government",
    ) == ROLE_OPERATIONAL_REALITY
    assert classify_source_role_prior(
        "https://www.uscis.gov/forms/i-907",
        "Premium processing",
        "Official eligibility guidance and Form I-907 instructions.",
        "government",
    ) == ROLE_OFFICIAL_POLICY


def test_research_evidence_extracts_dates_with_confidence():
    published, updated, confidence = extract_source_dates("Last updated: April 4, 2026")
    assert published is None
    assert updated is not None
    assert updated.year == 2026
    assert confidence == "exact"

    published, updated, confidence = extract_source_dates("This policy changed in 2024 and 2026.")
    assert published is None
    assert updated is not None
    assert updated.year == 2026
    assert confidence == "year"


def test_research_evidence_metadata_admits_anecdotal_operational_sources():
    meta = build_source_evidence_metadata(
        url="https://www.immihelp.com/forum/topic",
        title="H4 EAD timelines",
        content="Users report receipt dates and approval timelines for H4 EAD.",
        source_type="forum",
        credibility=0.35,
        relevance=0.8,
        freshness=0.9,
    )
    assert meta.source_tier == SOURCE_TIER_ANECDOTAL
    assert meta.source_role_prior in {ROLE_ANECDOTAL_CASE, ROLE_OPERATIONAL_REALITY}
    assert meta.admission_status == "admitted"


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


def test_find_contradictions_detects_policy_polarity_by_thread():
    source_a = ResearchSource(id=1, run_id=1, question_id=1, title="Official", url="https://uscis.gov/a", provider="test")
    source_b = ResearchSource(id=2, run_id=1, question_id=1, title="Guide", url="https://example.com/b", provider="test")
    claims = [
        ResearchClaim(
            id=1,
            run_id=1,
            source_id=1,
            claim="H-4 EAD is not eligible for premium processing.",
            claim_type="policy",
        ),
        ResearchClaim(
            id=2,
            run_id=1,
            source_id=2,
            claim="H-4 EAD is eligible for premium processing.",
            claim_type="policy",
        ),
    ]
    contradictions = _find_contradictions(claims, [source_a, source_b])
    assert any("Policy/capability conflict" in c for c in contradictions)


def test_find_contradictions_detects_timeline_range_conflicts():
    source_a = ResearchSource(id=1, run_id=1, question_id=1, title="Official", url="https://uscis.gov/a", provider="test")
    source_b = ResearchSource(id=2, run_id=1, question_id=1, title="Forum", url="https://immihelp.com/b", provider="test")
    claims = [
        ResearchClaim(
            id=1,
            run_id=1,
            source_id=1,
            claim="H-1B premium processing takes 15 calendar days.",
            claim_type="timeline",
        ),
        ResearchClaim(
            id=2,
            run_id=1,
            source_id=2,
            claim="Recent H-4 EAD approvals are taking 4 to 6 months.",
            claim_type="timeline",
        ),
    ]
    contradictions = _find_contradictions(claims, [source_a, source_b])
    assert any("Timeline conflict" in c for c in contradictions)


def test_find_contradictions_ignores_compatible_timeline_ranges():
    source_a = ResearchSource(id=1, run_id=1, question_id=1, title="A", url="https://example.com/a", provider="test")
    source_b = ResearchSource(id=2, run_id=1, question_id=1, title="B", url="https://example.org/b", provider="test")
    claims = [
        ResearchClaim(id=1, run_id=1, source_id=1, claim="Processing is taking 2 to 3 months.", claim_type="timeline"),
        ResearchClaim(id=2, run_id=1, source_id=2, claim="Processing is often taking 8 to 10 weeks.", claim_type="timeline"),
    ]
    contradictions = _find_contradictions(claims, [source_a, source_b])
    assert not any("Timeline conflict" in c for c in contradictions)


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
                "claim_type": "capability",
                "evidence_role": "official_policy",
                "freshness_requirement": "current",
                "required_source_types": ["official_docs"],
                "required_source_tiers": ["tier_1_official"],
                "budget": {"max_rounds": 2, "max_sources": 3},
            }
        ]
    }
    planned = _planned_questions_from_data(data, [("fallback question", "fallback query")], parent_query="current feature support")
    assert planned[0].question == "What are the official capabilities?"
    assert planned[0].search_query == "official capabilities"
    assert planned[0].required_source_types == ["official_docs"]
    assert planned[0].claim_type == "capability"
    assert planned[0].evidence_role == "official_policy"
    assert planned[0].freshness_requirement == "current"
    assert planned[0].required_source_tiers == ["tier_1_official"]
    assert planned[0].budget["max_sources"] == 3


def test_planned_questions_fallback_infers_operational_timeline_thread():
    planned = _planned_questions_from_data(
        None,
        [("Currently how long is H4 EAD taking to approve?", "H4 EAD approval timeline")],
        parent_query="currently H4 EAD processing timeline",
    )
    assert planned[0].claim_type == "timeline"
    assert planned[0].evidence_role == ROLE_OPERATIONAL_REALITY
    assert planned[0].freshness_requirement == "current"


def test_thread_contract_status_records_policy_and_operational_gaps():
    policy_q = ResearchQuestion(
        id=1,
        run_id=1,
        question="Is H4 EAD eligible for premium processing?",
        claim_type="policy",
        evidence_role=ROLE_OFFICIAL_POLICY,
        required_source_tiers_json=json.dumps(["tier_1_official"]),
    )
    timeline_q = ResearchQuestion(
        id=2,
        run_id=1,
        question="How long is H4 EAD taking?",
        claim_type="timeline",
        evidence_role=ROLE_OPERATIONAL_REALITY,
        freshness_requirement="current",
    )
    blog = ResearchSource(
        id=1,
        run_id=1,
        question_id=policy_q.id,
        title="Blog",
        url="https://example-blog.com/h4",
        provider="test",
        source_tier="tier_2_expert",
        source_family="example-blog.com",
        source_role_prior="expert_interpretation",
        credibility_score=0.5,
        relevance_score=0.8,
        freshness_score=0.9,
        admission_status="admitted",
    )
    one_timeline = ResearchSource(
        id=2,
        run_id=1,
        question_id=timeline_q.id,
        title="Forum",
        url="https://www.immihelp.com/h4",
        provider="test",
        source_tier="tier_3_anecdotal",
        source_family="immihelp.com",
        source_role_prior=ROLE_ANECDOTAL_CASE,
        credibility_score=0.35,
        relevance_score=0.8,
        freshness_score=0.9,
        admission_status="admitted",
    )
    gaps = _apply_thread_contract_status([policy_q, timeline_q], [blog, one_timeline])
    assert policy_q.confidence == "low"
    assert "Missing required source tier" in (policy_q.stop_reason or "")
    assert timeline_q.confidence == "medium"
    assert "low independent source diversity" in (timeline_q.stop_reason or "")
    assert len(gaps) == 2


def test_thread_source_budget_reached_uses_planned_budget():
    q = ResearchQuestion(
        id=1,
        run_id=1,
        question="q",
        budget_json=json.dumps({"max_sources": 2}),
    )
    assert _thread_source_budget_reached(q, {1: 2})
    assert not _thread_source_budget_reached(q, {1: 1})


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


def test_research_verifier_runs_for_expert_mode_and_risky_deep_cases():
    assert _should_verify_research("expert") is True
    assert _should_verify_research("deep") is False
    assert _should_verify_research(
        "deep",
        domain="legal_regulatory",
        confidence="medium",
        gaps=[],
        contradictions=[],
        sources=[],
        query="currently how long is H4 EAD taking",
    ) is True
    anecdotal_sources = [
        ResearchSource(
            id=1, run_id=1, title="A", url="https://www.immihelp.com/a", provider="test",
            source_role_prior=ROLE_ANECDOTAL_CASE, source_tier=SOURCE_TIER_ANECDOTAL,
            admission_status="admitted",
        ),
        ResearchSource(
            id=2, run_id=1, title="B", url="https://www.lawfully.com/b", provider="test",
            source_role_prior=ROLE_OPERATIONAL_REALITY, source_tier=SOURCE_TIER_ANECDOTAL,
            admission_status="admitted",
        ),
    ]
    assert _should_verify_research(
        "deep",
        domain="general",
        confidence="medium",
        sources=anecdotal_sources,
    ) is True


def test_question_source_workers_run_in_parallel(monkeypatch):
    lock = threading.Lock()
    active = 0
    max_active = 0

    def fake_search(query, recency=None):
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
    def fake_search(_query, recency=None):
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


def test_research_eval_fixture_cases_have_expected_triage_signals():
    fixture_path = Path(__file__).parent / "fixtures" / "research_eval_cases.json"
    cases = json.loads(fixture_path.read_text())
    assert len(cases) >= 6
    for case in cases:
        query = case["query"]
        strategy = _research_domain_strategy(query)
        if case.get("domain") != "general":
            assert strategy.domain == case["domain"]
        if case.get("freshness_required"):
            assert _freshness_is_explicit(query) or strategy.recency in {"month", "year"}
        planned = _planned_questions_from_data(
            None,
            _make_questions(query, "deep"),
            parent_query=query,
            mode="deep",
        )
        if case.get("anecdotal_evidence") == "primary_for_operational_reality":
            assert any(p.evidence_role == ROLE_OPERATIONAL_REALITY for p in planned)
        if case.get("anecdotal_evidence") == "disallowed_for_recommendation":
            assert all(p.evidence_role != ROLE_ANECDOTAL_CASE for p in planned)


# ---------------------------------------------------------------------------
# Phase 11 — Tiered Research Budgets
# ---------------------------------------------------------------------------

def test_tiered_budgets_increase_from_deep_to_expert():
    assert _hard_max_sources("deep") == 12
    assert _hard_max_sources("expert") == 28
    assert _max_iterations("deep") == 3
    assert _max_iterations("expert") == 4
    assert _planned_questions_cap("deep") == 4
    assert _planned_questions_cap("expert") == 6


def test_planned_questions_respect_mode_cap():
    fallback = [(f"question {i}", f"query {i}") for i in range(10)]
    deep = _planned_questions_from_data(None, fallback, parent_query="q", mode="deep")
    expert = _planned_questions_from_data(None, fallback, parent_query="q", mode="expert")
    assert len(deep) == _planned_questions_cap("deep")
    assert len(expert) == _planned_questions_cap("expert")


# ---------------------------------------------------------------------------
# Phase 5 — Caching / Reuse Layer
# ---------------------------------------------------------------------------

def test_cache_category_and_ttl_classification():
    # Stable: official policy from a tier-1 official source, non-sensitive domain.
    category, ttl = _cache_category_and_ttl(SOURCE_TIER_OFFICIAL, ROLE_OFFICIAL_POLICY, "enterprise_technology")
    assert category == "stable"
    assert ttl == timedelta(days=21)

    # Conservative: sensitive domain overrides tier/role.
    category, ttl = _cache_category_and_ttl(SOURCE_TIER_OFFICIAL, ROLE_OFFICIAL_POLICY, "medical")
    assert category == "conservative"
    assert ttl == timedelta(hours=12)

    category, ttl = _cache_category_and_ttl(SOURCE_TIER_ANECDOTAL, ROLE_ANECDOTAL_CASE, "financial")
    assert category == "conservative"
    assert ttl == timedelta(hours=12)

    # Current: operational/anecdotal evidence in a non-sensitive domain.
    category, ttl = _cache_category_and_ttl(SOURCE_TIER_ANECDOTAL, ROLE_OPERATIONAL_REALITY, "legal_regulatory")
    assert category == "conservative"  # legal_regulatory is sensitive
    assert ttl == timedelta(hours=12)

    category, ttl = _cache_category_and_ttl(SOURCE_TIER_EXPERT, ROLE_OPERATIONAL_REALITY, "enterprise_technology")
    assert category == "current"
    assert ttl == timedelta(hours=4)


def _make_run_and_source(db, **source_kwargs):
    run = ResearchRun(user_id="u1", query="test query", mode="deep", status="running")
    db.add(run)
    db.commit()
    db.refresh(run)

    defaults = dict(
        run_id=run.id,
        title="Official docs",
        url="https://docs.vendor.com/cache-test",
        provider="test",
        source_type="documentation",
        source_tier=SOURCE_TIER_OFFICIAL,
        source_role_prior=ROLE_OFFICIAL_POLICY,
        credibility_score=0.9,
        relevance_score=0.8,
        freshness_score=0.9,
    )
    defaults.update(source_kwargs)
    source = ResearchSource(**defaults)
    db.add(source)
    db.commit()
    db.refresh(source)
    return run, source


def test_store_and_get_cached_source_round_trips_claims():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine)()
    try:
        _run, source = _make_run_and_source(db)
        claims = [
            ClaimRecord(
                claim="Premium processing is available for H-4 EAD filed with I-129.",
                score=0.8,
                quote="Premium processing eligibility now includes Form I-765 H-4 EAD",
                confidence="high",
                claim_type="policy",
                claim_role=ROLE_OFFICIAL_POLICY,
                freshness_risk="low",
            )
        ]
        assert _get_cached_source(db, source.url, "qsig") is None

        _store_source_cache(db, source, claims, "enterprise_technology", "qsig")
        db.commit()

        cached = _get_cached_source(db, source.url, "qsig")
        assert cached is not None
        assert cached.cache_category == "stable"
        assert cached.expires_at - cached.cached_at == timedelta(days=21)

        restored = _claim_records_from_cache(cached)
        assert len(restored) == 1
        assert restored[0].claim == claims[0].claim
        assert restored[0].claim_role == ROLE_OFFICIAL_POLICY
    finally:
        db.close()


def test_get_cached_source_returns_none_when_expired():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine)()
    try:
        _run, source = _make_run_and_source(
            db,
            url="https://www.reddit.com/r/cache-test",
            source_tier=SOURCE_TIER_ANECDOTAL,
            source_role_prior=ROLE_ANECDOTAL_CASE,
        )
        _store_source_cache(db, source, [], "enterprise_technology", "qsig")
        db.commit()

        # Force expiry directly.
        row = db.query(ResearchSourceCache).filter(ResearchSourceCache.url == source.url).first()
        row.expires_at = _now() - timedelta(seconds=1)
        db.commit()

        assert _get_cached_source(db, source.url, "qsig") is None
    finally:
        db.close()


def test_store_source_cache_upserts_by_url():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine)()
    try:
        _run, source = _make_run_and_source(db)
        _store_source_cache(db, source, [], "enterprise_technology", "qsig")
        db.commit()

        source.title = "Updated docs title"
        _store_source_cache(db, source, [], "enterprise_technology", "qsig")
        db.commit()

        rows = db.query(ResearchSourceCache).filter(ResearchSourceCache.url == source.url).all()
        assert len(rows) == 1
        assert rows[0].title == "Updated docs title"
    finally:
        db.close()


def test_store_source_cache_separates_by_query_signature():
    """A cache row for one question must not be reused by a different one."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine)()
    try:
        _run, source = _make_run_and_source(db)
        claims_a = [ClaimRecord(claim="Claim relevant to question A", score=0.8, quote="A", confidence="high", claim_type="policy", claim_role=ROLE_OFFICIAL_POLICY, freshness_risk="low")]
        claims_b = [ClaimRecord(claim="Claim relevant to question B", score=0.8, quote="B", confidence="high", claim_type="policy", claim_role=ROLE_OFFICIAL_POLICY, freshness_risk="low")]

        _store_source_cache(db, source, claims_a, "enterprise_technology", "sig-a")
        db.commit()

        # Different question against the same URL is a cache miss.
        assert _get_cached_source(db, source.url, "sig-b") is None

        _store_source_cache(db, source, claims_b, "enterprise_technology", "sig-b")
        db.commit()

        rows = db.query(ResearchSourceCache).filter(ResearchSourceCache.url == source.url).all()
        assert len(rows) == 2

        cached_a = _get_cached_source(db, source.url, "sig-a")
        cached_b = _get_cached_source(db, source.url, "sig-b")
        assert _claim_records_from_cache(cached_a)[0].claim == claims_a[0].claim
        assert _claim_records_from_cache(cached_b)[0].claim == claims_b[0].claim
    finally:
        db.close()
