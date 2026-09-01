"""The Learn library, authored in the repo and seeded into the database.

Kept as source rather than DB-only rows so it is versioned, reviewable, and
diffable like the rest of the app. Seeded into `celpip_lessons` because study
plan items and scored weaknesses link to lessons by id -- "review this" has to
resolve to something.

Each lesson declares the weakness tags it addresses. That mapping is what lets
a scored weakness surface the right lesson automatically, instead of a
hand-maintained table that goes stale the first time a lesson is renamed.
"""
from __future__ import annotations

LESSONS: list[dict] = [
    {
        "slug": "what-is-celpip",
        "title": "What CELPIP is, and which one you need",
        "category": "overview",
        "estimated_minutes": 6,
        "summary": "The test, the two versions, and what the levels are used for.",
        "tags": [],
        "body": """
CELPIP is a Canadian English proficiency test, delivered entirely on a computer
in one sitting. Everything is typed or spoken into a headset -- there is no
interviewer, no handwriting, and no paper.

**Two versions.**

- **CELPIP-General** tests Listening, Reading, Writing, and Speaking. This is
  the one used for permanent residence applications.
- **CELPIP-General LS** tests only Listening and Speaking. This is the one used
  for Canadian citizenship.

Take the one your application asks for. They are not interchangeable, and the
General LS result cannot be used where a General result is required.

**Levels.** Results are reported per component on a scale of 1 to 12, and the
CELPIP level maps directly onto the Canadian Language Benchmark: CELPIP 9 is
CLB 9. You get four separate scores, not one average, which matters more than
it sounds -- most immigration thresholds are applied to your *lowest*
component, so a 10 in Reading does not rescue a 6 in Speaking.

**What the common targets mean.**

- CLB 9 is what Express Entry candidates chase for maximum language points.
- CLB 7 is the usual Express Entry minimum.
- CLB 5 in Listening and Speaking meets the citizenship language requirement.

**One structural fact worth internalising early.** The whole test runs about
three hours for General, and the four components run back to back. Stamina is
a real variable: people who practise only in twenty-minute blocks routinely
score lower on the sections that fall late in the sitting. That is why this app
schedules full simulations rather than only drills.
""",
    },
    {
        "slug": "test-format-overview",
        "title": "The shape of the test, section by section",
        "category": "format",
        "estimated_minutes": 8,
        "summary": "What each component contains, how long it runs, and where the traps are.",
        "tags": ["time_management"],
        "body": """
**Listening** -- roughly 47-55 minutes, 38 scored questions across six parts:
Problem Solving, a Daily Life Conversation, Listening for Information, a News
Item, a Discussion, and Viewpoints. There is an unscored practice task at the
start.

The single most important rule: **the audio plays once**. There is no replay,
no rewind, and you cannot return to a question after moving on. Everything about
how you take notes has to be built around that fact.

**Reading** -- roughly 55-60 minutes, 38 scored questions across four parts:
Reading Correspondence, Reading to Apply a Diagram, Reading for Information,
and Reading for Viewpoints. Unlike Listening, you control the pace here, which
means Reading is where poor time management does its damage: candidates lose
marks in Part 4 because they overspent in Part 1.

**Writing** -- roughly 53-60 minutes, two tasks. Writing an Email (about 27
minutes) and Responding to Survey Questions (about 26 minutes). Both want
150-200 words.

**Speaking** -- roughly 15-20 minutes, eight tasks, each with a short
preparation window and a fixed recording window. There is an unscored practice
task first. Once recording starts it does not stop early and you cannot
re-record.

**The unscored content.** Listening and Reading may contain items that do not
count, and you are not told which. This is why "I ran out of time but the last
few were probably the unscored ones" is not a strategy -- you cannot know.

**Where time is actually lost.** In Reading, over-reading Part 1. In Listening,
writing full sentences instead of key words while the audio keeps moving. In
Writing, planning past five minutes. In Speaking, spending preparation time
writing a script you then try to read aloud, which always sounds like reading.
""",
    },
    {
        "slug": "how-scoring-works",
        "title": "How you are actually scored",
        "category": "scoring",
        "estimated_minutes": 6,
        "summary": "Keyed answers for Listening and Reading, four rated dimensions for Writing and Speaking.",
        "tags": [],
        "body": """
**Listening and Reading** are keyed. Each question has one correct answer, and
your raw score out of 38 converts to a level. There is no penalty for a wrong
answer, which has one immediate practical consequence: **never leave a question
blank**. A guess is worth strictly more than a skip.

**Writing and Speaking** are rated, not counted, on four dimensions:

- **Content and Coherence** -- are your ideas developed and connected, or just
  listed? This is where most candidates who "wrote plenty" still score low.
- **Vocabulary** -- range and precision. A word used ambitiously and wrongly
  costs you; a narrow but accurate vocabulary caps you.
- **Readability** (Writing) or **Listenability** (Speaking) -- how much work the
  reader or listener has to do. For Speaking this is about comprehensibility,
  not accent. An accent is not an error.
- **Task Fulfilment** -- did you do what was asked, for the person named, in the
  register that person requires?

**Task Fulfilment is the cheapest score in the test.** If the prompt names three
things to cover and you cover two beautifully, you have capped yourself for a
reason that has nothing to do with your English. Before you write or speak,
count the requirements in the prompt. Then count them again in your answer.

**A note on this app's estimates.** The official raw-score-to-level conversion
is not published and varies by test form, so every level here is shown as an
approximate range. For Writing and Speaking, two independent evaluations are run
and reconciled; where they disagree, the range you see widens rather than being
averaged into false precision. Treat a wide range as what it is -- a signal that
the response was genuinely borderline.
""",
    },
    # --- Listening strategies -------------------------------------------
    {
        "slug": "listening-note-taking",
        "title": "Note-taking that survives a single play",
        "category": "strategy",
        "skill": "listening",
        "estimated_minutes": 7,
        "summary": "What to write down when the audio will not repeat.",
        "tags": ["detail_retrieval", "speaker_attribution"],
        "body": """
The audio plays once. Almost every avoidable Listening mark is lost to a
note-taking habit built for material you can replay.

**Write nouns, numbers, and names. Never sentences.** A full sentence takes
four seconds to write and you lose the next two sentences writing it. Symbols
and fragments only: arrows for cause, a slash for contrast, initials for people.

**Track who said what, not just what was said.** Two of the six listening parts
-- Discussion and Viewpoints -- are largely testing attribution. Use a column
per speaker from the first line, even before you know it matters. Reconstructing
"was that the manager or the customer" afterwards is impossible.

**Predict from the question stems in the pause.** You get a moment before each
audio segment. Read what you can and mark what kind of information you are
listening for: a number, a reason, a decision, an opinion. Listening for a
specific shape of answer is far more accurate than listening for everything.

**When you miss something, drop it immediately.** The most expensive error in
this section is spending the next fifteen seconds trying to recover a detail you
missed, while three more go past. Mark the question, guess, and move.

**Guess every blank before moving on.** No penalty for wrong answers, and you
cannot come back.
""",
    },
    {
        "slug": "listening-problem-solving",
        "title": "Part 1 — Listening to Problem Solving",
        "category": "strategy",
        "skill": "listening",
        "task_key": "listening_problem_solving",
        "estimated_minutes": 5,
        "summary": "Three segments, eight questions, one running problem.",
        "tags": ["detail_retrieval", "inference"],
        "body": """
Someone has a problem; options get discussed; something is decided. The audio
comes in three segments with questions after each, so the situation develops
while you are being asked about it.

**Structure your notes as problem → options → decision** from the first line.
That is the shape of every one of these, and having the frame ready means you
are filing details rather than transcribing.

**Watch for the option that gets raised and rejected.** It is the single most
common distractor in this part: something genuinely discussed, which the
speakers then decided against. "Was it mentioned" and "was it chosen" are
different questions, and the wrong options are built out of that gap.

**Later segments can revise earlier ones.** A decision made in segment one gets
changed in segment three often enough that you should treat early answers as
provisional until the part ends -- but answer them anyway when asked, because
you cannot go back.
""",
    },
    {
        "slug": "listening-daily-life",
        "title": "Part 2 — Listening to a Daily Life Conversation",
        "category": "strategy",
        "skill": "listening",
        "task_key": "listening_daily_life",
        "estimated_minutes": 4,
        "summary": "Two people, ordinary business, five questions.",
        "tags": ["detail_retrieval", "inference"],
        "body": """
An everyday exchange between two people -- arranging something, sorting out a
misunderstanding, making a plan.

**The questions are usually about relationship and intention**, not only facts.
Who are these people to each other? What does each of them want out of this
conversation? Tone carries most of that, and tone is the thing candidates
listening only for keywords miss entirely.

**Listen for the turn.** Almost every one of these conversations pivots -- an
agreement, a change of mind, a new piece of information. That pivot is where the
questions cluster.
""",
    },
    {
        "slug": "listening-information",
        "title": "Part 3 — Listening for Information",
        "category": "strategy",
        "skill": "listening",
        "task_key": "listening_information",
        "estimated_minutes": 4,
        "summary": "Dense factual content: numbers, dates, names, conditions.",
        "tags": ["detail_retrieval"],
        "body": """
This part is deliberately dense with specifics, and it is the one where
note-taking discipline pays most directly.

**Write every number with its label.** "40" is worthless three minutes later;
"40 min drive" is an answer. The distractors here are built from numbers that
appeared attached to something else.

**Conditions matter as much as facts.** "It opens at nine" and "it opens at nine
*except on Sundays*" are different answers, and the exception is what gets
tested. Listen past the fact to the qualifier that follows it.
""",
    },
    {
        "slug": "listening-news",
        "title": "Part 4 — Listening to a News Item",
        "category": "strategy",
        "skill": "listening",
        "task_key": "listening_news",
        "estimated_minutes": 4,
        "summary": "A short broadcast report, five questions.",
        "tags": ["detail_retrieval", "inference"],
        "body": """
One broadcaster, one report, no conversation to anchor you.

**News items front-load.** The what, who, and when arrive in the first two
sentences, and the rest is cause, reaction, and consequence. Knowing that shape
tells you where to concentrate.

**Attribution still matters even with one voice.** The reporter says things; so
do the people the reporter quotes. Questions exploit the difference between what
was reported and what someone claimed.

**Expect one question about implication**, not fact -- what the report suggests
will happen, or why something matters. That one is not answerable from a
transcript scan, so listen for the framing, not only the content.
""",
    },
    {
        "slug": "listening-discussion",
        "title": "Part 5 — Listening to a Discussion",
        "category": "strategy",
        "skill": "listening",
        "task_key": "listening_discussion",
        "estimated_minutes": 5,
        "summary": "Three speakers, eight questions, mostly about who thinks what.",
        "tags": ["speaker_attribution", "inference"],
        "body": """
Three people, shown on screen, discussing something they do not fully agree on.
This is the attribution part, and it is where a column-per-speaker note layout
stops being a nice habit and becomes the difference between 6 and 9.

**Set up three columns before the audio starts.** Name them the moment each
speaker is introduced. Log positions, not words.

**Track agreement as well as disagreement.** "Which two speakers agreed" is a
standard question, and it is invisible if you only recorded each person's view
in isolation.

**Watch for the speaker who changes position.** Someone usually does, and the
question will ask what they thought *by the end*.
""",
    },
    {
        "slug": "listening-viewpoints",
        "title": "Part 6 — Listening to Viewpoints",
        "category": "strategy",
        "skill": "listening",
        "task_key": "listening_viewpoints",
        "estimated_minutes": 5,
        "summary": "One longer monologue carrying several positions.",
        "tags": ["speaker_attribution", "inference"],
        "body": """
The longest listening piece, and the last one -- which means fatigue is part of
what is being tested. One voice presents several viewpoints, often including
ones the speaker does not hold.

**Separate the speaker's own view from the views they report.** "Some people
argue that…" is not the speaker's position, and the questions are built precisely
on candidates who fail to keep those apart.

**Note the discourse markers.** "However", "that said", "critics counter" are
each announcing a change of position. Marking those transitions gives you the
structure of the piece even where you lose individual sentences.

**It ends with a conclusion, and that conclusion is always tested.** Whatever
your concentration is doing by this point, hold it for the last thirty seconds.
""",
    },
    # --- Reading strategies ---------------------------------------------
    {
        "slug": "reading-time-management",
        "title": "Budgeting the Reading section",
        "category": "strategy",
        "skill": "reading",
        "estimated_minutes": 5,
        "summary": "Where the minutes go, and where they should go.",
        "tags": ["time_management", "scanning_speed"],
        "body": """
You control the pace in Reading, which is exactly why it is where pacing fails.

**A workable split of roughly 55 minutes:** about 11 minutes on Part 1, 11 on
Part 2, 13 on Part 3, and 15 on Part 4, leaving a few minutes of slack. Part 4
is last, longest, and hardest, and it is routinely reached with eight minutes
left by candidates who read Part 1 like literature.

**Read the questions first for Parts 2 and 3.** Those two are retrieval tasks --
you are locating information, not absorbing a text. Reading the passage first is
wasted work.

**For Parts 1 and 4, read the text first.** Those test understanding of a whole
argument or exchange, and question-first reading fragments it.

**Set a hard stop per part.** If you hit your limit, guess the rest of that part
and move. Marks in Part 4 are worth exactly as much as marks in Part 1, and they
are the ones that go unclaimed.
""",
    },
    {
        "slug": "reading-correspondence",
        "title": "Part 1 — Reading Correspondence",
        "category": "strategy",
        "skill": "reading",
        "task_key": "reading_correspondence",
        "estimated_minutes": 5,
        "summary": "A message, then a reply with gaps to complete.",
        "tags": ["inference", "distractor_confusion"],
        "body": """
Two halves. First, questions about a message. Then a *reply* to that message with
blanks, and you choose what belongs in each blank.

**The second half is not a grammar test.** Every option usually fits
grammatically. What decides it is the content of the original message -- the
blank is testing whether you understood what was actually said. If you are
choosing on the basis of what sounds right, you are answering the wrong question.

**Reread the relevant line of the original for each blank.** The reply is a
response, so each gap corresponds to something specific in the message.

**Watch tone.** The reply's register is set by the relationship in the message.
Some options are eliminated purely by being too formal or too casual for it.
""",
    },
    {
        "slug": "reading-diagram",
        "title": "Part 2 — Reading to Apply a Diagram",
        "category": "strategy",
        "skill": "reading",
        "task_key": "reading_diagram",
        "estimated_minutes": 5,
        "summary": "A visual document plus an email that must be completed from it.",
        "tags": ["detail_retrieval", "scanning_speed"],
        "body": """
A schedule, notice, advertisement, map, or listing, and an email whose gaps you
fill by applying it. This is a retrieval and matching task, not a comprehension
one.

**Read the email's requirements first, then scan the diagram.** The email states
constraints -- a day, a budget, a time, an accessibility need. Extract the
constraints, then go hunting.

**Constraints combine.** The right row usually satisfies two or three conditions
at once, and each wrong option satisfies all but one. When two options look
equally good, you have missed a constraint; go back to the email rather than
guessing between them.

**Check the footnotes.** Asterisks, "except", "members only", and "prices from"
exist precisely to invalidate the option you would otherwise pick.
""",
    },
    {
        "slug": "reading-information",
        "title": "Part 3 — Reading for Information",
        "category": "strategy",
        "skill": "reading",
        "task_key": "reading_information",
        "estimated_minutes": 5,
        "summary": "Match each statement to a paragraph, or to 'not given'.",
        "tags": ["distractor_confusion", "scanning_speed"],
        "body": """
Four short related passages labelled A to D, and nine statements to match. Every
statement gets a paragraph -- or E, "not given".

**"Not given" is a real answer and it is under-chosen.** Candidates assume a
statement must belong somewhere and force it into the nearest-sounding
paragraph. If you cannot point at the words that support it, it is not given.

**Match meaning, not vocabulary.** The statements paraphrase. A paragraph
sharing three words with a statement is often the trap, and the correct
paragraph frequently shares none.

**Work statement by statement, not paragraph by paragraph.** Read a statement,
decide what it claims, then scan for that claim. Re-reading all four paragraphs
nine times is how this part eats fifteen minutes.
""",
    },
    {
        "slug": "reading-viewpoints",
        "title": "Part 4 — Reading for Viewpoints",
        "category": "strategy",
        "skill": "reading",
        "task_key": "reading_viewpoints",
        "estimated_minutes": 6,
        "summary": "An article with opposing positions, then a reader's comment to complete.",
        "tags": ["speaker_attribution", "inference"],
        "body": """
The hardest reading part, and the last one. An article presents opposing
positions, usually attributed to named people, and then you complete a reader's
comment about it.

**Annotate attribution as you read.** Put a mark by each name and a one-word
label for their position. The questions turn on who held which view, and
recovering that afterwards means rereading the whole article.

**The comment half tests whether you understood the argument, not the words.**
The reader is agreeing with someone, disagreeing with someone, or splitting the
difference. Work out which before choosing anything -- once you know the
comment's stance, most options eliminate themselves.

**Protect time for this part.** It is worth ten questions, and it is the part
most often answered in a rush.
""",
    },
    # --- Writing strategies ---------------------------------------------
    {
        "slug": "writing-email",
        "title": "Task 1 — Writing an Email",
        "category": "strategy",
        "skill": "writing",
        "task_key": "writing_email",
        "estimated_minutes": 8,
        "summary": "A reusable structure, and the register decision that sets your ceiling.",
        "tags": ["organization", "register_formality", "task_missing_requirement", "task_underlength"],
        "body": """
About 27 minutes, 150-200 words, one email to a named recipient for a stated
purpose. The prompt lists things to cover -- usually three.

**Decide register before you write a word.** Who is this person to you? A
building manager, a government office, and a friend take three different
registers, and a mismatch caps Task Fulfilment no matter how clean the English.

**A structure that works every time:**

1. **Purpose** -- one sentence saying why you are writing. Not a warm-up
   paragraph; the first sentence.
2. **One paragraph per required point** -- and each one *developed*: the point,
   then a detail, reason, or consequence. A paragraph that only states the point
   is the single most common reason a competent writer scores 7 instead of 9.
3. **What you want to happen** -- the specific action or response you are asking
   for, with a timeframe if one applies.
4. **A close matching the register.**

**Budget: 4 minutes planning, 18 writing, 5 checking.** The check is not
optional -- it is where you catch the tense slips and the missing requirement.

**Before you submit, count the prompt's requirements and find each one in your
email.** If you cannot point at it, the rater cannot either.

**On length:** under 150 words is penalised. Over 200 is not rewarded --
past that point you are adding surface for errors, not marks.
""",
    },
    {
        "slug": "writing-survey",
        "title": "Task 2 — Responding to Survey Questions",
        "category": "strategy",
        "skill": "writing",
        "task_key": "writing_survey",
        "estimated_minutes": 8,
        "summary": "Choose one option and defend it. The defence is the score.",
        "tags": ["idea_development", "organization", "connector_variety"],
        "body": """
About 26 minutes, 150-200 words. A survey offers two options; you pick one and
explain why, addressed to the body running the survey.

**Pick fast and commit.** Neither option is correct. Two minutes spent choosing
is two minutes not spent developing, and a hedged answer that half-argues both
scores below a clear argument for the weaker option.

**Structure:**

1. **State your choice in the first sentence.** No preamble.
2. **Two developed reasons, one paragraph each.** Developed means: claim,
   then *why* it follows, then a concrete consequence or example. Two reasons
   properly developed beat four asserted -- comfortably.
3. **One sentence acknowledging the other option** and saying why it still does
   not win. This is where the higher bands separate: it shows you can handle a
   counter-position.
4. **A one-line close** restating the choice.

**Reasons must be specific to the scenario.** "It is more convenient" is a
placeholder. "It would let shift workers use the service after 7pm, which the
current hours exclude" is a reason.

**Register is semi-formal.** You are writing to an organisation, not a friend
and not a court.
""",
    },
    {
        "slug": "writing-common-errors",
        "title": "The errors that cost the most marks",
        "category": "strategy",
        "skill": "writing",
        "estimated_minutes": 6,
        "summary": "Patterns worth checking for in the final five minutes.",
        "tags": ["verb_tense", "subject_verb_agreement", "article_preposition",
                 "run_on_fragment", "sentence_variety", "spelling_mechanics"],
        "body": """
Rated on patterns, not single slips. One tense error is invisible; the same
tense error six times sets your Readability band.

**Check these, in this order, in your final five minutes:**

1. **Tense consistency.** Pick a time frame per paragraph and hold it. Drifting
   between past and present mid-narrative is the most common recurring error.
2. **Subject-verb agreement**, especially where words separate the subject from
   its verb: "the list of requirements *is*", not *are*.
3. **Run-ons.** Two complete sentences joined by a comma is an error, and it is
   the most frequent one in test writing. Full stop, or a linking word.
4. **Articles.** A missing "the" rarely blocks meaning but accumulates fast.
5. **Sentence variety.** If every sentence is the same length and shape, that
   caps Readability on its own. One deliberate complex sentence per paragraph is
   enough.

**Do not reach for vocabulary you are unsure of.** An ambitious word used
wrongly costs more than the plain word would have earned. Range means using
accurate words precisely, not rare ones approximately.
""",
    },
    # --- Speaking strategies --------------------------------------------
    {
        "slug": "speaking-general-technique",
        "title": "How to use the preparation window",
        "category": "strategy",
        "skill": "speaking",
        "estimated_minutes": 6,
        "summary": "Why scripting fails, and what to do with 30 seconds instead.",
        "tags": ["filler_words", "long_pauses", "incomplete_response", "idea_development"],
        "body": """
Every speaking task gives you 30 or 60 seconds to prepare and then records for a
fixed window. Three habits decide most of the score.

**Do not script.** Thirty seconds is not enough to write anything worth reading,
and a half-written script produces the worst possible delivery: you read the
part you wrote, then fall off a cliff. Note three or four *words* -- the beats
you will hit -- and speak from those.

**Plan an ending.** Running out of things to say at 40 seconds of a 60-second
window is scored as an incomplete response. Decide your last beat during
preparation, and pace towards it.

**Keep talking through a stumble.** A five-second silence while you retrieve a
word costs more than the imperfect word would have. Self-correct out loud and
move on -- that is normal speech, and it is not penalised.

**Fill the window, and stop when it stops.** Being cut off mid-sentence is
normal and is not penalised; stopping twenty seconds early is.

**On accent:** it is not scored. Listenability is about whether a listener can
follow you -- pace, clarity, structure. Speaking more slowly and clearly helps.
Trying to sound Canadian does not.
""",
    },
    {
        "slug": "speaking-advice",
        "title": "Task 1 — Giving Advice",
        "category": "strategy",
        "skill": "speaking",
        "task_key": "speaking_advice",
        "estimated_minutes": 4,
        "summary": "90 seconds, one recommendation, properly justified.",
        "tags": ["idea_development", "incomplete_response"],
        "body": """
Someone faces a choice or a difficulty and you advise them. Ninety seconds --
the longest window in the section, and the one most often under-filled.

**Frame: acknowledge → recommend → justify → anticipate.**

1. One line showing you understood their situation.
2. A single clear recommendation. Not a list of options -- that is the failure
   mode of this task.
3. Two reasons, each developed with a consequence.
4. One line addressing the obvious objection, or what to do if it does not work.

**Speak to them, not about them.** Second person throughout: "you should", "if I
were you". A response in the third person reads as commentary and misses the
task.
""",
    },
    {
        "slug": "speaking-personal-experience",
        "title": "Task 2 — Talking about a Personal Experience",
        "category": "strategy",
        "skill": "speaking",
        "task_key": "speaking_personal_experience",
        "estimated_minutes": 4,
        "summary": "60 seconds of narrative with a point.",
        "tags": ["verb_tense", "organization", "idea_development"],
        "body": """
Tell a real experience matching the prompt. Sixty seconds.

**Frame: when and where → what happened → why it mattered.** The third part is
what separates bands. A sequence of events with no significance is a list; the
task wants a story.

**Watch your tenses.** This is the task where tense consistency breaks most --
past simple for the events, past continuous for the background, and stay there.

**Pick a small, specific experience.** A detailed account of one afternoon beats
a vague summary of a year, and specificity is where vocabulary range shows.

**Invent freely if you need to.** Nobody is checking whether it happened. A
clean invented story scores better than a true one you cannot organise.
""",
    },
    {
        "slug": "speaking-describing-scene",
        "title": "Task 3 — Describing a Scene",
        "category": "strategy",
        "skill": "speaking",
        "task_key": "speaking_scene",
        "estimated_minutes": 5,
        "summary": "60 seconds describing a picture to someone who cannot see it.",
        "tags": ["vocabulary_range", "organization", "incomplete_response"],
        "body": """
You describe an image to a listener who cannot see it. This is the most
mechanical task in the section, and therefore the most reliably improvable.

**Move systematically through the picture.** Foreground, then middle, then
background -- or left to right. A fixed route stops you circling the same two
details and running dry at 35 seconds.

**Say what people are doing, not just what is there.** Present continuous
throughout: "a woman is loading boxes into a van", not "there is a woman".

**Include position language deliberately** -- "in the foreground", "behind
them", "to the left of the counter". Those phrases are directly assessable
vocabulary and they are free marks.

**Do not interpret.** Save speculation for Task 4, which is literally about
predicting. Here, describe.

**Fill the full 60 seconds.** There is always more detail: clothing, weather,
objects, expressions, signage.
""",
    },
    {
        "slug": "speaking-predictions",
        "title": "Task 4 — Making Predictions",
        "category": "strategy",
        "skill": "speaking",
        "task_key": "speaking_predictions",
        "estimated_minutes": 4,
        "summary": "60 seconds on what happens next in the scene you just described.",
        "tags": ["idea_development", "vocabulary_range"],
        "body": """
Same picture, now predict what happens next. Sixty seconds.

**Ground every prediction in something visible.** "The sky is dark, so I think
they will move the tables inside" is a scored answer. "Maybe they will go home"
is not, because nothing in the image supports it. The link between evidence and
prediction is the task.

**Use a range of future forms** -- "will", "is going to", "is likely to",
"might", "I would expect". Repeating one form is a vocabulary cap you can avoid
by deciding in advance to vary it.

**Two or three predictions, each developed**, beats six listed. Say what happens
next and then what follows from that.
""",
    },
    {
        "slug": "speaking-comparing",
        "title": "Task 5 — Comparing and Persuading",
        "category": "strategy",
        "skill": "speaking",
        "task_key": "speaking_comparing",
        "estimated_minutes": 5,
        "summary": "60 seconds preparation, 60 seconds persuading a specific person.",
        "tags": ["idea_development", "register_formality", "connector_variety"],
        "body": """
Two options are shown; you choose one and persuade a named person. The
preparation window is 60 seconds here, twice the usual.

**Persuade, do not compare.** The most common failure is a balanced comparison
of both options. You are talking to someone specific to bring them round -- the
other option exists only to be dismissed.

**Frame: choice → two reasons aimed at *that person* → dismiss the alternative
→ ask.** The middle is where the score lives: the reasons should connect to what
this particular person cares about, which the prompt tells you.

**Use comparative language explicitly** -- "cheaper than", "far more
convenient", "the main advantage over". It is directly assessable and easy to
plan.

**Address them directly.** "I really think you should" outperforms "the second
option is better".
""",
    },
    {
        "slug": "speaking-difficult-situation",
        "title": "Task 6 — Dealing with a Difficult Situation",
        "category": "strategy",
        "skill": "speaking",
        "task_key": "speaking_difficult_situation",
        "estimated_minutes": 5,
        "summary": "60 seconds preparation, and a choice of who to speak to.",
        "tags": ["register_formality", "organization", "idea_development"],
        "body": """
An awkward situation, two people you could address, and you choose one. The
hardest task in the section, because tone is being tested as much as language.

**Choose the person, then commit -- and say who you are speaking to.** The
grader needs to know which conversation this is. "Hi Mark, I wanted to talk to
you about Saturday" does that in one line.

**Frame: acknowledge → explain → propose → soften.**

1. Name the situation without blaming.
2. Explain your constraint honestly.
3. Propose a specific alternative. A complaint with no proposal scores low.
4. Close warmly enough to preserve the relationship.

**Hedging language is the skill on display here** -- "I'm really sorry to do
this", "would it be possible", "I completely understand if not". Blunt directness
scores low even when the grammar is perfect, because the task is about managing
a relationship.
""",
    },
    {
        "slug": "speaking-opinions",
        "title": "Task 7 — Expressing Opinions",
        "category": "strategy",
        "skill": "speaking",
        "task_key": "speaking_opinions",
        "estimated_minutes": 4,
        "summary": "90 seconds arguing a position.",
        "tags": ["idea_development", "connector_variety", "incomplete_response"],
        "body": """
State an opinion on an issue and defend it. Ninety seconds -- long, and often
under-filled.

**Take a side in the first sentence.** "I strongly believe that…" Balanced,
uncommitted answers cap Content and Coherence on this task specifically.

**Two developed reasons, not four listed.** Each one: claim, why it holds, then
a concrete example or consequence. Ninety seconds is roughly 220 words -- enough
for exactly this structure and not much more.

**Concede once.** One sentence acknowledging the opposing view and saying why it
does not change your position demonstrates a range of argument that a one-sided
answer cannot.

**Signpost.** "The main reason is…", "Beyond that…", "That said…" -- the
connectives are assessed, and they also stop you losing your own thread at the
70-second mark.
""",
    },
    {
        "slug": "speaking-unusual",
        "title": "Task 8 — Describing an Unusual Situation",
        "category": "strategy",
        "skill": "speaking",
        "task_key": "speaking_unusual",
        "estimated_minutes": 5,
        "summary": "60 seconds describing something the listener would not recognise.",
        "tags": ["vocabulary_range", "idea_development", "long_pauses"],
        "body": """
The last task, and the one that most rewards a prepared technique: describe an
unfamiliar object or scene to someone who cannot see it and would not recognise
it.

**Describe by comparison.** You are not expected to know the word -- you are
expected to get the idea across without it. "It looks like a large metal
umbrella, about the height of a person, with what seem to be solar panels on
top." That sentence scores well precisely because it works around a gap.

**Frame: overall impression → size and shape → parts and materials → guess at
purpose.** Having that order ready is what stops the long silence this task
otherwise produces.

**Reach for the hedging phrases deliberately** -- "it appears to be", "something
like", "I'd guess it's used for", "a sort of". They are the assessable skill
here, not decoration.

**Never stall on a missing word.** Going quiet while you search for a noun is
the worst outcome on this task. Describe around it and keep moving; that is
literally what is being tested.
""",
    },
]

