from collections import defaultdict
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Query
from app.auth import CurrentUser
from app.db.models import Conversation, ConversationMessage, RequestLog, SessionLocal
from app.schemas import (
    AnalyticsResponse, AnalyticsSummary, DailyStat,
    ModelUsageStat, TaskStat, ModelDetailStat,
)

router = APIRouter(prefix="/analytics", tags=["analytics"])

_RANGES: dict[str, timedelta] = {
    "1d": timedelta(days=1),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}

# Normalized row shape used for all aggregations.
# {created_at, model_used, task_type, estimated_cost_usd, latency_ms, prompt_tokens, completion_tokens}


@router.get("", response_model=AnalyticsResponse)
def get_analytics(
    range: str = Query(default="7d", pattern="^(1d|7d|30d|all)$"),
    user_id: str = CurrentUser,
) -> AnalyticsResponse:
    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        start = now - _RANGES[range] if range in _RANGES else None

        # ── Collect from ConversationMessage (multi-turn responses) ────────────
        cq = (
            db.query(
                ConversationMessage.created_at,
                ConversationMessage.model_used,
                ConversationMessage.task_type,
                ConversationMessage.estimated_cost_usd,
                ConversationMessage.latency_ms,
                ConversationMessage.prompt_tokens,
                ConversationMessage.completion_tokens,
            )
            .join(Conversation, ConversationMessage.conversation_id == Conversation.id)
            .filter(ConversationMessage.role == "assistant", Conversation.user_id == user_id)
        )
        if start:
            cq = cq.filter(ConversationMessage.created_at >= start)
        conv_rows = [
            {
                "created_at":        r.created_at,
                "model_used":        r.model_used,
                "task_type":         r.task_type,
                "estimated_cost_usd": r.estimated_cost_usd,
                "latency_ms":        r.latency_ms,
                "prompt_tokens":     r.prompt_tokens,
                "completion_tokens": r.completion_tokens,
            }
            for r in cq.all()
        ]

        # ── Collect from RequestLog (single-turn /chat requests) ───────────────
        rq = db.query(RequestLog).filter(RequestLog.status == "success", RequestLog.user_id == user_id)
        if start:
            rq = rq.filter(RequestLog.created_at >= start)
        req_rows = [
            {
                "created_at":        r.created_at,
                "model_used":        r.model_used,
                "task_type":         r.task_type,
                "estimated_cost_usd": r.estimated_cost_usd,
                "latency_ms":        r.latency_ms,
                "prompt_tokens":     r.prompt_tokens,
                "completion_tokens": r.completion_tokens,
            }
            for r in rq.all()
        ]

        rows = conv_rows + req_rows

        # ── Summary ────────────────────────────────────────────────────────────
        total_cost = sum(float(r["estimated_cost_usd"] or 0) for r in rows)
        total_tokens = sum((r["prompt_tokens"] or 0) + (r["completion_tokens"] or 0) for r in rows)
        latencies = [r["latency_ms"] for r in rows if r["latency_ms"] is not None]
        avg_latency = sum(latencies) / len(latencies) if latencies else 0.0
        summary = AnalyticsSummary(
            total_cost=round(total_cost, 6),
            total_requests=len(rows),
            total_tokens=total_tokens,
            avg_latency_ms=round(avg_latency, 1),
        )

        # ── Cost by day ────────────────────────────────────────────────────────
        daily: dict[str, dict] = defaultdict(lambda: {"cost": 0.0, "requests": 0})
        for r in rows:
            key = r["created_at"].strftime("%Y-%m-%d")
            daily[key]["cost"] += float(r["estimated_cost_usd"] or 0)
            daily[key]["requests"] += 1
        cost_by_day = [
            DailyStat(date=d, cost=round(v["cost"], 6), requests=v["requests"])
            for d, v in sorted(daily.items())
        ]

        # ── Per-model accumulation (single pass) ───────────────────────────────
        by_model: dict[str, dict] = defaultdict(lambda: {
            "requests": 0, "cost": 0.0,
            "latencies": [], "prompt_tokens": [], "completion_tokens": [],
        })
        for r in rows:
            if not r["model_used"]:
                continue
            m = by_model[r["model_used"]]
            m["requests"] += 1
            m["cost"] += float(r["estimated_cost_usd"] or 0)
            if r["latency_ms"] is not None:
                m["latencies"].append(r["latency_ms"])
            if r["prompt_tokens"] is not None:
                m["prompt_tokens"].append(r["prompt_tokens"])
            if r["completion_tokens"] is not None:
                m["completion_tokens"].append(r["completion_tokens"])

        model_usage = sorted(
            [
                ModelUsageStat(
                    model=name,
                    requests=d["requests"],
                    total_cost=round(d["cost"], 6),
                    avg_latency_ms=round(sum(d["latencies"]) / len(d["latencies"]), 1)
                    if d["latencies"] else 0.0,
                )
                for name, d in by_model.items()
            ],
            key=lambda x: -x.requests,
        )

        # ── Task distribution ──────────────────────────────────────────────────
        by_task: dict[str, int] = defaultdict(int)
        for r in rows:
            if r["task_type"]:
                by_task[r["task_type"]] += 1
        task_distribution = [
            TaskStat(task_type=t, count=c)
            for t, c in sorted(by_task.items(), key=lambda x: -x[1])
        ]

        # ── Model detail stats with p50 / p95 ─────────────────────────────────
        model_stats: list[ModelDetailStat] = []
        for name, d in sorted(by_model.items(), key=lambda x: -x[1]["requests"]):
            lats = sorted(d["latencies"])
            n = len(lats)
            p50 = lats[max(0, int(n * 0.50) - 1)] if n else 0
            p95 = lats[min(int(n * 0.95), n - 1)] if n else 0
            pt, ct = d["prompt_tokens"], d["completion_tokens"]
            model_stats.append(ModelDetailStat(
                model=name,
                requests=d["requests"],
                avg_latency_ms=round(sum(lats) / n, 1) if n else 0.0,
                p50_latency_ms=p50,
                p95_latency_ms=p95,
                avg_prompt_tokens=round(sum(pt) / len(pt), 1) if pt else 0.0,
                avg_completion_tokens=round(sum(ct) / len(ct), 1) if ct else 0.0,
                total_cost=round(d["cost"], 6),
            ))

        return AnalyticsResponse(
            range=range,
            summary=summary,
            cost_by_day=cost_by_day,
            model_usage=model_usage,
            task_distribution=task_distribution,
            model_stats=model_stats,
        )
    finally:
        db.close()
