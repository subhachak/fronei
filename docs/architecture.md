# Architecture

Fronei is a two-app monorepo:

```
apps/web   Next.js 14 + React + Tailwind + Clerk
apps/api   FastAPI + SQLAlchemy + Alembic + LiteLLM
```

The frontend never calls model providers directly. All model access, auth verification, budget enforcement, routing decisions, and persistence live in the FastAPI backend.

---

## Runtime Flow

```
Browser (AgentShell)
  -> POST /turns          (durable enqueue)
  -> Clerk JWT verification
  -> rate limit check
  -> persist queued Turn + serialized request
  -> bounded turn worker claims a renewable DB lease
  -> orchestrator.py
       - LLM-backed route decision (OrchestratorDecision)
       - signal-based escalation from routing_policy.py
       - output format and research level resolution
  -> runtime.py dispatches to subtree worker:
       fast_path.py        direct answer, low-latency
       research_subtree.py web search + source scoring + citation synthesis
       document_subtree.py document/markdown artifact generation
       deck_subtree.py     PPTX artifact generation
  -> llm_gateway.py via LiteLLM
       - model selected by model_policy.py (DB-backed, per role)
       - per-turn model override for admin users
       - configured fallbacks
  -> persist progress events for status polling
  -> persist Turn result, Events, ToolCalls, Artifacts
  -> background: profile consolidation
```

---

## Frontend

### Entry point

`apps/web/app/page.tsx` renders `<AgentShell />`. Everything else is mounted inside it.

### Component tree

```
AgentShell
├── LibraryPanel (left rail, collapsible)
│   ├── Logo + workspace/conversation list
│   ├── Search, create, rename, delete workspace/conversation
│   └── AccountMenu (profile, admin, theme toggle, sign out)
├── Work pane (center)
│   ├── Desktop header (workspace/conversation breadcrumb, status badge)
│   ├── Timeline
│   │   ├── TurnPair (user bubble + assistant bubble per turn)
│   │   ├── LiveTurn (streaming: user bubble + rolling commentary)
│   │   └── Empty state placeholder
│   └── Composer
│       ├── Textarea + send button
│       ├── Quality mode selector (draft / standard / executive)
│       ├── Output format selector (chat / markdown / docx / pptx)
│       ├── Research level selector (auto / easy / regular / deep)
│       ├── File attach
│       └── Template selector (admin: model override)
├── ContextPanel (right rail, collapsible)
│   ├── Work summary
│   ├── Quick settings (quality, format, research level)
│   ├── Engine events log
│   ├── Sources
│   └── Artifact download
├── ProfileView (full-pane, swapped in via view state)
└── AdminShell (full-pane embedded, admin users only)
```

**Mobile layout**: rails are hidden; Library and Context open as side sheets. Top bar is view-aware — shows the fronei icon + active workspace/conversation in chat view, and a back button in profile/admin view.

### Theme system

Theme is stored in `localStorage` and applied as `data-theme="dark"` or `data-theme="light"` on `<html>`. A blocking inline script in `layout.tsx` reads this before first paint to prevent flash.

Tailwind's `neutral` color scale is remapped to the brand slate/navy palette so all `neutral-*` utility classes in components render as brand colors:

| Token | Value |
|-------|-------|
| neutral-950 | `#0a0f1e` (darkest background) |
| neutral-900 | `#0f172a` (nav/sidebar background) |
| neutral-800 | `#1e293b` (raised surfaces) |
| gold | `#fbbf24` (accent, active states) |
| gold-dark | `#d97706` (light mode accent) |

CSS custom properties in `globals.css` (`--bg-base`, `--bg-nav`, `--ac`, `--t1`, etc.) drive any components not using Tailwind utilities directly. Both dark and light blocks are updated to the navy/gold palette; purple has been removed.

### State management

`useAgent` (custom hook) owns all agent state: workspaces, conversations, turns, running flag, events stream, result, attachments, templates, and all CRUD actions. It is the single source of truth passed down as props.

`useTheme` manages the `data-theme` attribute and localStorage persistence.

---

## Backend

### API routers

| Router | Prefix | Purpose |
|--------|--------|---------|
| `agent.py` | (no prefix) | Workspaces, conversations, turns (sync + streaming), artifacts |
| `documents.py` | `/documents` | Upload/extract documents, manage document templates |
| `profile.py` | `/profile` | User preferences, settings, workspace priorities, usage, export, privacy delete |
| `users.py` | (no prefix) | `/me` — current user record |
| `admin.py` | `/admin` | Users, usage overview, audit logs, system settings, model policy, routing signals |
| `internal.py` | (internal) | Internal service calls |

Key agent endpoints:

```
POST /turns             sync turn
POST /turns/stream      streaming SSE turn
GET  /turns/{id}        turn result
GET  /turns/{id}/status polling status

GET    /workspaces
POST   /workspaces
PATCH  /workspaces/{id}
DELETE /workspaces/{id}

POST   /workspaces/{id}/conversations
DELETE /conversations/{id}
GET    /conversations/{id}/turns

GET    /artifacts/{id}/download
```

### Agent services (`apps/api/app/services/agent/`)

