import json
from pathlib import Path
from types import SimpleNamespace
from datetime import datetime

from app.db.models import Conversation, ConversationTurn
from app.schemas import ConvChatRequest
from app.services import chat_pipeline
from app.services.planner import passthrough, plan_to_dict
from app.services.turn_graph import (
    TOOL_REGISTRY,
    ResearchToolInput,
    ArtifactRenderToolInput,
    DocumentGenerationToolInput,
    TurnGraphState,
    content_plan_node,
    decompose_research_node,
    execute_generate_document_tool,
    execute_quality_check_tool,
    execute_render_artifact_tool,
    execute_deep_research_tool,
    execute_answer_directly_tool,
    graph_rollout_decision,
    graph_trace_payload,
    mcp_adapter_for_tool,
    mcp_adapter_payload,
    run_planning_shadow_graph,
    run_turn_graph_shell,
    state_from_turn,
    tool_registry_payload,
    triage_node,
    research_stage_for_progress,
    search_research_node,
)
from app.services.llm_gateway import LLMResult
from app.schemas import RouteDecision
from app.routers.conversations import _turn_graph_canary_plan
from app.routers.admin import _turn_row


def test_turn_graph_shell_wraps_existing_pipeline_result():
    state = TurnGraphState(user_message="Explain API gateways.")

    def existing_pipeline(in_state: TurnGraphState):
        return {"final_answer": f"Answered: {in_state.user_message}"}

    result = run_turn_graph_shell(state, existing_pipeline=existing_pipeline)

    assert result.status == "completed"
    assert result.final_answer == "Answered: Explain API gateways."
    assert [event.node for event in result.events] == [
        "start",
        "execute_existing_pipeline",
        "execute_existing_pipeline",
        "end",
    ]
    assert result.node_timings[0].node == "execute_existing_pipeline"
    assert result.node_timings[0].status == "completed"


def test_turn_graph_shell_records_existing_pipeline_failure():
    state = TurnGraphState(user_message="Break")

    def existing_pipeline(_state: TurnGraphState):
        raise RuntimeError("boom")

    result = run_turn_graph_shell(state, existing_pipeline=existing_pipeline)

    assert result.status == "failed"
    assert result.error == "boom"
    assert result.node_timings[0].status == "failed"
    assert result.events[-1].event == "failed"


def test_state_from_turn_maps_existing_conversation_fields():
    conv = Conversation(
        id=10,
        public_id="conv_public",
        user_id="user_1",
        profile="focused",
        running_summary="Prior context",
        active_task_json='{"goal": "ship graph migration"}',
    )
    turn = ConversationTurn(public_id="turn_public", user_id="user_1", conversation_id=10)

    state = state_from_turn(
        conversation=conv,
        turn=turn,
        user_message="Continue",
        history=[{"role": "user", "content": "Start"}],
        user_memory="Likes concise answers.",
    )

    assert state.conversation_id == "conv_public"
    assert state.turn_id == "turn_public"
    assert state.user_id == "user_1"
    assert state.tenant_id == "user_1"
    assert state.user_role == "user"
    assert state.profile == "focused"
    assert state.running_summary == "Prior context"
    assert state.active_task == {"goal": "ship graph migration"}
    assert state.history == [{"role": "user", "content": "Start"}]
    assert state.user_memory == "Likes concise answers."

    admin_state = state_from_turn(
        conversation=conv,
        turn=turn,
        user_message="Override judge result",
        user_role="admin",
    )
    assert admin_state.user_role == "admin"


def test_graph_trace_payload_serializes_events_and_timings():
    state = TurnGraphState(user_message="Explain API gateways.")
    result = run_turn_graph_shell(state, existing_pipeline=lambda _state: {"final_answer": "ok"})

    payload = graph_trace_payload(result)

    assert payload["status"] == "completed"
    assert payload["error"] is None
    assert payload["events"][0]["node"] == "start"
    assert payload["node_timings"][0]["node"] == "execute_existing_pipeline"
    assert payload["node_timings"][0]["status"] == "completed"


def test_turn_graph_routing_fixture_has_expected_contract_shape():
    path = Path(__file__).parent / "fixtures" / "turn_graph_routing_cases.json"
    cases = json.loads(path.read_text())

    assert len(cases) >= 5
    for case in cases:
        assert case["id"]
        assert case["message"]
        assert case["expected_path"][0] == "load_context"
        assert case["expected_path"][-1] in {"persist_result", "ask_user"}
        assert isinstance(case["expected_tools"], list)


