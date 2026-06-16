from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.services.agent_runtime.job_checkpoint import JobCheckpoint, resume_tier
from app.services.agent_runtime.registry import _load_from_files
from app.services.agent_runtime.research_agent import ResearchAgent
from app.services.agent_runtime.document_agent import DocumentAgent
from app.services.turn_graph.state import TurnGraphState


def test_job_checkpoint_save_and_load(monkeypatch):
    store: dict[tuple[str, str], str] = {}
    monkeypatch.setattr(JobCheckpoint, "_write", lambda self, t, s, p: store.__setitem__((t, s), p))
    monkeypatch.setattr(JobCheckpoint, "_read", lambda self, t, s: store.get((t, s)))

    cp = JobCheckpoint()
    cp.save("t1", "stage", {"x": 1}, score=0.7)
    payload, score = cp.load("t1", "stage")

    assert payload == {"x": 1}
    assert score == 0.7


def test_job_checkpoint_overwrite_existing(monkeypatch):
    store: dict[tuple[str, str], str] = {}
    monkeypatch.setattr(JobCheckpoint, "_write", lambda self, t, s, p: store.__setitem__((t, s), p))
    monkeypatch.setattr(JobCheckpoint, "_read", lambda self, t, s: store.get((t, s)))

    cp = JobCheckpoint()
    cp.save("t1", "stage", {"x": 1}, score=0.7)
    cp.save("t1", "stage", {"x": 2}, score=0.9)

    assert cp.load("t1", "stage") == ({"x": 2}, 0.9)


def test_job_checkpoint_load_missing_returns_none(monkeypatch):
    monkeypatch.setattr(JobCheckpoint, "_read", lambda self, t, s: None)
    assert JobCheckpoint().load("missing", "stage") == (None, None)


def test_job_checkpoint_load_score_below_min_returns_not_trusted():
    assert not JobCheckpoint().should_trust(0.59)
    assert JobCheckpoint().should_trust(0.6)


