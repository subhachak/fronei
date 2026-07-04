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

## Auth Setup

The normal trigger handles auth freshness automatically. If `.auth/live-user.json`
is missing or stale, it opens Chrome and prompts you to log in.

```bash
cd apps/web
npm run test:e2e:live
```

Log in normally in the opened browser, wait until the workbench is usable, then
press Enter in the terminal. Playwright writes `.auth/live-user.json`, which is
ignored by git.

The auth helper launches installed Google Chrome (`--channel=chrome`) rather
than Playwright's bundled Chromium. Google OAuth often rejects bundled
automation browsers with "This browser or app may not be secure."

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

Single trigger for the stable production smoke suite:

```bash
cd apps/web
npm run test:e2e:live
```

This runs a small live canary against `https://www.fronei.com` by default:
authenticated workbench load, facts modal, quick preferences, and one cheap
direct turn. The shell/modal checks use the browser UI; the direct-turn canary
uses the authenticated browser session to create an isolated temporary
workspace/conversation through the API and poll backend status, so it is not
coupled to whichever real conversation is selected in the sidebar.

To run against a different frontend URL:

```bash
cd apps/web
bash scripts/live-eval.sh http://127.0.0.1:3100
```

The old expensive matrix is intentionally opt-in:

```bash
cd apps/web
npm run test:e2e:live:full
```

For Playwright UI mode:

```bash
cd apps/web
npm run test:e2e:live:ui
```

## Current Scenarios

- smoke suite: authenticated workbench controls, facts modal, quick preferences,
  and one cheap direct model turn
- full suite: expensive live matrix covering research, document generation,
  multi-turn continuity, facts CRUD, and Context OS scenarios

The full suite is not the default regression gate because it shares real
production state and makes many live model/tool calls.
