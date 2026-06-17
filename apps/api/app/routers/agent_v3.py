from __future__ import annotations

import json
import logging
import time
from queue import Empty, Queue
from threading import Thread

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response, StreamingResponse

from app.auth import CurrentUser
from app.services.agent_v3 import persistence
from app.services.agent_v3.models import (
    AgentV3ConversationCreate,
    AgentV3Request,
    AgentV3Result,
    AgentV3WorkspaceCreate,
    AgentV3WorkspaceUpdate,
    Goal,
    ProgressEvent,
    StreamEnvelope,
)
from app.services.agent_v3.runtime import AgentV3Runtime

router = APIRouter(prefix="/agent-v3", tags=["agent-v3"])
logger = logging.getLogger(__name__)

AGENT_V3_SSE_HEARTBEAT_SECONDS = 10.0
_DONE = object()


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
    conversation = persistence.ensure_conversation(user_id, request.conversation_id, request.message)
    request = request.model_copy(
        update={
            "conversation_id": conversation.id,
            "conversation_context": persistence.conversation_context_text(user_id, conversation.id),
        }
    )

    def generate():
        turn_id: str | None = None
        stream_queue: Queue[StreamEnvelope | object] = Queue()

        def produce() -> None:
            try:
                for envelope in runtime.run_stream(request, user_id=user_id):
                    stream_queue.put(envelope)
            except BaseException as exc:  # pragma: no cover - defensive stream boundary.
                logger.exception("Agent v3 runtime stream failed")
                stream_queue.put(
                    StreamEnvelope(
                        type="error",
                        data={
                            "message": "Agent v3 failed while working on this turn.",
                            "detail": str(exc),
                        },
                    )
                )
            finally:
                stream_queue.put(_DONE)

        worker = Thread(target=produce, name="agent-v3-stream-producer", daemon=True)
        worker.start()
        last_heartbeat = time.monotonic()
        poll_seconds = min(1.0, max(0.01, AGENT_V3_SSE_HEARTBEAT_SECONDS / 2))
        while True:
            try:
                item = stream_queue.get(timeout=poll_seconds)
            except Empty:
                now = time.monotonic()
                if turn_id and now - last_heartbeat >= AGENT_V3_SSE_HEARTBEAT_SECONDS:
                    heartbeat = ProgressEvent(
                        turn_id=turn_id,
                        stage="keepalive",
                        message="Still working through the task.",
                        data={"ephemeral": True},
                    )
                    yield _sse(StreamEnvelope(type="progress", data=heartbeat.model_dump(mode="json")))
                    last_heartbeat = now
                if not worker.is_alive() and stream_queue.empty():
                    break
                continue

            if item is _DONE:
                break

            envelope = item
            try:
                if envelope.type == "start":
                    turn_id = str(envelope.data.get("turn_id") or "")
                    goal = Goal.model_validate(envelope.data.get("goal"))
                    persistence.create_turn(goal, turn_id)
                elif envelope.type == "progress":
                    progress_event = ProgressEvent.model_validate(envelope.data)
                    if not progress_event.data.get("ephemeral"):
                        persistence.append_event(progress_event)
                elif envelope.type == "result":
                    result = AgentV3Result.model_validate(envelope.data)
                    persistence.complete_turn(result)
                elif envelope.type == "error" and turn_id:
                    persistence.fail_turn(turn_id, str(envelope.data.get("message") or "Agent v3 failed"))
            except Exception as exc:
                logger.exception("Agent v3 stream persistence failed for envelope type=%s", envelope.type)
                if envelope.type == "result" and turn_id:
                    persistence.fail_turn(turn_id, f"Result persistence failed: {exc}")
                yield _sse(
                    StreamEnvelope(
                        type="error",
                        data={
                            "turn_id": turn_id,
                            "message": "Agent v3 could not save this turn cleanly.",
                            "detail": str(exc),
                        },
                    )
                )
                break

            yield _sse(envelope)
            last_heartbeat = time.monotonic()

        worker.join(timeout=1)

    return StreamingResponse(generate(), media_type="text/event-stream")


@router.get("/turns/{turn_id}")
def get_agent_v3_turn(turn_id: str, user_id: str = CurrentUser) -> dict:
    result = persistence.load_turn(turn_id, user_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Agent v3 turn not found")
    return result.model_dump(mode="json")


@router.get("/workspaces")
def list_agent_v3_workspaces(user_id: str = CurrentUser) -> dict:
    workspaces = persistence.list_workspaces(user_id)
    return {"workspaces": [workspace.model_dump(mode="json") for workspace in workspaces]}


@router.post("/workspaces")
def create_agent_v3_workspace(payload: AgentV3WorkspaceCreate, user_id: str = CurrentUser) -> dict:
    workspace = persistence.create_workspace(user_id, payload.name)
    return workspace.model_dump(mode="json")


@router.patch("/workspaces/{workspace_id}")
def update_agent_v3_workspace(
    workspace_id: str,
    payload: AgentV3WorkspaceUpdate,
    user_id: str = CurrentUser,
) -> dict:
    workspace = persistence.update_workspace(user_id, workspace_id, payload.name)
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return workspace.model_dump(mode="json")


@router.delete("/workspaces/{workspace_id}")
def delete_agent_v3_workspace(workspace_id: str, user_id: str = CurrentUser) -> dict:
    if not persistence.delete_workspace(user_id, workspace_id):
        raise HTTPException(status_code=404, detail="Workspace not found")
    return {"deleted": workspace_id}


@router.post("/workspaces/{workspace_id}/conversations")
def create_agent_v3_conversation(
    workspace_id: str,
    payload: AgentV3ConversationCreate,
    user_id: str = CurrentUser,
) -> dict:
    conversation = persistence.create_conversation(user_id, workspace_id, payload.title)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return conversation.model_dump(mode="json")


@router.delete("/conversations/{conversation_id}")
def delete_agent_v3_conversation(conversation_id: str, user_id: str = CurrentUser) -> dict:
    if not persistence.delete_conversation(user_id, conversation_id):
        raise HTTPException(status_code=404, detail="Conversation not found")
    return {"deleted": conversation_id}


@router.get("/conversations/{conversation_id}/turns")
def list_agent_v3_conversation_turns(
    conversation_id: str,
    limit: int = 20,
    before: str | None = None,
    user_id: str = CurrentUser,
) -> dict:
    turns = persistence.list_conversation_turns(user_id, conversation_id, limit=limit, before=before)
    return {"turns": [turn.model_dump(mode="json") for turn in turns]}


@router.get("/artifacts/{artifact_id}/download")
def download_agent_v3_artifact(artifact_id: str, user_id: str = CurrentUser) -> Response:
    artifact_payload = persistence.get_artifact_for_user(artifact_id, user_id)
    if artifact_payload is None:
        raise HTTPException(status_code=404, detail="Artifact not found")
    artifact, content = artifact_payload
    safe_filename = str(artifact.filename).replace('"', "")
    return Response(
        content=content,
        media_type=artifact.mime_type,
        headers={"Content-Disposition": f'attachment; filename="{safe_filename}"'},
    )
