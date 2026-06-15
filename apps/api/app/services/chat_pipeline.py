"""Shared pipeline logic for synchronous and streaming conversation handlers.

Public API
----------
PipelineResult      — full result from a non-streaming execution
SubQueryExecution   — intermediate state after parallel sub-query execution,
                      before synthesis (used by the streaming path to stream synthesis)
run_pipeline()      — non-streaming full pipeline used by /chat
build_exec_log()    — constructs ExecutionLog from a completed execution

Internal helpers (prefixed _) are importable for the streaming path in conversations.py.
"""
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Callable

from app.config import Settings, get_settings
from app.db.models import Conversation
from app.schemas import (
    ConvChatRequest, ExecutionLog, PlannerLog, PlannerSubQuery,
    RouteDecision, SubQueryLog, WebContextLog, WorkerLog,
)
from app.services.llm_gateway import LLMResult, invoke_llm, synthesize_answers
from app.services.planner import Plan, apply_confirmed_plan, run_planner
from app.services.prompts import ARTIFACT_PROMPTS
from app.services.router import choose_route
from app.services.web_context import WebContextResult, gather_web_context
from app.services.document_generator import parse_deck_plan
from app.services.brand import BrandProfile, UserDocumentProfile
from app.services.components.quality_mode import normalize_quality_mode


def _build_doc_context(docs: list) -> str:
    """Combine one or more attached documents into a single context block."""
    if not docs:
        return ""
    if len(docs) == 1:
        d = docs[0]
        meta = f" ({d.pages}p · {d.method})" if d.pages > 1 else f" ({d.method})"
        return f"ATTACHED DOCUMENT: {d.name}{meta}\n\n{d.text}"
    parts = [f"ATTACHED DOCUMENTS ({len(docs)} files):"]
    for i, d in enumerate(docs, 1):
        meta = f"{d.pages}p · {d.method}" if d.pages > 1 else d.method
        parts.append(f"\n--- [{i}/{len(docs)}] {d.name} ({meta}) ---\n{d.text}")
    return "\n".join(parts)


# ── Result types ──────────────────────────────────────────────────────────────

@dataclass
class PipelineResult:
    result: LLMResult
    sq_logs: list[SubQueryLog]
    exec_log: ExecutionLog
    plan: Plan
    route: RouteDecision
    wc: WebContextResult


@dataclass
class SubQueryExecution:
    """Parallel sub-query results before synthesis. Callers choose sync or streaming synthesis."""
    sub_results: list[tuple[str, str]]
    sq_logs: list[SubQueryLog]
    total_prompt_tokens: int
    total_completion_tokens: int
    total_latency_ms: int
    total_cost_usd: float
    synthesis_route: RouteDecision


@dataclass
class PipelineSetup:
    """Intermediate state after the pre-LLM steps (planner → web context → route)."""
    plan: Plan
    route: RouteDecision
    wc: WebContextResult
    enable_native: bool
    planner_ctx: str | None
    running_summary: str
    profile: str
    doc_context: str = ""
    artifact_context: str = ""


# ── Helpers (moved from conversations.py) ────────────────────────────────────

def _conversation_state(conv: Conversation) -> tuple[str, dict | None]:
    """Return (running_summary, active_task) ready to pass to run_planner."""
    summary = conv.running_summary or ""
    task: dict | None = None
    if conv.active_task_json:
        try:
            task = json.loads(conv.active_task_json)
        except (json.JSONDecodeError, ValueError):
            task = None
    return summary, task


def _build_worker_context(plan: Plan, running_summary: str) -> str | None:
    """Combine rolling conversation summary with current-turn context for workers."""
    parts: list[str] = []
    if running_summary:
        parts.append(f"Conversation history:\n{running_summary}")
    if plan.context_summary:
        parts.append(f"Current turn context:\n{plan.context_summary}")
    return "\n\n".join(parts) or None


