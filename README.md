# Fronei

Fronei is a personal AI workbench that plans, routes, researches, reads documents, remembers user context, and adapts responses to the user's writing style. The user asks in plain language; Fronei chooses the right execution path and model stack behind the scenes.

## What's built

- Next.js web UI with dark/light theme, collapsible sidebar, streaming chat, workbench mode, settings, and usage dashboard
- FastAPI backend with planner → web/document context → policy router → worker/synthesis/refinement pipeline
- Clerk authentication (email, Google OAuth)
- Multi-provider support: OpenAI, Anthropic, Gemini, DeepSeek, Qwen, Perplexity via LiteLLM
- YAML-driven routing policy (task type × complexity × profile)
- Quick / Smart / Thorough routing profiles
- Web search via Tavily (Brave and DuckDuckGo fallback)
- Deep research mode with source search, source scoring, claim extraction, findings, gaps, contradictions, and citation metadata
- Document and image attachment extraction for PDF, DOCX, PPTX, XLSX, CSV/TSV, text-like files, HTML/SVG/XML/JSON/YAML, and common image types
- Within-conversation memory (rolling summary + active task state)
- Persistent user memories extracted from useful conversation facts
- Twin profile voice adaptation from user writing samples
- Artifact formatting for ADRs, solution comparisons, trade-off matrices, executive briefs, risk registers, NFR analysis, and steering updates
- Admin section for users, usage, provider status, routing tests, research runs, privacy actions, audit logs, and system configuration
- Analytics dashboard with cost, latency, and model usage charts
- Daily budget guard
- SQLite by default, Postgres-ready via `DATABASE_URL`
- Alembic migrations
- Docker Compose for local development

## Local setup

### 1. Clone and configure

```bash
git clone <your-repo-url> fronei
cd fronei
cp apps/api/.env.example apps/api/.env
```

Fill in `apps/api/.env` with your provider keys and Clerk issuer URL.

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

## Run without Docker

### API

```bash
cd apps/api
uv sync          # or: python -m venv .venv && pip install -e .
alembic upgrade head
uvicorn app.main:app --reload --port 8000
```

### Web

```bash
cd apps/web
npm install
npm run dev
```

## Routing policy

Model assignments live in `apps/api/app/policies/routing_rules.yaml`. Edit this file to change which model handles each task type, complexity level, and profile. The policy is cached by the API process, so restart the API after changes.

The UI labels map to backend profiles:

| UI label | API profile | Intent |
|----------|-------------|--------|
| Quick | `cost_saver` | Lower cost and latency |
| Smart | `balanced` | Default daily work |
| Thorough | `best_quality` | Stronger models and deeper reasoning |

## Deployment

- Backend: Render, Railway, Fly.io, or a VPS (see `infra/render.yaml`)
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

See `docs/fronei-roadmap.md` for the full phased roadmap with implementation prompts.
