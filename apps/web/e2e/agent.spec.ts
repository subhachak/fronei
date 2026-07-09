import { expect, test } from '@playwright/test'
import { mockFroneiApi } from './api-mocks'

test.beforeEach(async ({ page }) => {
  await mockFroneiApi(page)
})

test('loads the root agent workbench', async ({ page }, testInfo) => {
  await page.goto('/app')

  if (testInfo.project.name === 'mobile-chrome') {
    await expect(page.getByAltText('Fronei').first()).toBeVisible()
  } else {
    await expect(page.getByRole('heading', { name: 'Workbench' })).toBeVisible()
    await expect(page.getByText('Ready')).toBeVisible()
    await expect(page.getByTitle('Rename workspace')).toBeVisible()
  }
  await expect(page.getByPlaceholder('Give Fronei a task...')).toBeVisible()
})

test('runs a mocked agent turn from the composer', async ({ page }) => {
  await page.goto('/app')

  await page.getByPlaceholder('Give Fronei a task...').fill('Draft a launch plan')
  await page.getByRole('button', { name: 'Start' }).click()

  await expect(page.getByText('E2E answer from mocked API.')).toBeVisible()
})

test('renders the mobile agent shell controls', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto('/app')

  await expect(page.getByAltText('Fronei').first()).toBeVisible()
  await expect(page.getByRole('button', { name: 'Open library' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Open context' })).toBeVisible()
})
