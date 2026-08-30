# CELPIP preparation app — design and implementation plan

**Status:** design agreed, build in progress.
**Deadline context:** the owner sits a CELPIP test roughly one month out. That single
fact decides most of the trade-offs below. Anything that helps a real person raise a
real score stays; anything that exists to administer content for *other* people goes.

---

## 0. What already exists, confirmed by reading the code

| Capability | Where | How CELPIP uses it |
|---|---|---|
| Admin gate | `app/auth.py` `RequireAdmin`; `apps/web/app/admin/hooks/useAdmin.ts` | Every CELPIP endpoint takes `RequireAdmin`. The UI gate is cosmetic; the server check is the boundary. |
| Draft → review → publish vertical | `app/routers/blog.py` | Shape reference for the Question Bank, **not** its gating policy (see §5). |
| Model selection by role | `app/services/agent/model_policy.py` | New CELPIP roles become tunable from the existing Model policy tab with no deploy. |
| LLM call | `model_client.complete(messages, role=..., max_tokens=..., timeout_s=...)` | Generation, validation, and evaluation all go through it. |
| JSON coercion | `app/services/agent/research_utils._parse_json` | Every structured LLM response. |
| Durable job queue | `app/services/maintenance_jobs.py` — claim/lease/heartbeat/complete/requeue, dispatched by `execute_job(job_type, payload)` | Generation runs, TTS synthesis, and evaluation batches. |
| Blob storage | `app/services/blob_store.py` — local + S3, presigned URLs | Listening audio, speaking recordings, diagram assets. |
| Idempotent migrations | `app/db/migration_helpers.py` — `table_exists` / `column_exists` / `index_exists` | The CELPIP migration. |

**The one genuinely new capability:** speech. Nothing in this repo does text-to-speech or
transcription today; `llm_gateway` is LiteLLM text-only. Listening and Speaking both
depend on a new `app/services/speech.py`. This is the highest-risk piece, so it is
exercised early rather than at integration time.

---

## 1. Product shape

Three surfaces for the learner, one for the operator.

- **Home** — countdown, target level, latest four component levels, readiness, today's
  recommended activity, weekly completion, weakest task types, recent attempts, and one
  prominent *Start today's plan* / *Resume* button.
- **Learn** — the full library: what CELPIP is, General vs General-LS, the
  computer-delivered format, per-section anatomy with official timings, the 1–12 band and
  CLB mapping, and a strategy page per task type that links straight into the matching drill.
- **Practice** — every official task type, in three modes (§6).
- **Mock Tests** — full General, full LS, single component, or a custom set.
- **Results** — per-attempt review with evidence, distractor explanations, corrections,
  model responses, and the retry loop.
- **Study Plan** — the adaptive one-month schedule.
- **Question Bank** — operator surface: preview, edit, regenerate, disable, approve.

### Placement

One `CELPIP` entry in the existing admin navigation, which opens a **dedicated
`/celpip` route with its own shell** rather than rendering inside `AdminShell`'s tab
strip. Reason: a timed exam simulation needs full-screen and distraction-free, and the
admin panel's scrolling pane under a tab bar actively fights that. The route reuses
`useAdmin()` for the client gate and every endpoint independently enforces `RequireAdmin`.

API prefix: `/admin/celpip`, mounted as its own router module — the CELPIP workspace has
its own service layer and tables and does **not** route through Fronei's conversational
agent stack.

---

## 2. Readiness is computed, not opined

A model-generated "you're 72% ready" is worthless. Readiness is a weighted function of
signals the system actually holds:

| Signal | Meaning |
|---|---|
| Recent component levels | Latest estimate per component vs target |
| Full-test completion | Has a complete simulation been sat, and how recently |
| Consistency | Variance across recent attempts in the same component |
| Timing success | Fraction of tasks completed inside the official limit |
| Coverage | Fraction of the official task types attempted at all |
| Recency | Decay on stale practice |

Each sub-score is stored alongside the composite so the Home dashboard can explain *why*
readiness moved, and so a low number points at the specific gap.

