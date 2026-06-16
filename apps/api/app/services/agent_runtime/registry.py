from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from app.services.agent_runtime.models import (
    AgentDefinition,
    GuardrailPolicy,
    ModelPolicy,
    PromptTemplate,
    ToolDefinition,
)


T = TypeVar("T", bound=BaseModel)

DEFAULTS_DIR = Path(__file__).parent / "defaults"


class RuntimeRegistry(BaseModel):
    """File-backed Phase-A registry for inert runtime defaults."""

    agents: dict[str, AgentDefinition]
    model_policies: dict[str, ModelPolicy]
    prompts: dict[str, PromptTemplate]
    guardrails: dict[str, GuardrailPolicy]
    tools: dict[str, ToolDefinition]

    def validate_references(self) -> None:
        missing: list[str] = []
        for agent in self.agents.values():
            if agent.prompt_template_id not in self.prompts:
                missing.append(f"agent {agent.id} prompt {agent.prompt_template_id}")
            if agent.model_policy_id not in self.model_policies:
                missing.append(f"agent {agent.id} model_policy {agent.model_policy_id}")
            for tool_id in agent.allowed_tools:
                if tool_id not in self.tools:
                    missing.append(f"agent {agent.id} tool {tool_id}")
            for policy_id in agent.guardrail_policy_ids:
                if policy_id not in self.guardrails:
                    missing.append(f"agent {agent.id} guardrail {policy_id}")

        for tool in self.tools.values():
            for agent_id in tool.allowed_agent_ids:
                if agent_id not in self.agents and not _is_future_agent(agent_id):
                    missing.append(f"tool {tool.id} allowed_agent {agent_id}")
            for policy_id in tool.guardrail_policy_ids:
                if policy_id not in self.guardrails:
                    missing.append(f"tool {tool.id} guardrail {policy_id}")

        if missing:
            raise ValueError("Invalid runtime registry references: " + ", ".join(missing))

    def agent(self, agent_id: str) -> AgentDefinition:
        return _get(self.agents, agent_id, "agent")

    def tool(self, tool_id: str) -> ToolDefinition:
        return _get(self.tools, tool_id, "tool")

    def prompt(self, prompt_id: str) -> PromptTemplate:
        return _get(self.prompts, prompt_id, "prompt")

    def model_policy(self, policy_id: str) -> ModelPolicy:
        return _get(self.model_policies, policy_id, "model policy")

    def guardrail(self, policy_id: str) -> GuardrailPolicy:
        return _get(self.guardrails, policy_id, "guardrail policy")


def _is_future_agent(agent_id: str) -> bool:
    """Allow Phase-A tools to reference agents planned for later phases."""

    return agent_id in {
        "source_scout",
        "source_reader",
        "content_strategist",
        "evidence_binder",
        "deck_designer",
        "artifact_renderer",
        "repair_agent",
    }


def _get(mapping: dict[str, T], key: str, label: str) -> T:
    try:
        return mapping[key]
    except KeyError:
        raise KeyError(f"Unknown runtime {label}: {key}") from None


def _load_list(filename: str, model: type[T]) -> dict[str, T]:
    raw = json.loads((DEFAULTS_DIR / filename).read_text())
    if isinstance(raw, dict) and "policies" in raw:
        raw = raw["policies"]
    items = [model.model_validate(item) for item in raw]
    return {item.id: item for item in items}


@lru_cache(maxsize=1)
def load_default_registry() -> RuntimeRegistry:
    registry = RuntimeRegistry(
        agents=_load_list("agents.json", AgentDefinition),
        model_policies=_load_list("model_policies.json", ModelPolicy),
        prompts=_load_list("prompts.json", PromptTemplate),
        guardrails=_load_list("guardrails.json", GuardrailPolicy),
        tools=_load_list("tools.json", ToolDefinition),
    )
    registry.validate_references()
    return registry


def runtime_registry_payload(registry: RuntimeRegistry | None = None) -> dict[str, list[dict]]:
    registry = registry or load_default_registry()
    return {
        "agents": [item.model_dump(mode="json") for item in registry.agents.values()],
        "model_policies": [item.model_dump(mode="json") for item in registry.model_policies.values()],
        "prompts": [item.model_dump(mode="json") for item in registry.prompts.values()],
        "guardrails": [item.model_dump(mode="json") for item in registry.guardrails.values()],
        "tools": [item.model_dump(mode="json") for item in registry.tools.values()],
    }
