"""Deterministic item validation.

These are the checks that stand between a generated item and a learner being
marked wrong for a right answer, so each test names a specific way a generator
fails in practice rather than exercising the happy path twice.
"""
from __future__ import annotations

import copy

import pytest

from app.services.celpip import schemas
from app.services.celpip.spec import TASKS_BY_KEY


def _news_item() -> dict:
    """A valid Listening: News Item -- 5 questions, one speaker, one segment."""
    script = (
        "Good evening. City council voted last night to extend the Riverside "
        "bus route by four kilometres, adding six new stops in the Eastfield "
        "district. The extension takes effect on the first of March. Council "
        "member Dana Whitfield said the change answers three years of requests "
        "from residents who currently walk more than twenty minutes to reach "
        "the nearest stop. The project will cost one point two million dollars, "
        "funded from the existing transit reserve rather than a fare increase. "
        "Service on the extended route will run every fifteen minutes during "
        "peak hours and every half hour at other times. Riders can find the "
        "revised timetable on the transit authority website from Monday. "
        "In other news, the Eastfield library will close for two weeks in "
        "April while its roof is replaced, and the community centre will host "
        "story time sessions in the meantime."
    )
    questions = []
    facts = [
        ("What did city council vote to do?", "Extend a bus route", "extend the Riverside bus route by four kilometres"),
        ("When does the extension take effect?", "The first of March", "The extension takes effect on the first of March"),
        ("How is the project funded?", "From the existing transit reserve", "funded from the existing transit reserve"),
        ("How often will buses run at peak hours?", "Every fifteen minutes", "run every fifteen minutes during"),
        ("Why will the library close?", "Its roof is being replaced", "close for two weeks in April while its roof is replaced"),
    ]
    for i, (prompt, correct, evidence) in enumerate(facts):
        options = {"A": correct, "B": f"Wrong option {i}a", "C": f"Wrong option {i}b", "D": f"Wrong option {i}c"}
        questions.append({
            "prompt": prompt,
            "options": options,
            "answer": "A",
            "evidence": evidence,
            "rationales": {
                "A": "Stated directly in the report.",
                "B": "Not mentioned.",
                "C": "Contradicts the report.",
                "D": "Confuses two details.",
            },
        })
    # Vary the key so the degenerate-key check has nothing to complain about.
    questions[1]["options"] = {"A": "Wrong", "B": "The first of March", "C": "Wrong b", "D": "Wrong c"}
    questions[1]["answer"] = "B"
    questions[3]["options"] = {"A": "Wrong", "B": "Wrong b", "C": "Every fifteen minutes", "D": "Wrong c"}
    questions[3]["answer"] = "C"
    return {
        "topic": "local transit",
        "stimulus": {
            "segments": [{
                "index": 0,
                "speakers": [{"name": "Announcer", "gender_hint": "female"}],
                "lines": [{"speaker": "Announcer", "text": script}],
            }],
        },
        "questions": questions,
    }


def test_valid_news_item_passes() -> None:
    assert schemas.validate_payload("listening_news", _news_item()) == []


def test_wrong_question_count_is_rejected() -> None:
    payload = _news_item()
    payload["questions"] = payload["questions"][:3]
    errors = schemas.validate_payload("listening_news", payload)
    assert any("exactly 5 questions" in e for e in errors), errors


def test_answer_key_naming_a_missing_option_is_rejected() -> None:
    payload = _news_item()
    payload["questions"][0]["answer"] = "E"
    errors = schemas.validate_payload("listening_news", payload)
    assert any("not one of its options" in e for e in errors), errors


def test_evidence_absent_from_the_stimulus_is_rejected() -> None:
    """The failure that silently marks a correct learner wrong: a keyed answer
    with no support anywhere in the source."""
    payload = _news_item()
    payload["questions"][0]["evidence"] = "council voted to build a new subway line"
    errors = schemas.validate_payload("listening_news", payload)
    assert any("does not appear in the stimulus" in e for e in errors), errors


def test_evidence_matching_only_by_punctuation_still_passes() -> None:
    payload = _news_item()
    payload["questions"][0]["evidence"] = "extend the Riverside bus route, by four kilometres!"
    assert schemas.validate_payload("listening_news", payload) == []


def test_missing_distractor_rationale_is_rejected() -> None:
    payload = _news_item()
    del payload["questions"][0]["rationales"]["C"]
    errors = schemas.validate_payload("listening_news", payload)
    assert any("missing rationales" in e for e in errors), errors


