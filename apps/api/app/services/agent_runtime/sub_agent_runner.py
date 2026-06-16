from __future__ import annotations

import logging
from typing import Any

from app.services.agent_runtime.budget_guard import BudgetExceeded, RuntimeBudgetGuard
from app.services.agent_runtime.circuit_breaker import CircuitBreakerRegistry, CircuitOpen
from app.services.agent_runtime.guardrails import GuardrailService
from app.services.agent_runtime.model_fallback import invoke_with_policy_fallback
from app.services.agent_runtime.output_sanitizer import sanitize_text
from app.services.agent_runtime.registry import RuntimeRegistry
from app.services.agent_runtime.tool_runner import ToolCallResult, ToolRunner
from app.services.agent_runtime.tracing import AgentRunTrace, AgentStepTrace, AgentTrace


logger = logging.getLogger(__name__)


class SubAgentRunner:
    """Isolated execution context for one declared sub-agent."""

    def __init__(
        self,
        agent_id: str,
        registry: RuntimeRegistry,
        *,
        trace: AgentTrace | None = None,
        budget_guard: RuntimeBudgetGuard | None = None,
    ) -> None:
        self.agent_id = agent_id
        self.registry = registry
        self.agent_def = registry.agent(agent_id)
        self.model_policy = registry.model_policy(self.agent_def.model_policy_id)
        self.prompt_def = registry.prompt(self.agent_def.prompt_template_id)
        self.trace = trace
        self.trace_run: AgentRunTrace | None = trace.start_run(agent_id) if trace else None
        self.budget_guard = budget_guard
        self.tool_runner = ToolRunner(
            registry=registry,
            agent_id=agent_id,
            guardrail_service=GuardrailService(registry),
            budget_guard=budget_guard,
            trace=trace,
            trace_run=self.trace_run,
        )

    @property
    def system_prompt(self) -> str:
        return self.prompt_def.system_prompt or ""

    @property
    def developer_prompt(self) -> str | None:
        return self.prompt_def.developer_prompt or None

    @property
    def is_claude(self) -> bool:
        model = self.model_policy.primary_model or ""
        return "claude" in model.lower()

    @property
    def route(self):
        from app.services.agent_runtime.adapters import model_policy_to_route

        return model_policy_to_route(self.model_policy)

    def build_messages(self, user_content: str) -> list[dict[str, str]]:
        messages: list[dict[str, str]] = [{"role": "system", "content": self.system_prompt}]
        if self.developer_prompt:
            role = "developer" if self.is_claude else "system"
            messages.append({"role": role, "content": self.developer_prompt})
        messages.append({"role": "user", "content": user_content})
        return messages

    def invoke(self, message: str, **kwargs: Any) -> Any:
        from app.services.llm_gateway import invoke_llm

        if self.budget_guard:
            self.budget_guard.check_model_call()

        def _call(route):
            return invoke_llm(message=message, route=route, **kwargs)

        breaker = CircuitBreakerRegistry.get().breaker(f"llm:{self.model_policy.primary_model}")

        if self.trace and self.trace_run:
            with self.trace.step(self.trace_run, "model", input_summary=message) as step:
                result = breaker.call(lambda: invoke_with_policy_fallback(self.model_policy, _call))
                return self._record_and_return(result, step)

        try:
            result = breaker.call(lambda: invoke_with_policy_fallback(self.model_policy, _call))
        except CircuitOpen:
            logger.warning("Circuit open for agent %s model %s", self.agent_id, self.model_policy.primary_model)
            raise
        return self._record_and_return(result)

    def invoke_json(self, messages: list[dict[str, str]]) -> Any:
        from app.services.llm_gateway import invoke_llm_json

        if self.budget_guard:
            self.budget_guard.check_model_call()

        def _call(route):
            return invoke_llm_json(messages, route)

        summary = "\n".join(str(m.get("content", "")) for m in messages[-2:])[:500]
        breaker = CircuitBreakerRegistry.get().breaker(f"llm:{self.model_policy.primary_model}")
        if self.trace and self.trace_run:
            with self.trace.step(self.trace_run, "model", input_summary=summary) as step:
                result = breaker.call(lambda: invoke_with_policy_fallback(self.model_policy, _call))
                return self._record_and_return(result, step)

        try:
            result = breaker.call(lambda: invoke_with_policy_fallback(self.model_policy, _call))
        except CircuitOpen:
            logger.warning("Circuit open for agent %s model %s", self.agent_id, self.model_policy.primary_model)
            raise
        return self._record_and_return(result)

    def _record_and_return(self, result: Any, step: AgentStepTrace | None = None) -> Any:
        answer = getattr(result, "answer", None)
        if isinstance(answer, str):
            result.answer = sanitize_text(answer)
        cost = float(getattr(result, "estimated_cost_usd", 0.0) or 0.0)
        if step is not None:
            step.model_used = getattr(result, "model_used", None)
            step.output_summary = str(getattr(result, "answer", "") or "")[:500]
            step.cost_usd = cost
        if self.budget_guard:
            self.budget_guard.record_cost(cost)
        return result

    def run_tool(
        self,
        tool_name: str,
        args: dict[str, Any],
        *,
        state: Any,
        plan: dict | None = None,
    ) -> ToolCallResult:
        try:
            breaker = CircuitBreakerRegistry.get().breaker(f"tool:{tool_name}")
            return breaker.call(lambda: self.tool_runner.run(tool_name, args, state=state, plan=plan))
        except CircuitOpen:
            logger.warning("Tool circuit open for agent %s tool %s", self.agent_id, tool_name)
            raise
        except BudgetExceeded:
            logger.warning("Sub-agent %s exceeded runtime budget during %s", self.agent_id, tool_name)
            raise
