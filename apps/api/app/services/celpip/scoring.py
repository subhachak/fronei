"""Scoring: deterministic for the receptive skills, two-pass rubric for the
productive ones.

Listening and Reading are keyed, so they are scored by comparison -- no model
involved, and every question comes back with the evidence span and the
per-distractor rationale the item was banked with.

Writing and Speaking run two independent evaluator passes and reconcile them.
All three outputs are stored. Where the passes disagree, confidence falls and
the reported level widens rather than being averaged into false precision.

Speaking additionally carries deterministic delivery metrics computed from the
word timings, because a cleaned-up transcript hides every hesitation: a
response delivered in a confident 55 seconds and the same words delivered with
four eight-second silences read identically as text.
"""
from __future__ import annotations

import json
import logging
import re
from collections import Counter
from datetime import datetime, timezone

from app.db.models import (
    CelpipAttempt,
    CelpipEvaluation,
    CelpipQuestion,
    CelpipResponse,
    CelpipTestItem,
    SessionLocal,
)
from app.services.agent.models import new_id
from app.services.celpip import rubric
from app.services.celpip.spec import (
    RUBRIC_VERSION,
    TASKS_BY_KEY,
    WEAKNESS_TAGS,
    dimensions_for,
    estimate_level_range,
)

logger = logging.getLogger(__name__)

# Multi-word fillers are matched before single words so "you know" is not
# counted as two separate hits.
FILLER_PHRASES = ("you know", "i mean", "sort of", "kind of", "you see")
FILLER_WORDS = ("um", "uh", "erm", "ah", "eh", "like", "basically", "actually", "literally", "right")

# A gap this long mid-response is a hesitation a listener notices, not a breath.
PAUSE_THRESHOLD_SECONDS = 1.5

# Below this fraction of the allotted time, the response is treated as
# incomplete regardless of what the words say.
UNDERUSE_RATIO = 0.6

# Two evaluator passes further apart than this on the same dimension is a
# material disagreement and triggers reconciliation.
DISAGREEMENT_THRESHOLD = 1


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _loads(raw: str, default):
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return default
    return value if value is not None else default


# --- Deterministic delivery metrics --------------------------------------

def delivery_metrics(
    *, transcript: str, words: list[dict], limit_seconds: int, audio_duration: float
) -> dict:
    """Pace, pauses, fillers, repetition, and completeness, from timings only."""
    text = (transcript or "").strip()
    tokens = re.findall(r"[a-z']+", text.lower())
    spoken_seconds = audio_duration or (words[-1]["end"] if words else 0.0)

    lowered = f" {' '.join(tokens)} "
    filler_hits = 0
    for phrase in FILLER_PHRASES:
        filler_hits += lowered.count(f" {phrase} ")
        lowered = lowered.replace(f" {phrase} ", " ")
    counts = Counter(lowered.split())
    filler_hits += sum(counts.get(word, 0) for word in FILLER_WORDS)

    pauses: list[float] = []
    for prev, nxt in zip(words, words[1:]):
        gap = float(nxt.get("start", 0.0)) - float(prev.get("end", 0.0))
        if gap >= PAUSE_THRESHOLD_SECONDS:
            pauses.append(round(gap, 2))

    # Immediate repetition -- the same word or bigram twice in a row -- is the
    # kind a listener hears as stalling, distinct from ordinary lexical reuse.
    immediate_repeats = sum(1 for a, b in zip(tokens, tokens[1:]) if a == b)
    bigrams = list(zip(tokens, tokens[1:]))
    repeated_bigrams = sum(1 for a, b in zip(bigrams, bigrams[1:]) if a == b)

    wpm = round(len(tokens) / (spoken_seconds / 60), 1) if spoken_seconds > 0 else 0.0
    used_ratio = round(spoken_seconds / limit_seconds, 2) if limit_seconds else 0.0

    tags: list[str] = []
    if wpm and wpm > 190:
        tags.append("pace_too_fast")
    if wpm and wpm < 100:
        tags.append("pace_too_slow")
    if len(tokens) and filler_hits / max(len(tokens), 1) > 0.06:
        tags.append("filler_words")
    if len(pauses) >= 3:
        tags.append("long_pauses")
    if immediate_repeats + repeated_bigrams >= 4:
        tags.append("repetition")
    if used_ratio and used_ratio < UNDERUSE_RATIO:
        tags.append("incomplete_response")

    return {
        "word_count": len(tokens),
        "words_per_minute": wpm,
        "spoken_seconds": round(spoken_seconds, 2),
        "limit_seconds": limit_seconds,
        "time_used_ratio": used_ratio,
        "filler_count": filler_hits,
        "filler_rate": round(filler_hits / len(tokens), 3) if tokens else 0.0,
        "pause_count": len(pauses),
        "longest_pause_seconds": max(pauses) if pauses else 0.0,
        "pauses": pauses[:20],
        "immediate_repeats": immediate_repeats + repeated_bigrams,
        "has_word_timings": bool(words),
        "flags": tags,
    }


