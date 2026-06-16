from __future__ import annotations

import logging
from typing import Any

from app.services.agent_runtime.guardrails import GuardrailService
from app.services.agent_runtime.registry import RuntimeRegistry
from app.services.agent_runtime.tool_runner import ToolCallResult, ToolRunner


logger = logging.getLogger(__name__)


class SubAgentRunner:
    """Isolated execution context for one declared sub-agent."""

    def __init__(self, agent_id: str, registry: RuntimeRegistry) -> None:
        self.agent_id = agent_id
        self.registry = registry
        self.agent_def = registry.agent(agent_id)
        self.model_policy = registry.model_policy(self.agent_def.model_policy_id)
        self.prompt_def = registry.prompt(self.agent_def.prompt_template_id)
        self.tool_runner = ToolRunner(
            registry=registry,
            agent_id=agent_id,
            guardrail_service=GuardrailService(registry),
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

        return invoke_llm(message=message, route=self.route, **kwargs)

    def invoke_json(self, messages: list[dict[str, str]]) -> Any:
        from app.services.llm_gateway import invoke_llm_json

        return invoke_llm_json(messages, self.route)

    def run_tool(
        self,
        tool_name: str,
        args: dict[str, Any],
        *,
        state: Any,
        plan: dict | None = None,
    ) -> ToolCallResult:
        return self.tool_runner.run(tool_name, args, state=state, plan=plan)
