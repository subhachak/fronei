from unittest.mock import patch

from app.services.chat_pipeline import _run_sub_queries
from app.services.planner import SubQuery, passthrough


@patch("app.services.chat_pipeline.invoke_llm")
def test_on_complete_callback_called_per_subquery(mock_llm):
    from app.services.llm_gateway import LLMResult

    mock_llm.return_value = LLMResult(
        answer="ok",
        model_used="gpt-4.1-mini",
        latency_ms=100,
        prompt_tokens=10,
        completion_tokens=5,
        estimated_cost_usd=0.001,
    )
    plan = passthrough("test")
    plan.sub_queries = [
        SubQuery(query="q1", purpose="", task_type="coding", preferred_model=None),
        SubQuery(query="q2", purpose="", task_type="research", preferred_model=None),
    ]
    plan.complexity = "medium"
    plan.task_type = "coding"
    plan.intent = "test"

    completed = []

    def on_complete(idx, model, task_type, latency_ms, cost):
        completed.append(idx)

    _run_sub_queries(
        plan,
        [],
        None,
        False,
        False,
        None,
        "balanced",
        on_complete=on_complete,
    )

    assert len(completed) == 2
    assert set(completed) == {0, 1}


def test_on_complete_none_does_not_raise():
    import inspect

    sig = inspect.signature(_run_sub_queries)
    assert "on_complete" in sig.parameters
    assert sig.parameters["on_complete"].default is None
