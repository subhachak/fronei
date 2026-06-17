import json
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import get_current_user_id
from app.db.models import AgentV3Artifact, AgentV3Event, AgentV3ToolCall, AgentV3Turn, Base
from app.main import app
from app.services.agent_v3.models import AgentV3Request, Source
from app.services.agent_v3.runtime import AgentV3Runtime
from app.services.agent_v3.tools import AgentV3Tools


class FakeTools(AgentV3Tools):
    def __init__(self):
        super().__init__(tavily_api_key="fake")

    def search_web(self, query: str, max_results: int = 6):
        sources = [Source(title="Example", url="https://example.com", snippet="A useful snippet")]
        from app.services.agent_v3.models import ToolCall

        return sources, ToolCall(name="web_search", input={"query": query}, output={"source_count": 1}, latency_ms=1)

    def extract_urls(self, urls: list[str], max_chars_per_source: int = 2500):
        extracted = [Source(title="Example", url="https://example.com", content="Detailed evidence")]
        from app.services.agent_v3.models import ToolCall

        return extracted, ToolCall(name="read_url", input={"urls": urls}, output={"source_count": 1}, latency_ms=1)


def _patch_completion(monkeypatch, text="# Answer\n\nDone."):
    from app.services.agent_v3 import model_client

    def fake_complete(messages, *, preferred_model=None, timeout_s=30, max_tokens=1200):
        user_payload = json.loads(messages[-1]["content"])
        lowered = user_payload["message"].lower()
        if "research" in lowered and ("report" in lowered or "docx" in lowered):
            route = "research_document"
        elif "research" in lowered:
            route = "research"
        elif "report" in lowered or "docx" in lowered:
            route = "document"
        else:
            route = "direct"
        return SimpleNamespace(
            text=json.dumps({"route": route, "confidence": 0.91, "reason": "test decision"}),
            model_used="fake-orchestrator",
            latency_ms=2,
            cost_usd=0.0,
        )

    def fake_simple_completion(system, user, *, max_tokens=1200):
        return SimpleNamespace(text=text, model_used="fake-model", latency_ms=3, cost_usd=0.0)

    monkeypatch.setattr(model_client, "complete", fake_complete)
    monkeypatch.setattr(model_client, "simple_completion", fake_simple_completion)


def _collect_stream(runtime: AgentV3Runtime, request: AgentV3Request):
    return list(runtime.run_stream(request, user_id="u1"))


def _sqlite_session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)


def test_agent_v3_direct_stream(monkeypatch):
    _patch_completion(monkeypatch, "Plain answer.")
    runtime = AgentV3Runtime(tools=FakeTools())

    envelopes = _collect_stream(runtime, AgentV3Request(message="Explain API rate limits."))

    assert [e.type for e in envelopes] == ["start", "progress", "progress", "result", "done"]
    result = envelopes[3].data
    assert result["route"] == "direct"
    assert result["answer"] == "Plain answer."
    assert envelopes[1].data["data"]["model_used"] == "fake-orchestrator"
    assert "available_routes" in envelopes[1].data["data"]
    assert "web_search" in envelopes[1].data["data"]["available_tools"]


def test_agent_v3_clarify_route(monkeypatch):
    from app.services.agent_v3 import model_client

    def fake_complete(messages, *, preferred_model=None, timeout_s=30, max_tokens=1200):
        return SimpleNamespace(
            text=json.dumps(
                {
                    "route": "clarify",
                    "confidence": 0.87,
                    "reason": "Needs a target.",
                    "clarification_question": "What should I research?",
                }
            ),
            model_used="fake-orchestrator",
            latency_ms=2,
            cost_usd=0.0,
        )

    monkeypatch.setattr(model_client, "complete", fake_complete)
    runtime = AgentV3Runtime(tools=FakeTools())

    envelopes = _collect_stream(runtime, AgentV3Request(message="Research it."))

    result = next(e.data for e in envelopes if e.type == "result")
    assert result["route"] == "clarify"
    assert result["answer"] == "What should I research?"
    assert not result["tool_calls"]


def test_agent_v3_orchestrator_falls_back_to_heuristic(monkeypatch):
    from app.services.agent_v3 import model_client

    def fail_complete(*args, **kwargs):
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(model_client, "complete", fail_complete)

    def fake_simple_completion(system, user, *, max_tokens=1200):
        return SimpleNamespace(text="Fallback answer.", model_used="fake-model", latency_ms=3, cost_usd=0.0)

    monkeypatch.setattr(model_client, "simple_completion", fake_simple_completion)
    runtime = AgentV3Runtime(tools=FakeTools())

    envelopes = _collect_stream(runtime, AgentV3Request(message="Research current AI governance trends."))

    progress = [e.data for e in envelopes if e.type == "progress"]
    assert progress[0]["data"]["source"] == "heuristic"
    result = next(e.data for e in envelopes if e.type == "result")
    assert result["route"] == "research"


