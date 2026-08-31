"""Generation and validation prompts, built from the spec rather than typed out.

The per-task JSON shape a prompt asks for is derived from the same
`TaskSpec` the validator enforces, so a prompt cannot quietly drift from the
rules that reject its output. When the spec says a News Item has five
questions and one speaker, the prompt says so because it read the spec.
"""
from __future__ import annotations

import json

from app.services.celpip.schemas import NOT_GIVEN_LABEL, PARAGRAPH_LABELS
from app.services.celpip.spec import TASKS_BY_KEY, TaskSpec, word_bounds

GENERATOR_SYSTEM = """You write practice items for the CELPIP-General English test.

You are writing for one adult learner preparing for a real test in weeks, not
for a textbook. Two things matter more than anything else:

1. **The keyed answer must be provably correct from the source alone.** Every
   question carries an `evidence` field that must be an EXACT VERBATIM SPAN
   copied from the stimulus you just wrote -- not a paraphrase, not a summary.
   It is checked by string containment and the item is thrown away if the
   span is not found.
2. **Exactly one option may be defensible.** A second reviewer will answer
   your questions seeing only the stimulus and the options, with no access to
   your key. If they pick a different option, or say two options work, the
   item is discarded. Write distractors that are wrong for a reason you can
   state, not distractors that are merely vaguer.

Also required:
- Canadian setting, register, and spelling. Everyday contexts: workplaces,
  housing, transit, community services, study, family logistics.
- No specialist knowledge. A well-educated non-expert must be able to answer
  from the text alone.
- Natural spoken or written English. Scripts must sound like people talking,
  including interruptions and hedges where realistic -- not like prose read aloud.
- Nothing offensive, and no real named people, companies, or organisations.

Return ONLY a JSON object. No prose, no code fence, no commentary."""


VALIDATOR_SYSTEM = """You are an independent reviewer of CELPIP practice items.

You will be shown a stimulus and a set of questions WITHOUT the answer key.
Answer each question yourself from the stimulus alone, then judge the item.

Be strict. These items are used by someone preparing for a real test, and an
item with two defensible answers teaches them that their correct reasoning is
wrong -- worse than no practice at all.

For each question, report:
- the option you would choose
- your confidence, 0.0 to 1.0
- every OTHER option you consider also defensible, with a one-line reason

Then judge the item overall on:
- `natural_language`: does it read like real English, or like generated text
- `format_fit`: does it match the task type it claims to be
- `needs_specialist_knowledge`: could it only be answered by a subject expert
- `context_complete`: is anything needed to answer it missing

Return ONLY JSON:
{"answers": [{"index": 0, "choice": "B", "confidence": 0.9,
              "also_defensible": [{"option": "C", "reason": "..."}]}],
 "natural_language": true, "format_fit": true,
 "needs_specialist_knowledge": false, "context_complete": true,
 "notes": "one or two sentences on anything a reviewer should know"}"""


def _question_shape(task: TaskSpec) -> str:
    # Options and rationales are built from one list of keys. They were written
    # out separately, and for Reading for Information the options ran A-E while
    # the rationales example showed only A-D -- so every generated item omitted
    # the rationale for "Not given" and was rejected for it.
    if task.key == "reading_information":
        option_keys = [*PARAGRAPH_LABELS, NOT_GIVEN_LABEL]
        option_values = {
            **{label: f"Paragraph {label}" for label in PARAGRAPH_LABELS},
            NOT_GIVEN_LABEL: "Not given",
        }
        extra = (
            '\n    Options are always exactly these five. Between 1 and 3 of the 9\n'
            '    statements should be "Not given" -- ideas that sound plausible for the\n'
            '    topic but are genuinely absent from all four paragraphs. For those,\n'
            '    set "evidence" to a short note saying which paragraph a careless\n'
            '    reader would wrongly pick and why it does not actually say this.'
        )
    else:
        option_keys = ["A", "B", "C", "D"]
        option_values = {label: "..." for label in option_keys}
        extra = ""

    options = json.dumps(option_values)
    rationale_hint = {
        label: "why this is right" if label == "B" else "why this is wrong"
        for label in option_keys
    }
    rationales = json.dumps(rationale_hint)
    segment_field = (
        '\n      "segment_index": 0,   // which segment this question follows'
        if task.key == "listening_problem_solving" else ""
    )
    # A diagram is a table, not prose, so there is no span to copy. Evidence
    # there names the row and the cell values the answer rests on, and the
    # check is that those values really appear in the table.
    evidence_hint = json.dumps(
        "the row and cell values this answer rests on, copied exactly as they "
        "appear in the diagram (for example: Yoga Monday 6:00 PM)"
        if task.key == "reading_diagram"
        else "an EXACT verbatim span copied from the stimulus above"
    )
    return f"""  "questions": [            // exactly {task.question_count} of these
    {{
      "prompt": "...",{segment_field}
      "options": {options},
      "answer": "B",
      "evidence": {evidence_hint},
      "rationales": {rationales}   // one for EVERY option above, no exceptions
    }}
  ]{extra}"""


