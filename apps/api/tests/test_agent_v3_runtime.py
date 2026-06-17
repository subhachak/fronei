import json
import time
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import get_current_user_id
from app.db.models import AgentV3Artifact, AgentV3Conversation, AgentV3Event, AgentV3ToolCall, AgentV3Turn, AgentV3Workspace, Base
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

        return sources, ToolCall(name="web_search", input={"query": query}, output={"provider": "FakeSearch", "source_count": 1}, latency_ms=1)

    def extract_urls(self, urls: list[str], max_chars_per_source: int = 2500):
        extracted = [Source(title="Example", url="https://example.com", content="Detailed evidence")]
        from app.services.agent_v3.models import ToolCall

        return extracted, ToolCall(name="read_url", input={"urls": urls}, output={"source_count": 1}, latency_ms=1)


def _patch_completion(monkeypatch, text="# Answer\n\nDone."):
    from app.services.agent_v3 import model_client

    def fake_complete(messages, *, preferred_model=None, timeout_s=30, max_tokens=1200):
        if "research lead" in messages[0]["content"].lower():
            user_payload = json.loads(messages[-1]["content"])
            message = user_payload["message"]
            return SimpleNamespace(
                text=json.dumps(
                    {
                        "questions": ["What is changing?", "What evidence supports it?"],
                        "search_queries": [message, f"{message} evidence"],
                        "max_sources": 5,
                        "min_evidence_items": 1,
                    }
                ),
                model_used="fake-research-planner",
                latency_ms=2,
                cost_usd=0.0,
            )
        if "document planner" in messages[0]["content"].lower():
            return SimpleNamespace(
                text=json.dumps(
                    {
                        "title": "Agent V3 Report",
                        "format": "docx",
                        "audience": "executives",
                        "sections": ["Executive summary", "Evidence", "Next steps"],
                    }
                ),
                model_used="fake-document-planner",
                latency_ms=2,
                cost_usd=0.0,
            )
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


def _set_artifact_dir(monkeypatch, tmp_path):
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "agent_v3_artifact_storage_dir", str(tmp_path / "agent_v3_artifacts"))


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
    assert "Planning focused research questions." in progress_messages
    assert "Research plan ready with 2 search worker(s)." in progress_messages
    assert "Search worker 1 running." in progress_messages
    assert "Search worker 1 used FakeSearch." in progress_messages
    assert "Search worker 2 running." in progress_messages
    assert "Search worker 2 used FakeSearch." in progress_messages
    assert "Selected tool web_search." in progress_messages
    assert "Tool web_search completed." in progress_messages
    assert "Selected 1 unique source candidate(s)." in progress_messages
    assert "Selected tool read_url." in progress_messages
    assert "Tool read_url completed." in progress_messages
    assert "Bound 1 evidence item(s)." in progress_messages
    assert "Synthesizing source-grounded answer from evidence." in progress_messages
    stages = [e.data["stage"] for e in envelopes if e.type == "progress"]
    for stage in [
        "research_planning",
        "research_plan",
        "search_worker",
        "search_worker_provider",
        "source_selection",
        "source_reader",
        "evidence_binder",
        "synthesis",
    ]:
        assert stage in stages
    result = next(e.data for e in envelopes if e.type == "result")
    assert result["sources"][0]["url"] == "https://example.com"
    provider_events = [e.data for e in envelopes if e.type == "progress" and e.data["stage"] == "search_worker_provider"]
    assert provider_events[0]["data"]["provider"] == "FakeSearch"


def test_agent_v3_research_registry_exposes_agent_team():
    from app.services.agent_v3.research_subtree import get_research_registry

    registry = get_research_registry()

    assert set(registry.agents) == {
        "research_lead",
        "search_worker",
        "source_ranker",
        "source_reader",
        "deep_link_agent",
        "evidence_binder",
        "gap_agent",
        "synthesis_agent",
        "research_judge",
        "claim_verifier",
        "repair_agent",
    }
    assert registry.agent("search_worker").allowed_tools == ["web_search"]
    assert registry.agent("source_reader").allowed_tools == ["read_url"]
    assert registry.prompt_for("research_lead").id == "research.lead.v1"


