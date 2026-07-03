# Manual Live UI Eval

Use this when you want a real browser-driven eval through the real frontend,
backend, auth, LangGraph runtime, model calls, and tools.

This is intentionally separate from `apps/web/e2e/`, which uses API mocks for
CI-safe UI coverage.

## What Is Real

- real Next frontend
- real Clerk session
- real API backend
- real database persistence
- real LangGraph research runtime
- real model calls
- real tool/search behavior
- real pause/resume approval flow when the backend pauses

## What Is Not Faked

No API routes are mocked. The live config does not start the E2E auth-bypass
web server and does not set `E2E_AUTH_BYPASS`.

## One-Time Auth Setup

Start or deploy the app you want to test, then save a real browser session:

```bash
cd apps/web
PLAYWRIGHT_BASE_URL=http://127.0.0.1:3100 npm run test:e2e:live:auth
```

Log in normally in the opened browser, wait until the workbench is usable, then
close the browser. Playwright writes `.auth/live-user.json`, which is ignored by
git.

The auth helper launches installed Google Chrome (`--channel=chrome`) rather
than Playwright's bundled Chromium. Google OAuth often rejects bundled
automation browsers with "This browser or app may not be secure."

For a deployed environment, replace `PLAYWRIGHT_BASE_URL` with that URL.

Production example:

```bash
cd apps/web
PLAYWRIGHT_BASE_URL=https://fronei.com npm run test:e2e:live:auth
```

### If Google OAuth Still Blocks Login

If Google shows "This browser or app may not be secure," use an already-normal
Chrome session through the Chrome DevTools Protocol instead of logging in inside
a fresh Playwright-owned browser.

On macOS, quit Chrome completely, then start Chrome with remote debugging:

```bash
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
  --remote-debugging-port=9222 \
  --user-data-dir="$HOME/Library/Application Support/Google/Chrome"
```

In that Chrome window, open `https://fronei.com` and log in normally. Then, in a
separate terminal:

```bash
cd apps/web
PLAYWRIGHT_BASE_URL=https://fronei.com npm run test:e2e:live:auth:cdp
```

Press Enter in the terminal after the Fronei workbench is visible. The helper
saves `.auth/live-user.json` from that real Chrome session.

## Run The Eval

```bash
cd apps/web
PLAYWRIGHT_BASE_URL=http://127.0.0.1:3100 npm run test:e2e:live
```

Production example:

```bash
cd apps/web
PLAYWRIGHT_BASE_URL=https://fronei.com npm run test:e2e:live
```

For Playwright UI mode:

```bash
cd apps/web
PLAYWRIGHT_BASE_URL=http://127.0.0.1:3100 npm run test:e2e:live:ui
```

## Current Scenarios

- real comparison-matrix research turn
- real repair-pressure research turn
- real short affirmative follow-up after an offered next step

Each scenario waits for the turn to complete or pause. If the backend returns a
budget approval pause and the logged-in user is an admin, the test clicks
`Approve and continue` and waits for the resumed completion.
