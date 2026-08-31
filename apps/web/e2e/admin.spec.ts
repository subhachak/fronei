import { expect, test } from '@playwright/test'
import { mockFroneiApi } from './api-mocks'

test.beforeEach(async ({ page }) => {
  await mockFroneiApi(page)
})

test('loads the admin overview with mocked admin access', async ({ page }) => {
  await page.goto('/admin')

  await expect(page.getByRole('heading', { name: 'Admin' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Overview' })).toBeVisible()
  await expect(page.getByText('Requests today')).toBeVisible()
  await expect(page.getByText('Conversations')).toBeVisible()
})

test('switches admin tabs', async ({ page }) => {
  await page.goto('/admin')

  await page.getByRole('button', { name: 'Model policy' }).click()
  await expect(page.getByRole('heading', { name: 'Fronei model policy' })).toBeVisible()

  await page.getByRole('button', { name: 'System' }).click()
  await expect(page.getByText('Configuration')).toBeVisible()
  await expect(page.getByText('Providers')).toBeVisible()
})

test('the overview survives a response missing the route breakdown', async ({ page }) => {
  // Object.entries(undefined) threw out of OverviewTab, and because the throw
  // escaped the component AdminShell rendered nothing: Users, Model policy and
  // System were all unreachable because one breakdown table had no data. The
  // contract does promise the field; a breach of it should cost one table.
  await page.route('**/admin/overview', route =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        users: 1,
        requests_today: 3,
        spend_today: 0.12,
        errors_today: 0,
        running_research_runs: 0,
        total_conversations: 1,
        total_memories: 0,
        total_writing_samples: 0,
        total_research_runs: 0,
      }),
    }),
  )
  await page.goto('/admin')

  await expect(page.getByText('Requests today')).toBeVisible()
  await expect(page.getByText('Conversations')).toBeVisible()
  await page.getByRole('button', { name: 'System' }).click()
  await expect(page.getByText('Configuration')).toBeVisible()
})
