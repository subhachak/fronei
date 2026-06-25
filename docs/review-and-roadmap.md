# Fronei Current Review & Roadmap

This document reflects the current codebase state. For system structure, see `docs/architecture.md`. For model policy details, see `docs/routing-policy.md`.

## Current Product Shape

Fronei is a personal AI workbench with:

- Authenticated multi-turn chat through Clerk.
- Streaming responses over Server-Sent Events.
- Orchestrator-led routing with direct, web, research, document, and
  research-document workflows.
- DB-backed model policy with per-role assignment and controlled fallbacks.
- Durable deep research with persisted events, tools, sources, citations,
  budgets, verification, and repair.
- Document and image attachment extraction.
- Conversation and workspace context plus consolidated user preferences.
- Usage analytics for cost, requests, tokens, latency, model usage, and task distribution.
- Workspace-oriented UI, downloadable artifacts, structured execution events,
  admin operations, and provider/job monitoring.

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

- Strengthen production Clerk configuration by setting and enforcing
  `CLERK_AUDIENCE`.
- Extend the durable maintenance-job boundary to future scheduled tasks.
- Add edge-level abuse protection before scaling beyond one API instance.

### P2

- Expand live eval coverage for citation quality, artifact rendering, and
  personalization as production examples accumulate.
- Improve model cost estimation before dispatch, especially for document-heavy and research-heavy requests.
- Add pagination/search to admin and workspace surfaces as datasets grow.

### P3

- Add OpenTelemetry, Langfuse, or equivalent tracing.
- Expand deployment docs for Fly.io/Railway in addition to Render/Vercel.
- Add explicit retention controls for uploaded document text, memories, writing samples, and research evidence.

## Product Roadmap

### Near Term

- Polish deep research progress states and failure recovery.
- Make citation chips and source evidence easier to scan on mobile.
- Add "continue from evidence" controls for existing research runs.
- Add clearer settings copy for memory, voice profile, and developer mode.
- Improve first-run onboarding so persona, domain, and artifact defaults are set intentionally.

### Medium Term

- Add share/export flows for research briefs and architecture artifacts.
- Add feedback capture on answers, routes, research sources, and refinements.
- Refine per-user/provider budgets and usage limits.

### Longer Term

- Multi-agent research/evaluation loops with explicit quality thresholds.
- Team/shared workspace mode with permissions.
- Evaluation-driven routing policy tuning.
- First-class observability and cost governance.
