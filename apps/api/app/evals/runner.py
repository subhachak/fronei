from __future__ import annotations

import argparse
import json
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

from pydantic import BaseModel, Field

from app.services.agent import model_client, routing_policy
from app.services.agent.fast_path import FastPathDecision, decide_fast_path
from app.services.agent.models import TurnRequest
from app.services.agent.orchestrator import OrchestratorDecision, decide_with_options

DEFAULT_FIXTURE_PATH = Path(__file__).resolve().parents[2] / "evals" / "golden_turns.json"
AVAILABLE_ROUTES = ["direct", "clarify", "research", "document", "research_document"]
AVAILABLE_TOOLS = [
    "web_search",
    "read_url",
    "make_markdown_artifact",
    "make_docx_artifact",
    "make_pptx_artifact",
]


class ExpectedOutcome(BaseModel):
    fast_path: str
    route: str
    research_level: str = "regular"
    requires_confirmation: bool = False
    output_format: str = "chat"
    matched_signal_groups: list[str] = Field(default_factory=list)
    web_query_contains: str | None = None


class GoldenScenario(BaseModel):
    id: str
    category: str
    description: str
    request: TurnRequest
    fast_router_candidate: dict[str, Any] | None = None
    orchestrator_candidate: dict[str, Any] | None = None
    expected: ExpectedOutcome


@dataclass(frozen=True)
class ScenarioResult:
    scenario_id: str
    category: str
    passed: bool
    failures: tuple[str, ...]
    actual: dict[str, Any]


def load_scenarios(path: Path = DEFAULT_FIXTURE_PATH) -> list[GoldenScenario]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    scenarios = [GoldenScenario.model_validate(item) for item in payload]
    ids = [scenario.id for scenario in scenarios]
    if len(ids) != len(set(ids)):
        raise ValueError("Golden scenario IDs must be unique")
    return scenarios


def evaluate_scenario(scenario: GoldenScenario) -> ScenarioResult:
    with _deterministic_dependencies(scenario):
        fast_decision = decide_fast_path(scenario.request)
        orchestrator_decision = _orchestrator_decision(scenario, fast_decision)

    actual = _actual_outcome(scenario.request, fast_decision, orchestrator_decision)
    failures = _compare(scenario.expected, actual)
    return ScenarioResult(
        scenario_id=scenario.id,
        category=scenario.category,
        passed=not failures,
        failures=tuple(failures),
        actual=actual,
    )


def run_evals(path: Path = DEFAULT_FIXTURE_PATH) -> list[ScenarioResult]:
    return [evaluate_scenario(scenario) for scenario in load_scenarios(path)]


def assert_evals_pass(path: Path = DEFAULT_FIXTURE_PATH) -> list[ScenarioResult]:
    results = run_evals(path)
    failed = [result for result in results if not result.passed]
    if failed:
        details = "\n".join(
            f"- {result.scenario_id}: {'; '.join(result.failures)}"
            for result in failed
        )
        raise AssertionError(f"{len(failed)} golden scenario(s) failed:\n{details}")
    return results


def _orchestrator_decision(
    scenario: GoldenScenario,
    fast_decision: FastPathDecision,
) -> OrchestratorDecision | None:
    if fast_decision.path != "agentic":
        return None
    return decide_with_options(
        scenario.request,
        available_routes=AVAILABLE_ROUTES,
        available_tools=AVAILABLE_TOOLS,
    )


def _actual_outcome(
    request: TurnRequest,
    fast_decision: FastPathDecision,
    orchestrator_decision: OrchestratorDecision | None,
) -> dict[str, Any]:
    if fast_decision.path == "direct_fast":
        route = "direct"
        research_level = "regular"
        requires_confirmation = False
    elif fast_decision.path == "web_fast":
        route = "research"
        research_level = "easy"
        requires_confirmation = False
    else:
        if orchestrator_decision is None:
            raise AssertionError("Agentic scenarios require an orchestrator decision")
        route = orchestrator_decision.route
        research_level = orchestrator_decision.research_level
        requires_confirmation = orchestrator_decision.requires_confirmation
    return {
        "fast_path": fast_decision.path,
        "route": route,
        "research_level": research_level,
        "requires_confirmation": requires_confirmation,
        "output_format": request.output_format,
        "matched_signal_groups": sorted(fast_decision.matched_signal_groups),
        "web_query": fast_decision.web_query,
    }


