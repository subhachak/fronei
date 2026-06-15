import json
import threading
import time
from datetime import datetime, timedelta, timezone
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
    DocumentTemplate,
    ResearchClaim,
    ResearchQuestion,
    ResearchRun,
    ResearchSource,
    ConversationTurn,
    TwinProfile,
    UserProfile,
)
from app.main import app
from app.routers import conversations, research_runs
from app.schemas import RouteDecision
from app.services.chat_pipeline import PipelineSetup
from app.services.llm_gateway import LLMResult
from app.services.research_orchestrator import ResearchPipelineResult, ResearchFollowupResult
from app.services.planner import apply_confirmed_plan, passthrough, plan_to_dict
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


def _wait_for_completed_turn(Session, turn_id: str, timeout: float = 2.0) -> ConversationTurn:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with Session() as db:
            turn = db.query(ConversationTurn).filter(ConversationTurn.public_id == turn_id).first()
            if turn and turn.status == "completed":
                return turn
        time.sleep(0.02)
    with Session() as db:
        turn = db.query(ConversationTurn).filter(ConversationTurn.public_id == turn_id).first()
        assert turn is not None
        assert turn.status == "completed"
        return turn


def _latest_assistant_message(Session, conv_id: str) -> ConversationMessage:
    with Session() as db:
        conv = db.query(Conversation).filter(Conversation.public_id == conv_id).first()
        assert conv is not None
        msg = (
            db.query(ConversationMessage)
            .filter(ConversationMessage.conversation_id == conv.id, ConversationMessage.role == "assistant")
            .order_by(ConversationMessage.id.desc())
            .first()
        )
        assert msg is not None
        return msg


def test_durable_stream_worker_continues_after_client_iterator_closes():
    finished = threading.Event()
    emitted: list[str] = []

    def worker():
        emitted.append("started")
        yield conversations._sse("start", {"conversation_id": "abc"})
        time.sleep(0.02)
        emitted.append("finished")
        finished.set()
        yield conversations._sse("done", {"answer": "ok"})

    iterator = conversations._durable_event_iterator(worker)
    first = next(iterator)
    assert "event: start" in first

    iterator.close()

    assert finished.wait(timeout=1)
    assert emitted == ["started", "finished"]


def test_stream_creates_completed_turn_and_idempotent_replay(client, monkeypatch):
    c, Session = client
    _patch_pipeline(monkeypatch, "Durable answer.")

    response = c.post(
        "/conversations/chat/stream",
        json={
            "message": "Explain the durable path",
            "client_request_id": "client-req-1",
            "output_mode": "raw",
        },
    )

    assert response.status_code == 200
    events = _events(response.text)
    done = events[-1][1]

    with Session() as db:
        turns = db.query(ConversationTurn).all()
        assert len(turns) == 1
        turn = turns[0]
        assert turn.client_request_id == "client-req-1"
        assert turn.status == "completed"
        assert turn.assistant_message_id == done["message_id"]
        assert json.loads(turn.result_json or "{}")["message_id"] == done["message_id"]
        assert turn.completed_at is not None
        assert db.query(ConversationMessage).count() == 2

    replay = c.post(
        "/conversations/chat/stream",
        json={
            "message": "Explain the durable path",
            "client_request_id": "client-req-1",
            "output_mode": "raw",
        },
    )

    assert replay.status_code == 200
    replay_events = _events(replay.text)
    assert replay_events[-1][0] == "done"
    assert replay_events[-1][1]["message_id"] == done["message_id"]
    assert replay_events[-1][1]["answer"] == done["answer"]
    with Session() as db:
        assert db.query(ConversationTurn).count() == 1
        assert db.query(ConversationMessage).count() == 2


def test_get_conversation_includes_active_turn(client):
    c, Session = client
    with Session() as db:
        conv = Conversation(user_id="u1", title="Active", profile="balanced", message_count=1)
        db.add(conv)
        db.flush()
        db.add(ConversationMessage(conversation_id=conv.id, role="user", content="Work on this"))
        turn = ConversationTurn(
            user_id="u1",
            conversation_id=conv.id,
            status="running",
            progress_json=json.dumps([{"stage": "working", "message": "Still working", "ts": datetime.now(timezone.utc).isoformat()}]),
        )
        db.add(turn)
        db.commit()
        conv_id = conv.public_id
        turn_id = turn.public_id

    response = c.get(f"/conversations/{conv_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["active_turn"]["id"] == turn_id
    assert body["active_turn"]["status"] == "running"
    assert body["active_turn"]["progress"][0]["message"] == "Still working"


def test_cancel_conversation_turn_marks_turn_cancelled(client):
    c, Session = client
    with Session() as db:
        conv = Conversation(user_id="u1", title="Cancel", profile="balanced", message_count=1)
        db.add(conv)
        db.flush()
        turn = ConversationTurn(
            user_id="u1",
            conversation_id=conv.id,
            status="running",
            progress_json=json.dumps([{"stage": "working", "message": "Thinking"}]),
        )
        db.add(turn)
        db.commit()
        conv_id = conv.public_id
        turn_id = turn.public_id

    response = c.post(f"/conversations/{conv_id}/turns/{turn_id}/cancel")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "cancelled"
    assert body["error_message"] == "Cancelled by user."
    assert body["completed_at"] is not None

    with Session() as db:
        turn = db.query(ConversationTurn).filter(ConversationTurn.public_id == turn_id).one()
        assert turn.status == "cancelled"


def test_mark_stale_conversation_turns_marks_old_running_turns_failed(client):
    _c, Session = client
    old_time = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=2)
    with Session() as db:
        conv = Conversation(user_id="u1", title="Stale", profile="balanced", message_count=1)
        db.add(conv)
        db.flush()
        turn = ConversationTurn(
            user_id="u1",
            conversation_id=conv.id,
            status="running",
            created_at=old_time,
            updated_at=old_time,
        )
        db.add(turn)
        db.commit()
        turn_id = turn.id

    count = conversations.mark_stale_conversation_turns(timeout_minutes=30)

    assert count == 1
    with Session() as db:
        turn = db.get(ConversationTurn, turn_id)
        assert turn.status == "failed"
        assert "interrupted" in turn.error_message


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
    raw_answer = " ".join(["This response has enough words to trigger refinement"] * 18)
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
    assert any(event == "refine_token" and data["text"] == "Refined answer." for event, data in events)
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
    assert [log["stage"] for log in pipeline_logs[:4]] == ["planning", "planning", "routing", "working"]
    assert pipeline_logs[1]["message"].startswith(("Planner selected:", "Planner unavailable"))
    assert pipeline_logs[2]["route"]["task_type"] == "writing"

    assert events[-1][0] == "done"
    assert events[-1][1]["route"]["task_type"] == "writing"


