# New feature: explicit comparison matrix mode — Implementation Guide

**Correction to how this was originally pitched:** the earlier conversation framed this as "structured multi-topic research mode," implying the fan-out/comparison machinery needed to be built. Reading the actual pipeline changed that. It's already built, and it's more sophisticated than expected:

- `subject_derivation` (nodes.py) + `_extract_named_comparison_subjects` (research_contracts.py:313-403) already detect named subjects from free text — "compare AWS Bedrock, Snowflake Cortex, and Azure OpenAI" correctly yields 3 subjects today, no changes needed.
- `contract` (nodes.py:164-208) already builds a `CoverageContract` with a real subjects × dimensions matrix (`CoverageCell` per pair, research_models.py:694-728) and tracks per-cell fill status and evidence linkage.
- `plan` already derives one search worker per open cell's subject (research_planner.py:151-172).
- `judge_research` (research_synthesis.py:220-232) already penalizes an answer that omits a named subject, and `judge_research_final`'s `_framework_comparison_completion_issues` (research_planner.py:878-904) already checks for a closing recommendation section on multi-subject answers.

**So what's actually missing is narrower than "comparison mode doesn't exist":** two specific things. First, none of this can be *deliberately* invoked — it's purely automatic, triggered by whether the message text happens to match detection heuristics (comparison keywords + decision-context keywords + ≥3 named options, per `_requires_decision_grade_comparison`, research_synthesis.py:986-1022). Phrase a comparison request slightly differently and you silently get the generic narrative path instead. Second, and this matters: `_synthesis_report_contract` (research_synthesis.py:802+) *used to* mandate a rigid matrix structure and was deliberately changed away from that — the code comment at line 807-811 says so explicitly: forcing identical per-option sections produced "placeholder cells for thin-evidence subjects," i.e., the model fabricating content to fill a table it didn't have real evidence for. That was the right call for exploratory chat. It's the wrong call when you specifically want a scannable matrix for a governance or architecture decision doc and are fine with a cell honestly saying "insufficient evidence" instead of prose padding.

**The feature, correctly scoped:** an explicit, opt-in "comparison matrix" toggle that (1) reliably invokes the existing subject/contract/judge machinery regardless of phrasing, and (2) requests a literal Markdown table instead of the flexible narrative structure — while explicitly permitting honest empty cells, so it doesn't reintroduce the fabrication problem the codebase already fixed once.

## 1. Backend: new request field

`apps/api/app/services/agent/models.py`, `TurnRequest` (~line 22-52), add next to `confirm_deep_research`:

```python
confirm_deep_research: bool = False
comparison_mode: bool = False
```

## 2. Backend: force the full research route, never fast-path

`apps/api/app/services/agent/fast_path.py`, `decide_fast_path` (~line 82-103), add a guardrail alongside the existing `output_format != "chat"` and `confirm_deep_research` checks:

```python
if request.comparison_mode:
    return FastPathDecision(
        path="agentic",
        confidence=1.0,
        reason="Comparison mode requires the full research runtime.",
        source="guardrail",
    )
```

Also check `apps/api/app/services/agent/orchestrator.py`'s route-classification prompt/logic — `fast_path`'s "agentic" just means "hand off to the full orchestrator," which then does its own LLM-based route classification (chat/research/research_document/document). Confirm whether `comparison_mode=True` needs a similar hard override there to guarantee `route == "research"` specifically, rather than relying on the orchestrator's classifier to infer it from message text the same way it does today. If the orchestrator already reliably picks "research" for anything with named comparison subjects, this may not need a separate change — verify with a test case before assuming either way.

## 3. Backend: make comparison_mode reach the contract/synthesis layer

`TurnRequest` already flows into `nodes.py`'s `contract`/`plan`/`synthesize` via the `request` object passed through `RunnableConfig`, so `comparison_mode` is automatically available wherever `request` is — no new plumbing needed to get the flag there, just to act on it.

