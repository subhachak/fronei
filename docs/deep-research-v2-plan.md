# Deep Research v2 — Adaptive Evidence Engine

Status: design accepted, ready for implementation (Phase 0–2 first PR scope below).
Context: triggered by a staleness/quality review of the deep-research pipeline
(`research_orchestrator.py`, `web_context.py`), using the question *"Currently
how long is it taking for H4 and H4 EAD to be approved when filed together with
H1B renewal on premium processing"* as the worked example throughout.

## 1. North Star

> An adaptive, evidence-aware research system that spends only when the next
> step is likely to reduce answer-changing uncertainty, and that judges
> evidence quality by claim type, not by source count alone.

Explicitly **not**:
- "Always run deep research."
- "Ask the user about every step."
- "Official sources always win."
- "LLM judge every step."
- "Deep equals more."

A quiet, Apple-like workflow with evidence rigor under the hood: Fronei does
the minimum sufficient research, escalates only when evidence justifies it,
and exposes deeper options only when cost/time/uncertainty makes user choice
meaningful.

## 2. Core principle: evidence quality is claim-type dependent

This is the central correction from the original design and must be baked in
from day one, not retrofitted:

- **Official sources** are strongest for policy, legal eligibility, pricing,
  product capabilities — i.e. claims about *what the rule is*.
- **Recent anecdotal / practitioner / case-tracker evidence** can be the
  *primary* signal for operational reality — delays, backlogs, real-world
  processing times, support response times — i.e. claims about *what actually
  happens*. Official SLAs describe targets, not outcomes.
- **Expert commentary** (law firm blogs, analyst notes) helps interpretation
  but should not override primary evidence without explanation.
- **Anecdotes are useful but noisy.** They need independence, recency, and
  calibration before being trusted — a single fresh anecdote should not beat a
  broad, consistent, slightly-older consensus.

For the worked example: "H-1B premium SLA is 15 business days" is a policy
claim (official source wins). "H-4 EAD currently takes 4-6 months" is an
operational-reality claim (recent independent practitioner/case-tracker
evidence is the right primary signal, with the official SLA as context, not
as the answer).

## 3. Diagnosed root causes (already fixed, informs the design)

Three issues were already identified and patched in the existing pipeline,
and motivate parts of this plan:

1. **No current-date grounding** in LLM prompts → titles/synthesis defaulted
   to training-era years (2023-2024) instead of mid-2026. Fixed via
   `_today_str()` injected into synthesis/follow-up/verifier prompts.
2. **No recency filters** on search providers → stale sources ranked equally
   with current ones. Fixed via a `recency` field on
   `ResearchDomainStrategy`, threaded through `_search()` to Tavily
   (`time_range`), Brave (`freshness`), and DuckDuckGo (`timelimit`).
3. **Hardcoded `"2026"`** in query-suffix strings → would go stale again in
   2027. Fixed to `str(_now().year)`.

These fixes reduce staleness but don't address *evidence quality*,
*citation traceability*, or *cost-aware depth* — which is what this plan
covers.

## 4. Design clarifications (corrections from review)

### 4.1 Source role is a prior; claim role is authoritative

Phase 1 assigns a coarse, heuristic **source role prior**:
`official_policy`, `expert_interpretation`, `operational_reality`,
`anecdotal_case`, `statistical_data`, `background_context`.

This prior can be wrong — a single law firm blog post can contain a policy
summary, expert interpretation, *and* anecdotal client timelines. The
**authoritative role lives at the claim level** (Phase 8). Phase 4 evidence
contracts are written to accept source-role priors initially, with an
explicit upgrade path to claim-role enforcement once Phase 8 lands.

### 4.2 Golden set must test both anecdote directions

The eval set (Phase 0) must include cases where anecdotal evidence is the
*best available* signal, and cases where anecdotal evidence is *noisy/outlier*
and should not dominate. This prevents simply shifting the bias from "official
always wins" to "recent anecdote always wins." Full list in §6, Phase 0.

### 4.3 Independence proxy starts deterministic

"Source count" without independence is misleading (5 results can be 5 reposts
of 1 original article). V1 independence uses cheap, deterministic signals:
unique domains, source family, canonical URL, title similarity — with
quoted/reposted-content detection added later only if needed. Domain/source-
family diversity is captured as Phase 1 metadata because Phase 9 conflict
resolution depends on it.

### 4.4 Caching depends on thread/claim taxonomy

