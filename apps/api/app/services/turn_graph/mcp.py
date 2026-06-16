from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from app.services.turn_graph.tools import (
    DEEP_RESEARCH,
    LOAD_TEMPLATES,
    RENDER_ARTIFACT,
    WEB_CONTEXT,
    get_tool,
)


MCPAdapterStatus = Literal["candidate", "ready", "disabled"]


@dataclass(frozen=True)
class MCPAdapterDef:
    tool_name: str
    adapter_id: str
    server: str
    capability: str
    status: MCPAdapterStatus = "candidate"
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "tool": get_tool(self.tool_name).to_dict(),
            "adapter_id": self.adapter_id,
            "server": self.server,
            "capability": self.capability,
            "status": self.status,
            "notes": self.notes,
        }


MCP_TOOL_ADAPTERS: dict[str, MCPAdapterDef] = {
    WEB_CONTEXT: MCPAdapterDef(
        tool_name=WEB_CONTEXT,
        adapter_id="mcp.web.search",
        server="web/search",
        capability="search_and_fetch_sources",
        notes="Candidate backend for web_context once provider routing is moved behind tools.",
    ),
    DEEP_RESEARCH: MCPAdapterDef(
        tool_name=DEEP_RESEARCH,
        adapter_id="mcp.web.research",
        server="web/search",
        capability="durable_research_sources",
        notes="Research remains an internal subgraph; MCP can provide source retrieval/crawl backends.",
    ),
    LOAD_TEMPLATES: MCPAdapterDef(
        tool_name=LOAD_TEMPLATES,
        adapter_id="mcp.storage.templates",
        server="file-storage",
        capability="user_template_assets",
        notes="Candidate backend for external template/blob storage.",
    ),
    RENDER_ARTIFACT: MCPAdapterDef(
        tool_name=RENDER_ARTIFACT,
        adapter_id="mcp.drive.export",
        server="google-drive",
        capability="optional_export_or_upload",
        notes="Optional backend for exporting/sharing artifacts after internal rendering succeeds.",
    ),
}


def mcp_adapter_for_tool(tool_name: str) -> MCPAdapterDef | None:
    return MCP_TOOL_ADAPTERS.get(tool_name)


def mcp_adapter_payload() -> list[dict[str, Any]]:
    return [adapter.to_dict() for adapter in MCP_TOOL_ADAPTERS.values()]
