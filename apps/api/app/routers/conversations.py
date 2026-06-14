"""Multi-turn conversation router."""
import json
import logging
import queue as _queue_module
import re
import threading as _threading
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import func
from fastapi.responses import StreamingResponse

from app.auth import CurrentUser, CurrentUserIsAdmin
from app.config import get_settings
from app.db.models import (
    Conversation, ConversationMessage, ConversationTurn, RequestLog, SessionLocal,
    get_effective_monthly_budget, get_monthly_spend, get_turn_runtime_config,
    get_twin_profile, is_user_pending, is_user_suspended,
)
from app.schemas import (
    ConvChatRequest, ConvChatResponse, ExecutePlanRequest,
    ConversationDetail, ConversationSummary, ConversationTurnOut, ConversationUpdate, MessageOut,
    ExecutionLog, OutputMode, PlannerLog, RouteDecision, SubQueryLog, WebContextLog, WorkerLog,
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
from app.services import plan_gate
from app.services.planner import apply_confirmed_plan, passthrough, plan_from_dict, plan_to_dict, run_planner
from app.services.prompts import ARTIFACT_PROMPTS
from app.services.web_context import WebContextResult
from app.services.rate_limit import check_rate_limit, rate_limiter

router = APIRouter(prefix="/conversations", tags=["conversations"])

logger = logging.getLogger(__name__)


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
    return MessageOut(
        id=m.id, role=m.role, content=m.content,
        task_type=m.task_type, complexity=m.complexity, model_used=m.model_used,
        latency_ms=m.latency_ms, prompt_tokens=m.prompt_tokens,
        completion_tokens=m.completion_tokens, estimated_cost_usd=m.estimated_cost_usd,
        execution_log=execution_log,
        research_run_id=m.research_run_id,
        research=research,
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
    terminal = event_type in ("done", "error", "plan_proposed")
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
        "document_preview": None,
    })


def _durable_event_iterator(worker, on_client_disconnect=None):
    """Return an SSE iterator while `worker` runs independently in a thread."""
    q: _queue_module.Queue = _queue_module.Queue()

    def _worker() -> None:
        try:
            for event in worker():
                q.put(event)
        except HTTPException as exc:
            q.put(_sse("error", {"message": exc.detail}))
        except Exception as exc:
            q.put(_sse("error", {"message": _friendly_error(exc)}))
        finally:
            q.put(None)

    _threading.Thread(target=_worker, daemon=True).start()

    def _events():
        try:
            while True:
                item = q.get()
                if item is None:
                    break
                yield item
        except GeneratorExit:
            # Client went away; the worker owns persistence and should continue.
            if on_client_disconnect:
                on_client_disconnect()
            return

    return _events()


def _run_durable_stream(worker, on_client_disconnect=None) -> StreamingResponse:
    """Run a streaming turn independently of the client SSE connection."""
    return StreamingResponse(
        _durable_event_iterator(worker, on_client_disconnect=on_client_disconnect),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _pipeline_log(stage: str, message: str, **kwargs) -> str:
    data: dict = {"stage": stage, "message": message}
    data.update(kwargs)
    return f"event: pipeline_log\ndata: {json.dumps(data)}\n\n"


def _planner_selected_log(plan) -> str:
    model = plan.planner_model or "none"
    message = (
        "Planner unavailable — using safe passthrough"
        if model == "none"
        else f"Planner selected: {model}"
    )
    return _pipeline_log(
        "planning",
        message,
        model=model,
        latency_ms=plan.planner_latency_ms,
        cost_usd=plan.planner_cost_usd,
    )


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
                    yield _pipeline_log(
                        "working",
                        f"Turn is already {existing_turn.status}; reconnecting to the conversation when it finishes.",
                        turn_id=existing_turn.public_id,
                    )
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
            db.commit()

            for payload in _stream_turn(db, conv, req, user_id, is_admin, settings, history, user_memory, profile, user_msg):
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
    )


