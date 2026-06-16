from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.agent_runtime.budget_guard import BudgetExceeded, RuntimeBudgetGuard
from app.services.agent_runtime.circuit_breaker import CircuitBreakerRegistry, CircuitOpen, CircuitState
from app.services.agent_runtime.degradation import DegradationTier, resolve_tier
from app.services.agent_runtime.job_checkpoint import JobCheckpoint
from app.services.agent_runtime.models import RuntimeBudget
from app.services.agent_runtime.registry import _load_from_files
from app.services.agent_runtime.research_agent import ResearchAgent
from app.services.agent_runtime.document_agent import DocumentAgent
from app.services.agent_runtime.ssrf_guard import SSRFViolation, check_url_public
from app.services.turn_graph.state import TurnGraphState


def test_s1_direct_answer_agent_returns_answer(monkeypatch):
    """DirectAnswerAgent.answer() calls invoke_llm and returns the result."""
    from app.services.agent_runtime.direct_answer import DirectAnswerAgent

    monkeypatch.setattr(
        "app.services.agent_runtime.direct_answer.invoke_llm",
        lambda **kw: SimpleNamespace(answer="42", model_used="m", latency_ms=5, estimated_cost_usd=0.001),
    )
    result = DirectAnswerAgent(_load_from_files()).answer(
        TurnGraphState(user_message="What is 6 times 7?")
    )
    assert result.answer == "42"
    assert result.model_used == "m"


def test_s1_direct_answer_agent_passes_history(monkeypatch):
    """DirectAnswerAgent passes the last 8 history turns to invoke_llm."""
    from app.services.agent_runtime.direct_answer import DirectAnswerAgent

    captured: list[dict] = []

    def fake_invoke(**kw):
        captured.append({"history": kw.get("history")})
        return SimpleNamespace(answer="ok", model_used="m", latency_ms=1, estimated_cost_usd=0)

    monkeypatch.setattr("app.services.agent_runtime.direct_answer.invoke_llm", fake_invoke)
    history = [{"role": "user", "content": f"msg {idx}"} for idx in range(10)]
    DirectAnswerAgent(_load_from_files()).answer(
        TurnGraphState(user_message="follow-up", history=history)
    )
    assert len(captured[0]["history"]) == 8
    assert captured[0]["history"][0]["content"] == "msg 2"


def test_s1_direct_answer_agent_web_context_injected(monkeypatch):
    """DirectAnswerAgent passes web_context snippets to invoke_llm when present."""
    from app.services.agent_runtime.direct_answer import DirectAnswerAgent

    captured: list[dict] = []

    def fake_invoke(**kw):
        captured.append({"web_context": kw.get("web_context")})
        return SimpleNamespace(answer="ok", model_used="m", latency_ms=1, estimated_cost_usd=0)

    monkeypatch.setattr("app.services.agent_runtime.direct_answer.invoke_llm", fake_invoke)
    state = TurnGraphState(
        user_message="q",
        web_context={"snippets": [{"content": "snippet text"}]},
    )
    DirectAnswerAgent(_load_from_files()).answer(state)
    assert "snippet text" in (captured[0]["web_context"] or "")


def test_s2_research_sources_populated_from_checkpoint(monkeypatch):
    monkeypatch.setattr(JobCheckpoint, "load", lambda self, turn_id, stage: ({"research_sources": [{"url": "https://example.com"}]}, 0.9) if stage == "research.crawl_complete" else (None, None))
    state = TurnGraphState(user_message="research", turn_id="t1")
    assert ResearchAgent(_load_from_files())._try_resume_crawl(state, JobCheckpoint(), "t1")
    assert state.research_sources[0]["url"]


def test_s2_research_checkpoint_stage_selection(monkeypatch):
    monkeypatch.setattr(JobCheckpoint, "load", lambda self, turn_id, stage: ({"x": 1}, 0.9) if stage == "research.crawl_complete" else (None, None))
    from app.services.agent_runtime.job_checkpoint import resume_tier

    assert resume_tier("t1") == "research.crawl_complete"


def test_s2_research_result_has_answer_from_synthesis_checkpoint(monkeypatch):
    monkeypatch.setattr(JobCheckpoint, "load", lambda self, turn_id, stage: ({"research_answer": "Answer"}, 0.9) if stage == "research.synthesis_complete" else (None, None))
    monkeypatch.setattr(JobCheckpoint, "clear", lambda self, turn_id: None)
    result = ResearchAgent(_load_from_files()).run(TurnGraphState(user_message="q", turn_id="t1"), SimpleNamespace(plan={}))
    assert result.answer == "Answer"


