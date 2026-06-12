"""Single-turn (stateless) chat endpoint."""
from fastapi import APIRouter, HTTPException

from app.auth import CurrentUser, CurrentUserIsAdmin
from app.config import get_settings
from app.db.models import (
    RequestLog,
    SessionLocal,
    get_effective_monthly_budget,
    get_monthly_spend,
    is_user_pending,
    is_user_suspended,
)
from app.schemas import ChatRequest, ChatResponse
from app.services.budget_guard import enforce_global_monthly_budget
from app.services.llm_gateway import invoke_llm
from app.services.chat_pipeline import _build_doc_context
from app.services.planner import run_planner
from app.services.rate_limit import check_rate_limit, rate_limiter
from app.services.router import choose_route
from app.services.web_context import gather_web_context

router = APIRouter(prefix="/chat", tags=["chat"])


@router.post("", response_model=ChatResponse, dependencies=[rate_limiter("chat", "rate_limit_chat_per_minute", 60)])
def chat(req: ChatRequest, user_id: str = CurrentUser, is_admin: bool = CurrentUserIsAdmin) -> ChatResponse:
    settings = get_settings()
    route = None
    db = SessionLocal()
    try:
        # ── Budget gate (before any LLM calls) ───────────────────────────────
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

        # ── Plan ─────────────────────────────────────────────────────────────
        # No history for single-turn requests, but the planner still improves
        # task classification, web search detection, and prompt enrichment.
        doc_ctx = _build_doc_context(req.attached_documents)
        plan = run_planner(req.message, [], settings.planner_model, doc_context=doc_ctx)
        use_web = req.web_search or plan.needs_web_search
        wc = gather_web_context(plan.search_query or req.message, use_web or req.deep_research)

        # ── Route ─────────────────────────────────────────────────────────────
        route = choose_route(
            req.message, req.profile, req.force_model, req.deep_research,
            web_search=use_web,
            task_override=plan.task_type,
            complexity_override=plan.complexity,
        )
        if use_web or req.deep_research:
            route.reason = f"{route.reason} {wc.status}"
        if plan.planner_model != "none":
            route.reason = f"[planner:{plan.planner_model} {plan.planner_latency_ms}ms] {route.reason}"

        # ── Execute ───────────────────────────────────────────────────────────
        result = invoke_llm(
            plan.enriched_prompt,
            route,
            deep_research=req.deep_research,
            web_context=wc.context,
            enable_native_search=use_web or req.deep_research,
            planner_context=plan.context_summary or None,
            doc_context=doc_ctx or None,
        )
        # Include planner's own LLM cost in the reported total
        result.estimated_cost_usd = (result.estimated_cost_usd or 0.0) + plan.planner_cost_usd
        db.add(RequestLog(
            user_id=user_id,
            message=req.message,
            task_type=route.task_type,
            complexity=route.complexity,
            profile=route.profile,
            selected_model=route.primary_model,
            model_used=result.model_used,
            latency_ms=result.latency_ms,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            estimated_cost_usd=result.estimated_cost_usd,
            status="success",
        ))
        db.commit()
        return ChatResponse(
            answer=result.answer,
            route=route,
            model_used=result.model_used,
            latency_ms=result.latency_ms,
            estimated_cost_usd=result.estimated_cost_usd,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
        )
    except HTTPException:
        raise
    except Exception as exc:
        db.add(RequestLog(
            user_id=user_id,
            message=req.message,
            task_type=route.task_type if route else "unknown",
            complexity=route.complexity if route else "medium",
            profile=route.profile if route else (req.profile or "balanced"),
            selected_model=route.primary_model if route else "none",
            model_used="none",
            latency_ms=0,
            status="error",
            error=str(exc),
        ))
        db.commit()
        raise HTTPException(status_code=502, detail=str(exc))
    finally:
        db.close()
