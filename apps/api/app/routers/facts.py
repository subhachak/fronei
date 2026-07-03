from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.auth import CurrentActiveUser
from app.db.models import SessionLocal
from app.services.agent.known_facts import delete_fact, get_facts_for_type, upsert_fact

router = APIRouter(prefix="/facts", tags=["facts"])


class FactIn(BaseModel):
    entity_id: str = Field(min_length=1, max_length=200)
    entity_type: str = Field(min_length=1, max_length=100)
    fact_key: str = Field(min_length=1, max_length=200)
    fact_value: str = Field(min_length=1, max_length=2000)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class FactOut(BaseModel):
    id: str
    entity_id: str
    entity_type: str
    fact_key: str
    fact_value: str
    confidence: float
    source_conversation_id: str | None
    created_at: str | None
    updated_at: str | None


@router.get("", response_model=list[FactOut])
def list_facts(
    entity_type: str = "workspace",
    user_id: str = CurrentActiveUser,
) -> list[FactOut]:
    db = SessionLocal()
    try:
        rows = get_facts_for_type(user_id, entity_type, db=db)
        return [FactOut(**row) for row in rows]
    finally:
        db.close()


@router.put("", response_model=FactOut)
def put_fact(
    body: FactIn,
    user_id: str = CurrentActiveUser,
) -> FactOut:
    db = SessionLocal()
    try:
        upsert_fact(
            user_id,
            body.entity_id,
            body.entity_type,
            body.fact_key,
            body.fact_value,
            db=db,
            source_conversation_id=None,
            confidence=body.confidence,
        )
        rows = get_facts_for_type(user_id, body.entity_type, db=db)
        match = next(
            (row for row in rows if row["entity_id"] == body.entity_id and row["fact_key"] == body.fact_key),
            None,
        )
        if match is None:
            raise HTTPException(status_code=500, detail="fact not found after upsert")
        return FactOut(**match)
    finally:
        db.close()


@router.delete("/{entity_id}/{fact_key}", status_code=204)
def remove_fact(
    entity_id: str,
    fact_key: str,
    user_id: str = CurrentActiveUser,
) -> None:
    db = SessionLocal()
    try:
        delete_fact(user_id, entity_id, fact_key, db=db)
    finally:
        db.close()
