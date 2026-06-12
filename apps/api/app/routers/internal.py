import hmac

from fastapi import APIRouter, Header, HTTPException
from sqlalchemy import inspect, text

from app.config import get_settings
from app.db.models import SessionLocal, engine
from app.db.schema_check import check_schema_version
from app.services.memory_consolidator import consolidate_all_active_users


router = APIRouter(prefix="/internal", tags=["internal"])


def _require_internal_secret(x_internal_secret: str) -> None:
    settings = get_settings()
    if not settings.internal_task_secret or not hmac.compare_digest(
        x_internal_secret, settings.internal_task_secret
    ):
        raise HTTPException(status_code=403, detail="Forbidden")


@router.post("/consolidate-profiles")
def consolidate_profiles(x_internal_secret: str = Header(default="")) -> dict:
    _require_internal_secret(x_internal_secret)
    result = consolidate_all_active_users()
    return {"status": "ok", **result}


@router.post("/smoke")
def smoke_check(x_internal_secret: str = Header(default="")) -> dict:
    _require_internal_secret(x_internal_secret)
    settings = get_settings()
    check_schema_version(engine)

    required_tables = [
        "users",
        "conversations",
        "conversation_messages",
        "user_memories",
        "user_profiles",
        "admin_settings",
        "request_logs",
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
