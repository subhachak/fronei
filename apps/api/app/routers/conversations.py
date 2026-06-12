"""Multi-turn conversation router."""
import json
import queue as _queue_module
import re
import threading as _threading
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import func
from fastapi.responses import StreamingResponse

from app.auth import CurrentUser, CurrentUserIsAdmin
from app.config import get_settings
from app.db.models import (
    Conversation, ConversationMessage, RequestLog, SessionLocal,
    get_effective_monthly_budget, get_monthly_spend,
    get_twin_profile, is_user_pending, is_user_suspended,
)
from app.schemas import (
    ConvChatRequest, ConvChatResponse,
    ConversationDetail, ConversationSummary, ConversationUpdate, MessageOut,
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
    run_pipeline, build_exec_log, build_pipeline_setup,
    _run_sub_queries, _conversation_state,
)
from app.services.planner import passthrough, run_planner
from app.services.rate_limit import check_rate_limit, rate_limiter
from app.services.research_advisor import advise_research

router = APIRouter(prefix="/conversations", tags=["conversations"])


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
        id=conv.id, title=conv.title, profile=conv.profile,
        message_count=conv.message_count, total_cost_usd=0.0,
        created_at=_fmt(conv.created_at), updated_at=_fmt(conv.updated_at),
    )


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
        conv = db.get(Conversation, req.conversation_id)
        if not conv:
            raise HTTPException(status_code=404, detail="Conversation not found")
        if conv.user_id != user_id:
            raise HTTPException(status_code=403, detail="Forbidden")
        profile = req.profile or conv.profile
    return conv, profile


def _build_history(conv: Conversation, db) -> list[dict]:
    msgs = (
        db.query(ConversationMessage)
        .filter(ConversationMessage.conversation_id == conv.id)
        .order_by(ConversationMessage.id.desc())
        .limit(20)
        .all()
    )
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
                id=conv.id,
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
def get_conversation(conv_id: int, user_id: str = CurrentUser) -> ConversationDetail:
    db = SessionLocal()
    try:
        conv = db.get(Conversation, conv_id)
        if not conv:
            raise HTTPException(status_code=404, detail="Conversation not found")
        if conv.user_id != user_id:
            raise HTTPException(status_code=403, detail="Forbidden")
        return ConversationDetail(**_summary(conv).model_dump(), messages=[_msg_out(m, db, user_id) for m in conv.messages])
    finally:
        db.close()


@router.patch("/{conv_id}", response_model=ConversationSummary)
def update_conversation(
    conv_id: int,
    body: ConversationUpdate,
    user_id: str = CurrentUser,
) -> ConversationSummary:
    db = SessionLocal()
    try:
        conv = db.get(Conversation, conv_id)
        if not conv:
            raise HTTPException(status_code=404, detail="Conversation not found")
        if conv.user_id != user_id:
            raise HTTPException(status_code=403, detail="Forbidden")
        conv.title = body.title.strip()
        conv.updated_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(conv)
        return _summary(conv)
    finally:
        db.close()


@router.delete("/{conv_id}", status_code=204)
def delete_conversation(conv_id: int, user_id: str = CurrentUser) -> None:
    db = SessionLocal()
    try:
        conv = db.get(Conversation, conv_id)
        if not conv:
            raise HTTPException(status_code=404, detail="Conversation not found")
        if conv.user_id != user_id:
            raise HTTPException(status_code=403, detail="Forbidden")
        db.delete(conv)
        db.commit()
    finally:
        db.close()


