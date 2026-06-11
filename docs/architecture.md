# Architecture

Fronei is a two-app monorepo:

```text
apps/web  Next.js + React + Clerk
apps/api  FastAPI + SQLAlchemy + Alembic + LiteLLM
```

The frontend never calls model providers directly. It talks to the FastAPI backend, which owns authentication checks, provider keys, routing policy, budget enforcement, persistence, research orchestration, document extraction, and model fallback behavior.

## Main Runtime Flow

```text
Next.js chat UI
  -> POST /conversations/chat/stream
  -> Clerk JWT verification
  -> daily budget gate
  -> persist user message
  -> planner.py
       - intent and turn-type detection
       - complexity/task override
       - web-search decision
       - prompt enrichment
       - optional sub-query decomposition
       - deep-research recommendation
  -> chat_pipeline.py
       - document context assembly
       - artifact prompt injection
       - web context gathering when needed
       - route selection from routing_rules.yaml
  -> llm_gateway.py through LiteLLM
       - primary model
       - configured fallbacks
       - safety-net fallbacks
       - optional native search for supported models
  -> optional synthesis for decomposed work
  -> optional voice refinement from TwinProfile
  -> persist assistant message, execution log, cost, tokens, latency
  -> background memory summary and fact extraction
  -> stream SSE events back to the UI
```

The older stateless `/chat` endpoint still exists for single-turn use, but the main product path is the conversation streaming endpoint.

## Frontend

The primary UI lives in `apps/web/app/page.tsx`. It includes:

- Chat thread with streaming responses.
- Classic and workbench home modes.
- Sidebar conversation history with rename, delete, export, search, budget display, and sign out.
- Settings view for theme, developer mode, web search visibility, voice samples, saved memory, workspace persona, artifacts, model defaults, and local user profile fields.
- Dashboard view for spend, request volume, token usage, latency, model usage, and task distribution.
- File attachment flow that extracts documents before sending the chat request.
- Research evidence UI with citation chips, sources, claims, findings, gaps, contradictions, and verifier notes.
- Developer execution log panel showing planner, web context, routing, worker, model fallbacks, cost, tokens, and latency.

## Backend API

FastAPI routers are split by product surface:

| Router | Purpose |
|--------|---------|
| `/chat` | Stateless single-turn chat |
| `/conversations` | Stored multi-turn chat and SSE streaming |
| `/documents` | Upload and extract supported documents/images |
| `/memory` | List/create/delete persistent user memories |
| `/models` | Read the routing policy |
| `/analytics` | Usage, cost, token, latency, model, and task stats |
| `/research-runs` | Inspect persisted deep-research evidence |
| `/twin-profile` | Writing samples, style fingerprint, and voice preferences |
| `/admin` | Admin-only operations for users, usage, providers, routing, research, privacy, audit, and system status |

## Data Model

SQLAlchemy models live in `apps/api/app/db/models.py`. The core tables are:

- `users`
- `conversations`
- `conversation_messages`
- `request_logs`
- `user_memories`
- `writing_samples`
- `twin_profiles`
- `research_runs`
- `research_questions`
- `research_sources`
- `research_claims`
- `research_findings`
- `user_admin_controls`
- `admin_audit_logs`

SQLite is the default local database. Postgres is supported through `DATABASE_URL`. Alembic migrations live in `apps/api/alembic`.

## Planning And Routing

The planner is LLM-backed and returns structured intent metadata. The router is policy-first: it uses the planner's overrides when present, then selects from `apps/api/app/policies/routing_rules.yaml`, then appends safety-net fallbacks.

The UI presents profiles as:

- Quick -> `cost_saver`
- Smart -> `balanced`
- Thorough -> `best_quality`

Web-search requests prefer search-native models. Deep research uses a separate research orchestrator instead of the normal lightweight web-context path.

## Research Architecture

Deep research in `research_orchestrator.py` is separate from normal chat. It:

1. Plans subquestions.
2. Searches with Tavily, Brave, or DuckDuckGo.
3. Crawls direct URLs and search results.
4. Scores source credibility, relevance, freshness, and type.
5. Extracts citation-grade claims.
6. Evaluates gaps and contradictions.
7. Synthesizes a cited answer.
8. Stores the run, questions, sources, claims, and findings.
9. Answers follow-up turns from the existing evidence store when possible.

## Document Architecture

`document_extractor.py` supports:

- Vision extraction for PDFs and images.
- Parser extraction for DOCX, PPTX, XLSX, CSV/TSV, text/Markdown, HTML/SVG, JSON/YAML/XML.
- Upload limit: 30 MB.
- PDF extraction limit: first 30 pages.
- Extracted text limit: 60,000 characters.

The frontend sends extracted document text as attached document context to the conversation endpoint.

## Memory And Voice

Fronei has three personalization layers:

- Conversation-local rolling summary and active task state on `conversations`.
- Persistent memories in `user_memories`, extracted in the background from useful user facts.
- Twin profile voice adaptation from writing samples, stored as a structured fingerprint plus a rewrite prompt.

When a TwinProfile exists and the output mode is not `raw`, long enough answers can be streamed through the refinement pass.

## Design Choices

- Keep provider keys server-side.
- Enforce admin authorization server-side; frontend visibility is only a convenience.
- Use LiteLLM as the single provider SDK surface.
- Keep routing policy explainable and editable in YAML.
- Stream pipeline events so the UI can show progress without exposing provider internals by default.
- Persist execution logs for inspection, analytics, debugging, and future evaluation.
- Treat deep research as an evidence-backed workflow, not just "chat with web search."

## Production Hardening Backlog

- Rate limits and abuse protection.
- Provider availability checks before dispatch.
- More complete model cost tables and budget projections before each request.
- Structured evals and golden-prompt regression tests.
- OpenTelemetry, Langfuse, or equivalent tracing.
- Background job queue for memory, fingerprint extraction, and long research runs.
- Stronger concurrency handling for SQLite-heavy local workloads.
- Secret rotation and production Clerk audience enforcement.
