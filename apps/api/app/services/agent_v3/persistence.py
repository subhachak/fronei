from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from app.db.models import (
    AgentV3Artifact,
    AgentV3Event,
    AgentV3ToolCall,
    AgentV3Turn,
    SessionLocal,
)
from app.services.agent_v3.models import AgentV3Result, Artifact, Goal, ProgressEvent, Source, ToolCall


def _dumps(value: Any) -> str:
    return json.dumps(value, default=str)


def _loads(value: str | None, fallback):
    if not value:
        return fallback
    try:
        return json.loads(value)
    except Exception:
        return fallback


def create_turn(goal: Goal, turn_id: str) -> None:
    db = SessionLocal()
    try:
        existing = db.get(AgentV3Turn, turn_id)
        if existing:
            return
        db.add(
            AgentV3Turn(
                id=turn_id,
                user_id=goal.user_id,
                conversation_id=goal.conversation_id,
                objective=goal.objective,
                route=goal.route,
                quality_mode=goal.quality_mode,
                status="running",
            )
        )
        db.commit()
    finally:
        db.close()


def append_event(event: ProgressEvent) -> None:
    db = SessionLocal()
    try:
        if db.get(AgentV3Event, event.event_id):
            return
        db.add(
            AgentV3Event(
                id=event.event_id,
                turn_id=event.turn_id,
                stage=event.stage,
                message=event.message,
                data_json=_dumps(event.data),
                created_at=event.created_at,
            )
        )
        db.commit()
    finally:
        db.close()


def complete_turn(result: AgentV3Result) -> None:
    db = SessionLocal()
    try:
        turn = db.get(AgentV3Turn, result.turn_id)
        if turn is None:
            turn = AgentV3Turn(
                id=result.turn_id,
                user_id=result.goal.user_id,
                conversation_id=result.goal.conversation_id,
                objective=result.goal.objective,
                route=result.route,
                quality_mode=result.goal.quality_mode,
            )
            db.add(turn)
        turn.status = "completed"
        turn.answer = result.answer
        turn.model_used = result.model_used
        turn.sources_json = _dumps([source.model_dump(mode="json") for source in result.sources])
        turn.latency_ms = result.latency_ms
        turn.cost_usd = result.cost_usd
        turn.completed_at = datetime.now(timezone.utc)

        for tool in result.tool_calls:
            if db.get(AgentV3ToolCall, tool.id):
                continue
            db.add(
                AgentV3ToolCall(
                    id=tool.id,
                    turn_id=result.turn_id,
                    name=tool.name,
                    input_json=_dumps(tool.input),
                    output_json=_dumps(tool.output),
                    ok=tool.ok,
                    error=tool.error,
                    latency_ms=tool.latency_ms,
                )
            )

        for artifact in result.artifacts:
            if db.get(AgentV3Artifact, artifact.id):
                continue
            db.add(
                AgentV3Artifact(
                    id=artifact.id,
                    turn_id=result.turn_id,
                    kind=artifact.kind,
                    filename=artifact.filename,
                    mime_type=artifact.mime_type,
                    base64_data=artifact.base64_data,
                )
            )
        db.commit()
    finally:
        db.close()


def fail_turn(turn_id: str, message: str) -> None:
    db = SessionLocal()
    try:
        turn = db.get(AgentV3Turn, turn_id)
        if turn:
            turn.status = "failed"
            turn.error_message = message
            turn.completed_at = datetime.now(timezone.utc)
            db.commit()
    finally:
        db.close()


def load_turn(turn_id: str, user_id: str) -> AgentV3Result | None:
    db = SessionLocal()
    try:
        turn = db.get(AgentV3Turn, turn_id)
        if turn is None or turn.user_id != user_id:
            return None
        events = (
            db.query(AgentV3Event)
            .filter(AgentV3Event.turn_id == turn_id)
            .order_by(AgentV3Event.created_at.asc())
            .all()
        )
        tool_rows = (
            db.query(AgentV3ToolCall)
            .filter(AgentV3ToolCall.turn_id == turn_id)
            .order_by(AgentV3ToolCall.created_at.asc())
            .all()
        )
        artifact_rows = (
            db.query(AgentV3Artifact)
            .filter(AgentV3Artifact.turn_id == turn_id)
            .order_by(AgentV3Artifact.created_at.asc())
            .all()
        )
        goal = Goal(
            id=f"goal_for_{turn.id}",
            user_id=turn.user_id,
            conversation_id=turn.conversation_id,
            objective=turn.objective,
            route=turn.route,  # type: ignore[arg-type]
            quality_mode=turn.quality_mode,
            created_at=turn.created_at,
        )
        return AgentV3Result(
            turn_id=turn.id,
            goal=goal,
            answer=turn.answer,
            route=turn.route,  # type: ignore[arg-type]
            model_used=turn.model_used,
            sources=[Source.model_validate(item) for item in _loads(turn.sources_json, [])],
            tool_calls=[
                ToolCall(
                    id=row.id,
                    name=row.name,
                    input=_loads(row.input_json, {}),
                    output=_loads(row.output_json, {}),
                    ok=row.ok,
                    error=row.error,
                    latency_ms=row.latency_ms,
                )
                for row in tool_rows
            ],
            artifacts=[
                Artifact(
                    id=row.id,
                    kind=row.kind,  # type: ignore[arg-type]
                    filename=row.filename,
                    mime_type=row.mime_type,
                    base64_data=row.base64_data,
                )
                for row in artifact_rows
            ],
            events=[
                ProgressEvent(
                    event_id=row.id,
                    turn_id=row.turn_id,
                    stage=row.stage,
                    message=row.message,
                    data=_loads(row.data_json, {}),
                    created_at=row.created_at,
                )
                for row in events
            ],
            latency_ms=turn.latency_ms,
            cost_usd=turn.cost_usd,
            created_at=turn.created_at,
        )
    finally:
        db.close()
