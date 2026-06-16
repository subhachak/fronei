"""Turn graph foundation for Fronei's agentic runtime migration.

The initial implementation is intentionally dependency-light: it defines the
state and graph-shell interfaces that the existing pipeline can migrate onto
before the runtime depends on LangGraph itself.
"""

from app.services.turn_graph.graph import run_turn_graph_shell
from app.services.turn_graph.nodes import (
    gate_node,
    load_context_node,
    planner_node,
    run_planning_shadow_graph,
    triage_node,
)
from app.services.turn_graph.research import (
    ResearchSubgraphEvent,
    ResearchToolInput,
    crawl_research_node,
    decompose_research_node,
    execute_deep_research_tool,
    extract_research_node,
    research_stage_for_progress,
    research_stage_node,
    search_research_node,
    sufficiency_research_node,
    synthesize_research_node,
    verify_research_node,
)
from app.services.turn_graph.document import (
    ArtifactRenderToolInput,
    DocumentGenerationToolInput,
    DocumentSubgraphEvent,
    content_plan_node,
    design_plan_node,
    document_stage_node,
    execute_generate_document_tool,
    execute_quality_check_tool,
    execute_render_artifact_tool,
    final_preview_node,
    qa_polish_node,
    render_artifact_node,
)
from app.services.turn_graph.mcp import (
    MCP_TOOL_ADAPTERS,
    MCPAdapterDef,
    mcp_adapter_for_tool,
    mcp_adapter_payload,
)
from app.services.turn_graph.rollout import GraphRolloutDecision, graph_rollout_decision
from app.services.turn_graph.state import TurnGraphEvent, TurnGraphNodeTiming, TurnGraphState
from app.services.turn_graph.adapters import graph_trace_payload, state_from_turn
from app.services.turn_graph.tools import (
    TOOL_REGISTRY,
    execute_answer_directly_tool,
    get_tool,
    select_tools_from_state,
    tool_registry_payload,
)

__all__ = [
    "TurnGraphEvent",
    "TurnGraphNodeTiming",
    "TurnGraphState",
    "TOOL_REGISTRY",
    "ResearchSubgraphEvent",
    "ResearchToolInput",
    "ArtifactRenderToolInput",
    "DocumentGenerationToolInput",
    "DocumentSubgraphEvent",
    "GraphRolloutDecision",
    "MCPAdapterDef",
    "MCP_TOOL_ADAPTERS",
    "crawl_research_node",
    "content_plan_node",
    "design_plan_node",
    "document_stage_node",
    "decompose_research_node",
    "execute_generate_document_tool",
    "execute_quality_check_tool",
    "execute_render_artifact_tool",
    "execute_deep_research_tool",
    "extract_research_node",
    "execute_answer_directly_tool",
    "gate_node",
    "get_tool",
    "graph_trace_payload",
    "graph_rollout_decision",
    "load_context_node",
    "mcp_adapter_for_tool",
    "mcp_adapter_payload",
    "planner_node",
    "run_planning_shadow_graph",
    "run_turn_graph_shell",
    "select_tools_from_state",
    "state_from_turn",
    "research_stage_for_progress",
    "research_stage_node",
    "final_preview_node",
    "qa_polish_node",
    "render_artifact_node",
    "search_research_node",
    "sufficiency_research_node",
    "synthesize_research_node",
    "tool_registry_payload",
    "triage_node",
    "verify_research_node",
]