# --- Receptive scoring ----------------------------------------------------

def score_receptive_question(question: CelpipQuestion, responses: list[CelpipResponse]) -> dict:
    """Score one keyed item, returning per-question review detail.

    A response flagged `late` arrived after the section deadline plus its grace
    window, so it is treated as unanswered -- the real test would not have
    accepted it. The count is reported rather than silently applied, so the
    learner sees "this did not count because it arrived after time" instead of
    an unexplained wrong answer.
    """
    payload = _loads(question.payload_json, {})
    keyed = payload.get("questions") or []
    by_index = {r.question_index: r for r in responses}

    details = []
    correct = 0
    late_excluded = 0
    for index, q in enumerate(keyed):
        response = by_index.get(index)
        is_late = bool(response and response.late)
        if is_late:
            late_excluded += 1
        chosen = "" if is_late else ((response.selected_option if response else None) or "")
        answer = str(q.get("answer", ""))
        is_correct = bool(chosen) and chosen == answer
        correct += 1 if is_correct else 0
        rationales = q.get("rationales") or {}
        details.append({
            "index": index,
            "prompt": q.get("prompt", ""),
            "options": q.get("options", {}),
            "answer": answer,
            "chosen": chosen or None,
            "correct": is_correct,
            "answered": bool(chosen),
            "late": is_late,
            "evidence": q.get("evidence", ""),
            "why_correct": rationales.get(answer, ""),
            # Only the distractors, so the review screen explains the road not
            # taken rather than repeating the key.
            "why_others_wrong": {k: v for k, v in rationales.items() if k != answer},
            "time_spent_ms": response.time_spent_ms if response else 0,
        })

    return {
        "question_id": question.id,
        "task_key": question.task_key,
        "correct": correct,
        "total": len(keyed),
        "late_excluded": late_excluded,
        "questions": details,
    }


def _receptive_weakness_tags(items: list[dict]) -> list[str]:
    """Infer weakness tags from the pattern of wrong answers.

    Deliberately coarse -- it names what the data actually shows (unanswered
    questions, attribution errors on the tasks that test attribution) rather
    than guessing at a cause the response data cannot support.
    """
    tags: set[str] = set()
    unanswered = 0
    total = 0
    for item in items:
        for q in item["questions"]:
            total += 1
            if not q["answered"]:
                unanswered += 1
            elif not q["correct"]:
                if item["task_key"] in {"listening_discussion", "listening_viewpoints", "reading_viewpoints"}:
                    tags.add("speaker_attribution")
                else:
                    tags.add("distractor_confusion")
    if total and unanswered / total > 0.1:
        tags.add("time_management")
    return sorted(tags)


# --- Productive scoring ---------------------------------------------------

