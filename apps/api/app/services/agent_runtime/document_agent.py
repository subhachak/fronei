from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from app.services.agent_runtime.adapters import model_policy_to_route
from app.services.agent_runtime.guardrails import GuardrailService
from app.services.agent_runtime.registry import RuntimeRegistry
from app.services.agent_runtime.tool_runner import (
    ToolExecutionError,
    ToolNotPermittedError,
    ToolRunner,
)
from app.services.turn_graph.state import TurnGraphState


logger = logging.getLogger(__name__)
_SAFE_FILENAME_CHARS_RE = re.compile(r"[^\w\s-]")


@dataclass
class DocumentResult:
    title: str
    doc_type: str
    markdown: str
    docx_base64: str
    filename: str
    model_used: str
    prompt_id: str
    planning_latency_ms: int
    content_latency_ms: int
    latency_ms: int
    cost_usd: float
    run_id: str = field(default_factory=lambda: str(uuid.uuid4()))


class DocumentAgent:
    """Phase-F document_lead agent: plan -> generate content -> render DOCX."""

    def __init__(self, registry: RuntimeRegistry) -> None:
        self.registry = registry
        self.agent_def = registry.agent("document_lead")
        self.model_policy = registry.model_policy(self.agent_def.model_policy_id)
        self.prompt = registry.prompt(self.agent_def.prompt_template_id)

    def run(self, state: TurnGraphState, decision) -> DocumentResult:
        """Run the document pipeline. Never raises."""

        tool_runner = ToolRunner(
            registry=self.registry,
            agent_id="document_lead",
            guardrail_service=GuardrailService(self.registry),
        )
        brand_profile = _extract_brand_profile(decision.plan)
        quality_mode = getattr(state, "quality_mode", None) or "standard"

        try:
            planning_result = self._plan(state, brand_profile, quality_mode)
        except Exception:
            logger.exception("Document planning failed; using fallback brief")
            planning_result = SimpleNamespace(
                answer=json.dumps({
                    "document_brief": {
                        "title": state.user_message[:120],
                        "doc_type": "executive_report",
                    }
                }),
                model_used="unavailable",
                latency_ms=0,
                estimated_cost_usd=0.0,
            )
        brief = _extract_document_brief(planning_result.answer, state.user_message)

        try:
            content_result = self._generate_content(state, brief, decision)
        except Exception:
            logger.exception("Document content generation failed; using fallback content")
            title = brief.get("title") or state.user_message[:120] or "Document"
            content_result = SimpleNamespace(
                answer=f"# {title}\n\nI couldn't generate the full document content right now.",
                model_used=getattr(planning_result, "model_used", "unavailable"),
                latency_ms=0,
                estimated_cost_usd=0.0,
            )

        docx_base64 = ""
        filename = _fallback_filename(str(brief.get("title") or "document"))
        tool_latency_ms = 0
        try:
            tool_call = tool_runner.run(
                "generate_document",
                {
                    "title": brief.get("title", "Document"),
                    "content": content_result.answer,
                    "doc_type": brief.get("doc_type", "executive_report"),
                    "subtitle": brief.get("subtitle"),
                    "template_id": brand_profile.get("template_id") or None,
                },
                state=state,
                plan=decision.plan if isinstance(getattr(decision, "plan", None), dict) else None,
            )
            tool_latency_ms = tool_call.latency_ms
            docx_base64 = tool_call.output.get("docx_base64") or ""
            filename = tool_call.output.get("filename") or filename
        except (ToolNotPermittedError, ToolExecutionError) as exc:
            logger.warning("generate_document tool call failed: %s", exc)
        except Exception:
            logger.exception("Unexpected generate_document tool failure; returning markdown only")

        planning_cost = planning_result.estimated_cost_usd or 0.0
        content_cost = content_result.estimated_cost_usd or 0.0
        total_latency = planning_result.latency_ms + content_result.latency_ms + tool_latency_ms

        return DocumentResult(
            title=str(brief.get("title") or "Document"),
            doc_type=str(brief.get("doc_type") or "executive_report"),
            markdown=content_result.answer,
            docx_base64=docx_base64,
            filename=filename,
            model_used=content_result.model_used,
            prompt_id=self.prompt.id,
            planning_latency_ms=planning_result.latency_ms,
            content_latency_ms=content_result.latency_ms,
            latency_ms=total_latency,
            cost_usd=planning_cost + content_cost,
        )

    def _plan(self, state: TurnGraphState, brand_profile: dict, quality_mode: str):
        from app.services.llm_gateway import invoke_llm_json

        is_claude = self.model_policy.primary_model.startswith("claude")
        messages: list[dict[str, str]] = [{"role": "system", "content": self.prompt.system_prompt}]
        if self.prompt.developer_prompt:
            messages.append({
                "role": "developer" if is_claude else "system",
                "content": self.prompt.developer_prompt,
            })
        messages.append({
            "role": "user",
            "content": json.dumps({
                "goal": state.user_message,
                "brand_profile": brand_profile,
                "quality_mode": quality_mode,
            }),
        })
        return invoke_llm_json(messages, model_policy_to_route(self.model_policy))

    def _generate_content(self, state: TurnGraphState, brief: dict, decision):
        from app.services.llm_gateway import invoke_llm

        doc_context = (
            f"Document type: {brief.get('doc_type', 'executive_report')}\n"
            f"Title: {brief.get('title', state.user_message)}\n"
        )
        if brief.get("outline"):
            doc_context += f"Outline: {json.dumps(brief['outline'])}\n"

        return invoke_llm(
            message=state.user_message,
            route=model_policy_to_route(self.model_policy),
            history=state.history[-4:] if state.history else [],
            planner_context=state.running_summary or None,
            doc_context=doc_context,
        )


def _extract_brand_profile(plan: dict | None) -> dict:
    """Extract brand_profile from the orchestrator plan."""

    if not isinstance(plan, dict):
        return {}
    profile = plan.get("brand_profile") or {}
    if not isinstance(profile, dict):
        return {}
    return profile


def _extract_document_brief(planning_response: str, fallback_goal: str) -> dict:
    try:
        parsed = json.loads(planning_response)
    except (json.JSONDecodeError, TypeError, ValueError):
        return {"title": fallback_goal[:120], "doc_type": "executive_report"}

    if not isinstance(parsed, dict):
        return {"title": fallback_goal[:120], "doc_type": "executive_report"}

    brief = parsed.get("document_brief") or {}
    if not isinstance(brief, dict) or not brief:
        brief = parsed

    return {
        "title": str(brief.get("title") or fallback_goal[:120]),
        "doc_type": str(brief.get("doc_type") or "executive_report"),
        "subtitle": brief.get("subtitle") or None,
        "outline": brief.get("outline") or brief.get("sections") or None,
    }


def _fallback_filename(title: str) -> str:
    cleaned = _SAFE_FILENAME_CHARS_RE.sub("", title.strip().lower())
    normalized = "-".join(cleaned.split())
    return f"{normalized or 'document'}.docx"
