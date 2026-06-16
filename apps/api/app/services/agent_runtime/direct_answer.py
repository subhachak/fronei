from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.services.llm_gateway import LLMResult, invoke_llm
from app.services.turn_graph.state import TurnGraphState


@dataclass
class DirectAnswerResult:
    answer: str
    model_used: str
    prompt_id: str
    latency_ms: int
    cost_usd: float
    run_id: str


class DirectAnswerAgent:
    """Produce a direct text answer using registry-selected prompt/model policy."""

    def __init__(self, registry):
        self.registry = registry
        self.agent_def = registry.agent("direct_answer_agent")
        self.model_policy = registry.model_policy(self.agent_def.model_policy_id)
        self.prompt = registry.prompt(self.agent_def.prompt_template_id)

    def answer(self, state: TurnGraphState) -> DirectAnswerResult:
        from app.services.agent_runtime.orchestrator import _model_policy_to_route

        result: LLMResult = invoke_llm(
            message=state.user_message,
            route=_model_policy_to_route(self.model_policy),
            history=state.history[-8:] if state.history else [],
            web_context=_web_context_text(state),
            planner_context=state.running_summary or None,
        )
        return DirectAnswerResult(
            answer=result.answer,
            model_used=result.model_used,
            prompt_id=self.prompt.id,
            latency_ms=result.latency_ms,
            cost_usd=result.estimated_cost_usd or 0.0,
            run_id=str(uuid.uuid4()),
        )


def _web_context_text(state: TurnGraphState) -> str | None:
    web_context = state.web_context
    if not isinstance(web_context, dict):
        return None
    snippets = web_context.get("snippets") or web_context.get("results") or []
    if isinstance(snippets, list):
        return "\n\n".join(str(s.get("content") or s.get("text") or s) for s in snippets[:5])
    return str(web_context) if web_context else None