---

## 3. Data model

One migration, idempotent, guarded with `table_exists`. Confirm the current head with
`alembic heads` before setting `down_revision` — the tree has had concurrent branches.

| Table | Holds |
|---|---|
| `celpip_profiles` | Test type, test date, target level, weekday/weekend hours, self-identified weaknesses, onboarding state. |
| `celpip_lessons` | Learn content. Seeded from repo-versioned source, stored in DB so it is linkable/queryable from plans and results. |
| `celpip_questions` | The bank. One row = one self-contained task instance: skill, part, `payload_json` (stimulus + questions + keyed answers + per-distractor rationale), difficulty, source, status, validation verdict, model + prompt version, serve count. |
| `celpip_question_assets` | Audio, diagram images, and transcripts, by blob location + content hash. |
| `celpip_tests` | An assembled test: mode, component set, label. |
| `celpip_test_items` | Ordered membership, including any unscored-item flag. |
| `celpip_attempts` | A sitting. States: `not_started`, `in_progress`, `submitted`, `evaluating`, `completed`, `failed`. Carries section timers and the server-side clock. |
| `celpip_responses` | One answer: choice or text, audio blob + transcript for speaking, time spent, flagged-for-review. |
| `celpip_evaluations` | Scoring output. Stores **both** evaluator passes, the reconciled estimate, model versions, rubric version, confidence range, and timestamp. |
| `celpip_study_plan_items` | Scheduled activities with date, type, target task types, state, and rebalance history. |
| `celpip_generation_runs` | Audit of every generation batch: spec, prompts, accepted/rejected counts, rejection reasons. |

---

## 4. Timing and scoring authority

Official task counts, timings, and the raw-to-band conversion are treated as **data, not
code** — a single versioned `celpip_spec.py` constant holding the format definition and
the conversion table, with the rubric version recorded on every evaluation row. Two
consequences: the format can be corrected in one place when verified against the official
source, and every historical score stays interpretable because it names the rubric it was
scored under.

Raw-score-to-level conversion is surfaced to the learner as **an approximate range, always
labelled as such**. Official transformations vary by test form; presenting a single exact
number would be a lie the product cannot back up.

---

## 5. Generation pipeline

```
test specification
   ↓ prompt / script / passage generation
   ↓ question + keyed-answer + distractor-rationale generation
   ↓ independent answer validation   ← separate model call, sees only stimulus + questions
   ↓ difficulty + format validation  ← schema + spec conformance, deterministic where possible
   ↓ audio / visual asset generation
   ↓ ready for practice
```

An item is **rejected** when: more than one answer is defensible; the keyed answer has no
supporting evidence in the source; a distractor is accidentally correct; the task diverges
from the official format; required context is missing; the language is unnatural; it needs
specialist knowledge; audio and transcript disagree; or it substantially duplicates the bank
(checked by embedding-free normalized-shingle similarity against existing stimuli).

**Validated content is servable immediately.** Manual approval exists in the Question Bank
but never gates practice — a review queue the owner has to clear before studying would cost
more preparation time than it saves. The independent-validation call is the real gate.

The Reading "Apply a Diagram" task generates actual visual material — a schedule, notice,
advertisement, map, or event listing rendered as an asset — not prose pretending to be a
diagram. A prose stand-in trains the wrong skill.

---

## 6. Practice modes

Every task type supports all three:

| | Learn | Timed | Simulation |
|---|---|---|---|
| Instructions/strategy first | yes | no | section instructions only |
| Timing | untimed by default | official limits | strict, enforced |
| Hints | yes | no | no |
| Feedback | immediate, per task | after the set | withheld until submission |
| Sample answer | yes | after the set | in results |
| Answer changes | free | free | blocked where the official flow blocks them |
| Audio replay | free | limited | controlled, single play |
| Retry recording | yes | yes | no |

---

## 7. Full test simulation

