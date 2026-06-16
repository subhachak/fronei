from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from app.services.agent_runtime.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerRegistry,
    CircuitOpen,
    CircuitState,
)
from app.services.agent_runtime.degradation import DegradationTier, resolve_tier
from app.services.agent_runtime.health_monitor import TraceHealthMonitor
from app.services.agent_runtime.models import JudgeResult
from app.services.agent_runtime.registry import _load_from_files
from app.services.agent_runtime.research_agent import ResearchAgent
from app.services.agent_runtime.document_agent import DocumentAgent
from app.services.agent_runtime.tracing import AgentTrace
from app.services.turn_graph.state import TurnGraphState


def test_circuit_breaker_opens_after_threshold_failures():
    breaker = CircuitBreaker("x", failure_threshold=2)
    for _ in range(2):
        with pytest.raises(ValueError):
            breaker.call(lambda: (_ for _ in ()).throw(ValueError("bad")))
    assert breaker.state == CircuitState.OPEN


def test_circuit_breaker_half_open_after_timeout():
    breaker = CircuitBreaker("x", failure_threshold=1, recovery_timeout=0.01)
    with pytest.raises(ValueError):
        breaker.call(lambda: (_ for _ in ()).throw(ValueError("bad")))
    time.sleep(0.02)
    assert breaker.state == CircuitState.HALF_OPEN


def test_circuit_breaker_closes_on_success_from_half_open():
    breaker = CircuitBreaker("x", failure_threshold=1, recovery_timeout=0.01)
    with pytest.raises(ValueError):
        breaker.call(lambda: (_ for _ in ()).throw(ValueError("bad")))
    time.sleep(0.02)
    assert breaker.call(lambda: "ok") == "ok"
    assert breaker.state == CircuitState.CLOSED


def test_circuit_breaker_raises_circuit_open_when_open():
    breaker = CircuitBreaker("x", failure_threshold=1)
    with pytest.raises(ValueError):
        breaker.call(lambda: (_ for _ in ()).throw(ValueError("bad")))
    with pytest.raises(CircuitOpen):
        breaker.call(lambda: "nope")


def test_circuit_breaker_registry_get_returns_singleton():
    assert CircuitBreakerRegistry.get() is CircuitBreakerRegistry.get()


def test_circuit_breaker_registry_reset_clears_all():
    registry = CircuitBreakerRegistry.get()
    registry.breaker("x")
    registry.reset()
    assert registry.items() == []


def test_degradation_tier_full_when_no_circuits_open():
    assert resolve_tier() == DegradationTier.FULL


def test_degradation_tier_degraded_research_when_web_search_open():
    breaker = CircuitBreakerRegistry.get().breaker("tool:web_search", failure_threshold=1)
    with pytest.raises(ValueError):
        breaker.call(lambda: (_ for _ in ()).throw(ValueError("bad")))
    assert resolve_tier() == DegradationTier.DEGRADED_RESEARCH


def test_degradation_tier_minimal_when_llm_circuit_open():
    breaker = CircuitBreakerRegistry.get().breaker("llm:test", failure_threshold=1)
    with pytest.raises(ValueError):
        breaker.call(lambda: (_ for _ in ()).throw(ValueError("bad")))
    assert resolve_tier() == DegradationTier.MINIMAL


def test_health_monitor_ingest_and_snapshot_latency():
    trace = AgentTrace("t")
    run = trace.start_run("agent")
    run.latency_ms = 100
    monitor = TraceHealthMonitor()
    monitor.ingest(trace)
    snap = monitor.snapshot()
    assert snap.p95_latency_ms == 100
    assert snap.sample_count == 1


def test_health_monitor_error_rate():
    trace = AgentTrace("t")
    run = trace.start_run("agent")
    run.status = "failed"
    monitor = TraceHealthMonitor()
    monitor.ingest(trace)
    assert monitor.snapshot().error_rate == 1.0


def test_best_seen_repair_emits_highest_score_not_last(monkeypatch):
    agent = ResearchAgent(_load_from_files())
    holder = [SimpleNamespace(answer="initial")]
    scores = iter([
        JudgeResult(id="j1", target_type="research", target_id="t", judge_agent_id="j", score=0.5, status="repair", required_repairs=[{"instruction": "fix"}], can_publish=False),
        JudgeResult(id="j2", target_type="research", target_id="t", judge_agent_id="j", score=0.8, status="repair", required_repairs=[{"instruction": "fix"}], can_publish=False),
        JudgeResult(id="j3", target_type="research", target_id="t", judge_agent_id="j", score=0.6, status="repair", required_repairs=[{"instruction": "fix"}], can_publish=False),
    ])
    repairs = iter([SimpleNamespace(answer="best"), SimpleNamespace(answer="worse")])
    monkeypatch.setattr("app.services.agent_runtime.judge_service.JudgeService.evaluate", lambda *a, **kw: next(scores))
    monkeypatch.setattr(agent, "_resynthesize_with_repairs", lambda *a, **kw: next(repairs))

    agent._judge_synthesis_loop(TurnGraphState(user_message="q", quality_mode="executive"), SimpleNamespace(), holder)

    assert holder[0].answer == "best"


def test_poison_detection_breaks_after_two_consecutive_regressions(monkeypatch):
    registry = _load_from_files()
    registry.judges["research_judge"].max_repair_iterations = 3
    agent = ResearchAgent(registry)
    holder = [SimpleNamespace(answer="initial")]
    scores = iter([
        JudgeResult(id="j1", target_type="research", target_id="t", judge_agent_id="j", score=0.8, status="repair", required_repairs=[{"instruction": "fix"}], can_publish=False),
        JudgeResult(id="j2", target_type="research", target_id="t", judge_agent_id="j", score=0.6, status="repair", required_repairs=[{"instruction": "fix"}], can_publish=False),
        JudgeResult(id="j3", target_type="research", target_id="t", judge_agent_id="j", score=0.5, status="repair", required_repairs=[{"instruction": "fix"}], can_publish=False),
    ])
    calls = {"n": 0}

    def repair(*args, **kwargs):
        calls["n"] += 1
        return SimpleNamespace(answer=f"repair {calls['n']}")

    monkeypatch.setattr("app.services.agent_runtime.judge_service.JudgeService.evaluate", lambda *a, **kw: next(scores))
    monkeypatch.setattr(agent, "_resynthesize_with_repairs", repair)
    agent._judge_synthesis_loop(TurnGraphState(user_message="q", quality_mode="executive"), SimpleNamespace(), holder)

    assert calls["n"] == 2


def test_suggested_strategy_routing_injects_override(monkeypatch):
    captured: dict[str, str] = {}

    class FakeSubAgent:
        def __init__(self, agent_id, registry):
            pass

        def invoke(self, **kwargs):
            captured["doc_context"] = kwargs.get("doc_context", "")
            return SimpleNamespace(answer="# fixed", latency_ms=1, estimated_cost_usd=0, model_used="m")

        def run_tool(self, *args, **kwargs):
            return SimpleNamespace(output={"docx_base64": "DOCX", "filename": "doc.docx"}, latency_ms=1)

    monkeypatch.setattr("app.services.agent_runtime.sub_agent_runner.SubAgentRunner", FakeSubAgent)
    agent = DocumentAgent(_load_from_files())
    agent._regenerate_with_repairs(
        TurnGraphState(user_message="make doc"),
        {"title": "Doc", "doc_type": "executive_report"},
        [{"instruction": "cite"}],
        False,
        SimpleNamespace(plan={}),
        None,
        None,
        None,
        suggested_strategy="add_citations",
    )

    assert "add citations" in captured["doc_context"].lower()