def test_stream_research_mode_uses_research_pipeline(client, monkeypatch):
    c, Session = client
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
    assert [log["stage"] for log in logs] == ["working"]
    assert events[-1][0] == "job_started"

    turn = _wait_for_completed_turn(Session, events[-1][1]["turn_id"])
    done = json.loads(turn.result_json)
    assert done["research"]["run_id"] == 7
    assert done["research_run_id"] == 7
    assert done["route"]["task_type"] == "research"


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
        conv_id = conv.public_id

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
    events = _events(response.text)
    assert events[-1][0] == "job_started"
    turn = _wait_for_completed_turn(Session, events[-1][1]["turn_id"])
    assert captured["query"] == plan.enriched_prompt
    done = json.loads(turn.result_json)
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
        conv_id = conv.public_id
        run_id = run.id

    detail = c.get(f"/conversations/{conv_id}")
    assert detail.status_code == 200
    messages = detail.json()["messages"]
    research_msg = messages[-1]
    assert research_msg["research_run_id"] == run_id
    assert research_msg["research"]["run_id"] == run_id
    assert research_msg["research"]["sources"][0]["title"] == "Source"
    assert research_msg["research"]["claims"][0]["source_ref"] == "S1"


def test_execute_plan_with_research_and_document_confirmed_generates_document(client, monkeypatch):
    """Regression test for the bug where confirming web_search + deep_research +
    document together (via the plan_proposed popup) ran the research pipeline
    but never generated a document — it just streamed the research answer as
    chat text. The document branch must now run using the research findings."""
    c, Session = client

    plan = passthrough("Should we standardize on Bedrock or Snowflake Cortex?")
    plan.intent = "Compare Bedrock and Snowflake Cortex for a multi-region retail AI stack"
    plan.needs_web_search = True
    plan.recommend_deep_research = True
    plan.wants_document_output = False  # gets flipped on by confirmed_plan.document
    plan.document_brief = {"doc_type": "solution_comparison", "title": "Bedrock vs Snowflake Cortex"}
    plan.plan_confidence = "medium"

    with Session() as db:
        conv = Conversation(user_id="u1", title="AI stack", profile="balanced", message_count=0)
        db.add(conv)
        db.flush()
        user_msg = ConversationMessage(
            conversation_id=conv.id,
            role="user",
            content="Should we standardize our enterprise AI stack on Bedrock or Snowflake Cortex for a multi-region retail deployment?",
            plan_json=json.dumps(plan_to_dict(plan)),
        )
        db.add(user_msg)
        db.commit()
        conv_id = conv.public_id
        message_id = user_msg.id

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
        kwargs["progress"]("synthesising", "Synthesising 2 sources and 1 claim…", {"source_count": 2, "claim_count": 1})
        return ResearchPipelineResult(
            run=SimpleNamespace(id=42, confidence="medium"),
            result=LLMResult(
                answer="Bedrock and Snowflake Cortex both have trade-offs [S1][S2].",
                model_used="gpt-4.1",
                latency_ms=500,
                prompt_tokens=100,
                completion_tokens=200,
                estimated_cost_usd=0.02,
            ),
            route=route,
            source_logs=[
                {"title": "AWS Bedrock docs", "url": "https://aws.example.com/bedrock", "credibility_score": 0.9},
                {"title": "Snowflake Cortex docs", "url": "https://snowflake.example.com/cortex", "credibility_score": 0.85},
            ],
            questions=["Which platform fits a multi-region retail deployment?"],
            gaps=[],
            contradictions=[],
            verifier_notes=None,
        )

    monkeypatch.setattr(conversations, "run_research", fake_run_research)

    captured_doc_call: dict = {}

    def fake_generate_document_output(plan_arg, route_arg, history, wc, planner_ctx, doc_context, deep_research, enable_native, artifact_context="", user_memory="", db=None, **kwargs):
        captured_doc_call["doc_context"] = doc_context
        captured_doc_call["wants_document_output"] = plan_arg.wants_document_output
        doc_result = LLMResult(
            answer="# Bedrock vs Snowflake Cortex\n\nFull document body.\n\n---SUMMARY---\n- Overview\n- Recommendation",
            model_used="gpt-4.1",
            latency_ms=800,
            prompt_tokens=300,
            completion_tokens=400,
            estimated_cost_usd=0.05,
        )
        doc_body = "# Bedrock vs Snowflake Cortex\n\nFull document body."
        chat_summary = "- Overview\n- Recommendation"
        doc_type = "solution_comparison"
        return doc_result, doc_body, chat_summary, doc_type

    monkeypatch.setattr(conversations, "generate_document_output", fake_generate_document_output)

    response = c.post(
        f"/conversations/{conv_id}/messages/{message_id}/execute-plan",
        json={
            "confirmed_plan": {
                "web_search": True,
                "deep_research": True,
                "document": True,
                "document_format": "docx",
                "document_brief": {
                    "doc_type": "solution_comparison",
                    "title": "Bedrock vs Snowflake Cortex",
                },
            }
        },
    )

    assert response.status_code == 200
    events = _events(response.text)

    # Research/document turns now detach into a durable progressive job.
    logs = [data for event, data in events if event == "pipeline_log"]
    assert [log["stage"] for log in logs] == ["working"]
    assert events[-1][0] == "job_started"

    # Document branch ran and produced a preview.
    turn = _wait_for_completed_turn(Session, events[-1][1]["turn_id"])
    done = json.loads(turn.result_json)
    assert done["document_preview"] is not None
    assert done["document_preview"]["doc_type"] == "solution_comparison"
    assert done["answer"] == "- Overview\n- Recommendation"
    assert done["research_run_id"] == 42

    # The document generator was fed the research findings as context.
    assert captured_doc_call["wants_document_output"] is True
    assert "Bedrock and Snowflake Cortex both have trade-offs" in captured_doc_call["doc_context"]
    assert "AWS Bedrock docs" in captured_doc_call["doc_context"]