@router.delete("/{conv_id}/messages/from/{message_id}", status_code=204)
def truncate_conversation(
    conv_id: int,
    message_id: int,
    user_id: str = CurrentUser,
) -> None:
    """Delete message_id and all subsequent messages in the conversation."""
    db = SessionLocal()
    try:
        conv = db.get(Conversation, conv_id)
        if not conv:
            raise HTTPException(status_code=404, detail="Conversation not found")
        if conv.user_id != user_id:
            raise HTTPException(status_code=403, detail="Forbidden")
        target = db.get(ConversationMessage, message_id)
        if not target or target.conversation_id != conv_id:
            raise HTTPException(status_code=404, detail="Message not found")
        db.query(ConversationMessage).filter(
            ConversationMessage.conversation_id == conv_id,
            ConversationMessage.id >= message_id,
        ).delete(synchronize_session=False)
        conv.message_count = db.query(ConversationMessage).filter(
            ConversationMessage.conversation_id == conv_id
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
            conversation_id=conv.id,
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


# ── Streaming endpoint ────────────────────────────────────────────────────────

@router.post("/chat/stream", dependencies=[rate_limiter("chat", "rate_limit_chat_per_minute", 60)])
def chat_stream(req: ConvChatRequest, user_id: str = CurrentUser, is_admin: bool = CurrentUserIsAdmin) -> StreamingResponse:
    """
    Same pipeline as /chat but streams tokens via Server-Sent Events.
    Events: start → pipeline_log × N → token × N → done  (or error on failure)
    """
    settings = get_settings()

    def _sse(event: str, data: dict) -> str:
        return f"event: {event}\ndata: {json.dumps(data)}\n\n"

    def _pipeline_log(stage: str, message: str, **kwargs) -> str:
        data: dict = {"stage": stage, "message": message}
        data.update(kwargs)
        return f"event: pipeline_log\ndata: {json.dumps(data)}\n\n"

    def event_generator():
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

            yield _sse("start", {"conversation_id": conv.id})
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
                existing_run_id = _get_last_research_run_id(db, conv)
                running_summary, active_task = _conversation_state(conv)
                plan = run_planner(
                    req.message, history, settings.planner_model,
                    running_summary=running_summary,
                    active_task=active_task,
                    user_memory=user_memory,
                )
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

                if is_followup and existing_run_id:
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

                    for chunk in [final_answer[i:i+80] for i in range(0, len(final_answer), 80)]:
                        yield _sse("token", {"text": chunk})

                    exec_log = ExecutionLog(
                        planner=PlannerLog(
                            model="research_followup",
                            latency_ms=0, cost_usd=0.0,
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
                        total_latency_ms=result.latency_ms,
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
                if should_refine(result.answer, output_mode, twin_profile):
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

                exec_log = ExecutionLog(
                    planner=PlannerLog(
                        model="research_orchestrator",
                        latency_ms=0,
                        cost_usd=0.0,
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
                    total_latency_ms=result.latency_ms,
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
                    "was_refined": final_answer != result.answer,
                    "research_run_id": research.run.id,
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

            setup         = build_pipeline_setup(req, conv, history, settings, user_memory=user_memory)
            plan          = setup.plan
            route         = setup.route
            wc            = setup.wc
            enable_native = setup.enable_native
            planner_ctx   = setup.planner_ctx

            if req.allow_research_recommendation and not req.deep_research and req.research_mode == "quick":
                recommendation = advise_research(
                    req.message,
                    plan,
                    has_attached_documents=bool(req.attached_documents),
                )
                if recommendation.recommend:
                    yield _sse("research_recommendation", {
                        "conversation_id": conv.id,
                        "confidence": recommendation.confidence,
                        "reason": recommendation.reason,
                        "risk_factors": recommendation.risk_factors,
                        "suggested_mode": recommendation.suggested_mode,
                        "source": recommendation.source,
                        "planner": {
                            "model": plan.planner_model,
                            "latency_ms": plan.planner_latency_ms,
                            "intent": plan.intent,
                            "needs_web_search": plan.needs_web_search,
                        },
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
            })

        except HTTPException as exc:
            yield _sse("error", {"message": exc.detail})
        except Exception as exc:
            yield _sse("error", {"message": _friendly_error(exc)})
        finally:
            db.close()

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
