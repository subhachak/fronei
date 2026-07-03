import { chromium } from '@playwright/test'
import { mkdirSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { createInterface } from 'node:readline/promises'
import { stdin as input, stdout as output } from 'node:process'

const baseURL = process.env.PLAYWRIGHT_BASE_URL || process.env.LIVE_E2E_BASE_URL || 'https://fronei.com'
const cdpURL = process.env.LIVE_E2E_CDP_URL || 'http://127.0.0.1:9222'
const storagePath = resolve(process.cwd(), process.env.LIVE_E2E_STORAGE_STATE || '.auth/live-user.json')

async function main() {
  console.log(`Connecting to Chrome over CDP: ${cdpURL}`)
  const browser = await chromium.connectOverCDP(cdpURL)
  const context = browser.contexts()[0] || await browser.newContext()
  const page = context.pages()[0] || await context.newPage()

  await page.goto(baseURL, { waitUntil: 'domcontentloaded' })
  console.log(`Opened ${baseURL}`)
  console.log('Log in normally in that Chrome window. Wait until the Fronei workbench is usable.')

  const rl = createInterface({ input, output })
  await rl.question('Press Enter here after login is complete...')
  rl.close()

  mkdirSync(dirname(storagePath), { recursive: true })
  await context.storageState({ path: storagePath })
  await browser.close()
  console.log(`Saved auth storage state to ${storagePath}`)
}

main().catch(error => {
  console.error(error)
  process.exit(1)
})