def _response_context(question: CelpipQuestion, response: CelpipResponse, metrics: dict | None) -> str:
    payload = _loads(question.payload_json, {})
    stim = payload.get("stimulus") or {}
    body = response.transcript if response.skill == "speaking" else response.response_text
    parts = [
        "TASK PROMPT:",
        json.dumps(stim, ensure_ascii=False, indent=2),
        "",
        "CANDIDATE RESPONSE:",
        body or "(no response was given)",
    ]
    if metrics:
        parts += [
            "",
            "MEASURED DELIVERY (computed from the audio, not estimated -- use these numbers):",
            json.dumps(
                {k: metrics[k] for k in (
                    "word_count", "words_per_minute", "spoken_seconds", "limit_seconds",
                    "time_used_ratio", "filler_count", "pause_count", "longest_pause_seconds",
                    "immediate_repeats",
                )},
                indent=2,
            ),
        ]
    return "\n".join(parts)


def _evaluator_pass(task_key: str, context: str, *, pass_label: str, role: str) -> tuple[dict, str]:
    from app.services.agent import model_client
    from app.services.agent.research_utils import _parse_json

    system = rubric.build_scorer_prompt(task_key, pass_label=pass_label)
    response = model_client.complete(
        [{"role": "system", "content": system}, {"role": "user", "content": context}],
        role=role,
        max_tokens=4000,
        timeout_s=240,
    )
    return _parse_json(response.text), getattr(response, "model", "") or ""


def _levels(evaluation: dict, dims: tuple[str, ...]) -> dict[str, int]:
    out: dict[str, int] = {}
    for dim in dims:
        entry = (evaluation.get("dimensions") or {}).get(dim) or {}
        try:
            out[dim] = int(entry.get("level"))
        except (TypeError, ValueError):
            continue
    return out


def _needs_reconciliation(a: dict[str, int], b: dict[str, int]) -> list[dict]:
    gaps = []
    for dim in set(a) | set(b):
        if dim in a and dim in b and abs(a[dim] - b[dim]) > DISAGREEMENT_THRESHOLD:
            gaps.append({"dimension": dim, "a": a[dim], "b": b[dim]})
    return gaps


def _reconcile(task_key: str, context: str, a: dict, b: dict) -> tuple[dict, str]:
    from app.services.agent import model_client
    from app.services.agent.research_utils import _parse_json

    user = (
        f"{context}\n\nEVALUATION A:\n{json.dumps(a, ensure_ascii=False)[:12000]}"
        f"\n\nEVALUATION B:\n{json.dumps(b, ensure_ascii=False)[:12000]}"
    )
    response = model_client.complete(
        [{"role": "system", "content": rubric.RECONCILER_SYSTEM}, {"role": "user", "content": user}],
        role="celpip_score_reconciler",
        max_tokens=2500,
        timeout_s=240,
    )
    return _parse_json(response.text), getattr(response, "model", "") or ""


def _merge_feedback(a: dict, b: dict) -> dict:
    """Union the two passes' actionable output, preferring A's ordering.

    Both passes see the same response, so their corrections overlap heavily;
    deduplicating on the quoted span keeps the union useful rather than
    doubling every point.
    """
    def _dedupe(items: list, key) -> list:
        seen = set()
        out = []
        for item in items:
            marker = key(item)
            if marker in seen:
                continue
            seen.add(marker)
            out.append(item)
        return out

    corrections = _dedupe(
        [c for c in (a.get("corrections") or []) + (b.get("corrections") or []) if isinstance(c, dict)],
        lambda c: str(c.get("original", "")).strip().lower(),
    )
    severity_rank = {"high": 0, "medium": 1, "low": 2}
    corrections.sort(key=lambda c: severity_rank.get(str(c.get("severity", "low")).lower(), 3))

    return {
        "summary": "",
        "strengths": _dedupe(
            [str(s) for s in (a.get("strengths") or []) + (b.get("strengths") or [])],
            lambda s: s.strip().lower(),
        ),
        "met_requirements": _dedupe(
            [str(s) for s in (a.get("met_requirements") or []) + (b.get("met_requirements") or [])],
            lambda s: s.strip().lower(),
        ),
        "missing_requirements": _dedupe(
            [str(s) for s in (a.get("missing_requirements") or []) + (b.get("missing_requirements") or [])],
            lambda s: s.strip().lower(),
        ),
        "corrections": corrections[:20],
        "patterns": _dedupe(
            [str(s) for s in (a.get("patterns") or []) + (b.get("patterns") or [])],
            lambda s: s.strip().lower(),
        ),
        "outline": a.get("outline") or b.get("outline") or [],
    }


