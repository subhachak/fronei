from __future__ import annotations

import json
import logging
import time
from queue import Empty, Queue
from threading import Thread

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import RedirectResponse, Response, StreamingResponse

from app.auth import CurrentActiveUser, CurrentUserIsAdmin
from app.config import get_settings
from app.services.agent import model_policy, persistence
from app.services.agent.job_worker import turn_job_worker
from app.services.agent.models import (
    ConversationCreate,
    Goal,
    ProgressEvent,
    StreamEnvelope,
    TurnRequest,
    WorkspaceCreate,
    WorkspaceUpdate,
    new_id,
)
from app.services.agent.runtime import Runtime

router = APIRouter(tags=["agent"])
logger = logging.getLogger(__name__)

SSE_HEARTBEAT_SECONDS = 10.0
TURN_STREAM_POLL_SECONDS = 0.5


def _sanitize_model_overrides(request: TurnRequest, *, is_admin: bool) -> TurnRequest:
    """Per-turn model overrides are an admin-only capability. Non-admins get
    the field dropped outright, regardless of what they sent; admins get it
    filtered down to known role keys with non-empty values, so a stray typo
    just doesn't apply rather than 422ing the whole turn."""
    if not request.model_overrides:
        return request
    if not is_admin:
        return request.model_copy(update={"model_overrides": None})
    cleaned = {
        model_policy.canonical_role(role): model.strip()
        for role, model in request.model_overrides.items()
        if model_policy.canonical_role(role) and isinstance(model, str) and model.strip()
    }
    return request.model_copy(update={"model_overrides": cleaned or None})


# Matches the extracted-text cap already enforced by document_extractor.py /
# /documents/extract. Re-capped here too since attachment_context arrives
# straight from the request body -- a client could send arbitrary text
# directly without ever calling /documents/extract, and this text gets
# prepended into every model call this turn (multiplied across roles on
# research/document turns), so it's worth bounding defensively.
ATTACHMENT_CONTEXT_MAX_CHARS = 60_000


def _build_conversation_context(user_id: str, conversation_id: str, request: TurnRequest) -> str:
    base_context = persistence.conversation_context_text(user_id, conversation_id, current_message=request.message)
    attachment = (request.attachment_context or "").strip()[:ATTACHMENT_CONTEXT_MAX_CHARS]
    if not attachment:
        return base_context
    attachment_block = f"Attached file context:\n{attachment}"
    return f"{base_context}\n\n{attachment_block}" if base_context else attachment_block


_DONE = object()


def _sse(envelope: StreamEnvelope) -> str:
    return f"event: {envelope.type}\ndata: {json.dumps(envelope.data, default=str)}\n\n"


def _turn_update_sse(event: str, data: dict, *, event_id: str | None = None) -> str:
    lines = []
    if event_id:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event}")
    lines.append(f"data: {json.dumps(data, default=str)}")
    return "\n".join(lines) + "\n\n"


@router.post("/turns")
def start_turn(
    request: TurnRequest,
    user_id: str = CurrentActiveUser,
    is_admin: bool = CurrentUserIsAdmin,
) -> dict:
    """Start an Fronei turn as a durable background job.

    The browser can poll /turns/{turn_id}/status for telemetry and
    completion. The run continues server-side if the browser connection drops.
    """
    request = _sanitize_model_overrides(request, is_admin=is_admin)
    conversation = persistence.ensure_conversation(user_id, request.conversation_id, request.message)
    request = request.model_copy(
        update={
            "conversation_id": conversation.id,
            "conversation_context": _build_conversation_context(user_id, conversation.id, request),
        }
    )
    turn_id = new_id("turn")
    placeholder_goal = Goal(
        user_id=user_id,
        conversation_id=conversation.id,
        objective=request.message,
        route=request.force_route or ("research_document" if request.output_format != "chat" else "research"),
        quality_mode=request.quality_mode,
    )
    settings = get_settings()
    persistence.enqueue_turn(
        placeholder_goal,
        turn_id,
        request,
        max_attempts=settings.turn_worker_max_attempts,
    )
    persistence.append_event(
        ProgressEvent(
            turn_id=turn_id,
            stage="background_job",
            message="Started a durable background run.",
            data={"conversation_id": conversation.id},
        )
    )
    turn_job_worker.notify()
    return {
        "turn_id": turn_id,
        "conversation_id": conversation.id,
        "status": "running",
    }


