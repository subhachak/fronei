from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException, Query
from app.auth import CurrentUser
from app.db.models import UserMemory, SessionLocal
from app.schemas import MemoryCreate, MemoryItem

router = APIRouter(prefix="/memory", tags=["memory"])


def _fmt(dt: datetime) -> str:
    return dt.isoformat()

def _out(m: UserMemory) -> MemoryItem:
    return MemoryItem(
        id=m.id, content=m.content, category=m.category,
        source_conversation_id=m.source_conversation_id,
        created_at=_fmt(m.created_at), updated_at=_fmt(m.updated_at),
    )


@router.get("", response_model=list[MemoryItem])
def list_memories(user_id: str = CurrentUser) -> list[MemoryItem]:
    db = SessionLocal()
    try:
        mems = (
            db.query(UserMemory)
            .filter(UserMemory.user_id == user_id)
            .order_by(UserMemory.updated_at.desc())
            .all()
        )
        return [_out(m) for m in mems]
    finally:
        db.close()


@router.post("", response_model=MemoryItem, status_code=201)
def create_memory(body: MemoryCreate, user_id: str = CurrentUser) -> MemoryItem:
    db = SessionLocal()
    try:
        m = UserMemory(user_id=user_id, content=body.content.strip(), category=body.category)
        db.add(m); db.commit(); db.refresh(m)
        return _out(m)
    finally:
        db.close()


@router.delete("/{memory_id}", status_code=204)
def delete_memory(memory_id: int, user_id: str = CurrentUser) -> None:
    db = SessionLocal()
    try:
        m = db.get(UserMemory, memory_id)
        if not m:
            raise HTTPException(status_code=404, detail="Memory not found")
        if m.user_id != user_id:
            raise HTTPException(status_code=403, detail="Forbidden")
        db.delete(m); db.commit()
    finally:
        db.close()


@router.delete("", status_code=204)
def clear_memories(
    confirm: bool = Query(default=False),
    user_id: str = CurrentUser,
) -> None:
    if not confirm:
        raise HTTPException(status_code=400, detail="Pass ?confirm=true to clear all memories.")
    db = SessionLocal()
    try:
        db.query(UserMemory).filter(UserMemory.user_id == user_id).delete()
        db.commit()
    finally:
        db.close()
