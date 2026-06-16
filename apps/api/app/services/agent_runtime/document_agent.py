from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from app.services.agent_runtime.adapters import model_policy_to_route
from app.services.agent_runtime.guardrails import (
    GuardrailService,
    _query_template_ownership,
    _template_belongs_to_user_db,
)
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
    pptx_base64: str = ""
    run_id: str = field(default_factory=lambda: str(uuid.uuid4()))


class DocumentAgent:
    """Phase-F document_lead agent: plan -> generate content -> render DOCX."""

    def __init__(self, registry: RuntimeRegistry) -> None:
        self.registry = registry
        self.agent_def = registry.agent("document_lead")
        self.model_policy = registry.model_policy(self.agent_def.model_policy_id)
        self.prompt = registry.prompt(self.agent_def.prompt_template_id)

    def run(self, state: TurnGraphState, decision, *, db=None) -> DocumentResult:
        """Run the document pipeline. Never raises.

        Args:
            state: Current turn graph state.
            decision: Orchestrator routing decision.
            db: Optional request-scoped SQLAlchemy session. When provided, the
                template ownership guardrail and grammar fetcher use it directly
                rather than opening their own connections. When None, each
                function opens and closes its own session as a fallback.
        """

        tool_runner = ToolRunner(
            registry=self.registry,
            agent_id="document_lead",
            guardrail_service=GuardrailService(
                self.registry,
                template_owner_lookup=(
                    (lambda tid, uid: _query_template_ownership(db, tid, uid))
                    if db is not None
                    else _template_belongs_to_user_db
                ),
            ),
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
        is_presentation = str(brief.get("doc_type") or "").lower() == "presentation"
        template_id = brand_profile.get("template_id") or None

        grammar: dict | None = None
        if is_presentation:
            grammar = _fetch_template_grammar(
                user_id=str(getattr(state, "user_id", "") or ""),
                template_id=template_id,
                brief=brief,
                db=db,
            )

        research_summary: str | None = None
        if isinstance(getattr(state, "research_result", None), dict):
            research_summary = (
                state.research_result.get("answer")
                or state.research_result.get("summary")
                or None
            )

        try:
            content_result = self._generate_content(
                state,
                brief,
                decision,
                grammar=grammar,
                research_summary=research_summary,
            )
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
        pptx_base64 = ""
        filename = _fallback_filename(str(brief.get("title") or "document"))
        if is_presentation:
            filename = filename.replace(".docx", ".pptx")
        tool_latency_ms = 0
        try:
            if is_presentation:
                tool_call = tool_runner.run(
                    "render_pptx",
                    {
                        "title": brief.get("title", "Presentation"),
                        "content": content_result.answer,
                        "doc_type": "presentation",
                        "subtitle": brief.get("subtitle"),
                        "template_id": template_id,
                        "user_id": str(getattr(state, "user_id", "") or ""),
                    },
                    state=state,
                    plan=decision.plan if isinstance(getattr(decision, "plan", None), dict) else None,
                )
                tool_latency_ms = tool_call.latency_ms
                pptx_base64 = tool_call.output.get("pptx_base64") or ""
                filename = tool_call.output.get("filename") or filename
            else:
                tool_call = tool_runner.run(
                    "generate_document",
                    {
                        "title": brief.get("title", "Document"),
                        "content": content_result.answer,
                        "doc_type": brief.get("doc_type", "executive_report"),
                        "subtitle": brief.get("subtitle"),
                        "template_id": template_id,
                    },
                    state=state,
                    plan=decision.plan if isinstance(getattr(decision, "plan", None), dict) else None,
                )
                tool_latency_ms = tool_call.latency_ms
                docx_base64 = tool_call.output.get("docx_base64") or ""
                filename = tool_call.output.get("filename") or filename
        except (ToolNotPermittedError, ToolExecutionError) as exc:
            logger.warning(
                "%s tool call failed: %s",
                "render_pptx" if is_presentation else "generate_document",
                exc,
            )
        except Exception:
            logger.exception("Unexpected tool failure; returning markdown only")

        planning_cost = getattr(planning_result, "estimated_cost_usd", 0.0) or 0.0
        content_cost = getattr(content_result, "estimated_cost_usd", 0.0) or 0.0
        planning_latency_ms = getattr(planning_result, "latency_ms", 0) or 0
        content_latency_ms = getattr(content_result, "latency_ms", 0) or 0
        total_latency = planning_latency_ms + content_latency_ms + tool_latency_ms

        return DocumentResult(
            title=str(brief.get("title") or "Document"),
            doc_type=str(brief.get("doc_type") or "executive_report"),
            markdown=content_result.answer,
            docx_base64=docx_base64,
            pptx_base64=pptx_base64,
            filename=filename,
            model_used=content_result.model_used,
            prompt_id=self.prompt.id,
            planning_latency_ms=planning_latency_ms,
            content_latency_ms=content_latency_ms,
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

    def _generate_content(
        self,
        state: TurnGraphState,
        brief: dict,
        decision,
        grammar: dict | None = None,
        research_summary: str | None = None,
    ):
        from app.services.llm_gateway import invoke_llm

        is_presentation = str(brief.get("doc_type") or "").lower() == "presentation"
        if is_presentation and grammar is not None:
            doc_context = _build_pptx_doc_context(brief, grammar, research_summary)
        else:
            doc_context = (
                f"Document type: {brief.get('doc_type', 'executive_report')}\n"
                f"Title: {brief.get('title', state.user_message)}\n"
            )
            if brief.get("outline"):
                doc_context += f"Outline: {json.dumps(brief['outline'])}\n"
            if research_summary:
                doc_context += f"\nResearch context:\n{research_summary[:3000]}\n"

        return invoke_llm(
            message=state.user_message,
            route=model_policy_to_route(self.model_policy),
            history=state.history[-4:] if state.history else [],
            planner_context=state.running_summary or None,
            doc_context=doc_context,
        )


def _fetch_template_grammar(
    user_id: str,
    template_id: str | None,
    brief: dict | None,
    db=None,
) -> dict:
    """Fetch template grammar for presentation content generation. Never raises.

    Uses the provided session if given; otherwise opens its own.
    """

    from app.services.document_templates import template_grammar_for_selection

    def _call(session) -> dict:
        return template_grammar_for_selection(session, user_id, template_id, brief)

    if db is not None:
        try:
            return _call(db)
        except Exception:
            logger.warning(
                "Could not fetch template grammar for %r; proceeding without it",
                template_id,
            )
            return {}

    from app.db.models import SessionLocal

    try:
        with SessionLocal() as new_db:
            return _call(new_db)
    except Exception:
        if template_id:
            logger.warning(
                "Could not fetch template grammar for %r; proceeding without it",
                template_id,
            )
        else:
            logger.warning("Could not fetch default template grammar")
        return {}


def _build_pptx_doc_context(brief: dict, grammar: dict, research_summary: str | None) -> str:
    """Build the doc_context string for presentation content generation."""

    from app.services.document_templates import template_design_context

    lines = [
        "Document type: presentation",
        f"Title: {brief.get('title', 'Presentation')}",
    ]
    if brief.get("outline"):
        lines.append(f"Outline: {json.dumps(brief['outline'])}")
    if research_summary:
        lines.append(f"\nResearch context:\n{research_summary[:3000]}")
    design_ctx = template_design_context(grammar)
    if design_ctx:
        lines.append(f"\n{design_ctx}")
    return "\n".join(lines)


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
