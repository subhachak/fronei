"""Item payload shape, and the deterministic half of validation.

Every generated item is one JSON document: a `stimulus` whose shape depends on
the task type, and (for the receptive skills) a flat `questions` list the
session runner can render without knowing which task it came from.

Two layers of validation guard the bank, and the split matters:

* **Here (deterministic).** Question counts, answer keys that name an option
  that exists, evidence that actually appears in the stimulus, a rationale for
  every distractor, speaker counts, length bounds. These are cheap, exact, and
  catch the failures that would otherwise score a learner wrongly -- a keyed
  answer with no support in the source is a silent trap, not a hard question.
* **services/celpip/validation.py (model-based).** Whether a second answer is
  also defensible, whether a distractor is accidentally correct, whether the
  language reads naturally, whether it needs specialist knowledge. Judgement
  calls no assertion can make.

Nothing reaches the learner until both pass. An item that fails here never
costs a validator call.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any

from app.services.celpip.spec import STIMULUS_WORD_BOUNDS as SPEC_WORD_BOUNDS
from app.services.celpip.spec import TASKS_BY_KEY, TaskSpec

# The receptive tasks that are answered by choosing an option.
MULTIPLE_CHOICE_SKILLS = {"listening", "reading"}

# Reading Part 3 matches statements to paragraphs, with an explicit
# "not given" option -- five options rather than the usual four.
PARAGRAPH_LABELS = ("A", "B", "C", "D")
NOT_GIVEN_LABEL = "E"

# Tasks whose stimulus is structured data rather than prose. Evidence for these
# cannot be a contiguous span: flattening a table interleaves column labels with
# values ("class yoga day monday time 6 00 pm"), so a citation of a row is never
# a substring of it. Held to the prose rule, every diagram item failed every
# question -- a run of six items kept none. Evidence here is checked by content
# instead: the values it names must really appear in the table.
STRUCTURED_STIMULUS_TASKS = {"reading_diagram"}

# Ignored when checking that evidence names real content. Function words carry
# no claim, and a citation phrased as a sentence would otherwise fail for them.
EVIDENCE_STOPWORDS = frozenset({
    "the", "and", "for", "with", "from", "that", "this", "are", "was", "were",
    "has", "have", "had", "its", "his", "her", "their", "you", "your", "run",
    "runs", "held", "starts", "start", "costs", "cost", "each", "per", "any",
    "all", "not", "but", "can", "will", "would", "there", "here", "which",
})

# Re-exported for callers that already import it from here. The definition
# lives in spec.py, so the prompt that asks for a length and the check that
# enforces it cannot drift apart.
STIMULUS_WORD_BOUNDS = SPEC_WORD_BOUNDS


class PayloadError(ValueError):
    """Raised with a human-readable list of everything wrong with an item."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


# --- Text helpers ---------------------------------------------------------

_WS = re.compile(r"\s+")
_PUNCT = re.compile(r"[^\w\s]")


