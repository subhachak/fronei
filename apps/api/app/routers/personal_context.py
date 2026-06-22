from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter

from app.auth import CurrentActiveUser
from app.db.models import SessionLocal, UserProfile
from app.schemas import PersonalProfileOut, PersonalProfileUpdate

router = APIRouter(prefix="/personal-context", tags=["personal-context"])


def _fmt(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _load_profile_json(profile: UserProfile | None) -> dict[str, Any]:
    if not profile or not profile.profile_json:
        return {}
    try:
        data = json.loads(profile.profile_json)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def _ensure_profile(db, user_id: str, now: datetime) -> UserProfile:
    profile = db.query(UserProfile).filter(UserProfile.user_id == user_id).first()
    if profile:
        return profile
    profile = UserProfile(user_id=user_id, profile_json="{}", created_at=now, updated_at=now)
    db.add(profile)
    db.flush()
    return profile


@router.get("/profile", response_model=PersonalProfileOut)
def get_profile(user_id: str = CurrentActiveUser) -> PersonalProfileOut:
    db = SessionLocal()
    try:
        profile = db.query(UserProfile).filter(UserProfile.user_id == user_id).first()
        return PersonalProfileOut(
            profile=_load_profile_json(profile),
            last_consolidated_at=_fmt(profile.last_consolidated_at) if profile else None,
        )
    finally:
        db.close()


@router.patch("/profile", response_model=PersonalProfileOut)
def update_profile(body: PersonalProfileUpdate, user_id: str = CurrentActiveUser) -> PersonalProfileOut:
    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        profile = _ensure_profile(db, user_id, now)
        profile_json = _load_profile_json(profile)
        overrides = profile_json.get("overrides")
        if not isinstance(overrides, dict):
            overrides = {}
        for key, value in body.overrides.items():
            if value in ("", None, [], {}):
                overrides.pop(key, None)
            else:
                overrides[key] = value
        profile_json["overrides"] = overrides
        profile.profile_json = json.dumps(profile_json, ensure_ascii=False)
        profile.updated_at = now
        db.commit()
        db.refresh(profile)
        return PersonalProfileOut(
            profile=_load_profile_json(profile),
            last_consolidated_at=_fmt(profile.last_consolidated_at),
        )
    finally:
        db.close()
