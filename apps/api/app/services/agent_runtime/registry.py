from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from app.db.models import SessionLocal
from app.services.agent_runtime.db_models import (
    DBAgentDefinition,
    DBGuardrailPolicy,
    DBModelPolicy,
    DBPromptTemplate,
    DBToolDefinition,
)
from app.services.agent_runtime.models import (
    AgentDefinition,
    GuardrailPolicy,
    ModelPolicy,
    PromptTemplate,
    ToolDefinition,
)


T = TypeVar("T", bound=BaseModel)

DEFAULTS_DIR = Path(__file__).parent / "defaults"
logger = logging.getLogger(__name__)


class RegistryNotSeeded(RuntimeError):
    """Raised when the DB registry has not been seeded yet."""


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


def _json_loads(value: str | None, fallback):
    if value is None or value == "":
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def _registry_from_rows(
    *,
    agents: list[DBAgentDefinition],
    prompts: list[DBPromptTemplate],
    model_policies: list[DBModelPolicy],
    tools: list[DBToolDefinition],
    guardrails: list[DBGuardrailPolicy],
) -> RuntimeRegistry:
    registry = RuntimeRegistry(
        agents={
            row.id: AgentDefinition(
                id=row.id,
                name=row.name,
                role=row.role,
                prompt_template_id=row.prompt_template_id,
                allowed_tools=_json_loads(row.allowed_tools, []),
                model_policy_id=row.model_policy_id,
                guardrail_policy_ids=_json_loads(row.guardrail_policy_ids, []),
                judge_policy_id=row.judge_policy_id,
                max_iterations=row.max_iterations,
                max_tool_calls=row.max_tool_calls,
                enabled=row.enabled,
                version=row.version,
            )
            for row in agents
        },
        prompts={
            row.id: PromptTemplate(
                id=row.id,
                agent_id=row.agent_id,
                version=row.version,
                system_prompt=row.system_prompt,
                developer_prompt=row.developer_prompt,
                output_schema=_json_loads(row.output_schema, None) if row.output_schema else None,
                variables=_json_loads(row.variables, []),
                status=row.status,  # type: ignore[arg-type]
            )
            for row in prompts
        },
        model_policies={
            row.id: ModelPolicy(
                id=row.id,
                name=row.name,
                allowed_models=_json_loads(row.allowed_models, []),
                primary_model=row.primary_model,
                fallback_models=_json_loads(row.fallback_models, []),
                max_input_tokens=row.max_input_tokens,
                max_output_tokens=row.max_output_tokens,
                max_cost_usd_per_call=row.max_cost_usd_per_call,
                timeout_ms=row.timeout_ms,
                parallel_fallback_enabled=row.parallel_fallback_enabled,
                quality_modes=_json_loads(row.quality_modes, ["draft", "standard"]),
                sensitive_domain_allowed=row.sensitive_domain_allowed,
                enabled=row.enabled,
            )
            for row in model_policies
        },
        tools={
            row.id: ToolDefinition(
                id=row.id,
                name=row.name,
                description=row.description,
                input_schema=_json_loads(row.input_schema, {}),
                output_schema=_json_loads(row.output_schema, {}),
                allowed_agent_ids=_json_loads(row.allowed_agent_ids, []),
                required_user_roles=_json_loads(row.required_user_roles, []),
                guardrail_policy_ids=_json_loads(row.guardrail_policy_ids, []),
                timeout_ms=row.timeout_ms,
                retry_policy=_json_loads(row.retry_policy, {"max_attempts": 1}),
                idempotent=row.idempotent,
                backend=row.backend,  # type: ignore[arg-type]
                backend_ref=row.backend_ref,
                enabled=row.enabled,
                version=row.version,
            )
            for row in tools
        },
        guardrails={
            row.id: GuardrailPolicy(
                id=row.id,
                name=row.name,
                applies_to=_json_loads(row.applies_to, []),
                checks=_json_loads(row.checks, []),
                action_map=_json_loads(row.action_map, {}),
                severity=row.severity,  # type: ignore[arg-type]
                enabled=row.enabled,
                version=row.version,
            )
            for row in guardrails
        },
    )
    registry.validate_references()
    return registry


def load_registry_from_db(db) -> RuntimeRegistry:
    agents = db.query(DBAgentDefinition).all()
    prompts = db.query(DBPromptTemplate).all()
    model_policies = db.query(DBModelPolicy).all()
    tools = db.query(DBToolDefinition).all()
    guardrails = db.query(DBGuardrailPolicy).all()
    if not all([agents, prompts, model_policies, tools, guardrails]):
        raise RegistryNotSeeded("Runtime registry DB tables are not fully seeded.")
    return _registry_from_rows(
        agents=agents,
        prompts=prompts,
        model_policies=model_policies,
        tools=tools,
        guardrails=guardrails,
    )


def _try_load_from_db(db) -> RuntimeRegistry | None:
    try:
        return load_registry_from_db(db)
    except RegistryNotSeeded:
        return None


def _load_from_files() -> RuntimeRegistry:
    registry = RuntimeRegistry(
        agents=_load_list("agents.json", AgentDefinition),
        model_policies=_load_list("model_policies.json", ModelPolicy),
        prompts=_load_list("prompts.json", PromptTemplate),
        guardrails=_load_list("guardrails.json", GuardrailPolicy),
        tools=_load_list("tools.json", ToolDefinition),
    )
    registry.validate_references()
    return registry


@lru_cache(maxsize=1)
def load_default_registry() -> RuntimeRegistry:
    try:
        db = SessionLocal()
        try:
            registry = _try_load_from_db(db)
            if registry is not None:
                return registry
        finally:
            db.close()
    except Exception:
        logger.warning("DB registry load failed; falling back to file defaults", exc_info=True)
    return _load_from_files()


def invalidate_registry_cache() -> None:
    load_default_registry.cache_clear()


def runtime_registry_payload(registry: RuntimeRegistry | None = None) -> dict[str, list[dict]]:
    registry = registry or load_default_registry()
    return {
        "agents": [item.model_dump(mode="json") for item in registry.agents.values()],
        "model_policies": [item.model_dump(mode="json") for item in registry.model_policies.values()],
        "prompts": [item.model_dump(mode="json") for item in registry.prompts.values()],
        "guardrails": [item.model_dump(mode="json") for item in registry.guardrails.values()],
        "tools": [item.model_dump(mode="json") for item in registry.tools.values()],
    }