# Every official task lesson gets the same actionable second layer. The core
# lesson explains the task; this playbook turns it into a repeatable routine.
# Keeping these task-specific (rather than appending generic study advice) is
# important: the useful decision rule in Reading Part 2 is not the one needed
# in Listening Part 5 or Speaking Task 6.
TASK_PLAYBOOKS: dict[str, dict[str, list[str] | str]] = {
    "listening_problem_solving": {
        "steps": ["Draw three headings: problem, options, decision.", "After each segment, mark every option +, −, or ?.", "Answer from the latest decision, not the first suggestion."],
        "rules": ["Mentioned is not selected.", "A reason introduced with ‘but’ usually belongs to the rejected option.", "If the plan changes, the final plan controls."],
        "traps": ["Choosing a vividly discussed option that was rejected", "Carrying an early decision into a later segment", "Writing sentences and missing the change of plan"],
        "drill": "Play a three-minute planning conversation once. Produce only a problem/options/decision table, then summarize the final choice in one sentence.",
    },
    "listening_daily_life": {
        "steps": ["Identify the relationship in the opening exchange.", "Write one word for each speaker’s goal.", "Mark the pivot where a misunderstanding or plan changes."],
        "rules": ["Tone answers relationship questions; keywords do not.", "The final agreement outweighs the opening request.", "An implied intention must fit both wording and tone."],
        "traps": ["Confusing polite language with friendship", "Selecting the original plan after it changes", "Treating an inference as a directly stated fact"],
        "drill": "Use a short service or scheduling dialogue. Note only relationship, goal A, goal B, pivot, and outcome; replay afterward to audit what you missed.",
    },
    "listening_information": {
        "steps": ["Create slots for names, numbers, dates, locations, and conditions.", "Attach a label to every number you write.", "Circle exceptions and eligibility words."],
        "rules": ["A number without its unit or subject is unusable.", "The clause after ‘unless’, ‘except’, or ‘only if’ often decides the answer.", "Similar figures are deliberate distractors."],
        "traps": ["Swapping two numbers from the same talk", "Ignoring a condition stated immediately after a fact", "Trying to record every sentence"],
        "drill": "Listen once to a two-minute informational announcement and build a five-column fact sheet. Score labels and conditions, not total words captured.",
    },
    "listening_news": {
        "steps": ["Capture who/what/where in the first two sentences.", "Add cause, quoted reaction, and likely consequence.", "Separate the reporter’s framing from quoted claims."],
        "rules": ["The lead carries the event; later lines explain why it matters.", "‘According to’ transfers ownership of a claim.", "Implication answers must follow from the report, not outside knowledge."],
        "traps": ["Missing the lead while preparing to take notes", "Attributing a quoted opinion to the reporter", "Choosing a plausible consequence the report never supports"],
        "drill": "Listen to a 60–90 second local news clip once. Write a six-box brief: event, people, place/time, cause, reaction, consequence.",
    },
    "listening_discussion": {
        "steps": ["Set up one column per speaker before content begins.", "Record each position in five words or fewer.", "Draw arrows when speakers agree, challenge, or change."],
        "rules": ["Answer ‘by the end’ questions from the last stated position.", "Agreement may be partial; note what they agree about.", "A speaker can repeat an idea without endorsing it."],
        "traps": ["Correct idea, wrong speaker", "Missing a late change of mind", "Assuming two speakers agree on everything"],
        "drill": "Use a three-person panel clip. Build a speaker matrix with opening view, reason, agreement, and final view for each person.",
    },
    "listening_viewpoints": {
        "steps": ["Write the topic and speaker’s thesis first.", "Start a new line at every contrast marker.", "Reserve the final line for the conclusion."],
        "rules": ["A reported viewpoint is not automatically the speaker’s view.", "Contrast markers signal where testable distinctions begin.", "The conclusion can qualify everything before it."],
        "traps": ["Assigning critics’ arguments to the narrator", "Losing structure inside a long monologue", "Relaxing before the final conclusion"],
        "drill": "Listen once to a short opinion commentary. Outline only thesis → other view → response → conclusion, then identify ownership of every claim.",
    },
    "reading_correspondence": {
        "steps": ["Identify sender, recipient, purpose, and tone.", "Map each question to the smallest relevant sentence.", "For reply gaps, verify meaning against the original message."],
        "rules": ["Grammar fit is necessary but not sufficient.", "A reply option must preserve both fact and register.", "Strong words such as always/never need equally strong textual support."],
        "traps": ["Choosing the smoothest-sounding reply", "Using general message meaning instead of the relevant line", "Missing who performed an action"],
        "drill": "Take one email and write a four-line fact map: purpose, requests, constraints, tone. Complete questions using only that map, then verify in the text.",
    },
    "reading_diagram": {
        "steps": ["Turn the email request into a constraint checklist.", "Scan columns instead of reading every row.", "Test the surviving row against footnotes and exceptions."],
        "rules": ["The correct option satisfies every constraint.", "A row matching all but one condition is a designed distractor.", "Footnotes override the main row."],
        "traps": ["Stopping after the first matching detail", "Ignoring units, dates, or membership conditions", "Reading the visual before knowing what to find"],
        "drill": "Use any timetable or product table. Invent three constraints, eliminate rows one condition at a time, and state the exact cell that eliminated each distractor.",
    },
    "reading_information": {
        "steps": ["Reduce each statement to its core claim.", "Scan paragraph topic sentences, then verify the full sentence.", "Choose Not Given unless you can point to direct support."],
        "rules": ["Shared vocabulary is not shared meaning.", "Partly true is still unsupported.", "Not Given means absent, not contradicted."],
        "traps": ["Forcing every statement into A–D", "Matching one keyword while ignoring the claim", "Confusing contradiction with absence"],
        "drill": "Read four short paragraphs and write one supported paraphrase plus one plausible-but-absent statement for each. Explain the evidence boundary aloud.",
    },
    "reading_viewpoints": {
        "steps": ["Build a name → position map while reading.", "Mark support, opposition, and qualifications.", "Determine the commenter’s stance before filling gaps."],
        "rules": ["Ownership matters as much as content.", "A qualified position is not the same as full agreement.", "Comment completions must preserve the commenter’s logic and tone."],
        "traps": ["Correct viewpoint assigned to the wrong person", "Ignoring concession words", "Reaching Part 4 without protected time"],
        "drill": "Annotate an opinion article with one margin label per person. Reconstruct the argument without looking, then check every attribution.",
    },
    "writing_email": {
        "steps": ["Underline recipient, purpose, register, and every required point.", "Plan one paragraph per requirement.", "Draft purpose first and requested action last.", "Use the final five minutes for a requirement and error audit."],
        "rules": ["Every prompt bullet must be visible in the response.", "A point needs a reason, detail, or consequence to count as developed.", "Register must remain consistent from greeting to close."],
        "traps": ["Polished writing that omits one bullet", "A long introduction before the purpose", "Generic closing with no requested action"],
        "drill": "Plan three email prompts in four minutes each without writing them. Then write one in 18 minutes and audit it with a three-bullet coverage checklist.",
    },
    "writing_survey": {
        "steps": ["Choose a side within 60 seconds.", "Plan two scenario-specific reasons and one example each.", "Acknowledge the alternative once, then defeat it.", "Check that the conclusion matches the opening choice."],
        "rules": ["Depth beats number of reasons.", "A concrete consequence develops an idea better than another adjective.", "Do not spend half the response explaining both options."],
        "traps": ["Hedging instead of choosing", "Repeating the same reason in different words", "Examples unrelated to the stated community or organisation"],
        "drill": "For five survey prompts, produce choice + reason + because + example in 90 seconds each. Write only the strongest outline as a full response.",
    },
    "speaking_advice": {
        "steps": ["Name the problem in one line.", "Give one direct recommendation.", "Develop two benefits or reasons.", "Handle one likely concern and close."],
        "rules": ["Advice must be actionable.", "Speak to the person as ‘you’.", "One developed recommendation beats a menu of possibilities."],
        "traps": ["Retelling the situation for 30 seconds", "Offering choices without recommending one", "Ending before addressing consequences"],
        "drill": "Record three 90-second answers using four cue words only: acknowledge, recommend, reason 1, reason 2/objection.",
    },
    "speaking_personal_experience": {
        "steps": ["Choose one small event.", "Set when, where, and who quickly.", "Tell the turning point in chronological order.", "End with the result or lesson."],
        "rules": ["Specific sensory details create vocabulary range.", "Past tense should dominate.", "The story needs significance, not just events."],
        "traps": ["Choosing a story too large for 60 seconds", "Tense switching", "No clear ending or relevance to the prompt"],
        "drill": "Build a bank of six adaptable stories. Practise each as five beats, never a memorized script.",
    },
    "speaking_scene": {
        "steps": ["State the overall setting.", "Move foreground → middle → background.", "Describe actions and relationships.", "Use remaining time for positions, clothing, weather, and objects."],
        "rules": ["Follow one spatial route.", "Use present continuous for actions.", "Describe visible evidence; do not predict yet."],
        "traps": ["Jumping randomly around the image", "Listing nouns without actions", "Repeating ‘there is’ in every sentence"],
        "drill": "Describe one busy photo for 60 seconds, then repeat using no more than two instances of ‘there is/are’ and at least five position phrases.",
    },
    "speaking_predictions": {
        "steps": ["Select two or three visible clues.", "Turn each clue into a prediction.", "Add the consequence that follows.", "Vary certainty language."],
        "rules": ["Every prediction needs a visible reason.", "Develop fewer predictions rather than listing many.", "Use might/likely/will to show calibrated certainty."],
        "traps": ["Predictions unrelated to the image", "Continuing to describe instead of predicting", "Repeating ‘will’ mechanically"],
        "drill": "For one image, make three clue → prediction → consequence chains in 30 seconds, then deliver them for 60 seconds.",
    },
    "speaking_comparing": {
        "steps": ["Choose one option immediately.", "Identify what the named listener values.", "Give two listener-specific advantages.", "Dismiss the alternative and make a direct ask."],
        "rules": ["The goal is persuasion, not balanced comparison.", "Comparatives must connect to the listener.", "Commitment sounds stronger than endless qualification."],
        "traps": ["Describing both pictures equally", "Giving reasons that could apply to anyone", "Forgetting to address the listener directly"],
        "drill": "Pick between two ordinary products and persuade three different people; change the reasons to fit each listener.",
    },
    "speaking_difficult_situation": {
        "steps": ["Choose whom to address and greet them.", "Acknowledge the difficulty without blame.", "Explain your constraint briefly.", "Offer a concrete alternative and preserve the relationship."],
        "rules": ["A solution is required, not only an apology.", "Tone must fit the relationship.", "Hedging softens the request without making it unclear."],
        "traps": ["Never identifying the chosen person", "Sounding accusatory", "Apologizing repeatedly without proposing a solution"],
        "drill": "Practise one scenario twice: once to a manager and once to a friend. Compare how greeting, modal verbs, explanation, and closing change.",
    },
    "speaking_opinions": {
        "steps": ["State a clear position.", "Develop reason one with an example.", "Develop reason two with a consequence.", "Concede one point, rebut it, and conclude."],
        "rules": ["Position must be clear in the first sentence.", "Examples should prove the reason, not introduce a new topic.", "A concession earns range only if you return to your position."],
        "traps": ["Giving a neutral overview", "Listing undeveloped reasons", "Losing the conclusion when the timer is nearly over"],
        "drill": "Use 15 seconds to create P-R-E-P cues: position, reason, example, position. Record for 90 seconds and mark every unsupported claim.",
    },
    "speaking_unusual": {
        "steps": ["Give the overall impression.", "Describe size, shape, colour, and material.", "Explain parts by position and comparison.", "Guess its purpose with hedging."],
        "rules": ["Describe around unknown vocabulary.", "Comparison is more useful than silence.", "Separate visible description from guessed purpose."],
        "traps": ["Freezing while searching for the exact noun", "Naming parts without explaining location", "Stating a guessed purpose as fact"],
        "drill": "Choose five unfamiliar objects. Describe each for 60 seconds without naming it; a listener should be able to sketch or identify it.",
    },
}


def _append_playbook(lesson: dict, playbook: dict[str, list[str] | str]) -> None:
    def bullets(values: list[str]) -> str:
        return "\n".join(f"- {value}" for value in values)

    lesson["body"] = lesson["body"].rstrip() + f"""

---

## Detailed execution plan

{bullets(playbook["steps"])}

## Fast decision rules

{bullets(playbook["rules"])}

## Traps to recognize immediately

{bullets(playbook["traps"])}

## A focused 10-minute drill

{playbook["drill"]}

## Final self-check

- Did I follow the task's required structure rather than improvise a new one?
- Can I point to the evidence, requirement, or visible clue behind each choice?
- Did I protect enough time to complete the response and make a final decision?
"""
    lesson["estimated_minutes"] = max(int(lesson.get("estimated_minutes", 5)), 9)


for _lesson in LESSONS:
    _task_key = _lesson.get("task_key")
    if _task_key in TASK_PLAYBOOKS:
        _append_playbook(_lesson, TASK_PLAYBOOKS[_task_key])
