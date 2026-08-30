"""The model-based half of item validation.

A second model answers the item blind -- stimulus and options only, no key, no
evidence, no rationales -- and the item is accepted only if that independent
answer agrees with the key on every question with real confidence, and the
reviewer flags no second defensible option.

This is deliberately expensive and deliberately strict. The alternative is a
bank that looks full and quietly contains items where the learner's correct
reasoning is marked wrong, which is worse than a smaller bank: it teaches the
wrong lesson and destroys trust in every score the app reports.

The reviewer defaults to a different provider from the writer
(`celpip_item_validator` vs `celpip_item_writer` in model_policy) so the two
passes do not share blind spots.
"""
from __future__ import annotations

import logging
from typing import Any

from app.services.celpip import prompts, schemas
from app.services.celpip.spec import TASKS_BY_KEY

logger = logging.getLogger(__name__)

# Below this, the reviewer was guessing and the item is too ambiguous to serve.
MIN_REVIEWER_CONFIDENCE = 0.6

# Rejection reasons, recorded per candidate on the generation run so a task
# type that keeps failing is visible as a prompt problem rather than an
# inexplicably empty bank.
REASON_SCHEMA = "schema"
REASON_DUPLICATE = "duplicate"
REASON_KEY_DISAGREEMENT = "key_disagreement"
REASON_AMBIGUOUS = "ambiguous"
REASON_LOW_CONFIDENCE = "low_confidence"
REASON_FORMAT = "format_mismatch"
REASON_UNNATURAL = "unnatural_language"
REASON_SPECIALIST = "specialist_knowledge"
REASON_INCOMPLETE = "incomplete_context"
REASON_REVIEW_FAILED = "review_call_failed"
REASON_PARSE = "unparseable_generation"


def _model_call(system: str, user: str, *, role: str, max_tokens: int) -> tuple[dict, str]:
    from app.services.agent import model_client
    from app.services.agent.research_utils import _parse_json

    response = model_client.complete(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        role=role,
        max_tokens=max_tokens,
        timeout_s=180,
    )
    return _parse_json(response.text), getattr(response, "model", "") or ""


def review_receptive(task_key: str, payload: dict) -> dict:
    """Blind-answer a Listening or Reading item and compare to its key."""
    system, user = prompts.build_validation_prompt(task_key, payload)
    data, model = _model_call(system, user, role="celpip_item_validator", max_tokens=4000)

    questions = payload.get("questions") or []
    answers = {int(a.get("index", -1)): a for a in (data.get("answers") or []) if isinstance(a, dict)}

    disagreements: list[dict] = []
    ambiguous: list[dict] = []
    low_confidence: list[dict] = []

    for i, q in enumerate(questions):
        keyed = str(q.get("answer", ""))
        review = answers.get(i)
        if review is None:
            disagreements.append({"index": i, "keyed": keyed, "reviewer": None,
                                  "detail": "reviewer did not answer this question"})
            continue
        choice = str(review.get("choice", ""))
        try:
            confidence = float(review.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        if choice != keyed:
            disagreements.append({"index": i, "keyed": keyed, "reviewer": choice,
                                  "detail": str(review.get("reason", ""))[:400]})
        elif confidence < MIN_REVIEWER_CONFIDENCE:
            low_confidence.append({"index": i, "confidence": confidence})
        for other in review.get("also_defensible") or []:
            if isinstance(other, dict) and str(other.get("option", "")) != keyed:
                ambiguous.append({"index": i, "option": str(other.get("option", "")),
                                  "reason": str(other.get("reason", ""))[:400]})

    reasons: list[dict] = []
    if disagreements:
        reasons.append({"reason": REASON_KEY_DISAGREEMENT,
                        "detail": f"{len(disagreements)} question(s) answered differently by the reviewer",
                        "questions": disagreements})
    if ambiguous:
        reasons.append({"reason": REASON_AMBIGUOUS,
                        "detail": f"{len(ambiguous)} question(s) have a second defensible option",
                        "questions": ambiguous})
    if low_confidence:
        reasons.append({"reason": REASON_LOW_CONFIDENCE,
                        "detail": "reviewer agreed but was unsure",
                        "questions": low_confidence})
    reasons.extend(_quality_reasons(data))

    return {
        "kind": "receptive",
        "validator_model": model,
        "accepted": not reasons,
        "reasons": reasons,
        "reviewer_notes": str(data.get("notes", ""))[:1000],
        "questions_reviewed": len(answers),
        "raw": data,
    }


def _quality_reasons(data: dict) -> list[dict]:
    reasons: list[dict] = []
    if data.get("format_fit") is False:
        reasons.append({"reason": REASON_FORMAT, "detail": str(data.get("notes", ""))[:400]})
    if data.get("natural_language") is False:
        reasons.append({"reason": REASON_UNNATURAL, "detail": str(data.get("notes", ""))[:400]})
    if data.get("needs_specialist_knowledge") is True:
        reasons.append({"reason": REASON_SPECIALIST, "detail": str(data.get("notes", ""))[:400]})
    if data.get("context_complete") is False:
        reasons.append({"reason": REASON_INCOMPLETE, "detail": str(data.get("notes", ""))[:400]})
    if data.get("answerable_in_time") is False:
        reasons.append({"reason": REASON_FORMAT,
                        "detail": "reviewer judged the task unanswerable in the official time limit"})
    return reasons


def review_productive(task_key: str, payload: dict) -> dict:
    """Review a Writing or Speaking prompt. There is no key to check, so this
    judges only whether the prompt is usable as the task it claims to be."""
    system, user = prompts.build_productive_validation_prompt(task_key, payload)
    data, model = _model_call(system, user, role="celpip_item_validator", max_tokens=1200)
    reasons = _quality_reasons(data)
    return {
        "kind": "productive",
        "validator_model": model,
        "accepted": not reasons,
        "reasons": reasons,
        "reviewer_notes": str(data.get("notes", ""))[:1000],
        "raw": data,
    }


def review_item(task_key: str, payload: dict) -> dict:
    """Full model review. Never raises: a failed review call rejects the item
    rather than letting an unchecked one through."""
    task = TASKS_BY_KEY.get(task_key)
    if task is None:
        return {"accepted": False, "reasons": [{"reason": REASON_SCHEMA,
                                                "detail": f"unknown task {task_key}"}]}
    try:
        if task.question_count:
            return review_receptive(task_key, payload)
        return review_productive(task_key, payload)
    except Exception as exc:
        logger.warning("celpip item review failed for %s: %s", task_key, exc)
        return {
            "kind": "receptive" if task.question_count else "productive",
            "validator_model": "",
            "accepted": False,
            "reasons": [{"reason": REASON_REVIEW_FAILED, "detail": str(exc)[:400]}],
            "reviewer_notes": "",
            "raw": {},
        }


def check_schema(task_key: str, payload: Any) -> dict:
    """Deterministic validation, in the same verdict shape as the model review."""
    errors = schemas.validate_payload(task_key, payload)
    return {
        "accepted": not errors,
        "reasons": [{"reason": REASON_SCHEMA, "detail": e} for e in errors],
    }
