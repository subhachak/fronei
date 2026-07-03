from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field

from app.services.agent.context_contracts import (
    LAYER_L1,
    SCOPE_ATTACHMENT,
    SCOPE_CONVERSATION,
    SCOPE_CROSS_WORKSPACE,
    SCOPE_WORKSPACE,
    ContextLayer,
    ContextScope,
)
from app.services.agent.models import TurnRequest
from app.services.agent.grounding import prior_turn_grounded


ContextIntent = Literal[
    "standalone",
    "same_conversation_followup",
    "vague_unresolved_followup",
    "same_workspace_recall",
    "explicit_cross_workspace_recall",
    "live_current_lookup",
    "attachment_context",
]


class ContextBudget(BaseModel):
    max_context_tokens: int = 24_000
    max_latency_ms: int = 1_500


class ContextDecision(BaseModel):
    intent: ContextIntent
    needs_context: bool
    target_scopes: list[ContextScope] = Field(default_factory=list)
    layers: list[ContextLayer] = Field(default_factory=lambda: [LAYER_L1])
    live_search: bool = False
    reason: str
    budget: ContextBudget = Field(default_factory=ContextBudget)


_LIVE_CURRENT_PATTERNS = (
    r"\blatest\b",
    r"\bcurrent\b",
    r"\btoday\b",
    r"\brecent\b",
    r"\bpricing\b",
    r"\bprice\b",
    r"\bwho is\b",
    r"\bwho's\b",
    r"\brelease(?:d)?\b",
    r"\bannounce\b",
    r"\bannounced\b",
    r"\bnews\b",
)

_EXPLICIT_CROSS_WORKSPACE_PATTERNS = (
    r"\bacross (?:my )?workspaces\b",
    r"\ball (?:my )?workspaces\b",
    r"\ball workspace notes\b",
    r"\bother workspace\b",
    r"\banother workspace\b",
    r"\bany workspace\b",
    r"\bcross-workspace\b",
    r"\bin the [\w -]+ workspace\b",
    r"\bfrom the [\w -]+ workspace\b",
    r"\bnotes from the [\w -]+ workspace\b",
)

_WORKSPACE_RECALL_PATTERNS = (
    r"\bproject facts\b",
    r"\bworkspace facts\b",
    r"\bworkspace priorities\b",
    r"\bworkspace memory\b",
    r"\bpinned facts\b",
    r"\bknown facts\b",
    r"\bsaved facts\b",
    r"\bproject notes\b",
    r"\bproject decisions\b",
    r"\bproject assumptions\b",
    r"\bthis project\b",
    r"\bthis workspace\b",
    r"\bproject context\b",
    r"\buse (?:the )?(?:project|workspace) facts\b",
    r"\bwhat did we decide(?: for| on)? (?:this|the) project\b",
)

_VAGUE_FOLLOWUP_PATTERNS = (
    r"\bcircle back\b",
    r"\bthat thing\b",
    r"\bwhere did we land\b",
    r"\bwhat did we decide\b",
    r"\bwhat did you find earlier\b",
    r"\bwhat we discussed\b",
    r"\bsummarize what we discussed\b",
    r"\bresearch it\b",
    r"\bdo it\b",
    r"\bmake it better\b",
    r"\bpick up from\b",
    r"\bgo back to\b",
    r"\bcontinue from\b",
    r"^use that[.!?]?$",
    r"\bsame as before\b",
    r"\bprevious answer\b",
)

_REFERENTIAL_PATTERNS = (
    r"\bthat\b",
    r"\bthis\b",
    r"\bit\b",
    r"\bthey\b",
    r"\bthem\b",
    r"\bthe (?:first|second|third|other|same) one\b",
    r"\bsame question\b",
)


def classify_context_need(request: TurnRequest) -> ContextDecision:
    text = " ".join((request.message or "").lower().split())

    if request.attachment_context and not prior_turn_grounded(request):
        return ContextDecision(
            intent="attachment_context",
            needs_context=True,
            target_scopes=[SCOPE_ATTACHMENT],
            reason="deterministic_rule: attachment_only_context",
        )

    if _matches_any(text, _EXPLICIT_CROSS_WORKSPACE_PATTERNS):
        return ContextDecision(
            intent="explicit_cross_workspace_recall",
            needs_context=True,
            target_scopes=[SCOPE_CROSS_WORKSPACE],
            reason="deterministic_rule: explicit_cross_workspace_recall",
        )

    if _matches_any(text, _WORKSPACE_RECALL_PATTERNS):
        return ContextDecision(
            intent="same_workspace_recall",
            needs_context=True,
            target_scopes=[SCOPE_WORKSPACE],
            reason="deterministic_rule: same_workspace_recall",
        )

    if _matches_any(text, _LIVE_CURRENT_PATTERNS):
        return ContextDecision(
            intent="live_current_lookup",
            needs_context=False,
            target_scopes=[],
            live_search=True,
            reason="deterministic_rule: live_current_lookup",
        )

    if _matches_any(text, _VAGUE_FOLLOWUP_PATTERNS):
        grounded = prior_turn_grounded(request)
        return ContextDecision(
            intent="same_conversation_followup" if grounded else "vague_unresolved_followup",
            needs_context=True,
            target_scopes=[SCOPE_CONVERSATION] if grounded else [],
            reason=(
                "deterministic_rule: same_conversation_followup"
                if grounded
                else "deterministic_rule: vague_unresolved_followup"
            ),
        )

    if prior_turn_grounded(request) and _matches_any(text, _REFERENTIAL_PATTERNS):
        return ContextDecision(
            intent="same_conversation_followup",
            needs_context=True,
            target_scopes=[SCOPE_CONVERSATION],
            reason="deterministic_rule: referential_followup_with_prior_turn",
        )

    return ContextDecision(
        intent="standalone",
        needs_context=False,
        target_scopes=[],
        layers=[],
        reason="deterministic_rule: standalone",
    )


def _matches_any(text: str, patterns: tuple[str, ...]) -> bool:
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns)
