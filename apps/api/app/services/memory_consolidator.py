from __future__ import annotations

import json
import logging
import math
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from litellm import completion
from sqlalchemy import distinct

from app.db.models import Conversation, SessionLocal, TwinProfile, UserMemory, UserProfile, WritingSample

logger = logging.getLogger(__name__)

_MODEL = "gemini/gemini-2.5-flash"
_MAX_MEMORIES = 80

_PROMPT = """\
You maintain a compact personal profile for a user of an AI workbench.

Inputs:
- existing profile JSON
- active memories grouped by category, with confidence, seen_count, recency, and pinned status
- writing style context from TwinProfile and recent writing samples, when available

Update the profile JSON. Rules:
- Recency, higher confidence, and repeated memories should carry more weight.
- Pinned memories are authoritative and must not be contradicted.
- Do not invent facts. Omit unknown fields or use empty arrays.
- Keep it concise. This profile is used in prompts, not as a biography.
- Preserve any existing `overrides` object exactly.
- Fold writing style context into `communication_style`. When a style
  fingerprint is provided, keep the same useful keys: sentence_length,
  formality, directness, hedging, structure, technical_depth, preferred_phrases,
  forbidden_phrases, avoid_patterns, signature_patterns, tone_by_audience.

Return ONLY valid JSON with this shape:
{
  "bio": {},
  "role": null,
  "company": null,
  "location": null,
  "active_projects": [],
  "key_preferences": [],
  "constraints": [],
  "communication_style": {},
  "work_context": [],
  "personal_context": [],
  "overrides": {}
}
"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_json(raw: str) -> dict | None:
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group())
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


def _load_profile_json(profile: UserProfile | None) -> dict:
    if not profile or not profile.profile_json:
        return {}
    try:
        data = json.loads(profile.profile_json)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def _memory_payload(memories: list[UserMemory], now: datetime) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for memory in memories[:_MAX_MEMORIES]:
        last_seen = memory.last_seen_at or memory.updated_at or memory.created_at
        if last_seen and last_seen.tzinfo is None:
            last_seen = last_seen.replace(tzinfo=timezone.utc)
        age_days = ((now - last_seen).total_seconds() / 86400) if last_seen else None
        grouped[memory.category or "general"].append({
            "id": memory.id,
            "content": memory.content,
            "scope": memory.scope or "global",
            "confidence": memory.confidence if memory.confidence is not None else 0.6,
            "seen_count": memory.seen_count or 1,
            "last_seen_at": last_seen.isoformat() if last_seen else None,
            "age_days": round(age_days, 2) if age_days is not None else None,
            "pinned": bool(memory.pinned),
            "source": memory.source or "stated",
        })
    return dict(grouped)


def _style_context(db, user_id: str) -> dict:
    profile = db.query(TwinProfile).filter(TwinProfile.user_id == user_id).first()
    fingerprint = None
    prefs = None
    if profile and profile.fingerprint_json:
        try:
            fingerprint = json.loads(profile.fingerprint_json)
        except json.JSONDecodeError:
            fingerprint = None
    if profile and profile.prefs_json:
        try:
            prefs = json.loads(profile.prefs_json)
        except json.JSONDecodeError:
            prefs = None
    samples = (
        db.query(WritingSample)
        .filter(WritingSample.user_id == user_id)
        .order_by(WritingSample.created_at.desc())
        .limit(5)
        .all()
    )
    return {
        "fingerprint": fingerprint if isinstance(fingerprint, dict) else None,
        "prefs": prefs if isinstance(prefs, dict) else None,
        "recent_samples": [
            {
                "label": sample.label,
                "char_count": sample.char_count,
                "excerpt": sample.content[:500],
            }
            for sample in samples
        ],
    }


def _merge_style_fallback(profile_json: dict, style_context: dict) -> dict:
    fingerprint = style_context.get("fingerprint")
    if isinstance(fingerprint, dict) and fingerprint and not profile_json.get("communication_style"):
        profile_json["communication_style"] = fingerprint
    prefs = style_context.get("prefs")
    if isinstance(prefs, dict) and prefs:
        style = profile_json.setdefault("communication_style", {})
        if isinstance(style, dict):
            for key in ["preferred_phrases", "forbidden_phrases", "tone_by_audience"]:
                if key in prefs and key not in style:
                    style[key] = prefs[key]
    return profile_json


def _archive_stale_memories(db, memories: list[UserMemory], now: datetime) -> int:
    archived = 0
    for memory in memories:
        if memory.pinned or (memory.status or "active") != "active":
            continue
        last_seen = memory.last_seen_at or memory.updated_at or memory.created_at
        if last_seen and last_seen.tzinfo is None:
            last_seen = last_seen.replace(tzinfo=timezone.utc)
        if not last_seen:
            continue
        age_days = max(0.0, (now - last_seen).total_seconds() / 86400)
        recency_weight = math.exp(-(memory.decay_rate or 0.05) * age_days)
        if recency_weight < 0.05 and (memory.confidence or 0.0) < 0.5:
            memory.status = "archived"
            memory.updated_at = now
            archived += 1
    if archived:
        db.flush()
    return archived


def _ensure_profile(db, user_id: str, now: datetime) -> UserProfile:
    profile = db.query(UserProfile).filter(UserProfile.user_id == user_id).first()
    if profile:
        return profile
    profile = UserProfile(user_id=user_id, profile_json="{}", created_at=now, updated_at=now)
    db.add(profile)
    db.flush()
    return profile


def consolidate_user(user_id: str) -> dict:
    now = _now()
    db = SessionLocal()
    try:
        profile = _ensure_profile(db, user_id, now)
        memories = (
            db.query(UserMemory)
            .filter(UserMemory.user_id == user_id, UserMemory.status == "active")
            .order_by(UserMemory.pinned.desc(), UserMemory.last_seen_at.desc().nullslast())
            .all()
        )
        existing_profile = _load_profile_json(profile)
        archived = _archive_stale_memories(db, memories, now)
        active_memories = [m for m in memories if (m.status or "active") == "active"]
        style_context = _style_context(db, user_id)
        has_style_context = bool(
            style_context.get("fingerprint")
            or style_context.get("prefs")
            or style_context.get("recent_samples")
        )
        has_style_memories = any(m.category == "communication_style" for m in active_memories)

        if active_memories or has_style_context:
            resp = completion(
                model=_MODEL,
                messages=[
                    {"role": "system", "content": _PROMPT},
                    {"role": "user", "content": json.dumps({
                        "existing_profile": existing_profile,
                        "memories_by_category": _memory_payload(active_memories, now),
                        "style_context": style_context,
                    }, ensure_ascii=False)},
                ],
                temperature=0.1,
                max_tokens=1200,
            )
            raw = (resp.choices[0].message.content or "").strip()
            updated_profile = _parse_json(raw)
            if updated_profile is None:
                updated_profile = existing_profile
        else:
            updated_profile = existing_profile

        updated_profile = _merge_style_fallback(updated_profile, style_context)
        if not has_style_context and not has_style_memories:
            updated_profile.pop("communication_style", None)

        if isinstance(existing_profile.get("overrides"), dict):
            updated_profile["overrides"] = existing_profile["overrides"]

        profile.profile_json = json.dumps(updated_profile, ensure_ascii=False)
        profile.last_consolidated_at = now
        profile.updated_at = now
        db.commit()
        return {"user_id": user_id, "archived": archived, "memory_count": len(active_memories)}
    finally:
        db.close()


def consolidate_user_silent(user_id: str) -> None:
    try:
        consolidate_user(user_id)
    except Exception:
        logger.warning("Profile consolidation failed for %s", user_id, exc_info=True)


def consolidate_all_active_users(since: timedelta = timedelta(hours=24)) -> dict:
    cutoff = _now() - since
    db = SessionLocal()
    try:
        memory_ids = {
            user_id for (user_id,) in (
                db.query(distinct(UserMemory.user_id))
                .filter(UserMemory.last_seen_at >= cutoff)
                .all()
            )
            if user_id
        }
        conversation_ids = {
            user_id for (user_id,) in (
                db.query(distinct(Conversation.user_id))
                .filter(Conversation.updated_at >= cutoff)
                .all()
            )
            if user_id
        }
        user_ids = sorted(memory_ids | conversation_ids)
    finally:
        db.close()

    results = []
    failures = 0
    for user_id in user_ids:
        try:
            results.append(consolidate_user(user_id))
        except Exception:
            failures += 1
            logger.warning("Profile consolidation failed for %s", user_id, exc_info=True)
    return {"users": len(user_ids), "consolidated": len(results), "failures": failures}