def test_s3_contradiction_resolver_executive_only(monkeypatch):
    state = TurnGraphState(user_message="q", quality_mode="standard")
    state.research_claims = [{"text": "A"}, {"text": "B"}]
    ResearchAgent(_load_from_files())._resolve_contradictions(state)
    assert state.research_progress == []


def test_s3_deep_research_claims_shape():
    state = TurnGraphState(user_message="q", quality_mode="executive")
    state.research_claims = [{"text": "Claim", "source_url": "u", "confidence": 0.8}]
    assert state.research_claims[0]["confidence"] == 0.8


def test_s3_synthesis_checkpoint_trusted(monkeypatch):
    monkeypatch.setattr(JobCheckpoint, "load", lambda self, turn_id, stage: ({"research_answer": "A"}, 0.9))
    obj = ResearchAgent(_load_from_files())._try_resume_synthesis(TurnGraphState(user_message="q"), JobCheckpoint(), "t")
    assert obj.answer == "A"


def test_s4_document_plan_checkpoint_saved(monkeypatch):
    saved = []
    monkeypatch.setattr(JobCheckpoint, "save", lambda self, turn_id, stage, payload, **kw: saved.append(stage))
    monkeypatch.setattr("app.services.llm_gateway.invoke_llm_json", lambda messages, route: SimpleNamespace(answer='{"title":"Doc","doc_type":"executive_report"}'))
    DocumentAgent(_load_from_files())._plan_stage(TurnGraphState(user_message="doc", turn_id="t"), {}, "standard")
    assert "document.plan_complete" in saved


def test_s4_document_generate_checkpoint_saved(monkeypatch):
    saved = []
    monkeypatch.setattr(JobCheckpoint, "save", lambda self, turn_id, stage, payload, **kw: saved.append(stage))
    monkeypatch.setattr("app.services.agent_runtime.tool_runner.ToolRunner.run", lambda *a, **kw: SimpleNamespace(output={"docx_base64": "DOCX", "filename": "doc.docx"}, latency_ms=1))
    monkeypatch.setattr("app.services.llm_gateway.invoke_llm", lambda **kw: SimpleNamespace(answer="# Doc", model_used="m", latency_ms=1, estimated_cost_usd=0))
    DocumentAgent(_load_from_files())._generate_stage(TurnGraphState(user_message="doc", turn_id="t"), {"title": "Doc", "doc_type": "executive_report"}, SimpleNamespace(plan={}), None, None, False, None, SimpleNamespace(run=lambda *a, **kw: SimpleNamespace(output={"docx_base64": "DOCX", "filename": "doc.docx"}, latency_ms=1)), [], [])
    assert "document.generate_complete" in saved


def test_s4_evidence_binder_used_for_non_presentation(monkeypatch):
    used: list[str] = []

    class FakeSubAgent:
        def __init__(self, agent_id, registry):
            used.append(agent_id)

        def invoke(self, **kwargs):
            return SimpleNamespace(answer="# Report", model_used="m", latency_ms=1, estimated_cost_usd=0)

    monkeypatch.setattr("app.services.agent_runtime.document_agent.SubAgentRunner", FakeSubAgent)

    result = DocumentAgent(_load_from_files())._generate_content(
        TurnGraphState(user_message="doc"),
        {"title": "Doc", "doc_type": "executive_report"},
        SimpleNamespace(plan={}),
    )

    assert result.answer == "# Report"
    assert used == ["evidence_binder"]


def test_s5_design_stage_fetches_grammar_for_presentation(monkeypatch):
    """_design_stage calls _fetch_template_grammar for presentation doc_type."""
    fetched: list[str | None] = []

    def fake_fetch(user_id, template_id, brief, db):
        fetched.append(template_id)
        return {"slide_patterns": [{"role": "title"}, {"role": "content"}]}

    monkeypatch.setattr("app.services.agent_runtime.document_agent._fetch_template_grammar", fake_fetch)
    monkeypatch.setattr(
        "app.services.agent_runtime.document_agent.brand_profile_for_selection",
        lambda db, user_id, tid, grammar=None: SimpleNamespace(to_dict=lambda: {"source": "builtin"}),
    )
    grammar_holder: list[dict] = []
    state = TurnGraphState(user_message="deck", user_id="u1")
    DocumentAgent(_load_from_files())._design_stage(
        state,
        {"title": "Deck", "doc_type": "presentation"},
        is_presentation=True,
        template_id="tmpl-1",
        db=None,
        grammar_holder=grammar_holder,
    )
    assert fetched == ["tmpl-1"]
    assert grammar_holder[0]["slide_patterns"][0]["role"] == "title"
    assert state.brand_profile["source"] == "builtin"