def test_triage_node_uses_llm_simple_direct_without_full_planner():
    state = TurnGraphState(user_message="Explain API gateway rate limiting in plain English.")
    req = ConvChatRequest(message=state.user_message)

    result = triage_node(
        state,
        request=req,
        triage_fn=lambda _req: {
            "decision": "simple_direct",
            "reason": "timeless low-risk explainer",
            "task_type": "reasoning",
            "complexity": "low",
        },
    )

    assert result.triage_decision["decision"] == "simple_direct"
    assert result.triage_decision["mode"] == "llm"
    assert result.plan["action"] == "answer_directly"
    assert result.plan["plan_confidence"] == "high"


def test_triage_node_requires_planner_for_explicit_document_signal():
    state = TurnGraphState(user_message="Create a strategy deck.")
    req = ConvChatRequest(message=state.user_message, document_requested=True)

    result = triage_node(
        state,
        request=req,
        triage_fn=lambda _req: {"decision": "simple_direct"},
    )

    assert result.triage_decision["decision"] == "planner_required"
    assert result.triage_decision["reason"] == "explicit_tool_or_artifact_signal"
    assert result.plan is None


def test_planning_shadow_graph_runs_planner_and_gate_for_document_recommendation():
    state = TurnGraphState(user_message="Create a strategic presentation on Q3 platform modernization.")
    req = ConvChatRequest(message=state.user_message)

    def fake_planner(*_args, **_kwargs):
        plan = passthrough(state.user_message)
        plan.intent = "Create a strategic presentation."
        plan.action = "use_workers"
        plan.wants_document_output = True
        plan.document_brief = {
            "doc_type": "presentation",
            "title": "Q3 Platform Modernization",
            "audience": "executives",
            "tone": "board-ready",
            "length": "10 slides",
        }
        plan.document_format_options = ["pptx"]
        plan.document_format_recommendation = "pptx"
        plan.plan_confidence = "high"
        return plan

    result = run_planning_shadow_graph(
        state,
        request=req,
        settings=type("SettingsStub", (), {"planner_model": "test-model"})(),
        triage_fn=lambda _req: {"decision": "planner_required", "reason": "document requested"},
        planner_fn=fake_planner,
    )

    assert result.status == "completed"
    assert result.plan["wants_document_output"] is True
    assert result.gate["mode"] == "confirm"
    assert result.gate["capabilities"]["document"]["enabled"] is True
    assert [tool["name"] for tool in result.selected_tools] == [
        "ask_user",
        "generate_document",
        "render_artifact",
    ]
    assert [timing.node for timing in result.node_timings] == [
        "load_context",
        "triage",
        "planner",
        "gate",
    ]


def test_planning_shadow_graph_matches_pipeline_setup_gate_contract(monkeypatch):
    req = ConvChatRequest(message="Create a strategic presentation on Q3 platform modernization.")
    conv = Conversation(id=11, public_id="conv_11", user_id="user_1", profile="balanced")
    history: list[dict] = []

    def fake_planner(*_args, **_kwargs):
        plan = passthrough(req.message)
        plan.intent = "Create a strategic presentation."
        plan.action = "use_workers"
        plan.wants_document_output = True
        plan.document_brief = {
            "doc_type": "presentation",
            "title": "Q3 Platform Modernization",
            "audience": "executives",
            "tone": "board-ready",
            "length": "10 slides",
        }
        plan.document_format_options = ["pptx"]
        plan.document_format_recommendation = "pptx"
        plan.plan_confidence = "high"
        return plan

    monkeypatch.setattr(chat_pipeline, "_run_fast_turn_triage", lambda _req: {"decision": "planner_required"})
    monkeypatch.setattr(chat_pipeline, "run_planner", fake_planner)
    monkeypatch.setattr(
        chat_pipeline,
        "gather_web_context",
        lambda *_args, **_kwargs: chat_pipeline.WebContextResult(
            context=None,
            status="No web context requested.",
            provider="",
            sources_count=0,
            search_query=None,
        ),
    )
    monkeypatch.setattr(
        chat_pipeline,
        "choose_route",
        lambda *_args, **_kwargs: chat_pipeline.RouteDecision(
            task_type="writing",
            complexity="medium",
            profile="balanced",
            primary_model="test-model",
            fallbacks=[],
            reason="test",
        ),
    )

    setup = chat_pipeline.build_pipeline_setup(
        req,
        conv,
        history,
        settings=type("SettingsStub", (), {"planner_model": "test-model"})(),
    )
    graph = run_planning_shadow_graph(
        TurnGraphState(user_message=req.message),
        request=req,
        settings=type("SettingsStub", (), {"planner_model": "test-model"})(),
        history=history,
        triage_fn=lambda _req: {"decision": "planner_required"},
        planner_fn=fake_planner,
    )

    assert graph.plan["action"] == setup.plan.action
    assert graph.plan["wants_document_output"] == setup.plan.wants_document_output
    assert graph.gate["mode"] == "confirm"
    web_timing = next(timing for timing in setup.stage_timings if timing.stage == "web_context")
    assert web_timing.meta["deferred"] is True
    assert graph.gate["capabilities"]["document"]["enabled"] is True


