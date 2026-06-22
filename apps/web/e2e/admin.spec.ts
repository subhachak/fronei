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
  await expect(page.getByRole('heading', { name: 'Agent v3 model policy' })).toBeVisible()

  await page.getByRole('button', { name: 'System' }).click()
  await expect(page.getByText('Configuration')).toBeVisible()
  await expect(page.getByText('Providers')).toBeVisible()
})
