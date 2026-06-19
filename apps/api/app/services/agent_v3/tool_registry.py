from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.services.agent_v3.models import Artifact, Source, ToolCall, ToolDefinition
from app.services.agent_v3.tools import AgentV3Tools


ToolHandler = Callable[[dict[str, Any]], tuple[Any, ToolCall]]


@dataclass
class RegisteredTool:
    definition: ToolDefinition
    handler: ToolHandler


class ToolRegistry:
    def __init__(self, tools: AgentV3Tools | None = None):
        self.tools = tools or AgentV3Tools.from_settings()
        self._tools: dict[str, RegisteredTool] = {}
        self._register_defaults()

    def list_definitions(self) -> list[ToolDefinition]:
        return [tool.definition for tool in self._tools.values() if tool.definition.enabled]

    def describe(self) -> list[dict[str, Any]]:
        return [definition.model_dump(mode="json") for definition in self.list_definitions()]

    def tool_names_for_route(self, route: str) -> list[str]:
        return [
            tool.definition.name
            for tool in self._tools.values()
            if tool.definition.enabled and route in tool.definition.route_tags
        ]

    def run(self, name: str, inputs: dict[str, Any]) -> tuple[Any, ToolCall]:
        registered = self._tools.get(name)
        if registered is None or not registered.definition.enabled:
            return None, ToolCall(
                name=name,
                input=inputs,
                ok=False,
                error=f"Tool '{name}' is not registered or enabled.",
            )
        return registered.handler(inputs)

    def register(self, definition: ToolDefinition, handler: ToolHandler) -> None:
        self._tools[definition.name] = RegisteredTool(definition=definition, handler=handler)

    def _register_defaults(self) -> None:
        self.register(
            ToolDefinition(
                name="web_search",
                description="Search the public web for source candidates.",
                input_schema={"query": "str", "max_results": "int"},
                output_schema={"sources": "list[Source]"},
                route_tags=["research", "research_document"],
            ),
            self._web_search,
        )
        self.register(
            ToolDefinition(
                name="read_url",
                description="Extract readable content from source URLs.",
                input_schema={"urls": "list[str]", "max_chars_per_source": "int"},
                output_schema={"sources": "list[Source]"},
                route_tags=["research", "research_document"],
            ),
            self._read_url,
        )
        self.register(
            ToolDefinition(
                name="make_markdown_artifact",
                description="Create a downloadable markdown artifact.",
                input_schema={"title": "str", "markdown": "str"},
                output_schema={"artifact": "Artifact"},
                route_tags=["document", "research_document"],
            ),
            self._make_markdown_artifact,
        )
        self.register(
            ToolDefinition(
                name="make_docx_artifact",
                description="Create a downloadable DOCX artifact.",
                input_schema={"title": "str", "markdown": "str", "expected_sections": "list[str]"},
                output_schema={"artifact": "Artifact"},
                route_tags=["document", "research_document"],
            ),
            self._make_docx_artifact,
        )
        self.register(
            ToolDefinition(
                name="make_pptx_artifact",
                description="Create a downloadable PPTX presentation artifact.",
                input_schema={"title": "str", "markdown": "str", "expected_slides": "list[str]"},
                output_schema={"artifact": "Artifact"},
                route_tags=["document", "research_document"],
            ),
            self._make_pptx_artifact,
        )

    def _web_search(self, inputs: dict[str, Any]) -> tuple[list[Source], ToolCall]:
        query = str(inputs.get("query") or "")
        max_results = int(inputs.get("max_results") or 6)
        return self.tools.search_web(query, max_results=max_results)

    def _read_url(self, inputs: dict[str, Any]) -> tuple[list[Source], ToolCall]:
        urls = [str(url) for url in inputs.get("urls", []) if url]
        max_chars = int(inputs.get("max_chars_per_source") or 2500)
        return self.tools.extract_urls(urls, max_chars_per_source=max_chars)

    def _make_markdown_artifact(self, inputs: dict[str, Any]) -> tuple[Artifact, ToolCall]:
        title = str(inputs.get("title") or "Agent v3 document")
        markdown = str(inputs.get("markdown") or "")
        artifact = self.tools.make_markdown_artifact(title, markdown)
        return artifact, ToolCall(
            name="make_markdown_artifact",
            input={"title": title, "markdown_chars": len(markdown)},
            output={"artifact_id": artifact.id, "filename": artifact.filename},
        )

    def _make_docx_artifact(self, inputs: dict[str, Any]) -> tuple[Artifact, ToolCall]:
        title = str(inputs.get("title") or "Agent v3 document")
        markdown = str(inputs.get("markdown") or "")
        expected_sections = [str(section) for section in (inputs.get("expected_sections") or []) if section]
        artifact, qa_issue_codes = self.tools.make_docx_artifact(title, markdown, expected_sections=expected_sections)
        return artifact, ToolCall(
            name="make_docx_artifact",
            input={"title": title, "markdown_chars": len(markdown), "expected_sections": expected_sections},
            output={
                "artifact_id": artifact.id,
                "filename": artifact.filename,
                "kind": artifact.kind,
                "qa_issue_codes": qa_issue_codes,
            },
        )

    def _make_pptx_artifact(self, inputs: dict[str, Any]) -> tuple[Artifact, ToolCall]:
        title = str(inputs.get("title") or "Agent v3 presentation")
        markdown = str(inputs.get("markdown") or "")
        expected_slides = [str(slide) for slide in (inputs.get("expected_slides") or []) if slide]
        artifact, metadata = self.tools.make_pptx_artifact(title, markdown, expected_slides=expected_slides)
        return artifact, ToolCall(
            name="make_pptx_artifact",
            input={"title": title, "markdown_chars": len(markdown), "expected_slides": expected_slides},
            output={
                "artifact_id": artifact.id,
                "filename": artifact.filename,
                "kind": artifact.kind,
                **metadata,
            },
        )
