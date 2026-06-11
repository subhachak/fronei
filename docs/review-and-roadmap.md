# Fronei Current Review & Roadmap

This document reflects the current codebase state. For system structure, see `docs/architecture.md`. For model policy details, see `docs/routing-policy.md`.

## Current Product Shape

Fronei is a personal AI workbench with:

- Authenticated multi-turn chat through Clerk.
- Streaming responses over Server-Sent Events.
- Planner-driven prompt enrichment, task classification, web-search detection, and sub-query decomposition.
- YAML policy routing across OpenAI, Anthropic, Gemini, OpenRouter, DeepSeek, Qwen, and Perplexity model strings.
- Deep research runs with persisted questions, sources, claims, findings, gaps, contradictions, confidence, and verifier notes.
- Document and image attachment extraction.
- Conversation-local memory through rolling summary and active task state.
- Persistent user memories extracted in the background.
- Twin profile voice adaptation from writing samples.
- Usage analytics for cost, requests, tokens, latency, model usage, and task distribution.
- Workbench/persona UI, artifact formatting, developer execution logs, and dashboard views.

## Recently Addressed Areas

The codebase already includes fixes or implementations that older review notes called out:

- Provider keys are configured during FastAPI lifespan startup.
- Routing policy is cached with `lru_cache`.
- Forced models still receive safety-net fallbacks.
- The conversation pipeline has been extracted into `app/services/chat_pipeline.py`.
- Analytics combines conversation messages and stateless request logs.
- Daily budget checks run before model dispatch.
- Alembic migrations exist for schema evolution.
- Conversation memory state is persisted and passed to the planner.
- The frontend hides model internals behind developer mode by default.

## Remaining Engineering Priorities

### P0/P1

- Add rate limiting and abuse controls around chat, document extraction, and research endpoints.
- Move background LLM work to a durable queue for production. Current memory, summary, and fingerprint jobs run in local thread pools.
- Add provider health checks or circuit breakers so repeated provider outages do not burn latency across every fallback chain.
- Strengthen production Clerk configuration by setting and enforcing `CLERK_AUDIENCE`.
- Add request-level cancellation/cleanup for long research runs when clients disconnect.

### P2

- Add more regression coverage for streaming SSE event sequences and frontend state transitions.
- Add evals/golden prompts for planner JSON quality, routing decisions, research citation quality, and voice refinement.
- Improve model cost estimation before dispatch, especially for document-heavy and research-heavy requests.
- Add pagination/search to memory and research-run endpoints if those surfaces become large.
- Consider normalizing execution logs into first-class tables if analytics needs deeper slicing.

### P3

- Add OpenTelemetry, Langfuse, or equivalent tracing.
- Expand deployment docs for Fly.io/Railway in addition to Render/Vercel.
- Build an admin/provider status view for configured model/search providers.
- Add explicit retention controls for uploaded document text, memories, writing samples, and research evidence.

## Product Roadmap

### Near Term

- Polish deep research progress states and failure recovery.
- Make citation chips and source evidence easier to scan on mobile.
- Add "continue from evidence" controls for existing research runs.
- Add clearer settings copy for memory, voice profile, and developer mode.
- Improve first-run onboarding so persona, domain, and artifact defaults are set intentionally.

### Medium Term

- Add workspace/project organization around conversations and research.
- Add share/export flows for research briefs and architecture artifacts.
- Support reusable prompt/artifact templates.
- Add feedback capture on answers, routes, research sources, and refinements.
- Add per-user/provider budgets and usage limits.

### Longer Term

- Durable job orchestration for deep research and long document workflows.
- Multi-agent research/evaluation loops with explicit quality thresholds.
- Team/shared workspace mode with permissions.
- Evaluation-driven routing policy tuning.
- First-class observability and cost governance.