Pre-test microphone and audio check; section instructions; **server-side autosave on every
response**; inter-section transitions; timer recovery after refresh (the clock is the
server's `started_at` + limit, never the browser's); flag-for-review where the official flow
allows it; submit confirmation; automatic completion when time expires; resume permitted
only after a genuine technical interruption; results withheld until the whole simulation
completes. A full mock occasionally includes unscored Listening or Reading content, not
identified during the attempt — matching the real test's behaviour.

---

## 8. Scoring

### Listening and Reading — deterministic
Keyed answers, plus per-question: correct/incorrect, the passage or transcript span that
proves the key, an explanation of the correct answer, and an explanation of why each
distractor fails. Aggregated into accuracy by task type, timing by question, and an
approximate level range.

### Writing and Speaking — two evaluators plus reconciliation
Each response is scored independently twice against the official dimensions — Content and
Coherence, Vocabulary, Readability (writing) or Listenability (speaking), and Task
Fulfilment. Where the two materially disagree, a third reconciliation call adjudicates with
both rationales in front of it. All three outputs persist. Consistency is the whole point of
a score estimate, and it costs more; that is an accepted trade.

Writing feedback returns: estimated level with a confidence range, evidence quoted from the
submitted response, missing requirements, organisation and paragraph analysis, grammar and
vocabulary patterns, corrections grouped by importance, a better outline, an improved sample
response, and one focused retry exercise.

Speaking adds deterministic delivery signals computed from the audio and transcript — pace,
pause distribution, filler words, repetition, response completeness, and approximate
intelligibility — because a clean transcript hides hesitation entirely. **Pronunciation
feedback addresses comprehensibility only and never penalises a legitimate accent.**

The improved sample response is generated on a **separate call from scoring**, so the model
is not scoring an answer against one it just wrote.

### Weakness taxonomy
Every evaluation must tag weaknesses from a fixed enum, not free text. Fixed tags aggregate
into trends across attempts and drive the study planner; free-text feedback cannot.

---

## 9. Feedback loop

Every result ends in actions, not a dead end: retry the same prompt, try a parallel prompt,
review the relevant lesson, add the weakness to the study plan, compare against an improved
response, or drill only the weakest scoring dimension. A retry renders original and revision
side by side and states what changed.

---

## 10. Study planner

Generated from the diagnostic, rebalanced after every scored attempt. Missed sessions move
forward selectively rather than doubling tomorrow's load — a plan that punishes a missed day
gets abandoned.

| Week | Focus |
|---|---|
| 1 | Diagnostic, format, foundational strategy, every task type once |
| 2 | Weak-skill drills, response structures, vocabulary, timing |
| 3 | Timed components, targeted correction, two full simulations |
| 4 | Full mocks, consistency, pacing, final error correction, lighter review before the test |

Onboarding collects test type, date, target level, available hours, and self-identified
weaknesses, then administers a shortened diagnostic across the selected components. It is
skippable and every setting is editable later.

---

## 11. Model policy roles

Added to `MODEL_ROLES` and `DEFAULT_MODEL_POLICY`:

| Role | Default tier | Why |
|---|---|---|
| `celpip_item_writer` | sonnet | Volume generation of stimuli and questions. |
| `celpip_item_validator` | sonnet | Independent answer check; must not be the writer's own model reasoning. |
| `celpip_writing_scorer` | opus | Score credibility is the product. |
| `celpip_speaking_scorer` | opus | Same. |
| `celpip_score_reconciler` | opus | Adjudicates disagreement between two evaluator passes. |
| `celpip_feedback_writer` | sonnet | Exemplars, outlines, retry exercises — generation, not judgement. |
| `celpip_planner` | sonnet | Study-plan construction and rebalancing. |

---

## 12. Delivery order

Dependency-driven, one build. The three named risks — audio persistence, full-test timer
recovery, and trustworthy Writing/Speaking estimates — are pulled forward.

1. Data model, migration, service layer, API skeleton, `/celpip` shell
2. Question Bank and generation pipeline
3. Reading and Writing runners
4. Listening audio generation and runner
5. Speaking recording, transcription, runner
6. Scoring and feedback
7. Full-test orchestration
8. Results analytics and study planner
9. Complete Learn content
10. End-to-end testing and scoring calibration

