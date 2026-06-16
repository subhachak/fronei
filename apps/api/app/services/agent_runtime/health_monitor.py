from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.services.agent_runtime.tracing import AgentTrace


@dataclass
class HealthSnapshot:
    p95_latency_ms: float = 0.0
    error_rate: float = 0.0
    repair_rate: float = 0.0
    sample_count: int = 0


class TraceHealthMonitor:
    """Rolling-window health metrics from completed AgentTrace objects."""

    LATENCY_WINDOW_S: float = 300.0
    ERROR_WINDOW_S: float = 120.0

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._latencies: deque[tuple[float, float]] = deque()
        self._outcomes: deque[tuple[float, str]] = deque()

    def ingest(self, trace: "AgentTrace") -> None:
        now = time.monotonic()
        with self._lock:
            for run in trace.runs:
                self._latencies.append((now, float(run.latency_ms or 0)))
                status = "error" if run.status == "failed" else "ok"
                if any(step.step_type == "repair" for step in run.steps):
                    status = "repair"
                self._outcomes.append((now, status))
            self._prune(now)

    def snapshot(self) -> HealthSnapshot:
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            latencies = [latency for _, latency in self._latencies]
            outcomes = [outcome for _, outcome in self._outcomes]

        if not latencies:
            return HealthSnapshot()
        latencies.sort()
        p95_idx = max(0, int(len(latencies) * 0.95) - 1)
        return HealthSnapshot(
            p95_latency_ms=latencies[p95_idx],
            error_rate=outcomes.count("error") / len(outcomes) if outcomes else 0.0,
            repair_rate=outcomes.count("repair") / len(outcomes) if outcomes else 0.0,
            sample_count=len(latencies),
        )

    def _prune(self, now: float) -> None:
        while self._latencies and now - self._latencies[0][0] > self.LATENCY_WINDOW_S:
            self._latencies.popleft()
        while self._outcomes and now - self._outcomes[0][0] > self.ERROR_WINDOW_S:
            self._outcomes.popleft()


default_health_monitor = TraceHealthMonitor()
