from __future__ import annotations

from typing import Any, Callable

from app.services.agent.models import new_id


ProgressCallback = Callable[..., None]

# Bump this when the graph state schema changes.
# Consumers can detect schema version mismatches via the state_version field
# present on every emitted graph event.
SLICE_VERSION = "slice_0b"


def emit_graph_event(
    progress: ProgressCallback | None,
    *,
    run_id: str,
    node_name: str,
    message: str,
    **data: Any,
) -> None:
    """Emit a structured graph event with full identity fields.

    Every event carries:
      event_id        — globally unique per-event ID (lgevt prefix)
      run_id          — ties all events in one graph execution together
      node_name       — emitting node (also used as the progress stage key)
      attempt         — monotonic attempt counter (1 for first call; bump on retry)
      state_version   — SLICE_VERSION constant; consumers detect schema changes here
      budget_snapshot — lightweight budget counters snapshot (populated by callers
                        via cost_usd_spent / tool_calls_made / model_calls_made kwargs)
    """
    if progress is None:
        return
    budget_snapshot = {
        k: data.pop(k)
        for k in ("cost_usd_spent", "tool_calls_made", "model_calls_made")
        if k in data
    }
    progress(
        node_name,
        message,
        event_id=new_id("lgevt"),
        run_id=run_id,
        node_name=node_name,
        attempt=1,
        state_version=SLICE_VERSION,
        budget_snapshot=budget_snapshot,
        **data,
    )
