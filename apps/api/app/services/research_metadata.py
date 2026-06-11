"""Serialization helpers for persisted research runs."""
from __future__ import annotations

import json

from app.db.models import ResearchClaim, ResearchFinding, ResearchQuestion, ResearchRun, ResearchSource
from app.schemas import ResearchClaimOut, ResearchFindingOut, ResearchMeta, ResearchSourceOut


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


def research_meta_for_run(db, run: ResearchRun) -> ResearchMeta:
    sources = (
        db.query(ResearchSource)
        .filter(ResearchSource.run_id == run.id)
        .order_by(ResearchSource.credibility_score.desc(), ResearchSource.relevance_score.desc())
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
    return ResearchMeta(
        run_id=run.id,
        mode=run.mode,
        sources=[
            ResearchSourceOut(
                id=source.id,
                title=source.title,
                url=source.url,
                provider=source.provider,
                credibility_score=source.credibility_score,
                relevance_score=source.relevance_score,
                freshness_score=source.freshness_score,
                source_type=source.source_type,
            )
            for source in sources
        ],
        claims=[
            ResearchClaimOut(
                id=claim.id,
                claim=claim.claim,
                quote=claim.quote,
                confidence=claim.confidence,
                relevance_score=claim.relevance_score,
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
