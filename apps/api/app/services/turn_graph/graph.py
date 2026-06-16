from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from app.services.turn_graph.state import TurnGraphState


GraphShellHandler = Callable[[TurnGraphState], TurnGraphState | dict[str, Any] | None]


def run_turn_graph_shell(
    state: TurnGraphState,
    *,
    existing_pipeline: GraphShellHandler | None = None,
) -> TurnGraphState:
    """Run the first feature-flag-safe turn graph shell.

    This is not the final LangGraph runtime. It is the compatibility layer that
    lets us introduce a canonical state, graph events, and node timings while
    reusing the existing pipeline as a single node. The next phase can replace
    `execute_existing_pipeline` with real LangGraph nodes one at a time.
    """

    state.status = "running"
    state.add_event("start", "started", "Turn graph shell started")

    started = time.perf_counter()
    state.add_event("execute_existing_pipeline", "started", "Delegating to existing pipeline")
    try:
        result = existing_pipeline(state) if existing_pipeline else None
        if isinstance(result, TurnGraphState):
            state = result
        elif isinstance(result, dict):
            for key, value in result.items():
                if hasattr(state, key):
                    setattr(state, key, value)
        state.add_timing(
            "execute_existing_pipeline",
            "completed",
            int((time.perf_counter() - started) * 1000),
        )
        state.add_event("execute_existing_pipeline", "completed", "Existing pipeline completed")
        if state.status == "running":
            state.status = "completed"
    except Exception as exc:
        state.error = str(exc)
        state.status = "failed"
        state.add_timing(
            "execute_existing_pipeline",
            "failed",
            int((time.perf_counter() - started) * 1000),
            error=state.error,
        )
        state.add_event("execute_existing_pipeline", "failed", state.error)

    state.add_event("end", state.status, "Turn graph shell finished")
    return state
