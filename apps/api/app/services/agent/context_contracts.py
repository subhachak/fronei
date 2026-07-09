from __future__ import annotations

from dataclasses import dataclass
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
LAYER_L2: ContextLayer = "L2"
LAYER_L3: ContextLayer = "L3"

SCOPE_CONVERSATION: ContextScope = "conversation"
SCOPE_WORKSPACE: ContextScope = "workspace"
SCOPE_CROSS_WORKSPACE: ContextScope = "cross_workspace"
SCOPE_ATTACHMENT: ContextScope = "attachment"

SOURCE_PRIOR_TURN: ContextSourceType = "prior_turn"
SOURCE_ATTACHMENT: ContextSourceType = "attachment"
SOURCE_SUMMARY: ContextSourceType = "summary"
SOURCE_FACT: ContextSourceType = "fact"


class ContextItem(BaseModel):
    layer: ContextLayer
    scope: ContextScope
    source_type: ContextSourceType
    content: str
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    provenance: str = ""


@dataclass
class ContextTokenBudget:
    """Splits a resolved model's context window across context-assembly
    layers instead of leaving dozens of independent hardcoded item/char
    caps scattered across the codebase.

    "conversation" covers L1 (prior-turn/attachment) + L2 (cross-session
    recall) context_registry.py items; "facts" covers L3 known_facts items;
    "evidence" covers the research pipeline's own EvidencePack content
    (source excerpts, typed claims, architecture cards) assembled in
    research_synthesis.py -- a separate, usually much larger, consumer than
    the L1-L3 context-registry layers.

    conversation_share/facts_share/evidence_share are a starting point, not
    a mandate -- see docs/... admin GET /admin/context-usage for real usage
    data once turns start recording context_tokens_json, and reconsider the
    split if that data suggests it.

    Reserves headroom for the system prompt and expected output tokens
    before splitting what's left across the three shares, so the shares
    apply to what's actually available for context, not the full window.
    """

    total_tokens: int
    conversation_share: float = 0.15
    facts_share: float = 0.25
    evidence_share: float = 0.60
    system_prompt_reserve: int = 1500
    output_reserve: int = 2000

    @property
    def available_tokens(self) -> int:
        """Tokens left for context assembly after system prompt + output headroom."""
        return max(0, self.total_tokens - self.system_prompt_reserve - self.output_reserve)

    @property
    def conversation_tokens(self) -> int:
        return int(self.available_tokens * self.conversation_share)

    @property
    def facts_tokens(self) -> int:
        return int(self.available_tokens * self.facts_share)

    @property
    def evidence_tokens(self) -> int:
        return int(self.available_tokens * self.evidence_share)

    @classmethod
    def for_model(cls, model: str, **overrides) -> "ContextTokenBudget":
        """Build a budget from a resolved model's actual context window."""
        from app.services.agent.model_client import resolve_context_window

        return cls(total_tokens=resolve_context_window(model), **overrides)
