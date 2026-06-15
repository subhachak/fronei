"""Vision-model slide judge for executive AgentDeck QA.

This is intentionally optional and best-effort. It turns rendered slide
thumbnails into the same `QAIssue` taxonomy used by deterministic QA so the
existing structural repair loop can act on visual critiques without a second
repair mechanism.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from litellm import completion, completion_cost

from app.config import get_settings
from app.services.components import DocPlan
from app.services.llm_gateway import LLMResult

from .types import QAIssue

logger = logging.getLogger(__name__)

_ALLOWED_TYPES = {
    "dangling_punctuation",
    "duplicate_label",
    "missing_title",
    "missing_dek",
    "missing_decision_ask",
    "too_many_items",
    "fit_overflow",
    "blank",
    "dense_text",
    "dense_ink",
    "tiny_text_risk",
    "empty_zone",
    "excessive_whitespace",
}
_ALLOWED_SEVERITIES = {"info", "warning", "error"}

_VISION_JUDGE_PROMPT = """You are Fronei's executive presentation visual QA judge.

Inspect the rendered slide image like a human design reviewer. Be strict about:
- text overflow, unreadable small text, clipped content, collisions, or cramped slides
- empty or underused slides that look unfinished
- missing title/dek/decision ask when the slide clearly needs one
- duplicate labels, dangling punctuation, orphaned headings, chart label collisions
- low executive readiness: generic bullet dumps, weak visual hierarchy, or unclear action

Return ONLY valid JSON:
{
  "status": "pass|warn|fail",
  "score": 0.0-1.0,
  "issues": [
    {
      "type": "dense_text|dense_ink|tiny_text_risk|blank|excessive_whitespace|missing_title|missing_dek|missing_decision_ask|duplicate_label|dangling_punctuation|fit_overflow|too_many_items|empty_zone",
      "severity": "info|warning|error",
      "detail": "short actionable issue"
    }
  ],
  "summary": "one sentence"
}

Use "pass" with no issues when the slide is visually executive-ready.
"""


def judge_rendered_slides(
    *,
    doc_plan: DocPlan,
    render_qa: dict | None,
) -> tuple[list[QAIssue], list[dict], LLMResult]:
    images = (render_qa or {}).get("images") or []
    if not images:
        return [], [], LLMResult("", "vision_judge_unavailable", 0, None, None, None)

    settings = get_settings()
    model = settings.agentdeck_vision_judge_model
    started = time.perf_counter()
    total_prompt_tokens = 0
    total_completion_tokens = 0
    total_cost = 0.0
    saw_prompt_usage = False
    saw_completion_usage = False
    saw_cost = False
    slide_results: list[dict] = []
    issues: list[QAIssue] = []

    section_by_slide = {idx + 2: section for idx, section in enumerate(doc_plan.sections)}
    for image in images[: max(1, settings.agentdeck_vision_judge_max_slides)]:
        slide_number = int(image.get("slide") or 0)
        section = section_by_slide.get(slide_number)
        context = _slide_context(section)
        payload = _call_vision_model(model, image, context)
        slide_results.append({"slide": slide_number, **payload})
        for raw_issue in payload.get("issues") or []:
            issue = _issue_from_payload(raw_issue, slide_number, section.slide_id if section else None)
            if issue:
                issues.append(issue)

        usage = payload.get("_usage") or {}
        if usage.get("prompt_tokens") is not None:
            saw_prompt_usage = True
            total_prompt_tokens += int(usage["prompt_tokens"])
        if usage.get("completion_tokens") is not None:
            saw_completion_usage = True
            total_completion_tokens += int(usage["completion_tokens"])
        if usage.get("cost") is not None:
            saw_cost = True
            total_cost += float(usage["cost"])

    latency_ms = int((time.perf_counter() - started) * 1000)
    return issues, _strip_private_usage(slide_results), LLMResult(
        answer=json.dumps({"slides": _strip_private_usage(slide_results)}, ensure_ascii=False),
        model_used=model,
        latency_ms=latency_ms,
        prompt_tokens=total_prompt_tokens if saw_prompt_usage else None,
        completion_tokens=total_completion_tokens if saw_completion_usage else None,
        estimated_cost_usd=total_cost if saw_cost else None,
    )


def _call_vision_model(model: str, image: dict, context: str) -> dict:
    messages = [{
        "role": "user",
        "content": [
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:{image.get('mime_type') or 'image/png'};base64,{image.get('base64') or ''}"
                },
            },
            {"type": "text", "text": f"{_VISION_JUDGE_PROMPT}\n\nSLIDE CONTEXT:\n{context}"},
        ],
    }]
    response = completion(model=model, messages=messages, temperature=0.0, max_tokens=1200)
    text = (response.choices[0].message.content or "").strip()
    data = _parse_json_object(text) or {"status": "warn", "score": 0.5, "issues": [], "summary": text[:240]}
    usage = getattr(response, "usage", None)
    try:
        cost = float(completion_cost(completion_response=response))
    except Exception:
        cost = None
    data["_usage"] = {
        "prompt_tokens": getattr(usage, "prompt_tokens", None) if usage else None,
        "completion_tokens": getattr(usage, "completion_tokens", None) if usage else None,
        "cost": cost,
    }
    return data


def _slide_context(section: Any) -> str:
    if section is None:
        return "Title/cover slide or slide without structured section metadata."
    return json.dumps(
        {
            "slide_id": section.slide_id,
            "layout": section.slide_layout,
            "title": section.section_title or section.hero_title or section.closing_text,
            "dek": section.dek or section.subtitle,
            "purpose": section.purpose,
            "message": section.message,
            "audience_question": section.audience_question,
        },
        ensure_ascii=False,
        default=str,
    )


def _parse_json_object(content: str) -> dict[str, Any] | None:
    stripped = content.strip()
    if not stripped:
        return None
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, flags=re.DOTALL | re.IGNORECASE)
    if fence:
        stripped = fence.group(1)
    elif not (stripped.startswith("{") and stripped.endswith("}")):
        start, end = stripped.find("{"), stripped.rfind("}")
        if start == -1 or end <= start:
            return None
        stripped = stripped[start:end + 1]
    try:
        data = json.loads(stripped)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _issue_from_payload(payload: Any, slide_number: int, slide_id: str | None) -> QAIssue | None:
    if not isinstance(payload, dict):
        return None
    issue_type = str(payload.get("type") or "").strip()
    if issue_type not in _ALLOWED_TYPES:
        return None
    severity = str(payload.get("severity") or "warning").strip()
    if severity not in _ALLOWED_SEVERITIES:
        severity = "warning"
    detail = str(payload.get("detail") or issue_type).strip()[:500]
    return QAIssue(
        type=issue_type,  # type: ignore[arg-type]
        severity=severity,  # type: ignore[arg-type]
        stage="render",
        slide=slide_number,
        slide_id=slide_id,
        detail=detail,
    )


def _strip_private_usage(slide_results: list[dict]) -> list[dict]:
    return [{k: v for k, v in result.items() if k != "_usage"} for result in slide_results]