def test_turn_graph_canary_accepts_only_high_confidence_simple_direct():
    state = TurnGraphState(user_message="Explain API gateway rate limiting.")
    plan = passthrough(state.user_message)
    plan.action = "answer_directly"
    plan.plan_confidence = "high"
    state.plan = plan_to_dict(plan)
    state.gate = {
        "mode": "auto",
        "plan_confidence": "high",
        "open_questions": [],
        "capabilities": {
            "web_search": {"enabled": False, "recommended": False},
            "deep_research": {"enabled": False, "recommended": False},
            "document": {"enabled": False, "recommended": False},
        },
    }

    canary = _turn_graph_canary_plan(state)

    assert canary is not None
    assert canary.action == "answer_directly"


def test_turn_graph_canary_rejects_document_tool_plan():
    state = TurnGraphState(user_message="Create a deck.")
    plan = passthrough(state.user_message)
    plan.action = "answer_directly"
    plan.plan_confidence = "high"
    state.plan = plan_to_dict(plan)
    state.gate = {
        "mode": "auto",
        "plan_confidence": "high",
        "open_questions": [],
        "capabilities": {
            "document": {"enabled": True, "recommended": False},
        },
    }

    assert _turn_graph_canary_plan(state) is None


def test_tool_registry_exposes_core_tool_contracts():
    names = {tool["name"] for tool in tool_registry_payload()}

    assert {
        "answer_directly",
        "ask_user",
        "web_context",
        "deep_research",
        "generate_document",
        "render_artifact",
        "quality_check",
        "load_memory",
        "load_templates",
    }.issubset(names)
    assert TOOL_REGISTRY["deep_research"].execution_mode == "durable"
    assert TOOL_REGISTRY["ask_user"].requires_confirmation is True


def test_execute_answer_directly_tool_uses_injected_executor():
    state = TurnGraphState(user_message="Explain rate limiting.")
    plan = passthrough(state.user_message)
    plan.action = "answer_directly"
    plan.enriched_prompt = "Explain rate limiting."
    state.plan = plan_to_dict(plan)
    state.history = [{"role": "user", "content": "Hi"}]

    captured = {}

    def fake_executor(prompt, route, **kwargs):
        captured["prompt"] = prompt
        captured["route"] = route
        captured["kwargs"] = kwargs
        return LLMResult(
            answer="Rate limiting controls request volume.",
            model_used="test-model",
            latency_ms=7,
            prompt_tokens=3,
            completion_tokens=4,
            estimated_cost_usd=0.001,
        )

    output = execute_answer_directly_tool(
        state,
        route=RouteDecision(
            task_type="reasoning",
            complexity="low",
            profile="balanced",
            primary_model="test-model",
            fallbacks=[],
            reason="test",
        ),
        executor=fake_executor,
    )

    assert output.status == "ok"
    assert output.user_message == "Rate limiting controls request volume."
    assert state.final_answer == "Rate limiting controls request volume."
    assert captured["prompt"] == "Explain rate limiting."
    assert captured["kwargs"]["history"] == [{"role": "user", "content": "Hi"}]


def test_research_stage_for_progress_maps_current_engine_terms():
    assert research_stage_for_progress("planning") == "decompose"
    assert research_stage_for_progress("searching") == "search"
    assert research_stage_for_progress("reading") == "crawl"
    assert research_stage_for_progress("extracting") == "extract"
    assert research_stage_for_progress("checking") == "sufficiency"
    assert research_stage_for_progress("synthesising") == "synthesize"
    assert research_stage_for_progress("verifying") == "verify"
    assert research_stage_for_progress("complete") == "complete"