def _run_sub_queries(
    plan: Plan,
    history: list[dict],
    web_context: str | None,
    enable_native_search: bool,
    deep_research: bool,
    planner_ctx: str | None,
    profile: str,
    on_complete: Callable[[int, str, str | None, int, float | None], None] | None = None,
    doc_context: str = "",
) -> SubQueryExecution:
    """Execute sub-queries in parallel. Caller handles synthesis (sync or streaming)."""
    n = len(plan.sub_queries)
    sub_routes = [
        choose_route(
            sq.query, profile=profile,
            deep_research=deep_research,
            web_search=enable_native_search,
            task_override=sq.task_type or plan.task_type,
            complexity_override=plan.complexity,
            preferred_model=sq.preferred_model,
        )
        for sq in plan.sub_queries
    ]

    sub_results: list[tuple[str, str]] = [("", "")] * n
    _sq_logs: list[SubQueryLog | None] = [None] * n
    total_pt = total_ct = total_latency = 0
    total_cost = 0.0

    def _call(idx: int) -> tuple[int, LLMResult]:
        return idx, invoke_llm(
            plan.sub_queries[idx].query, sub_routes[idx],
            history=history, deep_research=deep_research,
            web_context=web_context, enable_native_search=enable_native_search,
            planner_context=planner_ctx, doc_context=doc_context or None,
        )

    with ThreadPoolExecutor(max_workers=min(n, get_settings().max_decompose_workers)) as pool:
        for future in as_completed([pool.submit(_call, i) for i in range(n)]):
            idx, r = future.result()
            sq = plan.sub_queries[idx]
            sub_results[idx] = (sq.query, r.answer)
            fallback_err = "; ".join(r.fallback_errors) if r.fallback_errors else None
            _sq_logs[idx] = SubQueryLog(
                query=sq.query, task_type=sq.task_type,
                model_requested=sub_routes[idx].primary_model,
                model_used=r.model_used, fallback_error=fallback_err,
                cost_usd=r.estimated_cost_usd, latency_ms=r.latency_ms,
            )
            total_latency += r.latency_ms
            total_pt += r.prompt_tokens or 0
            total_ct += r.completion_tokens or 0
            total_cost += r.estimated_cost_usd or 0.0
            if on_complete:
                on_complete(
                    idx,
                    r.model_used,
                    sq.task_type,
                    r.latency_ms,
                    r.estimated_cost_usd,
                )

    synthesis_route = choose_route(
        plan.intent, profile=profile,
        task_override="summarization", complexity_override="medium",
    )
    return SubQueryExecution(
        sub_results=sub_results,
        sq_logs=[log for log in _sq_logs if log is not None],
        total_prompt_tokens=total_pt,
        total_completion_tokens=total_ct,
        total_latency_ms=total_latency,
        total_cost_usd=total_cost,
        synthesis_route=synthesis_route,
    )


def _run_multi_query(
    plan: Plan,
    history: list[dict],
    web_context: str | None,
    enable_native_search: bool,
    deep_research: bool,
    planner_ctx: str | None,
    profile: str,
    doc_context: str = "",
) -> tuple[LLMResult, list[SubQueryLog]]:
    """Sub-queries in parallel, then non-streaming synthesis."""
    exe = _run_sub_queries(
        plan, history, web_context, enable_native_search, deep_research, planner_ctx, profile,
        doc_context=doc_context,
    )
    final = synthesize_answers(plan.intent, exe.sub_results, exe.synthesis_route)
    combined = LLMResult(
        answer=final.answer,
        model_used=final.model_used,
        latency_ms=exe.total_latency_ms + final.latency_ms,
        prompt_tokens=exe.total_prompt_tokens + (final.prompt_tokens or 0),
        completion_tokens=exe.total_completion_tokens + (final.completion_tokens or 0),
        estimated_cost_usd=exe.total_cost_usd + (final.estimated_cost_usd or 0.0),
    )
    return combined, exe.sq_logs


