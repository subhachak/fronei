import pytest
from unittest.mock import MagicMock, patch
from app.services.chat_pipeline import build_pipeline_setup, run_pipeline
from app.services import plan_gate
from app.services.planner import passthrough


def _make_conv(profile="balanced"):
    conv = MagicMock()
    conv.profile = profile
    conv.running_summary = None
    conv.active_task_json = None
    return conv


def _make_req(message="Hello", profile=None, force_model=None,
              web_search=False, deep_research=False, conversation_id=None):
    req = MagicMock()
    req.message = message
    req.profile = profile
    req.force_model = force_model
    req.web_search = web_search
    req.deep_research = deep_research
    req.research_mode = "quick"
    req.document_requested = False
    req.artifact_type = None
    req.attached_documents = []
    req.confirmed_plan = None
    req.conversation_id = conversation_id
    req.force_model = force_model
    return req


def _make_settings():
    s = MagicMock()
    s.planner_model = "gemini/gemini-2.5-flash"
    s.default_profile = "balanced"
    return s


@patch("app.services.chat_pipeline.run_planner")
@patch("app.services.chat_pipeline.gather_web_context")
@patch("app.services.chat_pipeline._run_fast_turn_triage")
def test_build_pipeline_setup_returns_setup(mock_triage, mock_web, mock_planner):
    mock_triage.return_value = None
    mock_planner.return_value = passthrough("Hello")
    mock_web.return_value = MagicMock(context=None, status="ok", provider="",
                                      sources_count=0, search_query=None)
    setup = build_pipeline_setup(_make_req(), _make_conv(), [], _make_settings())
    assert setup.plan is not None
    assert setup.route is not None
    assert setup.profile == "balanced"


@patch("app.services.chat_pipeline.run_planner")
@patch("app.services.chat_pipeline.gather_web_context")
@patch("app.services.chat_pipeline.invoke_llm")
@patch("app.services.chat_pipeline._run_fast_turn_triage")
def test_run_pipeline_returns_result(mock_triage, mock_llm, mock_web, mock_planner):
    from app.services.llm_gateway import LLMResult
    mock_triage.return_value = None
    mock_planner.return_value = passthrough("What is 2+2?")
    mock_web.return_value = MagicMock(context=None, status="ok", provider="",
                                      sources_count=0, search_query=None)
    mock_llm.return_value = LLMResult(
        answer="4", model_used="gpt-4.1-mini",
        latency_ms=100, prompt_tokens=10,
        completion_tokens=5, estimated_cost_usd=0.0001,
    )
    pr = run_pipeline(_make_req("What is 2+2?"), _make_conv(), [], _make_settings())
    assert pr.result.answer == "4"
    assert pr.exec_log is not None


@patch("app.services.chat_pipeline.run_planner")
@patch("app.services.chat_pipeline.gather_web_context")
def test_build_pipeline_setup_skips_planner_for_trivial_followup(mock_web, mock_planner):
    mock_web.return_value = MagicMock(context=None, status="ok", provider="",
                                      sources_count=0, search_query=None)
    history = [{"role": "assistant", "content": "Previous answer"}]

    setup = build_pipeline_setup(_make_req("thanks"), _make_conv(), history, _make_settings())

    mock_planner.assert_not_called()
    assert setup.plan.action == "answer_directly"
    assert setup.plan.turn_type == "follow_up"
    assert setup.stage_timings[0].stage == "planner_fast_path"


@patch("app.services.chat_pipeline.run_planner")
@patch("app.services.chat_pipeline.gather_web_context")
def test_build_pipeline_setup_skips_planner_for_punctuated_trivial_followup(mock_web, mock_planner):
    mock_web.return_value = MagicMock(context=None, status="ok", provider="",
                                      sources_count=0, search_query=None)
    history = [{"role": "assistant", "content": "Previous answer"}]

    setup = build_pipeline_setup(_make_req("Thanks."), _make_conv(), history, _make_settings())

    mock_planner.assert_not_called()
    assert setup.plan.action == "answer_directly"
    assert setup.plan.turn_type == "follow_up"
    assert setup.stage_timings[0].stage == "planner_fast_path"


@patch("app.services.chat_pipeline.run_planner")
@patch("app.services.chat_pipeline.gather_web_context")
@patch("app.services.chat_pipeline._run_fast_turn_triage")
def test_build_pipeline_setup_skips_planner_when_agentic_triage_allows_it(mock_triage, mock_web, mock_planner):
    mock_triage.return_value = {
        "decision": "simple_direct",
        "reason": "evergreen concept explanation",
        "task_type": "writing",
        "complexity": "low",
    }
    mock_web.return_value = MagicMock(context=None, status="ok", provider="",
                                      sources_count=0, search_query=None)

    setup = build_pipeline_setup(
        _make_req("Explain API gateway rate limiting in plain English."),
        _make_conv(),
        [],
        _make_settings(),
    )

    mock_planner.assert_not_called()
    mock_triage.assert_called_once()
    assert setup.plan.action == "answer_directly"
    assert setup.plan.plan_confidence == "high"
    assert setup.stage_timings[0].stage == "planner_triage"
    assert setup.stage_timings[0].meta["decision"] == "simple_direct"