**Out of scope on purpose:** social features, payments, public access, multi-instructor
workflows. None of them help clear a test next month.

---

## Verification after every phase

```
cd apps/api && python3 -m py_compile $(git diff --name-only -- '*.py')
cd apps/api && uv run alembic upgrade head
cd apps/web && npx tsc --noEmit -p tsconfig.json
```

---

## Built — and where it diverged from the plan above

Seven decisions changed during implementation. Each is a place where the design
above was wrong or underspecified, so they are recorded rather than quietly
absorbed.

**1. The study planner is deterministic, not a model call.** Everything a plan
needs — days remaining, hours available, which task types have been attempted,
which weakness tags were measured — is already in the database, so scheduling is
arithmetic over a template. That makes it reproducible, instant, free to
rebalance after every attempt, and explainable line by line. The
`celpip_planner` model role was therefore **removed**: a policy entry that
controls nothing is worse than no entry, and this codebase already documents
that principle for its rule-based judges.

**2. The second evaluator got its own model-policy role.** `..._scorer_b`
defaults to a different provider from `..._scorer`. Running both passes through
one role would have meant one model asked twice, which makes the agreement — and
therefore the reported confidence — meaningless.

**3. Listening audio is synthesised per speaker turn, not per segment.** There is
no ffmpeg in this stack and concatenating MP3 frames server-side is the kind of
thing that works until it doesn't. Per-turn files keep each speaker's voice
distinct, let the player insert a natural beat between turns, and let one failed
turn be retried without re-synthesising a six-minute conversation. The player
preloads the whole sequence before playback so it is gapless.

**4. Reading Part 2's diagram is rendered client-side from structured rows**
rather than generated as an image: sharper, screen-readable, and it cannot drift
from the data the questions were keyed against. Speaking tasks 3/4/5/8 *do* get a
generated image, because describing a text description is a different skill from
describing a picture.

**5. Duplicate detection is length-aware.** Shingle overlap is only meaningful on
a substantial stimulus. Writing and speaking prompts run 40–60 words and are
heavily formulaic, so two genuinely different scenarios sharing a sentence frame
overlapped enough to look like duplicates — caught by a test, not in production.
Below 80 words, only an exact fingerprint or a repeated topic counts.

**6. Items have an `awaiting_assets` state.** A listening item is validated but
not servable until its audio exists; serving one whose synthesis failed would
burn a timed section on a silent question.

**7. The admin nav entry is a link, not a tab.** It sits in the existing admin
tab strip and opens the full-screen `/celpip` workspace, satisfying both the
"one entry in the existing navigation" requirement and the exam simulation's need
for a distraction-free shell.

### What is verified

- 88 CELPIP tests across four files, plus the full existing suite (1063 passing;
  one pre-existing flake in `tests/test_agent_runtime.py` that also fails on a
  clean tree).
- Deterministic item validation, including the evidence-containment check that
  catches a keyed answer with no support in its source.
- The exam clock: idempotent section start, server-derived time remaining across
  a reload, late answers recorded but flagged, simulation answer-locking.
- What the client may see: no listening script, no answer key mid-attempt, no
  unscored-item labelling.
- Two-evaluator scoring: agreement yields one level, disagreement widens the
  range and lowers confidence, both passes stored, empty responses never sent to
  a model.
- The generation pipeline: schema gate before the model gate, duplicate
  rejection, listening items held back until audio exists.
- `npm run build` and `tsc --noEmit` clean; `alembic upgrade head` applied.

### Not yet exercised against live providers

Every model and speech call is covered by stubs, not by real API traffic. The
first real generation run is where prompt quality, TTS voice quality, and
transcription accuracy get their actual test — budget time for tuning the
generator prompts against what the independent validator rejects.

---

## Post-review fixes

Four defects found in review, all reachable during a scored mock. Each has a
regression test that was confirmed to fail against the code before the fix.

