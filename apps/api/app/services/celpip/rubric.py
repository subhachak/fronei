"""Scoring rubric and prompts for the productive skills.

Writing and Speaking are rated on four dimensions, scored twice by independent
passes, and reconciled when the two materially disagree. That structure is the
point of this module: a level estimate the learner will reorganise a week of
study around has to be reproducible, and a single call to a single model is
not. Where the passes diverge, the divergence itself is reported as reduced
confidence rather than hidden behind an average.

Two rules the prompts enforce that are easy to get wrong:

* **Evidence must be quoted from the learner's own response.** A criticism the
  learner cannot locate in what they wrote is unactionable, and an ungrounded
  criticism is often simply invented.
* **Pronunciation feedback addresses comprehensibility only.** A legitimate
  accent is not an error, and the official criterion is whether a listener can
  follow -- not whether the speaker sounds Canadian.
"""
from __future__ import annotations

import json

from app.services.celpip.spec import (
    DIMENSION_LABELS,
    RUBRIC_VERSION,
    TASKS_BY_KEY,
    WEAKNESS_TAGS,
    dimensions_for,
)

# Compressed band anchors. Deliberately behavioural ("develops each point with
# a reason or example") rather than evaluative ("good development"), so two
# independent passes are judging the same thing.
BAND_ANCHORS: dict[str, str] = {
    "11-12": (
        "Fully developed ideas with precise support; effortless organisation; "
        "wide, accurate, idiomatic vocabulary; error-free or near enough that a "
        "reader never pauses; every task requirement met and extended."
    ),
    "9-10": (
        "Ideas developed with reasons and examples; clear organisation with "
        "purposeful transitions; varied vocabulary used accurately with occasional "
        "imprecision; minor errors that never obscure meaning; all task requirements met."
    ),
    "7-8": (
        "Main ideas present and mostly developed, some points asserted without "
        "support; organisation visible but mechanical; adequate vocabulary with "
        "noticeable repetition or imprecision; recurring errors that occasionally "
        "slow the reader; most task requirements met."
    ),
    "5-6": (
        "Ideas listed rather than developed; organisation loose or formulaic; "
        "limited vocabulary with frequent repetition; errors frequent enough to "
        "force rereading; one or more task requirements thin or missed."
    ),
    "3-4": (
        "Fragmentary ideas; little organisation; very limited vocabulary; errors "
        "throughout that regularly obscure meaning; task largely unfulfilled."
    ),
}

DIMENSION_GUIDANCE: dict[str, str] = {
    "content_coherence": (
        "Are the ideas developed, relevant, and connected? Judge whether each point "
        "carries a reason, example, or consequence -- not whether the response sounds fluent."
    ),
    "vocabulary": (
        "Range, precision, and idiomaticity. A narrow but accurate vocabulary scores "
        "lower than a wide accurate one, and an ambitious word used wrongly is an error, "
        "not evidence of range."
    ),
    "readability": (
        "Grammar, sentence structure, spelling, punctuation, and paragraphing -- how much "
        "effort the reader spends decoding rather than understanding."
    ),
    "listenability": (
        "Grammar, sentence structure, pace, and clarity -- how much effort the LISTENER "
        "spends. Judge comprehensibility only. A legitimate accent is not an error and "
        "must never lower this score."
    ),
    "task_fulfillment": (
        "Did the response do what the prompt asked, for the audience the prompt named, "
        "in the register that audience requires? Check every stated requirement individually."
    ),
}


def _weakness_menu() -> str:
    return "\n".join(f"  {tag}: {desc}" for tag, desc in WEAKNESS_TAGS.items())


def _anchor_block() -> str:
    return "\n".join(f"  Level {band}: {text}" for band, text in BAND_ANCHORS.items())