def test_execute_plan_research_followup_with_document_confirmed_generates_document(client, monkeypatch):
    """Regression test for confirmed document output being dropped by the fast
    research-followup path for continuation/correction/constraint_change turns.
    """
    c, Session = client

    plan = passthrough("Now make it CFO-ready and add a recommendation.")
    plan.turn_type = "constraint_change"
    plan.intent = "Revise the existing research into a CFO-ready recommendation."
    plan.enriched_prompt = "Use the previous research and make the recommendation CFO-ready."
    plan.needs_web_search = True
    plan.recommend_deep_research = True
    plan.wants_document_output = False  # confirmed_plan.document flips this on
    plan.document_brief = {"doc_type": "executive_report", "title": "CFO Recommendation"}
    plan.plan_confidence = "medium"

    with Session() as db:
        conv = Conversation(user_id="u1", title="Prior research", profile="balanced", message_count=2)
        db.add(conv)
        db.flush()
        db.add(ConversationMessage(
            conversation_id=conv.id,
            role="user",
            content="Research platform options.",
        ))
        db.add(ConversationMessage(
            conversation_id=conv.id,
            role="assistant",
            content="Prior research answer [S1].",
            task_type="research",
            complexity="high",
            research_run_id=77,
        ))
        user_msg = ConversationMessage(
            conversation_id=conv.id,
            role="user",
            content="Now make it CFO-ready and add a recommendation.",
            plan_json=json.dumps(plan_to_dict(plan)),
        )
        db.add(user_msg)
        db.commit()
        conv_id = conv.public_id
        message_id = user_msg.id

    route = RouteDecision(
        task_type="research",
        complexity="high",
        profile="balanced",
        primary_model="gpt-4.1",
        fallbacks=[],
        reason="followup test",
    )

    def fake_run_research_followup(_db, **kwargs):
        kwargs["progress"]("synthesising", "Synthesising from existing evidence…", {"source_count": 1})
        return ResearchFollowupResult(
            run=SimpleNamespace(id=77, confidence="medium"),
            result=LLMResult(
                answer="Updated CFO-ready findings from prior evidence [S1].",
                model_used="gpt-4.1",
                latency_ms=250,
                prompt_tokens=50,
                completion_tokens=80,
                estimated_cost_usd=0.01,
            ),
            route=route,
            source_logs=[{"title": "Existing evidence", "url": "https://example.com/evidence"}],
            questions=["What changed for the CFO audience?"],
        )

    monkeypatch.setattr(conversations, "run_research_followup", fake_run_research_followup)

    captured_doc_call: dict = {}

    def fake_generate_document_output(plan_arg, route_arg, history, wc, planner_ctx, doc_context, deep_research, enable_native, artifact_context="", user_memory="", db=None, **kwargs):
        captured_doc_call["doc_context"] = doc_context
        captured_doc_call["wants_document_output"] = plan_arg.wants_document_output
        return (
            LLMResult(
                answer="# CFO Recommendation\n\nFull document body.\n\n---SUMMARY---\n- CFO-ready recommendation",
                model_used="gpt-4.1",
                latency_ms=500,
                prompt_tokens=120,
                completion_tokens=180,
                estimated_cost_usd=0.03,
            ),
            "# CFO Recommendation\n\nFull document body.",
            "- CFO-ready recommendation",
            "executive_report",
        )

    monkeypatch.setattr(conversations, "generate_document_output", fake_generate_document_output)

    response = c.post(
        f"/conversations/{conv_id}/messages/{message_id}/execute-plan",
        json={
            "confirmed_plan": {
                "web_search": True,
                "deep_research": True,
                "document": True,
                "document_format": "docx",
            }
        },
    )

    assert response.status_code == 200
    events = _events(response.text)
    assert events[-1][0] == "job_started"
    turn = _wait_for_completed_turn(Session, events[-1][1]["turn_id"])
    done = json.loads(turn.result_json)

    assert done["research_run_id"] == 77
    assert done["document_preview"] is not None
    assert done["document_preview"]["doc_type"] == "executive_report"
    assert done["answer"] == "- CFO-ready recommendation"
    assert captured_doc_call["wants_document_output"] is True
    assert "Updated CFO-ready findings from prior evidence" in captured_doc_call["doc_context"]
    assert "Existing evidence" in captured_doc_call["doc_context"]


