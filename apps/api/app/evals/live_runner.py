from __future__ import annotations

import argparse
import base64
import html
import json
import os
import re
import time
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from app.services.agent import model_client
from app.services.agent.models import StreamEnvelope, TurnRequest
from app.services.agent.runtime import Runtime

DEFAULT_FIXTURE_PATH = Path(__file__).resolve().parents[2] / "evals" / "live_quality.json"
DEFAULT_REPORT_PATH = Path("artifacts/live-eval-report.json")
DEFAULT_BUDGET_USD = 0.25


class LiveExpected(BaseModel):
    route: str
    min_answer_chars: int = 120
    min_sources: int = 0
    min_citations: int = 0
    artifact_kind: str | None = None
    min_judge_score: float = Field(default=0.7, ge=0.0, le=1.0)
    max_latency_ms: int = 180_000


class LiveScenario(BaseModel):
    id: str
    category: str
    description: str
    request: TurnRequest
    expected: LiveExpected
    reserved_cost_usd: float = Field(gt=0.0)


@dataclass
class LiveScenarioResult:
    scenario_id: str
    category: str
    passed: bool
    checks: dict[str, bool]
    failures: list[str]
    route: str
    model_used: str
    answer_chars: int
    source_count: int
    citation_count: int
    invalid_citations: list[str]
    artifact_kinds: list[str]
    tool_calls: list[str]
    fallback_count: int
    latency_ms: int
    reported_cost_usd: float
    judge_score: float
    judge_reason: str
    error: str | None = None


def load_live_scenarios(path: Path = DEFAULT_FIXTURE_PATH) -> list[LiveScenario]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    scenarios = [LiveScenario.model_validate(item) for item in payload]
    ids = [scenario.id for scenario in scenarios]
    if len(ids) != len(set(ids)):
        raise ValueError("Live scenario IDs must be unique")
    return scenarios


def run_live_evals(
    *,
    fixtures: Path = DEFAULT_FIXTURE_PATH,
    report_path: Path = DEFAULT_REPORT_PATH,
    max_budget_usd: float = DEFAULT_BUDGET_USD,
    model: str | None = None,
) -> dict[str, Any]:
    scenarios = load_live_scenarios(fixtures)
    reserved_total = sum(scenario.reserved_cost_usd for scenario in scenarios)
    if reserved_total > max_budget_usd:
        raise ValueError(
            f"Fixture reservations (${reserved_total:.4f}) exceed live eval budget "
            f"(${max_budget_usd:.4f})"
        )

    results: list[LiveScenarioResult] = []
    reported_cost = 0.0
    reserved_spend = 0.0
    for scenario in scenarios:
        if reserved_spend + scenario.reserved_cost_usd > max_budget_usd:
            break
        reserved_spend += scenario.reserved_cost_usd
        result = evaluate_live_scenario(scenario, model=model)
        results.append(result)
        reported_cost += result.reported_cost_usd
        if reported_cost >= max_budget_usd:
            break

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model_override": model,
        "budget_usd": max_budget_usd,
        "reserved_spend_usd": round(reserved_spend, 6),
        "reported_cost_usd": round(reported_cost, 6),
        "scenario_count": len(results),
        "passed": sum(result.passed for result in results),
        "failed": sum(not result.passed for result in results),
        "results": [asdict(result) for result in results],
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def evaluate_live_scenario(scenario: LiveScenario, *, model: str | None = None) -> LiveScenarioResult:
    request = _with_model_override(scenario.request, model)
    started = time.perf_counter()
    try:
        envelopes = list(Runtime().run_stream(request, user_id="live-eval"))
        result = _result_envelope(envelopes)
        measured_latency = int((time.perf_counter() - started) * 1000)
        return _score_result(scenario, result, envelopes, measured_latency, model=model)
    except Exception as exc:
        return LiveScenarioResult(
            scenario_id=scenario.id,
            category=scenario.category,
            passed=False,
            checks={},
            failures=[f"runtime error: {exc}"],
            route="",
            model_used="",
            answer_chars=0,
            source_count=0,
            citation_count=0,
            invalid_citations=[],
            artifact_kinds=[],
            tool_calls=[],
            fallback_count=0,
            latency_ms=int((time.perf_counter() - started) * 1000),
            reported_cost_usd=0.0,
            judge_score=0.0,
            judge_reason="Runtime did not produce a result.",
            error=str(exc)[:500],
        )


def _score_result(
    scenario: LiveScenario,
    result: dict[str, Any],
    envelopes: list[StreamEnvelope],
    measured_latency_ms: int,
    *,
    model: str | None,
) -> LiveScenarioResult:
    answer = str(result.get("answer") or "")
    sources = list(result.get("sources") or [])
    artifacts = list(result.get("artifacts") or [])
    tool_calls = list(result.get("tool_calls") or [])
    citations = sorted(set(re.findall(r"\[S(\d+)\]", answer)))
    invalid_citations = [
        f"S{number}"
        for number in citations
        if int(number) < 1 or int(number) > len(sources)
    ]
    fallback_count = sum(
        len((event.get("data") or {}).get("failed_model_attempts") or [])
        for event in (envelope.data for envelope in envelopes if envelope.type == "progress")
    )
    judge_score, judge_reason, judge_cost = _judge_output(scenario, result, model=model)
    latency_ms = max(measured_latency_ms, int(result.get("latency_ms") or 0))
    expected = scenario.expected
    artifact_kinds = [str(item.get("kind") or "") for item in artifacts]
    checks = {
        "route": result.get("route") == expected.route,
        "answer_length": len(answer) >= expected.min_answer_chars,
        "sources": len(sources) >= expected.min_sources,
        "citations": len(citations) >= expected.min_citations and not invalid_citations,
        "artifact": expected.artifact_kind is None or expected.artifact_kind in artifact_kinds,
        "latency": latency_ms <= expected.max_latency_ms,
        "judge": judge_score >= expected.min_judge_score,
    }
    failures = [name for name, passed in checks.items() if not passed]
    return LiveScenarioResult(
        scenario_id=scenario.id,
        category=scenario.category,
        passed=not failures,
        checks=checks,
        failures=failures,
        route=str(result.get("route") or ""),
        model_used=str(result.get("model_used") or ""),
        answer_chars=len(answer),
        source_count=len(sources),
        citation_count=len(citations),
        invalid_citations=invalid_citations,
        artifact_kinds=artifact_kinds,
        tool_calls=[str(item.get("name") or "") for item in tool_calls],
        fallback_count=fallback_count,
        latency_ms=latency_ms,
        reported_cost_usd=round(float(result.get("cost_usd") or 0.0) + judge_cost, 6),
        judge_score=judge_score,
        judge_reason=judge_reason,
    )


