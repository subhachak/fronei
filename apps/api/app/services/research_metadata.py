"""Serialization helpers for persisted research runs."""
from __future__ import annotations

import json

from app.db.models import ResearchClaim, ResearchFinding, ResearchQuestion, ResearchRun, ResearchSource
from app.schemas import ResearchClaimOut, ResearchFindingOut, ResearchMeta, ResearchQuestionOut, ResearchSourceOut


def _json_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, ValueError, TypeError):
        return []
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _json_any_list(raw: str | None) -> list[dict]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, ValueError, TypeError):
        return []
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _json_str_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, ValueError, TypeError):
        return []
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def _json_dict(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def research_meta_for_run(db, run: ResearchRun) -> ResearchMeta:
    sources = (
        db.query(ResearchSource)
        .filter(ResearchSource.run_id == run.id, ResearchSource.admission_status != "rejected")
        .order_by(ResearchSource.credibility_score.desc(), ResearchSource.relevance_score.desc())
        .all()
    )
    rejected_sources = (
        db.query(ResearchSource)
        .filter(ResearchSource.run_id == run.id, ResearchSource.admission_status == "rejected")
        .order_by(ResearchSource.relevance_score.desc())
        .all()
    )
    source_by_id = {source.id: source for source in sources}
    source_index = {source.id: i for i, source in enumerate(sources, 1)}
    claims = (
        db.query(ResearchClaim)
        .filter(ResearchClaim.run_id == run.id)
        .order_by(ResearchClaim.relevance_score.desc())
        .limit(80)
        .all()
    )
    findings = (
        db.query(ResearchFinding)
        .filter(ResearchFinding.run_id == run.id)
        .order_by(ResearchFinding.id.asc())
        .all()
    )
    questions = (
        db.query(ResearchQuestion)
        .filter(ResearchQuestion.run_id == run.id)
        .order_by(ResearchQuestion.id.asc())
        .all()
    )
    def _source_out(source: ResearchSource) -> ResearchSourceOut:
        return ResearchSourceOut(
            id=source.id,
            title=source.title,
            url=source.url,
            provider=source.provider,
            credibility_score=source.credibility_score,
            relevance_score=source.relevance_score,
            freshness_score=source.freshness_score,
            source_type=source.source_type,
            source_tier=source.source_tier,
            source_family=source.source_family,
            source_role_prior=source.source_role_prior,
            published_at=source.published_at,
            updated_at=source.updated_at,
            source_date_confidence=source.source_date_confidence,
            admission_status=source.admission_status,
            admission_reason=source.admission_reason,
        )

    return ResearchMeta(
        run_id=run.id,
        mode=run.mode,
        sources=[_source_out(source) for source in sources],
        claims=[
            ResearchClaimOut(
                id=claim.id,
                claim=claim.claim,
                quote=claim.quote,
                confidence=claim.confidence,
                relevance_score=claim.relevance_score,
                claim_type=claim.claim_type,
                claim_role=claim.claim_role,
                freshness_risk=claim.freshness_risk,
                source_id=claim.source_id,
                source_ref=f"S{source_index.get(claim.source_id, '?')}",
                source_title=source_by_id.get(claim.source_id).title if source_by_id.get(claim.source_id) else None,
                source_url=source_by_id.get(claim.source_id).url if source_by_id.get(claim.source_id) else None,
            )
            for claim in claims
        ],
        findings=[
            ResearchFindingOut(
                id=finding.id,
                finding=finding.finding,
                evidence=_json_any_list(finding.evidence_json),
                confidence=finding.confidence,
            )
            for finding in findings
        ],
        questions=[question.question for question in questions],
        question_threads=[
            ResearchQuestionOut(
                id=question.id,
                question=question.question,
                search_query=question.search_query,
                status=question.status,
                claim_type=question.claim_type,
                evidence_role=question.evidence_role,
                freshness_requirement=question.freshness_requirement,
                required_source_tiers=_json_str_list(question.required_source_tiers_json),
                budget=_json_dict(question.budget_json),
                stop_reason=question.stop_reason,
                confidence=question.confidence,
            )
            for question in questions
        ],
        rejected_sources=[_source_out(source) for source in rejected_sources],
        gaps=_json_list(run.gaps_json),
        contradictions=_json_list(run.contradictions_json),
        verifier_notes=run.verifier_notes,
        confidence=run.confidence,
    )


def research_meta_for_run_id(db, run_id: int, user_id: str) -> ResearchMeta | None:
    run = db.get(ResearchRun, run_id)
    if not run or run.user_id != user_id:
        return None
    return research_meta_for_run(db, run)
