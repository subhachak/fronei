from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from app.auth import CurrentUser
from app.services.agent_v3 import persistence
from app.services.agent_v3.models import AgentV3Request, AgentV3Result, Goal, ProgressEvent, StreamEnvelope
from app.services.agent_v3.runtime import AgentV3Runtime

router = APIRouter(prefix="/agent-v3", tags=["agent-v3"])


def _sse(envelope: StreamEnvelope) -> str:
    return f"event: {envelope.type}\ndata: {json.dumps(envelope.data, default=str)}\n\n"


@router.post("/turns/stream")
def stream_agent_v3_turn(request: AgentV3Request, user_id: str = CurrentUser) -> StreamingResponse:
    """Run the fresh v3 runtime.

    This endpoint intentionally bypasses conversations, turn_graph, the old planner,
    legacy research, and legacy document generation. It is an isolated proving
    ground for the clean runtime.
    """

    runtime = AgentV3Runtime()

    def generate():
        turn_id: str | None = None
        for envelope in runtime.run_stream(request, user_id=user_id):
            if envelope.type == "start":
                turn_id = str(envelope.data.get("turn_id") or "")
                goal = Goal.model_validate(envelope.data.get("goal"))
                persistence.create_turn(goal, turn_id)
            elif envelope.type == "progress":
                persistence.append_event(ProgressEvent.model_validate(envelope.data))
            elif envelope.type == "result":
                result = AgentV3Result.model_validate(envelope.data)
                persistence.complete_turn(result)
            elif envelope.type == "error" and turn_id:
                persistence.fail_turn(turn_id, str(envelope.data.get("message") or "Agent v3 failed"))
            yield _sse(envelope)

    return StreamingResponse(generate(), media_type="text/event-stream")


@router.get("/turns/{turn_id}")
def get_agent_v3_turn(turn_id: str, user_id: str = CurrentUser) -> dict:
    result = persistence.load_turn(turn_id, user_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Agent v3 turn not found")
    return result.model_dump(mode="json")