def _execute_plan(
    plan: Plan,
    route: RouteDecision,
    history: list[dict],
    web_context: str | None,
    enable_native_search: bool,
    deep_research: bool,
    profile: str,
    running_summary: str = "",
    doc_context: str = "",
    artifact_context: str = "",
) -> tuple[LLMResult, list[SubQueryLog]]:
    """Run single-query or multi-query execution depending on the plan."""
    art_ctx = artifact_context or None
    if plan.action == "answer_directly":
        planner_ctx = _build_worker_context(plan, running_summary)
        result = invoke_llm(
            plan.enriched_prompt, route,
            history=history,
            deep_research=False,
            web_context=None,
            enable_native_search=False,
            planner_context=planner_ctx,
            doc_context=doc_context or None,
            artifact_context=art_ctx,
        )
        return result, []

    planner_ctx = _build_worker_context(plan, running_summary)

    if len(plan.sub_queries) > 1:
        return _run_multi_query(
            plan, history, web_context, enable_native_search, deep_research, planner_ctx, profile,
            doc_context=doc_context,
        )

    result = invoke_llm(
        plan.enriched_prompt, route,
        history=history,
        deep_research=deep_research,
        web_context=web_context,
        enable_native_search=enable_native_search,
        planner_context=planner_ctx,
        doc_context=doc_context or None,
        artifact_context=art_ctx,
    )
    return result, []


# ── Public helpers ────────────────────────────────────────────────────────────

def build_exec_log(
    plan: Plan,
    wc: WebContextResult,
    result: LLMResult,
    sq_logs: list[SubQueryLog],
    use_web: bool,
    deep_research: bool,
) -> ExecutionLog:
    """Build ExecutionLog from a completed execution (before planner-cost rollup)."""
    return ExecutionLog(
        planner=PlannerLog(
            model=plan.planner_model,
            latency_ms=plan.planner_latency_ms,
            cost_usd=plan.planner_cost_usd,
            turn_type=plan.turn_type,
            action=plan.action,
            intent=plan.intent,
            enriched_prompt=plan.enriched_prompt,
            needs_web_search=plan.needs_web_search,
            search_query=plan.search_query,
            sub_queries=[
                PlannerSubQuery(query=sq.query, task_type=sq.task_type, preferred_model=sq.preferred_model)
                for sq in plan.sub_queries
            ],
            context_summary=plan.context_summary,
        ),
        web_context=WebContextLog(
            enabled=use_web or deep_research,
            provider=wc.provider,
            sources_count=wc.sources_count,
            search_query=wc.search_query,
            status=wc.status,
        ),
        worker=WorkerLog(
            model=result.model_used,
            latency_ms=result.latency_ms,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            cost_usd=result.estimated_cost_usd,
            sub_queries_count=len(plan.sub_queries) if len(plan.sub_queries) > 1 else 0,
            sub_query_logs=sq_logs,
        ),
        total_cost_usd=(result.estimated_cost_usd or 0.0) + plan.planner_cost_usd,
        total_latency_ms=plan.planner_latency_ms + result.latency_ms,
    )


def build_pipeline_setup(
    req: ConvChatRequest,
    conv: Conversation,
    history: list[dict],
    settings: Settings,
    user_memory: str = "",
    plan: Plan | None = None,
    confirmed_plan: dict | None = None,
) -> PipelineSetup:
    """
    Runs the pre-LLM steps shared between the sync and streaming paths:
    planner → web context → route → annotate route reason.
    Does NOT call any LLM worker. Safe to call before opening a stream.

    If `plan` is provided (e.g. reconstructed from a persisted plan_json for
    an execute-plan re-submission), the planner is not re-run. `confirmed_plan`
    (if any) is applied as overrides onto whichever plan is used.
    """
    profile = req.profile or conv.profile
    running_summary, active_task = _conversation_state(conv)

    doc_context: str = _build_doc_context(req.attached_documents)
    artifact_context: str = ARTIFACT_PROMPTS.get(req.artifact_type or "", "")

    if plan is None:
        plan = run_planner(
            req.message, history, settings.planner_model,
            running_summary=running_summary, active_task=active_task,
            user_memory=user_memory, doc_context=doc_context,
            user_hints={"deep_research": req.deep_research, "document": req.document_requested},
        )
    plan = apply_confirmed_plan(plan, confirmed_plan)
    use_web = (req.web_search or plan.needs_web_search) and plan.action != "answer_directly"
    wc = gather_web_context(plan.search_query or req.message, use_web or req.deep_research)

    route = choose_route(
        req.message, profile, req.force_model, req.deep_research,
        web_search=use_web,
        task_override=plan.task_type,
        complexity_override="low" if plan.action == "answer_directly" else plan.complexity,
        preferred_model=plan.preferred_model,
    )
    if use_web or req.deep_research:
        route.reason = f"{route.reason} {wc.status}"
    if plan.planner_model != "none":
        route.reason = f"[planner:{plan.planner_model} {plan.planner_latency_ms}ms] {route.reason}"

    enable_native = use_web or req.deep_research
    planner_ctx = _build_worker_context(plan, running_summary)

    return PipelineSetup(
        plan=plan, route=route, wc=wc,
        enable_native=enable_native,
        planner_ctx=planner_ctx,
        running_summary=running_summary,
        profile=profile,
        doc_context=doc_context,
        artifact_context=artifact_context,
    )


