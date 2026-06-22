#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
API_DIR="$ROOT_DIR/apps/api"
WEB_DIR="$ROOT_DIR/apps/web"
SMOKE_DB="${SMOKE_DB:-/tmp/fronei_smoke_${USER:-user}_$$.db}"
NEXT_ENV_FILE="$WEB_DIR/next-env.d.ts"
NEXT_ENV_BACKUP=""

cleanup() {
  rm -f "$SMOKE_DB"
  if [[ -n "$NEXT_ENV_BACKUP" && -f "$NEXT_ENV_BACKUP" ]]; then
    cp "$NEXT_ENV_BACKUP" "$NEXT_ENV_FILE"
    rm -f "$NEXT_ENV_BACKUP"
  fi
}
trap cleanup EXIT

step() {
  printf '\n==> %s\n' "$1"
}

require_path() {
  local path="$1"
  if [[ ! -e "$path" ]]; then
    printf 'Missing required path: %s\n' "$path" >&2
    exit 1
  fi
}

reject_path() {
  local path="$1"
  if [[ -e "$path" ]]; then
    printf 'Unexpected legacy path still exists: %s\n' "$path" >&2
    exit 1
  fi
}

step "Checking route files"
require_path "$WEB_DIR/app/page.tsx"
require_path "$WEB_DIR/app/admin/page.tsx"
require_path "$API_DIR/app/routers/agent.py"
require_path "$API_DIR/app/routers/admin.py"
reject_path "$WEB_DIR/app/agent-v3"
reject_path "$WEB_DIR/app/v2"
reject_path "$WEB_DIR/app/legacy"
reject_path "$API_DIR/app/routers/agent_v3.py"
reject_path "$API_DIR/app/services/agent_v3"
reject_path "$API_DIR/app/services/agent_runtime"

step "Scanning active code for stale route and module references"
if rg -n "agent-v3|/v2|/legacy" "$WEB_DIR/app"; then
  printf 'Found stale legacy route references in web app code.\n' >&2
  exit 1
fi

if rg -n "agent_v3|agent-runtime" "$API_DIR/app" --glob '!services/design_systems/**'; then
  printf 'Found stale legacy module references in API app code.\n' >&2
  exit 1
fi

step "Validating FastAPI routes and OpenAPI"
(
  cd "$API_DIR"
  uv run python - <<'PY'
from app.main import app

openapi = app.openapi()
paths = set(openapi["paths"])
required = {
    "/health",
    "/turns/stream",
    "/turns",
    "/workspaces",
    "/workspaces/{workspace_id}/conversations",
    "/conversations/{conversation_id}/turns",
    "/admin/me",
}
missing = sorted(required - paths)
if missing:
    raise SystemExit(f"Missing API paths: {missing}")

legacy_prefixes = ("/chat", "/memory", "/research-runs", "/twin-profile")
legacy = sorted(path for path in paths if path.startswith(legacy_prefixes))
if legacy:
    raise SystemExit(f"Legacy API paths still exposed: {legacy}")

print(f"openapi ok: {len(paths)} paths")
PY
)

step "Running Alembic migrations against scratch SQLite"
(
  cd "$API_DIR"
  DATABASE_URL="sqlite:///$SMOKE_DB" uv run python -m alembic upgrade head
)

step "Running focused API smoke tests"
(
  cd "$API_DIR"
  uv run --with pytest python -m pytest -q \
    tests/test_internal_smoke.py \
    tests/test_agent_runtime.py \
    tests/test_agent_model_policy.py \
    tests/test_agent_model_overrides_router.py
)

step "Building web app"
NEXT_ENV_BACKUP="$(mktemp)"
cp "$NEXT_ENV_FILE" "$NEXT_ENV_BACKUP"
(
  cd "$WEB_DIR"
  npm run build
)

step "Smoke test passed"