def _compare(expected: ExpectedOutcome, actual: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    for field in ("fast_path", "route", "research_level", "requires_confirmation", "output_format"):
        expected_value = getattr(expected, field)
        actual_value = actual[field]
        if actual_value != expected_value:
            failures.append(f"{field}: expected {expected_value!r}, got {actual_value!r}")
    for group in expected.matched_signal_groups:
        if group not in actual["matched_signal_groups"]:
            failures.append(
                f"matched_signal_groups: expected {group!r} in {actual['matched_signal_groups']!r}"
            )
    if expected.web_query_contains:
        query = str(actual.get("web_query") or "").lower()
        if expected.web_query_contains.lower() not in query:
            failures.append(
                f"web_query: expected to contain {expected.web_query_contains!r}, got {query!r}"
            )
    return failures


@contextmanager
def _deterministic_dependencies(scenario: GoldenScenario) -> Iterator[None]:
    original_complete = model_client.complete
    original_candidate_matches = routing_policy._approved_candidate_matches

    def fake_complete(
        _messages,
        *,
        role: str | None = None,
        **_kwargs,
    ) -> SimpleNamespace:
        if role == "fast_router":
            candidate = scenario.fast_router_candidate
        elif role == "orchestrator":
            candidate = scenario.orchestrator_candidate
        else:
            raise AssertionError(f"Unexpected model role in routing eval: {role}")
        if candidate is None:
            raise RuntimeError(f"Scenario {scenario.id} requested deterministic model fallback")
        return SimpleNamespace(
            text=json.dumps(candidate),
            model_used=f"eval-{role}",
            latency_ms=1,
            cost_usd=0.0,
        )

    model_client.complete = fake_complete
    routing_policy._approved_candidate_matches = lambda _text: []
    try:
        yield
    finally:
        model_client.complete = original_complete
        routing_policy._approved_candidate_matches = original_candidate_matches


def _print_results(results: list[ScenarioResult], *, github: bool) -> None:
    for result in results:
        if result.passed:
            print(f"PASS {result.scenario_id} [{result.category}]")
            continue
        for failure in result.failures:
            if github:
                print(f"::error title=Agent eval {result.scenario_id}::{failure}")
            else:
                print(f"FAIL {result.scenario_id} [{result.category}]: {failure}")
    passed = sum(result.passed for result in results)
    print(f"\nAgent evals: {passed}/{len(results)} passed")
    _print_per_category_metrics(results)


def _print_per_category_metrics(results: list[ScenarioResult]) -> None:
    from collections import defaultdict

    by_category: dict[str, list[ScenarioResult]] = defaultdict(list)
    for r in results:
        by_category[r.category].append(r)

    col = "{:<32} {:>6} {:>6} {:>6} {:>6}"
    print()
    print(col.format("category", "total", "pass", "prec", "recall"))
    print("-" * 60)
    for cat in sorted(by_category):
        cat_results = by_category[cat]
        total = len(cat_results)
        n_pass = sum(r.passed for r in cat_results)
        # Within-category precision = passed / total (no false positive concept at this level)
        # For cross-category false-positive rate we track expected vs actual category outcomes.
        # Since routing evals don't have a single binary label for precision/recall in the
        # standard sense, we report pass rate and flag failure count.
        n_fail = total - n_pass
        pct = f"{n_pass / total:.0%}" if total else "n/a"
        fail_str = f"{n_fail} fail" if n_fail else "ok"
        print(f"  {cat:<30} {total:>5}  {n_pass:>5}  {pct:>5}  {fail_str}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run deterministic Fronei golden scenarios")
    parser.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURE_PATH)
    parser.add_argument("--format", choices=("text", "github"), default="text")
    args = parser.parse_args()
    results = run_evals(args.fixtures)
    _print_results(results, github=args.format == "github")
    return 0 if all(result.passed for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