def _stream_turn(db, conv, req, user_id, is_admin, settings, history, user_memory, profile, user_msg, preloaded_plan=None):
            yield _sse("start", {"conversation_id": conv.public_id})
            yield _pipeline_log("planning", "Analysing your request…")

            twin_profile = get_twin_profile(db, user_id)
            output_mode: OutputMode = req.output_mode
            research_mode = req.research_mode if req.research_mode != "quick" else ("deep" if req.deep_research else "quick")

            if research_mode != "quick":
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
                    pre_gate = plan_gate.evaluate(plan)
                    if pre_gate.mode == "confirm":
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

                    gate = plan_gate.evaluate(plan)
                    document_preview = None
                    if plan.wants_document_output and gate.capabilities["document"].enabled:
                        yield _pipeline_log("working", "Drafting your document from research findings…")
                        doc_cap = gate.capabilities["document"]
                        fmt = doc_cap.extra.get("format_recommendation", "markdown")

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
                        artifact_context = ARTIFACT_PROMPTS.get(req.artifact_type or "", "")
                        followup_cost = result.estimated_cost_usd or 0.0

                        result, doc_body, chat_summary, doc_type = generate_document_output(
                            plan, route, history, empty_wc, planner_ctx,
                            doc_context, False, False,
                            artifact_context=artifact_context,
                        )
                        for chunk in [chat_summary[i:i + 80] for i in range(0, len(chat_summary), 80)]:
                            yield _sse("token", {"text": chunk})
                        final_answer = chat_summary
                        result.estimated_cost_usd = (
                            (result.estimated_cost_usd or 0.0)
                            + followup_cost
                            + plan.planner_cost_usd
                        )
                        title = (plan.document_brief or {}).get("title") or plan.intent
                        document_preview = build_document_artifact(title or "Fronei document", doc_body, doc_type, fmt)
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
                    )
                    db.add(asst_msg)
                    _maybe_update_title(conv, plan.intent or research_query)
                    conv.message_count += 2
                    conv.updated_at = datetime.now(timezone.utc)
                    db.commit()
                    db.refresh(asst_msg)

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
                        error_holder[0] = exc
                    finally:
                        research_db.close()
                        progress_q.put(None)

                t = _threading.Thread(target=_run_research_worker, daemon=True)
                t.start()

                while True:
                    update = progress_q.get()
                    if update is None:
                        break
                    stage = update.pop("stage")
                    message = update.pop("message")
                    yield _pipeline_log(stage, message, **update)

                if error_holder[0]:
                    raise error_holder[0]
                research = research_holder[0]
                if research is None:
                    yield _sse("error", {"message": "Research pipeline returned no result"})
                    return

                result = research.result
                final_answer = result.answer
                route = research.route

                # ── Document capability (post-research) ─────────────────────
                # If the confirmed plan also wants a document, feed the research
                # findings into the document generator instead of just streaming
                # the research answer as chat text.
                gate = plan_gate.evaluate(plan)
                document_preview = None
                if plan.wants_document_output and gate.capabilities["document"].enabled:
                    yield _pipeline_log("working", "Drafting your document from research findings…")
                    doc_cap = gate.capabilities["document"]
                    fmt = doc_cap.extra.get("format_recommendation", "markdown")

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
                    artifact_context = ARTIFACT_PROMPTS.get(req.artifact_type or "", "")

                    result, doc_body, chat_summary, doc_type = generate_document_output(
                        plan, route, history, empty_wc, planner_ctx,
                        doc_context, False, False,
                        artifact_context=artifact_context,
                    )
                    for chunk in [chat_summary[i:i + 80] for i in range(0, len(chat_summary), 80)]:
                        yield _sse("token", {"text": chunk})
                    final_answer = chat_summary
                    result.estimated_cost_usd = (
                        (result.estimated_cost_usd or 0.0)
                        + (research.result.estimated_cost_usd or 0.0)
                        + plan.planner_cost_usd
                    )
                    title = (plan.document_brief or {}).get("title") or plan.intent
                    document_preview = build_document_artifact(title or "Fronei document", doc_body, doc_type, fmt)
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
                )

                asst_msg = ConversationMessage(
                    conversation_id=conv.id, role="assistant",
                    content=final_answer, task_type=route.task_type, complexity=route.complexity,
                    model_used=result.model_used, latency_ms=result.latency_ms,
                    prompt_tokens=result.prompt_tokens, completion_tokens=result.completion_tokens,
                    estimated_cost_usd=result.estimated_cost_usd,
                    execution_log_json=exec_log.model_dump_json(),
                    research_run_id=research.run.id,
                )
                plan.action = f"research_{research_mode}"
                rules_entry = _update_conversation_state(conv, plan, final_answer)
                _maybe_update_title(conv, plan.intent or research_query)
                db.add(asst_msg)
                conv.message_count += 2
                conv.updated_at = datetime.now(timezone.utc)
                db.commit()
                db.refresh(asst_msg)

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
            gate = plan_gate.evaluate(plan)
            if gate.mode == "confirm" and confirmed is None:
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
                yield _pipeline_log("working", "Drafting your document…")
                doc_cap = gate.capabilities["document"]
                fmt = doc_cap.extra.get("format_recommendation", "markdown")
                result, doc_body, chat_summary, doc_type = generate_document_output(
                    plan, route, history, wc, planner_ctx,
                    setup.doc_context, req.deep_research, enable_native,
                    artifact_context=setup.artifact_context or "",
                )
                for chunk in [chat_summary[i:i + 80] for i in range(0, len(chat_summary), 80)]:
                    yield _sse("token", {"text": chunk})

                final_answer = chat_summary
                sq_logs: list[SubQueryLog] = []
                exec_log = build_exec_log(plan, wc, result, sq_logs, enable_native, req.deep_research)
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
                document_preview = build_document_artifact(title or "Fronei document", doc_body, doc_type, fmt)

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
            exec_log = build_exec_log(plan, wc, result, sq_logs, setup.enable_native, req.deep_research)
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

            req = ConvChatRequest(
                message=message_text,
                conversation_id=conv_id,
                profile=profile,
                deep_research=bool(body.confirmed_plan.deep_research) if body.confirmed_plan.deep_research is not None else plan.recommend_deep_research,
                research_mode=body.confirmed_plan.research_mode or "quick",
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
            db.commit()

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
    )
