from __future__ import annotations

import json
import logging
import threading

from app.db.models import AgentTraceRow, SessionLocal
from app.services.agent_runtime.health_monitor import TraceHealthMonitor, default_health_monitor
from app.services.agent_runtime.tracing import AgentTrace


logger = logging.getLogger(__name__)


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

    def complete_run(
        self,
        trace: AgentTrace,
        *,
        health_monitor: TraceHealthMonitor | None = None,
    ) -> None:
        (health_monitor or default_health_monitor).ingest(trace)
        self.put(trace)
        threading.Thread(target=self._flush_to_db, args=(trace,), daemon=True).start()

    def _flush_to_db(self, trace: AgentTrace) -> None:
        try:
            with SessionLocal() as db:
                row = db.query(AgentTraceRow).filter(AgentTraceRow.id == trace.id).first()
                data_json = json.dumps(trace.model_dump(mode="json"))
                if row:
                    row.data_json = data_json
                else:
                    db.add(AgentTraceRow(id=trace.id, data_json=data_json))
                db.commit()
        except Exception:
            logger.warning("Trace DB flush failed for trace %s", trace.id, exc_info=True)


default_trace_store = AgentTraceStore()