def test_execute_deep_research_tool_wraps_runner_progress_and_result():
    state = TurnGraphState(user_message="Research AI governance.")
    forwarded = []

    def fake_runner(_db, **kwargs):
        progress = kwargs["progress"]
        progress("planning", "Planning research questions…", {"questions": ["q1"]})
        progress("searching", "Search pass 1…", {"iteration": 1})
        progress("extracting", "Extracting claims…", {"source_count": 2})
        progress("synthesising", "Synthesising evidence…", {})
        progress("complete", "Research complete.", {"research_run_id": 42})
        return SimpleNamespace(
            run=SimpleNamespace(id=42, confidence="high", mode="deep"),
            result=LLMResult(
                answer="AI governance answer.",
                model_used="test-model",
                latency_ms=55,
                prompt_tokens=10,
                completion_tokens=20,
                estimated_cost_usd=0.02,
            ),
            route=RouteDecision(
                task_type="research",
                complexity="high",
                profile="balanced",
                primary_model="test-model",
                fallbacks=[],
                reason="test",
            ),
            source_logs=[{"url": "https://example.com"}],
            claim_logs=[{"claim": "claim"}],
            questions=["q1"],
            gaps=[],
            contradictions=[],
            verifier_notes=None,
        )

    output = execute_deep_research_tool(
        state,
        db=object(),
        tool_input=ResearchToolInput(
            user_id="user_1",
            conversation_id=123,
            query="Research AI governance.",
            profile="balanced",
            mode="deep",
        ),
        runner=fake_runner,
        progress_sink=lambda stage, message, extra: forwarded.append((stage, message, extra)),
    )

    assert output.status == "ok"
    assert output.user_message == "AI governance answer."
    assert state.research_result["run_id"] == 42
    assert state.research_result["sources_count"] == 1
    assert [event["stage"] for event in state.research_progress] == [
        "decompose",
        "search",
        "extract",
        "synthesize",
        "complete",
    ]
    assert state.node_timings[-1].node == "deep_research"
    assert state.node_timings[-1].status == "completed"
    assert forwarded[0] == ("planning", "Planning research questions…", {"questions": ["q1"]})


def test_research_stage_nodes_record_timing_and_progress():
    state = TurnGraphState(user_message="Research AI governance.")

    decompose_research_node(state, fn=lambda _state: {"questions": ["q1", "q2"]})
    search_research_node(state, fn=lambda _state: {"sources": 3})

    assert [timing.node for timing in state.node_timings] == [
        "research.decompose",
        "research.search",
    ]
    assert [event["stage"] for event in state.research_progress] == ["decompose", "search"]
    assert state.research_progress[0]["data"] == {"questions": ["q1", "q2"]}
    assert state.events[-1].node == "research.search"
    assert state.events[-1].event == "completed"


def test_research_stage_node_records_failure():
    state = TurnGraphState(user_message="Research AI governance.")

    def fail(_state):
        raise RuntimeError("search backend down")

    try:
        search_research_node(state, fn=fail)
    except RuntimeError:
        pass

    assert state.node_timings[-1].node == "research.search"
    assert state.node_timings[-1].status == "failed"
    assert state.events[-1].event == "failed"


def test_document_stage_nodes_record_events():
    state = TurnGraphState(user_message="Create a deck.")

    content_plan_node(state, fn=lambda _state: {"sections": 8})

    assert state.node_timings[-1].node == "document.content_plan"
    assert state.node_timings[-1].status == "completed"
    assert state.document_result["events"][0]["stage"] == "content_plan"
    assert state.document_result["events"][0]["data"] == {"sections": 8}


def test_execute_generate_document_tool_wraps_generator_result():
    state = TurnGraphState(user_message="Create a deck.")

    def fake_generator(**_kwargs):
        return (
            LLMResult(
                answer="body\n---SUMMARY---\nsummary",
                model_used="test-model",
                latency_ms=25,
                prompt_tokens=5,
                completion_tokens=8,
                estimated_cost_usd=0.003,
            ),
            "{\"title\":\"Deck\"}",
            "Deck summary",
            "presentation",
        )

    output = execute_generate_document_tool(
        state,
        tool_input=DocumentGenerationToolInput(
            title="Deck",
            doc_type="presentation",
            format="pptx",
            quality_mode="standard",
        ),
        generator=fake_generator,
        plan=object(),
    )

    assert output.status == "ok"
    assert output.user_message == "Deck summary"
    assert state.document_result["doc_type"] == "presentation"
    assert state.document_result["body"] == "{\"title\":\"Deck\"}"
    assert state.node_timings[-1].node == "generate_document"