def test_s5_design_stage_skips_grammar_for_non_presentation(monkeypatch):
    """_design_stage does NOT call _fetch_template_grammar for non-presentation doc types."""
    fetched: list[bool] = []
    monkeypatch.setattr(
        "app.services.agent_runtime.document_agent._fetch_template_grammar",
        lambda *a, **kw: fetched.append(True) or {},
    )
    grammar_holder: list[dict] = []
    DocumentAgent(_load_from_files())._design_stage(
        TurnGraphState(user_message="doc", user_id="u1"),
        {"title": "Doc", "doc_type": "executive_report"},
        is_presentation=False,
        template_id=None,
        db=None,
        grammar_holder=grammar_holder,
    )
    assert fetched == []
    assert grammar_holder == []


def test_s5_deck_designer_used_for_presentation_content(monkeypatch):
    """_generate_content routes to deck_designer agent for doc_type presentation."""
    used: list[str] = []

    class FakeDeckAgent:
        def __init__(self, agent_id, registry):
            used.append(agent_id)

        def invoke(self, **kwargs):
            return SimpleNamespace(answer="# Slide deck", model_used="m", latency_ms=1, estimated_cost_usd=0)

    monkeypatch.setattr("app.services.agent_runtime.document_agent.SubAgentRunner", FakeDeckAgent)
    DocumentAgent(_load_from_files())._generate_content(
        TurnGraphState(user_message="quarterly deck"),
        {"title": "Q4 Review", "doc_type": "presentation"},
        SimpleNamespace(plan={}),
        grammar={"slide_patterns": []},
        research_summary=None,
    )
    assert used == ["deck_designer"]


def test_s6_research_repair_best_seen(monkeypatch):
    from app.services.agent_runtime.models import JudgeResult

    agent = ResearchAgent(_load_from_files())
    holder = [SimpleNamespace(answer="initial")]
    scores = iter([
        JudgeResult(id="1", target_type="research", target_id="t", judge_agent_id="j", score=0.5, status="repair", required_repairs=[{"instruction": "fix"}], can_publish=False),
        JudgeResult(id="2", target_type="research", target_id="t", judge_agent_id="j", score=0.9, status="pass", can_publish=True),
    ])
    monkeypatch.setattr("app.services.agent_runtime.judge_service.JudgeService.evaluate", lambda *a, **kw: next(scores))
    monkeypatch.setattr(agent, "_resynthesize_with_repairs", lambda *a, **kw: SimpleNamespace(answer="fixed"))
    agent._judge_synthesis_loop(TurnGraphState(user_message="q", quality_mode="executive"), SimpleNamespace(), holder)
    assert holder[0].answer == "fixed"


def test_s6_research_repair_uses_synthesizer_registry():
    assert "research_synthesizer" in _load_from_files().agents


def test_s6_research_repair_quality_mode_gates():
    from app.services.agent_runtime.utils import effective_max_repair_iters

    assert effective_max_repair_iters("draft", None) == 0


@pytest.mark.parametrize("url", ["http://192.168.1.1", "http://localhost/admin", "http://metadata.google.internal"])
def test_s7_ssrf_blocks_unsafe_urls(url):
    with pytest.raises(SSRFViolation):
        check_url_public(url)


def test_s8_budget_blocks_second_model_call():
    guard = RuntimeBudgetGuard(RuntimeBudget(max_model_calls=1))
    guard.check_model_call()
    with pytest.raises(BudgetExceeded):
        guard.check_model_call()


def test_s8_budget_blocks_cost_overrun():
    with pytest.raises(BudgetExceeded):
        RuntimeBudgetGuard(RuntimeBudget(max_turn_cost_usd=50.0)).record_cost(100.0)


def test_s8_budget_tool_call_guard():
    guard = RuntimeBudgetGuard(RuntimeBudget(max_tool_calls=0))
    with pytest.raises(BudgetExceeded):
        guard.check_tool_call()


