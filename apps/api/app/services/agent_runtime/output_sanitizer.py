from __future__ import annotations

import re
from typing import Any


INSTRUCTION_PATTERNS = [
    re.compile(r"<tool>.*?</tool>", re.IGNORECASE | re.DOTALL),
    re.compile(r"\[/?INST\]", re.IGNORECASE),
    re.compile(r"\bSYSTEM\s*:\s*.*", re.IGNORECASE),
    re.compile(r"\bDEVELOPER\s*:\s*.*", re.IGNORECASE),
]


def sanitize_text(text: str) -> str:
    cleaned = text
    for pattern in INSTRUCTION_PATTERNS:
        cleaned = pattern.sub("", cleaned)
    return cleaned.strip()


def sanitize_output(value: Any) -> Any:
    if isinstance(value, str):
        return sanitize_text(value)
    if isinstance(value, dict):
        return {key: sanitize_output(item) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize_output(item) for item in value]
    return value