def test_agent_v3_research_public_url_guardrail_filters_private_sources():
    from app.services.agent_v3.research_subtree import is_public_source_url

    assert is_public_source_url("https://example.com/report")
    assert not is_public_source_url("http://127.0.0.1/private")
    assert not is_public_source_url("http://10.0.0.4/private")
    assert not is_public_source_url("file:///tmp/secret")


def test_agent_v3_research_budget_ledger_stops_tools_but_allows_synthesis():
    from app.services.agent_v3.research_subtree import ResearchBudget, ResearchBudgetLedger

    ledger = ResearchBudgetLedger(
        budget=ResearchBudget(
            max_tool_calls=1,
            max_model_calls=2,
            max_cost_usd=1.0,
            max_elapsed_ms=10_000,
        )
    )

    ledger.record_tool_call(latency_ms=10, sources_seen=3)

    assert ledger.stopped
    assert ledger.stop_reason == "tool budget exhausted"
    assert not ledger.can_start_tool("web_search")
    assert ledger.can_start_model("synthesis_agent")


def test_agent_v3_research_emits_agentic_goal_guardrail_and_judge_events(monkeypatch):
    _patch_completion(monkeypatch, "Research answer [S1].")
    runtime = AgentV3Runtime(tools=FakeTools())

    envelopes = _collect_stream(runtime, AgentV3Request(message="Research current AI governance trends."))

    progress = [e.data for e in envelopes if e.type == "progress"]
    by_stage = {event["stage"]: event for event in progress}
    assert by_stage["research_registry"]["data"]["agent_count"] == 11
    assert "judge_before_publish" in by_stage["research_goal"]["data"]["goal"]["guardrails"]
    assert by_stage["research_planning"]["data"]["agent_id"] == "research_lead"
    assert by_stage["source_ranker"]["data"]["agent_id"] == "source_ranker"
    assert by_stage["deep_link_agent"]["data"]["agent_id"] == "deep_link_agent"
    assert by_stage["evidence_binder"]["data"]["agent_id"] == "evidence_binder"
    assert by_stage["claim_verifier"]["data"]["agent_id"] == "claim_verifier"
    assert by_stage["research_judge_result"]["data"]["status"] in {"pass", "repair", "fail"}
    assert "score" in by_stage["research_judge_result"]["data"]
    assert by_stage["research_budget"]["data"]["budget_ledger"]["model_calls"] >= 2
    assert "max_tool_calls" in by_stage["research_goal"]["data"]["goal"]["budget"]


def test_agent_v3_research_source_ranking_and_deep_link_helpers():
    from app.services.agent_v3.research_subtree import (
        ResearchPlan,
        SearchWorkerPlan,
        classify_source_type,
        extract_deep_link_candidates,
        rank_sources,
    )

    plan = ResearchPlan(
        questions=["What is the official policy?"],
        workers=[SearchWorkerPlan(question="What is the official policy?", query="official policy")],
    )
    sources = [
        Source(title="Blog", url="https://example.com/post", snippet="Opinion"),
        Source(title="Government PDF", url="https://agency.gov/report.pdf", snippet="Official policy"),
    ]

    ranked = rank_sources(sources, plan)

    assert ranked[0].source.url == "https://agency.gov/report.pdf"
    assert classify_source_type("https://agency.gov/report.pdf") == "pdf"
    links = extract_deep_link_candidates(
        [
            Source(
                title="Parent",
                url="https://example.com/a",
                content="See [source](https://example.com/b) and https://127.0.0.1/secret",
            )
        ],
        max_links=3,
    )
    assert [link.url for link in links] == ["https://example.com/b"]


