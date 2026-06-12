from fastapi import APIRouter, Header, HTTPException

from app.config import get_settings
from app.services.memory_consolidator import consolidate_all_active_users


router = APIRouter(prefix="/internal", tags=["internal"])


@router.post("/consolidate-profiles")
def consolidate_profiles(x_internal_secret: str = Header(default="")) -> dict:
    settings = get_settings()
    if not settings.internal_task_secret or x_internal_secret != settings.internal_task_secret:
        raise HTTPException(status_code=403, detail="Forbidden")
    result = consolidate_all_active_users()
    return {"status": "ok", **result}