def _collect_tags(*sources: dict, extra: list[str] | None = None) -> list[str]:
    tags: set[str] = set(extra or [])
    for source in sources:
        for tag in source.get("weakness_tags") or []:
            if str(tag) in WEAKNESS_TAGS:
                tags.add(str(tag))
    return sorted(tags)


def evaluate_response(db, attempt: CelpipAttempt, response: CelpipResponse) -> CelpipEvaluation:
    """Score one Writing or Speaking response end to end."""
    question = db.get(CelpipQuestion, response.question_id)
    task = TASKS_BY_KEY.get(response.task_key)
    dims = dimensions_for(response.skill)

    metrics = None
    if response.skill == "speaking":
        metrics = delivery_metrics(
            transcript=response.transcript,
            words=_loads(response.transcript_words_json, []),
            limit_seconds=task.response_seconds if task else 0,
            audio_duration=response.audio_duration_seconds,
        )

    evaluation = CelpipEvaluation(
        id=new_id("ceval"),
        attempt_id=attempt.id,
        response_id=response.id,
        question_id=response.question_id,
        skill=response.skill,
        task_key=response.task_key,
        method="rubric",
        status="scoring",
        rubric_version=RUBRIC_VERSION,
        delivery_metrics_json=json.dumps(metrics or {}),
    )
    db.add(evaluation)
    db.commit()

    body = response.transcript if response.skill == "speaking" else response.response_text
    if not (body or "").strip():
        # Nothing was submitted. Scoring an empty response with a model would
        # produce a confident-looking number from nothing.
        evaluation.status = "complete"
        evaluation.level_low = evaluation.level_high = 0
        evaluation.confidence = 1.0
        evaluation.feedback_json = json.dumps({
            "summary": "No response was recorded for this task.",
            "strengths": [], "met_requirements": [],
            "missing_requirements": ["The task was not attempted."],
            "corrections": [], "patterns": [], "outline": [],
        })
        evaluation.weakness_tags_json = json.dumps(["incomplete_response"])
        evaluation.completed_at = _now()
        db.commit()
        return evaluation

    context = _response_context(question, response, metrics)
    base_role = "celpip_speaking_scorer" if response.skill == "speaking" else "celpip_writing_scorer"

    try:
        eval_a, model_a = _evaluator_pass(response.task_key, context, pass_label="A", role=base_role)
        # Pass B has its own model-policy role, defaulting to a different
        # provider. Two passes from one model is one opinion asked twice.
        eval_b, model_b = _evaluator_pass(
            response.task_key, context, pass_label="B", role=f"{base_role}_b"
        )
    except Exception as exc:
        logger.warning("celpip evaluation failed for response %s: %s", response.id, exc)
        evaluation.status = "failed"
        evaluation.error = str(exc)[:2000]
        evaluation.completed_at = _now()
        db.commit()
        return evaluation

    levels_a = _levels(eval_a, dims)
    levels_b = _levels(eval_b, dims)
    gaps = _needs_reconciliation(levels_a, levels_b)

    reconciliation: dict = {}
    reconciler_model = ""
    final_levels: dict[str, int] = {}
    if gaps:
        try:
            reconciliation, reconciler_model = _reconcile(response.task_key, context, eval_a, eval_b)
            final_levels = _levels(reconciliation, dims)
        except Exception as exc:
            logger.warning("celpip reconciliation failed for %s: %s", response.id, exc)
            reconciliation = {"error": str(exc)[:400]}
    if not final_levels:
        # Agreement, or a failed reconciliation: average per dimension.
        for dim in dims:
            values = [d[dim] for d in (levels_a, levels_b) if dim in d]
            if values:
                final_levels[dim] = round(sum(values) / len(values))

    # The reported range expresses how far apart the two independent passes
    # landed, not how far apart the dimensions did. A candidate can legitimately
    # be a 10 on vocabulary and a 7 on coherence; that spread is information
    # about them, not uncertainty about the estimate. Disagreement between
    # evaluators is the uncertainty.
    def _overall(levels: dict[str, int]) -> float | None:
        values = list(levels.values())
        return sum(values) / len(values) if values else None

    candidates = [v for v in (_overall(levels_a), _overall(levels_b), _overall(final_levels)) if v is not None]
    if candidates:
        level_low = int(round(min(candidates)))
        level_high = int(round(max(candidates)))
    else:
        level_low = level_high = 0

    # Confidence falls with disagreement: two passes a level apart is a softer
    # estimate than two that matched, and saying so is more useful than an
    # average that hides it.
    spread_penalty = 0.15 * len(gaps)
    stated = [float(e.get("confidence", 0.7) or 0.7) for e in (eval_a, eval_b)]
    confidence = max(0.1, min(1.0, sum(stated) / len(stated) - spread_penalty))
    if reconciliation.get("confidence"):
        try:
            confidence = max(0.1, min(1.0, float(reconciliation["confidence"])))
        except (TypeError, ValueError):
            pass

    feedback = _merge_feedback(eval_a, eval_b)
    feedback["summary"] = str(reconciliation.get("summary", "")) or _fallback_summary(
        level_low, level_high, feedback
    )
    feedback["disagreements"] = reconciliation.get("disagreements") or gaps
    feedback["dimension_comments"] = {
        dim: {
            "level": final_levels.get(dim),
            "a": (eval_a.get("dimensions") or {}).get(dim, {}),
            "b": (eval_b.get("dimensions") or {}).get(dim, {}),
        }
        for dim in dims
    }

    evaluation.status = "complete"
    evaluation.level_low = level_low
    evaluation.level_high = level_high
    evaluation.dimensions_json = json.dumps(final_levels)
    evaluation.confidence = round(confidence, 2)
    evaluation.evaluator_a_json = json.dumps(eval_a, ensure_ascii=False)[:200000]
    evaluation.evaluator_b_json = json.dumps(eval_b, ensure_ascii=False)[:200000]
    evaluation.reconciliation_json = json.dumps(reconciliation, ensure_ascii=False)[:100000]
    evaluation.feedback_json = json.dumps(feedback, ensure_ascii=False)[:200000]
    evaluation.weakness_tags_json = json.dumps(
        _collect_tags(eval_a, eval_b, extra=(metrics or {}).get("flags"))
    )
    evaluation.evaluator_a_model = model_a[:120]
    evaluation.evaluator_b_model = model_b[:120]
    evaluation.reconciler_model = reconciler_model[:120]
    evaluation.completed_at = _now()
    db.commit()
    return evaluation