def test_agent_v3_research_repair_loop_runs_when_judge_requests_repair(monkeypatch):
    from app.services.agent_v3 import model_client

    def fake_complete(messages, *, preferred_model=None, timeout_s=30, max_tokens=1200):
        if "research lead" in messages[0]["content"].lower():
            user_payload = json.loads(messages[-1]["content"])
            message = user_payload["message"]
            return SimpleNamespace(
                text=json.dumps(
                    {
                        "questions": ["What changed?"],
                        "workers": [
                            {
                                "question": "What changed?",
                                "query": message,
                                "rationale": "Need current evidence.",
                                "max_results": 3,
                            }
                        ],
                        "max_sources": 3,
                        "min_evidence_items": 1,
                        "judge_threshold": 0.72,
                        "repair_iterations": 1,
                    }
                ),
                model_used="fake-research-planner",
                latency_ms=2,
                cost_usd=0.0,
            )
        return SimpleNamespace(
            text=json.dumps({"route": "research", "confidence": 0.91, "reason": "test decision"}),
            model_used="fake-orchestrator",
            latency_ms=2,
            cost_usd=0.0,
        )

    responses = iter(
        [
            "This is a long answer with useful structure but no citation marker. It explains the research finding in enough detail to avoid the short-answer penalty.",
            "This is a repaired answer with useful structure and a clear citation marker [S1]. It explains the research finding in enough detail.",
        ]
    )

    def fake_simple_completion(system, user, *, max_tokens=1200):
        return SimpleNamespace(text=next(responses), model_used="fake-model", latency_ms=3, cost_usd=0.0)

    monkeypatch.setattr(model_client, "complete", fake_complete)
    monkeypatch.setattr(model_client, "simple_completion", fake_simple_completion)
    runtime = AgentV3Runtime(tools=FakeTools())

    envelopes = _collect_stream(runtime, AgentV3Request(message="Research current AI governance trends."))

    stages = [e.data["stage"] for e in envelopes if e.type == "progress"]
    assert "research_repair" in stages
    assert "research_repair_result" in stages
    result = next(e.data for e in envelopes if e.type == "result")
    assert "repaired answer" in result["answer"]


def test_agent_v3_web_search_prefers_tavily_provider(monkeypatch):
    import app.services.agent_v3.tools as tools_module

    get_calls: list = []
    post_calls: list = []

    class TavilyResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "results": [
                    {
                        "title": "Tavily result",
                        "url": "https://tavily.example/result",
                        "content": "Tavily snippet",
                    }
                ]
            }

    def fake_get(*args, **kwargs):
        get_calls.append((args, kwargs))
        raise AssertionError("You.com should not be called when Tavily succeeds")

    def fake_post(*args, **kwargs):
        post_calls.append((args, kwargs))
        return TavilyResponse()

    monkeypatch.setattr(tools_module.httpx, "get", fake_get)
    monkeypatch.setattr(tools_module.httpx, "post", fake_post)

    sources, call = AgentV3Tools(you_api_key="you-key", tavily_api_key="tavily-key").search_web("query")

    assert call.ok
    assert call.output["provider"] == "Tavily"
    assert sources[0].url == "https://tavily.example/result"
    assert post_calls[0][0][0] == "https://api.tavily.com/search"
    assert post_calls[0][1]["json"]["query"] == "query"
    assert get_calls == []


def test_agent_v3_web_search_falls_back_to_you_provider(monkeypatch):
    import app.services.agent_v3.tools as tools_module

    get_calls: list = []
    post_calls: list = []

    class EmptyTavilyResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"results": []}

    class YouResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "results": {
                    "web": [
                        {
                            "title": "You result",
                            "url": "https://you.example/result",
                            "snippets": ["You.com snippet"],
                        }
                    ]
                }
            }

    def fake_post(*args, **kwargs):
        post_calls.append((args, kwargs))
        return EmptyTavilyResponse()

    def fake_get(*args, **kwargs):
        get_calls.append((args, kwargs))
        return YouResponse()

    monkeypatch.setattr(tools_module.httpx, "post", fake_post)
    monkeypatch.setattr(tools_module.httpx, "get", fake_get)

    sources, call = AgentV3Tools(you_api_key="you-key", tavily_api_key="tavily-key").search_web("query")

    assert call.ok
    assert call.output["provider"] == "You.com"
    assert sources[0].url == "https://you.example/result"
    assert post_calls[0][0][0] == "https://api.tavily.com/search"
    assert get_calls[0][0][0] == "https://ydc-index.io/v1/search"


