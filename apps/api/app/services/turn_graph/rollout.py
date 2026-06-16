from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.config import Settings
from app.services.turn_graph.tools import ANSWER_DIRECTLY


GraphRolloutMode = Literal["disabled", "shadow_canary", "authoritative"]


@dataclass(frozen=True)
class GraphRolloutDecision:
    mode: GraphRolloutMode
    record_shadow_trace: bool
    allow_canary_execution: bool
    allow_full_execution: bool
    reason: str

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "record_shadow_trace": self.record_shadow_trace,
            "allow_canary_execution": self.allow_canary_execution,
            "allow_full_execution": self.allow_full_execution,
            "reason": self.reason,
        }


def graph_rollout_decision(settings: Settings, *, tool_name: str | None = None) -> GraphRolloutDecision:
    """Centralize graph cutover rules.

    For now, enabling `turn_graph_enabled` means shadow traces plus the narrow
    no-tool `answer_directly` canary. All expensive/durable tools stay on the
    current pipeline until their graph wrappers have accumulated trace parity.
    """

    if not getattr(settings, "turn_graph_enabled", False):
        return GraphRolloutDecision(
            mode="disabled",
            record_shadow_trace=False,
            allow_canary_execution=False,
            allow_full_execution=False,
            reason="turn_graph_enabled=false",
        )
    if getattr(settings, "turn_graph_authoritative", False):
        return GraphRolloutDecision(
            mode="authoritative",
            record_shadow_trace=True,
            allow_canary_execution=True,
            allow_full_execution=True,
            reason="graph pipeline is authoritative",
        )
    return GraphRolloutDecision(
        mode="shadow_canary",
        record_shadow_trace=True,
        allow_canary_execution=tool_name in {None, ANSWER_DIRECTLY},
        allow_full_execution=False,
        reason="shadow tracing enabled; only answer_directly canary may execute",
    )
