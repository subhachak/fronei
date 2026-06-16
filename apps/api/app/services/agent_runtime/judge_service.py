from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from app.services.agent_runtime.models import JudgePolicy, JudgeResult, JudgeStatus
from app.services.agent_runtime.registry import RuntimeRegistry


logger = logging.getLogger(__name__)


class JudgeService:
    """LLM-as-judge evaluator.

    Mirrors the GuardrailService pattern: reads policy from registry, runs a
    bounded LLM call, and returns a structured JudgeResult. It never raises;
    failures produce a failing JudgeResult that can be logged or repaired by a
    later phase.
    """

    def __init__(self, registry: RuntimeRegistry) -> None:
        self.registry = registry

    def evaluate(
        self,
        policy_id: str,
        *,
        content: str,
        context: dict[str, Any] | None = None,
        target_id: str | None = None,
    ) -> JudgeResult:
        """Evaluate content against a named judge policy."""

        policy = self._get_policy(policy_id)
        if policy is None or not policy.enabled:
            return self._skip_result(policy_id, target_id)

        try:
            return self._run_judge(policy, content, context or {}, target_id or "")
        except Exception:
            logger.exception("JudgeService.evaluate failed for policy=%r", policy_id)
            return JudgeResult(
                id=str(uuid.uuid4()),
                target_type=policy.target_type,
                target_id=target_id or "",
                judge_agent_id=policy_id,
                score=0.0,
                status="fail",
                issues=[{"type": "judge_error", "message": "Judge call failed; treating as fail."}],
                required_repairs=[],
                can_publish=False,
            )

    def _get_policy(self, policy_id: str) -> JudgePolicy | None:
        try:
            return self.registry.judge(policy_id)
        except KeyError:
            logger.warning("JudgeService: unknown policy_id=%r", policy_id)
            return None

    def _skip_result(self, policy_id: str, target_id: str | None) -> JudgeResult:
        return JudgeResult(
            id=str(uuid.uuid4()),
            target_type="answer",
            target_id=target_id or "",
            judge_agent_id=policy_id,
            score=1.0,
            status="pass",
            issues=[],
            required_repairs=[],
            can_publish=True,
        )

    def _run_judge(
        self,
        policy: JudgePolicy,
        content: str,
        context: dict[str, Any],
        target_id: str,
    ) -> JudgeResult:
        from app.services.agent_runtime.adapters import model_policy_to_route
        from app.services.llm_gateway import invoke_llm

        model_policy = self.registry.model_policy(policy.model_policy_id)
        criteria_block = "\n".join(f"{index + 1}. {criterion}" for index, criterion in enumerate(policy.criteria))
        user_question = context.get("user_question", "")
        sources_block = context.get("sources_summary", "")

        prompt_parts = [
            f"User question: {user_question}" if user_question else "",
            f"Sources used:\n{sources_block}" if sources_block else "",
            f"Content to evaluate:\n{content}",
        ]
        prompt = "\n\n".join(part for part in prompt_parts if part)

        result = invoke_llm(
            message=prompt,
            route=model_policy_to_route(model_policy),
            history=[],
            system_prompt=_judge_system_prompt(policy.target_type, criteria_block),
        )

        parsed = json.loads(_strip_json_fence((getattr(result, "answer", "") or "").strip()))
        score = max(0.0, min(1.0, float(parsed.get("score", 0.0))))

        issues = [
            {"type": str(issue.get("type", "issue")), "message": str(issue.get("message", ""))}
            for issue in (parsed.get("issues") or [])
            if isinstance(issue, dict)
        ]
        required_repairs = [
            {"section": str(repair.get("section", "")), "instruction": str(repair.get("instruction", ""))}
            for repair in (parsed.get("required_repairs") or [])
            if isinstance(repair, dict)
        ]

        if score >= policy.pass_threshold:
            status: JudgeStatus = "pass"
        elif score >= policy.repair_threshold:
            status = "repair"
        else:
            status = "fail"

        return JudgeResult(
            id=str(uuid.uuid4()),
            target_type=policy.target_type,
            target_id=target_id,
            judge_agent_id=policy.id,
            score=score,
            status=status,
            issues=issues,
            required_repairs=required_repairs if status == "repair" else [],
            can_publish=(status == "pass"),
        )


def _judge_system_prompt(target_type: str, criteria_block: str) -> str:
    return (
        f"You are a {target_type} quality judge. "
        "Evaluate the provided content against the following criteria:\n\n"
        f"{criteria_block}\n\n"
        "Respond with a JSON object with these keys:\n"
        "- \"score\": float 0.0-1.0 representing overall quality\n"
        "- \"issues\": array of objects with \"type\" and \"message\" for each criterion that failed\n"
        "- \"required_repairs\": array of objects with \"section\" and \"instruction\" "
        "for each repair needed (empty array if score >= pass threshold)\n\n"
        "Output ONLY valid JSON."
    )


def _strip_json_fence(raw: str) -> str:
    if raw.startswith("```"):
        parts = raw.split("```")
        raw = parts[1] if len(parts) > 1 else raw
        if raw.startswith("json"):
            raw = raw[4:]
    return raw.strip()
