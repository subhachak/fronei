from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from string import Formatter
from typing import Any

from app.services.agent_runtime.registry import DEFAULTS_DIR, RuntimeRegistry


FIXTURES_DIR = DEFAULTS_DIR / "fixtures"
_ALLOWED_EXPECT_FIELDS = {"response_contains", "tool_called", "no_tool_called"}


@dataclass
class FixtureResult:
    scenario: str
    passed: bool
    failure_reason: str | None = None


@dataclass
class FixtureRunSummary:
    prompt_id: str
    total: int
    passed: int
    failed: int
    results: list[FixtureResult]

    @property
    def all_passed(self) -> bool:
        return self.failed == 0

    def model_dump(self) -> dict[str, Any]:
        return {
            "prompt_id": self.prompt_id,
            "total": self.total,
            "passed": self.passed,
            "failed": self.failed,
            "results": [
                {
                    "scenario": result.scenario,
                    "passed": result.passed,
                    "failure_reason": result.failure_reason,
                }
                for result in self.results
            ],
        }


class _SafeFormatDict(dict):
    def __missing__(self, key):
        raise KeyError(key)


class PromptFixtureRunner:
    def __init__(self, registry: RuntimeRegistry):
        self.registry = registry

    def run(self, prompt_id: str) -> FixtureRunSummary:
        prompt = self.registry.prompt(prompt_id)
        fixture_path = FIXTURES_DIR / f"{prompt_id}.json"
        if not fixture_path.exists():
            return _summary(prompt_id, [FixtureResult("fixture file exists", False, "fixture file not found")])

        try:
            raw = json.loads(fixture_path.read_text())
        except json.JSONDecodeError as exc:
            return _summary(prompt_id, [FixtureResult("fixture file parses", False, str(exc))])

        if not isinstance(raw, list):
            return _summary(prompt_id, [FixtureResult("fixture file shape", False, "fixture must be a list")])

        results = [self._run_one(prompt, item) for item in raw]
        return _summary(prompt_id, results)

    def _run_one(self, prompt, item: Any) -> FixtureResult:
        scenario = item.get("scenario", "unnamed") if isinstance(item, dict) else "invalid fixture"
        if not isinstance(item, dict):
            return FixtureResult(scenario, False, "fixture must be an object")
        fixture_input = item.get("input")
        expect = item.get("expect", {})
        if not isinstance(fixture_input, dict):
            return FixtureResult(scenario, False, "fixture input must be an object")
        if not isinstance(expect, dict):
            return FixtureResult(scenario, False, "fixture expect must be an object")

        unknown_expect = sorted(set(expect) - _ALLOWED_EXPECT_FIELDS)
        if unknown_expect:
            return FixtureResult(scenario, False, f"unknown expected fields: {', '.join(unknown_expect)}")

        missing_variables = [variable for variable in prompt.variables if variable not in fixture_input]
        if missing_variables:
            return FixtureResult(scenario, False, f"missing variables: {', '.join(missing_variables)}")

        try:
            self._render_prompt_text(prompt.system_prompt, fixture_input)
            if prompt.developer_prompt:
                self._render_prompt_text(prompt.developer_prompt, fixture_input)
        except KeyError as exc:
            return FixtureResult(scenario, False, f"missing render variable: {exc.args[0]}")
        except Exception as exc:
            return FixtureResult(scenario, False, f"prompt render failed: {exc}")

        return FixtureResult(scenario, True)

    def _render_prompt_text(self, text: str, values: dict[str, Any]) -> str:
        # Only validate Python-format placeholders when they exist; plain text prompts
        # render as-is, which keeps Phase C offline and side-effect free.
        fields = [field_name for _, field_name, _, _ in Formatter().parse(text) if field_name]
        if not fields:
            return text
        return text.format_map(_SafeFormatDict(values))


def _summary(prompt_id: str, results: list[FixtureResult]) -> FixtureRunSummary:
    passed = sum(1 for result in results if result.passed)
    failed = len(results) - passed
    return FixtureRunSummary(
        prompt_id=prompt_id,
        total=len(results),
        passed=passed,
        failed=failed,
        results=results,
    )
