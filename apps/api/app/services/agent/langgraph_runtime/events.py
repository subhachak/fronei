from __future__ import annotations

from typing import Any, Callable

from app.services.agent.models import new_id


ProgressCallback = Callable[..., None]

# Bump this when the graph state schema changes (e.g., Slice 0B → "slice_0b").
# Referenced in every emitted graph event so consumers can detect schema changes.
SLICE_VERSION = "slice_0a"


def emit_graph_event(
    progress: ProgressCallback | None,
    *,
    run_id: str,
    node_name: str,
    message: str,
    **data: Any,
) -> None:
    if progress is None:
        return
    progress(
        node_name,
        message,
        event_id=new_id("lgevt"),
        run_id=run_id,
        node_name=node_name,
        attempt=1,
        state_version=SLICE_VERSION,
        budget_snapshot={},
        **data,
    )
