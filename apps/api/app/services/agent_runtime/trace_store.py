from __future__ import annotations

import threading

from app.services.agent_runtime.tracing import AgentTrace


class AgentTraceStore:
    """Process-local trace store for active and recently completed turns."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, AgentTrace] = {}

    def get_or_create(self, trace_id: str | None = None) -> AgentTrace:
        trace = AgentTrace(trace_id)
        with self._lock:
            self._items[trace.id] = trace
        return trace

    def put(self, trace: AgentTrace) -> None:
        with self._lock:
            self._items[trace.id] = trace

    def get(self, trace_id: str) -> AgentTrace | None:
        with self._lock:
            return self._items.get(trace_id)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


default_trace_store = AgentTraceStore()