DOCUMENT_OUTPUT_INSTRUCTIONS = """
OUTPUT FORMAT — IMPORTANT:
After writing the complete document body, append a line containing exactly:
---SUMMARY---
followed by a short bullet-point outline (one bullet per major section/heading
of the document you just wrote), 3-8 bullets. This outline is shown to the user
in the chat as the description of the document — it must stand on its own and
should NOT repeat the document body.
"""


REVISION_SYSTEM_PROMPT = """You are Fronei's document revision editor. You are given a draft document (and its \
chat-facing summary) that was just generated for a mid-level professional to send to senior stakeholders.

Your job is to produce a tightened final version — not a rewrite from scratch. Go through the draft and fix:
- Generic filler phrases ("various stakeholders," "robust solution," "leverage synergies," "in today's \
fast-paced environment," "holistic approach," "moving forward," "best-in-class," "seamless," "cutting-edge," and \
similar) — replace with concrete, specific language drawn from the draft's own content, or remove.
- Sections or sentences that just restate the prompt, repeat another section, or add no new information — \
cut or merge them.
- Analytical points that state an observation without its implication — add the "so what" (impact, risk, or \
recommendation) where it's missing.
- Comparisons or trade-offs presented as one-sided lists — restructure as genuine comparisons (table or explicit \
criteria) where the underlying content supports it.
- Tone/vocabulary mismatches with the stated audience.
- Formatting issues that would break when pasted into Word/Excel (broken tables, inconsistent heading levels, \
stray markdown).

Do NOT:
- Add new facts, figures, names, or claims not present in the draft.
- Change the document's overall structure or doc type unless it's clearly broken.
- Make the document significantly longer — tightening should usually shorten or hold length steady.

If the draft is already strong and has no meaningful issues, return it essentially unchanged — do not introduce \
churn for its own sake.

OUTPUT FORMAT — IMPORTANT: Return the full revised document body in Markdown, followed by a line containing \
exactly ---SUMMARY--- and then a short bullet-point outline (3-8 bullets, one per major section), exactly as in \
the input format. Output nothing else — no commentary about what you changed.
"""


PRESENTATION_REVISION_SYSTEM_PROMPT = """You are Fronei's presentation editor. You are given a draft DeckPlan JSON \
body and its chat-facing summary.

Your job is to tighten the deck plan without changing it into Markdown or prose. Improve:
- Assertion-style slide titles: each title should make a concrete point, not label a topic.
- Slide density: keep bullets short, specific, and scannable; split overloaded slides only if the existing \
content supports it.
- Executive narrative: context -> analysis/options -> recommendation -> next steps.
- Speaker notes: move nuance, caveats, transitions, and presenter talk track into speaker_notes instead of \
overloading slide bullets.
- Generic filler: remove stock phrases and replace them with specific language already present in the draft.

Do NOT:
- Add new facts, figures, names, dates, or claims not present in the draft.
- Convert the body to Markdown.
- Add commentary about the edits.

OUTPUT FORMAT — IMPORTANT:
Return a valid DeckPlan JSON object first, followed by a line containing exactly ---SUMMARY--- and then a \
short bullet-point outline (3-8 bullets). The JSON object must include title, optional subtitle, and slides.
"""