def test_agent_v3_web_search_falls_back_to_nimble(monkeypatch):
    import app.services.agent_v3.tools as tools_module

    post_calls: list[dict] = []

    def fake_get(*args, **kwargs):
        class EmptyYouResponse:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return {"results": {"web": []}}

        return EmptyYouResponse()

    def fake_post(*args, **kwargs):
        post_calls.append(kwargs)

        class NimbleResponse:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "results": [
                        {
                            "title": "Nimble result",
                            "url": "https://nimble.example/result",
                            "description": "Nimble snippet",
                        }
                    ]
                }

        return NimbleResponse()

    monkeypatch.setattr(tools_module.httpx, "get", fake_get)
    monkeypatch.setattr(tools_module.httpx, "post", fake_post)

    sources, call = AgentV3Tools(you_api_key="you-key", nimble_api_key="nimble-key").search_web("query")

    assert call.ok
    assert call.output["provider"] == "Nimble"
    assert sources[0].url == "https://nimble.example/result"
    assert post_calls[0]["json"]["search_depth"] == "lite"
    assert post_calls[0]["json"]["focus"] == "general"


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
    progress_stages = [e.data["stage"] for e in envelopes if e.type == "progress"]
    for stage in [
        "document_planner",
        "document_plan",
        "document_writer",
        "document_judge",
        "document_judge_result",
        "document_repair",
        "artifact_builder",
        "artifact_result",
    ]:
        assert stage in progress_stages
    plan_event = next(e.data for e in envelopes if e.type == "progress" and e.data["stage"] == "document_plan")
    assert plan_event["data"]["title"] == "Agent V3 Report"
    assert [call["name"] for call in result["tool_calls"]] == [
        "web_search",
        "web_search",
        "read_url",
        "make_docx_artifact",
    ]


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


def test_agent_v3_background_turn_persists_and_polls_status(monkeypatch):
    _patch_completion(monkeypatch, "Background answer.")
    from app.services.agent_v3 import persistence

    Session = _sqlite_session()
    monkeypatch.setattr(persistence, "SessionLocal", Session)
    app.dependency_overrides[get_current_user_id] = lambda: "u1"
    try:
        with TestClient(app) as client:
            started = client.post("/agent-v3/turns", json={"message": "Hello from background v3"})
            assert started.status_code == 200
            turn_id = started.json()["turn_id"]
            deadline = time.time() + 5
            status_payload = None
            while time.time() < deadline:
                status = client.get(f"/agent-v3/turns/{turn_id}/status")
                assert status.status_code == 200
                status_payload = status.json()
                if status_payload["status"] == "completed":
                    break
                time.sleep(0.05)

        assert status_payload is not None
        assert status_payload["status"] == "completed"
        assert status_payload["turn"]["answer"] == "Background answer."
        assert any(event["stage"] == "background_job" for event in status_payload["turn"]["events"])

        with Session() as db:
            row = db.get(AgentV3Turn, turn_id)
            assert row is not None
            assert row.status == "completed"
    finally:
        app.dependency_overrides.clear()


def test_agent_v3_api_stream_emits_keepalive_during_quiet_work(monkeypatch):
    from app.routers import agent_v3 as agent_v3_router
    from app.services.agent_v3 import persistence
    from app.services.agent_v3.models import Goal, StreamEnvelope

    class SlowRuntime:
        def run_stream(self, request, *, user_id: str):
            goal = Goal(user_id=user_id, conversation_id=request.conversation_id, objective=request.message, route="direct")
            yield StreamEnvelope(type="start", data={"turn_id": "turn_slow", "goal": goal.model_dump(mode="json")})
            time.sleep(0.04)
            yield StreamEnvelope(
                type="result",
                data={
                    "turn_id": "turn_slow",
                    "goal": goal.model_dump(mode="json"),
                    "answer": "done",
                    "route": "direct",
                    "model_used": "fake",
                },
            )
            yield StreamEnvelope(type="done", data={"turn_id": "turn_slow"})

    Session = _sqlite_session()
    monkeypatch.setattr(persistence, "SessionLocal", Session)
    monkeypatch.setattr(agent_v3_router, "AgentV3Runtime", lambda: SlowRuntime())
    monkeypatch.setattr(agent_v3_router, "AGENT_V3_SSE_HEARTBEAT_SECONDS", 0.01)
    app.dependency_overrides[get_current_user_id] = lambda: "u1"
    try:
        with TestClient(app) as client:
            response = client.post("/agent-v3/turns/stream", json={"message": "Hello from v3"})
        assert response.status_code == 200
        assert '"stage": "keepalive"' in response.text
        assert "event: result" in response.text
    finally:
        app.dependency_overrides.clear()


