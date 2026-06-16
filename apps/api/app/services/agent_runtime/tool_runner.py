from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.services.agent_runtime.guardrails import GuardrailContext, GuardrailService, max_boundary_action
from app.services.agent_runtime.registry import RuntimeRegistry
from app.services.turn_graph.state import TurnGraphState


logger = logging.getLogger(__name__)

MAX_CONTENT_CHARS = 2_000
MAX_SOURCES = 5

_NATIVE_BACKENDS: dict[str, Callable[[dict], dict]] = {}


def register_native_backend(ref: str, fn: Callable[[dict], dict]) -> None:
    """Register a native in-process executor for a ToolDefinition.backend_ref."""

    _NATIVE_BACKENDS[ref] = fn


class ToolNotPermittedError(Exception):
    """Raised when an agent attempts to call a tool it is not allowed to use."""


class ToolExecutionError(Exception):
    """Raised when the tool backend fails and no fallback is available."""


@dataclass
class ToolCallResult:
    tool_name: str
    input_summary: str
    output: dict[str, Any]
    latency_ms: int
    step_id: str = field(default_factory=lambda: str(uuid.uuid4()))


class ToolRunner:
    """Single execution path for all agent tool calls."""

    def __init__(
        self,
        registry: RuntimeRegistry,
        agent_id: str,
        guardrail_service: GuardrailService,
    ) -> None:
        self.registry = registry
        self.agent_id = agent_id
        self.guardrail_service = guardrail_service

    def run(
        self,
        tool_name: str,
        inputs: dict[str, Any],
        *,
        state: TurnGraphState,
    ) -> ToolCallResult:
        try:
            tool_def = self.registry.tool(tool_name)
        except KeyError:
            raise ToolNotPermittedError(f"Unknown tool: {tool_name!r}") from None

        if self.agent_id not in (tool_def.allowed_agent_ids or []):
            raise ToolNotPermittedError(
                f"Agent {self.agent_id!r} is not permitted to call tool {tool_name!r}. "
                f"Allowed: {tool_def.allowed_agent_ids}"
            )

        pre_context = GuardrailContext(
            boundary="tool_pre",
            user_id=state.user_id or "",
            tenant_id=None,
            tool_name=tool_name,
            tool_input=inputs,
            tool_output=None,
            request_text=state.user_message,
            plan=None,
            response_text=None,
        )
        pre_decisions = self.guardrail_service.evaluate_boundary("tool_pre", pre_context)
        if max_boundary_action(pre_decisions) == "block":
            reason = pre_decisions[0].reason if pre_decisions else "guardrail block"
            raise ToolNotPermittedError(f"Tool {tool_name!r} blocked by guardrail: {reason}")

        started = time.perf_counter()
        raw_output = self._execute(tool_def, inputs)
        latency_ms = int((time.perf_counter() - started) * 1000)
        sanitized = _sanitize_tool_output(tool_name, raw_output)

        try:
            post_context = GuardrailContext(
                boundary="tool_post",
                user_id=state.user_id or "",
                tenant_id=None,
                tool_name=tool_name,
                tool_input=inputs,
                tool_output=sanitized,
                request_text=state.user_message,
                plan=None,
                response_text=None,
            )
            post_decisions = self.guardrail_service.evaluate_boundary("tool_post", post_context)
            for decision in post_decisions:
                if decision.action in {"transform", "redact"} and decision.modified_payload is not None:
                    sanitized = decision.modified_payload
        except Exception:
            logger.exception("Post-guardrail evaluation failed for tool %s; ignoring", tool_name)

        input_summary = str(inputs.get("query") or inputs.get("url") or inputs)[:200]
        return ToolCallResult(
            tool_name=tool_name,
            input_summary=input_summary,
            output=sanitized,
            latency_ms=latency_ms,
        )

    def _execute(self, tool_def, inputs: dict[str, Any]) -> dict[str, Any]:
        from app.services.web_context import crawl_url, search_web_sources

        try:
            if tool_def.backend == "mcp":
                if tool_def.id == "web_search":
                    provider, sources = search_web_sources(
                        str(inputs.get("query") or ""),
                        recency=inputs.get("recency"),
                    )
                    max_results = int(inputs.get("max_results") or MAX_SOURCES)
                    return {
                        "sources": [
                            {"title": source.title, "url": source.url, "content": source.content}
                            for source in sources[:max(1, min(max_results, MAX_SOURCES))]
                        ],
                        "provider": provider,
                    }
                if tool_def.id == "read_url":
                    url = str(inputs.get("url") or "")
                    source = crawl_url(url)
                    return {
                        "content": source.content if source else "",
                        "url": url,
                        "title": source.title if source else "",
                        "provider": "crawl",
                    }
            if tool_def.backend == "native":
                backend_fn = _NATIVE_BACKENDS.get(tool_def.backend_ref or "")
                if backend_fn is None:
                    raise ToolExecutionError(
                        f"No native backend registered for ref={tool_def.backend_ref!r}. "
                        "Call register_native_backend() at startup."
                    )
                return backend_fn(inputs)
        except (ToolNotPermittedError, ToolExecutionError):
            raise
        except Exception as exc:
            raise ToolExecutionError(f"Tool {tool_def.id!r} failed: {exc}") from exc

        raise ToolExecutionError(f"No executor for backend={tool_def.backend!r}, tool={tool_def.id!r}")


def _sanitize_tool_output(tool_name: str, raw: dict[str, Any]) -> dict[str, Any]:
    blocked_keys = {"api_key", "token", "secret", "password", "key", "authorization"}

    if tool_name == "web_search":
        clean_sources: list[dict[str, str]] = []
        sources = raw.get("sources") or []
        for source in sources:
            if not isinstance(source, dict):
                continue
            clean_sources.append({
                "title": str(source.get("title") or "")[:300],
                "url": str(source.get("url") or ""),
                "content": str(source.get("content") or "")[:MAX_CONTENT_CHARS],
            })
        return {"sources": clean_sources, "provider": str(raw.get("provider") or "")}

    if tool_name == "read_url":
        return {
            "content": str(raw.get("content") or "")[:MAX_CONTENT_CHARS * 4],
            "url": str(raw.get("url") or ""),
            "title": str(raw.get("title") or "")[:300],
            "provider": str(raw.get("provider") or ""),
        }

    if tool_name == "generate_document":
        return {
            "title": str(raw.get("title") or ""),
            "doc_type": str(raw.get("doc_type") or ""),
            "filename": str(raw.get("filename") or ""),
            "markdown_preview": str(raw.get("markdown") or "")[:500],
            "docx_base64": raw.get("docx_base64") or "",
        }

    return {key: value for key, value in raw.items() if key.lower() not in blocked_keys}
