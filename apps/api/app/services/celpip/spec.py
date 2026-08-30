"""The CELPIP test format, as data.

Every other CELPIP module reads timings, task counts, and score conversion
from here rather than hard-coding them, for two reasons:

1. The official format is external truth this repo cannot verify at runtime.
   When a detail is corrected against celpip.ca it is corrected in one place,
   and `RUBRIC_VERSION` is bumped so historical evaluations stay interpretable
   under the rubric they were actually scored with.
2. Generation, the session runner, and the scorer must agree exactly. A
   generator that writes 6 questions for a part the runner times as 8 produces
   a test that is wrong in a way no single module can detect.

Raw-score-to-level conversion (`estimate_level_range`) is an APPROXIMATION and
is labelled as such everywhere it surfaces. Paragon does not publish the
transformation, and it varies by test form. Presenting a single exact level
from a raw score would be a claim this system cannot support.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

RUBRIC_VERSION = "2026-08-30.1"
SPEC_VERSION = "2026-08-30.1"

Skill = Literal["listening", "reading", "writing", "speaking"]
TestType = Literal["general", "general_ls"]
PracticeMode = Literal["learn", "timed", "simulation"]

SKILLS: tuple[Skill, ...] = ("listening", "reading", "writing", "speaking")
TEST_TYPE_COMPONENTS: dict[str, tuple[Skill, ...]] = {
    "general": ("listening", "reading", "writing", "speaking"),
    "general_ls": ("listening", "speaking"),
}


@dataclass(frozen=True)
class TaskSpec:
    """One official task type."""

    key: str
    skill: Skill
    part: int
    label: str
    # Questions this part contributes to the scored total. 0 for the
    # productive skills, which are rated not counted.
    question_count: int
    # Seconds. prep_seconds is speaking-only; response_seconds is the writing
    # or speaking limit; section timing for L/R is set at the section level
    # because the official test does not time individual reading parts.
    prep_seconds: int = 0
    response_seconds: int = 0
    word_range: tuple[int, int] | None = None
    # How many distinct speakers the generated audio should carry.
    speakers: int = 1
    # One line the generator, the runner, and the Learn page all quote, so the
    # learner reads the same description of the task everywhere.
    description: str = ""
    # Whether the official flow lets the candidate change an answer after
    # moving on. Enforced only in simulation mode.
    allows_answer_change: bool = True
    # Listening audio plays once in the real test.
    audio_replays: int = 0


LISTENING_TASKS: tuple[TaskSpec, ...] = (
    TaskSpec(
        key="listening_problem_solving", skill="listening", part=1,
        label="Listening to Problem Solving", question_count=8, speakers=2,
        allows_answer_change=False, audio_replays=0,
        description=(
            "A conversation in which someone describes a problem and options are "
            "discussed. The audio arrives in three segments, with questions after each."
        ),
    ),
    TaskSpec(
        key="listening_daily_life", skill="listening", part=2,
        label="Listening to a Daily Life Conversation", question_count=5, speakers=2,
        allows_answer_change=False,
        description="An everyday conversation between two people, played once, then questions.",
    ),
    TaskSpec(
        key="listening_information", skill="listening", part=3,
        label="Listening for Information", question_count=6, speakers=2,
        allows_answer_change=False,
        description="An informational exchange carrying specific facts, figures, and details.",
    ),
    TaskSpec(
        key="listening_news", skill="listening", part=4,
        label="Listening to a News Item", question_count=5, speakers=1,
        allows_answer_change=False,
        description="A short broadcast news report, followed by comprehension questions.",
    ),
    TaskSpec(
        key="listening_discussion", skill="listening", part=5,
        label="Listening to a Discussion", question_count=8, speakers=3,
        allows_answer_change=False,
        description=(
            "A discussion among three people, presented with a visual of the speakers. "
            "Questions ask who said what and what each position was."
        ),
    ),
    TaskSpec(
        key="listening_viewpoints", skill="listening", part=6,
        label="Listening to Viewpoints", question_count=6, speakers=1,
        allows_answer_change=False,
        description="A longer monologue or report presenting several viewpoints on one issue.",
    ),
)

READING_TASKS: tuple[TaskSpec, ...] = (
    TaskSpec(
        key="reading_correspondence", skill="reading", part=1,
        label="Reading Correspondence", question_count=11,
        description=(
            "A personal message, then a reply to the same message with blanks to "
            "complete. The second half tests whether the message was understood well "
            "enough to answer it."
        ),
    ),
    TaskSpec(
        key="reading_diagram", skill="reading", part=2,
        label="Reading to Apply a Diagram", question_count=8,
        description=(
            "A visual document -- a schedule, notice, advertisement, map, or event "
            "listing -- plus an email that must be completed by applying it."
        ),
    ),
    TaskSpec(
        key="reading_information", skill="reading", part=3,
        label="Reading for Information", question_count=9,
        description=(
            "Four short related passages. Each statement is matched to the paragraph "
            "it belongs to, or to 'not given' when no paragraph supports it."
        ),
    ),
    TaskSpec(
        key="reading_viewpoints", skill="reading", part=4,
        label="Reading for Viewpoints", question_count=10,
        description=(
            "An article presenting opposing positions, followed by a reader's comment "
            "to complete. Tests whose view is whose, not just what was said."
        ),
    ),
)

WRITING_TASKS: tuple[TaskSpec, ...] = (
    TaskSpec(
        key="writing_email", skill="writing", part=1,
        label="Writing an Email", question_count=0,
        response_seconds=27 * 60, word_range=(150, 200),
        description=(
            "A situation is described and an email must be written to a named "
            "recipient for a stated purpose."
        ),
    ),
    TaskSpec(
        key="writing_survey", skill="writing", part=2,
        label="Responding to Survey Questions", question_count=0,
        response_seconds=26 * 60, word_range=(150, 200),
        description=(
            "A survey presents two options. One is chosen and defended with reasons, "
            "addressed to the surveying body."
        ),
    ),
)

SPEAKING_TASKS: tuple[TaskSpec, ...] = (
    TaskSpec(
        key="speaking_advice", skill="speaking", part=1,
        label="Giving Advice", question_count=0,
        prep_seconds=30, response_seconds=90, allows_answer_change=False,
        description="Advise a named person facing a described choice or difficulty.",
    ),
    TaskSpec(
        key="speaking_personal_experience", skill="speaking", part=2,
        label="Talking about a Personal Experience", question_count=0,
        prep_seconds=30, response_seconds=60, allows_answer_change=False,
        description="Narrate a personal experience matching a given prompt.",
    ),
    TaskSpec(
        key="speaking_scene", skill="speaking", part=3,
        label="Describing a Scene", question_count=0,
        prep_seconds=30, response_seconds=60, allows_answer_change=False,
        description=(
            "Describe an image in enough detail that a listener who cannot see it "
            "could picture what is happening."
        ),
    ),
    TaskSpec(
        key="speaking_predictions", skill="speaking", part=4,
        label="Making Predictions", question_count=0,
        prep_seconds=30, response_seconds=60, allows_answer_change=False,
        description="Predict what happens next in the scene just described.",
    ),
    TaskSpec(
        key="speaking_comparing", skill="speaking", part=5,
        label="Comparing and Persuading", question_count=0,
        prep_seconds=60, response_seconds=60, allows_answer_change=False,
        description="Choose between two options shown, then persuade someone of the choice.",
    ),
    TaskSpec(
        key="speaking_difficult_situation", skill="speaking", part=6,
        label="Dealing with a Difficult Situation", question_count=0,
        prep_seconds=60, response_seconds=60, allows_answer_change=False,
        description=(
            "Handle an awkward situation by speaking to one of two people, choosing "
            "who to address and managing the message's tone."
        ),
    ),
    TaskSpec(
        key="speaking_opinions", skill="speaking", part=7,
        label="Expressing Opinions", question_count=0,
        prep_seconds=30, response_seconds=90, allows_answer_change=False,
        description="State and defend a position on a stated issue.",
    ),
    TaskSpec(
        key="speaking_unusual", skill="speaking", part=8,
        label="Describing an Unusual Situation", question_count=0,
        prep_seconds=30, response_seconds=60, allows_answer_change=False,
        description=(
            "Describe an unfamiliar or unusual object or scene to someone who cannot "
            "see it and would not recognise it."
        ),
    ),
)

ALL_TASKS: tuple[TaskSpec, ...] = LISTENING_TASKS + READING_TASKS + WRITING_TASKS + SPEAKING_TASKS
TASKS_BY_KEY: dict[str, TaskSpec] = {t.key: t for t in ALL_TASKS}
TASKS_BY_SKILL: dict[str, tuple[TaskSpec, ...]] = {
    "listening": LISTENING_TASKS,
    "reading": READING_TASKS,
    "writing": WRITING_TASKS,
    "speaking": SPEAKING_TASKS,
}


@dataclass(frozen=True)
class SectionSpec:
    """Section-level timing for a full simulation."""

    skill: Skill
    label: str
    # Total section limit in seconds. Listening is paced by its own audio, so
    # its limit is a ceiling rather than the thing that binds.
    limit_seconds: int
    scored_questions: int
    # Listening and Reading each open with an unscored practice task.
    has_practice_task: bool


SECTIONS: dict[str, SectionSpec] = {
    "listening": SectionSpec("listening", "Listening", 55 * 60, 38, True),
    "reading": SectionSpec("reading", "Reading", 60 * 60, 38, True),
    "writing": SectionSpec("writing", "Writing", 53 * 60, 0, False),
    "speaking": SectionSpec("speaking", "Speaking", 20 * 60, 0, True),
}


# --- Levels ---------------------------------------------------------------

# CELPIP levels map 1:1 onto CLB levels, so a CELPIP 9 is a CLB 9. The
# descriptors are compressed from the official level descriptions.
LEVEL_DESCRIPTORS: dict[int, str] = {
    12: "Advanced proficiency: consistently fluent, precise, and fully idiomatic.",
    11: "Advanced proficiency: highly fluent and precise, with rare lapses.",
    10: "Highly effective: fluent and well-organised, minor lapses that never obscure meaning.",
    9: "Effective: comfortable in most contexts, occasional lapses under pressure.",
    8: "Good: generally effective, with noticeable but non-blocking lapses.",
    7: "Adequate: message gets through, with recurring gaps in range or accuracy.",
    6: "Developing: functional in familiar contexts, effortful outside them.",
    5: "Limited: basic exchanges succeed, complex ones break down.",
    4: "Limited: relies on simple, familiar language and needs listener support.",
    3: "Minimal: isolated, basic communication only.",
}

# The immigration and citizenship thresholds people actually aim at, so the
# Home dashboard can say what a target level is *for*.
LEVEL_MILESTONES: dict[int, str] = {
    9: "CLB 9 -- the level most Express Entry candidates target for maximum language points.",
    7: "CLB 7 -- the common minimum for Express Entry eligibility.",
    5: "CLB 5 -- meets the language requirement for Canadian citizenship (Listening & Speaking).",
    4: "CLB 4 -- the common minimum for several PNP and trades streams.",
}


@dataclass(frozen=True)
class LevelRange:
    """An approximate level estimate, never a single exact number."""

    low: int
    high: int
    raw_score: int
    max_score: int
    note: str = (
        "Approximate. Official raw-score-to-level conversion is not published and "
        "varies by test form."
    )

    @property
    def label(self) -> str:
        return f"{self.low}" if self.low == self.high else f"{self.low}-{self.high}"

    def as_dict(self) -> dict:
        return {
            "low": self.low, "high": self.high, "label": self.label,
            "raw_score": self.raw_score, "max_score": self.max_score, "note": self.note,
        }


# Approximate percentage-correct bands for the receptive skills. Deliberately
# expressed as ranges: the honest output of 30/38 is "somewhere around 8-9",
# not "8".
_RECEPTIVE_BANDS: tuple[tuple[float, int, int], ...] = (
    (0.95, 10, 12),
    (0.89, 9, 11),
    (0.82, 8, 10),
    (0.74, 7, 9),
    (0.63, 6, 8),
    (0.52, 5, 7),
    (0.41, 4, 6),
    (0.30, 3, 5),
    (0.0, 3, 4),
)


def estimate_level_range(raw_score: int, max_score: int) -> LevelRange:
    """Approximate CELPIP level range from a receptive-skill raw score."""
    if max_score <= 0:
        return LevelRange(low=3, high=4, raw_score=0, max_score=0)
    ratio = max(0.0, min(1.0, raw_score / max_score))
    for threshold, low, high in _RECEPTIVE_BANDS:
        if ratio >= threshold:
            return LevelRange(low=low, high=high, raw_score=raw_score, max_score=max_score)
    return LevelRange(low=3, high=4, raw_score=raw_score, max_score=max_score)


# --- Rating dimensions ----------------------------------------------------

# The four official dimensions each productive skill is rated on. Readability
# is writing-only; Listenability is its speaking counterpart.
WRITING_DIMENSIONS: tuple[str, ...] = (
    "content_coherence", "vocabulary", "readability", "task_fulfillment",
)
SPEAKING_DIMENSIONS: tuple[str, ...] = (
    "content_coherence", "vocabulary", "listenability", "task_fulfillment",
)
DIMENSION_LABELS: dict[str, str] = {
    "content_coherence": "Content & Coherence",
    "vocabulary": "Vocabulary",
    "readability": "Readability",
    "listenability": "Listenability",
    "task_fulfillment": "Task Fulfillment",
}


def dimensions_for(skill: str) -> tuple[str, ...]:
    return SPEAKING_DIMENSIONS if skill == "speaking" else WRITING_DIMENSIONS


# --- Weakness taxonomy ----------------------------------------------------

# Evaluations must tag weaknesses from this fixed set. Free-text feedback
# cannot be aggregated across attempts, so it cannot drive a study plan; a
# closed vocabulary can. Every tag names something a drill can target.
WEAKNESS_TAGS: dict[str, str] = {
    # Content and structure
    "idea_development": "Ideas stated but not developed with detail, example, or reason.",
    "organization": "Weak paragraphing or ordering; the reader has to reconstruct the shape.",
    "connector_variety": "Narrow or repetitive linking between ideas.",
    "task_underlength": "Response falls short of the expected length.",
    "task_missing_requirement": "One or more explicit prompt requirements went unaddressed.",
    "off_topic_drift": "Response drifts from what the prompt actually asked.",
    # Language
    "verb_tense": "Tense choice or consistency errors.",
    "subject_verb_agreement": "Agreement errors.",
    "article_preposition": "Article and preposition errors.",
    "sentence_variety": "Sentences are uniformly simple or uniformly overlong.",
    "run_on_fragment": "Run-on sentences or fragments.",
    "word_choice": "Imprecise or unidiomatic word choice.",
    "vocabulary_range": "Narrow lexical range; the same words carry too much work.",
    "register_formality": "Tone mismatched to the audience the prompt names.",
    "spelling_mechanics": "Spelling, capitalisation, and punctuation.",
    # Delivery (speaking)
    "pace_too_fast": "Delivery outruns clarity.",
    "pace_too_slow": "Delivery too slow to cover the task in the time given.",
    "filler_words": "Frequent fillers interrupt the message.",
    "long_pauses": "Pauses long enough to break the listener's thread.",
    "repetition": "Repeats words or whole phrases while searching for the next idea.",
    "incomplete_response": "Ran out of time or stopped before finishing the task.",
    "intelligibility": "Passages a listener would struggle to follow.",
    # Receptive
    "detail_retrieval": "Misses specific stated facts, numbers, or names.",
    "inference": "Misses meaning that is implied rather than stated.",
    "speaker_attribution": "Confuses who held which view.",
    "distractor_confusion": "Chooses plausible-sounding options contradicted by the source.",
    "scanning_speed": "Correct when untimed, wrong or unfinished under time pressure.",
    "time_management": "Time spent is badly distributed across the section.",
}


def is_valid_weakness_tag(tag: str) -> bool:
    return tag in WEAKNESS_TAGS


def components_for_test_type(test_type: str) -> tuple[Skill, ...]:
    return TEST_TYPE_COMPONENTS.get(test_type, TEST_TYPE_COMPONENTS["general"])


def task_keys_for_test_type(test_type: str) -> list[str]:
    skills = components_for_test_type(test_type)
    return [t.key for t in ALL_TASKS if t.skill in skills]


def spec_summary() -> dict:
    """Serialisable format description for the frontend Learn pages, so the UI
    never restates timings that could drift from the ones the runner enforces."""
    return {
        "spec_version": SPEC_VERSION,
        "rubric_version": RUBRIC_VERSION,
        "sections": [
            {
                "skill": s.skill, "label": s.label,
                "limit_seconds": s.limit_seconds,
                "scored_questions": s.scored_questions,
                "has_practice_task": s.has_practice_task,
                "tasks": [
                    {
                        "key": t.key, "part": t.part, "label": t.label,
                        "question_count": t.question_count,
                        "prep_seconds": t.prep_seconds,
                        "response_seconds": t.response_seconds,
                        "word_range": list(t.word_range) if t.word_range else None,
                        "speakers": t.speakers,
                        "description": t.description,
                        "allows_answer_change": t.allows_answer_change,
                    }
                    for t in TASKS_BY_SKILL[s.skill]
                ],
            }
            for s in SECTIONS.values()
        ],
        "levels": LEVEL_DESCRIPTORS,
        "milestones": LEVEL_MILESTONES,
        "dimensions": DIMENSION_LABELS,
        "weakness_tags": WEAKNESS_TAGS,
    }