def test_agent_v3_stream_persists_turn_events_tools_and_artifacts(monkeypatch, tmp_path):
    _patch_completion(monkeypatch, "## Durable report\n\nDone.")
    from app.services.agent_v3 import persistence

    _set_artifact_dir(monkeypatch, tmp_path)
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
            assert stored_payload["artifacts"][0]["download_url"].endswith("/download")

            download = client.get(stored_payload["artifacts"][0]["download_url"])
            assert download.status_code == 200
            assert download.content

        with Session() as db:
            assert db.get(AgentV3Turn, turn_id).status == "completed"
            assert db.query(AgentV3Event).filter(AgentV3Event.turn_id == turn_id).count() >= 4
            tool_names = [
                row.name
                for row in db.query(AgentV3ToolCall)
                .filter(AgentV3ToolCall.turn_id == turn_id)
                .order_by(AgentV3ToolCall.created_at.asc())
                .all()
            ]
            assert tool_names.count("web_search") >= 2
            assert "read_url" in tool_names
            assert tool_names[-1] == "make_docx_artifact"
            artifact = db.query(AgentV3Artifact).filter(AgentV3Artifact.turn_id == turn_id).one()
            assert artifact.base64_data == ""
            assert artifact.storage_path
            assert Path(artifact.storage_path).exists()
    finally:
        app.dependency_overrides.clear()


def test_agent_v3_workspace_api_is_user_isolated(monkeypatch):
    from app.services.agent_v3 import persistence

    Session = _sqlite_session()
    monkeypatch.setattr(persistence, "SessionLocal", Session)
    app.dependency_overrides[get_current_user_id] = lambda: "u1"
    try:
        with TestClient(app) as client:
            created = client.post("/agent-v3/workspaces", json={"name": "U1 workspace"})
            assert created.status_code == 200
            workspace_id = created.json()["id"]
            duplicate = client.post("/agent-v3/workspaces", json={"name": "U1 workspace"})
            assert duplicate.status_code == 200
            assert duplicate.json()["name"] == "U1 workspace 2"
            renamed = client.patch(f"/agent-v3/workspaces/{duplicate.json()['id']}", json={"name": "U1 workspace"})
            assert renamed.status_code == 200
            assert renamed.json()["name"] == "U1 workspace 2"
            conversation = client.post(f"/agent-v3/workspaces/{workspace_id}/conversations", json={"title": "Private work"})
            assert conversation.status_code == 200

            u1_list = client.get("/agent-v3/workspaces")
            assert "U1 workspace" in json.dumps(u1_list.json())

            app.dependency_overrides[get_current_user_id] = lambda: "u2"
            u2_list = client.get("/agent-v3/workspaces")
            assert u2_list.status_code == 200
            assert "U1 workspace" not in json.dumps(u2_list.json())

        with Session() as db:
            assert db.query(AgentV3Workspace).filter(AgentV3Workspace.user_id == "u1").count() == 2
            assert db.query(AgentV3Conversation).filter(AgentV3Conversation.user_id == "u1").count() == 1
            assert db.query(AgentV3Workspace).filter(AgentV3Workspace.user_id == "u2").count() == 1
    finally:
        app.dependency_overrides.clear()


def test_agent_v3_conversation_turns_are_conversation_scoped(monkeypatch, tmp_path):
    _patch_completion(monkeypatch, "Stored answer.")
    from app.services.agent_v3 import persistence

    _set_artifact_dir(monkeypatch, tmp_path)
    Session = _sqlite_session()
    monkeypatch.setattr(persistence, "SessionLocal", Session)
    app.dependency_overrides[get_current_user_id] = lambda: "u1"
    try:
        with TestClient(app) as client:
            workspace = client.post("/agent-v3/workspaces", json={"name": "Workspace"}).json()
            conversation = client.post(
                f"/agent-v3/workspaces/{workspace['id']}/conversations",
                json={"title": "Scoped conversation"},
            ).json()
            response = client.post(
                "/agent-v3/turns/stream",
                json={"message": "Hello scoped v3", "conversation_id": conversation["id"]},
            )
            assert response.status_code == 200
            turns = client.get(f"/agent-v3/conversations/{conversation['id']}/turns?limit=6")
            assert turns.status_code == 200
            payload = turns.json()
            assert len(payload["turns"]) == 1
            assert payload["turns"][0]["goal"]["conversation_id"] == conversation["id"]

            app.dependency_overrides[get_current_user_id] = lambda: "u2"
            denied = client.get(f"/agent-v3/conversations/{conversation['id']}/turns?limit=6")
            assert denied.status_code == 200
            assert denied.json()["turns"] == []
    finally:
        app.dependency_overrides.clear()


