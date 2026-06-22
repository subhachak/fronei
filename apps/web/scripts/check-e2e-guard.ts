import { spawnSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'

const script = `
  const guard = require(${JSON.stringify(fileURLToPath(new URL('../app/lib/e2e.ts', import.meta.url)))});
  if (guard.e2eAuthBypassEnabled()) process.exit(10);
  if (guard.e2eProxyBypassEnabled()) process.exit(11);
  try {
    guard.assertNoProductionE2EBypass();
    process.exit(12);
  } catch (err) {
    if (!String(err && err.message || err).includes('E2E auth bypass flags must never be enabled in production.')) {
      console.error(err);
      process.exit(13);
    }
  }
`

const result = spawnSync(process.execPath, ['--import', 'tsx', '-e', script], {
  stdio: 'inherit',
  env: {
    ...process.env,
    NODE_ENV: 'production',
    E2E_AUTH_BYPASS: 'true',
    NEXT_PUBLIC_E2E_AUTH_BYPASS: 'true',
  },
})

if (result.status !== 0) {
  process.exit(result.status ?? 1)
}
