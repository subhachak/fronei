from __future__ import annotations

import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator, Literal


TraceStepType = Literal["model", "tool", "guardrail", "judge", "repair"]
TraceStatus = Literal["running", "completed", "failed"]


@dataclass
class AgentStepTrace:
    """In-memory trace record for one agent runtime step."""

    id: str
    run_id: str
    agent_id: str
    step_type: TraceStepType
    status: TraceStatus = "running"
    input_summary: str = ""
    output_summary: str = ""
    model_used: str | None = None
    tool_name: str | None = None
    latency_ms: int = 0
    cost_usd: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentRunTrace:
    """In-memory trace record for one agent invocation tree."""

    id: str
    agent_id: str
    parent_run_id: str | None = None
    status: TraceStatus = "running"
    total_cost_usd: float = 0.0
    latency_ms: int = 0
    steps: list[AgentStepTrace] = field(default_factory=list)


class AgentTrace:
    """Small trace accumulator shared by sub-agents, tools, and judges.

    The runtime still persists existing AgentGoal/AgentRunLog rows elsewhere.
    This class gives newer agent code a common object to record into without
    requiring a request DB session at every call site.
    """

    def __init__(self, trace_id: str | None = None) -> None:
        self.id = trace_id or str(uuid.uuid4())
        self.runs: list[AgentRunTrace] = []

    def start_run(self, agent_id: str, *, parent_run_id: str | None = None) -> AgentRunTrace:
        run = AgentRunTrace(id=str(uuid.uuid4()), agent_id=agent_id, parent_run_id=parent_run_id)
        self.runs.append(run)
        return run

    @contextmanager
    def step(
        self,
        run: AgentRunTrace,
        step_type: TraceStepType,
        *,
        input_summary: str = "",
        tool_name: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Iterator[AgentStepTrace]:
        step = AgentStepTrace(
            id=str(uuid.uuid4()),
            run_id=run.id,
            agent_id=run.agent_id,
            step_type=step_type,
            input_summary=input_summary[:500],
            tool_name=tool_name,
            metadata=metadata or {},
        )
        run.steps.append(step)
        started = time.perf_counter()
        try:
            yield step
            step.status = "completed"
        except Exception:
            step.status = "failed"
            raise
        finally:
            step.latency_ms = int((time.perf_counter() - started) * 1000)
            run.latency_ms += step.latency_ms
            run.total_cost_usd += step.cost_usd

    def model_dump(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "runs": [
                {
                    "id": run.id,
                    "agent_id": run.agent_id,
                    "parent_run_id": run.parent_run_id,
                    "status": run.status,
                    "total_cost_usd": run.total_cost_usd,
                    "latency_ms": run.latency_ms,
                    "steps": [step.__dict__ for step in run.steps],
                }
                for run in self.runs
            ],
        }