def test_document_generation_pauses_for_late_finalization(client, monkeypatch):
    c, Session = client

    plan = passthrough("Create a client presentation about the migration strategy.")
    plan.intent = "Create a client presentation about the migration strategy"
    plan.wants_document_output = True
    plan.document_brief = {
        "doc_type": "presentation",
        "title": "Migration Strategy",
        "audience": "Client steering committee",
    }
    plan.document_format_options = ["pptx", "docx", "markdown"]
    plan.document_format_recommendation = "pptx"
    plan.plan_confidence = "medium"

    route = RouteDecision(
        task_type="writing",
        complexity="high",
        profile="balanced",
        primary_model="gpt-4.1",
        fallbacks=[],
        reason="document finalization test",
    )
    wc = WebContextResult(context=None, status="Web context not requested.", provider="", sources_count=0, search_query=None)

    def fake_build_pipeline_setup(req, conv_arg, history, settings, **kwargs):
        return PipelineSetup(
            plan=plan,
            route=route,
            wc=wc,
            enable_native=False,
            planner_ctx=None,
            running_summary="",
            profile="balanced",
            doc_context="",
            artifact_context="",
        )

    monkeypatch.setattr(conversations, "build_pipeline_setup", fake_build_pipeline_setup)

    def fail_generate(*args, **kwargs):
        raise AssertionError("document generation should wait for finalization")

    monkeypatch.setattr(conversations, "generate_document_output", fail_generate)

    response = c.post(
        "/conversations/chat/stream",
        json={"message": "Create a client presentation about the migration strategy.", "document_requested": True},
    )

    assert response.status_code == 200
    events = _events(response.text)
    assert events[-1][0] == "document_brief_proposed"
    proposal = events[-1][1]
    assert proposal["brief"]["doc_type"] == "presentation"
    assert proposal["format_recommendation"] == "pptx"
    template_ids = [t["id"] for t in proposal["templates"]]
    assert "fronei-default" in template_ids
    assert "executive-navy" not in template_ids
    assert proposal["template_recommendation"] == "fronei-default"
    assert proposal["template_design"]["mode"] == "fronei_premium_freehand"
    assert proposal["template_design"]["available_slide_types"]

    with Session() as db:
        user_msg = db.get(ConversationMessage, proposal["message_id"])
        assert user_msg is not None
        assert user_msg.plan_json
        turn = db.query(ConversationTurn).order_by(ConversationTurn.id.desc()).first()
        assert turn.status == "awaiting_confirmation"


def test_presentation_finalization_defaults_to_pptx_even_without_planner_format_options(client, monkeypatch):
    c, _Session = client

    plan = passthrough("Create a presentation about the migration strategy.")
    plan.intent = "Create a presentation about the migration strategy"
    plan.wants_document_output = True
    plan.document_brief = {"doc_type": "presentation", "title": "Migration Strategy"}
    plan.document_format_options = []
    plan.document_format_recommendation = None
    plan.plan_confidence = "medium"

    route = RouteDecision(
        task_type="writing",
        complexity="high",
        profile="balanced",
        primary_model="gpt-4.1",
        fallbacks=[],
        reason="presentation format fallback test",
    )
    wc = WebContextResult(context=None, status="Web context not requested.", provider="", sources_count=0, search_query=None)

    def fake_build_pipeline_setup(req, conv_arg, history, settings, **kwargs):
        return PipelineSetup(
            plan=plan,
            route=route,
            wc=wc,
            enable_native=False,
            planner_ctx=None,
            running_summary="",
            profile="balanced",
            doc_context="",
            artifact_context="",
        )

    monkeypatch.setattr(conversations, "build_pipeline_setup", fake_build_pipeline_setup)

    response = c.post(
        "/conversations/chat/stream",
        json={"message": "Create a presentation about the migration strategy.", "document_requested": True},
    )

    assert response.status_code == 200
    events = _events(response.text)
    assert events[-1][0] == "document_brief_proposed"
    proposal = events[-1][1]
    assert proposal["brief"]["doc_type"] == "presentation"
    assert proposal["format_recommendation"] == "pptx"
    assert proposal["format_options"][0] == "pptx"


