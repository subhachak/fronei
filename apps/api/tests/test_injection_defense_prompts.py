"""Verify untrusted-content-ingestion prompts carry injection-defense framing,
matching the pattern already used in profile_consolidator.py's _SYSTEM_PROMPT:
untrusted content is data to analyze, never an instruction to obey.
"""
from __future__ import annotations

from app.services.agent.fast_path import WEB_FAST_PROMPT
from app.services.agent.profile_consolidator import _SYSTEM_PROMPT as PROFILE_SYSTEM_PROMPT
from app.services.agent.research_profiles import SYNTHESIS_PROMPT
from app.services.document_extractor import _EXTRACTION_PROMPT, _IMAGE_PROMPT

_INJECTION_MARKERS = ("instruction", "ignore previous instructions")


def _has_injection_guard(prompt: str) -> bool:
    lowered = prompt.lower()
    return all(marker in lowered for marker in _INJECTION_MARKERS)


def test_profile_consolidator_prompt_has_injection_guard_reference():
    # Sanity check on the reference pattern itself.
    assert _has_injection_guard(PROFILE_SYSTEM_PROMPT)


def test_document_extraction_prompt_guards_against_embedded_instructions():
    assert _has_injection_guard(_EXTRACTION_PROMPT)
    assert "do not comply" in " ".join(_EXTRACTION_PROMPT.lower().split())


def test_image_description_prompt_guards_against_embedded_instructions():
    assert _has_injection_guard(_IMAGE_PROMPT)
    assert "do not comply" in " ".join(_IMAGE_PROMPT.lower().split())


def test_web_fast_prompt_guards_against_embedded_instructions():
    assert _has_injection_guard(WEB_FAST_PROMPT)
    assert "do not comply" in " ".join(WEB_FAST_PROMPT.lower().split())


def test_synthesis_prompt_guards_against_embedded_instructions():
    assert _has_injection_guard(SYNTHESIS_PROMPT)
    assert "do not comply" in " ".join(SYNTHESIS_PROMPT.lower().split())
