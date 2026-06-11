"""
Streaming anti-slop refinement pass.

Takes a completed raw LLM response and rewrites it in the user's voice
using the stored rewrite_prompt from their TwinProfile.

Streams tokens via the same Generator[str | LLMResult] protocol as llm_gateway.
"""
from typing import Generator

from app.db.models import TwinProfile
from app.services.llm_gateway import LLMResult, _stream_models

_REFINEMENT_MODEL = "claude-haiku-4-5-20251001"
_MIN_WORDS_TO_REFINE = 50
_MAX_INPUT_CHARS = 12000

_MODE_ADDENDUM: dict[str, str] = {
    "default": "Light edit only. Fix obvious AI language. Preserve structure.",
    "client_ready": (
        "External-facing tone. Professional, confident, no jargon. "
        "Remove anything that sounds like AI boilerplate."
    ),
    "exec_ready": (
        "Executive audience. Lead with outcome. Maximum 3 bullets if any. "
        "No technical jargon. Crisp. Under 150 words if possible."
    ),
    "email": (
        "Format as a professional email in first person. "
        "No subject line. Direct opening, clear ask or close."
    ),
    "proposal": (
        "Proposal language. Structured, authoritative. "
        "Clear recommendation, rationale, next step."
    ),
    "architecture": (
        "Technical architecture tone. Precise. "
        "Preserve all technical detail. State trade-offs explicitly."
    ),
    "pushback": (
        "Challenge mode. Be direct and critical. "
        "State the weak assumption first. Don't soften the message."
    ),
}


def should_refine(
    raw_text: str,
    output_mode: str,
    profile: TwinProfile | None,
) -> bool:
    """Return True when the refinement pass should run."""
    if output_mode == "raw":
        return False
    if profile is None or not profile.rewrite_prompt:
        return False
    if len(raw_text.split()) < _MIN_WORDS_TO_REFINE:
        return False
    return True


def stream_refinement(
    raw_text: str,
    profile: TwinProfile,
    output_mode: str,
) -> Generator:
    """
    Stream a refined rewrite of raw_text in the user's voice.
    Yields str tokens then a final LLMResult sentinel — same protocol
    as stream_llm in llm_gateway.py.
    """
    addendum = _MODE_ADDENDUM.get(output_mode, _MODE_ADDENDUM["default"])
    system_prompt = f"{profile.rewrite_prompt}\n\nMODE: {addendum}"

    text_input = raw_text[:_MAX_INPUT_CHARS]
    msgs = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": text_input},
    ]
    yield from _stream_models(
        models=[_REFINEMENT_MODEL, "gemini/gemini-2.5-flash"],
        msgs=msgs,
        max_tokens=4096,
        enable_native_search=False,
    )