def test_planner_questions_are_asked_before_document_finalization(client, monkeypatch):
    c, _Session = client

    plan = passthrough("Create a presentation about the roadmap.")
    plan.intent = "Create a presentation about the roadmap"
    plan.wants_document_output = True
    plan.document_brief = {"doc_type": "presentation", "title": "Roadmap"}
    plan.document_format_options = ["pptx"]
    plan.document_format_recommendation = "pptx"
    plan.plan_confidence = "low"
    plan.open_questions = ["Which audience should this deck target?"]

    route = RouteDecision(
        task_type="writing",
        complexity="high",
        profile="balanced",
        primary_model="gpt-4.1",
        fallbacks=[],
        reason="clarification before document finalization test",
    )
    wc = WebContextResult(context=None, status="Web context not requested.", provider="", sources_count=0, search_query=None)

    def fake_build_pipeline_setup(req, conv_arg, history, settings, **kwargs):
        return PipelineSetup(
            plan=plan,
            route=route,
            wc=wc,
            enable_native=False,
            planner_ctx=None,
            running_summary="",
            profile="balanced",
            doc_context="",
            artifact_context="",
        )

    monkeypatch.setattr(conversations, "build_pipeline_setup", fake_build_pipeline_setup)

    response = c.post(
        "/conversations/chat/stream",
        json={"message": "Create a presentation about the roadmap.", "document_requested": True},
    )

    assert response.status_code == 200
    events = _events(response.text)
    assert events[-1][0] == "plan_proposed"
    proposal = events[-1][1]
    assert proposal["open_questions"] == ["Which audience should this deck target?"]
    assert proposal["capabilities"]["document"]["enabled"] is True


def test_high_confidence_presentation_skips_late_finalization_and_applies_defaults(client):
    _c, Session = client

    plan = passthrough("Create a presentation about Q3 platform modernization.")
    plan.intent = "Create a presentation about Q3 platform modernization"
    plan.wants_document_output = True
    plan.document_brief = {"doc_type": "presentation", "title": "Q3 Platform Modernization"}
    plan.document_format_options = []
    plan.document_format_recommendation = None
    plan.plan_confidence = "high"
    plan.open_questions = []

    with Session() as db:
        conv = Conversation(user_id="u1", title="Deck", profile="balanced", message_count=0)
        db.add(conv)
        db.flush()
        user_msg = ConversationMessage(
            conversation_id=conv.id,
            role="user",
            content="Create a presentation about Q3 platform modernization.",
        )
        db.add(user_msg)
        db.commit()

        gate = conversations.plan_gate.evaluate(plan)
        event = conversations._maybe_propose_document_finalization(
            db,
            "u1",
            conv,
            user_msg,
            conversations.ConvChatRequest(message="Create a presentation about Q3 platform modernization."),
            plan,
            gate,
        )

    assert event is None
    assert plan.document_format_recommendation == "pptx"
    assert plan.document_format_options[0] == "pptx"
    assert plan.document_brief["template_id"] == "fronei-default"
    assert plan.document_brief["theme"] == "dark"


def test_high_confidence_presentation_defaults_to_user_template_light_theme(client):
    _c, Session = client

    plan = passthrough("Using my Fronei brand template, build a 10-slide deck.")
    plan.intent = "Build a deck using the user's brand template"
    plan.wants_document_output = True
    plan.document_brief = {"doc_type": "presentation", "title": "Q3 Platform Modernization Review"}
    plan.document_format_options = []
    plan.document_format_recommendation = None
    plan.plan_confidence = "high"
    plan.open_questions = []

    with Session() as db:
        db.add(DocumentTemplate(
            public_id="tpl_brand",
            user_id="u1",
            name="Fronei Brand",
            doc_type="presentation",
            storage_key="missing-template.pptx",
            original_filename="brand.pptx",
            content_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
            file_size=123,
            is_active=True,
            updated_at=datetime.now(timezone.utc),
        ))
        conv = Conversation(user_id="u1", title="Deck", profile="balanced", message_count=0)
        db.add(conv)
        db.flush()
        user_msg = ConversationMessage(
            conversation_id=conv.id,
            role="user",
            content="Using my Fronei brand template, build a 10-slide deck.",
        )
        db.add(user_msg)
        db.commit()

        gate = conversations.plan_gate.evaluate(plan)
        event = conversations._maybe_propose_document_finalization(
            db,
            "u1",
            conv,
            user_msg,
            conversations.ConvChatRequest(message="Using my Fronei brand template, build a 10-slide deck."),
            plan,
            gate,
        )

    assert event is None
    assert plan.document_format_recommendation == "pptx"
    assert plan.document_brief["template_id"] == "tpl_brand"
    assert plan.document_brief["theme"] == "light"