**1. Answer state leaked between questions (P0).** `QuestionCard` seeded its
answer state in `useState` initialisers, which run only on first mount. React
reused one instance across questions, so the next question rendered with the
previous one's selections, its simulation answer-lock, and — worst — the
previous writing task's text still in the editor. The autosave effect then
compared that stale text against the new question's empty response and posted
task 1's essay against task 2's id. Fixed by keying the component on
`question_id` at the call site *and* re-seeding on question change inside it,
with an ownership guard that refuses to autosave while the editor still holds
another question's text. None of the three guards is solely load-bearing.

**2. A finished section could be restarted with a fresh full timer.**
`start_section` only short-circuited for a section that was started *and not
completed*. The browser's "already started" guard does not survive a reload, so
refreshing after a section expired re-POSTed start and handed back a brand-new
deadline. The server now refuses to reopen a closed section.

**3. Sections were only closed on expiry, never on advance.** Moving from the
last Listening item into Reading left the Listening clock running. The runner
now closes a section when the learner leaves it, which — with fix 2 — is also
what stops a re-entered section reopening.

**4. `late` was recorded but never enforced.** The schema comment claimed late
responses were excluded from scoring; nothing checked the flag, so an answer
saved after the deadline still earned a mark. Receptive answers flagged late are
now scored as unanswered, and the count surfaces in the results
("2 answers arrived after the section deadline and did not count") rather than
appearing as unexplained wrong answers. Written and spoken responses are
deliberately *not* dropped on this flag: their text accumulates through
autosave, so one late save would discard the whole essay.

Also fixed alongside these: `handleExpiry` had a dead condition (its `remaining`
list always contained the current skill) and compared `item.position` against an
array index — correct only while the two happened to coincide.

Verification after the fixes: 93 CELPIP backend tests, 40 web tests, full API
suite at 1068 passing, `npm run build` and `tsc --noEmit` clean. The one
remaining failure, `tests/test_agent_runtime.py`, is a pre-existing
order-dependent flake that also fails on a clean tree with this work stashed.

### Second review round

**[P1] Section enforcement was incomplete.** The earlier fix stopped a closed
section reopening, but four holes remained, all of which let a learner sidestep
the exam clock:

- The API accepted any skill, whether or not the test contained it. Now
  validated against the test's `components_json`.
- Multiple sections could be open at once. A start is now refused while another
  section is active.
- Sections could be started out of order — leaving a hard section unopened, sitting
  the rest, and returning to it later with its clock untouched. Earlier sections
  must now be completed first.
- **Responses were accepted for a section that had never started.** An unstarted
  section has no deadline, and `_section_expired` reads a missing deadline as
  "not expired", so the timer check waved the answer through. That is the entire
  exam clock bypassed by never pressing start. `save_response` and
  `save_audio_response` now require the item's section to be open.

All four apply to timed and simulation runs only. Learn mode is explicitly
untimed with free navigation and changeable answers, and a drill you cannot move
around in is a worse drill — covered by its own test.

The runner had to change with it: cross-section navigation now 409s, so moving
between sections is an explicit "Finish <section>" step rather than something
that happens by drifting into another section's item, the strip and arrows stay
inside the open section in enforced modes, and a refused section start surfaces
instead of being swallowed.

**[P2] A transient scoring failure stopped the results screen polling.** Every
exception marked the attempt `failed`, but the maintenance worker requeues up to
`max_attempts`. The results view only polled `submitted`/`evaluating`, so it
stopped checking while a retry was pending and a later successful run never
appeared without a manual reload. `evaluate_attempt` now consults the job's retry
state and stays in `evaluating` while a try remains, marking `failed` only when
the job is genuinely out of attempts. Belt and braces, the results endpoint also
returns `evaluation_pending` and the job's attempt counts, so the client keeps
polling — and says "attempt 2 of 3" — even when a crashed worker leaves the
attempt in an odd state.

Verification: 104 CELPIP backend tests, 40 web tests, full API suite 1080 passing
with no failures, `npm run build` and `tsc --noEmit` clean, ruff clean. The P2
fix and the P0 runner fix were both confirmed to fail their new tests when
reverted.