def _stimulus_shape(task: TaskSpec) -> str:
    if task.skill == "listening":
        segments = 3 if task.key == "listening_problem_solving" else 1
        speaker_note = (
            f"exactly {task.speakers} speaker" + ("s" if task.speakers != 1 else "")
        )
        limits = word_bounds(task.key)
        budget = (
            f" -- {limits[0]}-{limits[1]} words in total across all segments"
            if limits else ""
        )
        return f"""  "stimulus": {{
    "segments": [           // exactly {segments} segment(s), with {speaker_note} overall{budget}
      {{
        "index": 0,
        "speakers": [{{"name": "Priya", "gender_hint": "female"}}],
        "lines": [{{"speaker": "Priya", "text": "..."}}]
      }}
    ]
  }},"""
    if task.key == "reading_correspondence":
        return """  "stimulus": {
    "message": {"from": "...", "to": "...", "subject": "...", "body": "the message being read"},
    "reply": {"body": "a reply to that message, written normally and completely"}
  },
  // Questions 1-6 ask about the message. Questions 7-11 each quote a short
  // phrase from the REPLY and ask which word or phrase belongs there, so the
  // reply must be written in full first and the options must be alternatives
  // that fit grammatically but only one of which fits the message's meaning."""
    if task.key == "reading_diagram":
        return """  "stimulus": {
    "diagram": {
      "kind": "schedule",   // schedule | notice | advertisement | map | listing
      "title": "...",
      "columns": ["Class", "Day", "Time", "Location", "Cost"],
      "entries": [          // at least 3, each a real row rendered as a visual document
        {"Class": "...", "Day": "...", "Time": "...", "Location": "...", "Cost": "..."}
      ],
      "footnotes": ["..."]
    },
    "email": {"from": "...", "body": "an email whose blanks are filled by applying the diagram"}
  },"""
    if task.key == "reading_information":
        return """  "stimulus": {
    "paragraphs": [       // exactly four, labelled A-D, on one shared topic
      {"label": "A", "text": "..."},
      {"label": "B", "text": "..."},
      {"label": "C", "text": "..."},
      {"label": "D", "text": "..."}
    ]
  },"""
    if task.key == "reading_viewpoints":
        return """  "stimulus": {
    "article": {"title": "...", "body": "an article presenting at least two opposing positions, attributed to named people"},
    "comment": {"author": "...", "body": "a reader's comment responding to the article"}
  },
  // Roughly the first half of the questions ask about the article -- especially
  // WHO held WHICH view. The rest complete the reader's comment."""
    if task.key == "writing_email":
        return """  "stimulus": {
    "prompt": "the situation, in 2-4 sentences, ending with who to write to and why",
    "recipient": "who the email is addressed to, and their relationship to the writer",
    "bullets": ["three specific points the response must cover"],
    "register": "formal | semi-formal | informal"
  }"""
    if task.key == "writing_survey":
        return """  "stimulus": {
    "prompt": "the survey scenario, in 2-4 sentences",
    "option_a": "the first choice, stated in one line",
    "option_b": "the second choice, stated in one line",
    "bullets": ["at least two things the response must explain"],
    "recipient": "the body conducting the survey"
  }"""
    # Speaking
    fields = ['    "prompt": "what the candidate is asked to do, in 2-4 sentences"']
    if task.key in {"speaking_scene", "speaking_predictions", "speaking_unusual"}:
        fields.append(
            '    "image_brief": "a detailed description of the picture to render -- '
            'setting, people, actions, objects, and at least six concrete details a '
            'speaker could name"'
        )
    if task.key == "speaking_comparing":
        fields.append('    "image_brief": "a description of the two options shown side by side"')
        fields.append('    "option_a": "the first option"')
        fields.append('    "option_b": "the second option"')
    if task.key == "speaking_difficult_situation":
        fields.append(
            '    "people": [{"name": "...", "role": "...", "why_awkward": "..."}, '
            '{"name": "...", "role": "...", "why_awkward": "..."}]'
        )
    fields.append('    "bullets": ["two or three things a strong answer would cover"]')
    return '  "stimulus": {\n' + ",\n".join(fields) + "\n  }"


