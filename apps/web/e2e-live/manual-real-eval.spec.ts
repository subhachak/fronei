import { expect, test } from '@playwright/test'

const TURN_TIMEOUT = 12 * 60_000

test.describe.serial('manual real UI eval', () => {
  test.beforeAll(() => {
    if (process.env.LIVE_E2E !== '1' && process.env.LIVE_E2E !== 'true') {
      throw new Error('Live UI evals make real backend/model/tool calls. Run through npm run test:e2e:live or set LIVE_E2E=1 explicitly.')
    }
  })

  test.beforeEach(async ({ page }) => {
    await page.goto('/')
    await expect(page.getByPlaceholder('Give Fronei a task...')).toBeVisible({
      timeout: 120_000,
    })
  })

  test('real comparison matrix turn completes through the UI', async ({ page }) => {
    await openTaskOptions(page)
    await page.getByLabel('Format').selectOption('matrix')

    await runPromptAndWaitForCompletion(
      page,
      'Live eval: compare Epic, Oracle Health/Cerner, Meditech Expanse, athenahealth, and eClinicalWorks for enterprise hospital deployment. Use a comparison matrix with rows for interoperability, implementation complexity, analytics, AI readiness, and total cost risk.',
    )

    await expect(page.getByText(/Took \d|Took under 1 sec/).last()).toBeVisible()
  })

  test('real repair-pressure research turn completes through the UI', async ({ page }) => {
    await runPromptAndWaitForCompletion(
      page,
      'Live eval: research whether Anker Solix has a clearly higher hardware failure rate than EcoFlow and Bluetti. Distinguish confirmed failure rates from anecdotal complaint volume, warranty friction, and support experience.',
    )

    await expect(page.getByText(/Took \d|Took under 1 sec/).last()).toBeVisible()
  })

  test('real short affirmative follow-up keeps conversation context', async ({ page }) => {
    await runPromptAndWaitForCompletion(
      page,
      'Live eval: give a concise answer about managing nighttime blood sugar for type 2 diabetes and end by offering to provide meal-planning details.',
    )

    await runPromptAndWaitForCompletion(page, 'yes')

    await expect(page.getByText(/meal|planning|details|blood sugar/i).last()).toBeVisible()
  })
})

async function openTaskOptions(page: import('@playwright/test').Page) {
  await page.getByRole('button', { name: 'Task options' }).click()
}

async function runPromptAndWaitForCompletion(page: import('@playwright/test').Page, prompt: string) {
  await page.getByPlaceholder('Give Fronei a task...').fill(prompt)
  await page.getByRole('button', { name: 'Start' }).click()

  await expect(page.getByText(prompt.slice(0, 80)).last()).toBeVisible({ timeout: 30_000 })

  const pausedCard = page.getByText('Research paused — budget approval needed')
  const completedFooter = page.getByText(/Took \d|Took under 1 sec/).last()

  await expect.poll(async () => {
    if (await completedFooter.isVisible()) return 'completed'
    if (await pausedCard.isVisible()) return 'paused'
    if (await page.getByText('Working').isVisible()) return 'working'
    return 'waiting'
  }, { timeout: 30_000 }).not.toBe('waiting')

  const outcome = await waitForCompletionOrPause(page)

  if (outcome === 'paused') {
    await page.getByRole('button', { name: 'Approve and continue' }).click()
    await expect(completedFooter).toBeVisible({ timeout: TURN_TIMEOUT })
  }

  await expect(page.getByText('Ready')).toBeVisible({ timeout: 60_000 })
}

async function waitForCompletionOrPause(page: import('@playwright/test').Page) {
  return expect.poll(async () => {
    if (await page.getByText(/Took \d|Took under 1 sec/).last().isVisible()) return 'completed'
    if (await page.getByText('Research paused — budget approval needed').isVisible()) return 'paused'
    return 'running'
  }, {
    timeout: TURN_TIMEOUT,
    message: 'live turn should complete or pause for approval',
  }).not.toBe('running').then(async () => {
    if (await page.getByText('Research paused — budget approval needed').isVisible()) return 'paused' as const
    return 'completed' as const
  })
}
