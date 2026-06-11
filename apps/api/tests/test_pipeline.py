import pytest
from unittest.mock import MagicMock, patch
from app.services.chat_pipeline import build_pipeline_setup, run_pipeline
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
    req.conversation_id = conversation_id
    return req


def _make_settings():
    s = MagicMock()
    s.planner_model = "gemini/gemini-2.5-flash"
    s.default_profile = "balanced"
    s.daily_budget_usd = 10.0
    return s


@patch("app.services.chat_pipeline.run_planner")
@patch("app.services.chat_pipeline.gather_web_context")
def test_build_pipeline_setup_returns_setup(mock_web, mock_planner):
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
def test_run_pipeline_returns_result(mock_llm, mock_web, mock_planner):
    from app.services.llm_gateway import LLMResult
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
