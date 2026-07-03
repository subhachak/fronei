from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


ContextLayer = Literal["L1", "L2", "L3", "L4"]
ContextScope = Literal["conversation", "workspace", "cross_workspace", "attachment"]
ContextSourceType = Literal[
    "current_message",
    "prior_turn",
    "summary",
    "fact",
    "artifact",
    "profile",
    "attachment",
]


LAYER_L1: ContextLayer = "L1"

SCOPE_CONVERSATION: ContextScope = "conversation"
SCOPE_WORKSPACE: ContextScope = "workspace"
SCOPE_CROSS_WORKSPACE: ContextScope = "cross_workspace"
SCOPE_ATTACHMENT: ContextScope = "attachment"

SOURCE_PRIOR_TURN: ContextSourceType = "prior_turn"
SOURCE_ATTACHMENT: ContextSourceType = "attachment"


class ContextItem(BaseModel):
    layer: ContextLayer
    scope: ContextScope
    source_type: ContextSourceType
    content: str
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    provenance: str = ""
