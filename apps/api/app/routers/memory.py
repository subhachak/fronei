from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException, Query
from app.auth import CurrentActiveUser
from app.db.models import DEFAULT_DECAY_RATES, UserMemory, SessionLocal
from app.schemas import MemoryCreate, MemoryItem, MemoryUpdate

router = APIRouter(prefix="/memory", tags=["memory"])


def _fmt(dt: datetime) -> str:
    return dt.isoformat()

def _out(m: UserMemory) -> MemoryItem:
    return MemoryItem(
        id=m.id, content=m.content, category=m.category,
        scope=m.scope or "global",
        confidence=m.confidence if m.confidence is not None else 0.6,
        source=m.source or "stated",
        seen_count=m.seen_count or 1,
        last_seen_at=_fmt(m.last_seen_at) if m.last_seen_at else None,
        importance=m.importance if m.importance is not None else 0.5,
        pinned=bool(m.pinned),
        status=m.status or "active",
        source_conversation_id=m.source_conversation_id,
        created_at=_fmt(m.created_at), updated_at=_fmt(m.updated_at),
    )


@router.get("", response_model=list[MemoryItem])
def list_memories(
    include_superseded: bool = Query(default=False),
    user_id: str = CurrentActiveUser,
) -> list[MemoryItem]:
    db = SessionLocal()
    try:
        q = db.query(UserMemory).filter(UserMemory.user_id == user_id)
        if not include_superseded:
            q = q.filter(UserMemory.status == "active")
        mems = q.order_by(UserMemory.updated_at.desc()).all()
        return [_out(m) for m in mems]
    finally:
        db.close()


@router.post("", response_model=MemoryItem, status_code=201)
def create_memory(body: MemoryCreate, user_id: str = CurrentActiveUser) -> MemoryItem:
    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        category = body.category.strip() or "general"
        m = UserMemory(
            user_id=user_id,
            content=body.content.strip(),
            category=category,
            scope=body.scope.strip() or "global",
            source="confirmed",
            confidence=1.0,
            importance=0.9,
            decay_rate=DEFAULT_DECAY_RATES.get(category, DEFAULT_DECAY_RATES["general"]),
            pinned=True,
            status="active",
            last_seen_at=now,
            created_at=now,
            updated_at=now,
        )
        db.add(m); db.commit(); db.refresh(m)
        return _out(m)
    finally:
        db.close()


@router.patch("/{memory_id}", response_model=MemoryItem)
def update_memory(memory_id: int, body: MemoryUpdate, user_id: str = CurrentActiveUser) -> MemoryItem:
    db = SessionLocal()
    try:
        m = db.get(UserMemory, memory_id)
        if not m:
            raise HTTPException(status_code=404, detail="Memory not found")
        if m.user_id != user_id:
            raise HTTPException(status_code=403, detail="Forbidden")
        now = datetime.now(timezone.utc)
        if body.content is not None:
            m.content = body.content.strip()
            m.source = "confirmed"
            m.confidence = 1.0
            m.importance = max(m.importance or 0.5, 0.9)
        if body.category is not None:
            m.category = body.category.strip() or "general"
            m.decay_rate = DEFAULT_DECAY_RATES.get(m.category, DEFAULT_DECAY_RATES["general"])
        if body.scope is not None:
            m.scope = body.scope.strip() or "global"
        if body.confidence is not None:
            m.confidence = body.confidence
        if body.status is not None:
            m.status = body.status
        if body.pinned is not None:
            m.pinned = body.pinned
            if body.pinned:
                m.source = "confirmed"
                m.confidence = 1.0
                m.importance = max(m.importance or 0.5, 0.9)
        m.updated_at = now
        m.last_seen_at = now
        db.commit()
        db.refresh(m)
        return _out(m)
    finally:
        db.close()


@router.delete("/{memory_id}", status_code=204)
def delete_memory(memory_id: int, user_id: str = CurrentActiveUser) -> None:
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
    user_id: str = CurrentActiveUser,
) -> None:
    if not confirm:
        raise HTTPException(status_code=400, detail="Pass ?confirm=true to clear all memories.")
    db = SessionLocal()
    try:
        db.query(UserMemory).filter(UserMemory.user_id == user_id).delete()
        db.commit()
    finally:
        db.close()