def test_execute_plan_after_document_finalization_generates_artifact(client, monkeypatch):
    c, Session = client

    plan = passthrough("Create a client presentation about the migration strategy.")
    plan.intent = "Create a client presentation about the migration strategy"
    plan.wants_document_output = True
    plan.document_brief = {
        "doc_type": "presentation",
        "title": "Migration Strategy",
        "_source_context": "Research findings:\nUse phased migration to reduce delivery risk.",
    }
    plan.document_format_options = ["pptx", "markdown"]
    plan.document_format_recommendation = "pptx"
    plan.plan_confidence = "high"

    with Session() as db:
        conv = Conversation(user_id="u1", title="Deck", profile="balanced", message_count=0)
        db.add(conv)
        db.add(UserProfile(
            user_id="u1",
            profile_json=json.dumps({
                "preferred_tone": "direct",
                "preferred_slide_density": "sparse",
                "common_audiences": ["Client steering committee"],
                "communication_style": "Concise, decisive, and specific.",
                "key_preferences": ["Avoid generic slide copy"],
            }),
        ))
        db.flush()
        user_msg = ConversationMessage(
            conversation_id=conv.id,
            role="user",
            content="Create a client presentation about the migration strategy.",
            plan_json=json.dumps(plan_to_dict(plan)),
        )
        db.add(user_msg)
        db.commit()
        conv_id = conv.public_id
        message_id = user_msg.id

    route = RouteDecision(
        task_type="writing",
        complexity="high",
        profile="balanced",
        primary_model="gpt-4.1",
        fallbacks=[],
        reason="document finalization execute test",
    )
    wc = WebContextResult(context=None, status="Web context not requested.", provider="", sources_count=0, search_query=None)

    def fake_build_pipeline_setup(req, conv_arg, history, settings, **kwargs):
        setup_plan = kwargs["plan"]
        if kwargs.get("confirmed_plan"):
            apply_confirmed_plan(setup_plan, kwargs["confirmed_plan"])
        return PipelineSetup(
            plan=setup_plan,
            route=route,
            wc=wc,
            enable_native=False,
            planner_ctx=None,
            running_summary="",
            profile="balanced",
            doc_context="",
            artifact_context="",
        )

    monkeypatch.setattr(conversations, "build_pipeline_setup", fake_build_pipeline_setup)

    captured: dict = {}

    def fake_generate_document_output(plan_arg, route_arg, history, wc, planner_ctx, doc_context, deep_research, enable_native, artifact_context="", user_memory="", db=None, **kwargs):
        captured["brief"] = plan_arg.document_brief
        captured["doc_context"] = doc_context
        captured["artifact_context"] = artifact_context
        captured["brand_profile"] = kwargs.get("brand_profile")
        captured["user_document_profile"] = kwargs.get("user_document_profile")
        return (
            LLMResult(
                answer="# Migration Strategy\n\nBody.\n\n---SUMMARY---\n- Deck summary",
                model_used="gpt-4.1",
                latency_ms=100,
                prompt_tokens=50,
                completion_tokens=50,
                estimated_cost_usd=0.01,
            ),
            "# Migration Strategy\n\nBody.",
            "- Deck summary",
            "presentation",
        )

    monkeypatch.setattr(conversations, "generate_document_output", fake_generate_document_output)

    response = c.post(
        f"/conversations/{conv_id}/messages/{message_id}/execute-plan",
        json={
            "confirmed_plan": {
                "document": True,
                "document_format": "markdown",
                "document_brief": {
                    "doc_type": "presentation",
                    "title": "Client Migration Strategy",
                    "audience": "Client steering committee",
                    "template_id": "fronei-default",
                },
            }
        },
    )

    assert response.status_code == 200
    events = _events(response.text)
    assert events[-1][0] == "done"
    done = events[-1][1]
    assert done["document_preview"] is not None
    assert done["document_preview"]["format"] == "pptx"
    assert done["document_preview"]["pptx_base64"]
    assert done["answer"] == "- Deck summary"
    assert captured["brief"]["title"] == "Client Migration Strategy"
    assert captured["brief"]["template_id"] == "fronei-default"
    assert "Use phased migration" in captured["doc_context"]
    assert "TEMPLATE-FIRST PRESENTATION DESIGN BRIEF" in captured["artifact_context"]
    assert "Fronei premium freehand" in captured["artifact_context"]
    assert captured["brand_profile"].source_template_id == "fronei-default"
    assert captured["user_document_profile"].preferred_tone == "direct"
    assert captured["user_document_profile"].preferred_slide_density == "sparse"


def test_document_generation_profiles_regenerates_missing_brand_design_system(client, monkeypatch):
    _c, Session = client

    plan = passthrough("Create a client presentation.")
    plan.document_brief = {
        "doc_type": "presentation",
        "template_id": "tpl-brand",
        "title": "Client deck",
    }

    with Session() as db:
        row = DocumentTemplate(
            public_id="tpl-brand",
            user_id="u1",
            name="Brand Template",
            description="Uploaded brand deck",
            doc_type="presentation",
            storage_key="templates/u1/tpl-brand.pptx",
            original_filename="brand.pptx",
            content_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
            file_size=123,
            design_system_id="brand_u1_tpl_brand",
            is_active=True,
        )
        db.add(row)
        db.commit()

        monkeypatch.setattr(conversations, "template_grammar_for_selection", lambda *args, **kwargs: {
            "mode": "template_following",
            "template_id": "tpl-brand",
            "colors": ["0057B8", "FFC72C"],
            "fonts": ["Aptos"],
        })
        monkeypatch.setattr(conversations, "get_design_system", lambda _design_system_id: (_ for _ in ()).throw(KeyError("missing")))
        regenerated: dict[str, str] = {}

        def fake_write_brand_design_system(brand_profile, *, design_system_id, base="agentdeck_v1"):
            regenerated["design_system_id"] = design_system_id
            regenerated["source_template_id"] = brand_profile.source_template_id

        monkeypatch.setattr(conversations, "write_brand_design_system", fake_write_brand_design_system)

        brand_profile, design_system_id, user_profile = conversations._document_generation_profiles(db, "u1", plan)

        assert brand_profile.source_template_id == "tpl-brand"
        assert design_system_id == "brand_u1_tpl_brand"
        assert regenerated == {
            "design_system_id": "brand_u1_tpl_brand",
            "source_template_id": "tpl-brand",
        }
        assert user_profile.user_id == "u1"