def test_agent_v3_conversation_context_connects_followup_turns(monkeypatch, tmp_path):
    from app.services.agent_v3 import model_client, persistence

    _set_artifact_dir(monkeypatch, tmp_path)
    Session = _sqlite_session()
    monkeypatch.setattr(persistence, "SessionLocal", Session)
    captured_prompts: list[str] = []

    def fake_complete(messages, *, preferred_model=None, timeout_s=30, max_tokens=1200):
        return SimpleNamespace(
            text=json.dumps({"route": "direct", "confidence": 0.9, "reason": "direct test"}),
            model_used="fake-orchestrator",
            latency_ms=1,
            cost_usd=0.0,
        )

    answers = iter(["First answer about gateway limits.", "Follow-up answer using prior context."])

    def fake_simple_completion(system, user, *, max_tokens=1200):
        captured_prompts.append(user)
        return SimpleNamespace(text=next(answers), model_used="fake-model", latency_ms=1, cost_usd=0.0)

    monkeypatch.setattr(model_client, "complete", fake_complete)
    monkeypatch.setattr(model_client, "simple_completion", fake_simple_completion)
    app.dependency_overrides[get_current_user_id] = lambda: "u1"
    try:
        with TestClient(app) as client:
            workspace = client.post("/agent-v3/workspaces", json={"name": "Context workspace"}).json()
            conversation = client.post(
                f"/agent-v3/workspaces/{workspace['id']}/conversations",
                json={"title": "Rate limit thread"},
            ).json()
            first = client.post(
                "/agent-v3/turns/stream",
                json={"message": "Explain API gateway rate limiting.", "conversation_id": conversation["id"]},
            )
            assert first.status_code == 200
            persistence.wait_for_context_updates()
            second = client.post(
                "/agent-v3/turns/stream",
                json={"message": "Make that shorter.", "conversation_id": conversation["id"]},
            )
            assert second.status_code == 200
            persistence.wait_for_context_updates()

        assert "First answer about gateway limits." in captured_prompts[-1]
        assert "Explain API gateway rate limiting." in captured_prompts[-1]
        with Session() as db:
            row = db.get(AgentV3Conversation, conversation["id"])
            context = json.loads(row.context_json)
            assert context["running_summary"]
            assert len(context["recent_turns"]) == 2
            assert len(persistence.conversation_context_text("u1", conversation["id"])) <= 6000
    finally:
        app.dependency_overrides.clear()


def test_agent_v3_workspace_context_is_shared_across_conversations(monkeypatch, tmp_path):
    from app.services.agent_v3 import model_client, persistence

    _set_artifact_dir(monkeypatch, tmp_path)
    Session = _sqlite_session()
    monkeypatch.setattr(persistence, "SessionLocal", Session)
    captured_prompts: list[str] = []

    def fake_complete(messages, *, preferred_model=None, timeout_s=30, max_tokens=1200):
        return SimpleNamespace(
            text=json.dumps({"route": "direct", "confidence": 0.9, "reason": "direct test"}),
            model_used="fake-orchestrator",
            latency_ms=1,
            cost_usd=0.0,
        )

    answers = iter(["Shared workspace context about platform modernization.", "Second conversation answer."])

    def fake_simple_completion(system, user, *, max_tokens=1200):
        captured_prompts.append(user)
        return SimpleNamespace(text=next(answers), model_used="fake-model", latency_ms=1, cost_usd=0.0)

    monkeypatch.setattr(model_client, "complete", fake_complete)
    monkeypatch.setattr(model_client, "simple_completion", fake_simple_completion)
    app.dependency_overrides[get_current_user_id] = lambda: "u1"
    try:
        with TestClient(app) as client:
            workspace = client.post("/agent-v3/workspaces", json={"name": "Shared workspace"}).json()
            first_conversation = client.post(
                f"/agent-v3/workspaces/{workspace['id']}/conversations",
                json={"title": "First thread"},
            ).json()
            second_conversation = client.post(
                f"/agent-v3/workspaces/{workspace['id']}/conversations",
                json={"title": "Second thread"},
            ).json()
            first = client.post(
                "/agent-v3/turns/stream",
                json={"message": "Capture platform modernization context.", "conversation_id": first_conversation["id"]},
            )
            assert first.status_code == 200
            persistence.wait_for_context_updates()
            second = client.post(
                "/agent-v3/turns/stream",
                json={"message": "Use what we know in this workspace.", "conversation_id": second_conversation["id"]},
            )
            assert second.status_code == 200
            persistence.wait_for_context_updates()

        assert "Workspace context:" in captured_prompts[-1]
        assert "Shared workspace context about platform modernization." in captured_prompts[-1]
        assert "Capture platform modernization context." in captured_prompts[-1]
        with Session() as db:
            row = db.get(AgentV3Workspace, workspace["id"])
            context = json.loads(row.context_json)
            assert context["running_summary"]
            assert len(context["recent_turns"]) == 2
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