def test_duplicate_options_are_rejected() -> None:
    payload = _news_item()
    payload["questions"][0]["options"]["C"] = payload["questions"][0]["options"]["B"]
    errors = schemas.validate_payload("listening_news", payload)
    assert any("repeats an option" in e for e in errors), errors


def test_degenerate_answer_key_is_rejected() -> None:
    payload = _news_item()
    for q in payload["questions"]:
        first = sorted(q["options"])[0]
        q["answer"] = first
        q["evidence"] = "Good evening"
    errors = schemas.validate_payload("listening_news", payload)
    assert any("degenerate" in e for e in errors), errors


def test_speaker_count_must_match_the_task() -> None:
    """A News Item is one broadcaster. A generator that writes it as a
    two-person chat has produced a Daily Life Conversation by mistake."""
    payload = _news_item()
    segment = payload["stimulus"]["segments"][0]
    segment["speakers"].append({"name": "Guest", "gender_hint": "male"})
    segment["lines"].append({"speaker": "Guest", "text": "That is welcome news for the district."})
    errors = schemas.validate_payload("listening_news", payload)
    assert any("expects 1 speaker" in e for e in errors), errors


def test_line_from_an_undeclared_speaker_is_rejected() -> None:
    payload = _news_item()
    payload["stimulus"]["segments"][0]["lines"].append(
        {"speaker": "Mystery", "text": "Who am I."}
    )
    errors = schemas.validate_payload("listening_news", payload)
    assert any("undeclared speaker" in e for e in errors), errors


def test_problem_solving_requires_three_segments() -> None:
    payload = _news_item()
    errors = schemas.validate_payload("listening_problem_solving", payload)
    assert any("exactly 3 segment" in e for e in errors), errors


def test_stimulus_length_bounds_are_enforced() -> None:
    payload = _news_item()
    payload["stimulus"]["segments"][0]["lines"][0]["text"] = "Short."
    errors = schemas.validate_payload("listening_news", payload)
    assert any("expects 120-350" in e for e in errors), errors


def test_reading_diagram_needs_structured_entries_not_prose() -> None:
    """The Apply-a-Diagram task must render a real visual document. Prose
    pretending to be a schedule trains reading, not diagram application."""
    payload = {
        "topic": "community centre",
        "stimulus": {
            "diagram": {"kind": "schedule", "title": "Spring Class Schedule", "entries": []},
            "email": {"body": "Hi Sam, I want to sign up for a class that runs in the evening on a weekday, ideally something active, and I cannot attend before six."},
        },
        "questions": [],
    }
    errors = schemas.validate_payload("reading_diagram", payload)
    assert any("at least 3 structured rows" in e for e in errors), errors


def test_reading_information_options_must_be_the_paragraph_labels() -> None:
    payload = {
        "topic": "four rentals",
        "stimulus": {
            "paragraphs": [
                {"label": label, "text": " ".join([f"Paragraph {label} sentence {i}." for i in range(12)])}
                for label in ("A", "B", "C", "D")
            ],
        },
        "questions": [
            {
                "prompt": f"Statement {i}",
                "options": {"A": "A", "B": "B", "C": "C"},
                "answer": "A",
                "evidence": "Paragraph A sentence 1",
                "rationales": {"A": "x", "B": "y", "C": "z"},
            }
            for i in range(9)
        ],
    }
    errors = schemas.validate_payload("reading_information", payload)
    assert any("options must be exactly" in e for e in errors), errors


def test_not_given_answers_need_no_containment_evidence() -> None:
    """'Not given' is evidenced by absence -- requiring a quote would make the
    only correct kind of answer impossible to key."""
    paragraphs = [
        {"label": label, "text": " ".join([f"Paragraph {label} detail {i} about housing." for i in range(10)])}
        for label in ("A", "B", "C", "D")
    ]
    options = {"A": "A", "B": "B", "C": "C", "D": "D", "E": "Not given"}
    questions = []
    for i in range(9):
        answer = "E" if i % 3 == 0 else "A"
        questions.append({
            "prompt": f"Statement {i}",
            "options": dict(options),
            "answer": answer,
            "evidence": "no paragraph mentions this" if answer == "E" else "Paragraph A detail 1 about housing",
            "rationales": {k: "reason" for k in options},
        })
    payload = {"topic": "housing", "stimulus": {"paragraphs": paragraphs}, "questions": questions}
    assert schemas.validate_payload("reading_information", payload) == []


def test_speaking_scene_requires_an_image_brief() -> None:
    payload = {
        "topic": "park",
        "stimulus": {"prompt": "Describe what you can see in the picture below in as much detail as you can."},
    }
    errors = schemas.validate_payload("speaking_scene", payload)
    assert any("image_brief" in e for e in errors), errors