def build_generation_prompt(
    task_key: str, *, difficulty: int, topic_hint: str = "", avoid_topics: list[str] | None = None,
) -> tuple[str, str]:
    """Return (system, user) for one item generation call."""
    task = TASKS_BY_KEY[task_key]
    shape = _stimulus_shape(task)
    if task.question_count:
        shape = shape + "\n" + _question_shape(task)

    # State the length budget. Without it the generator writes a natural-sounding
    # script roughly 40% over, and the validator discards every one of them for
    # breaking a rule it was never given -- which is most of the cost of a run
    # and most of the reason the buffer fills slowly.
    length_lines: list[str] = []
    bounds = word_bounds(task_key)
    if bounds:
        low, high = bounds
        aim = round((low + high) / 2 / 10) * 10
        segments = 3 if task.key == "listening_problem_solving" else 1
        what = "spoken script" if task.skill == "listening" else "passage text"
        length_lines.append(
            f"LENGTH: the {what} must total {low}-{high} words. Aim for about {aim}. "
            "This is checked and enforced -- an item outside the range is discarded, "
            "however good it is, so keep it tight rather than rich."
        )
        if segments > 1:
            length_lines.append(
                f"Across {segments} segments that is roughly {aim // segments} words per segment."
            )

    timing = []
    if task.prep_seconds:
        timing.append(f"{task.prep_seconds}s preparation")
    if task.response_seconds:
        unit = f"{task.response_seconds // 60} minutes" if task.response_seconds >= 120 else f"{task.response_seconds}s"
        timing.append(f"{unit} to respond")
    if task.word_range:
        timing.append(f"{task.word_range[0]}-{task.word_range[1]} words")

    lines = [
        f"Write one CELPIP **{task.label}** item (Part {task.part}, {task.skill.title()}).",
        "",
        f"Task: {task.description}",
    ]
    if timing:
        lines.append(f"Official constraints: {', '.join(timing)}.")
    lines += [
        "",
        f"Target difficulty: CELPIP level {difficulty}. Pitch the vocabulary, sentence "
        f"length, and inference load at a candidate scoring {difficulty}.",
    ]
    if length_lines:
        lines += ["", *length_lines]
    if topic_hint:
        lines.append(f"Topic to use: {topic_hint}")
    if avoid_topics:
        joined = "; ".join(avoid_topics[:12])
        lines.append(
            f"Do NOT reuse these situations -- the bank already has them: {joined}"
        )
    lines += ["", "Return exactly this JSON shape:", "", "{", '  "topic": "3-6 words naming the situation",', shape, "}"]
    return GENERATOR_SYSTEM, "\n".join(lines)


def build_validation_prompt(task_key: str, payload: dict) -> tuple[str, str]:
    """Return (system, user) for the independent answer check.

    The answer key, the evidence spans, and the rationales are stripped: the
    reviewer must reach its own answer from the stimulus, or the check is
    just the generator agreeing with itself.
    """
    task = TASKS_BY_KEY[task_key]
    blind = {
        "task": task.label,
        "stimulus": payload.get("stimulus"),
        "questions": [
            {"index": i, "prompt": q.get("prompt"), "options": q.get("options")}
            for i, q in enumerate(payload.get("questions") or [])
        ],
    }
    user = (
        f"Task type: {task.label} ({task.skill}, Part {task.part}).\n"
        f"Expected: {task.description}\n\n"
        f"{json.dumps(blind, ensure_ascii=False, indent=2)}"
    )
    return VALIDATOR_SYSTEM, user


PRODUCTIVE_VALIDATOR_SYSTEM = """You are reviewing a CELPIP practice PROMPT for a
Writing or Speaking task -- there is no answer key to check, so judge only
whether the prompt itself is usable.

Reject a prompt that: does not match the official task type it claims to be;
is missing context a candidate would need; names a real person, company, or
organisation; requires specialist knowledge; reads unnaturally; or is so open
that "task fulfilment" could not be judged against it.

Return ONLY JSON:
{"format_fit": true, "context_complete": true, "needs_specialist_knowledge": false,
 "natural_language": true, "answerable_in_time": true,
 "notes": "one or two sentences"}"""


def build_productive_validation_prompt(task_key: str, payload: dict) -> tuple[str, str]:
    task = TASKS_BY_KEY[task_key]
    limit = task.response_seconds
    window = f"{limit // 60} minutes" if limit >= 120 else f"{limit} seconds"
    prep = f", after {task.prep_seconds}s preparation" if task.prep_seconds else ""
    user = (
        f"Task type: {task.label} ({task.skill}, Part {task.part}).\n"
        f"Expected: {task.description}\n"
        f"The candidate has {window}{prep} to respond"
        + (f", in {task.word_range[0]}-{task.word_range[1]} words.\n\n" if task.word_range else ".\n\n")
        + json.dumps(payload.get("stimulus"), ensure_ascii=False, indent=2)
    )
    return PRODUCTIVE_VALIDATOR_SYSTEM, user
