"""Shared quality-mode type for AgentDeck v2.

The value is intentionally defined early and not threaded through the pipeline
yet. Later phases use it to tune design density, judge strictness, and repair
iteration caps without reintroducing ad hoc string literals.
"""

from __future__ import annotations

from typing import Literal

QualityMode = Literal["draft", "standard", "executive"]

DEFAULT_QUALITY_MODE: QualityMode = "standard"
