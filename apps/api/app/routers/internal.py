import hmac

from fastapi import APIRouter, Header, HTTPException, Query, status
from sqlalchemy import inspect, text

from app.config import get_settings
from app.db.models import SessionLocal, engine
from app.db.schema_check import check_schema_version
from app.services.maintenance_jobs import (
    enqueue_langgraph_checkpoint_cleanup,
    enqueue_profile_consolidation,
    get_job,
    maintenance_job_worker,
)

router = APIRouter(prefix="/internal", tags=["internal"])


def _require_internal_secret(x_internal_secret: str) -> None:
    settings = get_settings()
    if not settings.internal_task_secret or not hmac.compare_digest(
        x_internal_secret, settings.internal_task_secret
    ):
        raise HTTPException(status_code=403, detail="Forbidden")


@router.post("/consolidate-profiles", status_code=status.HTTP_202_ACCEPTED)
def consolidate_profiles(
    x_internal_secret: str = Header(default=""),
    lookback_days: int = Query(default=30, ge=1, le=365),
    max_workspaces: int = Query(default=500, ge=1, le=5000),
) -> dict:
    """Idempotently enqueue durable profile consolidation."""
    _require_internal_secret(x_internal_secret)
    job, created = enqueue_profile_consolidation(
        lookback_days=lookback_days,
        max_workspaces=max_workspaces,
    )
    maintenance_job_worker.notify()
    return {"status": "queued" if created else "already_queued", "job": job}


@router.post("/cleanup-langgraph-checkpoints", status_code=status.HTTP_202_ACCEPTED)
def cleanup_langgraph_checkpoints_endpoint(
    x_internal_secret: str = Header(default=""),
    retention_days: int | None = Query(default=None, ge=1, le=365),
) -> dict:
    """Idempotently enqueue LangGraph checkpoint + run-context cleanup (Gap 2/4)."""
    _require_internal_secret(x_internal_secret)
    job, created = enqueue_langgraph_checkpoint_cleanup(retention_days=retention_days)
    maintenance_job_worker.notify()
    return {"status": "queued" if created else "already_queued", "job": job}


@router.get("/maintenance-jobs/{job_id}")
def maintenance_job_status(
    job_id: str,
    x_internal_secret: str = Header(default=""),
) -> dict:
    _require_internal_secret(x_internal_secret)
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Maintenance job not found.")
    return {"status": "ok", "job": job}


@router.post("/smoke")
def smoke_check(x_internal_secret: str = Header(default="")) -> dict:
    _require_internal_secret(x_internal_secret)
    settings = get_settings()
    check_schema_version(engine)

    required_tables = [
        "users",
        "workspaces",
        "conversations",
        "turns",
        "maintenance_jobs",
        "admin_settings",
    ]
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    missing_tables = [table for table in required_tables if table not in existing_tables]
    if missing_tables:
        raise HTTPException(
            status_code=500,
            detail=f"Missing required tables: {', '.join(missing_tables)}",
        )

    db = SessionLocal()
    try:
        db.execute(text("SELECT 1"))
    finally:
        db.close()

    config = {
        "app_env": settings.app_env,
        "clerk_issuer_configured": bool(settings.clerk_issuer),
        "clerk_audience_configured": bool(settings.clerk_audience),
        "clerk_authorized_parties_configured": bool(
            settings.clerk_authorized_party_list
        ),
        "llm_provider_configured": bool(
            settings.openrouter_api_key
            or settings.openai_api_key
            or settings.anthropic_api_key
            or settings.gemini_api_key
        ),
    }
    return {
        "status": "ok",
        "database": "ok",
        "schema": "ok",
        "tables_checked": required_tables,
        "config": config,
    }