def test_agent_v3_research_streams_milestones(monkeypatch):
    _patch_completion(monkeypatch, "Research answer [S1].")
    runtime = AgentV3Runtime(tools=FakeTools())

    envelopes = _collect_stream(runtime, AgentV3Request(message="Research current AI governance trends."))

    progress_messages = [e.data["message"] for e in envelopes if e.type == "progress"]
    assert "Searching the web with the fresh v3 tool runner." in progress_messages
    assert "Selected tool web_search." in progress_messages
    assert "Tool web_search completed." in progress_messages
    assert "Found 1 candidate sources." in progress_messages
    assert "Selected tool read_url." in progress_messages
    assert "Tool read_url completed." in progress_messages
    assert "Read 1 source pages." in progress_messages
    result = next(e.data for e in envelopes if e.type == "result")
    assert result["sources"][0]["url"] == "https://example.com"


def test_agent_v3_research_document_creates_artifact(monkeypatch):
    _patch_completion(monkeypatch, "## Report\n\n- Finding")
    runtime = AgentV3Runtime(tools=FakeTools())

    envelopes = _collect_stream(
        runtime,
        AgentV3Request(
            message="Research current AI governance trends and create a docx report.",
            output_format="docx",
        ),
    )

    result = next(e.data for e in envelopes if e.type == "result")
    assert result["route"] == "research_document"
    assert result["artifacts"][0]["filename"].endswith(".docx")
    assert result["artifacts"][0]["base64_data"]
    assert [call["name"] for call in result["tool_calls"]] == ["web_search", "read_url", "make_docx_artifact"]


def test_agent_v3_api_stream(monkeypatch):
    _patch_completion(monkeypatch, "API answer.")
    from app.services.agent_v3 import persistence

    Session = _sqlite_session()
    monkeypatch.setattr(persistence, "SessionLocal", Session)
    app.dependency_overrides[get_current_user_id] = lambda: "u1"
    try:
        with TestClient(app) as client:
            response = client.post("/agent-v3/turns/stream", json={"message": "Hello from v3"})
        assert response.status_code == 200
        assert "event: result" in response.text
        assert "API answer." in response.text
    finally:
        app.dependency_overrides.clear()


def test_agent_v3_stream_persists_turn_events_tools_and_artifacts(monkeypatch):
    _patch_completion(monkeypatch, "## Durable report\n\nDone.")
    from app.services.agent_v3 import persistence

    Session = _sqlite_session()
    monkeypatch.setattr(persistence, "SessionLocal", Session)
    app.dependency_overrides[get_current_user_id] = lambda: "u1"
    try:
        with TestClient(app) as client:
            response = client.post(
                "/agent-v3/turns/stream",
                json={
                    "message": "Research current AI governance trends and create a docx report.",
                    "output_format": "docx",
                },
            )
            assert response.status_code == 200
            result_frame = [chunk for chunk in response.text.split("\n\n") if chunk.startswith("event: result")][0]
            result_payload = json.loads(result_frame.split("data: ", 1)[1])
            turn_id = result_payload["turn_id"]

            stored = client.get(f"/agent-v3/turns/{turn_id}")
            assert stored.status_code == 200
            stored_payload = stored.json()
            assert stored_payload["turn_id"] == turn_id
            assert stored_payload["artifacts"][0]["filename"].endswith(".docx")

        with Session() as db:
            assert db.get(AgentV3Turn, turn_id).status == "completed"
            assert db.query(AgentV3Event).filter(AgentV3Event.turn_id == turn_id).count() >= 4
            assert db.query(AgentV3ToolCall).filter(AgentV3ToolCall.turn_id == turn_id).count() == 3
            assert db.query(AgentV3Artifact).filter(AgentV3Artifact.turn_id == turn_id).count() == 1
    finally:
        app.dependency_overrides.clear()


def test_agent_v3_runtime_source_does_not_import_legacy_pipelines():
    root = Path(__file__).resolve().parents[1] / "app" / "services" / "agent_v3"
    combined = "\n".join(path.read_text() for path in root.glob("*.py"))
    forbidden = [
        "chat_pipeline",
        "research_orchestrator",
        "turn_graph",
        "agent_runtime",
        "llm_gateway",
        "document_generator",
    ]
    for token in forbidden:
        assert token not in combined
