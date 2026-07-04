#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${1:-https://www.fronei.com}"
SUITE="${2:-${LIVE_E2E_SUITE:-smoke}}"
CDP_PORT=9222
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CHROME_TMP_DIR="/tmp/chrome-debug-fronei"
AUTH_FILE=".auth/live-user.json"
AUTH_MAX_AGE_SECONDS=3600

cd "$(dirname "$0")/.."

if [[ "$SUITE" != "smoke" && "$SUITE" != "full" ]]; then
  echo "Unknown suite '$SUITE'. Use 'smoke' or 'full'."
  echo "Examples:"
  echo "  bash scripts/live-eval.sh"
  echo "  bash scripts/live-eval.sh https://www.fronei.com full"
  exit 2
fi

if [[ "$SUITE" == "full" ]]; then
  TEST_TARGET="e2e-live/manual-real-eval.spec.ts"
else
  TEST_TARGET="e2e-live/smoke-live.spec.ts"
fi

needs_auth=true
if [[ -f "$AUTH_FILE" ]]; then
  age=$(( $(date +%s) - $(date -r "$AUTH_FILE" +%s) ))
  if (( age < AUTH_MAX_AGE_SECONDS )); then
    echo "Auth state is fresh (${age}s old) — skipping login."
    needs_auth=false
  else
    echo "Auth state is stale (${age}s old) — re-authenticating."
  fi
fi

if [[ "$needs_auth" == true ]]; then
  pkill -f "remote-debugging-port=$CDP_PORT" 2>/dev/null || true
  sleep 1

  "$CHROME" \
    --remote-debugging-port="$CDP_PORT" \
    --user-data-dir="$CHROME_TMP_DIR" \
    --no-first-run \
    --no-default-browser-check \
    2>/dev/null &
  CHROME_PID=$!

  echo "Waiting for Chrome..."
  for i in $(seq 1 20); do
    if curl -sf "http://127.0.0.1:$CDP_PORT/json/version" >/dev/null 2>&1; then
      echo "Chrome ready."
      break
    fi
    sleep 0.5
  done

  echo ""
  echo "Log into Fronei in the Chrome window, then press Enter here."
  PLAYWRIGHT_BASE_URL="$BASE_URL" npx tsx scripts/save-live-auth-from-cdp.ts

  kill "$CHROME_PID" 2>/dev/null || true
  sleep 1
fi

echo ""
echo "Running live evals against $BASE_URL (suite: $SUITE)..."
set +e
PLAYWRIGHT_BASE_URL="$BASE_URL" LIVE_E2E=1 LIVE_E2E_SUITE="$SUITE" \
  npx playwright test -c playwright.live.config.ts --headed --project=chrome "$TEST_TARGET"
TEST_STATUS=$?
set -e

echo ""
echo "Opening results in Chrome..."
# Start the report server in the background, then open it in Chrome.
# The server keeps running until this script is killed (Ctrl+C).
npx playwright show-report --port 9323 &
REPORT_PID=$!
sleep 2
open -a "Google Chrome" "http://localhost:9323"
echo "Report open at http://localhost:9323 — press Ctrl+C to close."
wait "$REPORT_PID" || true
exit "$TEST_STATUS"