def _fallback_summary(low: int, high: int, feedback: dict) -> str:
    band = f"level {low}" if low == high else f"level {low}-{high}"
    missing = len(feedback.get("missing_requirements") or [])
    tail = f" {missing} task requirement(s) went unaddressed." if missing else ""
    return f"Estimated {band}.{tail}"


# --- Exemplar (separate call, after scoring) ------------------------------

def generate_exemplar(evaluation_id: str) -> dict:
    """Write an improved version of the learner's own response.

    Deliberately a separate call made after the score is fixed: an evaluator
    that has just written a model answer scores the candidate against it.
    """
    from app.services.agent import model_client
    from app.services.agent.research_utils import _parse_json

    db = SessionLocal()
    try:
        evaluation = db.get(CelpipEvaluation, evaluation_id)
        if evaluation is None:
            raise ValueError("evaluation not found")
        if evaluation.exemplar_json and evaluation.exemplar_json != "{}":
            return _loads(evaluation.exemplar_json, {})

        response = db.get(CelpipResponse, evaluation.response_id) if evaluation.response_id else None
        question = db.get(CelpipQuestion, evaluation.question_id) if evaluation.question_id else None
        if response is None or question is None:
            raise ValueError("evaluation has no response to improve")

        metrics = _loads(evaluation.delivery_metrics_json, {})
        context = _response_context(question, response, metrics or None)
        rating = {
            "levels": _loads(evaluation.dimensions_json, {}),
            "overall": [evaluation.level_low, evaluation.level_high],
            "feedback": _loads(evaluation.feedback_json, {}),
        }
        user = f"{context}\n\nRATING ALREADY GIVEN (fixed -- do not re-score):\n{json.dumps(rating, ensure_ascii=False)[:12000]}"

        result = model_client.complete(
            [{"role": "system", "content": rubric.EXEMPLAR_SYSTEM}, {"role": "user", "content": user}],
            role="celpip_feedback_writer",
            max_tokens=3000,
            timeout_s=240,
        )
        data = _parse_json(result.text)
        evaluation.exemplar_json = json.dumps(data, ensure_ascii=False)[:100000]
        db.commit()
        return data
    finally:
        db.close()


