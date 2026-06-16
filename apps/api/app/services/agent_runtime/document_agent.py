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
from app.services.agent_runtime.judge_service import JudgeService
from app.services.agent_runtime.models import JudgeResult
from app.services.agent_runtime.registry import RuntimeRegistry
from app.services.agent_runtime.tool_runner import (
    ToolExecutionError,
    ToolNotPermittedError,
    ToolRunner,
)
from app.services.turn_graph.document import (
    content_plan_node,
    design_plan_node,
    final_preview_node,
    qa_polish_node,
    render_artifact_node,
)
from app.services.turn_graph.state import TurnGraphState
from app.services.agent_runtime.utils import effective_max_repair_iters


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
        """Run the document pipeline through all five stage nodes. Never raises.

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
        if isinstance(getattr(decision, "plan", None), dict):
            state.plan = decision.plan
        quality_mode = getattr(state, "quality_mode", None) or "standard"

        state = content_plan_node(
            state,
            fn=lambda s: self._plan_stage(s, brand_profile, quality_mode),
        )
        brief = state.document_brief or _fallback_brief(state)

        judge_result, brief = self._judge_plan_loop(state, brief)
        if judge_result is not None:
            logger.info(
                "Document judge final: policy=%s status=%s score=%.2f",
                self.agent_def.judge_policy_id,
                judge_result.status,
                judge_result.score,
            )
        state.document_brief = brief

        is_presentation = str(brief.get("doc_type") or "").lower() == "presentation"
        template_id = brand_profile.get("template_id") or None
        grammar_holder: list[dict] = []
        state = design_plan_node(
            state,
            fn=lambda s: self._design_stage(s, brief, is_presentation, template_id, db, grammar_holder),
        )
        grammar = grammar_holder[0] if grammar_holder else None
        research_summary = _extract_research_summary(state)

        content_holder: list[Any] = []
        tool_result_holder: list[dict] = []
        state = render_artifact_node(
            state,
            fn=lambda s: self._generate_stage(
                s,
                brief,
                decision,
                grammar,
                research_summary,
                is_presentation,
                template_id,
                tool_runner,
                content_holder,
                tool_result_holder,
            ),
        )
        content_obj = content_holder[0] if content_holder else None
        tool_result = tool_result_holder[0] if tool_result_holder else {}

        state = qa_polish_node(
            state,
            fn=lambda s: self._qa_repair_stage(
                s,
                content_obj,
                brief,
                is_presentation,
                decision,
                grammar,
                research_summary,
                template_id,
                tool_runner,
                content_holder,
                tool_result_holder,
            ),
        )
        content_obj = content_holder[0] if content_holder else None
        tool_result = tool_result_holder[0] if tool_result_holder else {}

        state = final_preview_node(state)

        return self._build_document_result(state, brief, content_obj, tool_result)

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

    def _plan_stage(
        self,
        state: TurnGraphState,
        brand_profile: dict,
        quality_mode: str,
    ) -> dict | None:
        """Stage fn for content_plan_node. Calls _plan(); writes state.document_brief."""

        try:
            result = self._plan(state, brand_profile, quality_mode)
        except Exception:
            logger.exception("Document planning failed; using fallback brief")
            result = SimpleNamespace(
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
        brief = _extract_document_brief(result.answer, state.user_message)
        state.document_brief = brief
        return {"doc_type": brief.get("doc_type"), "title": brief.get("title")}

    def _judge_plan_loop(
        self,
        state: TurnGraphState,
        brief: dict,
    ) -> tuple[JudgeResult | None, dict]:
        """Run judge against the plan and repair via re-plan when requested."""

        judge_policy_id = self.agent_def.judge_policy_id
        if not judge_policy_id:
            return None, brief

        judge_result = JudgeService(self.registry).evaluate(
            judge_policy_id,
            content=_brief_for_judge(brief),
            context={"user_question": state.user_message, "stage": "plan"},
            target_id=str(getattr(state, "turn_id", "") or ""),
        )
        logger.info(
            "Document plan judge [0]: policy=%s status=%s score=%.2f",
            judge_policy_id,
            judge_result.status,
            judge_result.score,
        )
        policy = self.registry.judges.get(judge_policy_id)
        quality_mode = getattr(state, "quality_mode", None) or "standard"
        max_iters = effective_max_repair_iters(quality_mode, policy)

        if judge_result.status != "repair" or max_iters == 0:
            if judge_result.status == "repair" and max_iters == 0:
                logger.info("Document plan repair skipped: quality_mode=%s", quality_mode)
            return judge_result, brief

        brand_profile = _extract_brand_profile(getattr(state, "plan", None) or {})

        for attempt in range(max_iters):
            logger.info(
                "Document judge repair %d/%d: re-planning (repairs=%s)",
                attempt + 1,
                max_iters,
                judge_result.required_repairs,
            )
            brief = self._replan_with_repairs(
                state,
                brief,
                judge_result.required_repairs,
                brand_profile,
                quality_mode,
            )
            state.document_brief = brief
            judge_result = JudgeService(self.registry).evaluate(
                judge_policy_id,
                content=_brief_for_judge(brief),
                context={"user_question": state.user_message, "stage": "plan"},
                target_id=str(getattr(state, "turn_id", "") or ""),
            )
            logger.info(
                "Document plan judge [%d]: policy=%s status=%s score=%.2f",
                attempt + 1,
                judge_policy_id,
                judge_result.status,
                judge_result.score,
            )
            if judge_result.status != "repair":
                break

        return judge_result, brief

    def _replan_with_repairs(
        self,
        state: TurnGraphState,
        original_brief: dict,
        required_repairs: list[dict[str, Any]],
        brand_profile: dict,
        quality_mode: str,
    ) -> dict:
        """Re-invoke the planning LLM with repair instructions appended. Never raises."""

        try:
            from app.services.llm_gateway import invoke_llm_json

            is_claude = self.model_policy.primary_model.startswith("claude")
            messages: list[dict[str, str]] = [
                {"role": "system", "content": self.prompt.system_prompt},
            ]
            if self.prompt.developer_prompt:
                messages.append({
                    "role": "developer" if is_claude else "system",
                    "content": self.prompt.developer_prompt,
                })
            repair_note = (
                "The previous plan was evaluated and requires revision. "
                "Required repairs:\n" + "\n".join(
                    f"- {_repair_instruction_text(repair)}" for repair in required_repairs
                )
            )
            messages.append({
                "role": "user",
                "content": json.dumps({
                    "goal": state.user_message,
                    "brand_profile": brand_profile,
                    "quality_mode": quality_mode,
                    "previous_brief": original_brief,
                    "repair_instructions": repair_note,
                }),
            })
            # Uses document_lead for now; a future sub-agent runner will invoke
            # content_strategist directly for this repair step.
            result = invoke_llm_json(messages, model_policy_to_route(self.model_policy))
            return _extract_document_brief(result.answer, state.user_message)
        except Exception:
            logger.exception("Replan failed; retaining original brief")
            return original_brief

    def _design_stage(
        self,
        state: TurnGraphState,
        brief: dict,
        is_presentation: bool,
        template_id: str | None,
        db: Any,
        grammar_holder: list[dict],
    ) -> dict | None:
        """Stage fn for design_plan_node. Fetches grammar for presentations."""

        if is_presentation:
            grammar = _fetch_template_grammar(
                user_id=str(getattr(state, "user_id", "") or ""),
                template_id=template_id,
                brief=brief,
                db=db,
            )
            grammar_holder.append(grammar)
        return {"is_presentation": is_presentation, "has_grammar": bool(grammar_holder)}

    def _generate_stage(
        self,
        state: TurnGraphState,
        brief: dict,
        decision: Any,
        grammar: dict | None,
        research_summary: str | None,
        is_presentation: bool,
        template_id: str | None,
        tool_runner: ToolRunner,
        content_holder: list[Any],
        tool_result_holder: list[dict],
    ) -> dict | None:
        """Stage fn for render_artifact_node. Calls _generate_content() then render tool."""

        try:
            content_obj = self._generate_content(
                state,
                brief,
                decision,
                grammar=grammar,
                research_summary=research_summary,
            )
        except Exception:
            logger.exception("Document content generation failed; using fallback content")
            title = brief.get("title") or state.user_message[:120] or "Document"
            content_obj = SimpleNamespace(
                answer=f"# {title}\n\nI couldn't generate the full document content right now.",
                model_used="unavailable",
                latency_ms=0,
                estimated_cost_usd=0.0,
            )
        content_holder.append(content_obj)
        state.document_content = content_obj.answer

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
                        "content": content_obj.answer,
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
                        "content": content_obj.answer,
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

        tool_result_holder.append({
            "docx_base64": docx_base64,
            "pptx_base64": pptx_base64,
            "filename": filename,
            "tool_latency_ms": tool_latency_ms,
        })
        return {"content_length": len(content_obj.answer), "filename": filename}

    def _qa_repair_stage(
        self,
        state: TurnGraphState,
        content_obj: Any,
        brief: dict,
        is_presentation: bool,
        decision: Any,
        grammar: dict | None,
        research_summary: str | None,
        template_id: str | None,
        tool_runner: ToolRunner,
        content_holder: list[Any],
        tool_result_holder: list[dict],
    ) -> dict | None:
        """Stage fn for qa_polish_node. Judge final content and repair in place."""

        judge_policy_id = self.agent_def.judge_policy_id
        if not judge_policy_id or content_obj is None:
            return None
        quality_mode = getattr(state, "quality_mode", None) or "standard"
        policy = self.registry.judges.get(judge_policy_id)
        max_iters = effective_max_repair_iters(quality_mode, policy)

        content = getattr(content_obj, "answer", "") or ""
        judge_result = JudgeService(self.registry).evaluate(
            judge_policy_id,
            content=content[:4_000],
            context={"user_question": state.user_message, "stage": "content"},
            target_id=str(getattr(state, "turn_id", "") or ""),
        )
        logger.info(
            "Document content judge [0]: policy=%s status=%s score=%.2f",
            judge_policy_id,
            judge_result.status,
            judge_result.score,
        )
        if judge_result.status != "repair" or max_iters == 0:
            if judge_result.status == "repair" and max_iters == 0:
                logger.info("Document content repair skipped: quality_mode=%s", quality_mode)
            return {"judge_status": judge_result.status, "judge_score": judge_result.score}

        for attempt in range(max_iters):
            logger.info(
                "Document content repair %d/%d: re-generating (repairs=%s)",
                attempt + 1,
                max_iters,
                judge_result.required_repairs,
            )
            repaired_obj, repaired_tool = self._regenerate_with_repairs(
                state,
                brief,
                judge_result.required_repairs,
                is_presentation,
                decision,
                grammar,
                research_summary,
                template_id,
                tool_runner,
            )
            if repaired_obj is not None:
                if content_holder:
                    content_holder[0] = repaired_obj
                else:
                    content_holder.append(repaired_obj)
                content_obj = repaired_obj
                content = getattr(content_obj, "answer", "") or ""
                state.document_content = content
            if repaired_tool is not None:
                if tool_result_holder:
                    tool_result_holder[0] = repaired_tool
                else:
                    tool_result_holder.append(repaired_tool)

            judge_result = JudgeService(self.registry).evaluate(
                judge_policy_id,
                content=content[:4_000],
                context={"user_question": state.user_message, "stage": "content"},
                target_id=str(getattr(state, "turn_id", "") or ""),
            )
            logger.info(
                "Document content judge [%d]: policy=%s status=%s score=%.2f",
                attempt + 1,
                judge_policy_id,
                judge_result.status,
                judge_result.score,
            )
            if judge_result.status != "repair":
                break

        return {"judge_status": judge_result.status, "judge_score": judge_result.score}

    def _regenerate_with_repairs(
        self,
        state: TurnGraphState,
        brief: dict,
        required_repairs: list[dict[str, Any]],
        is_presentation: bool,
        decision: Any,
        grammar: dict | None,
        research_summary: str | None,
        template_id: str | None,
        tool_runner: ToolRunner,
    ) -> tuple[Any | None, dict | None]:
        """Re-generate content with repair context and re-run render tool. Never raises."""

        try:
            from app.services.llm_gateway import invoke_llm

            repair_note = (
                "REVISION REQUIRED. The previous content was evaluated and needs improvement:\n"
                + "\n".join(f"- {_repair_instruction_text(repair)}" for repair in required_repairs)
                + "\nAddress each point in your revised content."
            )
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
            doc_context = f"{repair_note}\n\n{doc_context}"

            content_obj = invoke_llm(
                message=state.user_message,
                route=model_policy_to_route(self.model_policy),
                history=state.history[-4:] if state.history else [],
                planner_context=state.running_summary or None,
                doc_context=doc_context,
            )
            tool_result = self._render_content_tool(
                state,
                brief,
                content_obj,
                is_presentation,
                decision,
                template_id,
                tool_runner,
            )
            return content_obj, tool_result
        except Exception:
            logger.exception("Document content re-generation failed; retaining original")
            return None, None

    def _render_content_tool(
        self,
        state: TurnGraphState,
        brief: dict,
        content_obj: Any,
        is_presentation: bool,
        decision: Any,
        template_id: str | None,
        tool_runner: ToolRunner,
    ) -> dict:
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
                        "content": content_obj.answer,
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
                        "content": content_obj.answer,
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
            logger.warning("Repair tool call failed: %s", exc)
        except Exception:
            logger.exception("Unexpected failure in repair tool call")

        return {
            "docx_base64": docx_base64,
            "pptx_base64": pptx_base64,
            "filename": filename,
            "tool_latency_ms": tool_latency_ms,
        }

    def _build_document_result(
        self,
        state: TurnGraphState,
        brief: dict,
        content_obj: Any,
        tool_result: dict,
    ) -> DocumentResult:
        planning_latency_ms = 0
        content_latency_ms = getattr(content_obj, "latency_ms", 0) or 0
        tool_latency_ms = tool_result.get("tool_latency_ms", 0)
        content_cost = getattr(content_obj, "estimated_cost_usd", 0.0) or 0.0

        for timing in state.node_timings:
            if timing.node == "document.content_plan":
                planning_latency_ms = timing.latency_ms
                break

        return DocumentResult(
            title=str(brief.get("title") or "Document"),
            doc_type=str(brief.get("doc_type") or "executive_report"),
            markdown=getattr(content_obj, "answer", "") or "",
            docx_base64=tool_result.get("docx_base64", ""),
            pptx_base64=tool_result.get("pptx_base64", ""),
            filename=tool_result.get("filename") or _fallback_filename(str(brief.get("title") or "document")),
            model_used=getattr(content_obj, "model_used", "unavailable") or "unavailable",
            prompt_id=self.prompt.id,
            planning_latency_ms=planning_latency_ms,
            content_latency_ms=content_latency_ms,
            latency_ms=planning_latency_ms + content_latency_ms + tool_latency_ms,
            cost_usd=content_cost,
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


def _fallback_brief(state: TurnGraphState) -> dict:
    """Return a minimal brief when planning fails to populate state.document_brief."""

    return {"title": state.user_message[:120], "doc_type": "executive_report"}


def _extract_research_summary(state: TurnGraphState) -> str | None:
    """Extract text summary from research_result if present."""

    if isinstance(getattr(state, "research_result", None), dict):
        return (
            state.research_result.get("answer")
            or state.research_result.get("summary")
            or None
        )
    return None


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


def _brief_for_judge(brief: dict) -> str:
    return json.dumps(brief, indent=2) if isinstance(brief, dict) else str(brief)


def _repair_instruction_text(repair: dict[str, Any] | str) -> str:
    if isinstance(repair, dict):
        section = str(repair.get("section", "")).strip()
        instruction = str(repair.get("instruction") or repair.get("message") or "").strip()
        if section and instruction:
            return f"{section}: {instruction}"
        return instruction or section or json.dumps(repair, sort_keys=True)
    return str(repair)


def _fallback_filename(title: str) -> str:
    cleaned = _SAFE_FILENAME_CHARS_RE.sub("", title.strip().lower())
    normalized = "-".join(cleaned.split())
    return f"{normalized or 'document'}.docx"
