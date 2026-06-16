from __future__ import annotations

import json
from typing import Any

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
from app.services.agent_runtime.registry import _load_list, invalidate_registry_cache


def _dump(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _seed_agents(db) -> int:
    items = _load_list("agents.json", AgentDefinition)
    for item in items.values():
        db.merge(DBAgentDefinition(
            id=item.id,
            name=item.name,
            role=item.role,
            prompt_template_id=item.prompt_template_id,
            allowed_tools=_dump(item.allowed_tools),
            model_policy_id=item.model_policy_id,
            guardrail_policy_ids=_dump(item.guardrail_policy_ids),
            judge_policy_id=item.judge_policy_id,
            max_iterations=item.max_iterations,
            max_tool_calls=item.max_tool_calls,
            enabled=item.enabled,
            version=item.version,
        ))
    return len(items)


def _seed_prompts(db) -> int:
    items = _load_list("prompts.json", PromptTemplate)
    for item in items.values():
        db.merge(DBPromptTemplate(
            id=item.id,
            agent_id=item.agent_id,
            version=item.version,
            system_prompt=item.system_prompt,
            developer_prompt=item.developer_prompt,
            output_schema=_dump(item.output_schema) if item.output_schema is not None else None,
            variables=_dump(item.variables),
            status=item.status,
        ))
    return len(items)


def _seed_model_policies(db) -> int:
    items = _load_list("model_policies.json", ModelPolicy)
    for item in items.values():
        db.merge(DBModelPolicy(
            id=item.id,
            name=item.name,
            allowed_models=_dump(item.allowed_models),
            primary_model=item.primary_model,
            fallback_models=_dump(item.fallback_models),
            max_input_tokens=item.max_input_tokens,
            max_output_tokens=item.max_output_tokens,
            max_cost_usd_per_call=item.max_cost_usd_per_call,
            timeout_ms=item.timeout_ms,
            parallel_fallback_enabled=item.parallel_fallback_enabled,
            quality_modes=_dump(item.quality_modes),
            sensitive_domain_allowed=item.sensitive_domain_allowed,
            enabled=item.enabled,
            version="1.0.0",
        ))
    return len(items)


def _seed_tools(db) -> int:
    items = _load_list("tools.json", ToolDefinition)
    for item in items.values():
        db.merge(DBToolDefinition(
            id=item.id,
            name=item.name,
            description=item.description,
            input_schema=_dump(item.input_schema),
            output_schema=_dump(item.output_schema),
            allowed_agent_ids=_dump(item.allowed_agent_ids),
            required_user_roles=_dump(item.required_user_roles),
            guardrail_policy_ids=_dump(item.guardrail_policy_ids),
            timeout_ms=item.timeout_ms,
            retry_policy=_dump(item.retry_policy),
            idempotent=item.idempotent,
            backend=item.backend,
            backend_ref=item.backend_ref,
            enabled=item.enabled,
            version=item.version,
        ))
    return len(items)


def _seed_guardrails(db) -> int:
    items = _load_list("guardrails.json", GuardrailPolicy)
    for item in items.values():
        db.merge(DBGuardrailPolicy(
            id=item.id,
            name=item.name,
            applies_to=_dump(item.applies_to),
            checks=_dump(item.checks),
            action_map=_dump(item.action_map),
            severity=item.severity,
            enabled=item.enabled,
            version=item.version,
        ))
    return len(items)


def seed_registry_from_defaults(db) -> dict[str, int]:
    """
    Populate DB registry tables from defaults/*.json if tables are empty.

    Idempotent at table level: if a table already has rows, it is left alone.
    To update a seeded table, use the admin registry endpoints or a targeted
    db.merge(); re-running this function will not overwrite existing data.
    """

    counts = {
        "agents": 0,
        "prompts": 0,
        "model_policies": 0,
        "tools": 0,
        "guardrails": 0,
    }
    if db.query(DBAgentDefinition).count() == 0:
        counts["agents"] = _seed_agents(db)
    if db.query(DBPromptTemplate).count() == 0:
        counts["prompts"] = _seed_prompts(db)
    if db.query(DBModelPolicy).count() == 0:
        counts["model_policies"] = _seed_model_policies(db)
    if db.query(DBToolDefinition).count() == 0:
        counts["tools"] = _seed_tools(db)
    if db.query(DBGuardrailPolicy).count() == 0:
        counts["guardrails"] = _seed_guardrails(db)
    db.commit()
    invalidate_registry_cache()
    return counts
