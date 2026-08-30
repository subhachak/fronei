"""Deterministic scoring: delivery metrics and keyed receptive review.

The model-driven half of scoring is covered by its own contract (two passes,
reconciliation) and is not exercised here. What is exercised is everything a
learner's result depends on that must be exactly right without a model: the
timing-derived speaking metrics, and the keyed comparison that decides whether
an answer was correct.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

from app.services.celpip.scoring import (
    _receptive_weakness_tags,
    delivery_metrics,
    score_receptive_question,
)


def _words(pairs: list[tuple[str, float, float]]) -> list[dict]:
    return [{"word": w, "start": s, "end": e} for w, s, e in pairs]


def test_pace_and_completeness_from_timings() -> None:
    # 150 words spoken evenly over 60 seconds of a 60-second task.
    words = _words([(f"word{i}", i * 0.4, i * 0.4 + 0.35) for i in range(150)])
    metrics = delivery_metrics(
        transcript=" ".join(f"word{i}" for i in range(150)),
        words=words, limit_seconds=60, audio_duration=60.0,
    )
    assert metrics["word_count"] == 150
    assert metrics["words_per_minute"] == 150.0
    assert metrics["time_used_ratio"] == 1.0
    assert "incomplete_response" not in metrics["flags"]
    assert "pace_too_fast" not in metrics["flags"]


def test_stopping_early_is_flagged_incomplete() -> None:
    words = _words([(f"word{i}", i * 0.4, i * 0.4 + 0.35) for i in range(40)])
    metrics = delivery_metrics(
        transcript=" ".join(f"word{i}" for i in range(40)),
        words=words, limit_seconds=60, audio_duration=20.0,
    )
    assert metrics["time_used_ratio"] == 0.33
    assert "incomplete_response" in metrics["flags"]


def test_long_pauses_are_detected_from_gaps_not_text() -> None:
    """The whole reason word timings are kept: this transcript and a fluent
    one are identical as text."""
    words = _words([
        ("I", 0.0, 0.2), ("think", 0.2, 0.6),
        ("the", 4.0, 4.2), ("best", 4.2, 4.6),      # 3.4s gap
        ("option", 9.0, 9.5),                        # 4.4s gap
        ("is", 13.0, 13.2),                          # 3.5s gap
    ])
    metrics = delivery_metrics(
        transcript="I think the best option is", words=words,
        limit_seconds=60, audio_duration=14.0,
    )
    assert metrics["pause_count"] == 3
    assert metrics["longest_pause_seconds"] > 4
    assert "long_pauses" in metrics["flags"]


def test_multiword_fillers_are_counted_once_not_twice() -> None:
    transcript = "you know I mean um the thing is you know basically that"
    metrics = delivery_metrics(
        transcript=transcript, words=[], limit_seconds=60, audio_duration=10.0,
    )
    # "you know" x2, "I mean" x1, "um" x1, "basically" x1 == 5.
    assert metrics["filler_count"] == 5
    assert "filler_words" in metrics["flags"]


def test_immediate_repetition_is_counted() -> None:
    metrics = delivery_metrics(
        transcript="I I think that that the the plan plan works",
        words=[], limit_seconds=60, audio_duration=10.0,
    )
    assert metrics["immediate_repeats"] >= 4
    assert "repetition" in metrics["flags"]


def test_metrics_survive_a_transcript_with_no_word_timings() -> None:
    """Transcription can come back without timings; scoring must not crash,
    and must say the timings are missing rather than inventing pauses."""
    metrics = delivery_metrics(
        transcript="A short answer.", words=[], limit_seconds=60, audio_duration=0.0,
    )
    assert metrics["has_word_timings"] is False
    assert metrics["pause_count"] == 0
    assert metrics["words_per_minute"] == 0.0


def _question(payload: dict) -> SimpleNamespace:
    return SimpleNamespace(id="q1", task_key="reading_information", payload_json=json.dumps(payload))


def _response(index: int, choice: str | None, *, late: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        question_index=index, selected_option=choice, time_spent_ms=1000, late=late,
    )


def _keyed_payload() -> dict:
    return {
        "stimulus": {"paragraphs": []},
        "questions": [
            {
                "prompt": "Statement one",
                "options": {"A": "A", "B": "B", "C": "C"},
                "answer": "B",
                "evidence": "the second paragraph says so",
                "rationales": {"A": "wrong because x", "B": "right because y", "C": "wrong because z"},
            },
            {
                "prompt": "Statement two",
                "options": {"A": "A", "B": "B", "C": "C"},
                "answer": "A",
                "evidence": "the first paragraph says so",
                "rationales": {"A": "right", "B": "wrong", "C": "wrong"},
            },
        ],
    }


def test_receptive_review_reports_evidence_and_distractor_reasons() -> None:
    result = score_receptive_question(
        _question(_keyed_payload()), [_response(0, "B"), _response(1, "C")]
    )
    assert result["correct"] == 1
    assert result["total"] == 2

    right, wrong = result["questions"]
    assert right["correct"] is True
    assert right["evidence"] == "the second paragraph says so"
    assert right["why_correct"] == "right because y"
    # The key itself is not repeated among the distractor explanations.
    assert "B" not in right["why_others_wrong"]
    assert set(right["why_others_wrong"]) == {"A", "C"}

    assert wrong["correct"] is False
    assert wrong["chosen"] == "C"
    assert wrong["why_others_wrong"]["C"] == "wrong"


def test_unanswered_question_is_wrong_but_marked_unanswered() -> None:
    """Skipped and wrong both cost a mark, but only one of them is a timing
    problem, and the study plan needs to tell them apart."""
    result = score_receptive_question(_question(_keyed_payload()), [_response(0, "B")])
    assert result["correct"] == 1
    skipped = result["questions"][1]
    assert skipped["answered"] is False
    assert skipped["correct"] is False
    assert skipped["chosen"] is None


def test_widespread_skipping_is_tagged_as_time_management() -> None:
    items = [{
        "task_key": "reading_information",
        "questions": [
            {"answered": False, "correct": False} for _ in range(3)
        ] + [{"answered": True, "correct": True} for _ in range(7)],
    }]
    assert "time_management" in _receptive_weakness_tags(items)


def test_viewpoint_errors_are_tagged_as_attribution() -> None:
    items = [{
        "task_key": "listening_viewpoints",
        "questions": [{"answered": True, "correct": False}, {"answered": True, "correct": True}],
    }]
    tags = _receptive_weakness_tags(items)
    assert "speaker_attribution" in tags
    assert "distractor_confusion" not in tags


def test_a_late_answer_reads_as_unanswered_not_as_a_wrong_choice(_late_marker=None) -> None:
    """Dropping a late answer must not look like the learner picked wrongly —
    the review screen has to explain why it did not count."""
    result = score_receptive_question(
        _question(_keyed_payload()), [_response(0, "B", late=True), _response(1, "A")]
    )
    assert result["correct"] == 1
    assert result["late_excluded"] == 1

    dropped = result["questions"][0]
    assert dropped["late"] is True
    assert dropped["answered"] is False
    assert dropped["chosen"] is None
    assert dropped["correct"] is False
