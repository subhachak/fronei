import json
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.auth import get_current_user_id
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

    def fake_simple_completion(system, user, *, max_tokens=1200):
        return SimpleNamespace(text=text, model_used="fake-model", latency_ms=3, cost_usd=0.0)

    monkeypatch.setattr(model_client, "simple_completion", fake_simple_completion)


def _collect_stream(runtime: AgentV3Runtime, request: AgentV3Request):
    return list(runtime.run_stream(request, user_id="u1"))


def test_agent_v3_direct_stream(monkeypatch):
    _patch_completion(monkeypatch, "Plain answer.")
    runtime = AgentV3Runtime(tools=FakeTools())

    envelopes = _collect_stream(runtime, AgentV3Request(message="Explain API rate limits."))

    assert [e.type for e in envelopes] == ["start", "progress", "progress", "result", "done"]
    result = envelopes[3].data
    assert result["route"] == "direct"
    assert result["answer"] == "Plain answer."


def test_agent_v3_research_streams_milestones(monkeypatch):
    _patch_completion(monkeypatch, "Research answer [S1].")
    runtime = AgentV3Runtime(tools=FakeTools())

    envelopes = _collect_stream(runtime, AgentV3Request(message="Research current AI governance trends."))

    progress_messages = [e.data["message"] for e in envelopes if e.type == "progress"]
    assert "Searching the web with the fresh v3 tool runner." in progress_messages
    assert "Found 1 candidate sources." in progress_messages
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


def test_agent_v3_api_stream(monkeypatch):
    _patch_completion(monkeypatch, "API answer.")
    app.dependency_overrides[get_current_user_id] = lambda: "u1"
    try:
        with TestClient(app) as client:
            response = client.post("/agent-v3/turns/stream", json={"message": "Hello from v3"})
        assert response.status_code == 200
        assert "event: result" in response.text
        assert "API answer." in response.text
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
