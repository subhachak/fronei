from __future__ import annotations

import re


_PRIVATE_PATTERNS = [
    re.compile(r"\b(Authorization|Bearer|api[-_]?key|access[-_]?token)\s*[:=]", re.IGNORECASE),
    re.compile(r"\bSet-Cookie\s*:", re.IGNORECASE),
    re.compile(r"(dashboard|inbox|profile|account|settings|notifications)/", re.IGNORECASE),
    re.compile(r"\bwelcome back\b.{0,40}(your name|account)", re.IGNORECASE),
]

_MIN_CONTENT_CHARS = 50


class SourceClassification:
    def __init__(self, is_public: bool, reason: str | None = None) -> None:
        self.is_public = is_public
        self.reason = reason


def classify_source_content(url: str, content: str) -> SourceClassification:
    """Return is_public=False if source content looks user-specific."""

    if len(content) < _MIN_CONTENT_CHARS:
        return SourceClassification(is_public=True)
    for pattern in _PRIVATE_PATTERNS:
        if pattern.search(content):
            return SourceClassification(
                is_public=False,
                reason=f"private_content_pattern:{pattern.pattern[:40]}",
            )
    return SourceClassification(is_public=True)
