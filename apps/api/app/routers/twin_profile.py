import json
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, HTTPException

from app.auth import CurrentUser
from app.db.models import SessionLocal, TwinProfile, UserProfile, WritingSample
from app.schemas import (
    FingerprintOut,
    TwinProfileOut,
    TwinProfilePrefsUpdate,
    WritingSampleIn,
    WritingSampleOut,
)
from app.services.rate_limit import rate_limiter

router = APIRouter(prefix="/twin-profile", tags=["twin-profile"])


def _fmt(dt: datetime) -> str:
    return dt.isoformat()


def _sample_out(s: WritingSample) -> WritingSampleOut:
    return WritingSampleOut(
        id=s.id, content=s.content, label=s.label,
        char_count=s.char_count, created_at=_fmt(s.created_at),
    )


def _profile_json(profile: UserProfile | None) -> dict:
    if not profile or not profile.profile_json:
        return {}
    try:
        data = json.loads(profile.profile_json)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _fingerprint_from_user_profile(profile: UserProfile | None) -> FingerprintOut | None:
    communication_style = _profile_json(profile).get("communication_style")
    if not isinstance(communication_style, dict) or not communication_style:
        return None
    fingerprint_source = communication_style.get("fingerprint")
    if isinstance(fingerprint_source, dict):
        communication_style = fingerprint_source
    try:
        return FingerprintOut(**communication_style)
    except Exception:
        return None


def _profile_out(profile: TwinProfile | None, sample_count: int, user_profile: UserProfile | None = None, user_id: str = "") -> TwinProfileOut:
    fingerprint = _fingerprint_from_user_profile(user_profile)
    if profile is None:
        return TwinProfileOut(user_id=user_id if fingerprint else "", fingerprint=fingerprint, sample_count=sample_count)
    if profile.fingerprint_json:
        try:
            fingerprint = fingerprint or FingerprintOut(**json.loads(profile.fingerprint_json))
        except Exception:
            pass
    prefs = {}
    if profile.prefs_json:
        try:
            prefs = json.loads(profile.prefs_json)
        except Exception:
            prefs = {}
    return TwinProfileOut(
        user_id=profile.user_id,
        fingerprint=fingerprint,
        rewrite_prompt=profile.rewrite_prompt,
        prefs=prefs,
        extracted_at=_fmt(profile.extracted_at) if profile.extracted_at else None,
        sample_count=sample_count,
    )


# ── Profile endpoints ─────────────────────────────────────────────────

@router.get("", response_model=TwinProfileOut)
def get_profile(user_id: str = CurrentUser) -> TwinProfileOut:
    db = SessionLocal()
    try:
        profile = db.query(TwinProfile).filter(TwinProfile.user_id == user_id).first()
        user_profile = db.query(UserProfile).filter(UserProfile.user_id == user_id).first()
        count = db.query(WritingSample).filter(WritingSample.user_id == user_id).count()
        return _profile_out(profile, count, user_profile=user_profile, user_id=user_id)
    finally:
        db.close()


@router.put("/prefs", response_model=TwinProfileOut)
def update_prefs(
    body: TwinProfilePrefsUpdate,
    background_tasks: BackgroundTasks,
    user_id: str = CurrentUser,
) -> TwinProfileOut:
    db = SessionLocal()
    try:
        profile = db.query(TwinProfile).filter(TwinProfile.user_id == user_id).first()
        if not profile:
            profile = TwinProfile(user_id=user_id)
            db.add(profile)
        prefs = json.loads(profile.prefs_json) if profile.prefs_json else {}
        if body.preferred_phrases is not None:
            prefs["preferred_phrases"] = body.preferred_phrases
        if body.forbidden_phrases is not None:
            prefs["forbidden_phrases"] = body.forbidden_phrases
        if body.tone_by_audience is not None:
            prefs["tone_by_audience"] = body.tone_by_audience
        profile.prefs_json = json.dumps(prefs)
        profile.updated_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(profile)
        background_tasks.add_task(_trigger_consolidation, user_id)
        count = db.query(WritingSample).filter(WritingSample.user_id == user_id).count()
        user_profile = db.query(UserProfile).filter(UserProfile.user_id == user_id).first()
        return _profile_out(profile, count, user_profile=user_profile, user_id=user_id)
    finally:
        db.close()


# ── Writing sample endpoints ──────────────────────────────────────────

@router.get("/samples", response_model=list[WritingSampleOut])
def list_samples(user_id: str = CurrentUser) -> list[WritingSampleOut]:
    db = SessionLocal()
    try:
        samples = (
            db.query(WritingSample)
            .filter(WritingSample.user_id == user_id)
            .order_by(WritingSample.created_at.desc())
            .all()
        )
        return [_sample_out(s) for s in samples]
    finally:
        db.close()


@router.post("/samples", response_model=WritingSampleOut, status_code=201)
def add_sample(
    body: WritingSampleIn,
    background_tasks: BackgroundTasks,
    user_id: str = CurrentUser,
) -> WritingSampleOut:
    db = SessionLocal()
    try:
        content = body.content.strip()
        sample = WritingSample(
            user_id=user_id,
            content=content,
            label=body.label,
            char_count=len(content),
        )
        db.add(sample)
        db.commit()
        db.refresh(sample)
        background_tasks.add_task(_trigger_extraction, user_id)
        return _sample_out(sample)
    finally:
        db.close()


@router.delete("/samples/{sample_id}", status_code=204)
def delete_sample(
    sample_id: int,
    background_tasks: BackgroundTasks,
    user_id: str = CurrentUser,
) -> None:
    db = SessionLocal()
    try:
        s = db.get(WritingSample, sample_id)
        if not s:
            raise HTTPException(status_code=404, detail="Sample not found")
        if s.user_id != user_id:
            raise HTTPException(status_code=403, detail="Forbidden")
        db.delete(s)
        db.commit()
        background_tasks.add_task(_trigger_extraction, user_id)
    finally:
        db.close()


@router.post(
    "/extract",
    status_code=202,
    dependencies=[rate_limiter("twin-extract", "rate_limit_extraction_per_hour", 3600)],
)
def trigger_extraction(
    background_tasks: BackgroundTasks,
    user_id: str = CurrentUser,
) -> dict:
    """Manually trigger fingerprint re-extraction from all samples."""
    background_tasks.add_task(_trigger_extraction, user_id)
    return {"status": "extraction queued"}


def _trigger_extraction(user_id: str) -> None:
    """Called as a background task. Import here to avoid circular imports."""
    from app.services.fingerprint_extractor import extract_and_store

    extract_and_store(user_id)
    _trigger_consolidation(user_id)


def _trigger_consolidation(user_id: str) -> None:
    """Called as a background task. Import here to avoid circular imports."""
    from app.services.memory_consolidator import consolidate_user_silent

    consolidate_user_silent(user_id)
