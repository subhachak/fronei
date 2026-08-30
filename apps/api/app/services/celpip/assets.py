"""Builds the media a question needs before it can be served.

Three kinds of item need something rendered:

* **Listening** -- the script becomes audio. Synthesised **per speaker turn**
  (consecutive lines by the same person merged into one call) rather than as
  one file per segment. There is no ffmpeg in this stack, and concatenating
  MP3 frames server-side is the kind of thing that works until it does not.
  Per-turn files let each speaker keep a distinct voice, let the player insert
  a natural beat between turns, and fail granularly -- one turn can be retried
  without re-synthesising a six-minute conversation. The runner preloads the
  whole sequence before the section starts, so playback is gapless.
* **Speaking 3/4/5/8** -- these tasks are answered *about a picture*. A text
  description would mean describing a description, which is a different skill,
  so the image brief is rendered to an actual image.
* **Reading Part 2** -- needs no asset job at all. The diagram is stored as
  structured rows and rendered as a real visual document by the client, which
  is sharper, accessible, and cannot drift from the data the questions were
  keyed against.

A listening item stays `awaiting_assets` until every turn has audio. Serving a
listening item whose audio failed would burn a timed section on a silent
question.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import httpx

from app.config import get_settings
from app.db.models import CelpipQuestion, CelpipQuestionAsset, SessionLocal
from app.services.agent.models import new_id
from app.services.blob_store import get_blob_store
from app.services.celpip import speech
from app.services.celpip.spec import TASKS_BY_KEY

logger = logging.getLogger(__name__)

IMAGE_URL = "https://api.openai.com/v1/images/generations"
IMAGE_MODEL = "gpt-image-1"
IMAGE_SIZE = "1024x1024"

# Tasks whose stimulus is a picture the candidate speaks about.
IMAGE_TASKS = {"speaking_scene", "speaking_predictions", "speaking_comparing", "speaking_unusual"}

# Rough words-per-second for natural speech, used to estimate a turn's
# duration when the provider does not report one. Only drives the player's
# progress bar, never scoring.
WORDS_PER_SECOND = 2.5


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _speaker_turns(payload: dict) -> list[dict]:
    """Flatten a listening script into consecutive same-speaker turns."""
    turns: list[dict] = []
    for segment in (payload.get("stimulus") or {}).get("segments") or []:
        seg_index = int(segment.get("index", 0) or 0)
        genders = {
            str(s.get("name", "")): str(s.get("gender_hint", "female"))
            for s in segment.get("speakers") or []
        }
        order = {name: i for i, name in enumerate(genders)}
        current: dict | None = None
        for line in segment.get("lines") or []:
            speaker = str(line.get("speaker", "")).strip()
            text = str(line.get("text", "")).strip()
            if not text:
                continue
            if current and current["speaker"] == speaker:
                current["text"] = f"{current['text']} {text}"
                continue
            current = {
                "segment_index": seg_index,
                "speaker": speaker,
                "text": text,
                "voice": speech.voice_for(genders.get(speaker, "female"), order.get(speaker, 0)),
            }
            turns.append(current)
    return turns


def _store_blob(key: str, payload: bytes, content_type: str):
    return get_blob_store().put(key, payload, content_type=content_type)


def _build_listening_audio(db, question: CelpipQuestion, payload: dict) -> tuple[int, list[str]]:
    turns = _speaker_turns(payload)
    if not turns:
        return 0, ["listening item has no speakable lines"]

    existing = {
        (a.segment_index, a.text_content): a
        for a in db.query(CelpipQuestionAsset)
        .filter(CelpipQuestionAsset.question_id == question.id)
        .filter(CelpipQuestionAsset.kind == "audio")
        .all()
    }

    built = 0
    errors: list[str] = []
    for order, turn in enumerate(turns):
        prior = existing.get((turn["segment_index"], turn["text"]))
        if prior is not None and prior.status == "ready":
            built += 1
            continue

        asset = prior or CelpipQuestionAsset(
            id=new_id("casset"),
            question_id=question.id,
            kind="audio",
            segment_index=turn["segment_index"],
        )
        asset.text_content = turn["text"]
        asset.voice = turn["voice"]
        asset.status = "pending"
        # Playback order is (segment_index, created_at), so a retried turn
        # keeps its original row and therefore its place in the sequence.
        if prior is None:
            db.add(asset)
        db.commit()

        try:
            audio = speech.synthesize(
                turn["text"],
                voice=turn["voice"],
                instructions=(
                    "Read this as natural spoken Canadian English in a conversation: "
                    "normal pace, ordinary intonation, not a dramatic reading."
                ),
            )
            stored = _store_blob(
                f"celpip/audio/{question.id}/{turn['segment_index']}-{order}.mp3",
                audio,
                "audio/mpeg",
            )
            asset.blob_location = stored.location
            asset.content_type = "audio/mpeg"
            asset.size_bytes = stored.size_bytes
            asset.sha256 = stored.sha256
            asset.duration_seconds = round(len(turn["text"].split()) / WORDS_PER_SECOND, 2)
            asset.status = "ready"
            asset.error = None
            built += 1
        except Exception as exc:
            logger.warning("celpip TTS failed for %s turn %s: %s", question.id, order, exc)
            asset.status = "failed"
            asset.error = str(exc)[:1000]
            errors.append(f"turn {order}: {exc}")
        db.commit()

    return built, errors


def _generate_image(brief: str) -> bytes:
    """Render a speaking-task picture. Returns PNG bytes."""
    import base64

    settings = get_settings()
    key = (settings.openai_api_key or "").strip()
    if not key:
        raise speech.SpeechUnavailable("OPENAI_API_KEY is not configured; cannot render task images.")

    prompt = (
        "A clear, realistic, everyday photograph-style illustration for an English "
        "language test. Ordinary Canadian setting, natural lighting, several people "
        "and objects clearly visible and easy to name. No text, no signage, no "
        "words anywhere in the image, no logos, no recognisable real people.\n\n"
        f"Scene: {brief}"
    )
    with httpx.Client(timeout=settings.celpip_speech_timeout_seconds) as client:
        response = client.post(
            IMAGE_URL,
            json={"model": IMAGE_MODEL, "prompt": prompt, "size": IMAGE_SIZE, "n": 1},
            headers={"Authorization": f"Bearer {key}"},
        )
    if response.status_code >= 400:
        raise speech.SpeechUnavailable(
            f"image generation failed ({response.status_code}): {response.text[:300]}"
        )
    data = (response.json().get("data") or [{}])[0]
    if data.get("b64_json"):
        return base64.b64decode(data["b64_json"])
    if data.get("url"):
        with httpx.Client(timeout=settings.celpip_speech_timeout_seconds) as client:
            return client.get(data["url"]).content
    raise speech.SpeechUnavailable("image generation returned no image")


def _build_task_image(db, question: CelpipQuestion, payload: dict) -> tuple[int, list[str]]:
    brief = str((payload.get("stimulus") or {}).get("image_brief", "")).strip()
    if not brief:
        return 0, ["task needs an image but the item carries no image_brief"]

    prior = (
        db.query(CelpipQuestionAsset)
        .filter(CelpipQuestionAsset.question_id == question.id)
        .filter(CelpipQuestionAsset.kind == "image")
        .first()
    )
    if prior is not None and prior.status == "ready":
        return 1, []

    asset = prior or CelpipQuestionAsset(
        id=new_id("casset"), question_id=question.id, kind="image", segment_index=0
    )
    asset.text_content = brief
    asset.status = "pending"
    if prior is None:
        db.add(asset)
    db.commit()

    try:
        image = _generate_image(brief)
        stored = _store_blob(f"celpip/images/{question.id}.png", image, "image/png")
        asset.blob_location = stored.location
        asset.content_type = "image/png"
        asset.size_bytes = stored.size_bytes
        asset.sha256 = stored.sha256
        asset.status = "ready"
        asset.error = None
        db.commit()
        return 1, []
    except Exception as exc:
        logger.warning("celpip image generation failed for %s: %s", question.id, exc)
        asset.status = "failed"
        asset.error = str(exc)[:1000]
        db.commit()
        return 0, [str(exc)]


def build_assets_for_question(question_id: str) -> dict:
    """Build every asset a question needs, then flip it to `ready` if complete."""
    db = SessionLocal()
    try:
        question = db.get(CelpipQuestion, question_id)
        if question is None:
            raise ValueError(f"question {question_id} not found")
        try:
            payload = json.loads(question.payload_json)
        except (TypeError, ValueError):
            payload = {}

        task = TASKS_BY_KEY.get(question.task_key)
        built = 0
        errors: list[str] = []

        if task and task.skill == "listening":
            built, errors = _build_listening_audio(db, question, payload)
            expected = len(_speaker_turns(payload))
            ready = expected > 0 and built == expected
        elif question.task_key in IMAGE_TASKS:
            built, errors = _build_task_image(db, question, payload)
            ready = built == 1
        else:
            # Reading Part 2's diagram is rendered client-side from structured
            # rows; everything else is pure text.
            ready = True

        if ready and question.status in {"awaiting_assets", "draft"}:
            question.status = "ready"
        elif not ready:
            question.status = "awaiting_assets"
        db.commit()

        return {
            "question_id": question_id,
            "ready": ready,
            "assets_built": built,
            "errors": errors[:10],
        }
    finally:
        db.close()


def asset_payload(db, question_id: str) -> dict:
    """Serialise a question's assets for the runner, newest state first.

    Audio is returned in playback order -- the sequence the player walks
    through -- because per-turn synthesis means order is meaning.
    """
    rows = (
        db.query(CelpipQuestionAsset)
        .filter(CelpipQuestionAsset.question_id == question_id)
        .order_by(CelpipQuestionAsset.segment_index.asc(), CelpipQuestionAsset.created_at.asc())
        .all()
    )
    audio = [
        {
            "id": r.id,
            "segment_index": r.segment_index,
            "speaker_voice": r.voice,
            "duration_seconds": r.duration_seconds,
            "status": r.status,
        }
        for r in rows if r.kind == "audio"
    ]
    image = next(
        (
            {"id": r.id, "status": r.status}
            for r in rows if r.kind == "image" and r.status == "ready"
        ),
        None,
    )
    return {"audio": audio, "image": image}
