import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import get_current_user_id, get_current_user_is_admin, get_current_user_payload
from app.db.models import (
    Base,
    Conversation,
    ConversationMessage,
    ResearchClaim,
    ResearchQuestion,
    ResearchRun,
    ResearchSource,
    TwinProfile,
)
from app.main import app
from app.routers import conversations, research_runs
from app.schemas import RouteDecision
from app.services.chat_pipeline import PipelineSetup
from app.services.llm_gateway import LLMResult
from app.services.research_orchestrator import ResearchPipelineResult
from app.services.planner import passthrough
from app.services.web_context import WebContextResult


def _events(body: str) -> list[tuple[str, dict]]:
    parsed = []
    for part in body.split("\n\n"):
        if not part.strip():
            continue
        event_type = "message"
        data = {}
        for line in part.splitlines():
            if line.startswith("event: "):
                event_type = line.removeprefix("event: ").strip()
            elif line.startswith("data: "):
                data = json.loads(line.removeprefix("data: "))
        parsed.append((event_type, data))
    return parsed


@pytest.fixture
def client(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)

    monkeypatch.setattr(conversations, "SessionLocal", Session)
    monkeypatch.setattr(research_runs, "SessionLocal", Session)
    monkeypatch.setattr(conversations.memory_writer, "schedule", lambda *args, **kwargs: None)
    monkeypatch.setattr(conversations.memory_extractor, "schedule", lambda *args, **kwargs: None)
    app.dependency_overrides[get_current_user_id] = lambda: "u1"
    app.dependency_overrides[get_current_user_payload] = lambda: {"sub": "u1"}
    app.dependency_overrides[get_current_user_is_admin] = lambda: False
    with TestClient(app) as c:
        yield c, Session
    app.dependency_overrides.clear()


def _patch_pipeline(monkeypatch, raw_answer: str):
    plan = passthrough("Explain the approach")
    route = RouteDecision(
        task_type="writing",
        complexity="medium",
        profile="balanced",
        primary_model="gpt-4.1-mini",
        fallbacks=[],
        reason="test",
    )
    wc = WebContextResult(context=None, status="Web context not requested.", provider="", sources_count=0, search_query=None)
    setup = PipelineSetup(
        plan=plan,
        route=route,
        wc=wc,
        enable_native=False,
        planner_ctx=None,
        running_summary="",
        profile="balanced",
    )
    monkeypatch.setattr(conversations, "build_pipeline_setup", lambda *args, **kwargs: setup)

    def fake_stream_llm(*args, **kwargs):
        yield raw_answer
        yield LLMResult(
            answer=raw_answer,
            model_used="gpt-4.1-mini",
            latency_ms=12,
            prompt_tokens=10,
            completion_tokens=20,
            estimated_cost_usd=0.001,
        )

    monkeypatch.setattr(conversations, "stream_llm", fake_stream_llm)


def test_stream_refinement_events_fire_with_profile(client, monkeypatch):
    c, Session = client
    raw_answer = " ".join(["This response has enough words to trigger refinement"] * 8)
    _patch_pipeline(monkeypatch, raw_answer)

    with Session() as db:
        db.add(TwinProfile(user_id="u1", rewrite_prompt="Rewrite this in my voice."))
        db.commit()

    def fake_refinement(*args, **kwargs):
        yield "Refined answer."
        yield LLMResult(
            answer="Refined answer.",
            model_used="claude-haiku-4-5-20251001",
            latency_ms=8,
            prompt_tokens=5,
            completion_tokens=5,
            estimated_cost_usd=0.0002,
        )

    monkeypatch.setattr(conversations, "stream_refinement", fake_refinement)

    response = c.post(
        "/conversations/chat/stream",
        json={"message": "Explain the approach", "output_mode": "client_ready"},
    )

    assert response.status_code == 200
    events = _events(response.text)
    event_names = [event for event, _ in events]
    assert "refine_start" in event_names
    assert ("refine_token", {"text": "Refined answer."}) in events
    assert events[-1][0] == "done"
    assert events[-1][1]["answer"] == "Refined answer."
    assert events[-1][1]["was_refined"] is True


