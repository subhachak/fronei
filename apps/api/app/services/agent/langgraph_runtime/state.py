from __future__ import annotations

from typing import Any, Literal, TypedDict


GraphNodeName = Literal[
    "brief",
    "subject_derivation",
    "contract",
    "plan",
    "search",
    "rank",
    "read",
    "classify_claims",
    "expand_source_graph",
    "bind",
    "synthesize",
    "verify",
    "judge",
    "repair",
]


class ResearchGraphState(TypedDict, total=False):
    """Minimal Slice 0A graph state.

    This is deliberately smaller than the real Slice 0B state. It exists so
    the compatibility shell can compile and execute without domain logic.
    """

    request_message: str
    visited_nodes: list[str]
    artifacts: dict[str, Any]
    answer: str
    model_used: str
    cost_usd: float
    latency_ms: int
