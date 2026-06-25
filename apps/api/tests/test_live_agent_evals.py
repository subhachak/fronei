from __future__ import annotations

import base64
import json
import zipfile
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

from app.evals import live_runner
from app.services.agent.models import StreamEnvelope


class FakeRuntime:
    def run_stream(self, request, *, user_id):
        artifact_kind = request.output_format if request.output_format in {"docx", "pptx"} else None
        sources = [
            {"title": "One", "url": "https://example.com/one"},
            {"title": "Two", "url": "https://example.com/two"},
        ] if request.force_route == "research" else []
        answer = (
            "A detailed response with enough substance to satisfy the requested evaluation. " * 8
        )
        if sources:
            answer += "The release policy is documented [S1] and the current release is listed [S2]."
        yield StreamEnvelope(type="start", data={})
        yield StreamEnvelope(
            type="result",
            data={
                "route": request.force_route,
                "answer": answer,
                "model_used": "fake-live-model",
                "sources": sources,
                "artifacts": (
                    [{"kind": artifact_kind, "filename": f"result.{artifact_kind}"}]
                    if artifact_kind else []
                ),
                "tool_calls": [{"name": "web_search"}] if sources else [],
                "events": [],
                "latency_ms": 10,
                "cost_usd": 0.001,
            },
        )
        yield StreamEnvelope(type="done", data={})


def test_live_eval_runner_writes_report_without_network(monkeypatch, tmp_path):
    fixtures = tmp_path / "live.json"
    report = tmp_path / "report.json"
    fixtures.write_text(json.dumps([
        {
            "id": "mock-live",
            "category": "answer",
            "description": "Mocked live scenario",
            "request": {"message": "Explain retries.", "force_route": "direct"},
            "expected": {
                "route": "direct",
                "min_answer_chars": 100,
                "min_judge_score": 0.7,
                "max_latency_ms": 1000
            },
            "reserved_cost_usd": 0.01
        }
    ]))
    monkeypatch.setattr(live_runner, "Runtime", FakeRuntime)
    monkeypatch.setattr(
        live_runner.model_client,
        "simple_completion",
        lambda *_args, **_kwargs: SimpleNamespace(
            text='{"score": 0.9, "reason": "Complete and relevant."}',
            cost_usd=0.001,
        ),
    )

    payload = live_runner.run_live_evals(
        fixtures=fixtures,
        report_path=report,
        max_budget_usd=0.02,
        model="fake-model",
    )

    assert payload["passed"] == 1
    assert payload["reported_cost_usd"] == 0.002
    assert report.exists()


def test_default_live_fixture_scores_all_scenarios_with_mock_runtime(monkeypatch, tmp_path):
    monkeypatch.setattr(live_runner, "Runtime", FakeRuntime)
    monkeypatch.setattr(
        live_runner.model_client,
        "simple_completion",
        lambda *_args, **_kwargs: SimpleNamespace(
            text='{"score": 0.9, "reason": "Complete and relevant."}',
            cost_usd=0.001,
        ),
    )

    payload = live_runner.run_live_evals(
        report_path=tmp_path / "report.json",
        max_budget_usd=0.25,
        model="fake-model",
    )

    assert payload["scenario_count"] == 4
    assert payload["passed"] == 4
    assert payload["reserved_spend_usd"] == 0.24


def test_live_eval_rejects_fixture_reservations_over_budget(tmp_path):
    fixtures = tmp_path / "live.json"
    fixtures.write_text(json.dumps([
        {
            "id": "too-expensive",
            "category": "answer",
            "description": "Budget guard",
            "request": {"message": "Explain retries.", "force_route": "direct"},
            "expected": {"route": "direct"},
            "reserved_cost_usd": 0.2
        }
    ]))

    try:
        live_runner.run_live_evals(
            fixtures=fixtures,
            report_path=Path(tmp_path / "report.json"),
            max_budget_usd=0.1,
        )
    except ValueError as exc:
        assert "exceed live eval budget" in str(exc)
    else:
        raise AssertionError("Expected the budget guard to reject the fixture")


def test_live_eval_extracts_office_text_for_judging():
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "word/document.xml",
            "<w:document><w:p><w:t>Migration goals and success measures</w:t></w:p></w:document>",
        )
    artifact = {
        "kind": "docx",
        "base64_data": base64.b64encode(buffer.getvalue()).decode("ascii"),
    }

    assert "Migration goals and success measures" in live_runner._artifact_text([artifact])