def test_s9_circuit_open_raises_circuit_open():
    breaker = CircuitBreakerRegistry.get().breaker("x", failure_threshold=1)
    with pytest.raises(ValueError):
        breaker.call(lambda: (_ for _ in ()).throw(ValueError("bad")))
    with pytest.raises(CircuitOpen):
        breaker.call(lambda: "ok")


def test_s9_degradation_tier_degraded_when_search_open():
    breaker = CircuitBreakerRegistry.get().breaker("tool:web_search", failure_threshold=1)
    with pytest.raises(ValueError):
        breaker.call(lambda: (_ for _ in ()).throw(ValueError("bad")))
    assert resolve_tier() == DegradationTier.DEGRADED_RESEARCH


def test_s9_circuit_half_open_after_timeout():
    breaker = CircuitBreakerRegistry.get().breaker("x", failure_threshold=1, recovery_timeout=0.01)
    with pytest.raises(ValueError):
        breaker.call(lambda: (_ for _ in ()).throw(ValueError("bad")))
    import time

    time.sleep(0.02)
    assert breaker.state == CircuitState.HALF_OPEN


def test_s10_best_seen_score_wins_over_latest(monkeypatch):
    """_qa_repair_stage emits best-scoring content, not the last iteration's content."""
    from app.services.agent_runtime.models import JudgeResult

    registry = _load_from_files()
    registry.judges["document_judge"].max_repair_iterations = 2
    agent = DocumentAgent(registry)

    scores = iter([
        JudgeResult(
            id="j0",
            target_type="document",
            target_id="t",
            judge_agent_id="j",
            score=0.5,
            status="repair",
            required_repairs=[{"instruction": "fix"}],
            can_publish=False,
        ),
        JudgeResult(
            id="j1",
            target_type="document",
            target_id="t",
            judge_agent_id="j",
            score=0.9,
            status="repair",
            required_repairs=[{"instruction": "minor"}],
            can_publish=False,
        ),
        JudgeResult(
            id="j2",
            target_type="document",
            target_id="t",
            judge_agent_id="j",
            score=0.3,
            status="repair",
            required_repairs=[{"instruction": "worse"}],
            can_publish=False,
        ),
    ])
    monkeypatch.setattr("app.services.agent_runtime.judge_service.JudgeService.evaluate", lambda *a, **kw: next(scores))

    call_count = 0

    def fake_regen(*a, **kw):
        nonlocal call_count
        call_count += 1
        return (
            SimpleNamespace(answer=f"v{call_count}", model_used="m", latency_ms=1, estimated_cost_usd=0),
            {"docx_base64": f"V{call_count}", "filename": f"v{call_count}.docx", "tool_latency_ms": 1},
        )

    monkeypatch.setattr(agent, "_regenerate_with_repairs", fake_regen)

    content_holder = [SimpleNamespace(answer="v0")]
    tool_holder = [{"docx_base64": "V0", "filename": "v0.docx", "tool_latency_ms": 1}]
    state = TurnGraphState(user_message="doc", quality_mode="executive")

    result = agent._qa_repair_stage(
        state,
        content_holder[0],
        {"title": "Doc", "doc_type": "executive_report"},
        False,
        SimpleNamespace(plan={}),
        None,
        None,
        None,
        content_holder,
        tool_holder,
    )

    assert content_holder[0].answer == "v1"
    assert tool_holder[0]["docx_base64"] == "V1"
    assert result["judge_score"] == 0.9


def test_s10_best_seen_content_holder_update(monkeypatch):
    """_qa_repair_stage leaves current content untouched when the first judge passes."""
    from app.services.agent_runtime.models import JudgeResult

    agent = DocumentAgent(_load_from_files())
    monkeypatch.setattr(
        "app.services.agent_runtime.judge_service.JudgeService.evaluate",
        lambda *a, **kw: JudgeResult(
            id="j-pass",
            target_type="document",
            target_id="t",
            judge_agent_id="j",
            score=0.92,
            status="pass",
            can_publish=True,
        ),
    )

    def fail_regen(*a, **kw):
        raise AssertionError("repair should not run after a passing judge")

    monkeypatch.setattr(agent, "_regenerate_with_repairs", fail_regen)
    content_holder = [SimpleNamespace(answer="best")]
    tool_holder = [{"docx_base64": "BEST", "filename": "best.docx", "tool_latency_ms": 1}]

    result = agent._qa_repair_stage(
        TurnGraphState(user_message="doc", quality_mode="executive"),
        content_holder[0],
        {"title": "Doc", "doc_type": "executive_report"},
        False,
        SimpleNamespace(plan={}),
        None,
        None,
        None,
        content_holder,
        tool_holder,
    )

    assert content_holder[0].answer == "best"
    assert tool_holder[0]["docx_base64"] == "BEST"
    assert result == {"judge_status": "pass", "judge_score": 0.92}