@patch("app.services.chat_pipeline.run_planner")
@patch("app.services.chat_pipeline.gather_web_context")
@patch("app.services.chat_pipeline._run_fast_turn_triage")
def test_build_pipeline_setup_uses_planner_when_agentic_triage_requires_it(mock_triage, mock_web, mock_planner):
    mock_triage.return_value = {
        "decision": "planner_required",
        "reason": "current immigration timing requires research judgment",
        "task_type": "research",
        "complexity": "high",
    }
    mock_planner.return_value = passthrough("What are current H4 EAD timelines?")
    mock_web.return_value = MagicMock(context=None, status="ok", provider="",
                                      sources_count=0, search_query=None)

    build_pipeline_setup(
        _make_req("What are current H4 EAD timelines?"),
        _make_conv(),
        [],
        _make_settings(),
    )

    mock_triage.assert_called_once()
    mock_planner.assert_called_once()


@patch("app.services.chat_pipeline.run_planner")
@patch("app.services.chat_pipeline.gather_web_context")
@patch("app.services.chat_pipeline._run_fast_turn_triage")
def test_build_pipeline_setup_does_not_skip_planner_when_tools_selected(mock_triage, mock_web, mock_planner):
    mock_planner.return_value = passthrough("thanks")
    mock_web.return_value = MagicMock(context=None, status="ok", provider="",
                                      sources_count=0, search_query=None)
    history = [{"role": "assistant", "content": "Previous answer"}]
    req = _make_req("thanks", web_search=True)

    build_pipeline_setup(req, _make_conv(), history, _make_settings())

    mock_triage.assert_not_called()
    mock_planner.assert_called_once()


@patch("app.services.chat_pipeline.run_planner")
@patch("app.services.chat_pipeline.gather_web_context")
@patch("app.services.chat_pipeline._run_fast_turn_triage")
def test_build_pipeline_setup_defers_web_context_until_plan_confirmed(mock_triage, mock_web, mock_planner):
    mock_triage.return_value = None
    plan = passthrough("Look up current vendor pricing")
    plan.needs_web_search = True
    plan.web_search_criticality = "material"
    plan.search_query = "current vendor pricing"
    plan.plan_confidence = "medium"
    mock_planner.return_value = plan
    req = _make_req("Look up current vendor pricing")

    setup = build_pipeline_setup(req, _make_conv(), [], _make_settings())

    mock_web.assert_not_called()
    assert setup.wc.status == "Deferred until plan confirmation."
    assert setup.stage_timings[1].stage == "web_context"
    assert setup.stage_timings[1].meta["deferred"] is True


def test_plan_gate_auto_runs_high_confidence_non_sensitive_research():
    plan = passthrough("Research current market trends")
    plan.recommend_deep_research = True
    plan.research_confidence = "high"
    plan.plan_confidence = "high"
    plan.research_risk_factors = []

    gate = plan_gate.evaluate(plan)

    assert gate.mode == "auto"
    assert gate.capabilities["deep_research"].enabled is True


def test_plan_gate_still_confirms_sensitive_research():
    plan = passthrough("Research H4 EAD processing times")
    plan.recommend_deep_research = True
    plan.research_confidence = "high"
    plan.plan_confidence = "high"
    plan.research_risk_factors = ["legal_regulatory"]

    gate = plan_gate.evaluate(plan)

    assert gate.mode == "confirm"


def test_plan_gate_confirms_planner_suggested_document_output():
    plan = passthrough("Summarize this as a client-ready deck")
    plan.wants_document_output = True
    plan.document_brief = {"doc_type": "presentation", "title": "Client Ready Deck"}
    plan.document_format_options = ["pptx"]
    plan.document_format_recommendation = "pptx"
    plan.plan_confidence = "high"

    gate = plan_gate.evaluate(plan)

    assert gate.mode == "confirm"
    assert gate.capabilities["document"].enabled is True
    assert gate.capabilities["document"].recommended is True


def test_plan_gate_does_not_confirm_explicit_document_request_again():
    plan = passthrough("Summarize this as a client-ready deck")
    plan.wants_document_output = True
    plan.document_brief = {"doc_type": "presentation", "title": "Client Ready Deck"}
    plan.document_format_options = ["pptx"]
    plan.document_format_recommendation = "pptx"
    plan.plan_confidence = "high"

    gate = plan_gate.evaluate(plan, explicit_document_request=True)

    assert gate.mode == "auto"
    assert gate.capabilities["document"].enabled is True
    assert gate.capabilities["document"].recommended is False