def test_stream_starts_before_route_and_reports_pipeline_logs(client, monkeypatch):
    c, _Session = client
    raw_answer = "Short raw answer."
    _patch_pipeline(monkeypatch, raw_answer)

    response = c.post(
        "/conversations/chat/stream",
        json={"message": "Explain the approach", "output_mode": "raw"},
    )

    assert response.status_code == 200
    events = _events(response.text)
    assert events[0][0] == "start"
    assert "conversation_id" in events[0][1]
    assert "route" not in events[0][1]

    pipeline_logs = [data for event, data in events if event == "pipeline_log"]
    assert [log["stage"] for log in pipeline_logs[:3]] == ["planning", "routing", "working"]
    assert pipeline_logs[1]["route"]["task_type"] == "writing"

    assert events[-1][0] == "done"
    assert events[-1][1]["route"]["task_type"] == "writing"


def test_stream_research_mode_uses_research_pipeline(client, monkeypatch):
    c, _Session = client
    route = RouteDecision(
        task_type="research",
        complexity="high",
        profile="balanced",
        primary_model="gpt-4.1",
        fallbacks=[],
        reason="research test",
    )

    def fake_run_research(_db, **kwargs):
        kwargs["progress"]("searching", "Searching targeted sources…", {"query": kwargs["query"]})
        kwargs["progress"]("synthesising", "Synthesising 1 source and 1 claim…", {"source_count": 1, "claim_count": 1})
        return ResearchPipelineResult(
            run=SimpleNamespace(id=7, confidence="medium"),
            result=LLMResult(
                answer="Research answer [S1].",
                model_used="gpt-4.1",
                latency_ms=22,
                prompt_tokens=11,
                completion_tokens=12,
                estimated_cost_usd=0.002,
            ),
            route=route,
            source_logs=[{"title": "Source", "url": "https://example.com", "credibility_score": 0.8}],
            questions=["Question"],
            gaps=[],
            contradictions=[],
            verifier_notes=None,
        )

    plan = passthrough("Research this")
    monkeypatch.setattr(conversations, "run_planner", lambda *args, **kwargs: plan)
    monkeypatch.setattr(conversations, "run_research", fake_run_research)

    response = c.post(
        "/conversations/chat/stream",
        json={"message": "Research this", "research_mode": "deep", "output_mode": "raw"},
    )

    assert response.status_code == 200
    events = _events(response.text)
    logs = [data for event, data in events if event == "pipeline_log"]
    assert [log["stage"] for log in logs] == ["planning", "routing", "searching", "synthesising"]
    assert events[-1][0] == "done"
    assert events[-1][1]["research"]["run_id"] == 7
    assert events[-1][1]["research_run_id"] == 7
    assert events[-1][1]["route"]["task_type"] == "research"


def test_stream_research_mode_enriches_vague_followup_from_history(client, monkeypatch):
    c, Session = client
    with Session() as db:
        conv = Conversation(user_id="u1", title="Dishwasher", profile="balanced", message_count=2)
        db.add(conv)
        db.flush()
        db.add(ConversationMessage(
            conversation_id=conv.id,
            role="user",
            content="I need a quiet Bosch-style dishwasher under $1,000 for an open kitchen.",
        ))
        db.add(ConversationMessage(
            conversation_id=conv.id,
            role="assistant",
            content="We should compare quiet 24-inch dishwashers with good drying and reliability.",
        ))
        db.commit()
        conv_id = conv.id

    plan = passthrough("perform a deep research to find one suitable for me")
    plan.intent = "Find a suitable dishwasher for the user's open kitchen constraints."
    plan.context_summary = "User wants a quiet Bosch-style dishwasher under $1,000 for an open kitchen."
    plan.enriched_prompt = (
        "Perform deep research to recommend one quiet Bosch-style 24-inch dishwasher "
        "under $1,000 for an open kitchen, prioritizing low noise, drying quality, "
        "reliability, and current availability."
    )
    monkeypatch.setattr(conversations, "run_planner", lambda *args, **kwargs: plan)

    captured: dict = {}
    route = RouteDecision(
        task_type="research",
        complexity="high",
        profile="balanced",
        primary_model="gpt-4.1",
        fallbacks=[],
        reason="research test",
    )

    def fake_run_research(_db, **kwargs):
        captured["query"] = kwargs["query"]
        return ResearchPipelineResult(
            run=SimpleNamespace(id=8, confidence="medium"),
            result=LLMResult(
                answer="Recommended dishwasher [S1].",
                model_used="gpt-4.1",
                latency_ms=22,
                prompt_tokens=11,
                completion_tokens=12,
                estimated_cost_usd=0.002,
            ),
            route=route,
            source_logs=[],
            questions=[],
            gaps=[],
            contradictions=[],
            verifier_notes=None,
        )

    monkeypatch.setattr(conversations, "run_research", fake_run_research)

    response = c.post(
        "/conversations/chat/stream",
        json={
            "message": "perform a deep research to find one suitable for me",
            "conversation_id": conv_id,
            "research_mode": "deep",
            "output_mode": "raw",
        },
    )

    assert response.status_code == 200
    assert captured["query"] == plan.enriched_prompt
    events = _events(response.text)
    done = events[-1][1]
    assert done["execution_log"]["planner"]["enriched_prompt"] == plan.enriched_prompt


