# Fronei

Fronei is a personal AI workbench. You describe a task in plain language; Fronei plans the work, picks the execution route, searches the web, reads documents, drafts artifacts, and keeps the output visible in a structured workspace.

## What's built

**Frontend (Next.js + React)**
- Three-panel shell: collapsible library rail (workspaces / conversations), center work pane (chat timeline + composer), collapsible context rail (work summary, settings, sources, events, artifacts)
- Mobile: single work pane with sheet drawers for library and context; view-aware top bar with back navigation
- Composer controls: quality mode (draft / standard / executive), output format (chat / markdown / docx / pptx), research level (auto / easy / regular / deep), file attachment, template selection
- Dark / light theme via `data-theme` attribute; navy + gold brand palette
- PWA-ready: manifest, icon sizes, apple-touch-icon, theme-color
- Clerk authentication (email + Google OAuth), with brand-matched sign-in / sign-up pages
- Admin view (embedded) for users, routing signals, model policy, audit logs, system settings

**Backend (FastAPI + Python)**
- Multi-agent runtime: orchestrator → route selection → subtree workers (fast path, web, research, document, deck)
- PPTX and DOCX artifact generation with design system support
- Web search via Tavily (Brave and DuckDuckGo fallback)
- Deep research: sub-question planning, source crawl, credibility scoring, claim extraction, citation synthesis
- Document and image extraction: PDF (vision + first 30 pages), DOCX, PPTX, XLSX, CSV/TSV, HTML/SVG/XML/JSON/YAML, images (30 MB limit, 60 k char output)
- Profile consolidation and per-user preference system
- DB-backed model policy with per-turn admin override
- Signal-based routing escalation (web_fast / agentic) with feedback loop
- Admin endpoints: user management, audit logs, system settings, routing signals, model policy
- SQLite by default, Postgres-ready via `DATABASE_URL`
- Alembic migrations

## Local setup

### 1. Clone and configure

```bash
git clone <your-repo-url> fronei
cd fronei
cp apps/api/.env.example apps/api/.env
```

Fill in `apps/api/.env` with your provider keys and Clerk config.

Create `apps/web/.env.local`:

```bash
NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY=pk_test_...
CLERK_SECRET_KEY=sk_test_...
NEXT_PUBLIC_CLERK_SIGN_IN_URL=/sign-in
NEXT_PUBLIC_CLERK_SIGN_UP_URL=/sign-up
NEXT_PUBLIC_CLERK_AFTER_SIGN_IN_URL=/
NEXT_PUBLIC_CLERK_AFTER_SIGN_UP_URL=/
NEXT_PUBLIC_API_BASE_URL=http://localhost:8000
```

### 2. Run with Docker Compose

```bash
docker compose -f infra/docker-compose.yml up --build
```

- Web: http://localhost:3000
- API health: http://localhost:8000/health
- API docs: http://localhost:8000/docs

### 3. Run without Docker

**API**
```bash
cd apps/api
uv sync          # or: python -m venv .venv && pip install -e .
alembic upgrade head
uvicorn app.main:app --reload --port 8000
```

**Web**
```bash
cd apps/web
npm install
npm run dev
```

## Composer controls

| Control | Options | Default |
|---------|---------|---------|
| Quality mode | draft · standard · executive | standard |
| Output format | chat · markdown · docx · pptx | chat |
| Research level | auto · easy · regular · deep | auto |

Quality mode shapes the model tier and prompt style. Output format determines the artifact type when the agent produces a deliverable. Research level overrides the orchestrator's automatic research depth decision.

## Deployment

- Backend: Render, Railway, Fly.io, or a VPS (`infra/render.yaml`)
- Frontend: Vercel or Cloudflare Pages
- Database: Supabase Postgres, Neon, or Render managed Postgres

Key backend environment variables:

```bash
DATABASE_URL=postgresql+psycopg://...
ALLOWED_ORIGINS=https://your-frontend.com
CLERK_ISSUER=https://your-app.clerk.accounts.dev
CLERK_AUDIENCE=...
ANTHROPIC_API_KEY=...
OPENAI_API_KEY=...
GEMINI_API_KEY=...
OPENROUTER_API_KEY=...
TAVILY_API_KEY=...
BRAVE_API_KEY=...
DAILY_BUDGET_USD=10
ADMIN_USER_IDS=user_...
ADMIN_EMAILS=admin@example.com
```

## Roadmap

See `docs/fronei-roadmap.md` for the full phased roadmap.