def test_execute_plan_with_pptx_format_coerces_generation_to_presentation(client, monkeypatch):
    c, Session = client

    plan = passthrough("Create an executive report that I can present as slides.")
    plan.intent = "Create an executive report that can be presented as slides"
    plan.wants_document_output = True
    plan.document_brief = {
        "doc_type": "executive_report",
        "title": "Platform Consolidation Recommendation",
    }
    plan.document_format_options = ["markdown", "docx", "pptx"]
    plan.document_format_recommendation = "markdown"
    plan.plan_confidence = "high"

    with Session() as db:
        conv = Conversation(user_id="u1", title="Deck", profile="balanced", message_count=0)
        db.add(conv)
        db.flush()
        user_msg = ConversationMessage(
            conversation_id=conv.id,
            role="user",
            content="Create an executive report that I can present as slides.",
            plan_json=json.dumps(plan_to_dict(plan)),
        )
        db.add(user_msg)
        db.commit()
        conv_id = conv.public_id
        message_id = user_msg.id

    route = RouteDecision(
        task_type="writing",
        complexity="high",
        profile="balanced",
        primary_model="gpt-4.1",
        fallbacks=[],
        reason="pptx format coercion test",
    )
    wc = WebContextResult(context=None, status="Web context not requested.", provider="", sources_count=0, search_query=None)

    def fake_build_pipeline_setup(req, conv_arg, history, settings, **kwargs):
        setup_plan = kwargs["plan"]
        if kwargs.get("confirmed_plan"):
            apply_confirmed_plan(setup_plan, kwargs["confirmed_plan"])
        return PipelineSetup(
            plan=setup_plan,
            route=route,
            wc=wc,
            enable_native=False,
            planner_ctx=None,
            running_summary="",
            profile="balanced",
            doc_context="",
            artifact_context="",
        )

    monkeypatch.setattr(conversations, "build_pipeline_setup", fake_build_pipeline_setup)

    captured: dict = {}

    def fake_generate_document_output(plan_arg, route_arg, history, wc, planner_ctx, doc_context, deep_research, enable_native, artifact_context="", user_memory="", db=None, **kwargs):
        captured["brief"] = plan_arg.document_brief
        captured["artifact_context"] = artifact_context
        return (
            LLMResult(
                answer=json.dumps({
                    "title": "Platform Consolidation Recommendation",
                    "slides": [
                        {
                            "layout": "executive_summary",
                            "title": "Consolidation reduces cost and delivery risk",
                            "bullets": ["Approve phased migration", "Retire duplicate tooling"],
                        }
                    ],
                }) + "\n\n---SUMMARY---\n- Deck summary",
                model_used="gpt-4.1",
                latency_ms=100,
                prompt_tokens=50,
                completion_tokens=50,
                estimated_cost_usd=0.01,
            ),
            json.dumps({
                "title": "Platform Consolidation Recommendation",
                "slides": [
                    {
                        "layout": "executive_summary",
                        "title": "Consolidation reduces cost and delivery risk",
                        "bullets": ["Approve phased migration", "Retire duplicate tooling"],
                    }
                ],
            }),
            "- Deck summary",
            "presentation",
        )

    monkeypatch.setattr(conversations, "generate_document_output", fake_generate_document_output)

    response = c.post(
        f"/conversations/{conv_id}/messages/{message_id}/execute-plan",
        json={
            "confirmed_plan": {
                "document": True,
                "document_format": "pptx",
                "document_brief": {
                    "doc_type": "executive_report",
                    "title": "Platform Consolidation Recommendation",
                    "audience": "Steering committee",
                },
            }
        },
    )

    assert response.status_code == 200
    events = _events(response.text)
    done = events[-1][1]
    assert done["document_preview"]["format"] == "pptx"
    assert done["document_preview"]["pptx_base64"]
    assert captured["brief"]["doc_type"] == "presentation"
    assert captured["brief"]["source_doc_type"] == "executive_report"
    assert "TEMPLATE-FIRST PRESENTATION DESIGN BRIEF" in captured["artifact_context"]