def _judge_output(
    scenario: LiveScenario,
    result: dict[str, Any],
    *,
    model: str | None,
) -> tuple[float, str, float]:
    response = model_client.simple_completion(
        """You are a strict evaluator. Score whether the response fulfills the request.
Return only JSON: {"score": 0.0-1.0, "reason": "one short sentence"}.
Penalize unsupported claims, missing requested deliverables, vague answers, and obvious incompleteness.""",
        json.dumps(
            {
                "request": scenario.request.message,
                "expected_route": scenario.expected.route,
                "answer": str(result.get("answer") or "")[:8000],
                "source_count": len(result.get("sources") or []),
                "artifact_kinds": [item.get("kind") for item in (result.get("artifacts") or [])],
                "artifact_text": _artifact_text(result.get("artifacts") or [])[:8000],
            },
            ensure_ascii=False,
        ),
        preferred_model=model,
        role="direct_answer",
        max_tokens=180,
        timeout_s=30,
    )
    try:
        payload = json.loads(response.text)
        score = max(0.0, min(1.0, float(payload.get("score") or 0.0)))
        reason = str(payload.get("reason") or "")[:300]
    except (TypeError, ValueError, json.JSONDecodeError):
        score = 0.0
        reason = f"Judge returned invalid JSON: {response.text[:200]}"
    return score, reason, response.cost_usd


def _artifact_text(artifacts: list[dict[str, Any]]) -> str:
    chunks: list[str] = []
    for artifact in artifacts:
        encoded = str(artifact.get("base64_data") or "")
        if not encoded:
            continue
        try:
            with zipfile.ZipFile(BytesIO(base64.b64decode(encoded))) as archive:
                for name in archive.namelist():
                    if not (
                        name == "word/document.xml"
                        or name.startswith("ppt/slides/slide") and name.endswith(".xml")
                    ):
                        continue
                    xml = archive.read(name).decode("utf-8", errors="ignore")
                    text = re.sub(r"<[^>]+>", " ", xml)
                    chunks.append(html.unescape(re.sub(r"\s+", " ", text)).strip())
        except (ValueError, zipfile.BadZipFile):
            continue
    return "\n".join(chunk for chunk in chunks if chunk)


def _with_model_override(request: TurnRequest, model: str | None) -> TurnRequest:
    if not model:
        return request
    roles = (
        "fast_router",
        "orchestrator",
        "direct_answer",
        "research_brief",
        "coverage_contract",
        "research_planner",
        "reflection",
        "citation_verifier",
        "repair",
        "document_planner",
        "document_writer",
        "synthesis",
        "synthesis_executive",
    )
    return request.model_copy(update={"model_overrides": {role: model for role in roles}})


def _result_envelope(envelopes: list[StreamEnvelope]) -> dict[str, Any]:
    result = next((envelope.data for envelope in envelopes if envelope.type == "result"), None)
    if result is None:
        error = next((envelope.data for envelope in envelopes if envelope.type == "error"), {})
        raise RuntimeError(str(error.get("detail") or error.get("message") or "No result envelope"))
    return result


def _print_summary(report: dict[str, Any], *, github: bool) -> None:
    for result in report["results"]:
        status = "PASS" if result["passed"] else "FAIL"
        print(
            f"{status} {result['scenario_id']} route={result['route']} "
            f"score={result['judge_score']:.2f} latency={result['latency_ms']}ms "
            f"cost=${result['reported_cost_usd']:.4f}"
        )
        if github and not result["passed"]:
            failures = ", ".join(result["failures"])
            print(f"::error title=Live agent eval {result['scenario_id']}::{failures}")
    print(
        f"\nLive evals: {report['passed']}/{report['scenario_count']} passed; "
        f"reported cost ${report['reported_cost_usd']:.4f}; "
        f"reserved ${report['reserved_spend_usd']:.4f}/${report['budget_usd']:.4f}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Run opt-in live Fronei quality evaluations")
    parser.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURE_PATH)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT_PATH)
    parser.add_argument(
        "--max-budget-usd",
        type=float,
        default=float(os.environ.get("LIVE_EVAL_MAX_BUDGET_USD", DEFAULT_BUDGET_USD)),
    )
    parser.add_argument("--model", default=os.environ.get("LIVE_EVAL_MODEL") or None)
    parser.add_argument("--format", choices=("text", "github"), default="text")
    args = parser.parse_args()
    report = run_live_evals(
        fixtures=args.fixtures,
        report_path=args.report,
        max_budget_usd=args.max_budget_usd,
        model=args.model,
    )
    _print_summary(report, github=args.format == "github")
    return 0 if report["failed"] == 0 and report["scenario_count"] > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
