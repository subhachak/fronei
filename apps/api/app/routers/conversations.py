"""Multi-turn conversation router."""
import json
import logging
import queue as _queue_module
import re
import threading as _threading
import time
import inspect
from concurrent.futures import ThreadPoolExecutor, TimeoutError as _FutureTimeoutError
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import func
from fastapi.responses import StreamingResponse

from app.auth import CurrentUser, CurrentUserIsAdmin
from app.config import get_settings
from app.db.models import (
    Conversation, ConversationMessage, ConversationTurn, DocumentTemplate, RequestLog,
    ResearchClaim, ResearchRun, ResearchSource, SessionLocal,
    get_effective_monthly_budget, get_monthly_spend, get_turn_runtime_config,
    get_twin_profile, is_user_pending, is_user_suspended, UserProfile,
)
from app.schemas import (
    ConvChatRequest, ConvChatResponse, ExecutePlanRequest,
    ConversationDetail, ConversationSummary, ConversationTurnOut, ConversationUpdate, MessageOut,
    ExecutionLog, OutputMode, PlannerLog, RouteDecision, StageTiming, SubQueryLog, WebContextLog, WorkerLog,
)
from app.services.llm_gateway import LLMResult, stream_llm, stream_synthesis
from app.services.refinement import should_refine, stream_refinement
from app.services.research_orchestrator import (
    ResearchPipelineResult, ResearchFollowupResult, run_research, run_research_followup,
)
from app.services.research_metadata import research_meta_for_run_id
from app.services import memory_extractor, memory_writer
from app.services.personal_context import build_context
from app.services.budget_guard import enforce_global_monthly_budget
from app.services.chat_pipeline import (
    PipelineSetup, PipelineResult, SubQueryExecution,
    run_pipeline, build_exec_log, build_pipeline_setup, generate_document_output,
    _run_sub_queries, _conversation_state, _build_worker_context, _build_doc_context,
)
from app.routers.documents import build_document_artifact
from app.services.components import log_doc_plan_usage, log_render_qa_failures, normalize_quality_mode
from app.services import plan_gate
from app.services.document_templates import (
    list_document_templates,
    recommend_template_id,
    resolve_template_path,
    template_design_context,
    template_grammar_for_selection,
)
from app.services.design_systems.brand_generator import design_system_id_for_template, write_brand_design_system
from app.services.design_systems.registry import get_design_system
from app.services.brand import (
    brand_profile_from_template_grammar,
    user_document_profile_from_memory,
)
from app.services.planner import apply_confirmed_plan, passthrough, plan_from_dict, plan_to_dict, run_planner
from app.services.prompts import ARTIFACT_PROMPTS
from app.services.turn_graph import graph_trace_payload, run_planning_shadow_graph, state_from_turn
from app.services.turn_graph import ResearchToolInput, execute_deep_research_tool
from app.services.turn_graph import (
    ArtifactRenderToolInput,
    DocumentGenerationToolInput,
    execute_generate_document_tool,
    execute_render_artifact_tool,
    graph_rollout_decision,
)
from app.services.web_context import WebContextResult
from app.services.rate_limit import check_rate_limit, rate_limiter

router = APIRouter(prefix="/conversations", tags=["conversations"])

logger = logging.getLogger(__name__)


def _turn_graph_debug(settings, message: str, **fields) -> None:
    """Emit searchable rollout diagnostics only when explicitly enabled."""

    if not getattr(settings, "turn_graph_debug_enabled", False):
        return
    safe_fields = {
        key: value
        for key, value in fields.items()
        if value is not None
    }
    logger.warning("turn_graph_debug %s %s", message, json.dumps(safe_fields, default=str, sort_keys=True))


# ── Bounded execution for long synchronous pipeline work (#168) ───────────────
# generate_document_output / build_document_artifact run a chain of LLM calls,
# subprocess renders, and QA/repair loops with no internal deadline — a single
# slow provider call or stuck repair loop can hang the worker thread (and thus
# the whole turn) indefinitely. Python can't forcibly preempt a running thread,
# so _run_with_timeout can't kill the underlying work, but it lets the *turn*
# give up and report failure to the user/turn-state after a bounded wait,
# instead of leaving the conversation stuck on "Drafting your document…"
# forever. The abandoned thread is left to finish (or fail) in the background;
# its result is simply discarded.
_DOCUMENT_PIPELINE_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="doc-pipeline")


class PipelineTimeout(Exception):
    """Raised when a bounded pipeline step exceeds its allotted time."""


def _run_with_timeout(fn, *args, timeout_seconds: float, **kwargs):
    future = _DOCUMENT_PIPELINE_EXECUTOR.submit(fn, *args, **kwargs)
    try:
        return future.result(timeout=timeout_seconds)
    except _FutureTimeoutError:
        raise PipelineTimeout(
            f"This is taking longer than expected (over {int(timeout_seconds // 60)} minutes) "
            "and has been stopped. Please retry — a simpler request or a different "
            "template/format may complete faster."
        )


def _document_pipeline_timeout_seconds(db) -> float:
    config = get_turn_runtime_config(db)
    return float(config.get("document_timeout_minutes", 120)) * 60.0


def _graph_research_timeout_seconds(db, mode: str) -> float:
    config = get_turn_runtime_config(db)
    configured_minutes = float(config.get("research_timeout_minutes", 180))
    cap_minutes = 30.0 if mode == "expert" else 20.0
    return max(60.0, min(configured_minutes, cap_minutes) * 60.0)


def _confirmed_research_mode(confirmed_plan, fallback_recommended: bool) -> str:
    """Resolve popup research choices without silently downgrading to quick."""

    if confirmed_plan.deep_research is True:
        return confirmed_plan.research_mode or "deep"
    if confirmed_plan.deep_research is False:
        return "quick"
    if fallback_recommended:
        return confirmed_plan.research_mode or "deep"
    return confirmed_plan.research_mode or "quick"


def _run_graph_research_agent(agent, state, decision, progress_sink):
    if "progress_sink" in inspect.signature(agent.run).parameters:
        return agent.run(state, decision, progress_sink=progress_sink)
    return agent.run(state, decision)


def _stage_timing(stage: str, started: float, **meta) -> StageTiming:
    return StageTiming(
        stage=stage,
        latency_ms=int((time.perf_counter() - started) * 1000),
        meta={k: v for k, v in meta.items() if v is not None},
    )


def _score_to_confidence(score: float | int | None) -> str:
    try:
        value = float(score)
    except (TypeError, ValueError):
        return "medium"
    if value >= 0.75:
        return "high"
    if value <= 0.35:
        return "low"
    return "medium"


def _persist_graph_research_compat_run(
    db,
    *,
    user_id: str,
    conversation_id: int,
    query: str,
    mode: str,
    answer: str,
    sources: list[dict],
    claims: list[dict],
):
    """Persist graph-native research in the legacy research tables.

    The authoritative ResearchAgent is the execution path, but the existing UI
    and follow-up synthesis still use research_runs/research_sources IDs as the
    public contract. Keep that compatibility layer populated until the frontend
    moves fully to agent trace objects.
    """

    now = datetime.now(timezone.utc)
    run = ResearchRun(
        user_id=user_id,
        conversation_id=conversation_id,
        query=query,
        mode=mode,
        status="completed",
        iterations=1,
        max_sources=max(len(sources), 0),
        source_count=len(sources),
        claim_count=len(claims),
        confidence="medium",
        gaps_json="[]",
        contradictions_json="[]",
        verifier_notes=None,
        final_answer=answer,
        created_at=now,
        updated_at=now,
    )
    db.add(run)
    db.flush()

    persisted_sources: list[ResearchSource] = []
    source_by_url: dict[str, ResearchSource] = {}
    for source in sources:
        url = str(source.get("url") or "")
        if not url:
            continue
        row = ResearchSource(
            run_id=run.id,
            title=str(source.get("title") or url)[:500],
            url=url,
            provider=str(source.get("provider") or source.get("source") or "agent_runtime"),
            excerpt=str(source.get("snippet") or source.get("excerpt") or source.get("content") or "")[:1200],
            credibility_score=float(source.get("credibility_score") or 0.7),
            relevance_score=float(source.get("relevance_score") or 0.7),
            freshness_score=float(source.get("freshness_score") or 0.5),
            source_type=source.get("source_type") if isinstance(source.get("source_type"), str) else None,
            source_tier=str(source.get("source_tier") or "tier_2_expert"),
            source_family=source.get("source_family") if isinstance(source.get("source_family"), str) else None,
            source_role_prior=str(source.get("source_role_prior") or "background_context"),
            source_date_confidence=str(source.get("source_date_confidence") or "unknown"),
            admission_status=str(source.get("admission_status") or "admitted"),
            admission_reason=source.get("admission_reason") if isinstance(source.get("admission_reason"), str) else None,
        )
        db.add(row)
        persisted_sources.append(row)
        source_by_url[url] = row

    db.flush()

    persisted_claims: list[ResearchClaim] = []
    fallback_source = persisted_sources[0] if persisted_sources else None
    for claim in claims:
        text = str(claim.get("text") or claim.get("claim") or "")
        if not text:
            continue
        source_url = str(claim.get("source_url") or claim.get("url") or "")
        source = source_by_url.get(source_url) or fallback_source
        if source is None:
            continue
        confidence_score = claim.get("confidence")
        row = ResearchClaim(
            run_id=run.id,
            source_id=source.id,
            claim=text,
            quote=claim.get("quote") if isinstance(claim.get("quote"), str) else None,
            confidence=_score_to_confidence(confidence_score),
            relevance_score=float(claim.get("relevance_score") or confidence_score or 0.7),
            claim_type=str(claim.get("claim_type") or "unknown"),
            claim_role=str(claim.get("claim_role") or "background_context"),
            freshness_risk=str(claim.get("freshness_risk") or "unknown"),
        )
        db.add(row)
        persisted_claims.append(row)

    run_id = run.id
    run_confidence = run.confidence
    source_logs = [
        {
            "id": source.id,
            "title": source.title,
            "url": source.url,
            "provider": source.provider,
            "credibility_score": source.credibility_score,
            "relevance_score": source.relevance_score,
            "freshness_score": source.freshness_score,
            "source_type": source.source_type,
            "source_tier": source.source_tier,
            "source_family": source.source_family,
            "source_role_prior": source.source_role_prior,
            "published_at": source.published_at.isoformat() if source.published_at else None,
            "updated_at": source.updated_at.isoformat() if source.updated_at else None,
            "source_date_confidence": source.source_date_confidence,
            "admission_status": source.admission_status,
            "admission_reason": source.admission_reason,
        }
        for source in persisted_sources
    ]
    claim_logs = [
        {
            "id": claim.id,
            "source_id": claim.source_id,
            "claim": claim.claim,
            "confidence": claim.confidence,
            "relevance_score": claim.relevance_score,
            "claim_type": claim.claim_type,
            "claim_role": claim.claim_role,
            "freshness_risk": claim.freshness_risk,
        }
        for claim in persisted_claims
    ]

    run.source_count = len(persisted_sources)
    run.claim_count = len(persisted_claims)
    db.commit()

    return SimpleNamespace(id=run_id, confidence=run_confidence), source_logs, claim_logs


# ── Error translation ─────────────────────────────────────────────────────────

def _friendly_error(exc: Exception) -> str:
    """Return a short plain-English description of a backend exception."""
    msg = str(exc)
    low = msg.lower()

    if "database is locked" in low:
        return "The database was temporarily busy — please try again."
    if ("402" in msg) or ("credit" in low and ("insufficient" in low or "balance" in low)):
        return "Insufficient API credits on one or more providers. Please top up your balance and retry."
    if "rate limit" in low or "429" in msg or ("quota" in low and "exceeded" in low):
        return "API rate limit reached. Please wait a moment and try again."
    if "401" in msg or "authentication" in low or ("api key" in low and "invalid" in low):
        return "API authentication failed. Check that your provider API keys are configured correctly."
    if ("context" in low or "token" in low) and ("length" in low or "limit" in low or "too long" in low):
        return "The conversation is too long for the selected model. Try starting a new chat."
    if "connection" in low or "timeout" in low or "unreachable" in low or "connect" in low:
        return "Could not reach the AI provider — check your network connection and try again."
    if msg.startswith("All models failed"):
        return "All AI models were unavailable. Check provider status or try a different profile."
    if msg.startswith("Synthesis failed"):
        return "The synthesis step failed after trying all models. Please retry."

    # Generic fallback: take the first sentence, no stack frames
    first_line = msg.split("\n")[0].strip()
    return first_line[:200] + ("…" if len(first_line) > 200 else "")


# ── Serialisation helpers ─────────────────────────────────────────────────────

def _fmt(dt: datetime) -> str:
    return dt.isoformat()


def _summary(conv: Conversation) -> ConversationSummary:
    return ConversationSummary(
        id=conv.public_id, title=conv.title, profile=conv.profile,
        message_count=conv.message_count, total_cost_usd=0.0,
        created_at=_fmt(conv.created_at), updated_at=_fmt(conv.updated_at),
    )


def _get_conversation(db, conv_id: str, user_id: str) -> Conversation:
    """Look up a conversation by its external hex public_id, enforcing ownership."""
    conv = db.query(Conversation).filter(Conversation.public_id == conv_id).first()
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if conv.user_id != user_id:
        raise HTTPException(status_code=403, detail="Forbidden")
    return conv


def _msg_out(m: ConversationMessage, db=None, user_id: str | None = None) -> MessageOut:
    execution_log = None
    if m.execution_log_json:
        try:
            execution_log = json.loads(m.execution_log_json)
        except (json.JSONDecodeError, ValueError):
            execution_log = None
    research = None
    if db is not None and user_id and m.research_run_id:
        research = research_meta_for_run_id(db, m.research_run_id, user_id)
    document_preview = None
    if m.document_preview_json:
        try:
            document_preview = json.loads(m.document_preview_json)
        except (json.JSONDecodeError, ValueError):
            document_preview = None
    return MessageOut(
        id=m.id, role=m.role, content=m.content,
        task_type=m.task_type, complexity=m.complexity, model_used=m.model_used,
        latency_ms=m.latency_ms, prompt_tokens=m.prompt_tokens,
        completion_tokens=m.completion_tokens, estimated_cost_usd=m.estimated_cost_usd,
        execution_log=execution_log,
        research_run_id=m.research_run_id,
        research=research,
        document_preview=document_preview,
        created_at=_fmt(m.created_at),
    )


ACTIVE_TURN_STATUSES = {"pending", "running"}


def _turn_out(turn: ConversationTurn | None) -> ConversationTurnOut | None:
    if not turn:
        return None
    try:
        progress = json.loads(turn.progress_json or "[]")
    except (TypeError, ValueError):
        progress = []
    try:
        lifecycle = json.loads(turn.lifecycle_json or "[]")
    except (TypeError, ValueError):
        lifecycle = []
    try:
        result = json.loads(turn.result_json) if turn.result_json else None
    except (TypeError, ValueError):
        result = None
    return ConversationTurnOut(
        id=turn.public_id,
        status=turn.status,
        turn_kind=turn.turn_kind or "quick",
        progress=progress if isinstance(progress, list) else [],
        lifecycle=lifecycle if isinstance(lifecycle, list) else [],
        result=result if isinstance(result, dict) else None,
        error_message=turn.error_message,
        user_message_id=turn.user_message_id,
        assistant_message_id=turn.assistant_message_id,
        created_at=_fmt(turn.created_at),
        updated_at=_fmt(turn.updated_at),
        completed_at=_fmt(turn.completed_at) if turn.completed_at else None,
    )


def _latest_active_turn(db, conv: Conversation) -> ConversationTurn | None:
    return (
        db.query(ConversationTurn)
        .filter(
            ConversationTurn.conversation_id == conv.id,
            ConversationTurn.status.in_(ACTIVE_TURN_STATUSES),
        )
        .order_by(ConversationTurn.updated_at.desc(), ConversationTurn.id.desc())
        .first()
    )