def normalize(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace.

    Used for evidence containment and duplicate detection, so that a quote
    differing from its source only by a curly apostrophe or a trailing comma
    still counts as present.
    """
    return _WS.sub(" ", _PUNCT.sub(" ", (text or "").lower())).strip()


def word_count(text: str) -> int:
    return len((text or "").split())


def stimulus_text(task_key: str, payload: dict) -> str:
    """Flatten a stimulus to the plain text an answer can be evidenced against.

    This must cover everything the learner is shown, because that is exactly
    what a question may legitimately cite. Diagram footnotes were missing, and
    they are the heart of Reading Part 2 -- "members only", "except holidays",
    the qualifier that invalidates the row you would otherwise pick. A question
    keyed on one could never be evidenced, so items built around the task's
    most characteristic question were rejected on sight. The sender line and
    the comment byline were missing for the same reason.
    """
    stim = payload.get("stimulus") or {}
    parts: list[str] = []

    if task_key.startswith("listening_"):
        for segment in stim.get("segments") or []:
            for line in segment.get("lines") or []:
                parts.append(str(line.get("text", "")))
    elif task_key == "reading_correspondence":
        message = stim.get("message") or {}
        parts += [
            str(message.get("from", "")),
            str(message.get("subject", "")),
            str(message.get("body", "")),
        ]
        parts.append(str((stim.get("reply") or {}).get("body", "")))
    elif task_key == "reading_diagram":
        diagram = stim.get("diagram") or {}
        parts.append(str(diagram.get("title", "")))
        for entry in diagram.get("entries") or []:
            parts.append(" ".join(f"{k}: {v}" for k, v in entry.items()))
        parts += [str(note) for note in diagram.get("footnotes") or []]
        email = stim.get("email") or {}
        parts += [str(email.get("from", "")), str(email.get("body", ""))]
    elif task_key == "reading_information":
        for para in stim.get("paragraphs") or []:
            parts.append(str(para.get("text", "")))
    elif task_key == "reading_viewpoints":
        article = stim.get("article") or {}
        parts += [str(article.get("title", "")), str(article.get("body", ""))]
        comment = stim.get("comment") or {}
        parts += [str(comment.get("author", "")), str(comment.get("body", ""))]
    else:
        # Productive tasks: the prompt is the whole stimulus.
        parts.append(str(stim.get("prompt", "")))
        parts += [str(b) for b in (stim.get("bullets") or [])]

    return "\n".join(p for p in parts if p)


def fingerprint(task_key: str, payload: dict) -> str:
    """Stable fingerprint of an item's stimulus, for duplicate rejection.

    Hashes sorted 5-word shingles rather than the raw text so that an item
    regenerated with cosmetic differences -- reordered sentences, a renamed
    speaker -- still collides with the one already in the bank.
    """
    words = normalize(stimulus_text(task_key, payload)).split()
    if len(words) < 5:
        digest_source = " ".join(words)
    else:
        shingles = {" ".join(words[i:i + 5]) for i in range(len(words) - 4)}
        digest_source = "|".join(sorted(shingles))
    return hashlib.sha256(f"{task_key}\n{digest_source}".encode()).hexdigest()


def shingle_set(task_key: str, payload: dict) -> set[str]:
    words = normalize(stimulus_text(task_key, payload)).split()
    return {" ".join(words[i:i + 5]) for i in range(max(0, len(words) - 4))}


def similarity(a: set[str], b: set[str]) -> float:
    """Jaccard overlap between two shingle sets."""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# --- Stimulus validation --------------------------------------------------

def _validate_listening_stimulus(task: TaskSpec, stim: dict, errors: list[str]) -> None:
    segments = stim.get("segments")
    if not isinstance(segments, list) or not segments:
        errors.append("stimulus.segments must be a non-empty list")
        return
    # Problem Solving is delivered as three segments with questions between
    # them; every other listening part is one continuous piece of audio.
    expected_segments = 3 if task.key == "listening_problem_solving" else 1
    if len(segments) != expected_segments:
        errors.append(
            f"{task.label} needs exactly {expected_segments} segment(s), got {len(segments)}"
        )
    speakers_seen: set[str] = set()
    for i, segment in enumerate(segments):
        lines = segment.get("lines")
        if not isinstance(lines, list) or not lines:
            errors.append(f"segment {i} has no lines")
            continue
        declared = {str(s.get("name", "")).strip() for s in (segment.get("speakers") or [])}
        if not declared:
            errors.append(f"segment {i} declares no speakers")
        for line in lines:
            name = str(line.get("speaker", "")).strip()
            if not name:
                errors.append(f"segment {i} has a line with no speaker")
            elif declared and name not in declared:
                errors.append(f"segment {i} line spoken by undeclared speaker {name!r}")
            speakers_seen.add(name)
            if not str(line.get("text", "")).strip():
                errors.append(f"segment {i} has an empty line for {name!r}")
    if speakers_seen and len(speakers_seen) != task.speakers:
        errors.append(
            f"{task.label} expects {task.speakers} speaker(s), script has {len(speakers_seen)}"
        )


def _require_text(container: dict, key: str, label: str, errors: list[str], min_words: int = 1) -> None:
    value = str(container.get(key, "")).strip()
    if word_count(value) < min_words:
        errors.append(f"{label} is missing or too short")


def _validate_reading_stimulus(task: TaskSpec, stim: dict, errors: list[str]) -> None:
    if task.key == "reading_correspondence":
        message = stim.get("message")
        reply = stim.get("reply")
        if not isinstance(message, dict):
            errors.append("stimulus.message is required")
        else:
            _require_text(message, "body", "message.body", errors, min_words=60)
            _require_text(message, "subject", "message.subject", errors)
        if not isinstance(reply, dict):
            errors.append("stimulus.reply is required")
        else:
            _require_text(reply, "body", "reply.body", errors, min_words=30)
    elif task.key == "reading_diagram":
        diagram = stim.get("diagram")
        if not isinstance(diagram, dict):
            errors.append("stimulus.diagram is required")
        else:
            kind = str(diagram.get("kind", ""))
            if kind not in {"schedule", "notice", "advertisement", "map", "listing"}:
                errors.append(f"diagram.kind {kind!r} is not a supported visual type")
            entries = diagram.get("entries")
            # The whole point of this task is applying a *visual* document.
            # Structured entries are what the renderer turns into a real
            # schedule/notice; prose here would train the wrong skill.
            if not isinstance(entries, list) or len(entries) < 3:
                errors.append("diagram.entries must hold at least 3 structured rows")
            elif not all(isinstance(e, dict) and e for e in entries):
                errors.append("every diagram entry must be a non-empty object")
        email = stim.get("email")
        if not isinstance(email, dict):
            errors.append("stimulus.email is required")
        else:
            _require_text(email, "body", "email.body", errors, min_words=30)
    elif task.key == "reading_information":
        paragraphs = stim.get("paragraphs")
        if not isinstance(paragraphs, list) or len(paragraphs) != len(PARAGRAPH_LABELS):
            errors.append(f"stimulus.paragraphs must hold exactly {len(PARAGRAPH_LABELS)} paragraphs")
        else:
            labels = [str(p.get("label", "")).upper() for p in paragraphs]
            if labels != list(PARAGRAPH_LABELS):
                errors.append(f"paragraph labels must be {list(PARAGRAPH_LABELS)}, got {labels}")
            for para in paragraphs:
                _require_text(para, "text", f"paragraph {para.get('label')}", errors, min_words=30)
    elif task.key == "reading_viewpoints":
        article = stim.get("article")
        if not isinstance(article, dict):
            errors.append("stimulus.article is required")
        else:
            _require_text(article, "body", "article.body", errors, min_words=150)
            _require_text(article, "title", "article.title", errors)
        comment = stim.get("comment")
        if not isinstance(comment, dict):
            errors.append("stimulus.comment is required")
        else:
            _require_text(comment, "body", "comment.body", errors, min_words=30)


def _validate_productive_stimulus(task: TaskSpec, stim: dict, errors: list[str]) -> None:
    _require_text(stim, "prompt", "stimulus.prompt", errors, min_words=10)
    bullets = stim.get("bullets")
    if task.skill == "writing":
        # Both writing tasks name explicit points the response must cover;
        # without them "task fulfilment" cannot be scored against anything.
        if not isinstance(bullets, list) or len(bullets) < 2:
            errors.append("writing prompts must list at least 2 required points")
    if task.key == "writing_survey":
        if not str(stim.get("option_a", "")).strip() or not str(stim.get("option_b", "")).strip():
            errors.append("survey task needs two named options to choose between")
    if task.key in {"speaking_scene", "speaking_predictions", "speaking_comparing", "speaking_unusual"}:
        # These tasks are answered about a picture. Without an image brief
        # there is nothing to render and the task collapses into a generic
        # "talk about something" prompt.
        if not str(stim.get("image_brief", "")).strip():
            errors.append(f"{task.label} needs an image_brief to render its visual")
    if task.key == "speaking_comparing":
        if not str(stim.get("option_a", "")).strip() or not str(stim.get("option_b", "")).strip():
            errors.append("comparing task needs two options to choose between")
    if task.key == "speaking_difficult_situation":
        people = stim.get("people")
        if not isinstance(people, list) or len(people) != 2:
            errors.append("difficult-situation task needs exactly 2 people to choose between")


# --- Question validation --------------------------------------------------

def _unsupported_terms(evidence: str, source_terms: set[str]) -> list[str]:
    """Terms in a citation that the structured stimulus does not actually hold.

    Only content words count. A citation phrased as a sentence ("Yoga runs on
    Monday at 6:00 PM") should pass against a table row; one naming a class the
    table never lists should not.
    """
    return [
        term for term in normalize(evidence).split()
        if len(term) > 2 and term not in EVIDENCE_STOPWORDS and term not in source_terms
    ]


def _validate_questions(task: TaskSpec, payload: dict, errors: list[str]) -> None:
    questions = payload.get("questions")
    if not isinstance(questions, list):
        errors.append("questions must be a list")
        return
    if len(questions) != task.question_count:
        errors.append(
            f"{task.label} must carry exactly {task.question_count} questions, got {len(questions)}"
        )

    source = normalize(stimulus_text(task.key, payload))
    source_terms = set(source.split())
    segment_count = len((payload.get("stimulus") or {}).get("segments") or [])

    for i, q in enumerate(questions):
        where = f"question {i + 1}"
        if not isinstance(q, dict):
            errors.append(f"{where} is not an object")
            continue
        if not str(q.get("prompt", "")).strip():
            errors.append(f"{where} has no prompt")

        options = q.get("options")
        if not isinstance(options, dict) or len(options) < 3:
            errors.append(f"{where} needs at least 3 options")
            continue

        # Reading for Information is the matching task: its options are the
        # paragraph labels plus "not given", and nothing else.
        if task.key == "reading_information":
            expected = set(PARAGRAPH_LABELS) | {NOT_GIVEN_LABEL}
            if set(options) != expected:
                errors.append(f"{where} options must be exactly {sorted(expected)}")

        texts = [normalize(str(v)) for v in options.values()]
        if len(set(texts)) != len(texts):
            errors.append(f"{where} repeats an option")
        if any(not t for t in texts):
            errors.append(f"{where} has an empty option")

        answer = str(q.get("answer", "")).strip()
        if answer not in options:
            errors.append(f"{where} keys answer {answer!r}, which is not one of its options")
            continue

        # Evidence must be a real span of the stimulus. This is the check that
        # catches the most damaging generator failure: a plausible-looking
        # question whose keyed answer is not actually supported by the source,
        # which marks a correct learner wrong and teaches them nothing.
        evidence = str(q.get("evidence", "")).strip()
        if not evidence:
            errors.append(f"{where} cites no evidence for its answer")
        elif task.skill in MULTIPLE_CHOICE_SKILLS:
            if task.key == "reading_information" and answer == NOT_GIVEN_LABEL:
                pass  # "not given" is evidenced by absence; nothing to contain.
            elif task.key in STRUCTURED_STIMULUS_TASKS:
                unsupported = _unsupported_terms(evidence, source_terms)
                if unsupported:
                    errors.append(
                        f"{where} cites {', '.join(unsupported[:4])}, "
                        "which the diagram does not contain"
                    )
            elif normalize(evidence) not in source:
                errors.append(f"{where} cites evidence that does not appear in the stimulus")

        rationales = q.get("rationales")
        if not isinstance(rationales, dict):
            errors.append(f"{where} has no per-option rationales")
        else:
            missing = [k for k in options if k not in rationales or not str(rationales[k]).strip()]
            if missing:
                errors.append(f"{where} is missing rationales for options {missing}")

        if segment_count > 1:
            seg = q.get("segment_index")
            if not isinstance(seg, int) or not 0 <= seg < segment_count:
                errors.append(f"{where} must name the segment it belongs to (0-{segment_count - 1})")

    # A question set whose key is all one letter is a generator artefact, not
    # a test. Only meaningful once there are enough questions to judge.
    keys = [str(q.get("answer", "")) for q in questions if isinstance(q, dict)]
    if len(keys) >= 5 and len(set(keys)) == 1:
        errors.append("every question keys the same option; the answer key is degenerate")


# --- Entry point ----------------------------------------------------------

def validate_payload(task_key: str, payload: Any) -> list[str]:
    """Return every deterministic problem with an item. Empty means it passed."""
    task = TASKS_BY_KEY.get(task_key)
    if task is None:
        return [f"unknown task type {task_key!r}"]
    if not isinstance(payload, dict):
        return ["payload is not an object"]

    errors: list[str] = []
    stim = payload.get("stimulus")
    if not isinstance(stim, dict):
        return ["payload.stimulus is required"]

    if task.skill == "listening":
        _validate_listening_stimulus(task, stim, errors)
    elif task.skill == "reading":
        _validate_reading_stimulus(task, stim, errors)
    else:
        _validate_productive_stimulus(task, stim, errors)

    bounds = STIMULUS_WORD_BOUNDS.get(task_key)
    if bounds:
        count = word_count(stimulus_text(task_key, payload))
        low, high = bounds
        if not low <= count <= high:
            errors.append(f"stimulus is {count} words; {task.label} expects {low}-{high}")

    if task.question_count:
        _validate_questions(task, payload, errors)
    elif payload.get("questions"):
        errors.append(f"{task.label} is a productive task and must not carry questions")

    return errors


def assert_valid(task_key: str, payload: Any) -> None:
    errors = validate_payload(task_key, payload)
    if errors:
        raise PayloadError(errors)
