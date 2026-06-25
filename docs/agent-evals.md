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