def test_agent_v3_research_level_budgets_are_distinct():
    from app.services.agent_v3.research_subtree import research_budget_for

    easy = research_budget_for(AgentV3Request(message="Check the latest RBI repo rate.", research_level="easy"))
    regular = research_budget_for(AgentV3Request(message="Research RBI digital lending guidelines.", research_level="regular"))
    deep = research_budget_for(AgentV3Request(message="Do deep research on IPL business economics.", research_level="deep"))

    assert easy.max_search_workers == 1
    assert easy.max_sources == 1
    assert easy.max_deep_links == 0
    assert regular.max_search_workers > easy.max_search_workers
    assert regular.max_sources > easy.max_sources
    assert deep.max_tool_calls > regular.max_tool_calls
    assert deep.max_sources > regular.max_sources
    assert deep.repair_iterations > regular.repair_iterations


def test_agent_v3_deep_research_requires_confirmation(monkeypatch):
    from app.services.agent_v3 import model_client

    def fake_complete(messages, *, preferred_model=None, timeout_s=30, max_tokens=1200):
        return SimpleNamespace(
            text=json.dumps(
                {
                    "route": "research",
                    "confidence": 0.93,
                    "reason": "Broad high-stakes research.",
                    "research_level": "deep",
                    "requires_confirmation": True,
                    "confirmation_message": "Deep research will take longer. Continue?",
                }
            ),
            model_used="fake-orchestrator",
            latency_ms=1,
            cost_usd=0.0,
        )

    monkeypatch.setattr(model_client, "complete", fake_complete)
    runtime = AgentV3Runtime(tools=FakeTools())

    envelopes = _collect_stream(
        runtime,
        AgentV3Request(message="Do deep research on enterprise AI governance regulations."),
    )

    result = next(e.data for e in envelopes if e.type == "result")
    assert result["route"] == "clarify"
    assert result["answer"] == "Deep research will take longer. Continue?"
    assert result["research_plan_preview"]["research_level"] == "deep"
    assert result["research_plan_preview"]["investigate"]
    assert any(e.data.get("stage") == "research_plan_preview" for e in envelopes if e.type == "progress")
    labels = [option["label"] for option in result["follow_up_options"]]
    assert labels == ["Start research", "Use regular research", "Answer directly"]
    assert not any(e.data.get("stage") == "research_registry" for e in envelopes if e.type == "progress")


def test_agent_v3_confirmed_deep_research_runs_deep_budget(monkeypatch):
    _patch_completion(monkeypatch, "Deep research answer [S1].")
    runtime = AgentV3Runtime(tools=FakeTools())

    envelopes = _collect_stream(
        runtime,
        AgentV3Request(
            message="Do deep research on enterprise AI governance regulations.",
            force_route="research",
            research_level="deep",
            confirm_deep_research=True,
        ),
    )

    progress = [e.data for e in envelopes if e.type == "progress"]
    goal_event = next(event for event in progress if event["stage"] == "research_goal")
    assert goal_event["data"]["goal"]["research_level"] == "deep"
    assert goal_event["data"]["budget_ledger"]["budget"]["max_sources"] == 32
    assert goal_event["data"]["budget_ledger"]["budget"]["max_results_per_worker"] == 12
    result = next(e.data for e in envelopes if e.type == "result")
    assert result["route"] == "research"
    assert result["sources"]
