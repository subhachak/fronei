from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

from app.services.turn_graph.state import TurnGraphState


logger = logging.getLogger(__name__)
OrchestratorRoute = Literal["direct_answer", "clarify", "research", "document"]


@dataclass
class OrchestratorDecision:
    route: OrchestratorRoute
    reasoning: str
    plan: dict[str, Any] = field(default_factory=dict)
    clarification_question: str | None = None
    selected_tools: list[str] = field(default_factory=list)
    model_used: str = ""
    prompt_id: str = ""
    latency_ms: int = 0
    cost_usd: float = 0.0
    run_id: str = field(default_factory=lambda: str(uuid.uuid4()))


class OrchestratorAgent:
    """Phase-D orchestrator: decide a route, but do not execute tools."""

    def __init__(self, registry):
        self.registry = registry
        self.agent_def = registry.agent("orchestrator")
        self.model_policy = registry.model_policy(self.agent_def.model_policy_id)
        self.prompt = registry.prompt(self.agent_def.prompt_template_id)

    def route(self, state: TurnGraphState) -> OrchestratorDecision:
        from app.services.llm_gateway import invoke_llm_json

        route_decision = _model_policy_to_route(self.model_policy)
        result = invoke_llm_json(self._build_messages(state), route_decision)
        decision = _parse_orchestrator_response(result.answer)
        decision.model_used = result.model_used
        decision.prompt_id = self.prompt.id
        decision.latency_ms = result.latency_ms
        decision.cost_usd = result.estimated_cost_usd or 0.0
        return decision

    def _build_messages(self, state: TurnGraphState) -> list[dict]:
        user_payload = json.dumps(
            {
                "user_message": state.user_message,
                "conversation_context": state.running_summary or "",
                "runtime_budget": {
                    "quality_mode": getattr(state, "quality_mode", "standard"),
                },
                "available_tools": self.agent_def.allowed_tools,
            },
            indent=2,
        )
        messages = [{"role": "system", "content": self.prompt.system_prompt}]
        if self.prompt.developer_prompt:
            role = "developer" if self.model_policy.primary_model.startswith("claude") else "system"
            messages.append({"role": role, "content": self.prompt.developer_prompt})
        messages.append({"role": "user", "content": user_payload})
        return messages


def _model_policy_to_route(policy) -> "RouteDecision":
    """Convert a ModelPolicy into the existing gateway RouteDecision."""

    from app.schemas import RouteDecision

    return RouteDecision(
        task_type="planning",
        complexity="low",
        profile="balanced",
        primary_model=policy.primary_model,
        fallbacks=policy.fallback_models,
        reason="orchestrator routing",
    )


def _parse_orchestrator_response(text: str) -> OrchestratorDecision:
    """Parse orchestrator JSON. Never raises; defaults to direct_answer."""

    try:
        raw = json.loads(text)
        route = raw.get("route", "direct_answer") if isinstance(raw, dict) else "direct_answer"
        if route not in {"direct_answer", "clarify", "research", "document"}:
            route = "direct_answer"
        return OrchestratorDecision(
            route=route,
            reasoning=str(raw.get("reasoning", "")) if isinstance(raw, dict) else "",
            plan=raw.get("plan") or {} if isinstance(raw, dict) else {},
            clarification_question=raw.get("clarification_question") if isinstance(raw, dict) else None,
            selected_tools=raw.get("selected_tools") or [] if isinstance(raw, dict) else [],
        )
    except Exception:
        logger.warning("Failed to parse orchestrator response; defaulting to direct_answer")
        return OrchestratorDecision(route="direct_answer", reasoning="parse_failed")