def mark_stale_conversation_turns(timeout_minutes: int = 30) -> int:
    """Fail old active turns after restart/timeout so reopen UX is deterministic."""
    db = SessionLocal()
    try:
        config = get_turn_runtime_config(db)
        now = datetime.now(timezone.utc)
        cutoffs = {
            "quick": now.replace(tzinfo=None) - timedelta(minutes=config.get("quick_timeout_minutes", timeout_minutes)),
            "research": now.replace(tzinfo=None) - timedelta(minutes=config.get("research_timeout_minutes", timeout_minutes)),
            "document": now.replace(tzinfo=None) - timedelta(minutes=config.get("document_timeout_minutes", timeout_minutes)),
        }
        stale = (
            db.query(ConversationTurn)
            .filter(ConversationTurn.status.in_(ACTIVE_TURN_STATUSES))
            .all()
        )
        marked = 0
        for turn in stale:
            kind = turn.turn_kind or "quick"
            cutoff = cutoffs.get(kind, cutoffs["quick"])
            if turn.updated_at >= cutoff:
                continue
            turn.status = "failed"
            turn.completed_at = now
            turn.updated_at = now
            turn.error_message = "Turn was interrupted by a server restart or timeout. Please retry."
            _append_turn_lifecycle(turn, "stale_failed", {"timeout_kind": kind})
            marked += 1
        db.commit()
        return marked
    finally:
        db.close()


# ── Chat helpers (DB/HTTP-layer concerns) ─────────────────────────────────────

def _update_conversation_state(conv: Conversation, plan, answer: str) -> str:
    """
    Rules-based state update after each response. Returns the summary entry
    that was appended so the background memory writer can identify and replace it.
    """
    # Rolling summary: one line per turn, capped at 12 entries
    snippet = answer[:160].replace("\n", " ").strip()
    if len(answer) > 160:
        snippet += "…"
    entry = f"[{plan.turn_type}] {plan.intent} → {snippet}"
    lines = (conv.running_summary or "").splitlines()
    lines.append(entry)
    conv.running_summary = "\n".join(lines[-12:])

    # Active task state
    if plan.turn_type == "new_task" or not conv.active_task_json:
        task: dict = {
            "goal": plan.intent,
            "constraints": [],
            "completed_steps": [],
            "pending_steps": [sq.query for sq in plan.sub_queries],
            "last_turn_type": plan.turn_type,
        }
    else:
        try:
            task = json.loads(conv.active_task_json)
        except (json.JSONDecodeError, ValueError):
            task = {"goal": plan.intent, "constraints": [], "completed_steps": [], "pending_steps": []}
        task["completed_steps"] = (task.get("completed_steps") or []) + [plan.intent]
        if plan.turn_type == "constraint_change":
            task["constraints"] = (task.get("constraints") or []) + [plan.intent]
        # Remove satisfied pending steps
        done = {plan.intent, *(sq.query for sq in plan.sub_queries)}
        task["pending_steps"] = [s for s in (task.get("pending_steps") or []) if s not in done]
        task["last_turn_type"] = plan.turn_type

    conv.active_task_json = json.dumps(task)
    return entry


def _strip_user_context(message: str) -> str:
    """Remove frontend-injected profile metadata from a message."""
    return re.sub(r"^\[Context:[^\]]*\]\s*", "", message or "", flags=re.IGNORECASE).strip()


def _conversation_title_seed(message: str) -> str:
    cleaned = _strip_user_context(message)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -:\n\t")
    return cleaned or "New chat"


def _title_from_text(text: str, fallback: str = "New chat") -> str:
    cleaned = re.sub(r"\s+", " ", _strip_user_context(text)).strip(" -:\n\t")
    if not cleaned:
        cleaned = fallback
    # Drop trailing sentence fragments less aggressively than raw [:80], but
    # keep the sidebar scannable.
    title = cleaned[:90].rstrip(" ,.;:-")
    return title or fallback


def _maybe_update_title(conv: Conversation, candidate: str) -> None:
    """Set a useful title for the first persisted turn only."""
    if conv.message_count != 0:
        return
    candidate_title = _title_from_text(candidate, conv.title)
    if candidate_title and candidate_title != "New chat":
        conv.title = candidate_title


def _resolve_conversation(db, req: ConvChatRequest, user_id: str) -> tuple[Conversation, str]:
    """Return (conversation, resolved_profile), creating a new conv if needed."""
    if req.conversation_id is None:
        profile = req.profile or "balanced"
        conv = Conversation(
            user_id=user_id, title=_title_from_text(_conversation_title_seed(req.message)), profile=profile, message_count=0
        )
        db.add(conv)
        db.flush()
    else:
        conv = _get_conversation(db, req.conversation_id, user_id)
        profile = req.profile or conv.profile
    return conv, profile


