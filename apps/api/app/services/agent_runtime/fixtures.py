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

    def run(self, prompt_id: str, *, live: bool = False) -> FixtureRunSummary:
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

        results = [self._run_one(prompt, item, live=live) for item in raw]
        return _summary(prompt_id, results)

    def _run_one(self, prompt, item: Any, *, live: bool = False) -> FixtureResult:
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

        if not live:
            return FixtureResult(scenario, True)
        return self._run_live(prompt, scenario, fixture_input, expect)

    def _run_live(
        self,
        prompt,
        scenario: str,
        fixture_input: dict[str, Any],
        expect: dict[str, Any],
    ) -> FixtureResult:
        from app.services.agent_runtime.adapters import model_policy_to_route
        from app.services.llm_gateway import invoke_llm_json

        try:
            agent_def = next(
                (
                    agent for agent in self.registry.agents.values()
                    if agent.prompt_template_id == prompt.id
                ),
                None,
            )
            if agent_def is None:
                agent_def = self.registry.agent(prompt.agent_id)
            model_policy = self.registry.model_policy(agent_def.model_policy_id)
        except KeyError as exc:
            return FixtureResult(scenario, False, f"cannot resolve model policy: {exc}")

        is_claude = model_policy.primary_model.startswith("claude")
        messages: list[dict[str, str]] = [
            {"role": "system", "content": self._render_prompt_text(prompt.system_prompt, fixture_input)}
        ]
        if prompt.developer_prompt:
            messages.append({
                "role": "developer" if is_claude else "system",
                "content": self._render_prompt_text(prompt.developer_prompt, fixture_input),
            })
        messages.append({"role": "user", "content": json.dumps(fixture_input)})

        try:
            result = invoke_llm_json(messages, model_policy_to_route(model_policy))
        except Exception as exc:
            return FixtureResult(scenario, False, f"model call failed: {exc}")

        try:
            parsed = json.loads(result.answer)
        except (json.JSONDecodeError, ValueError, TypeError):
            parsed = None

        for field_name, expected_value in expect.items():
            failure = _evaluate_expect(field_name, expected_value, result.answer, parsed)
            if failure:
                return FixtureResult(scenario, False, failure)
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


def _evaluate_expect(
    field_name: str,
    expected_value: Any,
    raw_answer: str,
    parsed: dict | None,
) -> str | None:
    if field_name == "response_contains":
        expected_values = expected_value if isinstance(expected_value, list) else [expected_value]
        missing = [value for value in expected_values if str(value).lower() not in raw_answer.lower()]
        if missing:
            return f"response_contains: {missing!r} not found in model response"
        return None

    selected_tools = (parsed or {}).get("selected_tools") or [] if isinstance(parsed, dict) else []
    if field_name == "tool_called":
        if expected_value not in selected_tools:
            return f"tool_called: {expected_value!r} not in selected_tools={selected_tools!r}"
    elif field_name == "no_tool_called":
        disallowed_values = expected_value if isinstance(expected_value, list) else [expected_value]
        found = [value for value in disallowed_values if value in selected_tools]
        if found:
            return f"no_tool_called: {found!r} found in selected_tools={selected_tools!r}"
    return None