def _personalization_block(user_memory: str, brief: dict) -> str | None:
    """Fold user profile/memory + audience framing into a single guidance block
    for the document writer, so personalization reaches the prompt directly
    rather than only through the planner's context_summary."""
    parts: list[str] = []
    if user_memory:
        parts.append(
            "Background on the author (use to calibrate examples, priorities, and "
            "what can be assumed as known — do not restate this back to them):\n" + user_memory
        )
    audience = brief.get("audience")
    if audience:
        parts.append(
            f"The stated audience for this document is: {audience}. Calibrate vocabulary, technical depth, "
            "and framing (outcomes/risk vs. implementation detail) specifically for that audience — write as "
            "if addressing them directly, not a generic reader."
        )
    if not parts:
        return None
    return "PERSONALIZATION:\n" + "\n\n".join(parts)


def generate_document_output(
    plan: Plan,
    route: RouteDecision,
    history: list[dict],
    wc: WebContextResult,
    planner_ctx: str | None,
    doc_context: str,
    deep_research: bool,
    enable_native_search: bool,
    artifact_context: str = "",
    user_memory: str = "",
    db: object | None = None,
    brand_profile: BrandProfile | None = None,
    user_document_profile: UserDocumentProfile | None = None,
    design_system: str | None = None,
) -> tuple[LLMResult, str, str, str]:
    """Two-pass document generation: draft, then a revision pass that tightens
    against an anti-"AI slop" checklist (generic phrasing, redundancy, missing
    so-what framing, audience mismatch).

    Returns (llm_result, document_body_markdown, chat_summary, doc_type).
    """
    from app.routers.documents import DOC_TYPE_PROMPTS, DOCUMENT_SYSTEM_PROMPT

    brief = plan.document_brief or {}
    doc_type = brief.get("doc_type") or "executive_report"
    quality_mode = normalize_quality_mode(brief.get("quality_mode"))
    presentation_theme = "light" if brief.get("theme") == "light" else "dark"
    parts = [DOCUMENT_SYSTEM_PROMPT, DOC_TYPE_PROMPTS.get(doc_type, DOC_TYPE_PROMPTS["executive_report"])]

    preferences = []
    if brief.get("audience"):
        preferences.append(f"- Audience: {brief['audience']}")
    if brief.get("tone"):
        preferences.append(f"- Tone: {brief['tone']}")
    if brief.get("length"):
        preferences.append(f"- Length/depth: {brief['length']}")
    if brief.get("title"):
        preferences.append(f"- Suggested title: {brief['title']}")
    if doc_type == "presentation":
        preferences.append(f"- Quality mode: {quality_mode}")
        preferences.append(f"- AgentDeck theme: {presentation_theme}")
    if preferences:
        parts.append("User-selected document brief:\n" + "\n".join(preferences))

    personalization = _personalization_block(user_memory, brief)
    if personalization:
        parts.append(personalization)

    # ── Presentations: structured DocPlan planner (Phase 3, #122) ──────────
    # Bypasses the draft+revision DeckPlan-JSON passes below entirely: a
    # two-step structured-output planner (layout selection, then component
    # selection) produces a validated `DocPlan`, serialized as the
    # `document_body` for downstream `build_document_artifact` (#124).
    if doc_type == "presentation":
        from app.services.components import generate_agentdeck_v2_plan

        extra_parts = list(preferences)
        if personalization:
            extra_parts.append(personalization)
        if doc_context:
            extra_parts.append("ATTACHED CONTEXT:\n" + doc_context)
        if enable_native_search and wc.context:
            extra_parts.append("WEB CONTEXT:\n" + wc.context)
        extra_context = "\n\n".join(extra_parts) or None

        doc_plan, _design_plan, plan_result = generate_agentdeck_v2_plan(
            plan.enriched_prompt,
            route,
            theme=presentation_theme,
            extra_context=extra_context,
            db=db,
            quality_mode=quality_mode,
            brand_profile=brand_profile,
            user_document_profile=user_document_profile,
            design_system=design_system or "agentdeck_v1",
        )
        body = doc_plan.model_dump_json()
        bullets = [
            f"- {label}"
            for section in doc_plan.sections
            if (label := (
                section.section_title or section.hero_title
                or section.closing_text or section.section_subtitle
            ))
        ]
        summary = "Here's your presentation — see the attached preview."
        if bullets:
            summary += "\n\n" + "\n".join(bullets[:8])
        return plan_result, body, summary, doc_type

    if artifact_context:
        parts.append(artifact_context)
    parts.append(DOCUMENT_OUTPUT_INSTRUCTIONS)
    sys_prompt = "\n\n".join(parts)

    draft_result = invoke_llm(
        plan.enriched_prompt, route,
        history=history,
        deep_research=deep_research,
        web_context=wc.context if enable_native_search else None,
        enable_native_search=enable_native_search,
        planner_context=planner_ctx,
        doc_context=doc_context or None,
        artifact_context=sys_prompt,
    )

    draft_body, _, draft_summary = draft_result.answer.partition("---SUMMARY---")
    draft_body = draft_body.strip() or draft_result.answer.strip()
    draft_summary = draft_summary.strip() or "Here's your document — see the attached preview."

    # ── Revision pass ────────────────────────────────────────────────────
    # A second pass against a dedicated anti-slop checklist catches generic
    # phrasing, redundancy, and missing "so what" framing that single-pass
    # generation reliably produces. Failures here fall back to the draft.
    revision_input = f"{draft_body}\n\n---SUMMARY---\n{draft_summary}"
    try:
        revision_prompt = PRESENTATION_REVISION_SYSTEM_PROMPT if doc_type == "presentation" else REVISION_SYSTEM_PROMPT
        revision_result = invoke_llm(
            revision_input, route,
            deep_research=False,
            artifact_context=revision_prompt,
        )
        body, _, summary = revision_result.answer.partition("---SUMMARY---")
        body = body.strip()
        summary = summary.strip()
        if not body:
            raise ValueError("empty revision body")
        if doc_type == "presentation" and parse_deck_plan(body) is None:
            raise ValueError("invalid revised DeckPlan JSON")
        result = LLMResult(
            answer=revision_result.answer,
            model_used=draft_result.model_used,
            latency_ms=draft_result.latency_ms + revision_result.latency_ms,
            prompt_tokens=(draft_result.prompt_tokens or 0) + (revision_result.prompt_tokens or 0),
            completion_tokens=(draft_result.completion_tokens or 0) + (revision_result.completion_tokens or 0),
            estimated_cost_usd=(draft_result.estimated_cost_usd or 0.0) + (revision_result.estimated_cost_usd or 0.0),
            fallback_errors=draft_result.fallback_errors + revision_result.fallback_errors,
        )
        return result, body, summary or draft_summary, doc_type
    except Exception:
        return draft_result, draft_body, draft_summary, doc_type


def run_pipeline(
    req: ConvChatRequest,
    conv: Conversation,
    history: list[dict],
    settings: Settings,
    user_memory: str = "",
) -> PipelineResult:
    """Full non-streaming pipeline: plan → web context → route → execute → exec_log."""
    setup = build_pipeline_setup(req, conv, history, settings, user_memory=user_memory)
    result, sq_logs = _execute_plan(
        setup.plan, setup.route, history, setup.wc.context,
        setup.enable_native, req.deep_research, setup.profile,
        running_summary=setup.running_summary,
        doc_context=setup.doc_context,
        artifact_context=setup.artifact_context,
    )
    exec_log = build_exec_log(setup.plan, setup.wc, result, sq_logs,
                              setup.enable_native, req.deep_research)
    return PipelineResult(result=result, sq_logs=sq_logs, exec_log=exec_log,
                          plan=setup.plan, route=setup.route, wc=setup.wc)