def build_scorer_prompt(task_key: str, *, pass_label: str) -> str:
    """System prompt for one independent evaluator pass."""
    task = TASKS_BY_KEY[task_key]
    dims = dimensions_for(task.skill)
    dim_block = "\n".join(
        f"  {d} ({DIMENSION_LABELS[d]}): {DIMENSION_GUIDANCE[d]}" for d in dims
    )
    limit = task.response_seconds
    window = f"{limit // 60} minutes" if limit >= 120 else f"{limit} seconds"
    length = (
        f"The expected length is {task.word_range[0]}-{task.word_range[1]} words."
        if task.word_range else
        f"The candidate had {window} of speaking time after {task.prep_seconds}s of preparation."
    )

    speaking_note = ""
    if task.skill == "speaking":
        speaking_note = (
            "\n\nYou are reading a TRANSCRIPT of speech, not writing. Do not penalise "
            "features of ordinary spoken English -- false starts, self-correction, "
            "contractions, or informal connectives -- unless they genuinely impede a "
            "listener. Never comment on accent. Transcription artefacts are not the "
            "candidate's errors.\n"
            "Pace, pauses, and filler counts are measured separately and given to you "
            "as data. Use them, do not estimate them."
        )

    return f"""You are evaluator {pass_label}, rating one CELPIP {task.label} response.

Rate INDEPENDENTLY. Another evaluator is rating the same response separately and
the two ratings are compared; agreeing with an imagined consensus defeats the
purpose. Rate what is in front of you.

Task: {task.description}
{length}{speaking_note}

Rate each dimension on the CELPIP 1-12 scale:
{dim_block}

Band anchors:
{_anchor_block()}

Rules:
- Every criticism must quote the candidate's own words. If you cannot quote it,
  do not claim it.
- Check each stated task requirement individually and report any that went unmet.
- Tag weaknesses ONLY from this closed list, using the exact keys:
{_weakness_menu()}
- Do not write an improved version of the response. That is a separate step and
  including it here biases your own score.

Return ONLY JSON:
{{
  "dimensions": {{
    {json.dumps(dims[0])}: {{"level": 9, "evidence": ["exact quote from the response"],
                            "comment": "one or two sentences"}}
  }},
  "overall_level": 9,
  "confidence": 0.8,
  "met_requirements": ["requirement the response satisfied"],
  "missing_requirements": ["requirement the response did not satisfy"],
  "corrections": [
    {{"severity": "high|medium|low", "original": "exact quote",
      "corrected": "the fix", "why": "short reason"}}
  ],
  "patterns": ["recurring habit worth naming, not a one-off slip"],
  "weakness_tags": ["exact keys from the list above"],
  "outline": ["what a stronger response would have covered, point by point"],
  "strengths": ["what genuinely worked, quoted"]
}}
Include an entry in "dimensions" for every one of: {', '.join(dims)}."""


RECONCILER_SYSTEM = """You are reconciling two independent evaluations of the same
CELPIP response.

You see the response, the task, and both evaluations. Decide the final rating.

- Where the two agree, keep the agreed level.
- Where they differ, decide which reading the response actually supports, using the
  quoted evidence. Do not split the difference as a reflex -- if one evaluator
  quoted evidence and the other asserted, prefer the evidence.
- If the disagreement is genuinely unresolvable, say so and widen the range rather
  than inventing precision.
- Report a confidence that reflects the disagreement: two passes a level apart is a
  softer estimate than two that matched exactly.

Return ONLY JSON:
{"dimensions": {"content_coherence": {"level": 9, "comment": "..."}},
 "overall_level_low": 8, "overall_level_high": 9, "confidence": 0.7,
 "disagreements": [{"dimension": "vocabulary", "a": 8, "b": 10,
                    "resolution": "why the final level is what it is"}],
 "summary": "two or three sentences the learner reads first"}"""


EXEMPLAR_SYSTEM = """You write model answers for a CELPIP candidate.

You are given the task, the candidate's own response, and the rating it already
received. The score is fixed and you cannot change it -- your job is to show the
candidate what the next level up looks like *for their own attempt*, not to write
an unrelated perfect answer.

Rules:
- Keep the candidate's own content, position, and examples wherever they work.
  A model answer about a different situation teaches nothing transferable.
- Stay inside the official length or time limit. An exemplar the candidate could
  not have produced in the time given is not a target.
- After the model answer, name what specifically changed and why each change
  raises the level.
- Give ONE focused retry exercise: a narrow, repeatable drill aimed at the single
  weakness that costs the most, not general advice.

Return ONLY JSON:
{"exemplar": "the improved response in full",
 "target_level": 10,
 "changes": [{"change": "what changed", "why": "why it raises the level"}],
 "retry_exercise": {"title": "...", "instructions": "...", "time_minutes": 10}}"""


def rubric_version() -> str:
    return RUBRIC_VERSION