| File | Role |
|------|------|
| `orchestrator.py` | LLM-backed route decision; returns `OrchestratorDecision` |
| `routing_policy.py` | Signal-based escalation (web_fast / agentic); feedback loop |
| `model_policy.py` | DB-backed model role assignments; per-turn admin override |
| `runtime.py` | Dispatches to subtree workers; streams events |
| `fast_path.py` | Direct-answer low-latency worker |
| `research_subtree.py` | Web search, source scoring, claim extraction, citation synthesis |
| `document_subtree.py` | DOCX and Markdown artifact generation |
| `deck_subtree.py` | PPTX artifact generation |
| `pptx_design.py` | Design system application for PPTX |
| `document_ast.py` | Document AST for structured artifact rendering |
| `prompt_library.py` | Centralized prompt management |
| `model_client.py` | Thin wrapper over `llm_gateway.py` |
| `tools.py` / `tool_registry.py` | Tool definitions and registry |
| `persistence.py` | Turn and event persistence helpers |
| `profile_consolidator.py` | Background user profile consolidation |

Other services:

| File | Role |
|------|------|
| `llm_gateway.py` | LiteLLM dispatch with fallbacks |
| `web_context.py` | Tavily / Brave / DuckDuckGo web search |
| `document_extractor.py` | Multi-format document extraction (PDF vision, DOCX, PPTX, XLSX, CSV, HTML, JSON/YAML/XML, images) |
| `document_templates.py` | User-uploaded PPTX template management |
| `clerk.py` | Clerk JWT verification |
| `rate_limit.py` | Per-user rate limiting |
| `notifications.py` | Notification helpers |

### Data model

SQLAlchemy models in `apps/api/app/db/models.py`:

| Model | Purpose |
|-------|---------|
| `User` | Clerk-sourced user record |
| `UserAdminControl` | Admin-managed per-user controls |
| `AdminAuditLog` | Immutable audit trail |
| `AdminSetting` | Key/value system configuration |
| `Workspace` | Named workspace per user |
| `Conversation` | Conversation within a workspace |
| `Turn` | One request/response pair; holds route, cost, latency, result JSON |
| `Event` | Streaming progress events per turn |
| `ToolCall` | Tool invocations per turn |
| `Artifact` | Generated file artifacts (PPTX, DOCX, etc.) |
| `PromptTemplate` | Versioned prompt templates |
| `DocumentTemplate` | User-uploaded PPTX base templates |
| `RoutingSignalCandidate` | Candidate signal phrases for routing escalation |
| `RoutingDecisionFeedback` | Feedback on routing signal matches |

SQLite is the default for local development. Postgres is supported via `DATABASE_URL`. Migrations live in `apps/api/alembic/`.

### Routing and orchestration

The orchestrator (`orchestrator.py`) makes an LLM-backed decision and returns a structured `OrchestratorDecision`:

```python
class OrchestratorDecision(BaseModel):
    route: RouteName            # fast_path | web_fast | agentic | ...
    confidence: float
    output_format: str | None
    research_level: Literal["easy", "regular", "deep"]
    requires_confirmation: bool
    rewritten_request: str | None
```

The routing policy (`routing_policy.py`) runs a signal-match pass over the request before the LLM call. Matched signals can escalate the route to `web_fast` or `agentic` before the orchestrator sees the request. Signal candidates are stored in the DB and updated via admin feedback.

Model assignments are managed in `model_policy.py` from the DB (`AdminSetting`), keyed by role (e.g. `orchestrator`, `direct_answer`, `research_planner`, `document_writer`, `synthesis`). Admin users can override the model for a single turn from the Composer.

### Document extraction limits

- Max upload size: 30 MB
- PDF: first 30 pages via vision extraction
- Output text cap: 60,000 characters

---

## Design decisions

- Provider keys are server-side only; the frontend never sees them.
- Admin authorization is enforced server-side; frontend visibility is a convenience layer only.
- LiteLLM is the single SDK surface for all model providers.
- The orchestrator decides route and format; the composer controls are user hints that the orchestrator may override or confirm.
- Streaming is SSE; the client polls for turn status as a recovery fallback.
- Primary turns execute through a bounded database-backed lease queue. Expired
  leases are retried after worker/process failure, and stale workers cannot
  commit a result after another worker has reclaimed the turn.
- Worker lifecycle logs can be emitted as structured JSON and correlated by
  turn/user/worker ID. Optional Sentry reporting captures terminal failures,
  while the admin Jobs view reports queue depth, worker liveness, retries,
  stale leases, and recent job details.
- Generated artifacts use a blob-store boundary. Local files remain available
  for development, while production can use a private S3-compatible bucket.
  Artifact rows store object locations and hashes; authenticated downloads use
  short-lived presigned URLs rather than sending binary data through the API.
  The private bucket permits browser GET requests from the Fronei web origin.
- All turn data (events, tool calls, cost, latency, route, result) is persisted for inspection, analytics, and future evals.
- Signal-based routing provides a fast, explainable pre-filter before LLM orchestration.

---

## Production hardening backlog

- Structured evals and golden-prompt regression tests
- OpenTelemetry / Langfuse span tracing for model/tool internals
- Durable execution for profile consolidation and other scheduled jobs
- Provider availability pre-checks before dispatch
- Rate limit abuse protection at the edge
- Secret rotation and full Clerk audience enforcement in production
