from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.schemas import RouteDecision
from app.services.llm_gateway import LLMResult
from app.services.planner import plan_from_dict
from app.services.turn_graph.state import TurnGraphState


ToolRisk = Literal["low", "medium", "high"]
ToolExecutionMode = Literal["sync", "durable"]


class TurnToolInput(BaseModel):
    """Base input for internal graph tools."""

    query: str = ""
    context: dict[str, Any] = Field(default_factory=dict)


class TurnToolOutput(BaseModel):
    """Base output for internal graph tools."""

    status: Literal["ok", "needs_user", "failed"] = "ok"
    result: dict[str, Any] = Field(default_factory=dict)
    user_message: str = ""
    error: str | None = None


@dataclass(frozen=True)
class TurnToolDef:
    name: str
    description: str
    risk: ToolRisk = "low"
    execution_mode: ToolExecutionMode = "sync"
    requires_confirmation: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "risk": self.risk,
            "execution_mode": self.execution_mode,
            "requires_confirmation": self.requires_confirmation,
        }


ANSWER_DIRECTLY = "answer_directly"
ASK_USER = "ask_user"
WEB_CONTEXT = "web_context"
DEEP_RESEARCH = "deep_research"
GENERATE_DOCUMENT = "generate_document"
RENDER_ARTIFACT = "render_artifact"
QUALITY_CHECK = "quality_check"
LOAD_MEMORY = "load_memory"
LOAD_TEMPLATES = "load_templates"


TOOL_REGISTRY: dict[str, TurnToolDef] = {
    ANSWER_DIRECTLY: TurnToolDef(
        name=ANSWER_DIRECTLY,
        description="Generate a normal chat answer from the current plan and context.",
    ),
    ASK_USER: TurnToolDef(
        name=ASK_USER,
        description="Pause execution and ask clarifying questions or request approval.",
        risk="low",
        requires_confirmation=True,
    ),
    WEB_CONTEXT: TurnToolDef(
        name=WEB_CONTEXT,
        description="Gather lightweight fresh web context for the current answer.",
        risk="medium",
    ),
    DEEP_RESEARCH: TurnToolDef(
        name=DEEP_RESEARCH,
        description="Run the durable deep-research workflow.",
        risk="high",
        execution_mode="durable",
        requires_confirmation=True,
    ),
    GENERATE_DOCUMENT: TurnToolDef(
        name=GENERATE_DOCUMENT,
        description="Generate structured document or presentation content.",
        risk="medium",
        execution_mode="durable",
        requires_confirmation=True,
    ),
    RENDER_ARTIFACT: TurnToolDef(
        name=RENDER_ARTIFACT,
        description="Render generated content into DOCX, PPTX, XLSX, or other artifacts.",
        risk="medium",
        execution_mode="durable",
    ),
    QUALITY_CHECK: TurnToolDef(
        name=QUALITY_CHECK,
        description="Run deterministic or judge-based quality checks on generated artifacts.",
        risk="medium",
        execution_mode="durable",
    ),
    LOAD_MEMORY: TurnToolDef(
        name=LOAD_MEMORY,
        description="Load ranked user memory and personalization context.",
    ),
    LOAD_TEMPLATES: TurnToolDef(
        name=LOAD_TEMPLATES,
        description="Load user document templates and brand/profile hints.",
    ),
}


def get_tool(name: str) -> TurnToolDef:
    try:
        return TOOL_REGISTRY[name]
    except KeyError:
        raise KeyError(f"Unknown turn graph tool: {name}") from None


def tool_registry_payload() -> list[dict[str, Any]]:
    return [tool.to_dict() for tool in TOOL_REGISTRY.values()]


def select_tools_from_state(state: TurnGraphState) -> list[dict[str, Any]]:
    """Derive explicit tool calls from planner/gate state.

    This is the bridge from the old capability flags to the new tool contract.
    It is deterministic and intentionally conservative: when the gate says
    confirmation is needed, `ask_user` is selected before any expensive tool.
    """

    plan = state.plan or {}
    gate = state.gate or {}
    tools: list[str] = []

    if gate.get("mode") == "confirm" or gate.get("open_questions"):
        tools.append(ASK_USER)

    capabilities = gate.get("capabilities") if isinstance(gate, dict) else {}
    if isinstance(capabilities, dict):
        web = capabilities.get("web_search") or {}
        research = capabilities.get("deep_research") or {}
        document = capabilities.get("document") or {}
        if web.get("enabled"):
            tools.append(WEB_CONTEXT)
        if research.get("enabled"):
            tools.append(DEEP_RESEARCH)
        if document.get("enabled"):
            tools.extend([GENERATE_DOCUMENT, RENDER_ARTIFACT])

    if not tools and plan.get("action") == ANSWER_DIRECTLY:
        tools.append(ANSWER_DIRECTLY)
    elif not tools:
        tools.append(ANSWER_DIRECTLY)

    seen: set[str] = set()
    selected: list[dict[str, Any]] = []
    for name in tools:
        if name in seen:
            continue
        seen.add(name)
        selected.append(get_tool(name).to_dict())
    return selected


AnswerExecutor = Callable[..., LLMResult]


def execute_answer_directly_tool(
    state: TurnGraphState,
    *,
    route: RouteDecision,
    executor: AnswerExecutor,
) -> TurnToolOutput:
    """Execute the first safe internal tool: normal chat answer.

    The actual LLM call is injected so the graph layer owns orchestration while
    the existing chat pipeline can continue owning provider routing and prompt
    assembly during migration.
    """

    if not state.plan:
        return TurnToolOutput(status="failed", error="answer_directly requires a plan")
    plan = plan_from_dict(state.plan, state.user_message)
    if plan.action != ANSWER_DIRECTLY:
        return TurnToolOutput(status="failed", error=f"unsupported plan action: {plan.action}")
    result = executor(
        plan.enriched_prompt,
        route,
        history=state.history,
        deep_research=False,
        web_context=None,
        enable_native_search=False,
        planner_context=None,
        doc_context=state.doc_context or None,
        artifact_context=state.artifact_context or None,
    )
    state.final_answer = result.answer
    return TurnToolOutput(
        status="ok",
        user_message=result.answer,
        result={
            "model_used": result.model_used,
            "latency_ms": result.latency_ms,
            "prompt_tokens": result.prompt_tokens,
            "completion_tokens": result.completion_tokens,
            "estimated_cost_usd": result.estimated_cost_usd,
        },
    )
