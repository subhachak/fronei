"""Research run inspection endpoints."""
from fastapi import APIRouter, HTTPException

from app.auth import CurrentUser
from app.db.models import SessionLocal
from app.schemas import ResearchMeta
from app.services.research_metadata import research_meta_for_run_id


router = APIRouter(prefix="/research-runs", tags=["research-runs"])


@router.get("/{run_id}", response_model=ResearchMeta)
def get_research_run(run_id: int, user_id: str = CurrentUser) -> ResearchMeta:
    db = SessionLocal()
    try:
        meta = research_meta_for_run_id(db, run_id, user_id)
        if meta is None:
            raise HTTPException(status_code=404, detail="Research run not found")
        return meta
    finally:
        db.close()
