"""Text-to-speech and transcription for the CELPIP app.

The only genuinely new capability this feature adds to the codebase. Fronei's
model gateway is LiteLLM text-only, so these calls go directly to OpenAI's
audio endpoints over httpx (already a declared dependency) rather than pulling
in another SDK.

Two deliberate choices:

* **Listening audio is synthesised once, at item-generation time, and cached
  in the blob store.** Synthesising at play time would put a multi-second wait
  in the middle of a timed section, and a network failure would end the
  attempt. An item is not servable until its audio exists.
* **whisper-1, not a newer transcription model.** It returns word-level
  timestamps, which is the whole basis of the deterministic speaking metrics
  (pace, pause distribution, fillers). A newer model with a cleaner transcript
  and no timings would make the feedback worse, not better.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

TTS_URL = "https://api.openai.com/v1/audio/speech"
STT_URL = "https://api.openai.com/v1/audio/transcriptions"

# OpenAI's TTS input cap. Listening scripts run well under this, but a
# generator that ignores its length bound should fail loudly here rather than
# producing silently truncated audio a learner would be tested on.
MAX_TTS_CHARS = 4000


class SpeechUnavailable(RuntimeError):
    """No API key configured, or the provider refused the request."""


@dataclass
class Transcript:
    text: str
    # [{word, start, end}] -- empty when the provider returned no timings.
    words: list[dict] = field(default_factory=list)
    duration_seconds: float = 0.0
    model: str = ""


def _api_key() -> str:
    key = (get_settings().openai_api_key or "").strip()
    if not key:
        raise SpeechUnavailable(
            "OPENAI_API_KEY is not configured; CELPIP listening audio and speaking "
            "transcription are unavailable."
        )
    return key


def voice_for(gender_hint: str, index: int) -> str:
    """Pick a distinct voice for the nth speaker of a script."""
    settings = get_settings()
    female = [v.strip() for v in settings.celpip_tts_female_voices.split(",") if v.strip()]
    male = [v.strip() for v in settings.celpip_tts_male_voices.split(",") if v.strip()]
    pool = male if str(gender_hint).strip().lower().startswith("m") else female
    if not pool:
        return "alloy"
    return pool[index % len(pool)]


def synthesize(text: str, *, voice: str, instructions: str = "") -> bytes:
    """Render one span of speech to MP3 bytes."""
    text = (text or "").strip()
    if not text:
        raise ValueError("nothing to synthesize")
    if len(text) > MAX_TTS_CHARS:
        raise ValueError(f"script span is {len(text)} chars; the TTS limit is {MAX_TTS_CHARS}")

    settings = get_settings()
    body: dict = {
        "model": settings.celpip_tts_model,
        "voice": voice,
        "input": text,
        "response_format": "mp3",
    }
    if instructions:
        body["instructions"] = instructions

    with httpx.Client(timeout=settings.celpip_speech_timeout_seconds) as client:
        response = client.post(
            TTS_URL, json=body, headers={"Authorization": f"Bearer {_api_key()}"}
        )
    if response.status_code >= 400:
        raise SpeechUnavailable(
            f"text-to-speech failed ({response.status_code}): {response.text[:300]}"
        )
    return response.content


def transcribe(audio: bytes, *, filename: str = "response.webm") -> Transcript:
    """Transcribe a spoken response, with word timings where available."""
    if not audio:
        raise ValueError("no audio to transcribe")

    settings = get_settings()
    data = {
        "model": settings.celpip_stt_model,
        "response_format": "verbose_json",
        "language": "en",
    }
    files = {"file": (filename, audio, "application/octet-stream")}
    # whisper-1 needs the granularity repeated as a list field; other models
    # ignore it. Sent unconditionally so switching models degrades to a plain
    # transcript rather than erroring.
    payload = [("timestamp_granularities[]", "word"), ("timestamp_granularities[]", "segment")]

    with httpx.Client(timeout=settings.celpip_speech_timeout_seconds) as client:
        response = client.post(
            STT_URL,
            data=[*data.items(), *payload],
            files=files,
            headers={"Authorization": f"Bearer {_api_key()}"},
        )
    if response.status_code >= 400:
        raise SpeechUnavailable(
            f"transcription failed ({response.status_code}): {response.text[:300]}"
        )

    body = response.json()
    words = [
        {
            "word": str(w.get("word", "")),
            "start": float(w.get("start", 0.0)),
            "end": float(w.get("end", 0.0)),
        }
        for w in (body.get("words") or [])
        if isinstance(w, dict)
    ]
    return Transcript(
        text=str(body.get("text", "")).strip(),
        words=words,
        duration_seconds=float(body.get("duration") or 0.0),
        model=settings.celpip_stt_model,
    )


def is_configured() -> bool:
    return bool((get_settings().openai_api_key or "").strip())