def test_new_conversation_title_strips_injected_user_context(client, monkeypatch):
    c, Session = client
    raw_answer = "Short raw answer."
    _patch_pipeline(monkeypatch, raw_answer)
    route = RouteDecision(
        task_type="writing",
        complexity="medium",
        profile="balanced",
        primary_model="gpt-4.1-mini",
        fallbacks=[],
        reason="test",
    )
    wc = WebContextResult(context=None, status="Web context not requested.", provider="", sources_count=0, search_query=None)

    def setup_from_actual_request(req, *_args, **_kwargs):
        plan = passthrough(req.message)
        return PipelineSetup(
            plan=plan,
            route=route,
            wc=wc,
            enable_native=False,
            planner_ctx=None,
            running_summary="",
            profile="balanced",
        )

    monkeypatch.setattr(conversations, "build_pipeline_setup", setup_from_actual_request)

    response = c.post(
        "/conversations/chat/stream",
        json={
            "message": "[Context: User: Subh | Domain: Enterprise architecture and AI]\n\nCompare quiet dishwashers under $1,000",
            "output_mode": "raw",
        },
    )

    assert response.status_code == 200
    with Session() as db:
        conv = db.query(Conversation).first()
        assert conv.title == "Compare quiet dishwashers under $1,000"


def test_new_conversation_title_uses_planner_intent(client, monkeypatch):
    c, Session = client
    raw_answer = "Short raw answer."
    _patch_pipeline(monkeypatch, raw_answer)
    original_setup = conversations.build_pipeline_setup

    def setup_with_intent(*args, **kwargs):
        setup = original_setup(*args, **kwargs)
        setup.plan.intent = "Compare quiet dishwashers under $1,000 for an open kitchen"
        return setup

    monkeypatch.setattr(conversations, "build_pipeline_setup", setup_with_intent)

    response = c.post(
        "/conversations/chat/stream",
        json={
            "message": "Can you compare these?",
            "output_mode": "raw",
        },
    )

    assert response.status_code == 200
    with Session() as db:
        conv = db.query(Conversation).first()
        assert conv.title == "Compare quiet dishwashers under $1,000 for an open kitchen"