def test_execute_render_artifact_tool_wraps_renderer_result():
    state = TurnGraphState(user_message="Create a deck.")

    def fake_renderer(title, body, doc_type, fmt, **kwargs):
        return {
            "title": title,
            "doc_type": doc_type,
            "format": fmt,
            "markdown": body,
            "quality_mode": kwargs["quality_mode"],
        }

    output = execute_render_artifact_tool(
        state,
        tool_input=ArtifactRenderToolInput(
            title="Deck",
            body="# Deck",
            doc_type="presentation",
            format="pptx",
            quality_mode="executive",
        ),
        renderer=fake_renderer,
    )

    assert output.status == "ok"
    assert state.artifact_result["format"] == "pptx"
    assert state.artifact_result["quality_mode"] == "executive"
    assert state.node_timings[-1].node == "render_artifact"


def test_execute_quality_check_tool_attaches_render_qa():
    state = TurnGraphState(user_message="Create a deck.")
    preview = {"title": "Deck", "format": "pptx"}

    output = execute_quality_check_tool(
        state,
        preview=preview,
        checker=lambda _preview, **_kwargs: {"available": True, "issues": []},
    )

    assert output.status == "ok"
    assert state.artifact_result["render_qa"] == {"available": True, "issues": []}
    assert state.node_timings[-1].node == "quality_check"


def test_mcp_adapter_catalog_maps_back_to_registered_tools():
    payload = mcp_adapter_payload()
    tool_names = {item["tool_name"] for item in payload}

    assert {"web_context", "deep_research", "load_templates", "render_artifact"}.issubset(tool_names)
    assert mcp_adapter_for_tool("web_context").adapter_id == "mcp.web.search"
    assert all(item["tool"]["name"] == item["tool_name"] for item in payload)


def test_graph_rollout_decision_keeps_kill_switch_and_answer_canary_only():
    disabled = graph_rollout_decision(
        type("SettingsStub", (), {"turn_graph_enabled": False, "turn_graph_authoritative": False})()
    )
    enabled_answer = graph_rollout_decision(
        type("SettingsStub", (), {"turn_graph_enabled": True, "turn_graph_authoritative": False})(),
        tool_name="answer_directly",
    )
    enabled_research = graph_rollout_decision(
        type("SettingsStub", (), {"turn_graph_enabled": True, "turn_graph_authoritative": False})(),
        tool_name="deep_research",
    )

    assert disabled.mode == "disabled"
    assert disabled.record_shadow_trace is False
    assert disabled.allow_canary_execution is False
    assert disabled.allow_full_execution is False
    assert enabled_answer.mode == "shadow_canary"
    assert enabled_answer.record_shadow_trace is True
    assert enabled_answer.allow_canary_execution is True
    assert enabled_answer.allow_full_execution is False
    assert enabled_research.record_shadow_trace is True
    assert enabled_research.allow_canary_execution is False
    assert enabled_research.allow_full_execution is False


def test_graph_rollout_decision_authoritative_allows_full_execution():
    decision = graph_rollout_decision(
        type("SettingsStub", (), {"turn_graph_enabled": True, "turn_graph_authoritative": True})(),
        tool_name="deep_research",
    )

    assert decision.mode == "authoritative"
    assert decision.record_shadow_trace is True
    assert decision.allow_canary_execution is True
    assert decision.allow_full_execution is True
    assert decision.to_dict()["allow_full_execution"] is True


def test_admin_turn_row_exposes_graph_summary_and_canary():
    turn = ConversationTurn(
        public_id="turn_public",
        user_id="user_1",
        conversation_id=10,
        turn_kind="quick",
        status="completed",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        lifecycle_json=json.dumps([
            {
                "event": "turn_graph_shadow",
                "status": "completed",
                "selected_tools": [{"name": "answer_directly"}],
                "node_timings": [
                    {"node": "load_context", "status": "completed", "latency_ms": 1},
                    {"node": "triage", "status": "completed", "latency_ms": 2},
                ],
                "events": [],
            },
            {
                "event": "turn_graph_canary",
                "mode": "simple_direct",
                "planner_model": "none",
                "action": "answer_directly",
                "plan_confidence": "high",
            },
        ]),
    )

    row = _turn_row(turn, "conv_public")

    assert row["graph_summary"]["status"] == "completed"
    assert row["graph_summary"]["path"] == ["load_context", "triage"]
    assert row["graph_summary"]["total_node_latency_ms"] == 3
    assert row["graph_summary"]["selected_tools"] == ["answer_directly"]
    assert row["graph_summary"]["canary"]["mode"] == "simple_direct"
