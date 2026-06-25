# Agent Evaluations

Fronei has a deterministic golden-scenario harness for routing and workflow
policy. It runs without provider credentials, network access, or paid model
calls.

Run it locally:

```bash
cd apps/api
uv run python -m app.evals.runner
```

The fixtures live in `apps/api/evals/golden_turns.json`. Each scenario supplies
a user request, deterministic candidate decisions, and the expected normalized
outcome. The harness executes the production fast-router and orchestrator
normalization code, including freshness signals, high-stakes escalation,
artifact routing, research-level selection, explicit route controls, and deep
research confirmation.

## Adding Scenarios

Add a fixture whenever a routing or workflow regression reaches production or
when a new policy is introduced. Prefer observable contract assertions:

- final route and fast path
- output format
- research level and confirmation
- required routing-signal groups
- required web-query terms

Do not assert model prose in this deterministic suite. Qualitative response,
citation, and presentation judging should run as a separate scheduled or
manually triggered evaluation because it requires live models and may incur
cost.

## Live Quality Evaluations

`.github/workflows/live-agent-evals.yml` runs weekly and can also be triggered
manually. It executes four end-to-end cases through the real runtime: direct
answer, sourced research, DOCX generation, and PPTX generation.

The workflow requires repository secrets `OPENAI_API_KEY` and `TAVILY_API_KEY`.
It defaults to `gpt-4.1-mini`, reserves at most `$0.25` across the fixture set,
has a 20-minute job timeout, and uploads
`artifacts/live-eval-report.json` for 30 days. A manual run can select another
LiteLLM model and a lower budget.

The report includes deterministic checks, compact model-judge scores, route,
sources, citation validity, artifacts, tools, fallback count, latency, and
LiteLLM-reported cost. DOCX/PPTX text is extracted in memory and supplied to
the judge but is not written into the report. Fixture reservation is the
preflight spending guard; reported provider cost is also checked between
scenarios.
