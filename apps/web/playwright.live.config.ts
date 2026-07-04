import { defineConfig, devices } from '@playwright/test'
import { existsSync } from 'node:fs'
import { resolve } from 'node:path'

const baseURL = process.env.PLAYWRIGHT_BASE_URL || process.env.LIVE_E2E_BASE_URL || 'http://127.0.0.1:3100'
const storageStatePath = resolve(process.cwd(), process.env.LIVE_E2E_STORAGE_STATE || '.auth/live-user.json')
const storageState = existsSync(storageStatePath) ? storageStatePath : undefined

export default defineConfig({
  testDir: './e2e-live',
  fullyParallel: false,
  forbidOnly: true,
  retries: 0,
  workers: 1,
  timeout: 15 * 60_000,
  expect: {
    timeout: 2 * 60_000,
  },
  reporter: [['list'], ['html', { open: 'never' }]],
  use: {
    baseURL,
    storageState,
    trace: 'off',
    screenshot: 'only-on-failure',
    video: 'off',
    actionTimeout: 30_000,
    navigationTimeout: 60_000,
  },
  projects: [
    { name: 'chrome', use: { ...devices['Desktop Chrome'], channel: 'chrome' } },
  ],
})