def test_job_checkpoint_save_failure_does_not_raise(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(JobCheckpoint, "_write", boom)
    JobCheckpoint().save("t1", "stage", {"x": 1})


def test_job_checkpoint_clear(monkeypatch):
    cleared: list[str] = []
    monkeypatch.setattr(JobCheckpoint, "_delete_all", lambda self, t: cleared.append(t))
    JobCheckpoint().clear("t1")
    assert cleared == ["t1"]


def test_resume_tier_selects_most_advanced_trusted_checkpoint(monkeypatch):
    data = {
        "research.synthesis_complete": json.dumps({"payload": {"a": 1}, "score": 0.8}),
        "document.plan_complete": json.dumps({"payload": {"b": 2}, "score": 0.9}),
    }
    monkeypatch.setattr(JobCheckpoint, "_read", lambda self, t, s: data.get(s))
    assert resume_tier("t1") == "document.plan_complete"


def test_research_agent_saves_checkpoint_after_crawl(monkeypatch):
    saved: list[str] = []
    monkeypatch.setattr(JobCheckpoint, "save", lambda self, turn_id, stage, payload, **kw: saved.append(stage))
    monkeypatch.setattr(JobCheckpoint, "clear", lambda self, turn_id: None)
    monkeypatch.setattr(JobCheckpoint, "load", lambda self, turn_id, stage: (None, None))
    monkeypatch.setattr("app.services.llm_gateway.invoke_llm", lambda **kw: SimpleNamespace(
        answer='{"search_queries":["q"],"claims":[{"text":"Claim","source_url":"https://example.com","confidence":0.8}]}',
        model_used="m",
        latency_ms=1,
        estimated_cost_usd=0,
    ))
    monkeypatch.setattr("app.services.web_context.search_web_sources", lambda query, recency=None: ("p", [SimpleNamespace(title="T", url="https://example.com", content="c")]))
    monkeypatch.setattr("app.services.web_context.crawl_url", lambda url: SimpleNamespace(title="T", url=url, content="body"))

    ResearchAgent(_load_from_files()).run(TurnGraphState(user_message="research", turn_id="t1"), SimpleNamespace(plan={}))

    assert "research.crawl_complete" in saved


def test_document_agent_saves_checkpoint_after_plan(monkeypatch):
    saved: list[str] = []
    monkeypatch.setattr(JobCheckpoint, "save", lambda self, turn_id, stage, payload, **kw: saved.append(stage))
    monkeypatch.setattr(JobCheckpoint, "clear", lambda self, turn_id: None)
    monkeypatch.setattr("app.services.llm_gateway.invoke_llm_json", lambda messages, route: SimpleNamespace(
        answer='{"document_brief":{"title":"Doc","doc_type":"executive_report"}}',
        model_used="m",
        latency_ms=1,
        estimated_cost_usd=0,
    ))
    monkeypatch.setattr("app.services.llm_gateway.invoke_llm", lambda **kw: SimpleNamespace(answer="# Doc", model_used="m", latency_ms=1, estimated_cost_usd=0))
    monkeypatch.setattr("app.services.agent_runtime.tool_runner.ToolRunner.run", lambda *a, **kw: SimpleNamespace(output={"docx_base64": "DOCX", "filename": "doc.docx"}, latency_ms=1))
    monkeypatch.setattr("app.services.agent_runtime.judge_service.JudgeService.evaluate", lambda *a, **kw: SimpleNamespace(status="pass", score=0.9, required_repairs=[], suggested_strategy=None))

    DocumentAgent(_load_from_files()).run(TurnGraphState(user_message="make doc", turn_id="t1"), SimpleNamespace(plan={}))

    assert "document.plan_complete" in saved
    assert "document.generate_complete" in saved


def test_resume_from_synthesis_checkpoint_skips_research(monkeypatch):
    payload = {"research_answer": "Checkpoint answer", "research_claims": [], "research_sources": []}
    monkeypatch.setattr(JobCheckpoint, "load", lambda self, turn_id, stage: (payload, 0.9) if stage == "research.synthesis_complete" else (None, None))
    monkeypatch.setattr(JobCheckpoint, "clear", lambda self, turn_id: None)

    def should_not_call(*args, **kwargs):
        raise AssertionError("tool should not run")

    monkeypatch.setattr("app.services.agent_runtime.sub_agent_runner.SubAgentRunner.run_tool", should_not_call)
    result = ResearchAgent(_load_from_files()).run(TurnGraphState(user_message="research", turn_id="t1"), SimpleNamespace(plan={}))

    assert result.answer == "Checkpoint answer"


def test_resume_from_document_generate_checkpoint_skips_generation(monkeypatch):
    payload = {
        "document_brief": {"title": "Deck", "doc_type": "presentation"},
        "document_content": "# Deck\n## Slide 1",
        "pptx_base64": "PPTX",
        "filename": "deck.pptx",
    }
    monkeypatch.setattr(JobCheckpoint, "load", lambda self, turn_id, stage: (payload, 0.9) if stage == "document.generate_complete" else (None, None))
    monkeypatch.setattr(JobCheckpoint, "clear", lambda self, turn_id: None)
    monkeypatch.setattr("app.services.agent_runtime.judge_service.JudgeService.evaluate", lambda *a, **kw: SimpleNamespace(status="pass", score=0.9, required_repairs=[], suggested_strategy=None))

    def should_not_call(*args, **kwargs):
        raise AssertionError("generation should be skipped")

    agent = DocumentAgent(_load_from_files())
    monkeypatch.setattr(agent, "_plan", should_not_call)
    monkeypatch.setattr(agent, "_generate_content", should_not_call)
    state = TurnGraphState(user_message="make deck", turn_id="t1")

    result = agent.run(state, SimpleNamespace(plan={}))

    assert state.checkpoint_key == "document.generate_complete"
    assert result.markdown == "# Deck\n## Slide 1"
    assert result.pptx_base64 == "PPTX"
    assert result.filename == "deck.pptx"


def test_resume_from_document_plan_checkpoint_skips_planning(monkeypatch):
    plan_payload = {"document_brief": {"title": "Doc", "doc_type": "executive_report"}}
    monkeypatch.setattr(JobCheckpoint, "load", lambda self, turn_id, stage: (plan_payload, 0.9) if stage == "document.plan_complete" else (None, None))
    monkeypatch.setattr(JobCheckpoint, "save", lambda *a, **kw: None)
    monkeypatch.setattr(JobCheckpoint, "clear", lambda self, turn_id: None)
    monkeypatch.setattr("app.services.agent_runtime.tool_runner.ToolRunner.run", lambda *a, **kw: SimpleNamespace(output={"docx_base64": "DOCX", "filename": "doc.docx"}, latency_ms=1))
    monkeypatch.setattr("app.services.agent_runtime.judge_service.JudgeService.evaluate", lambda *a, **kw: SimpleNamespace(status="pass", score=0.9, required_repairs=[], suggested_strategy=None))

    agent = DocumentAgent(_load_from_files())
    monkeypatch.setattr(agent, "_plan", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("planning should be skipped")))
    monkeypatch.setattr(agent, "_judge_plan_loop", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("plan judge should be skipped")))
    monkeypatch.setattr(agent, "_generate_content", lambda *a, **kw: SimpleNamespace(answer="# Doc", model_used="m", latency_ms=1, estimated_cost_usd=0))
    state = TurnGraphState(user_message="make doc", turn_id="t1")

    result = agent.run(state, SimpleNamespace(plan={}))

    assert state.checkpoint_key == "document.plan_complete"
    assert result.docx_base64 == "DOCX"
    assert result.title == "Doc"