def test_writing_task_must_name_required_points() -> None:
    payload = {
        "topic": "apartment",
        "stimulus": {"prompt": "Write an email to your building manager about a repair you need in your apartment."},
    }
    errors = schemas.validate_payload("writing_email", payload)
    assert any("at least 2 required points" in e for e in errors), errors


def test_productive_task_must_not_carry_questions() -> None:
    payload = {
        "topic": "apartment",
        "stimulus": {
            "prompt": "Write an email to your building manager about a repair you need in your apartment.",
            "bullets": ["describe the problem", "say when it started", "propose a time to visit"],
        },
        "questions": [{"prompt": "x"}],
    }
    errors = schemas.validate_payload("writing_email", payload)
    assert any("must not carry questions" in e for e in errors), errors


def test_unknown_task_key_is_rejected() -> None:
    assert schemas.validate_payload("listening_karaoke", {"stimulus": {}}) == [
        "unknown task type 'listening_karaoke'"
    ]


def test_fingerprint_ignores_cosmetic_edits_but_not_new_content() -> None:
    base = _news_item()
    cosmetic = copy.deepcopy(base)
    text = cosmetic["stimulus"]["segments"][0]["lines"][0]["text"]
    cosmetic["stimulus"]["segments"][0]["lines"][0]["text"] = text.replace(".", "!")
    assert schemas.fingerprint("listening_news", base) == schemas.fingerprint("listening_news", cosmetic)

    different = copy.deepcopy(base)
    different["stimulus"]["segments"][0]["lines"][0]["text"] = (
        "Entirely different broadcast about a marathon closing downtown streets on Sunday morning."
    )
    assert schemas.fingerprint("listening_news", base) != schemas.fingerprint("listening_news", different)


def test_similarity_flags_a_near_duplicate() -> None:
    base = _news_item()
    near = copy.deepcopy(base)
    text = near["stimulus"]["segments"][0]["lines"][0]["text"]
    near["stimulus"]["segments"][0]["lines"][0]["text"] = text + " Reporting live, this is Eastfield News."
    overlap = schemas.similarity(
        schemas.shingle_set("listening_news", base), schemas.shingle_set("listening_news", near)
    )
    assert overlap > 0.85, overlap


@pytest.mark.parametrize("task_key", sorted(TASKS_BY_KEY))
def test_every_task_type_has_a_validation_path(task_key: str) -> None:
    """No task may silently accept an empty stimulus for want of a rule."""
    errors = schemas.validate_payload(task_key, {"stimulus": {}})
    assert errors, f"{task_key} accepted an empty stimulus"


# --- The generator must be told the rules it is judged by -----------------

@pytest.mark.parametrize("task_key", sorted(schemas.STIMULUS_WORD_BOUNDS))
def test_the_prompt_states_the_length_it_will_be_rejected_for(task_key: str) -> None:
    """A validator enforcing an undisclosed rule just burns generation calls.

    This was real: length bounds lived only in the validator, so the generator
    wrote naturally-sized scripts about 40% over and had every one discarded.
    A whole run could come back "0 kept, 3 rejected", all for length.
    """
    from app.services.celpip.prompts import build_generation_prompt

    low, high = schemas.STIMULUS_WORD_BOUNDS[task_key]
    _, user = build_generation_prompt(task_key, difficulty=9)

    assert f"{low}-{high} words" in user, f"{task_key} never states its length budget"
    assert "LENGTH:" in user


def test_multi_segment_items_get_a_per_segment_budget() -> None:
    """A total is hard to hold to across three separately written segments."""
    from app.services.celpip.prompts import build_generation_prompt

    _, user = build_generation_prompt("listening_problem_solving", difficulty=9)
    assert "per segment" in user


def test_the_prompt_and_the_validator_read_the_same_bounds() -> None:
    """They were separate definitions once, which is how they drifted."""
    from app.services.celpip.spec import STIMULUS_WORD_BOUNDS as spec_bounds

    assert schemas.STIMULUS_WORD_BOUNDS is spec_bounds


def test_a_stimulus_inside_the_stated_budget_passes() -> None:
    """The bound the prompt advertises has to be one an item can actually meet."""
    payload = _news_item()
    low, high = schemas.STIMULUS_WORD_BOUNDS["listening_news"]
    aim = (low + high) // 2
    words = ["Council", "confirmed", "the", "revised", "timetable", "yesterday", "evening."]
    filler = " ".join(words[i % len(words)] for i in range(aim))
    payload["stimulus"]["segments"][0]["lines"][0]["text"] = (
        "extend the Riverside bus route by four kilometres. " + filler
    )
    for question in payload["questions"]:
        question["evidence"] = "extend the Riverside bus route by four kilometres"
        question["answer"] = "A"
        question["options"] = {"A": "Extend a route", "B": "b", "C": "c", "D": "d"}
    payload["questions"][1]["answer"] = "A"
    errors = [e for e in schemas.validate_payload("listening_news", payload) if "words" in e]
    assert errors == [], errors