@router.post("/turns/stream")
def stream_turn(
    request: TurnRequest,
    user_id: str = CurrentActiveUser,
    is_admin: bool = CurrentUserIsAdmin,
) -> StreamingResponse:
    """Run a turn synchronously and stream progress envelopes to the client."""
    request = _sanitize_model_overrides(request, is_admin=is_admin)
    runtime = Runtime()
    conversation = persistence.ensure_conversation(user_id, request.conversation_id, request.message)
    request = request.model_copy(
        update={
            "conversation_id": conversation.id,
            "conversation_context": _build_conversation_context(user_id, conversation.id, request),
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
                logger.exception("Fronei runtime stream failed")
                stream_queue.put(
                    StreamEnvelope(
                        type="error",
                        data={
                            "message": "Fronei failed while working on this turn.",
                            "detail": str(exc),
                        },
                    )
                )
            finally:
                stream_queue.put(_DONE)

        worker = Thread(target=produce, name="turn-stream-producer", daemon=True)
        worker.start()
        last_heartbeat = time.monotonic()
        poll_seconds = min(1.0, max(0.01, SSE_HEARTBEAT_SECONDS / 2))
        while True:
            try:
                item = stream_queue.get(timeout=poll_seconds)
            except Empty:
                now = time.monotonic()
                if turn_id and now - last_heartbeat >= SSE_HEARTBEAT_SECONDS:
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
                persistence.persist_turn_envelope(envelope, turn_id)
            except Exception as exc:
                logger.exception("Fronei stream persistence failed for envelope type=%s", envelope.type)
                if envelope.type == "result" and turn_id:
                    persistence.fail_turn(turn_id, f"Result persistence failed: {exc}")
                yield _sse(
                    StreamEnvelope(
                        type="error",
                        data={
                            "turn_id": turn_id,
                            "message": "Fronei could not save this turn cleanly.",
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
def get_turn(turn_id: str, user_id: str = CurrentActiveUser) -> dict:
    result = persistence.load_turn(turn_id, user_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Fronei turn not found")
    return result.model_dump(mode="json")


@router.get("/turns/{turn_id}/status")
def get_turn_status(turn_id: str, user_id: str = CurrentActiveUser) -> dict:
    status = persistence.load_turn_status(turn_id, user_id)
    if status is None:
        raise HTTPException(status_code=404, detail="Fronei turn not found")
    return status


@router.get("/turns/{turn_id}/stream")
def stream_turn_updates(
    turn_id: str,
    after: str | None = None,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    user_id: str = CurrentActiveUser,
) -> StreamingResponse:
    if persistence.load_turn_state(turn_id, user_id) is None:
        raise HTTPException(status_code=404, detail="Fronei turn not found")

    def generate():
        cursor = last_event_id or after
        last_heartbeat = time.monotonic()
        while True:
            updates = persistence.load_turn_events_after(turn_id, user_id, cursor)
            if updates is None:
                return
            for update in updates:
                cursor = update.event_id
                yield _turn_update_sse(
                    "progress",
                    update.model_dump(mode="json"),
                    event_id=update.event_id,
                )

            state = persistence.load_turn_state(turn_id, user_id)
            if state is None:
                return
            if state["status"] in {"completed", "failed", "cancelled"}:
                terminal = persistence.load_turn_status(turn_id, user_id)
                if terminal is not None:
                    yield _turn_update_sse(
                        "turn",
                        terminal,
                        event_id=f"terminal:{turn_id}:{state['status']}",
                    )
                return

            now = time.monotonic()
            if now - last_heartbeat >= SSE_HEARTBEAT_SECONDS:
                yield ": keepalive\n\n"
                last_heartbeat = now
            time.sleep(TURN_STREAM_POLL_SECONDS)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/turns/{turn_id}/cancel")
def cancel_turn(turn_id: str, user_id: str = CurrentActiveUser) -> dict:
    if not persistence.request_turn_cancellation(turn_id, user_id):
        raise HTTPException(status_code=409, detail="Turn is not queued or running.")
    turn_job_worker.notify()
    return {"turn_id": turn_id, "status": "cancellation_requested"}


@router.get("/workspaces")
def list_workspaces_view(user_id: str = CurrentActiveUser) -> dict:
    workspaces = persistence.list_workspaces(user_id)
    return {"workspaces": [workspace.model_dump(mode="json") for workspace in workspaces]}


@router.post("/workspaces")
def create_workspace(payload: WorkspaceCreate, user_id: str = CurrentActiveUser) -> dict:
    workspace = persistence.create_workspace(user_id, payload.name)
    return workspace.model_dump(mode="json")


@router.patch("/workspaces/{workspace_id}")
def update_workspace(
    workspace_id: str,
    payload: WorkspaceUpdate,
    user_id: str = CurrentActiveUser,
) -> dict:
    workspace = persistence.update_workspace(user_id, workspace_id, payload.name)
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return workspace.model_dump(mode="json")


@router.delete("/workspaces/{workspace_id}")
def delete_workspace(workspace_id: str, user_id: str = CurrentActiveUser) -> dict:
    if not persistence.delete_workspace(user_id, workspace_id):
        raise HTTPException(status_code=404, detail="Workspace not found")
    return {"deleted": workspace_id}


@router.post("/workspaces/{workspace_id}/conversations")
def create_conversation(
    workspace_id: str,
    payload: ConversationCreate,
    user_id: str = CurrentActiveUser,
) -> dict:
    conversation = persistence.create_conversation(user_id, workspace_id, payload.title)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return conversation.model_dump(mode="json")


@router.delete("/conversations/{conversation_id}")
def delete_conversation(conversation_id: str, user_id: str = CurrentActiveUser) -> dict:
    if not persistence.delete_conversation(user_id, conversation_id):
        raise HTTPException(status_code=404, detail="Conversation not found")
    return {"deleted": conversation_id}


@router.get("/conversations/{conversation_id}/turns")
def list_conversation_turns(
    conversation_id: str,
    limit: int = 20,
    before: str | None = None,
    user_id: str = CurrentActiveUser,
) -> dict:
    turns = persistence.list_conversation_turns(user_id, conversation_id, limit=limit, before=before)
    return {"turns": [turn.model_dump(mode="json") for turn in turns]}


@router.get("/artifacts/{artifact_id}/download")
def download_artifact(artifact_id: str, user_id: str = CurrentActiveUser) -> Response:
    artifact_payload = persistence.get_artifact_for_user(artifact_id, user_id)
    if artifact_payload is None:
        raise HTTPException(status_code=404, detail="Artifact not found")
    artifact, content, signed_url = artifact_payload
    if signed_url:
        return RedirectResponse(signed_url, status_code=307)
    if content is None:
        raise HTTPException(status_code=404, detail="Artifact content is unavailable")
    safe_filename = str(artifact.filename).replace('"', "")
    return Response(
        content=content,
        media_type=artifact.mime_type,
        headers={"Content-Disposition": f'attachment; filename="{safe_filename}"'},
    )