def test_execute_plan_confirmed_expert_mode_bypasses_followup_fast_path(client, monkeypatch):
    """A user-confirmed "expert" research mode on a follow-up turn must run
    fresh research, not the cheap existing-evidence synthesis fast path —
    even though the turn type otherwise qualifies for that fast path.
    """
    c, Session = client

    plan = passthrough("Now go deeper and verify everything with primary sources.")
    plan.turn_type = "constraint_change"
    plan.intent = "Re-run research with expert-grade verification."
    plan.enriched_prompt = "Re-run research with expert-grade verification of all claims."
    plan.needs_web_search = True
    plan.recommend_deep_research = True
    plan.plan_confidence = "medium"

    with Session() as db:
        conv = Conversation(user_id="u1", title="Prior research", profile="balanced", message_count=2)
        db.add(conv)
        db.flush()
        db.add(ConversationMessage(
            conversation_id=conv.id,
            role="user",
            content="Research platform options.",
        ))
        db.add(ConversationMessage(
            conversation_id=conv.id,
            role="assistant",
            content="Prior research answer [S1].",
            task_type="research",
            complexity="high",
            research_run_id=77,
        ))
        user_msg = ConversationMessage(
            conversation_id=conv.id,
            role="user",
            content="Now go deeper and verify everything with primary sources.",
            plan_json=json.dumps(plan_to_dict(plan)),
        )
        db.add(user_msg)
        db.commit()
        conv_id = conv.public_id
        message_id = user_msg.id

    route = RouteDecision(
        task_type="research",
        complexity="high",
        profile="balanced",
        primary_model="gpt-4.1",
        fallbacks=[],
        reason="expert mode test",
    )

    followup_calls: list = []

    def fake_run_research_followup(_db, **kwargs):
        followup_calls.append(kwargs)
        raise AssertionError("followup fast path should not run for confirmed expert mode")

    def fake_run_research(_db, **kwargs):
        kwargs["progress"]("searching", "Searching targeted sources…", {"query": kwargs["query"]})
        return ResearchPipelineResult(
            run=SimpleNamespace(id=99, confidence="high"),
            result=LLMResult(
                answer="Expert-verified findings [S1].",
                model_used="gpt-4.1",
                latency_ms=300,
                prompt_tokens=60,
                completion_tokens=90,
                estimated_cost_usd=0.02,
            ),
            route=route,
            source_logs=[{"title": "Primary source", "url": "https://example.com/primary", "credibility_score": 0.9}],
            questions=["Are the claims verified against primary sources?"],
            gaps=[],
            contradictions=[],
            verifier_notes=None,
        )

    monkeypatch.setattr(conversations, "run_research_followup", fake_run_research_followup)
    monkeypatch.setattr(conversations, "run_research", fake_run_research)

    response = c.post(
        f"/conversations/{conv_id}/messages/{message_id}/execute-plan",
        json={
            "confirmed_plan": {
                "web_search": True,
                "deep_research": True,
                "research_mode": "expert",
            }
        },
    )

    assert response.status_code == 200
    events = _events(response.text)
    assert events[-1][0] == "job_started"
    turn = _wait_for_completed_turn(Session, events[-1][1]["turn_id"])
    done = json.loads(turn.result_json)

    assert followup_calls == []
    assert done["research_run_id"] == 99
    assert "Expert-verified findings" in done["answer"]


def test_execute_plan_with_clarifications_reaches_generation(client, monkeypatch):
    """Regression test for the plan-confirmation popup showing open_questions
    with no way for the user to answer them. The PlanModal now lets the user
    type an answer, sent as confirmed_plan.clarifications; execute-plan must
    fold that into the enriched prompt actually used for generation."""
    c, Session = client

    plan = passthrough("What's the best way to structure our Q3 roadmap?")
    plan.action = "answer_directly"
    plan.intent = "Help structure the Q3 roadmap"
    plan.enriched_prompt = "Help structure the Q3 roadmap."
    plan.plan_confidence = "low"
    plan.open_questions = ["Which team's roadmap — engineering, product, or both?"]

    with Session() as db:
        conv = Conversation(user_id="u1", title="Roadmap", profile="balanced", message_count=0)
        db.add(conv)
        db.flush()
        user_msg = ConversationMessage(
            conversation_id=conv.id,
            role="user",
            content="What's the best way to structure our Q3 roadmap?",
            plan_json=json.dumps(plan_to_dict(plan)),
        )
        db.add(user_msg)
        db.commit()
        conv_id = conv.public_id
        message_id = user_msg.id

    route = RouteDecision(
        task_type="planning",
        complexity="low",
        profile="balanced",
        primary_model="gpt-4.1-mini",
        fallbacks=[],
        reason="clarification test",
    )
    wc = WebContextResult(context=None, status="Web context not requested.", provider="", sources_count=0, search_query=None)

    captured_setup_plan: dict = {}

    real_build_pipeline_setup = conversations.build_pipeline_setup

    def spy_build_pipeline_setup(req, conv_arg, history, settings, **kwargs):
        setup = real_build_pipeline_setup(req, conv_arg, history, settings, **kwargs)
        captured_setup_plan["enriched_prompt"] = setup.plan.enriched_prompt
        return PipelineSetup(
            plan=setup.plan, route=route, wc=wc,
            enable_native=False, planner_ctx=None,
            running_summary="", profile="balanced",
        )

    monkeypatch.setattr(conversations, "build_pipeline_setup", spy_build_pipeline_setup)

    captured_prompt: dict = {}

    def fake_stream_llm(prompt, *args, **kwargs):
        captured_prompt["prompt"] = prompt
        yield LLMResult(
            answer="Here's a Q3 roadmap structure for engineering and product.",
            model_used="gpt-4.1-mini",
            latency_ms=10,
            prompt_tokens=5,
            completion_tokens=10,
            estimated_cost_usd=0.001,
        )

    monkeypatch.setattr(conversations, "stream_llm", fake_stream_llm)

    response = c.post(
        f"/conversations/{conv_id}/messages/{message_id}/execute-plan",
        json={
            "confirmed_plan": {
                "clarifications": "This is for the engineering team's roadmap.",
            }
        },
    )

    assert response.status_code == 200
    events = _events(response.text)
    done = events[-1][1]

    assert done["answer"] == "Here's a Q3 roadmap structure for engineering and product."
    # The user's answer to the open question reached the prompt actually sent
    # for generation, not just the persisted message.
    assert "engineering team's roadmap" in captured_prompt["prompt"]
    assert "engineering team's roadmap" in captured_setup_plan["enriched_prompt"]


def test_stream_refinement_skips_raw_mode(client, monkeypatch):
    c, Session = client
    raw_answer = " ".join(["This response has enough words to trigger refinement"] * 18)
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
    raw_answer = " ".join(["This response has enough words to trigger refinement"] * 18)
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
    raw_answer = " ".join(["This response has enough words to trigger refinement"] * 18)
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