def _diagram_item() -> dict:
    entries = [
        {"Class": "Yoga", "Day": "Monday", "Time": "6:00 PM", "Cost": "$12"},
        {"Class": "Pottery", "Day": "Wednesday", "Time": "7:30 PM", "Cost": "$18"},
        {"Class": "Swimming", "Day": "Saturday", "Time": "9:00 AM", "Cost": "$10"},
    ]
    email = (
        "Hi Sam, I am looking for a class I could join after work on a weekday. "
        "Evenings only, and I would rather not spend more than fifteen dollars a "
        "session. Could you tell me which one fits and what it costs?"
    )
    questions = []
    for i in range(8):
        questions.append({
            "prompt": f"Question {i + 1}",
            "options": {"A": "Yoga", "B": "Pottery", "C": "Swimming", "D": "None of these"},
            "answer": "A" if i % 2 else "B",
            # Phrased as a sentence, as a person citing a table would.
            "evidence": "Yoga runs on Monday at 6:00 PM" if i % 2 else "Pottery on Wednesday costs $18",
            "rationales": {"A": "a", "B": "b", "C": "c", "D": "d"},
        })
    return {
        "topic": "community centre classes",
        "stimulus": {
            "diagram": {
                "kind": "schedule", "title": "Spring Class Schedule",
                "columns": ["Class", "Day", "Time", "Cost"], "entries": entries,
                "footnotes": ["Members pay half price."],
            },
            "email": {"from": "Alex", "body": email},
        },
        "questions": questions,
    }


def test_a_diagram_citation_may_read_as_a_sentence() -> None:
    """A table has no span to quote. Flattening one interleaves column labels
    with values, so a citation of a row is never a substring of it -- held to
    the prose rule, every diagram item failed every question."""
    errors = schemas.validate_payload("reading_diagram", _diagram_item())
    # Any evidence complaint at all, however worded -- the fallback prose rule
    # phrases it differently and would otherwise slip through this assertion.
    assert [e for e in errors if "evidence" in e or "diagram does not contain" in e] == [], errors


def test_a_diagram_citation_of_content_that_is_not_there_is_rejected() -> None:
    """Relaxing the span rule must not stop the check catching a made-up row."""
    payload = _diagram_item()
    payload["questions"][0]["evidence"] = "Fencing on Thursday costs $40"
    errors = schemas.validate_payload("reading_diagram", payload)
    assert any("diagram does not contain" in e for e in errors), errors
    assert any("fencing" in e for e in errors), errors


def test_prose_tasks_still_require_a_verbatim_span() -> None:
    """The relaxed rule is scoped to structured stimuli; prose keeps the
    stricter check, which is what catches an unsupported keyed answer."""
    payload = _news_item()
    payload["questions"][0]["evidence"] = "council voted to build a subway line"
    errors = schemas.validate_payload("listening_news", payload)
    assert any("does not appear in the stimulus" in e for e in errors), errors


@pytest.mark.parametrize("task_key", sorted(TASKS_BY_KEY))
def test_the_prompt_asks_for_a_rationale_for_every_option_it_offers(task_key: str) -> None:
    """Options and rationales were written out separately, and for Reading for
    Information the options ran A-E while the rationales example showed A-D.
    Every generated item omitted the rationale for "Not given" and was rejected
    for exactly that."""
    import json as _json
    import re as _re

    from app.services.celpip.prompts import build_generation_prompt

    if not TASKS_BY_KEY[task_key].question_count:
        pytest.skip("productive tasks carry no questions")

    _, user = build_generation_prompt(task_key, difficulty=9)

    def _object_after(label: str) -> dict:
        # The line carries a trailing comma or an inline comment; take the
        # braced object and nothing after it.
        match = _re.search(rf'"{label}": (\{{.*?\}})', user)
        assert match, f"{task_key} prompt never shows {label}"
        return _json.loads(match.group(1))

    options = _object_after("options")
    rationales = _object_after("rationales")
    assert set(rationales) == set(options), (
        f"{task_key} offers options {sorted(options)} but only shows rationales "
        f"for {sorted(rationales)}"
    )