def test_conversation_reload_rehydrates_research_metadata(client):
    c, Session = client
    with Session() as db:
        conv = Conversation(user_id="u1", title="Research", profile="balanced", message_count=2)
        db.add(conv)
        db.flush()
        run = ResearchRun(
            user_id="u1",
            conversation_id=conv.id,
            query="Research this",
            mode="deep",
            status="complete",
            confidence="medium",
            gaps_json='["Need pricing details"]',
            contradictions_json='["Docs conflict with marketing"]',
            verifier_notes='{"notes":"Checked citations","unsupported_claims":[],"citation_issues":[],"stale_or_overconfident_claims":[]}',
            final_answer="Research answer [S1].",
        )
        db.add(run)
        db.flush()
        question = ResearchQuestion(run_id=run.id, question="What is supported?", search_query="supported docs")
        db.add(question)
        db.flush()
        source = ResearchSource(
            run_id=run.id,
            question_id=question.id,
            title="Source",
            url="https://docs.example.com/source",
            provider="test",
            excerpt="Evidence",
            credibility_score=0.8,
            relevance_score=0.7,
            freshness_score=1.0,
            source_type="documentation",
        )
        db.add(source)
        db.flush()
        db.add(ResearchClaim(
            run_id=run.id,
            source_id=source.id,
            claim="Source supports the claim.",
            quote="Evidence",
            confidence="high",
            relevance_score=0.9,
        ))
        db.add(ConversationMessage(conversation_id=conv.id, role="user", content="Research this"))
        db.add(ConversationMessage(
            conversation_id=conv.id,
            role="assistant",
            content="Research answer [S1].",
            task_type="research",
            complexity="high",
            research_run_id=run.id,
        ))
        db.commit()
        conv_id = conv.id
        run_id = run.id

    detail = c.get(f"/conversations/{conv_id}")
    assert detail.status_code == 200
    messages = detail.json()["messages"]
    research_msg = messages[-1]
    assert research_msg["research_run_id"] == run_id
    assert research_msg["research"]["run_id"] == run_id
    assert research_msg["research"]["sources"][0]["title"] == "Source"
    assert research_msg["research"]["claims"][0]["source_ref"] == "S1"
    assert research_msg["research"]["gaps"] == ["Need pricing details"]

    run_resp = c.get(f"/research-runs/{run_id}")
    assert run_resp.status_code == 200
    assert run_resp.json()["claims"][0]["claim"] == "Source supports the claim."


def test_stream_refinement_skips_raw_mode(client, monkeypatch):
    c, Session = client
    raw_answer = " ".join(["This response has enough words to trigger refinement"] * 8)
    _patch_pipeline(monkeypatch, raw_answer)

    with Session() as db:
        db.add(TwinProfile(user_id="u1", rewrite_prompt="Rewrite this in my voice."))
        db.commit()

    response = c.post(
        "/conversations/chat/stream",
        json={"message": "Explain the approach", "output_mode": "raw"},
    )

    assert response.status_code == 200
    events = _events(response.text)
    event_names = [event for event, _ in events]
    assert "refine_start" not in event_names
    assert "refine_token" not in event_names
    assert events[-1][0] == "done"
    assert events[-1][1]["answer"] == raw_answer
    assert events[-1][1]["was_refined"] is False


def test_stream_refinement_skips_without_profile(client, monkeypatch):
    c, _Session = client
    raw_answer = " ".join(["This response has enough words to trigger refinement"] * 8)
    _patch_pipeline(monkeypatch, raw_answer)

    response = c.post(
        "/conversations/chat/stream",
        json={"message": "Explain the approach", "output_mode": "client_ready"},
    )

    assert response.status_code == 200
    events = _events(response.text)
    event_names = [event for event, _ in events]
    assert "refine_start" not in event_names
    assert "refine_token" not in event_names
    assert events[-1][1]["answer"] == raw_answer
    assert events[-1][1]["was_refined"] is False


def test_stream_refinement_failure_keeps_raw_answer(client, monkeypatch):
    c, Session = client
    raw_answer = " ".join(["This response has enough words to trigger refinement"] * 8)
    _patch_pipeline(monkeypatch, raw_answer)

    with Session() as db:
        db.add(TwinProfile(user_id="u1", rewrite_prompt="Rewrite this in my voice."))
        db.commit()

    def failing_refinement(*args, **kwargs):
        raise RuntimeError("refinement failed")
        yield ""

    monkeypatch.setattr(conversations, "stream_refinement", failing_refinement)

    response = c.post(
        "/conversations/chat/stream",
        json={"message": "Explain the approach", "output_mode": "client_ready"},
    )

    assert response.status_code == 200
    events = _events(response.text)
    event_names = [event for event, _ in events]
    assert "refine_start" in event_names
    assert "refine_token" not in event_names
    assert events[-1][1]["answer"] == raw_answer
    assert events[-1][1]["was_refined"] is False
