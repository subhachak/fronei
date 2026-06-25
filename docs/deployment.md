# Deployment

## Stack

| Layer | Service | Notes |
|-------|---------|-------|
| API | Render, Railway, Fly.io, or VPS | FastAPI app in `apps/api` |
| Frontend | Vercel or Cloudflare Pages | Next.js app in `apps/web` |
| Database | Neon, Supabase, Render Postgres, or local SQLite | Set via `DATABASE_URL` |
| Auth | Clerk | JWTs verified by the API |
| LLM providers | OpenAI, Anthropic, Gemini, OpenRouter | Accessed through LiteLLM |
| Search providers | Tavily, Brave, DuckDuckGo fallback | Tavily/Brave use API keys; DuckDuckGo is fallback |

---

## Step 1 — Postgres database (Neon or Supabase)

### Neon (recommended)
1. Sign up at [neon.tech](https://neon.tech)
2. Create a project → copy the **Connection string** (looks like `postgresql://user:pass@ep-xxx.us-east-1.aws.neon.tech/neondb?sslmode=require`)

### Supabase
1. Sign up at [supabase.com](https://supabase.com)
2. New project → **Settings → Database → Connection string → URI** (use the `postgresql://` URI, not the pooler URL)

Keep this URL handy — you'll need it in Steps 2 and 3.

> **Schema migrations are managed only by Alembic.** Application startup never
> creates or repairs tables. Run `uv run python -m alembic upgrade head` before
> the first start and whenever deployed code includes migrations. A blank or
> stale database causes startup to fail with a schema-version error.

---

## Step 2 — Deploy API to Render

1. Push this repo to GitHub.
2. Go to [render.com](https://render.com) → **New → Web Service** → connect your repo.
3. Render will detect `infra/render.yaml` automatically. Click **Apply**.
4. In the Render dashboard for `fronei-api`, go to **Environment** and fill in the `sync: false` vars:

| Key | Value |
|-----|-------|
| `APP_ENV` | Set to `production`. This enables a startup check requiring `CLERK_ISSUER`, `CLERK_AUDIENCE`, and at least one admin allowlist entry — the API will refuse to start without them. |
| `DATABASE_URL` | Your Postgres connection string from Step 1 |
| `ALLOWED_ORIGINS` | Your Vercel URL (add after Step 3, e.g. `https://fronei.vercel.app`) |
| `CLERK_ISSUER` | Your Clerk issuer/frontend API URL |
| `CLERK_AUDIENCE` | **Required in production.** Set this to your Clerk app's API audience (or configure an `aud` claim in your Clerk JWT template). Without it, JWT audience verification is disabled — the API will fail to start in production until this is set. |
| `OPENAI_API_KEY` | Your key |
| `ANTHROPIC_API_KEY` | Your key |
| `GEMINI_API_KEY` | Your key |
| `OPENROUTER_API_KEY` | Your key |
| `TAVILY_API_KEY` | Optional, enables web search |
| `BRAVE_API_KEY` | Optional, secondary web-search provider |
| `DAILY_BUDGET_USD` | Daily per-user budget guard |
| `ADMIN_USER_IDS` | Comma-separated Clerk user IDs allowed to access admin |
| `ADMIN_EMAILS` | Optional comma-separated admin emails when available in JWT claims |

At least one model provider key is required for chat. For the default routing policy, configure multiple providers if you want the fallback chains to work as intended.

5. Click **Save Changes** → Render redeploys automatically.
6. In the Render dashboard, open a **Shell** tab and run:
   ```bash
   cd apps/api && alembic upgrade head
   ```
7. Copy your API URL: `https://fronei-api.onrender.com` (shown at the top of the service page).

---

## Step 3 — Deploy frontend to Vercel

1. Go to [vercel.com](https://vercel.com) → **New Project** → import your repo.
2. Set **Root Directory** to `apps/web`.
3. Add environment variables:

| Key | Value |
|-----|-------|
| `NEXT_PUBLIC_API_BASE_URL` | Your Render API URL from Step 2 |
| `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY` | From Clerk dashboard |
| `CLERK_SECRET_KEY` | From Clerk dashboard |
| `NEXT_PUBLIC_CLERK_SIGN_IN_URL` | `/sign-in` |
| `NEXT_PUBLIC_CLERK_SIGN_UP_URL` | `/sign-up` |
| `NEXT_PUBLIC_CLERK_AFTER_SIGN_IN_URL` | `/` |
| `NEXT_PUBLIC_CLERK_AFTER_SIGN_UP_URL` | `/` |

4. Click **Deploy**.
5. Copy your Vercel URL (e.g. `https://fronei.vercel.app`).

---

## Step 4 — Wire up CORS

Go back to Render → `fronei-api` → **Environment** → set:

```
ALLOWED_ORIGINS=https://fronei.vercel.app
```

---

## Private artifact storage

Production should store generated DOCX/PPTX artifacts in a private
S3-compatible bucket rather than the API filesystem:

```bash
ARTIFACT_STORAGE_BACKEND=s3
ARTIFACT_S3_BUCKET=fronei-artifacts
ARTIFACT_S3_ENDPOINT_URL=https://<account-id>.r2.cloudflarestorage.com
ARTIFACT_S3_REGION=auto
ARTIFACT_S3_ACCESS_KEY_ID=<access-key>
ARTIFACT_S3_SECRET_ACCESS_KEY=<secret-key>
ARTIFACT_S3_KEY_PREFIX=artifacts
ARTIFACT_DOWNLOAD_URL_TTL_SECONDS=300
```

For AWS S3, omit `ARTIFACT_S3_ENDPOINT_URL` and set the AWS region. Configure
the bucket CORS policy to allow `GET` from the deployed frontend origin.

After configuring the target bucket, inspect legacy rows:

```bash
cd apps/api
uv run python -m app.services.artifact_migration --dry-run
```

Then migrate them:

```bash
uv run python -m app.services.artifact_migration
```

The migration updates rows only after a successful upload and does not delete
the legacy source file, allowing verification before manual cleanup.

---

## Local development

Local dev uses SQLite — no Postgres needed:

```bash
# apps/api/.env
DATABASE_URL=sqlite:///./fronei.db
CLERK_ISSUER=https://your-app.clerk.accounts.dev
```

Apply migrations before the first run:

```bash
cd apps/api
uv run python -m alembic upgrade head
uv run uvicorn app.main:app --reload --port 8000
```

Run the web app separately:

```bash
cd apps/web
npm install
npm run dev
```

Required local web env:

```bash
NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY=pk_test_...
CLERK_SECRET_KEY=sk_test_...
NEXT_PUBLIC_CLERK_SIGN_IN_URL=/sign-in
NEXT_PUBLIC_CLERK_SIGN_UP_URL=/sign-up
NEXT_PUBLIC_CLERK_AFTER_SIGN_IN_URL=/
NEXT_PUBLIC_CLERK_AFTER_SIGN_UP_URL=/
NEXT_PUBLIC_API_BASE_URL=http://localhost:8000
```

## Runtime Features To Account For

- Deep research can run substantially longer than normal chat because it searches, crawls, extracts claims, verifies gaps, and synthesizes from stored evidence.
- PDF/image extraction uses vision models and can be more expensive than parser-only document extraction.
- Background memory and writing-style extraction run in thread pools and call model providers after the response is returned.
- SQLite works for local development, but Postgres is recommended for production and multi-user usage.
- The API checks daily user spend before dispatching model calls.
- Admin functionality is hidden in the UI for non-admins and enforced by `/admin/*` backend authorization. Configure at least one of `ADMIN_USER_IDS` or `ADMIN_EMAILS` — these are the only supported admin allowlists; Clerk metadata roles are not checked. The same allowlist (by user ID or email) also exempts admins from per-user rate limits and deep-research throttling.

---

## Troubleshooting

**`502 Bad Gateway` on first Render deploy**
Render free tier spins down after 15 min of inactivity. The first request cold-starts in ~30s.

**`CORS error` in browser**
`ALLOWED_ORIGINS` doesn't match the exact Vercel URL. No trailing slash, scheme must be `https://`.

**`could not connect to server`**
The `DATABASE_URL` is wrong or the Postgres instance isn't running. Neon requires `?sslmode=require` at the end.

**Tables not created**
Run `alembic upgrade head` from `apps/api/` in the Render Shell.

**`401 Invalid or expired token`**
Check `CLERK_ISSUER`, frontend Clerk environment variables, and whether `CLERK_AUDIENCE` is set consistently between Clerk and the API.

**Research returns few sources**
Set `TAVILY_API_KEY` and optionally `BRAVE_API_KEY`. Without those, Fronei falls back to DuckDuckGo search and direct URL crawling.