# --- Attempt orchestration ------------------------------------------------

def _transcribe_pending(db, attempt: CelpipAttempt) -> None:
    """Transcribe any speaking response captured but not yet transcribed."""
    from app.services.blob_store import store_for_location
    from app.services.celpip import speech

    pending = (
        db.query(CelpipResponse)
        .filter(CelpipResponse.attempt_id == attempt.id)
        .filter(CelpipResponse.skill == "speaking")
        .filter(CelpipResponse.transcription_status.in_(("none", "pending", "failed")))
        .all()
    )
    for response in pending:
        if not response.audio_blob_location:
            response.transcription_status = "empty"
            db.commit()
            continue
        try:
            audio = store_for_location(response.audio_blob_location).read(response.audio_blob_location)
            transcript = speech.transcribe(audio, filename=f"{response.id}.webm")
            response.transcript = transcript.text
            response.transcript_words_json = json.dumps(transcript.words)
            if transcript.duration_seconds:
                response.audio_duration_seconds = transcript.duration_seconds
            response.transcription_status = "complete"
        except Exception as exc:
            logger.warning("celpip transcription failed for %s: %s", response.id, exc)
            response.transcription_status = "failed"
        db.commit()


def evaluate_attempt(attempt_id: str) -> dict:
    """Score a submitted attempt: transcribe, key the receptive sections, run
    the productive evaluators, then roll up per-component estimates."""
    db = SessionLocal()
    try:
        attempt = db.get(CelpipAttempt, attempt_id)
        if attempt is None:
            raise ValueError(f"attempt {attempt_id} not found")
        attempt.status = "evaluating"
        db.commit()

        _transcribe_pending(db, attempt)

        items = (
            db.query(CelpipTestItem)
            .filter(CelpipTestItem.test_id == attempt.test_id)
            .order_by(CelpipTestItem.position.asc())
            .all()
        )
        responses = db.query(CelpipResponse).filter(CelpipResponse.attempt_id == attempt.id).all()
        by_question: dict[str, list[CelpipResponse]] = {}
        for response in responses:
            by_question.setdefault(response.question_id, []).append(response)

        receptive: dict[str, list[dict]] = {"listening": [], "reading": []}
        productive: dict[str, list[CelpipEvaluation]] = {"writing": [], "speaking": []}
        failures = 0

        for item in items:
            question = db.get(CelpipQuestion, item.question_id)
            if question is None:
                continue
            answers = by_question.get(question.id, [])

            if question.skill in receptive:
                # Unscored content is delivered like everything else and simply
                # excluded here -- the learner is never told which it was.
                if item.is_unscored or item.is_practice_task:
                    continue
                receptive[question.skill].append(score_receptive_question(question, answers))
                continue

            for response in answers:
                evaluation = evaluate_response(db, attempt, response)
                if evaluation.status == "complete":
                    productive[question.skill].append(evaluation)
                else:
                    failures += 1

        results: dict[str, dict] = {}
        for skill, scored in receptive.items():
            if not scored:
                continue
            correct = sum(s["correct"] for s in scored)
            total = sum(s["total"] for s in scored)
            level = estimate_level_range(correct, total)
            by_task: dict[str, dict] = {}
            for entry in scored:
                bucket = by_task.setdefault(entry["task_key"], {"correct": 0, "total": 0})
                bucket["correct"] += entry["correct"]
                bucket["total"] += entry["total"]
            results[skill] = {
                "method": "deterministic",
                "raw_score": correct,
                "max_score": total,
                "late_excluded": sum(s.get("late_excluded", 0) for s in scored),
                "level": level.as_dict(),
                "accuracy_by_task": by_task,
                "weakness_tags": _receptive_weakness_tags(scored),
                "items": scored,
            }

        for skill, evaluations in productive.items():
            if not evaluations:
                continue
            lows = [e.level_low for e in evaluations if e.level_low is not None]
            highs = [e.level_high for e in evaluations if e.level_high is not None]
            tags: set[str] = set()
            for evaluation in evaluations:
                tags.update(_loads(evaluation.weakness_tags_json, []))
            results[skill] = {
                "method": "rubric",
                "level": {
                    "low": int(round(sum(lows) / len(lows))) if lows else 0,
                    "high": int(round(sum(highs) / len(highs))) if highs else 0,
                    "note": (
                        "Averaged across the tasks in this component, from two independent "
                        "evaluations each. Approximate."
                    ),
                },
                "confidence": round(
                    sum(e.confidence for e in evaluations) / len(evaluations), 2
                ),
                "weakness_tags": sorted(tags),
                "evaluation_ids": [e.id for e in evaluations],
                "rubric_version": RUBRIC_VERSION,
            }

        attempt.results_json = json.dumps(results, ensure_ascii=False)[:2000000]
        # A failed individual evaluation does not fail the attempt: the rest of
        # the results are still worth showing, and the failure is recorded on
        # the evaluation row and surfaced on the attempt.
        attempt.status = "completed"
        attempt.completed_at = _now()
        if failures:
            attempt.error = f"{failures} response(s) could not be scored; see their evaluations."
        db.commit()

        return {
            "attempt_id": attempt_id,
            "completed": True,
            "components": sorted(results),
            "failed_evaluations": failures,
        }
    except Exception as exc:
        db.rollback()
        # A transient failure (a provider blip, a timeout) is retried by the
        # maintenance worker. Marking the attempt `failed` on the first
        # exception would be a lie while a retry is queued, and the results
        # screen stops polling on any status other than submitted/evaluating --
        # so a later successful retry would never appear without a manual
        # reload. Stay in `evaluating` until the job has genuinely run out of
        # tries.
        from app.services import maintenance_jobs

        try:
            job = maintenance_jobs.celpip_evaluation_job_state(attempt_id)
        except Exception:
            job = None
        retry_pending = bool(job and job["retry_pending"])

        attempt = db.get(CelpipAttempt, attempt_id)
        if attempt is not None:
            attempt.status = "evaluating" if retry_pending else "failed"
            attempt.error = (
                f"Scoring failed and will be retried: {exc}"[:2000]
                if retry_pending
                else str(exc)[:2000]
            )
            db.commit()
        raise
    finally:
        db.close()