**Known limitation to design around, not solve in this pass:** if `comparison_mode=True` but `_extract_named_comparison_subjects` still finds fewer than 2 subjects (the user didn't name concrete things to compare — e.g. "compare our AI governance maturity across three dimensions" with no named products), don't try to guess subjects. Have `contract` (nodes.py:164-208) pass through unchanged in this case, and let the new synthesis instruction (below) handle it by explicitly asking the user to name at least two things to compare, rather than silently producing a table with fabricated columns. This mirrors the existing `needs_clarification` pattern already used in `fast_path.py` rather than inventing a new one.

## 4. Backend: the strict synthesis instruction

`apps/api/app/services/agent/research_synthesis.py`, `_synthesis_report_contract` (~line 802). Insert as the **first** check in the function, before the `output_format == "chat"` branches (~line 807) — comparison_mode should override profile/output_format-based branching entirely, not compete with it:

```python
def _synthesis_report_contract(profile: ResearchProfile, request: TurnRequest) -> str:
    S = SYNTHESIS_SUBSTANCE_REQUIREMENTS

    if request.comparison_mode:
        return (
            "Produce a strict comparison matrix, not a narrative report. "
            "Open with 1-2 sentences framing what's being compared and why it matters. "
            "Then produce exactly one Markdown table: one row per evaluation dimension, one column per named "
            "subject (plus a leading 'Dimension' column). "
            "Every cell must be filled. If the evidence gathered doesn't support a real answer for a specific "
            "cell, write 'Insufficient evidence' in that cell — do not fabricate, pad with generic claims, or "
            "silently drop a subject or dimension because evidence is thin for it. An honest gap is a correct "
            "answer; a plausible-sounding guess is not. "
            "If fewer than two distinct subjects can be identified from the request and evidence, do not "
            "produce a table — instead ask the user to name at least two specific things to compare. "
            "After the table, close with a short ranked recommendation: the top choice, the decision rule used, "
            "and the main condition under which a different choice would win."
        ) + S
    # ... existing branches unchanged below
```

This is a genuinely different instruction from the reverted rigid-matrix approach it superficially resembles — the earlier version's flaw wasn't the table structure, it was requiring every cell to contain substantive content regardless of evidence. This version explicitly makes "insufficient evidence" a valid, expected cell value, so it can't reintroduce the fabrication problem the Phase 7 change (referenced in the code comment) was fixing.

## 5. Backend: judge hardening for comparison_mode specifically

`apps/api/app/services/agent/research_synthesis.py`, `judge_research` (~line 202-237) or `research_planner.py`'s `judge_research_final` (~line 772-876, wherever `_framework_comparison_completion_issues` is called) — add a comparison_mode-specific structural check, applied *only* when the flag is set, so the existing flexible-mode judging is untouched:

```python
def _comparison_table_issues(request: TurnRequest, contract: CoverageContract, answer: str) -> list[str]:
    if not request.comparison_mode:
        return []
    issues: list[str] = []
    has_table = bool(re.search(r"^\s*\|.+\|\s*$\n\s*\|[-:\s|]+\|\s*$", answer or "", re.MULTILINE))
    if not has_table:
        issues.append("Comparison mode requires a Markdown table; the answer does not contain one.")
        return issues
    subject_count = len(contract.subjects)
    if subject_count >= 2:
        header_line = next((line for line in (answer or "").splitlines() if line.strip().startswith("|")), "")
        column_count = header_line.count("|") - 1
        if column_count < subject_count:
            issues.append(
                f"The table has fewer columns ({column_count}) than named subjects ({subject_count}) — "
                "every subject must have its own column."
            )
    return issues
```

Wire this into whichever judge function is the actual gate before repair triggers (confirm exact call site — `judge_research` is the one already read in full this session; `judge_research_final` in `research_planner.py` was referenced by the earlier research pass but not read line-by-line here, so verify which one is actually on the live path before wiring this in). A non-empty result should push `ResearchJudgeResult.status` to `"repair"` with these issues appended, using the same mechanism `_missing_named_subjects` already uses (research_synthesis.py:220-232) as the template.

## 6. Frontend: the toggle

`apps/web/app/components/Composer.tsx` already has `SelectField` dropdowns for `qualityMode` and `researchLevel` (~line 258-260) using a shared `SelectField label=... value=... onChange=... options=...` pattern with option arrays (`QUALITY_OPTIONS`, `RESEARCH_OPTIONS`) defined nearby. Add a third one:

```tsx
<SelectField
  label="Format"
  value={comparisonMode ? 'matrix' : 'normal'}
  onChange={value => setComparisonMode(value === 'matrix')}
  options={[
    { value: 'normal', label: 'Normal' },
    { value: 'matrix', label: 'Comparison matrix' },
  ]}
/>
```

Thread `comparisonMode`/`setComparisonMode` down from wherever `qualityMode`/`researchLevel` state already lives (likely `useAgent.ts`, mirroring how those two are currently managed) into `Composer`'s props, and include `comparison_mode: comparisonMode` in the `TurnRequest` payload built at submit time. The existing indicator line at Composer.tsx:196-197 (`{(outputFormat !== 'chat' || researchLevel !== 'auto') && ...}`) should get a third condition so the composer visibly shows when comparison mode is active, same as it does for non-default output format or research level today.

## Testing plan

- `_synthesis_report_contract`: assert `comparison_mode=True` produces the strict-table instruction regardless of `profile`/`output_format` — this should be a simple, fast unit test since the function is pure.
- `decide_fast_path`: assert `comparison_mode=True` always returns `path="agentic"` regardless of message content, mirroring the existing `output_format != "chat"` guardrail test if one exists.
- End-to-end: a message like "compare AWS Bedrock, Snowflake Cortex, and Azure OpenAI Service on data governance, cost model, and model catalog breadth" with `comparison_mode=True` — assert the final answer contains a Markdown table with a header row and at least 3 data columns, and that none of the cells are empty strings (either real content or the literal "Insufficient evidence").
- Edge case: `comparison_mode=True` with a message naming zero or one subject — assert the answer asks a clarifying question rather than producing a malformed one-column table.
- Regression: run existing tests that exercise `_synthesis_report_contract`'s other branches (if any) to confirm the new first-check doesn't shadow them when `comparison_mode` is unset (the default) — should be a non-issue since the new branch is gated on a flag that's `False` everywhere else, but worth a quick confirmation pass rather than assuming.
- Manual: the exact "AWS Bedrock vs Snowflake Cortex vs Azure OpenAI" scenario from your own use case — confirm the output is something you'd actually paste into a governance deck versus what the current default narrative produces for the same query.