def test_s10_document_repair_stage_emits_best_seen(monkeypatch):
    from app.services.agent_runtime.models import JudgeResult

    registry = _load_from_files()
    registry.judges["document_judge"].max_repair_iterations = 2
    agent = DocumentAgent(registry)
    scores = iter([
        JudgeResult(id="1", target_type="document", target_id="t", judge_agent_id="j", score=0.5, status="repair", required_repairs=[{"instruction": "fix"}], can_publish=False),
        JudgeResult(id="2", target_type="document", target_id="t", judge_agent_id="j", score=0.9, status="pass", can_publish=True),
    ])
    monkeypatch.setattr("app.services.agent_runtime.judge_service.JudgeService.evaluate", lambda *a, **kw: next(scores))
    monkeypatch.setattr(agent, "_regenerate_with_repairs", lambda *a, **kw: (SimpleNamespace(answer="best", model_used="m", latency_ms=1, estimated_cost_usd=0), {"docx_base64": "BEST", "filename": "best.docx", "tool_latency_ms": 1}))
    content_holder = [SimpleNamespace(answer="initial")]
    tool_holder = [{"docx_base64": "INITIAL", "filename": "initial.docx", "tool_latency_ms": 1}]
    state = TurnGraphState(user_message="doc", quality_mode="executive")

    result = agent._qa_repair_stage(
        state,
        content_holder[0],
        {"title": "Doc", "doc_type": "executive_report"},
        False,
        SimpleNamespace(plan={}),
        None,
        None,
        None,
        content_holder,
        tool_holder,
    )

    assert content_holder[0].answer == "best"
    assert tool_holder[0]["docx_base64"] == "BEST"
    assert result == {"judge_status": "pass", "judge_score": 0.9}


def test_s10_poison_detection_breaks_at_two_consecutive_regressions(monkeypatch):
    """_qa_repair_stage stops iterating after 2 consecutive score regressions."""
    from app.services.agent_runtime.models import JudgeResult

    registry = _load_from_files()
    registry.judges["document_judge"].max_repair_iterations = 10
    agent = DocumentAgent(registry)

    scores = iter([
        JudgeResult(
            id="j0",
            target_type="document",
            target_id="t",
            judge_agent_id="j",
            score=0.8,
            status="repair",
            required_repairs=[{"instruction": "fix"}],
            can_publish=False,
        ),
        JudgeResult(
            id="j1",
            target_type="document",
            target_id="t",
            judge_agent_id="j",
            score=0.4,
            status="repair",
            required_repairs=[{"instruction": "bad"}],
            can_publish=False,
        ),
        JudgeResult(
            id="j2",
            target_type="document",
            target_id="t",
            judge_agent_id="j",
            score=0.3,
            status="repair",
            required_repairs=[{"instruction": "worse"}],
            can_publish=False,
        ),
    ])
    monkeypatch.setattr("app.services.agent_runtime.judge_service.JudgeService.evaluate", lambda *a, **kw: next(scores))

    regen_calls = 0

    def fake_regen(*a, **kw):
        nonlocal regen_calls
        regen_calls += 1
        return (
            SimpleNamespace(answer=f"v{regen_calls}", model_used="m", latency_ms=1, estimated_cost_usd=0),
            {"docx_base64": f"V{regen_calls}", "filename": f"v{regen_calls}.docx", "tool_latency_ms": 1},
        )

    monkeypatch.setattr(agent, "_regenerate_with_repairs", fake_regen)

    content_holder = [SimpleNamespace(answer="initial")]
    tool_holder = [{"docx_base64": "INIT", "filename": "init.docx", "tool_latency_ms": 1}]

    agent._qa_repair_stage(
        TurnGraphState(user_message="doc", quality_mode="executive"),
        content_holder[0],
        {"title": "Doc", "doc_type": "executive_report"},
        False,
        SimpleNamespace(plan={}),
        None,
        None,
        None,
        content_holder,
        tool_holder,
    )

    assert regen_calls == 2
    assert content_holder[0].answer == "initial"