def _build_history(conv: Conversation, db, before_id: int | None = None) -> list[dict]:
    q = db.query(ConversationMessage).filter(ConversationMessage.conversation_id == conv.id)
    if before_id is not None:
        q = q.filter(ConversationMessage.id < before_id)
    msgs = q.order_by(ConversationMessage.id.desc()).limit(20).all()
    return [{"role": m.role, "content": m.content} for m in reversed(msgs)]


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("", response_model=list[ConversationSummary])
def list_conversations(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user_id: str = CurrentUser,
) -> list[ConversationSummary]:
    db = SessionLocal()
    try:
        cost_sq = (
            db.query(
                ConversationMessage.conversation_id,
                func.coalesce(
                    func.sum(ConversationMessage.estimated_cost_usd), 0.0
                ).label("total_cost"),
            )
            .filter(ConversationMessage.role == "assistant")
            .group_by(ConversationMessage.conversation_id)
            .subquery()
        )
        rows = (
            db.query(Conversation, cost_sq.c.total_cost)
            .outerjoin(cost_sq, Conversation.id == cost_sq.c.conversation_id)
            .filter(Conversation.user_id == user_id)
            .order_by(Conversation.updated_at.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )
        return [
            ConversationSummary(
                id=conv.public_id,
                title=conv.title,
                profile=conv.profile,
                message_count=conv.message_count,
                total_cost_usd=float(cost or 0.0),
                created_at=_fmt(conv.created_at),
                updated_at=_fmt(conv.updated_at),
            )
            for conv, cost in rows
        ]
    finally:
        db.close()


@router.get("/{conv_id}", response_model=ConversationDetail)
def get_conversation(conv_id: str, user_id: str = CurrentUser) -> ConversationDetail:
    db = SessionLocal()
    try:
        conv = _get_conversation(db, conv_id, user_id)
        return ConversationDetail(
            **_summary(conv).model_dump(),
            messages=[_msg_out(m, db, user_id) for m in conv.messages],
            active_turn=_turn_out(_latest_active_turn(db, conv)),
        )
    finally:
        db.close()


@router.get("/{conv_id}/turns/{turn_id}", response_model=ConversationTurnOut)
def get_conversation_turn(conv_id: str, turn_id: str, user_id: str = CurrentUser) -> ConversationTurnOut:
    db = SessionLocal()
    try:
        conv = _get_conversation(db, conv_id, user_id)
        turn = (
            db.query(ConversationTurn)
            .filter(
                ConversationTurn.conversation_id == conv.id,
                ConversationTurn.public_id == turn_id,
                ConversationTurn.user_id == user_id,
            )
            .first()
        )
        if not turn:
            raise HTTPException(status_code=404, detail="Turn not found")
        return _turn_out(turn)
    finally:
        db.close()


@router.post("/{conv_id}/turns/{turn_id}/cancel", response_model=ConversationTurnOut)
def cancel_conversation_turn(conv_id: str, turn_id: str, user_id: str = CurrentUser) -> ConversationTurnOut:
    db = SessionLocal()
    try:
        conv = _get_conversation(db, conv_id, user_id)
        turn = (
            db.query(ConversationTurn)
            .filter(
                ConversationTurn.conversation_id == conv.id,
                ConversationTurn.public_id == turn_id,
                ConversationTurn.user_id == user_id,
            )
            .first()
        )
        if not turn:
            raise HTTPException(status_code=404, detail="Turn not found")
        if turn.status in ACTIVE_TURN_STATUSES:
            _mark_turn_cancel_requested(turn.public_id)
            now = datetime.now(timezone.utc)
            turn.status = "cancelled"
            turn.error_message = "Cancelled by user."
            turn.completed_at = now
            turn.updated_at = now
            _append_turn_lifecycle(turn, "cancelled_by_user")
            db.commit()
        return _turn_out(turn)
    finally:
        db.close()


@router.patch("/{conv_id}", response_model=ConversationSummary)
def update_conversation(
    conv_id: str,
    body: ConversationUpdate,
    user_id: str = CurrentUser,
) -> ConversationSummary:
    db = SessionLocal()
    try:
        conv = _get_conversation(db, conv_id, user_id)
        conv.title = body.title.strip()
        conv.updated_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(conv)
        return _summary(conv)
    finally:
        db.close()


@router.delete("/{conv_id}", status_code=204)
def delete_conversation(conv_id: str, user_id: str = CurrentUser) -> None:
    db = SessionLocal()
    try:
        conv = _get_conversation(db, conv_id, user_id)
        db.delete(conv)
        db.commit()
    finally:
        db.close()


@router.delete("/{conv_id}/messages/from/{message_id}", status_code=204)
def truncate_conversation(
    conv_id: str,
    message_id: int,
    user_id: str = CurrentUser,
) -> None:
    """Delete message_id and all subsequent messages in the conversation."""
    db = SessionLocal()
    try:
        conv = _get_conversation(db, conv_id, user_id)
        target = db.get(ConversationMessage, message_id)
        if not target or target.conversation_id != conv.id:
            raise HTTPException(status_code=404, detail="Message not found")
        db.query(ConversationMessage).filter(
            ConversationMessage.conversation_id == conv.id,
            ConversationMessage.id >= message_id,
        ).delete(synchronize_session=False)
        conv.message_count = db.query(ConversationMessage).filter(
            ConversationMessage.conversation_id == conv.id
        ).count()
        conv.updated_at = datetime.now(timezone.utc)
        db.commit()
    finally:
        db.close()


@router.post("/chat", response_model=ConvChatResponse, dependencies=[rate_limiter("chat", "rate_limit_chat_per_minute", 60)])
def chat(req: ConvChatRequest, user_id: str = CurrentUser, is_admin: bool = CurrentUserIsAdmin) -> ConvChatResponse:
    settings = get_settings()
    db = SessionLocal()
    try:
        conv, profile = _resolve_conversation(db, req, user_id)

        if is_user_suspended(db, user_id):
            raise HTTPException(status_code=403, detail="This account is suspended.")
        if is_user_pending(db, user_id):
            raise HTTPException(status_code=403, detail="Your account is pending admin approval.")
        enforce_global_monthly_budget(db, is_admin)
        if req.deep_research and not is_admin:
            check_rate_limit(f"research:{user_id}", settings.rate_limit_research_per_hour, 3600)
        if not is_admin:
            monthly_spend = get_monthly_spend(db, user_id)
            monthly_budget = get_effective_monthly_budget(db, user_id)
            if monthly_spend >= monthly_budget:
                raise HTTPException(
                    status_code=429,
                    detail=f"Monthly budget of ${monthly_budget:.2f} reached "
                           f"(spent ${monthly_spend:.4f} this month). Ask an admin to adjust the limit."
                )

        history = _build_history(conv, db)
        user_memory = build_context(db, user_id)

        db.add(ConversationMessage(conversation_id=conv.id, role="user", content=req.message))
        db.flush()

        pr = run_pipeline(req, conv, history, settings, user_memory=user_memory)

        # Include planner cost in the stored/reported total (exec_log was built pre-rollup)
        pr.result.estimated_cost_usd = (pr.result.estimated_cost_usd or 0.0) + pr.plan.planner_cost_usd

        asst_msg = ConversationMessage(
            conversation_id=conv.id,
            role="assistant",
            content=pr.result.answer,
            task_type=pr.route.task_type,
            complexity=pr.route.complexity,
            model_used=pr.result.model_used,
            latency_ms=pr.result.latency_ms,
            prompt_tokens=pr.result.prompt_tokens,
            completion_tokens=pr.result.completion_tokens,
            estimated_cost_usd=pr.result.estimated_cost_usd,
            execution_log_json=pr.exec_log.model_dump_json(),
        )
        rules_entry = _update_conversation_state(conv, pr.plan, pr.result.answer)
        _maybe_update_title(conv, pr.plan.intent or pr.plan.enriched_prompt or req.message)
        db.add(asst_msg)
        conv.message_count += 2
        conv.updated_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(asst_msg)

        memory_writer.schedule(conv.id, pr.plan.turn_type, pr.plan.intent, pr.result.answer, rules_entry)
        memory_extractor.schedule(user_id, conv.id, req.message, pr.result.answer)

        return ConvChatResponse(
            conversation_id=conv.public_id,
            message_id=asst_msg.id,
            answer=pr.result.answer,
            route=pr.route,
            model_used=pr.result.model_used,
            latency_ms=pr.result.latency_ms,
            estimated_cost_usd=pr.result.estimated_cost_usd,
            prompt_tokens=pr.result.prompt_tokens,
            completion_tokens=pr.result.completion_tokens,
            execution_log=pr.exec_log,
        )
    except HTTPException:
        raise
    except Exception as exc:
        db.add(RequestLog(
            user_id=user_id,
            message=req.message,
            task_type="unknown",
            complexity="medium",
            profile=req.profile or "balanced",
            selected_model="none",
            model_used="none",
            latency_ms=0,
            status="error",
            error=str(exc),
        ))
        db.commit()
        raise HTTPException(status_code=502, detail=_friendly_error(exc))
    finally:
        db.close()


# ── Research routing helpers ─────────────────────────────────────────────────

def _get_last_research_run_id(db, conv: Conversation) -> int | None:
    """Return the research_run_id from the most recent research assistant message."""
    last = (
        db.query(ConversationMessage)
        .filter(
            ConversationMessage.conversation_id == conv.id,
            ConversationMessage.role == "assistant",
            ConversationMessage.research_run_id.isnot(None),
        )
        .order_by(ConversationMessage.id.desc())
        .first()
    )
    return last.research_run_id if last else None


_FOLLOWUP_TURN_TYPES = {"follow_up", "continuation", "correction", "constraint_change"}


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _parse_sse_event(payload: str) -> tuple[str, dict] | None:
    event_type = "message"
    data_str = ""
    for line in payload.splitlines():
        if line.startswith("event: "):
            event_type = line.removeprefix("event: ").strip()
        elif line.startswith("data: "):
            data_str = line.removeprefix("data: ")
    if not data_str:
        return None
    try:
        data = json.loads(data_str)
    except (TypeError, ValueError):
        data = {}
    return event_type, data


def _sse_with_extra(payload: str, extra: dict) -> str:
    parsed = _parse_sse_event(payload)
    if not parsed:
        return payload
    event_type, data = parsed
    data.update(extra)
    return _sse(event_type, data)


def _turn_kind_for_request(req: ConvChatRequest) -> str:
    if req.document_requested:
        return "document"
    if req.deep_research or req.research_mode in {"deep", "expert"}:
        return "research"
    return "quick"


def _should_detach_progressive_job(req: ConvChatRequest) -> bool:
    """Return true when the client should switch to turn polling immediately."""
    if req.deep_research or req.research_mode in {"deep", "expert"}:
        return True
    # Initial document turns may still need the metadata/template finalization
    # popup. Detach only after that confirmation has happened.
    return bool(req.document_requested and req.confirmed_plan is not None)


def _job_started_payload(conv: Conversation, turn: ConversationTurn, message: str) -> dict:
    return {
        "conversation_id": conv.public_id,
        "turn_id": turn.public_id,
        "status": turn.status,
        "turn_kind": turn.turn_kind or "quick",
        "message": message,
    }


# In-memory registry of turn public_ids whose cancellation has been requested.
# Checked on every streamed event (including tokens) without touching the DB;
# avoids a db.refresh() round-trip per token. Cancel endpoints populate this,
# and entries are cleared once the worker loop observes them.
#
# Per-process only — fine for the current single-instance deployment (see
# render.yaml / railway.toml, no `--workers` flag). If the API is ever scaled
# to multiple processes/instances, a cancel request can land on a different
# instance than the one streaming the turn and would silently no-op. Replace
# with a shared store (e.g. Redis pub/sub or a polled DB flag) behind the same
# _mark_turn_cancel_requested / _consume_turn_cancel_requested interface
# before scaling horizontally. (Same caveat as app/services/rate_limit.py.)
_CANCELLED_TURN_IDS: set[str] = set()
_CANCELLED_TURN_LOCK = _threading.Lock()


def _mark_turn_cancel_requested(turn_public_id: str | None) -> None:
    if not turn_public_id:
        return
    with _CANCELLED_TURN_LOCK:
        _CANCELLED_TURN_IDS.add(turn_public_id)


def _consume_turn_cancel_requested(turn_public_id: str | None) -> bool:
    if not turn_public_id:
        return False
    with _CANCELLED_TURN_LOCK:
        if turn_public_id in _CANCELLED_TURN_IDS:
            _CANCELLED_TURN_IDS.discard(turn_public_id)
            return True
    return False


def _is_turn_cancelled(db, turn: ConversationTurn | None, *, allow_db_check: bool = True) -> bool:
    if turn is None or turn.id is None:
        return False
    # Fast in-memory check first (covers same-process cancel-button clicks
    # without a DB hit on every token). The DB refresh (cross-process/admin
    # cancellation) is only performed when `allow_db_check` is set — callers
    # pass False for high-frequency token events so we don't add a query per
    # token, and True for lower-frequency events (pipeline_log, etc.).
    if _consume_turn_cancel_requested(turn.public_id):
        turn.status = "cancelled"
        return True
    if not allow_db_check:
        return False
    # Avoid db.refresh(turn): it would reload ALL columns from the DB and
    # discard the in-memory progress_json/lifecycle_json/status mutations
    # _record_turn_event has been accumulating but hasn't committed yet (those
    # are queued for the background flusher). Instead, check just the status
    # column for a cross-process/admin cancellation.
    status = db.query(ConversationTurn.status).filter(ConversationTurn.id == turn.id).scalar()
    return status == "cancelled"


def _append_turn_progress(turn: ConversationTurn, event_type: str, data: dict) -> None:
    if event_type != "pipeline_log":
        return
    try:
        rows = json.loads(turn.progress_json or "[]")
    except (TypeError, ValueError):
        rows = []
    rows.append({
        "stage": data.get("stage"),
        "message": data.get("message"),
        "ts": datetime.now(timezone.utc).isoformat(),
    })
    turn.progress_json = json.dumps(rows[-80:])


def _append_turn_lifecycle(turn: ConversationTurn, event: str, data: dict | None = None) -> None:
    try:
        rows = json.loads(turn.lifecycle_json or "[]")
    except (TypeError, ValueError):
        rows = []
    rows.append({
        "event": event,
        "ts": datetime.now(timezone.utc).isoformat(),
        **(data or {}),
    })
    turn.lifecycle_json = json.dumps(rows[-120:])


def _append_turn_graph_shadow(
    *,
    settings,
    conv: Conversation,
    turn: ConversationTurn,
    req: ConvChatRequest,
    history: list[dict],
    user_memory: str,
    profile: str,
    is_admin: bool = False,
) -> object | None:
    """Record a shadow graph trace without changing live turn execution."""

    if not graph_rollout_decision(settings).record_shadow_trace:
        return None
    try:
        graph_state = state_from_turn(
            conversation=conv,
            turn=turn,
            user_message=req.message,
            history=history,
            user_memory=user_memory,
            profile=profile,
            user_role="admin" if is_admin else "user",
        )
        graph_state.add_event(
            "shadow_mode",
            "observed",
            "Current chat pipeline remains authoritative",
            turn_kind=turn.turn_kind,
            document_requested=bool(req.document_requested),
            deep_research=bool(req.deep_research),
            web_search=bool(req.web_search),
        )
        graph_state = run_planning_shadow_graph(
            graph_state,
            request=req,
            settings=settings,
            history=history,
            user_memory=user_memory,
            running_summary=graph_state.running_summary,
            active_task=graph_state.active_task,
        )
        _append_turn_lifecycle(turn, "turn_graph_shadow", graph_trace_payload(graph_state))
        return graph_state
    except Exception:
        logger.warning("Failed to record turn graph shadow trace", exc_info=True)
        return None


def _turn_graph_canary_plan(graph_state: object | None):
    """Return a graph-driven plan only for the narrow simple-direct canary."""

    if graph_state is None:
        return None
    plan_data = getattr(graph_state, "plan", None)
    gate_data = getattr(graph_state, "gate", None)
    if not isinstance(plan_data, dict) or not isinstance(gate_data, dict):
        return None
    if plan_data.get("action") != "answer_directly":
        return None
    if plan_data.get("plan_confidence") != "high":
        return None
    if gate_data.get("mode") != "auto":
        return None
    capabilities = gate_data.get("capabilities") or {}
    if any(isinstance(cap, dict) and cap.get("enabled") for cap in capabilities.values()):
        return None
    return plan_from_dict(plan_data, str(plan_data.get("enriched_prompt") or ""))


# ── Background flush of non-terminal turn progress/lifecycle updates ──────────
# In prod (Neon Postgres), every commit is a network round trip. Progress
# updates (pipeline_log stage transitions, "pending -> running") are frequent
# but only matter for "what's this turn up to right now" polling/admin views —
# they don't need to land synchronously on the request thread. We stash the
# latest progress/lifecycle snapshot per turn in memory and let a background
# thread flush it periodically. Terminal events (done/error/cancelled/
# awaiting_confirmation) are still committed synchronously and immediately,
# since those matter for recovery and are low-frequency (once per turn).
#
# Per-process only, same caveat as _CANCELLED_TURN_IDS above: on a
# single-instance deployment this is safe, but on multiple instances each
# would maintain its own pending-flush queue. This is lower-risk than the
# cancellation registry — terminal commits are still synchronous and
# per-instance flushers all write the same rows — but a turn's "live"
# progress would only be visible from the instance handling its stream until
# the next terminal commit. Move to Redis (or a shared queue) if scaled out.
_TURN_PENDING: dict[str, dict] = {}
_TURN_PENDING_LOCK = _threading.Lock()
_TURN_FLUSH_INTERVAL_SECONDS = 1.5
_turn_flush_thread_started = False
_turn_flush_thread_lock = _threading.Lock()


def _ensure_turn_flush_thread() -> None:
    global _turn_flush_thread_started
    if _turn_flush_thread_started:
        return
    with _turn_flush_thread_lock:
        if _turn_flush_thread_started:
            return
        _turn_flush_thread_started = True
        _threading.Thread(target=_turn_flush_loop, daemon=True).start()


def _turn_flush_loop() -> None:
    import time as _time
    while True:
        _time.sleep(_TURN_FLUSH_INTERVAL_SECONDS)
        flush_pending_turn_updates()


def flush_pending_turn_updates() -> None:
    """Write any queued non-terminal turn progress/lifecycle updates."""
    global _TURN_PENDING
    with _TURN_PENDING_LOCK:
        if not _TURN_PENDING:
            return
        batch = _TURN_PENDING
        _TURN_PENDING = {}
    db = SessionLocal()
    try:
        for public_id, values in batch.items():
            db.query(ConversationTurn).filter(
                ConversationTurn.public_id == public_id,
                ConversationTurn.status.in_(ACTIVE_TURN_STATUSES),
            ).update(values, synchronize_session=False)
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()


def _queue_turn_progress_update(turn: ConversationTurn, values: dict) -> None:
    _ensure_turn_flush_thread()
    with _TURN_PENDING_LOCK:
        existing = _TURN_PENDING.get(turn.public_id, {})
        existing.update(values)
        _TURN_PENDING[turn.public_id] = existing


def _record_turn_lifecycle_by_public_id(turn_public_id: str | None, event: str, data: dict | None = None) -> None:
    if not turn_public_id:
        return
    db = SessionLocal()
    try:
        turn = db.query(ConversationTurn).filter(ConversationTurn.public_id == turn_public_id).first()
        if not turn:
            return
        _append_turn_lifecycle(turn, event, data)
        turn.updated_at = datetime.now(timezone.utc)
        db.commit()
    finally:
        db.close()


def _record_turn_event(db, turn: ConversationTurn | None, payload: str) -> None:
    if turn is None:
        return
    parsed = _parse_sse_event(payload)
    if not parsed:
        return
    event_type, data = parsed
    if event_type == "token":
        # Streamed answer tokens can number in the hundreds per turn. Persisting
        # each one would mean a DB commit per token, which serializes the whole
        # response behind disk I/O. Tokens aren't needed for recovery (the final
        # "done" event carries the full answer), so skip them entirely.
        return
    now = datetime.now(timezone.utc)
    turn.updated_at = now
    _append_turn_progress(turn, event_type, data)
    terminal = event_type in ("done", "error", "plan_proposed", "document_brief_proposed")
    if event_type == "done":
        turn.status = "completed"
        turn.completed_at = now
        turn.result_json = json.dumps(data)
        _append_turn_lifecycle(turn, "completed", {"assistant_message_id": data.get("message_id")})
        if data.get("message_id") is not None:
            turn.assistant_message_id = int(data["message_id"])
    elif event_type == "error":
        was_cancelled = turn.status == "cancelled"
        turn.status = "cancelled" if turn.status == "cancelled" else "failed"
        turn.completed_at = now
        turn.error_message = str(data.get("message") or "Unknown error")[:1000]
        _append_turn_lifecycle(turn, "cancelled" if was_cancelled else "failed", {"message": turn.error_message})
    elif event_type == "plan_proposed":
        turn.status = "awaiting_confirmation"
        turn.completed_at = now
        _append_turn_lifecycle(turn, "awaiting_confirmation", {"message_id": data.get("message_id")})
    elif event_type == "document_brief_proposed":
        turn.status = "awaiting_confirmation"
        turn.completed_at = now
        _append_turn_lifecycle(turn, "awaiting_document_brief", {"message_id": data.get("message_id")})
    elif turn.status == "pending":
        turn.status = "running"
        _append_turn_lifecycle(turn, "running")

    if terminal:
        # Drop any not-yet-flushed progress update for this turn — the full
        # state (including progress_json/lifecycle_json mutated above) is
        # written synchronously below, so the background writer doesn't need
        # to (and shouldn't, since the turn is no longer "active") touch it.
        with _TURN_PENDING_LOCK:
            _TURN_PENDING.pop(turn.public_id, None)
        try:
            db.commit()
        except Exception:
            # This commit only updates the conversation_turns bookkeeping row —
            # the actual answer (for "done") was already persisted to
            # conversation_messages by _stream_turn before this event fired.
            # If Neon hiccups on this specific write, don't let it swallow the
            # "done"/"error" SSE event and strand a fully-generated answer
            # behind a generic error. Roll back so the session is usable again,
            # log for visibility, and let the event reach the client; the
            # conversation_turns row will be reconciled (or marked stale) later.
            logger.exception("Failed to persist terminal turn state for turn %s", turn.public_id)
            db.rollback()
    else:
        # Non-terminal: avoid a synchronous commit (and the Neon round trip
        # that comes with it) on the request thread. Queue the latest
        # progress/lifecycle snapshot for the background flusher instead.
        _queue_turn_progress_update(turn, {
            "status": turn.status,
            "progress_json": turn.progress_json,
            "lifecycle_json": turn.lifecycle_json,
            "updated_at": turn.updated_at,
        })


def _turn_completed_done_event(db, turn: ConversationTurn) -> str | None:
    if turn.result_json:
        try:
            return _sse("done", json.loads(turn.result_json))
        except (TypeError, ValueError):
            pass
    if not turn.assistant_message_id:
        return None
    msg = db.get(ConversationMessage, turn.assistant_message_id)
    if not msg:
        return None
    route = RouteDecision(
        task_type=msg.task_type or "unknown",
        complexity=msg.complexity or "medium",
        profile="balanced",
        primary_model=msg.model_used or "",
        fallbacks=[],
        reason="Recovered completed durable turn.",
    )
    return _sse("done", {
        "message_id": msg.id,
        "answer": msg.content,
        "model_used": msg.model_used,
        "latency_ms": msg.latency_ms,
        "estimated_cost_usd": msg.estimated_cost_usd,
        "prompt_tokens": msg.prompt_tokens,
        "completion_tokens": msg.completion_tokens,
        "execution_log": json.loads(msg.execution_log_json) if msg.execution_log_json else None,
        "route": route.model_dump(),
        "was_refined": False,
        "research_run_id": msg.research_run_id,
        "document_preview": json.loads(msg.document_preview_json) if msg.document_preview_json else None,
    })


def _durable_event_iterator(worker, on_client_disconnect=None, detach_on_event=None):
    """Return an SSE iterator while `worker` runs independently in a thread."""
    q: _queue_module.Queue = _queue_module.Queue()
    detached = _threading.Event()

    def _worker() -> None:
        try:
            for event in worker():
                if not detached.is_set():
                    q.put(event)
        except HTTPException as exc:
            if not detached.is_set():
                q.put(_sse("error", {"message": exc.detail}))
        except Exception as exc:
            if not detached.is_set():
                q.put(_sse("error", {"message": _friendly_error(exc)}))
        finally:
            if not detached.is_set():
                q.put(None)

    _threading.Thread(target=_worker, daemon=True).start()

    def _events():
        try:
            while True:
                item = q.get()
                if item is None:
                    break
                yield item
                if detach_on_event and detach_on_event(item):
                    detached.set()
                    break
        except GeneratorExit:
            # Client went away; the worker owns persistence and should continue.
            if on_client_disconnect:
                on_client_disconnect()
            return

    return _events()


def _run_durable_stream(worker, on_client_disconnect=None, detach_on_event=None) -> StreamingResponse:
    """Run a streaming turn independently of the client SSE connection."""
    return StreamingResponse(
        _durable_event_iterator(
            worker,
            on_client_disconnect=on_client_disconnect,
            detach_on_event=detach_on_event,
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _detach_after_job_started(payload: str) -> bool:
    parsed = _parse_sse_event(payload)
    return bool(parsed and parsed[0] == "job_started")


def _pipeline_log(stage: str, message: str, **kwargs) -> str:
    data: dict = {"stage": stage, "message": message}
    data.update(kwargs)
    return f"event: pipeline_log\ndata: {json.dumps(data)}\n\n"


def _planner_selected_log(plan) -> str:
    model = plan.planner_model or "none"
    message = (
        "Planner unavailable — using safe passthrough"
        if model == "none"
        else "Planning complete"
    )
    return _pipeline_log(
        "planning",
        message,
        model=model,
        latency_ms=plan.planner_latency_ms,
        cost_usd=plan.planner_cost_usd,
    )


def _document_finalization_confirmed(req: ConvChatRequest) -> bool:
    if req.confirmed_plan is None:
        return False
    return bool(req.confirmed_plan.document_brief or req.confirmed_plan.document_format)


def _apply_document_generation_defaults(db, user_id: str, plan) -> None:
    """Fill deterministic document defaults when a high-confidence plan can run
    without showing the late document-details modal.

    The planner remains the source of intent; this only resolves mechanical
    choices that the modal would otherwise have supplied, especially template
    selection for presentations.
    """
    brief = dict(plan.document_brief or {})
    if brief.get("doc_type") != "presentation":
        plan.document_brief = brief
        return

    if not plan.document_format_recommendation:
        plan.document_format_recommendation = "pptx"
    if "pptx" not in (plan.document_format_options or []):
        plan.document_format_options = ["pptx", *(plan.document_format_options or [])]

    if not isinstance(brief.get("template_id"), str) or not brief.get("template_id"):
        templates = list_document_templates("presentation", brief, db=db, user_id=user_id)
        recommended = next((t for t in templates if t.get("recommended")), None)
        selected = recommended or (templates[0] if templates else None)
        if selected and selected.get("id"):
            brief["template_id"] = str(selected["id"])
            if not brief.get("theme"):
                is_brand = bool(selected.get("user_template")) or str(selected.get("design_system") or "").startswith("brand_")
                brief["theme"] = "light" if is_brand else "dark"
    plan.document_brief = brief


def _should_show_document_finalization(plan) -> bool:
    if plan.plan_confidence != "high" or plan.open_questions:
        return True
    brief = dict(plan.document_brief or {})
    doc_type = brief.get("doc_type")
    if not doc_type:
        return True
    if doc_type == "presentation":
        return False
    return not bool(plan.document_format_recommendation or plan.document_format_options)


def _should_propose_plan_before_execution(gate) -> bool:
    """Return True when the planner needs user input before any capability runs.

    Capability confirmations (web/deep research) and clarifying questions are
    both pre-execution decisions. Document finalization happens later, after
    research/web/context gathering has either completed or been declined.
    """
    return gate.mode == "confirm" or bool(gate.open_questions)


def _document_finalization_payload(db, user_id: str, conv: Conversation, user_msg: ConversationMessage, plan, gate) -> dict:
    doc_cap = gate.capabilities["document"]
    extra = doc_cap.extra or {}
    brief = dict(extra.get("brief") or plan.document_brief or {})
    brief = {k: v for k, v in brief.items() if not str(k).startswith("_")}
    format_options = list(extra.get("format_options") or plan.document_format_options or ["markdown"])
    supported_formats = list(extra.get("supported_formats") or ["docx", "markdown"])
    force_pptx = brief.get("doc_type") == "presentation"
    if force_pptx:
        if "pptx" not in format_options:
            format_options = ["pptx", *format_options]
        if "pptx" in supported_formats:
            plan.document_format_recommendation = "pptx"
    format_recommendation = (
        extra.get("format_recommendation")
        or plan.document_format_recommendation
        or (format_options[0] if format_options else "markdown")
    )
    if force_pptx and "pptx" in supported_formats:
        format_recommendation = "pptx"
    templates = list_document_templates(brief.get("doc_type"), brief, db=db, user_id=user_id)
    recommended_template_id = recommend_template_id(brief)
    recommended = next((t for t in templates if t.get("recommended")), None)
    if recommended:
        recommended_template_id = str(recommended.get("id"))
    elif not any(t.get("id") == recommended_template_id for t in templates):
        recommended_template_id = str(templates[0].get("id")) if templates else "fronei-default"
    template_design = None
    if brief.get("doc_type") == "presentation":
        try:
            grammar = template_grammar_for_selection(db, user_id, recommended_template_id, brief)
            template_design = {
                "mode": grammar.get("mode"),
                "template_id": grammar.get("template_id"),
                "available_slide_types": grammar.get("available_slide_types"),
                "fonts": grammar.get("fonts"),
                "colors": grammar.get("colors"),
            }
        except Exception:
            logger.exception("Failed to inspect presentation template grammar")
    return {
        "conversation_id": conv.public_id,
        "message_id": user_msg.id,
        "brief": brief,
        "format_options": format_options,
        "format_recommendation": format_recommendation,
        "supported_formats": supported_formats,
        "templates": templates,
        "template_recommendation": recommended_template_id,
        "template_design": template_design,
    }


def _maybe_propose_document_finalization(db, user_id: str, conv: Conversation, user_msg: ConversationMessage, req: ConvChatRequest, plan, gate) -> str | None:
    if not (plan.wants_document_output and gate.capabilities["document"].enabled):
        return None
    if _document_finalization_confirmed(req):
        return None
    _apply_document_generation_defaults(db, user_id, plan)
    if not _should_show_document_finalization(plan):
        return None
    user_msg.plan_json = json.dumps(plan_to_dict(plan))
    conv.updated_at = datetime.now(timezone.utc)
    db.commit()
    return _sse("document_brief_proposed", _document_finalization_payload(db, user_id, conv, user_msg, plan, gate))


def _remember_document_source_context(plan, context: str, research_run_id: int | None = None) -> None:
    if not context:
        return
    brief = dict(plan.document_brief or {})
    # Keep this under the request payload size limit while retaining enough
    # source-grounded material for the final artifact pass after confirmation.
    brief["_source_context"] = context[:28000]
    if research_run_id is not None:
        brief["_source_research_run_id"] = research_run_id
    plan.document_brief = brief


def _document_context_for_generation(base_context: str, plan) -> str:
    source_context = (plan.document_brief or {}).get("_source_context")
    if isinstance(source_context, str) and source_context.strip():
        return "\n\n".join(part for part in [base_context, source_context] if part)
    return base_context


_SUPPORTED_DOCUMENT_OUTPUT_FORMATS = {"markdown", "docx", "xlsx", "pptx"}


def _document_output_format(plan, gate) -> str:
    brief = dict(plan.document_brief or {})
    doc_cap = gate.capabilities["document"]
    extra = doc_cap.extra or {}
    supported = set(extra.get("supported_formats") or _SUPPORTED_DOCUMENT_OUTPUT_FORMATS)
    supported = supported.intersection(_SUPPORTED_DOCUMENT_OUTPUT_FORMATS) or {"markdown"}
    if brief.get("doc_type") == "presentation" and "pptx" in supported:
        return "pptx"
    recommendation = plan.document_format_recommendation or extra.get("format_recommendation")
    if recommendation in supported:
        return str(recommendation)
    for option in list(plan.document_format_options or extra.get("format_options") or []):
        if option in supported:
            return str(option)
    return "markdown"


def _document_quality_mode(plan) -> str:
    return normalize_quality_mode((plan.document_brief or {}).get("quality_mode"))


def _should_defer_artifact_qa(fmt: str, quality_mode: str) -> bool:
    return fmt == "pptx" and normalize_quality_mode(quality_mode) == "executive"


def _schedule_document_preview_polish(
    *,
    message_id: int,
    doc_type: str,
    title: str,
    doc_body: str,
    fmt: str,
    template_id: str | None,
    template_path: str | None,
    quality_mode: str,
) -> None:
    if not _should_defer_artifact_qa(fmt, quality_mode):
        return

    def _worker() -> None:
        started = time.perf_counter()
        db = SessionLocal()
        try:
            polished_preview = build_document_artifact(
                title or "Fronei document",
                doc_body,
                doc_type,
                fmt,
                template_id=template_id,
                template_path=template_path,
                quality_mode=quality_mode,
                defer_render_qa=False,
            )
            msg = db.get(ConversationMessage, message_id)
            if msg is None:
                return
            if _document_failure_answer(polished_preview):
                return
            msg.document_preview_json = json.dumps(polished_preview)
            try:
                exec_log = ExecutionLog.model_validate_json(msg.execution_log_json or "{}")
                timings = list(exec_log.stage_timings or [])
                timings.append(_stage_timing(
                    "artifact_polish",
                    started,
                    doc_type=doc_type,
                    format=polished_preview.get("format") if isinstance(polished_preview, dict) else fmt,
                    quality_mode=quality_mode,
                ))
                exec_log.stage_timings = timings
                msg.execution_log_json = exec_log.model_dump_json()
            except Exception:
                logger.exception("Failed to append document polish timing")
            log_render_qa_failures(db, doc_type, doc_body, polished_preview.get("render_qa"))
            db.commit()
        except Exception:
            logger.exception("Background document polish failed")
        finally:
            db.close()

    _DOCUMENT_PIPELINE_EXECUTOR.submit(_worker)


def _coerce_presentation_brief_for_pptx(plan, fmt: str) -> None:
    if fmt != "pptx":
        return
    brief = dict(plan.document_brief or {})
    if brief.get("doc_type") != "presentation":
        brief["source_doc_type"] = brief.get("doc_type") or "document"
        brief["doc_type"] = "presentation"
        plan.document_brief = brief


def _presentation_artifact_context(base_context: str, db, user_id: str, plan) -> str:
    brief = dict(plan.document_brief or {})
    if brief.get("doc_type") != "presentation":
        return base_context or ""
    template_id = brief.get("template_id")
    if not isinstance(template_id, str) or not template_id:
        template_id = recommend_template_id(brief)
    try:
        grammar = template_grammar_for_selection(db, user_id, template_id, brief)
        design_context = template_design_context(grammar)
    except Exception:
        logger.exception("Failed to build presentation design context")
        design_context = ""
    return "\n\n".join(part for part in [base_context or "", design_context] if part)


def _load_profile_json_for_documents(db, user_id: str) -> dict:
    try:
        profile = db.query(UserProfile).filter(UserProfile.user_id == user_id).first()
    except Exception:
        logger.exception("Failed to load user document profile")
        return {}
    if not profile or not profile.profile_json:
        return {}
    try:
        data = json.loads(profile.profile_json)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _document_generation_profiles(db, user_id: str, plan) -> tuple[object | None, str | None, object | None]:
    brief = dict(plan.document_brief or {})
    if brief.get("doc_type") != "presentation":
        return None, None, user_document_profile_from_memory(user_id, _load_profile_json_for_documents(db, user_id))

    user_profile = user_document_profile_from_memory(user_id, _load_profile_json_for_documents(db, user_id))
    template_id = brief.get("template_id")
    if not isinstance(template_id, str) or not template_id:
        template_id = recommend_template_id(brief)
    brand_profile = None
    try:
        grammar = template_grammar_for_selection(db, user_id, template_id, brief)
        brand_profile = brand_profile_from_template_grammar(grammar, user_id=user_id)
    except Exception:
        logger.exception("Failed to build AgentDeck brand profile")

    # #183: if the selected template is a user-uploaded template with a
    # generated brand design_system (#181/#182), render with that design
    # system instead of the default "agentdeck_v1".
    design_system_id: str | None = None
    if isinstance(template_id, str) and template_id:
        try:
            row = (
                db.query(DocumentTemplate)
                .filter(
                    DocumentTemplate.user_id == user_id,
                    DocumentTemplate.public_id == template_id,
                    DocumentTemplate.is_active.is_(True),
                )
                .first()
            )
            if row is not None:
                design_system_id = row.design_system_id or design_system_id_for_template(user_id, row.public_id)
                try:
                    get_design_system(design_system_id)
                except KeyError:
                    if brand_profile is not None:
                        write_brand_design_system(brand_profile, design_system_id=design_system_id)
                        row.design_system_id = design_system_id
                        row.updated_at = datetime.now(timezone.utc)
                        db.commit()
        except Exception:
            logger.exception("Failed to resolve brand design_system_id for template %s", template_id)

    return brand_profile, design_system_id, user_profile


def _document_failure_answer(document_preview: dict | None) -> str | None:
    failure = (document_preview or {}).get("generation_failure")
    if not isinstance(failure, dict):
        return None
    message = str(failure.get("user_message") or "").strip()
    if not message:
        message = "Document generation failed. Please retry."
    return f"{message} I kept the request state intact so you can retry generation."


# ── Streaming endpoint ────────────────────────────────────────────────────────

@router.post("/chat/stream", dependencies=[rate_limiter("chat", "rate_limit_chat_per_minute", 60)])
def chat_stream(req: ConvChatRequest, user_id: str = CurrentUser, is_admin: bool = CurrentUserIsAdmin) -> StreamingResponse:
    """
    Same pipeline as /chat but streams tokens via Server-Sent Events.
    Events: start → pipeline_log × N → token × N → done  (or error on failure)
    """
    settings = get_settings()
    turn_public_id: dict[str, str | None] = {"id": None}

    def run_turn_worker():
        db = SessionLocal()
        turn: ConversationTurn | None = None
        try:
            conv, profile = _resolve_conversation(db, req, user_id)

            if req.client_request_id:
                existing_turn = (
                    db.query(ConversationTurn)
                    .filter(
                        ConversationTurn.user_id == user_id,
                        ConversationTurn.client_request_id == req.client_request_id,
                    )
                    .first()
                )
                if existing_turn:
                    turn_public_id["id"] = existing_turn.public_id
                    _append_turn_lifecycle(existing_turn, "idempotent_replay", {"status": existing_turn.status})
                    existing_turn.updated_at = datetime.now(timezone.utc)
                    db.commit()
                    existing_conv = db.get(Conversation, existing_turn.conversation_id)
                    yield _sse("start", {
                        "conversation_id": existing_conv.public_id if existing_conv else conv.public_id,
                        "turn_id": existing_turn.public_id,
                    })
                    if existing_turn.status == "completed":
                        done_event = _turn_completed_done_event(db, existing_turn)
                        if done_event:
                            yield done_event
                            return
                    if existing_turn.status in ("failed", "cancelled"):
                        yield _sse("error", {
                            "message": existing_turn.error_message or "Turn did not complete successfully.",
                        })
                        return

                    # #172: the turn is still active (pending/running). Rather
                    # than emitting one informational log and ending the
                    # stream with no done/error — which strands a
                    # reconnecting client mid-"Drafting…" with nothing to
                    # react to — poll until the turn reaches a terminal state
                    # and emit the corresponding done/error event.
                    yield _pipeline_log(
                        "working",
                        f"Turn is already {existing_turn.status}; reconnecting to the conversation when it finishes.",
                        turn_id=existing_turn.public_id,
                    )
                    poll_deadline = time.monotonic() + _document_pipeline_timeout_seconds(db) + 60
                    while existing_turn.status in ("pending", "running") and time.monotonic() < poll_deadline:
                        time.sleep(2)
                        db.expire(existing_turn)
                        refreshed = db.get(ConversationTurn, existing_turn.id)
                        if refreshed is None:
                            yield _sse("error", {"message": "Turn no longer exists."})
                            return
                        existing_turn = refreshed

                    if existing_turn.status == "completed":
                        done_event = _turn_completed_done_event(db, existing_turn)
                        if done_event:
                            yield done_event
                            return
                        yield _sse("error", {"message": "Turn completed but its result could not be recovered."})
                        return
                    if existing_turn.status in ("failed", "cancelled"):
                        yield _sse("error", {
                            "message": existing_turn.error_message or "Turn did not complete successfully.",
                        })
                        return

                    # Still not terminal (e.g. stuck past the timeout, or
                    # awaiting_confirmation, which can't be faithfully
                    # replayed on a reconnect).
                    yield _sse("error", {
                        "message": "Still working on this in the background. Reload the page in a bit to check for results.",
                    })
                    return

            if is_user_suspended(db, user_id):
                raise HTTPException(status_code=403, detail="This account is suspended.")
            if is_user_pending(db, user_id):
                raise HTTPException(status_code=403, detail="Your account is pending admin approval.")
            enforce_global_monthly_budget(db, is_admin)
            if req.deep_research and not is_admin:
                check_rate_limit(f"research:{user_id}", settings.rate_limit_research_per_hour, 3600)
            if not is_admin:
                monthly_spend = get_monthly_spend(db, user_id)
                monthly_budget = get_effective_monthly_budget(db, user_id)
                if monthly_spend >= monthly_budget:
                    raise HTTPException(
                        status_code=429,
                        detail=f"Monthly budget of ${monthly_budget:.2f} reached "
                               f"(spent ${monthly_spend:.4f} this month). Ask an admin to adjust the limit."
                    )

            history = _build_history(conv, db)
            user_memory = build_context(db, user_id)
            user_msg = ConversationMessage(conversation_id=conv.id, role="user", content=req.message)
            db.add(user_msg)
            db.flush()
            turn = ConversationTurn(
                user_id=user_id,
                conversation_id=conv.id,
                user_message_id=user_msg.id,
                client_request_id=req.client_request_id,
                status="running",
                turn_kind=_turn_kind_for_request(req),
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
            db.add(turn)
            db.flush()
            turn_public_id["id"] = turn.public_id
            _append_turn_lifecycle(turn, "created", {"kind": turn.turn_kind})
            graph_state = _append_turn_graph_shadow(
                settings=settings,
                conv=conv,
                turn=turn,
                req=req,
                history=history,
                user_memory=user_memory,
                profile=profile,
                is_admin=is_admin,
            )
            graph_canary_plan = _turn_graph_canary_plan(graph_state)
            if (
                graph_canary_plan is not None
                and graph_rollout_decision(settings, tool_name="answer_directly").allow_canary_execution
            ):
                _append_turn_lifecycle(turn, "turn_graph_canary", {
                    "mode": "simple_direct",
                    "planner_model": graph_canary_plan.planner_model,
                    "action": graph_canary_plan.action,
                    "plan_confidence": graph_canary_plan.plan_confidence,
                })
            else:
                graph_canary_plan = None
            db.commit()

            if _should_detach_progressive_job(req):
                message = (
                    "Fronei is researching in the background…"
                    if turn.turn_kind == "research"
                    else "Fronei is generating your document in the background…"
                )
                payload = _sse_with_extra(
                    _pipeline_log("working", message, turn_kind=turn.turn_kind),
                    {"turn_id": turn.public_id},
                )
                _record_turn_event(db, turn, payload)
                yield payload
                payload = _sse("job_started", _job_started_payload(conv, turn, message))
                _record_turn_event(db, turn, payload)
                yield payload

            for payload in _stream_turn(
                db, conv, req, user_id, is_admin, settings, history, user_memory, profile, user_msg,
                preloaded_plan=graph_canary_plan,
            ):
                parsed_event = _parse_sse_event(payload)
                is_token = bool(parsed_event) and parsed_event[0] == "token"
                if _is_turn_cancelled(db, turn, allow_db_check=not is_token):
                    payload = _sse("error", {"message": "Turn cancelled."})
                    _record_turn_event(db, turn, payload)
                    yield payload
                    return
                payload = _sse_with_extra(payload, {"turn_id": turn.public_id})
                _record_turn_event(db, turn, payload)
                yield payload
            return

        except HTTPException as exc:
            payload = _sse("error", {"message": exc.detail})
            _record_turn_event(db, turn, payload)
            yield payload
        except Exception as exc:
            payload = _sse("error", {"message": _friendly_error(exc)})
            _record_turn_event(db, turn, payload)
            yield payload
        finally:
            db.close()

    return _run_durable_stream(
        run_turn_worker,
        on_client_disconnect=lambda: _record_turn_lifecycle_by_public_id(turn_public_id["id"], "client_disconnected"),
        detach_on_event=_detach_after_job_started,
    )


def _stream_turn(db, conv, req, user_id, is_admin, settings, history, user_memory, profile, user_msg, preloaded_plan=None):
            yield _sse("start", {"conversation_id": conv.public_id})
            yield _pipeline_log("planning", "Analyzing your request…")

            twin_profile = get_twin_profile(db, user_id)
            output_mode: OutputMode = req.output_mode
            research_mode = req.research_mode if req.research_mode != "quick" else ("deep" if req.deep_research else "quick")
            _turn_graph_debug(
                settings,
                "stream_turn_start",
                conversation_id=conv.public_id,
                user_id=user_id,
                request_research_mode=req.research_mode,
                resolved_research_mode=research_mode,
                deep_research=req.deep_research,
                document_requested=req.document_requested,
                web_search=req.web_search,
                turn_graph_enabled=getattr(settings, "turn_graph_enabled", False),
                turn_graph_authoritative=getattr(settings, "turn_graph_authoritative", False),
                orchestrator_enabled=getattr(settings, "orchestrator_enabled", False),
            )

            if research_mode != "quick":
                _turn_graph_debug(
                    settings,
                    "research_branch_entered",
                    conversation_id=conv.public_id,
                    user_id=user_id,
                    research_mode=research_mode,
                )
                # ── Adaptive research routing ─────────────────────────────
                # If a prior research run exists in this conversation, run the
                # planner first to classify the turn. Follow-ups / continuations
                # synthesize from the existing evidence store instead of
                # re-running the full pipeline (10-20s vs 3-7 minutes).
                #
                # If a plan was already produced (e.g. confirmed via the
                # plan_proposed → execute-plan round trip), reuse it instead of
                # re-running the planner, applying any confirmed overrides
                # (deep_research toggle, etc.) the user made in the popup.
                existing_run_id = _get_last_research_run_id(db, conv)
                running_summary, active_task = _conversation_state(conv)
                if preloaded_plan is not None:
                    plan = preloaded_plan
                    if req.confirmed_plan:
                        apply_confirmed_plan(plan, req.confirmed_plan.model_dump(exclude_none=True))
                else:
                    plan = run_planner(
                        req.message, history, settings.planner_model,
                        running_summary=running_summary,
                        active_task=active_task,
                        user_memory=user_memory,
                        user_hints={"deep_research": req.deep_research, "document": req.document_requested},
                    )
                yield _planner_selected_log(plan)
                research_query = plan.enriched_prompt or req.message
                yield _pipeline_log(
                    "routing",
                    "Preparing source-grounded research from conversation context",
                    intent=plan.intent or req.message,
                    turn_type=plan.turn_type,
                    research_query=research_query[:500],
                )

                is_followup = False
                if existing_run_id:
                    is_followup = plan.turn_type in _FOLLOWUP_TURN_TYPES

                # Surface the bundled confirmation popup before running any
                # deep/expert research — whether this is a brand-new research
                # task or a follow-up. For follow-ups, this also covers the
                # case where the user explicitly re-toggled "Research" and the
                # planner set recommend_deep_research=True per that hint; we
                # must surface plan_proposed instead of silently downgrading
                # to a cheap follow-up synthesis that ignores that signal.
                # Once the user has confirmed (req.confirmed_plan set), skip
                # straight to execution.
                if req.confirmed_plan is None:
                    pre_gate = plan_gate.evaluate(
                        plan,
                        explicit_document_request=bool(req.document_requested),
                    )
                    if _should_propose_plan_before_execution(pre_gate):
                        user_msg.plan_json = json.dumps(plan_to_dict(plan))
                        conv.updated_at = datetime.now(timezone.utc)
                        db.commit()
                        yield _sse("plan_proposed", {
                            "conversation_id": conv.public_id,
                            "message_id": user_msg.id,
                            "plan_confidence": pre_gate.plan_confidence,
                            "open_questions": pre_gate.open_questions,
                            "capabilities": {k: v.to_dict() for k, v in pre_gate.capabilities.items()},
                            "intent": plan.intent,
                        })
                        return

                # The plan_proposed popup only surfaces a "Research depth"
                # (Deep/Expert) picker when deep_research is enabled, and
                # always sends `research_mode` in that case. So a confirmed
                # plan carrying `deep_research: true` *and* an explicit
                # `research_mode` represents a deliberate decision to (re-)run
                # research for this turn — honor it with fresh research
                # rather than silently downgrading to the cheap follow-up
                # synthesis, which performs no new web searching. A plain
                # `deep_research: true` with no research_mode (e.g. "keep
                # research context on for this continuation" without going
                # through the depth picker) keeps the normal follow-up fast
                # path.
                confirmed_fresh_research = bool(
                    req.confirmed_plan is not None
                    and req.confirmed_plan.deep_research is True
                    and req.confirmed_plan.research_mode is not None
                )

                if is_followup and existing_run_id and not confirmed_fresh_research:
                    # ── Fast path: synthesize from existing evidence ───────
                    followup_holder: list[ResearchFollowupResult | None] = [None]
                    followup_error: list[BaseException | None] = [None]
                    followup_q: _queue_module.Queue = _queue_module.Queue()

                    def _run_followup_worker() -> None:
                        f_db = SessionLocal()
                        try:
                            def progress(stage: str, message: str, extra: dict) -> None:
                                followup_q.put({"stage": stage, "message": message, **extra})
                            followup_holder[0] = run_research_followup(
                                f_db,
                                user_id=user_id,
                                run_id=existing_run_id,
                                follow_up_question=research_query,
                                profile=profile,
                                force_model=req.force_model,
                                progress=progress,
                            )
                        except BaseException as exc:
                            followup_error[0] = exc
                        finally:
                            f_db.close()
                            followup_q.put(None)

                    followup_started = time.perf_counter()
                    ft = _threading.Thread(target=_run_followup_worker, daemon=True)
                    ft.start()

                    while True:
                        upd = followup_q.get()
                        if upd is None:
                            break
                        s, m = upd.pop("stage"), upd.pop("message")
                        yield _pipeline_log(s, m, **upd)

                    if followup_error[0]:
                        raise followup_error[0]
                    followup = followup_holder[0]
                    if followup is None:
                        yield _sse("error", {"message": "Follow-up synthesis returned no result"})
                        return

                    result = followup.result
                    final_answer = result.answer
                    route = followup.route

                    gate = plan_gate.evaluate(
                        plan,
                        explicit_document_request=bool(req.document_requested or req.confirmed_plan),
                    )
                    document_preview = None
                    defer_render_qa = False
                    quality_mode = "standard"
                    title = plan.intent or "Fronei document"
                    doc_body = ""
                    doc_type = str((plan.document_brief or {}).get("doc_type") or "presentation")
                    fmt = "markdown"
                    template_id = None
                    template_path = None
                    if plan.wants_document_output and gate.capabilities["document"].enabled:
                        yield _pipeline_log("working", "Drafting your document from research findings…")
                        fmt = _document_output_format(plan, gate)
                        _coerce_presentation_brief_for_pptx(plan, fmt)

                        followup_context_parts = [f"Research follow-up findings:\n{result.answer}"]
                        if followup.source_logs:
                            src_lines = "\n".join(
                                f"- {s.get('title', '')}: {s.get('url', '')}" for s in followup.source_logs[:15]
                            )
                            followup_context_parts.append(f"Sources:\n{src_lines}")
                        followup_context = "\n\n".join(followup_context_parts)
                        base_doc_context = _build_doc_context(req.attached_documents)
                        doc_context = f"{base_doc_context}\n\n{followup_context}".strip() if base_doc_context else followup_context

                        planner_ctx = _build_worker_context(plan, running_summary)
                        empty_wc = WebContextResult(context=None, status="", provider="", sources_count=0, search_query=None)
                        artifact_context = _presentation_artifact_context(
                            ARTIFACT_PROMPTS.get(req.artifact_type or "", ""),
                            db, user_id, plan,
                        )
                        followup_cost = result.estimated_cost_usd or 0.0
                        _remember_document_source_context(plan, doc_context, existing_run_id)
                        finalization_event = _maybe_propose_document_finalization(db, user_id, conv, user_msg, req, plan, gate)
                        if finalization_event:
                            yield finalization_event
                            return
                        brand_profile, design_system_id, user_document_profile = _document_generation_profiles(db, user_id, plan)

                        try:
                            result, doc_body, chat_summary, doc_type = _run_with_timeout(
                                generate_document_output,
                                plan, route, history, empty_wc, planner_ctx,
                                doc_context, False, False,
                                artifact_context=artifact_context,
                                user_memory=user_memory,
                                db=db,
                                brand_profile=brand_profile,
                                user_document_profile=user_document_profile,
                                design_system=design_system_id,
                                timeout_seconds=_document_pipeline_timeout_seconds(db),
                            )
                        except PipelineTimeout as exc:
                            document_preview = {"generation_failure": {"user_message": str(exc)}}
                            final_answer = _document_failure_answer(document_preview) or str(exc)
                            for chunk in [final_answer[i:i + 80] for i in range(0, len(final_answer), 80)]:
                                yield _sse("token", {"text": chunk})
                            result.estimated_cost_usd = (
                                (result.estimated_cost_usd or 0.0)
                                + followup_cost
                                + plan.planner_cost_usd
                            )
                        else:
                            log_doc_plan_usage(db, doc_type, doc_body)
                            yield _pipeline_log("working", "Document plan ready — preparing preview…", doc_type=doc_type, format=fmt)
                            for chunk in [chat_summary[i:i + 80] for i in range(0, len(chat_summary), 80)]:
                                yield _sse("token", {"text": chunk})
                            final_answer = chat_summary
                            result.estimated_cost_usd = (
                                (result.estimated_cost_usd or 0.0)
                                + followup_cost
                                + plan.planner_cost_usd
                            )
                            title = (plan.document_brief or {}).get("title") or plan.intent
                            template_id = (plan.document_brief or {}).get("template_id")
                            template_path = resolve_template_path(db, user_id, template_id if isinstance(template_id, str) else None)
                            quality_mode = _document_quality_mode(plan)
                            defer_render_qa = _should_defer_artifact_qa(fmt, quality_mode)
                            try:
                                yield _pipeline_log("working", f"Rendering {fmt.upper()} artifact…", doc_type=doc_type, format=fmt)
                                document_preview = _run_with_timeout(
                                    build_document_artifact,
                                    title or "Fronei document", doc_body, doc_type, fmt,
                                    template_id=template_id if isinstance(template_id, str) else None,
                                    template_path=str(template_path) if template_path else None,
                                    quality_mode=quality_mode,
                                    defer_render_qa=defer_render_qa,
                                    timeout_seconds=_document_pipeline_timeout_seconds(db),
                                )
                            except PipelineTimeout as exc:
                                document_preview = {"generation_failure": {"user_message": str(exc)}}
                            if failure_answer := _document_failure_answer(document_preview):
                                final_answer = failure_answer
                            if not defer_render_qa:
                                log_render_qa_failures(db, doc_type, doc_body, document_preview.get("render_qa"))
                    else:
                        for chunk in [final_answer[i:i+80] for i in range(0, len(final_answer), 80)]:
                            yield _sse("token", {"text": chunk})
                        result.estimated_cost_usd = (result.estimated_cost_usd or 0.0) + plan.planner_cost_usd

                    exec_log = ExecutionLog(
                        planner=PlannerLog(
                            model=plan.planner_model,
                            latency_ms=plan.planner_latency_ms,
                            cost_usd=plan.planner_cost_usd,
                            turn_type=plan.turn_type,
                            action="research_followup",
                            intent=plan.intent or req.message,
                            enriched_prompt=research_query,
                            needs_web_search=False,
                            search_query=None,
                            sub_queries=[],
                            context_summary=plan.context_summary or f"Follow-up on research run {existing_run_id}",
                        ),
                        web_context=WebContextLog(
                            enabled=False, provider="",
                            sources_count=len(followup.source_logs),
                            search_query=None,
                            status=f"Synthesised from research run {existing_run_id}: {len(followup.source_logs)} sources.",
                        ),
                        worker=WorkerLog(
                            model=result.model_used,
                            latency_ms=result.latency_ms,
                            prompt_tokens=result.prompt_tokens,
                            completion_tokens=result.completion_tokens,
                            cost_usd=result.estimated_cost_usd,
                            sub_queries_count=0, sub_query_logs=[],
                        ),
                        total_cost_usd=result.estimated_cost_usd or 0.0,
                        total_latency_ms=result.latency_ms + plan.planner_latency_ms,
                        stage_timings=[
                            StageTiming(stage="planner", latency_ms=plan.planner_latency_ms, meta={"model": plan.planner_model}),
                            _stage_timing("research_followup", followup_started, sources=len(followup.source_logs)),
                            StageTiming(stage="worker", latency_ms=result.latency_ms, meta={"model": result.model_used, "kind": "research_followup"}),
                        ],
                    )

                    plan.action = "research_followup"
                    rules_entry = _update_conversation_state(conv, plan, final_answer)

                    asst_msg = ConversationMessage(
                        conversation_id=conv.id, role="assistant",
                        content=final_answer, task_type=route.task_type,
                        complexity=route.complexity, model_used=result.model_used,
                        latency_ms=result.latency_ms,
                        prompt_tokens=result.prompt_tokens,
                        completion_tokens=result.completion_tokens,
                        estimated_cost_usd=result.estimated_cost_usd,
                        execution_log_json=exec_log.model_dump_json(),
                        research_run_id=existing_run_id,
                        document_preview_json=json.dumps(document_preview) if document_preview else None,
                    )
                    db.add(asst_msg)
                    _maybe_update_title(conv, plan.intent or research_query)
                    conv.message_count += 2
                    conv.updated_at = datetime.now(timezone.utc)
                    db.commit()
                    db.refresh(asst_msg)

                    if document_preview and defer_render_qa and not _document_failure_answer(document_preview):
                        _schedule_document_preview_polish(
                            message_id=asst_msg.id,
                            doc_type=doc_type,
                            title=title or "Fronei document",
                            doc_body=doc_body,
                            fmt=fmt,
                            template_id=template_id if isinstance(template_id, str) else None,
                            template_path=str(template_path) if template_path else None,
                            quality_mode=quality_mode,
                        )

                    memory_writer.schedule(conv.id, plan.turn_type, plan.intent or req.message, final_answer, rules_entry)
                    memory_extractor.schedule(user_id, conv.id, req.message, final_answer)

                    yield _sse("done", {
                        "message_id": asst_msg.id,
                        "answer": final_answer,
                        "model_used": result.model_used,
                        "latency_ms": result.latency_ms,
                        "estimated_cost_usd": result.estimated_cost_usd,
                        "prompt_tokens": result.prompt_tokens,
                        "completion_tokens": result.completion_tokens,
                        "execution_log": exec_log.model_dump(),
                        "route": route.model_dump(),
                        "was_refined": False,
                        "research_run_id": existing_run_id,
                        "document_preview": document_preview,
                        "research": {
                            "run_id": existing_run_id,
                            "mode": "followup",
                            "sources": followup.source_logs,
                            "questions": followup.questions,
                            "gaps": [], "contradictions": [],
                            "verifier_notes": None,
                            "confidence": followup.run.confidence,
                        },
                    })
                    return

                # ── Full research pipeline ────────────────────────────────
                conv_id = conv.id
                db.commit()
                progress_q: _queue_module.Queue = _queue_module.Queue()
                research_holder: list[ResearchPipelineResult | None] = [None]
                error_holder: list[BaseException | None] = [None]

                def _run_research_worker() -> None:
                    research_db = SessionLocal()
                    try:
                        def progress(stage: str, message: str, extra: dict) -> None:
                            progress_q.put({"stage": stage, "message": message, **extra})

                        rollout = graph_rollout_decision(settings, tool_name="deep_research")
                        _turn_graph_debug(
                            settings,
                            "research_rollout_decision",
                            conversation_id=conv.public_id,
                            user_id=user_id,
                            mode=rollout.mode,
                            allow_full_execution=rollout.allow_full_execution,
                            allow_canary_execution=rollout.allow_canary_execution,
                            record_shadow_trace=rollout.record_shadow_trace,
                            reason=rollout.reason,
                            research_mode=research_mode,
                        )
                        if rollout.allow_full_execution:
                            from app.services.agent_runtime.research_agent import ResearchAgent
                            from app.services.agent_runtime.registry import load_default_registry

                            _turn_graph_debug(
                                settings,
                                "research_authoritative_start",
                                conversation_id=conv.public_id,
                                user_id=user_id,
                                query=research_query[:500],
                                research_mode=research_mode,
                            )
                            progress("research_agent", "Running graph-native research agent…", {})
                            research_state = state_from_turn(
                                conversation=None,
                                turn=None,
                                user_message=research_query,
                                history=history,
                                user_memory=user_memory,
                                profile=profile,
                                user_role="admin" if is_admin else "user",
                            )
                            research_state.conversation_id = conv.public_id
                            research_state.user_id = user_id
                            research_state.tenant_id = user_id
                            research_state.quality_mode = "executive" if research_mode == "expert" else "standard"
                            decision = SimpleNamespace(plan=plan_to_dict(plan))
                            research_agent = ResearchAgent(load_default_registry())
                            agent_result = _run_with_timeout(
                                _run_graph_research_agent,
                                research_agent,
                                research_state,
                                decision,
                                progress,
                                timeout_seconds=_graph_research_timeout_seconds(research_db, research_mode),
                            )
                            _turn_graph_debug(
                                settings,
                                "research_authoritative_agent_result",
                                conversation_id=conv.public_id,
                                user_id=user_id,
                                model_used=agent_result.model_used,
                                latency_ms=agent_result.latency_ms,
                                source_count=len(agent_result.sources or []),
                                claim_count=len(research_state.research_claims or []),
                            )
                            compat_run, compat_sources, compat_claims = _persist_graph_research_compat_run(
                                research_db,
                                user_id=user_id,
                                conversation_id=conv_id,
                                query=research_query,
                                mode=research_mode,
                                answer=agent_result.answer,
                                sources=agent_result.sources,
                                claims=research_state.research_claims or [],
                            )
                            _turn_graph_debug(
                                settings,
                                "research_authoritative_compat_persisted",
                                conversation_id=conv.public_id,
                                user_id=user_id,
                                research_run_id=compat_run.id,
                                source_count=len(compat_sources),
                                claim_count=len(compat_claims),
                            )
                            research_holder[0] = ResearchPipelineResult(
                                run=compat_run,
                                result=LLMResult(
                                    answer=agent_result.answer,
                                    model_used=agent_result.model_used,
                                    latency_ms=agent_result.latency_ms,
                                    prompt_tokens=None,
                                    completion_tokens=None,
                                    estimated_cost_usd=agent_result.cost_usd,
                                ),
                                route=RouteDecision(
                                    task_type="research",
                                    complexity="high",
                                    profile=profile,
                                    primary_model=agent_result.model_used or "agent_runtime",
                                    fallbacks=[],
                                    reason="graph-native research agent",
                                ),
                                source_logs=compat_sources,
                                questions=research_state.research_queries or [],
                                gaps=[],
                                contradictions=[],
                                verifier_notes=None,
                                claim_logs=compat_claims,
                            )
                        elif getattr(settings, "turn_graph_enabled", False):
                            _turn_graph_debug(
                                settings,
                                "research_wrapper_path_start",
                                conversation_id=conv.public_id,
                                user_id=user_id,
                                research_mode=research_mode,
                            )
                            research_state = state_from_turn(
                                conversation=None,
                                turn=None,
                                user_message=research_query,
                                history=history,
                                user_memory=user_memory,
                                profile=profile,
                                user_role="admin" if is_admin else "user",
                            )
                            research_state.conversation_id = conv.public_id
                            research_state.user_id = user_id
                            research_state.tenant_id = user_id
                            research_output = execute_deep_research_tool(
                                research_state,
                                db=research_db,
                                tool_input=ResearchToolInput(
                                    user_id=user_id,
                                    conversation_id=conv_id,
                                    query=research_query,
                                    profile=profile,
                                    force_model=req.force_model,
                                    mode=research_mode,
                                ),
                                runner=run_research,
                                progress_sink=progress,
                            )
                            if research_output.status != "ok":
                                raise RuntimeError(research_output.error or "Research tool failed")
                            research_holder[0] = research_state.research_raw_result
                        else:
                            _turn_graph_debug(
                                settings,
                                "research_legacy_path_start",
                                conversation_id=conv.public_id,
                                user_id=user_id,
                                research_mode=research_mode,
                            )
                            research_holder[0] = run_research(
                                research_db,
                                user_id=user_id,
                                conversation_id=conv_id,
                                query=research_query,
                                profile=profile,
                                force_model=req.force_model,
                                mode=research_mode,
                                progress=progress,
                            )
                    except BaseException as exc:
                        _turn_graph_debug(
                            settings,
                            "research_worker_error",
                            conversation_id=conv.public_id,
                            user_id=user_id,
                            error=repr(exc),
                            error_type=type(exc).__name__,
                            research_mode=research_mode,
                        )
                        error_holder[0] = exc
                    finally:
                        research_db.close()
                        progress_q.put(None)

                research_started = time.perf_counter()
                t = _threading.Thread(target=_run_research_worker, daemon=True)
                t.start()
                heartbeat_count = 0
                research_timeout_seconds = _graph_research_timeout_seconds(db, research_mode)

                while True:
                    try:
                        update = progress_q.get(timeout=20)
                    except _queue_module.Empty:
                        elapsed_ms = int((time.perf_counter() - research_started) * 1000)
                        if not t.is_alive():
                            continue
                        if elapsed_ms / 1000 > research_timeout_seconds:
                            error_holder[0] = PipelineTimeout(
                                "Graph-native research exceeded its timeout. Please retry the research request."
                            )
                            _turn_graph_debug(
                                settings,
                                "research_stream_timeout",
                                conversation_id=conv.public_id,
                                user_id=user_id,
                                elapsed_ms=elapsed_ms,
                                research_mode=research_mode,
                            )
                            break
                        heartbeat_count += 1
                        _turn_graph_debug(
                            settings,
                            "research_stream_heartbeat",
                            conversation_id=conv.public_id,
                            user_id=user_id,
                            elapsed_ms=elapsed_ms,
                            heartbeat_count=heartbeat_count,
                            research_mode=research_mode,
                        )
                        yield _pipeline_log(
                            "research_agent",
                            "Still running graph-native research…",
                            elapsed_ms=elapsed_ms,
                            heartbeat_count=heartbeat_count,
                        )
                        continue
                    if update is None:
                        break
                    stage = update.pop("stage")
                    message = update.pop("message")
                    yield _pipeline_log(stage, message, **update)

                if error_holder[0]:
                    _turn_graph_debug(
                        settings,
                        "research_stream_error_propagating",
                        conversation_id=conv.public_id,
                        user_id=user_id,
                        error=repr(error_holder[0]),
                        error_type=type(error_holder[0]).__name__,
                        research_mode=research_mode,
                    )
                    raise error_holder[0]
                research = research_holder[0]
                if research is None:
                    _turn_graph_debug(
                        settings,
                        "research_stream_no_result",
                        conversation_id=conv.public_id,
                        user_id=user_id,
                        research_mode=research_mode,
                    )
                    yield _sse("error", {"message": "Research pipeline returned no result"})
                    return
                _turn_graph_debug(
                    settings,
                    "research_stream_result_ready",
                    conversation_id=conv.public_id,
                    user_id=user_id,
                    research_run_id=getattr(research.run, "id", None),
                    source_count=len(research.source_logs),
                    claim_count=len(research.claim_logs),
                    model_used=research.result.model_used,
                    research_mode=research_mode,
                )

                result = research.result
                final_answer = result.answer
                route = research.route

                # ── Document capability (post-research) ─────────────────────
                # If the confirmed plan also wants a document, feed the research
                # findings into the document generator instead of just streaming
                # the research answer as chat text.
                gate = plan_gate.evaluate(
                    plan,
                    explicit_document_request=bool(req.document_requested or req.confirmed_plan),
                )
                document_preview = None
                defer_render_qa = False
                quality_mode = "standard"
                title = plan.intent or "Fronei document"
                doc_body = ""
                doc_type = str((plan.document_brief or {}).get("doc_type") or "presentation")
                fmt = "markdown"
                template_id = None
                template_path = None
                if plan.wants_document_output and gate.capabilities["document"].enabled:
                    yield _pipeline_log("working", "Drafting your document from research findings…")
                    fmt = _document_output_format(plan, gate)
                    _coerce_presentation_brief_for_pptx(plan, fmt)

                    research_context_parts = [f"Research findings:\n{result.answer}"]
                    if research.source_logs:
                        src_lines = "\n".join(
                            f"- {s.get('title', '')}: {s.get('url', '')}" for s in research.source_logs[:15]
                        )
                        research_context_parts.append(f"Sources:\n{src_lines}")
                    research_context = "\n\n".join(research_context_parts)
                    base_doc_context = _build_doc_context(req.attached_documents)
                    doc_context = f"{base_doc_context}\n\n{research_context}".strip() if base_doc_context else research_context

                    planner_ctx = _build_worker_context(plan, running_summary)
                    empty_wc = WebContextResult(context=None, status="", provider="", sources_count=0, search_query=None)
                    artifact_context = _presentation_artifact_context(
                        ARTIFACT_PROMPTS.get(req.artifact_type or "", ""),
                        db, user_id, plan,
                    )
                    _remember_document_source_context(plan, doc_context, research.run.id)
                    finalization_event = _maybe_propose_document_finalization(db, user_id, conv, user_msg, req, plan, gate)
                    if finalization_event:
                        yield finalization_event
                        return
                    brand_profile, design_system_id, user_document_profile = _document_generation_profiles(db, user_id, plan)

                    try:
                        result, doc_body, chat_summary, doc_type = _run_with_timeout(
                            generate_document_output,
                            plan, route, history, empty_wc, planner_ctx,
                            doc_context, False, False,
                            artifact_context=artifact_context,
                            user_memory=user_memory,
                            db=db,
                            brand_profile=brand_profile,
                            user_document_profile=user_document_profile,
                            design_system=design_system_id,
                            timeout_seconds=_document_pipeline_timeout_seconds(db),
                        )
                    except PipelineTimeout as exc:
                        document_preview = {"generation_failure": {"user_message": str(exc)}}
                        final_answer = _document_failure_answer(document_preview) or str(exc)
                        for chunk in [final_answer[i:i + 80] for i in range(0, len(final_answer), 80)]:
                            yield _sse("token", {"text": chunk})
                        result.estimated_cost_usd = (
                            (result.estimated_cost_usd or 0.0)
                            + (research.result.estimated_cost_usd or 0.0)
                            + plan.planner_cost_usd
                        )
                    else:
                        log_doc_plan_usage(db, doc_type, doc_body)
                        yield _pipeline_log("working", "Document plan ready — preparing preview…", doc_type=doc_type, format=fmt)
                        for chunk in [chat_summary[i:i + 80] for i in range(0, len(chat_summary), 80)]:
                            yield _sse("token", {"text": chunk})
                        final_answer = chat_summary
                        result.estimated_cost_usd = (
                            (result.estimated_cost_usd or 0.0)
                            + (research.result.estimated_cost_usd or 0.0)
                            + plan.planner_cost_usd
                        )
                        title = (plan.document_brief or {}).get("title") or plan.intent
                        template_id = (plan.document_brief or {}).get("template_id")
                        template_path = resolve_template_path(db, user_id, template_id if isinstance(template_id, str) else None)
                        quality_mode = _document_quality_mode(plan)
                        defer_render_qa = _should_defer_artifact_qa(fmt, quality_mode)
                        try:
                            yield _pipeline_log("working", f"Rendering {fmt.upper()} artifact…", doc_type=doc_type, format=fmt)
                            document_preview = _run_with_timeout(
                                build_document_artifact,
                                title or "Fronei document", doc_body, doc_type, fmt,
                                template_id=template_id if isinstance(template_id, str) else None,
                                template_path=str(template_path) if template_path else None,
                                quality_mode=quality_mode,
                                defer_render_qa=defer_render_qa,
                                timeout_seconds=_document_pipeline_timeout_seconds(db),
                            )
                        except PipelineTimeout as exc:
                            document_preview = {"generation_failure": {"user_message": str(exc)}}
                        if failure_answer := _document_failure_answer(document_preview):
                            final_answer = failure_answer
                        if not defer_render_qa:
                            log_render_qa_failures(db, doc_type, doc_body, document_preview.get("render_qa"))
                elif should_refine(result.answer, output_mode, twin_profile):
                    yield _sse("refine_start", {})
                    refined_text = ""
                    try:
                        for item in stream_refinement(result.answer, twin_profile, output_mode):
                            if isinstance(item, str):
                                yield _sse("refine_token", {"text": item})
                                refined_text += item
                        if refined_text:
                            final_answer = refined_text
                    except Exception:
                        pass
                else:
                    for chunk in [final_answer[i:i + 80] for i in range(0, len(final_answer), 80)]:
                        yield _sse("token", {"text": chunk})

                if document_preview is None:
                    result.estimated_cost_usd = (result.estimated_cost_usd or 0.0) + plan.planner_cost_usd

                exec_log = ExecutionLog(
                    planner=PlannerLog(
                        model=plan.planner_model,
                        latency_ms=plan.planner_latency_ms,
                        cost_usd=plan.planner_cost_usd,
                        turn_type="new_task",
                        action=f"research_{research_mode}",
                        intent=plan.intent or req.message,
                        enriched_prompt=research_query,
                        needs_web_search=True,
                        search_query=research_query,
                        sub_queries=[],
                        context_summary=plan.context_summary,
                    ),
                    web_context=WebContextLog(
                        enabled=True,
                        provider="multi",
                        sources_count=len(research.source_logs),
                        search_query=research_query,
                        status=f"Research run {research.run.id}: {len(research.source_logs)} sources, confidence {research.run.confidence or 'unknown'}.",
                    ),
                    worker=WorkerLog(
                        model=result.model_used,
                        latency_ms=result.latency_ms,
                        prompt_tokens=result.prompt_tokens,
                        completion_tokens=result.completion_tokens,
                        cost_usd=result.estimated_cost_usd,
                        sub_queries_count=len(research.questions),
                        sub_query_logs=[],
                    ),
                    total_cost_usd=result.estimated_cost_usd or 0.0,
                    total_latency_ms=result.latency_ms + plan.planner_latency_ms,
                    stage_timings=[
                        StageTiming(stage="planner", latency_ms=plan.planner_latency_ms, meta={"model": plan.planner_model}),
                        _stage_timing("research_pipeline", research_started, mode=research_mode, sources=len(research.source_logs)),
                        StageTiming(stage="worker", latency_ms=result.latency_ms, meta={"model": result.model_used, "kind": "research"}),
                    ],
                )

                asst_msg = ConversationMessage(
                    conversation_id=conv.id, role="assistant",
                    content=final_answer, task_type=route.task_type, complexity=route.complexity,
                    model_used=result.model_used, latency_ms=result.latency_ms,
                    prompt_tokens=result.prompt_tokens, completion_tokens=result.completion_tokens,
                    estimated_cost_usd=result.estimated_cost_usd,
                    execution_log_json=exec_log.model_dump_json(),
                    research_run_id=research.run.id,
                    document_preview_json=json.dumps(document_preview) if document_preview else None,
                )
                plan.action = f"research_{research_mode}"
                rules_entry = _update_conversation_state(conv, plan, final_answer)
                _maybe_update_title(conv, plan.intent or research_query)
                db.add(asst_msg)
                conv.message_count += 2
                conv.updated_at = datetime.now(timezone.utc)
                db.commit()
                db.refresh(asst_msg)

                if document_preview and defer_render_qa and not _document_failure_answer(document_preview):
                    _schedule_document_preview_polish(
                        message_id=asst_msg.id,
                        doc_type=doc_type,
                        title=title or "Fronei document",
                        doc_body=doc_body,
                        fmt=fmt,
                        template_id=template_id if isinstance(template_id, str) else None,
                        template_path=str(template_path) if template_path else None,
                        quality_mode=quality_mode,
                    )

                memory_writer.schedule(conv.id, plan.turn_type, plan.intent or req.message, final_answer, rules_entry)
                memory_extractor.schedule(user_id, conv.id, req.message, final_answer)

                yield _sse("done", {
                    "message_id": asst_msg.id,
                    "answer": final_answer,
                    "model_used": result.model_used,
                    "latency_ms": result.latency_ms,
                    "estimated_cost_usd": result.estimated_cost_usd,
                    "prompt_tokens": result.prompt_tokens,
                    "completion_tokens": result.completion_tokens,
                    "execution_log": exec_log.model_dump(),
                    "route": route.model_dump(),
                    "was_refined": document_preview is None and final_answer != research.result.answer,
                    "research_run_id": research.run.id,
                    "document_preview": document_preview,
                    "research": {
                        "run_id": research.run.id,
                        "mode": research_mode,
                        "sources": research.source_logs,
                        "claims": research.claim_logs,
                        "questions": research.questions,
                        "gaps": research.gaps,
                        "contradictions": research.contradictions,
                        "verifier_notes": research.verifier_notes,
                        "confidence": research.run.confidence,
                    },
                })
                return

            confirmed = req.confirmed_plan.model_dump(exclude_none=True) if req.confirmed_plan else None
            setup         = build_pipeline_setup(
                req, conv, history, settings, user_memory=user_memory,
                plan=preloaded_plan, confirmed_plan=confirmed,
            )
            plan          = setup.plan
            route         = setup.route
            wc            = setup.wc
            enable_native = setup.enable_native
            planner_ctx   = setup.planner_ctx
            yield _planner_selected_log(plan)

            # ── Unified plan gate ───────────────────────────────────────────
            # Decide whether this turn can execute immediately ("auto") or needs
            # one bundled confirmation popup before execution starts. If the user
            # has already confirmed (confirmed is not None — either inline or via
            # execute-plan), we skip straight to execution regardless of gate mode.
            gate = plan_gate.evaluate(
                plan,
                explicit_document_request=bool(req.document_requested or confirmed),
            )
            if _should_propose_plan_before_execution(gate) and confirmed is None:
                user_msg.plan_json = json.dumps(plan_to_dict(plan))
                conv.updated_at = datetime.now(timezone.utc)
                db.commit()
                yield _sse("plan_proposed", {
                    "conversation_id": conv.public_id,
                    "message_id": user_msg.id,
                    "plan_confidence": gate.plan_confidence,
                    "open_questions": gate.open_questions,
                    "capabilities": {k: v.to_dict() for k, v in gate.capabilities.items()},
                    "intent": plan.intent,
                })
                return

            # ── Document capability ──────────────────────────────────────────
            # If the plan decided this turn should produce a document, that's
            # just the outcome of this turn — not a separate flow. Generate the
            # document body + a short chat-facing bullet outline in one call.
            if plan.wants_document_output and gate.capabilities["document"].enabled:
                finalization_event = _maybe_propose_document_finalization(db, user_id, conv, user_msg, req, plan, gate)
                if finalization_event:
                    yield finalization_event
                    return

                yield _pipeline_log("working", "Drafting your document…")
                fmt = _document_output_format(plan, gate)
                _coerce_presentation_brief_for_pptx(plan, fmt)
                brand_profile, design_system_id, user_document_profile = _document_generation_profiles(db, user_id, plan)
                stage_timings = list(setup.stage_timings or [])
                doc_graph_state = None
                graph_document_preview = None
                try:
                    started = time.perf_counter()
                    rollout = graph_rollout_decision(settings, tool_name="generate_document")
                    _turn_graph_debug(
                        settings,
                        "document_rollout_decision",
                        conversation_id=conv.public_id,
                        user_id=user_id,
                        mode=rollout.mode,
                        allow_full_execution=rollout.allow_full_execution,
                        allow_canary_execution=rollout.allow_canary_execution,
                        record_shadow_trace=rollout.record_shadow_trace,
                        reason=rollout.reason,
                        requested_format=fmt,
                        doc_type=str((plan.document_brief or {}).get("doc_type") or "document"),
                    )
                    if rollout.allow_full_execution:
                        from app.services.agent_runtime.document_agent import DocumentAgent
                        from app.services.agent_runtime.registry import load_default_registry

                        _turn_graph_debug(
                            settings,
                            "document_authoritative_start",
                            conversation_id=conv.public_id,
                            user_id=user_id,
                            quality_mode=_document_quality_mode(plan),
                            requested_format=fmt,
                        )
                        doc_graph_state = state_from_turn(
                            conversation=conv,
                            turn=None,
                            user_message=plan.enriched_prompt or req.message,
                            history=history,
                            user_memory=user_memory,
                            profile=profile,
                            user_role="admin" if is_admin else "user",
                        )
                        doc_graph_state.quality_mode = _document_quality_mode(plan)
                        doc_graph_state.plan = plan_to_dict(plan)
                        doc_result = DocumentAgent(load_default_registry()).run(
                            doc_graph_state,
                            SimpleNamespace(plan=doc_graph_state.plan),
                            db=db,
                        )
                        _turn_graph_debug(
                            settings,
                            "document_authoritative_result",
                            conversation_id=conv.public_id,
                            user_id=user_id,
                            doc_type=doc_result.doc_type,
                            filename=doc_result.filename,
                            has_docx=bool(doc_result.docx_base64),
                            has_pptx=bool(doc_result.pptx_base64),
                            model_used=doc_result.model_used,
                            latency_ms=doc_result.latency_ms,
                        )
                        result = LLMResult(
                            answer=doc_result.markdown,
                            model_used=doc_result.model_used,
                            latency_ms=doc_result.latency_ms,
                            prompt_tokens=None,
                            completion_tokens=None,
                            estimated_cost_usd=doc_result.cost_usd,
                        )
                        doc_body = doc_result.markdown
                        chat_summary = doc_result.markdown
                        doc_type = doc_result.doc_type
                        preview_format = "pptx" if doc_result.pptx_base64 else ("docx" if doc_result.docx_base64 else "markdown")
                        graph_document_preview = {
                            "title": doc_result.title,
                            "doc_type": doc_result.doc_type,
                            "format": preview_format,
                            "requested_format": fmt,
                            "quality_mode": doc_graph_state.quality_mode,
                            "markdown": doc_result.markdown,
                            "filename": doc_result.filename,
                        }
                        if doc_result.docx_base64:
                            graph_document_preview["docx_base64"] = doc_result.docx_base64
                        if doc_result.pptx_base64:
                            graph_document_preview["pptx_base64"] = doc_result.pptx_base64
                    elif getattr(settings, "turn_graph_enabled", False):
                        _turn_graph_debug(
                            settings,
                            "document_wrapper_path_start",
                            conversation_id=conv.public_id,
                            user_id=user_id,
                            requested_format=fmt,
                        )
                        doc_graph_state = state_from_turn(
                            conversation=conv,
                            turn=None,
                            user_message=plan.enriched_prompt or req.message,
                            history=history,
                            user_memory=user_memory,
                            profile=profile,
                            user_role="admin" if is_admin else "user",
                        )

                        def _generate_document_via_current_pipeline(**_kwargs):
                            return _run_with_timeout(
                                generate_document_output,
                                plan, route, history, wc, planner_ctx,
                                _document_context_for_generation(setup.doc_context, plan), req.deep_research, enable_native,
                                artifact_context=_presentation_artifact_context(setup.artifact_context or "", db, user_id, plan),
                                user_memory=user_memory,
                                db=db,
                                brand_profile=brand_profile,
                                user_document_profile=user_document_profile,
                                design_system=design_system_id,
                                timeout_seconds=_document_pipeline_timeout_seconds(db),
                            )

                        doc_output = execute_generate_document_tool(
                            doc_graph_state,
                            tool_input=DocumentGenerationToolInput(
                                title=(plan.document_brief or {}).get("title") or plan.intent or "Fronei document",
                                doc_type=str((plan.document_brief or {}).get("doc_type") or "document"),
                                format=fmt,
                                quality_mode=_document_quality_mode(plan),
                                template_id=(plan.document_brief or {}).get("template_id"),
                            ),
                            generator=_generate_document_via_current_pipeline,
                        )
                        if doc_output.status != "ok":
                            raise RuntimeError(doc_output.error or "Document generation failed")
                        result, doc_body, chat_summary, doc_type = doc_graph_state.document_raw_result
                    else:
                        _turn_graph_debug(
                            settings,
                            "document_legacy_path_start",
                            conversation_id=conv.public_id,
                            user_id=user_id,
                            requested_format=fmt,
                        )
                        result, doc_body, chat_summary, doc_type = _run_with_timeout(
                            generate_document_output,
                            plan, route, history, wc, planner_ctx,
                            _document_context_for_generation(setup.doc_context, plan), req.deep_research, enable_native,
                            artifact_context=_presentation_artifact_context(setup.artifact_context or "", db, user_id, plan),
                            user_memory=user_memory,
                            db=db,
                            brand_profile=brand_profile,
                            user_document_profile=user_document_profile,
                            design_system=design_system_id,
                            timeout_seconds=_document_pipeline_timeout_seconds(db),
                        )
                    stage_timings.append(_stage_timing("document_generation", started, doc_type=doc_type, format=fmt))
                except PipelineTimeout as exc:
                    # Nothing has been committed yet for this turn, so a plain
                    # error event is safe here.
                    yield _sse("error", {"message": str(exc)})
                    return
                log_doc_plan_usage(db, doc_type, doc_body)
                yield _pipeline_log("working", "Document plan ready — preparing preview…", doc_type=doc_type, format=fmt)
                for chunk in [chat_summary[i:i + 80] for i in range(0, len(chat_summary), 80)]:
                    yield _sse("token", {"text": chunk})

                final_answer = chat_summary
                sq_logs: list[SubQueryLog] = []
                stage_timings.append(StageTiming(stage="worker", latency_ms=result.latency_ms, meta={"model": result.model_used, "kind": "document"}))
                exec_log = build_exec_log(
                    plan, wc, result, sq_logs, enable_native, req.deep_research,
                    stage_timings=stage_timings,
                )
                result.estimated_cost_usd = (result.estimated_cost_usd or 0.0) + plan.planner_cost_usd

                asst_msg = ConversationMessage(
                    conversation_id=conv.id, role="assistant",
                    content=final_answer, task_type=route.task_type, complexity=route.complexity,
                    model_used=result.model_used, latency_ms=result.latency_ms,
                    prompt_tokens=result.prompt_tokens, completion_tokens=result.completion_tokens,
                    estimated_cost_usd=result.estimated_cost_usd,
                    execution_log_json=exec_log.model_dump_json(),
                )
                rules_entry = _update_conversation_state(conv, plan, final_answer)
                _maybe_update_title(conv, plan.intent or plan.enriched_prompt or req.message)
                db.add(asst_msg)
                conv.message_count += 2
                conv.updated_at = datetime.now(timezone.utc)
                db.commit()
                db.refresh(asst_msg)

                memory_writer.schedule(conv.id, plan.turn_type, plan.intent, final_answer, rules_entry)
                memory_extractor.schedule(user_id, conv.id, req.message, final_answer)

                title = (plan.document_brief or {}).get("title") or plan.intent
                template_id = (plan.document_brief or {}).get("template_id")
                template_path = resolve_template_path(db, user_id, template_id if isinstance(template_id, str) else None)
                quality_mode = _document_quality_mode(plan)
                defer_render_qa = _should_defer_artifact_qa(fmt, quality_mode)
                # #170: asst_msg (with the chat-facing summary) is already
                # committed at this point. If the artifact build fails or
                # times out, fall back to a generation_failure preview rather
                # than letting the exception bubble up as a bare "error"
                # event that would strand the saved message with no preview
                # and a final_answer the client never receives.
                try:
                    yield _pipeline_log("working", f"Rendering {fmt.upper()} artifact…", doc_type=doc_type, format=fmt)
                    started = time.perf_counter()
                    rollout = graph_rollout_decision(settings, tool_name="render_artifact")
                    _turn_graph_debug(
                        settings,
                        "render_rollout_decision",
                        conversation_id=conv.public_id,
                        user_id=user_id,
                        mode=rollout.mode,
                        allow_full_execution=rollout.allow_full_execution,
                        reason=rollout.reason,
                        requested_format=fmt,
                        graph_preview_available=graph_document_preview is not None,
                    )
                    if rollout.allow_full_execution and graph_document_preview is not None:
                        _turn_graph_debug(
                            settings,
                            "render_authoritative_preview_reused",
                            conversation_id=conv.public_id,
                            user_id=user_id,
                            requested_format=fmt,
                        )
                        document_preview = graph_document_preview
                    elif getattr(settings, "turn_graph_enabled", False) and doc_graph_state is not None:
                        _turn_graph_debug(
                            settings,
                            "render_wrapper_path_start",
                            conversation_id=conv.public_id,
                            user_id=user_id,
                            requested_format=fmt,
                        )
                        render_output = execute_render_artifact_tool(
                            doc_graph_state,
                            tool_input=ArtifactRenderToolInput(
                                title=title or "Fronei document",
                                body=doc_body,
                                doc_type=doc_type,
                                format=fmt,
                                template_id=template_id if isinstance(template_id, str) else None,
                                template_path=str(template_path) if template_path else None,
                                quality_mode=quality_mode,
                                defer_render_qa=defer_render_qa,
                            ),
                            renderer=lambda *args, **kwargs: _run_with_timeout(
                                build_document_artifact,
                                *args,
                                **kwargs,
                                timeout_seconds=_document_pipeline_timeout_seconds(db),
                            ),
                        )
                        if render_output.status != "ok":
                            raise RuntimeError(render_output.error or "Artifact rendering failed")
                        document_preview = doc_graph_state.artifact_result
                    else:
                        _turn_graph_debug(
                            settings,
                            "render_legacy_path_start",
                            conversation_id=conv.public_id,
                            user_id=user_id,
                            requested_format=fmt,
                        )
                        document_preview = _run_with_timeout(
                            build_document_artifact,
                            title or "Fronei document", doc_body, doc_type, fmt,
                            template_id=template_id if isinstance(template_id, str) else None,
                            template_path=str(template_path) if template_path else None,
                            quality_mode=quality_mode,
                            defer_render_qa=defer_render_qa,
                            timeout_seconds=_document_pipeline_timeout_seconds(db),
                        )
                    stage_timings.append(_stage_timing(
                        "artifact_build",
                        started,
                        doc_type=doc_type,
                        format=document_preview.get("format") if isinstance(document_preview, dict) else fmt,
                    ))
                except PipelineTimeout as exc:
                    document_preview = {"generation_failure": {"user_message": str(exc)}}
                    stage_timings.append(_stage_timing("artifact_build_timeout", started, doc_type=doc_type, format=fmt))
                if failure_answer := _document_failure_answer(document_preview):
                    final_answer = failure_answer
                    asst_msg.content = final_answer
                if not defer_render_qa:
                    log_render_qa_failures(db, doc_type, doc_body, document_preview.get("render_qa"))

                exec_log.stage_timings = stage_timings
                asst_msg.execution_log_json = exec_log.model_dump_json()
                asst_msg.document_preview_json = json.dumps(document_preview) if document_preview else None
                db.commit()

                if document_preview and defer_render_qa and not _document_failure_answer(document_preview):
                    _schedule_document_preview_polish(
                        message_id=asst_msg.id,
                        doc_type=doc_type,
                        title=title or "Fronei document",
                        doc_body=doc_body,
                        fmt=fmt,
                        template_id=template_id if isinstance(template_id, str) else None,
                        template_path=str(template_path) if template_path else None,
                        quality_mode=quality_mode,
                    )

                yield _sse("done", {
                    "message_id": asst_msg.id,
                    "answer": final_answer,
                    "model_used": result.model_used,
                    "latency_ms": result.latency_ms,
                    "estimated_cost_usd": result.estimated_cost_usd,
                    "prompt_tokens": result.prompt_tokens,
                    "completion_tokens": result.completion_tokens,
                    "execution_log": exec_log.model_dump(),
                    "route": route.model_dump(),
                    "was_refined": False,
                    "document_preview": document_preview,
                })
                return

            sq_previews = [
                {"query": sq.query[:120], "task_type": sq.task_type, "model_hint": sq.preferred_model}
                for sq in plan.sub_queries
            ] if plan.action == "decompose" else []

            if plan.action == "answer_directly":
                routing_msg = "Answering directly from context"
            elif plan.action == "decompose":
                routing_msg = f"Decomposing into {len(plan.sub_queries)} parallel sub-queries"
            else:
                routing_msg = f"Routing to {route.task_type} specialist"

            yield _pipeline_log(
                "routing",
                routing_msg,
                route=route.model_dump(),
                intent=plan.intent,
                turn_type=plan.turn_type,
                sub_queries=sq_previews,
            )

            # ── Stream tokens ─────────────────────────────────────────────────
            result: LLMResult | None = None
            sq_logs: list[SubQueryLog] = []

            if plan.action == "answer_directly":
                for item in stream_llm(plan.enriched_prompt, route, history=history,
                                       deep_research=False, web_context=None,
                                       enable_native_search=False, planner_context=planner_ctx,
                                       doc_context=setup.doc_context or None,
                                       artifact_context=setup.artifact_context or None):
                    if isinstance(item, str):
                        yield _sse("token", {"text": item})
                    elif isinstance(item, LLMResult):
                        result = item

            elif len(plan.sub_queries) > 1:
                n_queries = len(plan.sub_queries)
                yield _pipeline_log(
                    "working",
                    f"Running {n_queries} sub-queries in parallel…",
                    queries=[sq.query[:80] for sq in plan.sub_queries],
                )

                progress_q: _queue_module.Queue = _queue_module.Queue()
                exe_holder: list[SubQueryExecution | None] = [None]
                error_holder: list[BaseException | None] = [None]

                def _run_parallel() -> None:
                    def on_complete(idx, model, task_type, latency_ms, cost):
                        progress_q.put({
                            "idx": idx,
                            "model": model,
                            "task_type": task_type,
                            "latency_ms": latency_ms,
                            "cost_usd": cost,
                        })

                    try:
                        exe_holder[0] = _run_sub_queries(
                            plan, history, wc.context, enable_native, req.deep_research,
                            planner_ctx, setup.profile, on_complete=on_complete,
                            doc_context=setup.doc_context,
                        )
                    except BaseException as exc:
                        error_holder[0] = exc
                    finally:
                        progress_q.put(None)

                t = _threading.Thread(target=_run_parallel, daemon=True)
                t.start()

                completed = 0
                while True:
                    update = progress_q.get()
                    if update is None:
                        break
                    completed += 1
                    yield _pipeline_log(
                        "sub_complete",
                        f"Sub-query {completed}/{n_queries} complete",
                        **update,
                    )

                if error_holder[0]:
                    raise error_holder[0]
                exe = exe_holder[0]
                if exe is None:
                    yield _sse("error", {"message": "Sub-query execution returned no result"})
                    return

                sq_logs = exe.sq_logs
                yield _pipeline_log("synthesising", f"Synthesising {n_queries} responses…")

                for item in stream_synthesis(plan.intent, exe.sub_results, exe.synthesis_route):
                    if isinstance(item, str):
                        yield _sse("token", {"text": item})
                    elif isinstance(item, LLMResult):
                        result = LLMResult(
                            answer=item.answer, model_used=item.model_used,
                            latency_ms=exe.total_latency_ms + item.latency_ms,
                            prompt_tokens=exe.total_prompt_tokens + (item.prompt_tokens or 0),
                            completion_tokens=exe.total_completion_tokens + (item.completion_tokens or 0),
                            estimated_cost_usd=exe.total_cost_usd + (item.estimated_cost_usd or 0.0),
                        )

            else:
                yield _pipeline_log("working", "Working on your request…")
                for item in stream_llm(plan.enriched_prompt, route, history=history,
                                       deep_research=req.deep_research, web_context=wc.context,
                                       enable_native_search=enable_native, planner_context=planner_ctx,
                                       doc_context=setup.doc_context or None,
                                       artifact_context=setup.artifact_context or None):
                    if isinstance(item, str):
                        yield _sse("token", {"text": item})
                    elif isinstance(item, LLMResult):
                        result = item

            if result is None:
                yield _sse("error", {"message": "LLM returned no result"})
                return

            # ── Refinement pass (if warranted) ───────────────────────────────
            final_answer = result.answer
            if should_refine(result.answer, output_mode, twin_profile):
                yield _sse("refine_start", {})
                refined_text = ""
                _refined_result: LLMResult | None = None
                try:
                    for item in stream_refinement(result.answer, twin_profile, output_mode):
                        if isinstance(item, str):
                            yield _sse("refine_token", {"text": item})
                            refined_text += item
                        elif isinstance(item, LLMResult):
                            _refined_result = item
                    if refined_text:
                        final_answer = refined_text
                except Exception:
                    pass

            # ── Build exec log + persist ──────────────────────────────────────
            stage_timings = list(setup.stage_timings or [])
            stage_timings.append(StageTiming(stage="worker", latency_ms=result.latency_ms, meta={"model": result.model_used}))
            exec_log = build_exec_log(
                plan, wc, result, sq_logs, setup.enable_native, req.deep_research,
                stage_timings=stage_timings,
            )
            result.estimated_cost_usd = (result.estimated_cost_usd or 0.0) + plan.planner_cost_usd

            asst_msg = ConversationMessage(
                conversation_id=conv.id, role="assistant",
                content=final_answer, task_type=route.task_type, complexity=route.complexity,
                model_used=result.model_used, latency_ms=result.latency_ms,
                prompt_tokens=result.prompt_tokens, completion_tokens=result.completion_tokens,
                estimated_cost_usd=result.estimated_cost_usd,
                execution_log_json=exec_log.model_dump_json(),
            )
            rules_entry = _update_conversation_state(conv, plan, final_answer)
            _maybe_update_title(conv, plan.intent or plan.enriched_prompt or req.message)
            db.add(asst_msg)
            conv.message_count += 2
            conv.updated_at = datetime.now(timezone.utc)
            db.commit()
            db.refresh(asst_msg)

            memory_writer.schedule(conv.id, plan.turn_type, plan.intent, final_answer, rules_entry)
            memory_extractor.schedule(user_id, conv.id, req.message, final_answer)

            yield _sse("done", {
                "message_id": asst_msg.id,
                "answer": final_answer,
                "model_used": result.model_used,
                "latency_ms": result.latency_ms,
                "estimated_cost_usd": result.estimated_cost_usd,
                "prompt_tokens": result.prompt_tokens,
                "completion_tokens": result.completion_tokens,
                "execution_log": exec_log.model_dump(),
                "route": route.model_dump(),
                "was_refined": final_answer != result.answer,
                "document_preview": None,
            })


# ── Execute-plan (confirmation popup follow-up) ────────────────────────────────

@router.post(
    "/{conv_id}/messages/{message_id}/execute-plan",
    dependencies=[rate_limiter("chat", "rate_limit_chat_per_minute", 60)],
)
def execute_plan(
    conv_id: str,
    message_id: int,
    body: ExecutePlanRequest,
    user_id: str = CurrentUser,
    is_admin: bool = CurrentUserIsAdmin,
) -> StreamingResponse:
    """
    Resume a turn whose plan was surfaced via `plan_proposed`. The original
    user message (and its persisted plan_json) is referenced by message_id —
    the message text is not re-sent. Applies the user's confirmed overrides
    and executes the plan to completion (fully autopilot from here).
    """
    settings = get_settings()
    turn_public_id: dict[str, str | None] = {"id": None}

    def run_turn_worker():
        db = SessionLocal()
        turn: ConversationTurn | None = None
        try:
            conv = _get_conversation(db, conv_id, user_id)
            if is_user_suspended(db, user_id):
                raise HTTPException(status_code=403, detail="This account is suspended.")
            if is_user_pending(db, user_id):
                raise HTTPException(status_code=403, detail="Your account is pending admin approval.")
            enforce_global_monthly_budget(db, is_admin)
            if not is_admin:
                monthly_spend = get_monthly_spend(db, user_id)
                monthly_budget = get_effective_monthly_budget(db, user_id)
                if monthly_spend >= monthly_budget:
                    raise HTTPException(
                        status_code=429,
                        detail=f"Monthly budget of ${monthly_budget:.2f} reached "
                               f"(spent ${monthly_spend:.4f} this month). Ask an admin to adjust the limit."
                    )

            user_msg = db.get(ConversationMessage, message_id)
            if not user_msg or user_msg.conversation_id != conv.id or user_msg.role != "user":
                raise HTTPException(status_code=404, detail="Message not found")
            if not user_msg.plan_json:
                raise HTTPException(status_code=400, detail="No plan is pending confirmation for this message")

            if body.client_request_id:
                existing_turn = (
                    db.query(ConversationTurn)
                    .filter(
                        ConversationTurn.user_id == user_id,
                        ConversationTurn.client_request_id == body.client_request_id,
                    )
                    .first()
                )
                if existing_turn:
                    turn_public_id["id"] = existing_turn.public_id
                    _append_turn_lifecycle(existing_turn, "idempotent_replay", {"status": existing_turn.status})
                    existing_turn.updated_at = datetime.now(timezone.utc)
                    db.commit()
                    yield _sse("start", {"conversation_id": conv.public_id, "turn_id": existing_turn.public_id})
                    if existing_turn.status == "completed":
                        done_event = _turn_completed_done_event(db, existing_turn)
                        if done_event:
                            yield done_event
                            return
                    yield _pipeline_log(
                        "working",
                        f"Turn is already {existing_turn.status}; reconnecting to the conversation when it finishes.",
                        turn_id=existing_turn.public_id,
                    )
                    return

            try:
                plan_data = json.loads(user_msg.plan_json)
            except (json.JSONDecodeError, ValueError):
                raise HTTPException(status_code=500, detail="Stored plan is corrupted")
            plan = plan_from_dict(plan_data, user_msg.content)

            if plan.recommend_deep_research and body.confirmed_plan.deep_research:
                check_rate_limit(f"research:{user_id}", settings.rate_limit_research_per_hour, 3600) if not is_admin else None

            history = _build_history(conv, db, before_id=user_msg.id)
            user_memory = build_context(db, user_id)
            profile = conv.profile

            message_text = user_msg.content
            clarifications = (body.confirmed_plan.clarifications or "").strip()
            if clarifications:
                suffix = (
                    f"\n\nAdditional context from user (answers to clarifying questions):\n{clarifications}"
                )
                # ConvChatRequest.message is capped at 32000 chars. Truncate the
                # original message (not the clarifications, which are the user's
                # most recent and most relevant input) if the combination would
                # exceed that limit.
                max_base_len = 32000 - len(suffix)
                base = user_msg.content if max_base_len <= 0 else user_msg.content[:max_base_len]
                message_text = f"{base}{suffix}"[:32000]
                plan.enriched_prompt = (
                    f"{plan.enriched_prompt}\n\nUser clarifications:\n{clarifications}"
                    if plan.enriched_prompt else message_text
                )

            confirmed_deep_research = (
                bool(body.confirmed_plan.deep_research)
                if body.confirmed_plan.deep_research is not None
                else plan.recommend_deep_research
            )
            confirmed_research_mode = _confirmed_research_mode(
                body.confirmed_plan,
                plan.recommend_deep_research,
            )
            _turn_graph_debug(
                settings,
                "execute_plan_confirmed",
                conversation_id=conv.public_id,
                user_id=user_id,
                message_id=message_id,
                confirmed_deep_research=body.confirmed_plan.deep_research,
                confirmed_research_mode=body.confirmed_plan.research_mode,
                resolved_deep_research=confirmed_deep_research,
                resolved_research_mode=confirmed_research_mode,
                recommended_deep_research=plan.recommend_deep_research,
                document=body.confirmed_plan.document,
                web_search=body.confirmed_plan.web_search,
            )

            req = ConvChatRequest(
                message=message_text,
                conversation_id=conv_id,
                profile=profile,
                deep_research=confirmed_deep_research,
                research_mode=confirmed_research_mode,
                confirmed_plan=body.confirmed_plan,
            )
            turn = ConversationTurn(
                user_id=user_id,
                conversation_id=conv.id,
                user_message_id=user_msg.id,
                client_request_id=body.client_request_id,
                status="running",
                turn_kind=_turn_kind_for_request(req),
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
            db.add(turn)
            db.flush()
            turn_public_id["id"] = turn.public_id
            _append_turn_lifecycle(turn, "created", {"kind": turn.turn_kind, "source": "execute_plan"})
            _append_turn_graph_shadow(
                settings=settings,
                conv=conv,
                turn=turn,
                req=req,
                history=history,
                user_memory=user_memory,
                profile=profile,
                is_admin=is_admin,
            )
            db.commit()

            if _should_detach_progressive_job(req):
                message = (
                    "Fronei is researching in the background…"
                    if turn.turn_kind == "research"
                    else "Fronei is generating your document in the background…"
                )
                payload = _sse_with_extra(
                    _pipeline_log("working", message, turn_kind=turn.turn_kind),
                    {"turn_id": turn.public_id},
                )
                _record_turn_event(db, turn, payload)
                yield payload
                payload = _sse("job_started", _job_started_payload(conv, turn, message))
                _record_turn_event(db, turn, payload)
                yield payload

            for payload in _stream_turn(
                    db, conv, req, user_id, is_admin, settings, history, user_memory, profile, user_msg,
                    preloaded_plan=plan):
                parsed_event = _parse_sse_event(payload)
                is_token = bool(parsed_event) and parsed_event[0] == "token"
                if _is_turn_cancelled(db, turn, allow_db_check=not is_token):
                    payload = _sse("error", {"message": "Turn cancelled."})
                    _record_turn_event(db, turn, payload)
                    yield payload
                    return
                payload = _sse_with_extra(payload, {"turn_id": turn.public_id})
                _record_turn_event(db, turn, payload)
                yield payload

        except HTTPException as exc:
            payload = _sse("error", {"message": exc.detail})
            _record_turn_event(db, turn, payload)
            yield payload
        except Exception as exc:
            payload = _sse("error", {"message": _friendly_error(exc)})
            _record_turn_event(db, turn, payload)
            yield payload
        finally:
            db.close()

    return _run_durable_stream(
        run_turn_worker,
        on_client_disconnect=lambda: _record_turn_lifecycle_by_public_id(turn_public_id["id"], "client_disconnected"),
        detach_on_event=_detach_after_job_started,
    )
