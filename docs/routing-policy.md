# Routing policy

Fronei uses policy-first routing with planner overrides. The routing file is:

```text
apps/api/app/policies/routing_rules.yaml
```

The route selected for a normal chat turn is based on:

1. Planner task type, or the keyword classifier fallback.
2. Planner complexity, or the classifier fallback.
3. User-selected profile.
4. Optional forced model.
5. Optional planner-preferred model hint.
6. Web-search or deep-research mode.

Profiles:

| UI label | API value | Behavior |
|----------|-----------|----------|
| Quick | `cost_saver` | Prefer cheap/fast models |
| Smart | `balanced` | Good default for daily work |
| Thorough | `best_quality` | Prefer stronger frontier models |

## Task Types

Schema task types include:

- `coding`
- `reasoning`
- `architecture`
- `writing`
- `summarization`
- `research`
- `document_qa`
- `math`
- `email`
- `planning`
- `unknown`

The YAML policy currently defines first-class route tables for `coding`, `architecture`, `summarization`, `writing`, and `research`. `router.py` aliases nearby task types when a direct route does not exist:

| Classified type | Routed like |
|-----------------|-------------|
| `math` | `architecture` |
| `reasoning` | `architecture` |
| `document_qa` | `summarization` |
| `planning` | `writing` |
| `email` | `writing` |

Example:

```yaml
routes:
  architecture:
    high:
      balanced:
        primary: openrouter/deepseek/deepseek-r1
        fallback: [claude-sonnet-4-6, gpt-4.1, gemini/gemini-2.5-flash]
```

## Selection Rules

`choose_route()` in `apps/api/app/services/router.py` applies routes in this order:

1. Start with classifier output from `classifier.py`.
2. Apply planner task and complexity overrides when present.
3. Force `research/high` when `deep_research=true`.
4. If `force_model` is set, use it as primary and append safety-net fallbacks.
5. Look up the YAML policy by task, complexity, and profile.
6. Fall back from missing complexity to `medium`, then `high`, then the policy `default`.
7. If web search is enabled, prefer a search-native primary:
   - `cost_saver`: `gemini/gemini-2.5-flash`
   - `balanced`: `openrouter/perplexity/sonar`
   - `best_quality`: `openrouter/perplexity/sonar-pro`
8. If the planner provides a valid model hint, use it as primary and push the YAML primary into fallbacks.
9. Append safety-net fallbacks that are not already in the chain.

Safety-net fallbacks currently are:

```text
claude-sonnet-4-6
gpt-4.1-mini
gemini/gemini-2.5-flash
```

## Deep Research

Deep research does not simply toggle the normal web context. When `research_mode` is `deep` or `expert`, the conversation streaming endpoint uses `research_orchestrator.py` to run an evidence-backed workflow:

- Plan subquestions.
- Search and crawl sources.
- Score sources.
- Extract claims.
- Evaluate gaps and contradictions.
- Synthesize an answer with source references.
- Persist research metadata for later inspection and follow-up.

Follow-up turns can reuse the last research run in the conversation when the planner classifies the turn as a follow-up, continuation, correction, or constraint change.

## Updating The Policy

To add or change a model:

1. Add the LiteLLM model string to `routing_rules.yaml`.
2. Ensure the corresponding provider key is configured in backend environment variables.
3. Restart the API process because `load_policy()` is cached.

Provider keys supported by the current config:

- `OPENAI_API_KEY`
- `ANTHROPIC_API_KEY`
- `GEMINI_API_KEY`
- `OPENROUTER_API_KEY`

Search providers:

- `TAVILY_API_KEY`
- `BRAVE_API_KEY`
