import pytest

from app.services.agent_runtime import (
    AgentRun,
    DurableJob,
    RuntimeBudget,
    RuntimeContext,
    RuntimeTrace,
    ToolDefinition,
    build_goal_from_context,
    load_default_registry,
    runtime_registry_payload,
    runtime_trace_payload,
)
from app.services.agent_runtime.models import ModelPolicy


def test_default_runtime_registry_loads_and_validates_references():
    registry = load_default_registry()

    assert registry.agent("orchestrator").prompt_template_id == "prompt.orchestrator.default"
    assert registry.model_policy("model.fast").primary_model in registry.model_policy("model.fast").allowed_models
    assert registry.prompt("prompt.direct_answer.default").status == "active"
    assert registry.guardrail("tool.ssrf_prevention").severity == "critical"

    web_search = registry.tool("web_search")
    assert web_search.backend == "mcp"
    assert web_search.backend_ref == "mcp.web.search"
    assert "tool.ssrf_prevention" in web_search.guardrail_policy_ids
    assert "direct_answer_agent" in web_search.allowed_agent_ids


def test_runtime_registry_payload_is_json_safe():
    payload = runtime_registry_payload()

    assert {tool["id"] for tool in payload["tools"]} >= {"answer_directly", "web_search", "read_url"}
    assert {agent["id"] for agent in payload["agents"]} >= {"orchestrator", "direct_answer_agent"}


def test_runtime_budget_rejects_negative_limits():
    with pytest.raises(ValueError):
        RuntimeBudget(max_turn_cost_usd=-0.01)

    with pytest.raises(ValueError):
        RuntimeBudget(max_tool_calls=-1)


def test_model_policy_primary_model_must_be_allowed():
    with pytest.raises(ValueError):
        ModelPolicy(
            id="bad",
            name="Bad policy",
            allowed_models=["model-a"],
            primary_model="model-b",
        )


def test_tool_definition_carries_backend_guardrails_and_permissions():
    tool = ToolDefinition(
        id="read_url",
        name="read_url",
        description="Read a public URL.",
        input_schema={"url": "str"},
        output_schema={"content": "str"},
        allowed_agent_ids=["source_reader"],
        guardrail_policy_ids=["tool.ssrf_prevention"],
        backend="mcp",
        backend_ref="mcp.web.search",
    )

    assert tool.backend == "mcp"
    assert tool.timeout_ms == 15_000
    assert tool.retry_policy == {"max_attempts": 1}


def test_runtime_context_builds_inert_goal_trace():
    context = RuntimeContext(
        user_id="user_1",
        conversation_id="conv_1",
        turn_id="turn_1",
        user_message="Explain rate limiting.",
        memory_context="Prefers concise examples.",
    )

    goal = build_goal_from_context(context)

    assert goal.id == "goal_turn_1"
    assert goal.user_id == "user_1"
    assert goal.objective == "Explain rate limiting."
    assert goal.budget.quality_mode == "standard"
    assert goal.status == "created"


def test_runtime_trace_payload_serializes_nested_models():
    context = RuntimeContext(
        user_id="user_1",
        conversation_id="conv_1",
        turn_id="turn_1",
        user_message="Create a short answer.",
    )
    goal = build_goal_from_context(context)
    trace_payload = runtime_trace_payload(RuntimeTrace(goal=goal))

    assert trace_payload["goal"]["id"] == "goal_turn_1"
    assert trace_payload["goal"]["budget"]["quality_mode"] == "standard"


def test_agent_run_and_durable_job_failure_shapes_are_explicit():
    run = AgentRun(
        id="run_1",
        goal_id="goal_1",
        agent_id="research_lead",
        status="budget_exhausted",
        failure_code="budget.limit",
    )
    job = DurableJob(
        id="job_1",
        goal_id="goal_1",
        user_id="user_1",
        conversation_id="conv_1",
        turn_id="turn_1",
        job_type="research",
        idempotency_key="research:goal_1",
    )

    assert run.status == "budget_exhausted"
    assert job.status == "queued"
    assert job.progress_stage == "queued"
