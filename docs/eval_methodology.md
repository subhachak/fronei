# Eval Methodology and MAST Scorecard

Status as of 2026-07-09. This document maps Fronei's eval harnesses to the MAST
multi-agent-system failure taxonomy and reports current pass rates. Numbers
below were pulled directly from running the harnesses and inspecting the repo's
own eval database/CI history on this date — none are estimated or invented.
Where real data doesn't exist yet, that is stated explicitly rather than filled
in with a plausible-looking number.

## What MAST is

MAST (Multi-Agent System Failure Taxonomy) is an empirically-derived taxonomy
of why multi-agent LLM systems fail, published in ["Why Do Multi-Agent LLM
Systems Fail?"](https://arxiv.org/html/2503.13657v2) (Cemri et al., 2025). It
was built from ~1,600 annotated execution traces across 7 popular multi-agent
frameworks, with inter-annotator agreement (Cohen's Kappa 0.88).

MAST groups 14 specific failure modes into 3 top-level categories:

| Category | Share of observed failures | What it covers |
|---|---|---|
| **FC1 — Specification Issues** | 41.77% | System design decisions and poor/ambiguous prompt specifications |
| **FC2 — Inter-Agent Misalignment** | 36.94% | Breakdowns in inter-agent interaction and coordination during execution |
| **FC3 — Task Verification** | 21.30% | Inadequate verification processes that fail to detect or correct errors |

The 14 failure modes (with the paper's reported frequency across its dataset):

| Mode | Category | Description | Freq. in MAST dataset |
|---|---|---|---|
| FM-1.1 | FC1 | Disobey task specification | 10.98% |
| FM-1.2 | FC1 | Disobey role specification | 0.5% |
| FM-1.3 | FC1 | Step repetition | 17.14% |
| FM-1.4 | FC1 | Loss of conversation history | 3.33% |
| FM-1.5 | FC1 | Unaware of stopping conditions | 9.82% |
| FM-2.1 | FC2 | Conversation reset | 2.33% |
| FM-2.2 | FC2 | Fail to ask for clarification | 11.65% |
| FM-2.3 | FC2 | Task derailment | 7.15% |
| FM-2.4 | FC2 | Information withholding | 1.66% |
| FM-2.5 | FC2 | Ignored other agent's input | 0.17% |
| FM-2.6 | FC2 | Reasoning-action mismatch | 13.98% |
| FM-3.1 | FC3 | Premature termination | 7.82% |
| FM-3.2 | FC3 | No or incomplete verification | 6.82% |
| FM-3.3 | FC3 | Incorrect verification | 6.66% |

**These percentages describe MAST's own published dataset (other frameworks'
traces), not Fronei.** They're included here purely as the taxonomy's
reference frame for how failures are typically distributed in multi-agent
systems generally — see below for what's actually measured in Fronei.

## Fronei's eval harnesses today

Fronei has three distinct eval mechanisms, at three very different levels of
maturity. This is the honest current state, not an aspirational one.

### 1. Deterministic routing/policy harness (`app.evals.runner`) — real, currently passing

Runs `apps/api/evals/golden_turns.json` (fixed candidate decisions, no live
model calls) against the production fast-router and orchestrator normalization
code. Covers routing correctness, freshness escalation, high-stakes
escalation, artifact routing, research-level selection, and deep-research
confirmation gating.

**Current result, run 2026-07-09: 146/146 scenarios passing.**

```
category                          total   pass   prec recall
------------------------------------------------------------
  artifacts                          4      4   100%  ok
  attachment_context                15     15   100%  ok
  clarification                      2      2   100%  ok
  context                            1      1   100%  ok
  controls                           2      2   100%  ok
  explicit_cross_workspace_recall    15     15   100%  ok
  freshness                          3      3   100%  ok
  grounding_canary                   3      3   100%  ok
  live_current_lookup               17     17   100%  ok
  research                           3      3   100%  ok
  routing                            2      2   100%  ok
  safety                             1      1   100%  ok
  same_conversation_followup        17     17   100%  ok
  same_workspace_recall             23     23   100%  ok
  standalone                        22     22   100%  ok
  vague_unresolved_followup         16     16   100%  ok
```

This harness does **not** grade model prose, citations, or answer quality —
it only asserts routing/policy decisions (see `docs/agent-evals.md`).

### 2. DB-backed v2 scoring-axis system — framework exists, zero recorded runs

`app/routers/evals.py`, `EvalCase`/`EvalRun` tables, and 37 seeded admin-managed
eval cases (`eval_cases` table, confirmed populated in the local dev DB on
2026-07-09) implement a much richer per-axis scoring model:

- **gate_correct** — did an expected routing/tool gate fire (or correctly stay silent)?
- **retrieval_completeness** — coverage of required evidence subjects/dimensions
- **retrieval_independence** — minimum independent-source-domain diversity
- **synthesis_grounding** — citation validity against the evidence pack
- **latency_pass** — response time against tier ceilings
- **format_correct** / **must_not_recommend** — output-shape and safety-boundary checks

These axes are unit-tested in isolation (`tests/test_eval_v2_scoring_axes.py`,
`tests/test_eval_harness_integrity_gate.py`, `tests/test_eval_narrow_judge_rubrics.py`)
and the scoring logic itself is verified correct. **However, the `eval_runs`
table in the local dev database has zero rows** — no admin-triggered run of
this system has ever executed and produced results here. There is currently
no real pass-rate data to report for this system, by category or otherwise.

### 3. Live agentic quality evals (`.github/workflows/live-agent-evals.yml`) — infrastructure broken, not producing data

A weekly scheduled workflow that runs 4 end-to-end scenarios (direct answer,
sourced research, DOCX generation, PPTX generation) through the real runtime
with live model calls, judged for citation validity, route correctness, and a
compact quality score.

**Both of its last two scheduled runs failed outright:**

| Run date | Result | Cause |
|---|---|---|
| 2026-06-29 | 0/4 passed | Provider circuit breakers open (OpenAI, Gemini) after repeated failures |
| 2026-07-06 | 0/4 passed | `Missing Gemini API key` — `GEMINI_API_KEY`/`GOOGLE_API_KEY` not set in CI secrets, then provider circuits opened for the remaining scenarios |

(Pulled directly from the actual `live-eval-report.json` artifacts of both
runs via `gh run download`, not summarized from workflow logs.) This means
the live quality harness has never yet produced a usable pass/fail signal —
it needs its CI secrets fixed (at minimum `GEMINI_API_KEY`/`GOOGLE_API_KEY`
alongside the already-required `OPENAI_API_KEY`/`TAVILY_API_KEY`) before it
can be treated as a working quality gate, let alone a MAST-mapped one.

## Mapping Fronei's axes to MAST categories

Where Fronei's v2 scoring axes conceptually correspond to MAST failure modes
(this is a taxonomy mapping for future use once axis #2 above has real run
data — it does not imply current measured coverage):

| Fronei scoring axis | Nearest MAST failure mode(s) | Category |
|---|---|---|
| `gate_correct` (expected route fires/stays silent) | FM-1.1 Disobey task specification, FM-1.5 Unaware of stopping conditions | FC1 |
| `format_correct` | FM-1.1 Disobey task specification | FC1 |
| `must_not_recommend` (safety boundary) | FM-1.2 Disobey role specification | FC1 |
| `retrieval_completeness` | FM-3.2 No or incomplete verification | FC3 |
| `retrieval_independence` | FM-3.3 Incorrect verification (single-source bias) | FC3 |
| `synthesis_grounding` (citation validity) | FM-3.2 / FM-3.3 verification failures | FC3 |
| `latency_pass` | FM-3.1 Premature termination (proxy: budget/time exhaustion) | FC3 |
| *(no current axis)* | FM-2.1 through FM-2.6 — inter-agent misalignment | FC2 |

**Gap called out explicitly:** Fronei's current v2 axes have no coverage of
FC2 (Inter-Agent Misalignment) at all — reasonable given Fronei's LangGraph
research pipeline is closer to a fixed-topology pipeline with well-defined
node handoffs than a free-form multi-agent negotiation system, but worth
flagging as MAST's second-largest failure category (36.94% of failures in its
source dataset) with zero dedicated scoring today.

## What would need to happen to report real MAST-mapped pass rates

1. Fix the live-agent-evals.yml CI secrets (add `GEMINI_API_KEY`/`GOOGLE_API_KEY`)
   so the live harness produces real signal instead of 0/4 credential failures.
2. Trigger at least one real run of the v2 scoring-axis system against the 37
   seeded eval cases via the admin evals dashboard, so `eval_runs` has data to
   report per-axis pass rates from.
3. Once both produce real numbers, aggregate per-axis results using the
   mapping table above to get genuine FC1/FC2/FC3 pass rates.

None of this was in scope to execute as part of this task (it requires live
provider credentials and cost, and (1)/(2) are operational/CI actions rather
than code changes) — flagging it here as the concrete next step rather than
fabricating the numbers it would produce.