Caching (Phase 5) separates **stable policy/framework research** (long TTL)
from **current operational-reality research** (short TTL). This separation is
only meaningful once the system knows whether a thread is policy, timeline,
operational-reality, pricing, etc. — so Phase 5 is implemented after Phase
3/4 land, even though it runs before the adaptive loop (Phase 6) in the
execution order.

### 4.5 Anecdote-as-primary triggers verification

When a load-bearing answer depends primarily on anecdotal/operational
evidence — even if confidence looks medium — the verifier (Phase 10) should
run. This does **not** mean rejecting anecdotal evidence; it means checking
recency, independence, and source count of the anecdotes, and ensuring the
answer labels them appropriately (e.g., "recent reports suggest..." vs. "USCIS
states...").

### 4.6 Meta-reasoning cost is budgeted

Sufficiency checks, marginal-gain tracking, and conflict detection are
themselves computation (often LLM calls) and must not exceed the savings they
create:

- **Quick/Focused tiers**: deterministic heuristics only (new-domain count,
  required tier/role found, basic date check, simple conflict signals).
- **Deep/Expert tiers**: LLM-assisted sufficiency/conflict reasoning, used
  when heuristics can't decide or risk justifies it.

### 4.7 Unknown date is not stale

- `unknown date + tier_1_official domain` → usually remains admissible
  (most official pages lack clean "last updated" metadata but are current).
- `known old date + current operational claim` → penalized.
- Date confidence (`known` / `unknown` / `inferred`) is tracked explicitly so
  these two cases aren't conflated.

### 4.8 Conflict resolution is weighted, not simplistic

No "newer source always wins." Resolution weighs, jointly:
source tier, source role, source count, source independence, recency, and
claim type. A single fresh anecdote should not beat a broader, slightly-older
consensus; but a single fresh *official* update should beat an older anecdotal
consensus on a policy claim.

### 4.9 Budgets are calibrated from data, not guessed

Initial tier presets (Phase 11) are defaults only. Before locking ceilings,
instrument current runs (Phase 0) and compare behavior across the golden set.
No hardcoded numbers ship as final without log-backed calibration.

## 5. Architecture overview

```
TRIAGE
  → DECOMPOSE
  → ALLOCATE THREAD BUDGETS
  → SEARCH IN LANES
  → ADMIT SOURCES
  → EXTRACT CLAIMS
  → CHECK SUFFICIENCY + MARGINAL GAIN
  → ESCALATE TARGETEDLY OR STOP
  → SYNTHESIZE
  → VERIFY / JUDGE
  → ANSWER + QUIET GO-DEEPER OPTION IF NEEDED
```

A research run contains multiple **research threads**, one per sub-question.
Each thread has its own budget, evidence contract, and exit condition
(`locked`, `searching`, `stopped_with_gap`).

Worked-example decomposition (H-4/H-4 EAD question):

| Thread | Claim type | Evidence role needed |
|---|---|---|
| H-1B premium processing SLA | policy | official_policy |
| H-4 (I-539) premium eligibility | policy | official_policy |
| H-4 EAD (I-765) premium eligibility | policy | official_policy |
| Concurrent adjudication guarantee (post-Edakunni) | legal/policy | official_policy + expert_interpretation |
| Actual current approval timing | operational reality | anecdotal_case (primary) + statistical_data |

Synthesis answers thread-by-thread, then gives an overall conclusion —
producing a comparison rather than one blended, hedged narrative.

## 6. Phased implementation plan

### Phase 0 — Instrumentation + Eval Harness
- Log current research runs: query domain, mode, sources gathered (with
  domains/types), cost, latency, confidence, verifier used/skipped.
- Build a 10-15 query golden set covering:
  1. immigration/current timeline (anecdote-primary)
  2. legal policy (official-primary)
  3. medical current guidance
  4. finance/company facts
  5. tech product capability
  6. simple current fact (quick-check)
  7. compound legal/current comparison
  8. conflicting current sources
  9. stale but high-ranking SEO source (should be downweighted)
  10. official source with unknown date that's still current (should be admissible)
  11. old-dated source for a current operational claim (should be historical-only)
  12. anecdotal evidence is the *best available* signal (should be primary)
  13. anecdotal evidence is noisy/outlier (should not dominate; system should hedge)
  14. formal/client-ready document request (verifier required)
- For each: define required source types/roles, whether currentness matters,
  whether anecdotal evidence is acceptable/primary, expected confidence
  behavior, and unacceptable failure modes.
- This golden set is the guardrail for every later phase, especially Phase 6.

### Phase 1 — Source Metadata Foundation (first code PR)
Add to source records:
- `tier`: `tier_1_official` / `tier_2_expert` / `tier_3_anecdotal` / `tier_4_low_quality`
- `family` / `domain`
- `role_prior` (heuristic, non-authoritative): `official_policy`,
  `expert_interpretation`, `operational_reality`, `anecdotal_case`,
  `statistical_data`, `background_context`
- `date`, `date_confidence` (`known` / `unknown` / `inferred`)
- `admission_status` (`admitted` / `downgraded` / `rejected`) + `admission_reason`
- independence-proxy fields: unique domain, source family, canonical URL,
  title-similarity hash

Used in: source ranking, synthesis prompt, verifier prompt, citation
manifest, admin/dev evidence view.

### Phase 2 — Structured Citation Manifest
- Backend returns structured sources: `id`, citation label, title, url,
  domain, tier, role_prior, source type, date/date_confidence, freshness
  score, relevance score.
- UI renders the source table/hover cards from stored data — **the model
  never invents a source table**.
- Final answer cites `[S1]` etc.; references are generated deterministically
  from the manifest.

### Phase 3 — Query Decomposition Into Research Threads
- Split compound queries into sub-questions (see §5 worked example).
- Each thread stores: subquestion, search query, claim type needed, evidence
  role needed, required source tiers, freshness requirement, max
  rounds/sources/cost, status, confidence, unresolved conflicts.
- Synthesis answers per-thread, then concludes.

### Phase 4 — Evidence Contracts v2
Claim-type-aware contracts, not blanket "official required":
- **Policy/legal eligibility claims**: tier-1 official required; anecdotal
  evidence only as supporting context.
- **Operational reality claims** (timing, backlogs, delays): recent,
  independent anecdotal/practitioner/case-tracker evidence may be **primary**;
  official sources provide baseline/SLA context.
- **Legal interpretation claims**: official/legal source preferred, expert
  commentary supporting.

Contract dimensions: claim type, required evidence role, acceptable source
tiers, freshness requirement, minimum independent sources, anecdote usage
(primary / supporting / disallowed), verifier requirement.

Contracts initially key off `role_prior` (Phase 1); upgrade to claim-level
roles once Phase 8 lands (§4.1).

### Phase 5 — Caching / Reuse Layer
(Implemented after Phase 3/4 taxonomy exists, before adaptive loop becomes
expensive.)
- Cache stable policy/framework research separately from current operational
  research, separately from source metadata and claim extraction results.
- TTLs: stable policy = long; current timelines/prices/status = short;
  medical/financial/current-legal = conservative.

### Phase 6 — Adaptive Sufficiency Loop
Replace fixed iteration counts with sufficiency-gated depth, **per thread**:
- **Low tiers**: deterministic heuristics only — required tier/role found,
  enough independent domains, date acceptable (or unknown-official allowed),
  basic conflict signals, new-domain/new-claim count.
- **Deep/Expert tiers**: LLM-assisted sufficiency when heuristics are
  ambiguous.

Thread actions: `lock_thread`, `primary_source_search`,
`operational_reality_search`, `tighten_recency`, `broaden_query`,
`targeted_conflict_search`, `stop_with_gap`. Depth is a **ceiling**, not a
commitment — escalation is targeted at resolving a specific gap/conflict, not
a generic "search more."

### Phase 7 — Marginal Gain / Diminishing Returns
Per round, track (deterministic first): new independent domains, new source
roles, higher-tier source found, fresher source found, new normalized claims,
conflict resolved. If a round adds little/duplicate evidence: change query
angle once, then `stop_with_gap` with an explicit caveat if still weak.
Semantic/LLM claim-diffing reserved for Deep/Expert or ambiguous cases.

### Phase 8 — Claim-Level Metadata
Each extracted claim gets: claim text, source id, quote, claim type (`policy`
/ `timeline` / `statistic` / `price` / `capability` / `anecdote` /
`interpretation`), **claim role (authoritative — supersedes source role
prior)**, source tier, source date/date_confidence, confidence, freshness
risk. Enables synthesis to say "official policy is X (high confidence);
recent reports suggest Y (medium confidence, N independent sources, last
updated <date>)."

### Phase 9 — Conflict Detection and Resolution
Detect: numeric conflicts, policy conflicts, date/version conflicts,
source-tier conflicts, anecdotal-vs-official mismatches. Resolve via weighted
combination of tier, role, source count, independence, recency, and claim
type (§4.8) — never a single-factor rule. Unresolved conflicts become visible
caveats in the output, not silently dropped.

### Phase 10 — Research Judge / Verifier
Run when **at least one** hard trigger is met (explicit, conjunctive — not
"maybe sensitive"):
- domain ∈ {legal, medical, financial, regulatory} AND confidence ≤ medium
- freshness-critical AND currentness not satisfied
- conflicts detected
- formal/client-ready document requested
- expert mode
- old-year/title framing risk detected
- citation/source-manifest mismatch risk
- **load-bearing claim relies primarily on anecdotal evidence** (§4.5)

Verifier checks: citations support nearby claims, manifest matches citations,
title/date framing is current, stale sources aren't presented as current,
unsupported claims removed, confidence calibrated, gaps stated clearly.

### Phase 11 — Tiered Research Budgets (calibrated from data)
Define presets as starting points, to be tuned against Phase 0 logs:

| Tier | Threads | Rounds/thread | Sources | Verifier |
|---|---|---|---|---|
| Quick Check | 1 | 1 | 1-3 | none |
| Focused | up to 3 | up to 2 | up to 8 | conditional |
| Deep | up to 5 | up to 3 | up to 16 | for sensitive/current topics |
| Expert | up to 6 | up to 4 | up to 28 | always, stricter contracts |

Planner chooses initial tier; orchestrator exits early via Phase 6; user is
asked only when escalation crosses cost/time/risk thresholds. Track average
sources/rounds needed by domain, marginal gain by round, cost by tier,
confidence improvement, and verifier repair rate to refine these numbers.

### Phase 12 — Quiet UX and Admin (later, gated on 1/3/5/7/8 maturity)
**User-facing** (default — no popups for normal research):
- Light status messages ("Checking current sources…", "Comparing official
  evidence…", "Verifying citations…").
- Subtle confidence/currentness indicator on the answer.
- Optional, non-blocking "go deeper" chips — only shown when they'd
  meaningfully change the answer (e.g., "Go deeper on recent timelines",
  "Check official sources only", "Compare by service center", "Expand
  evidence"). Chips ship only once thread state (gaps/confidence/conflicts)
  is trustworthy — premature chips that don't change the answer erode trust.

**Ask the user only when**: escalation costs materially more, answer remains
uncertain and deeper research may help, high-stakes topic needs expert
review, formal artifact needs missing preferences, or budget policy requires
confirmation.

**Admin/settings** (postponed until real usage data exists):
- Settings: default research style (Conservative/Balanced/Thorough), ask
  before high-cost research, cost estimates, per-run/monthly budget caps,
  auto-escalate high-stakes topics, evidence/debug panel toggle.
- Admin dashboard: cost by user, failed/low-confidence runs, common
  unresolved gaps, source provider health, average depth used, marginal gain
  stats. Keep initial observability as logs / dev evidence panel; defer heavy
  dashboarding.

## 7. First PR scope

Foundation only — no adaptive loop yet, low risk, immediate quality gains:

1. Migration for research source metadata fields (Phase 1 schema).
2. Deterministic source tier classifier (domain-pattern based).
3. Deterministic source family/domain classifier (independence proxy input).
4. Coarse source role prior classifier.
5. Source date extractor with date confidence (`known`/`unknown`/`inferred`).
6. Admission status/reason fields.
7. Structured citation manifest output (Phase 2).
8. Prompt updates to synthesis/verifier to include tier/date/role_prior.
9. Unit tests for tier/date/family/role-prior classification.
10. Initial golden eval fixture file (Phase 0), even if not fully automated.

## 8. Open dependencies / sequencing notes

- Phase 4 contracts must be written expecting Phase 8 to later override
  `role_prior` with claim-level roles (§4.1) — don't hard-code against
  source-level roles as final.
- Phase 5 (caching) implementation starts once Phase 3/4 land, despite being
  numbered earlier than Phase 6 in execution order.
- Phase 9 independence weighting depends on Phase 1's domain/family
  diversity fields — don't defer those fields to "later."
- Phase 11 numbers are placeholders until Phase 0 logs exist; do not treat
  the table in §6 as final.
- Phase 12 UX chips are gated on Phases 1, 3, 5, 7, 8 — not just "do them in
  numeric order."
