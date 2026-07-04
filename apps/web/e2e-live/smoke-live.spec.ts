import { expect, test, type Page } from '@playwright/test'

const TURN_TIMEOUT = 90_000
const DEFAULT_LOCAL_API_BASE = 'http://localhost:8000'

type LiveApiContext = {
  apiBase: string
  token: string
}

type WorkspaceResponse = {
  id: string
  name: string
}

type ConversationResponse = {
  id: string
  title: string
}

type StartedTurnResponse = {
  turn_id: string
  conversation_id: string
  status: string
}

type TurnStatusResponse = {
  status: string
  error_message?: string | null
  turn?: {
    answer?: string
    route?: string
  }
}

type FactResponse = {
  entity_id: string
  entity_type: string
  fact_key: string
  fact_value: string
}

test.describe('Fronei production smoke — live', () => {
  test.skip(process.env.LIVE_E2E_SUITE === 'full', 'Smoke suite is skipped when the full live suite is requested.')

  test.beforeAll(() => {
    if (process.env.LIVE_E2E !== '1' && process.env.LIVE_E2E !== 'true') {
      throw new Error('Live UI smoke tests make real backend/model calls. Run through scripts/live-eval.sh or set LIVE_E2E=1 explicitly.')
    }
  })

  test.beforeEach(async ({ page }) => {
    await page.goto('/')
    await expect(page.getByPlaceholder('Give Fronei a task...')).toBeVisible({ timeout: 120_000 })
    await expect(page.getByRole('button', { name: 'Current work' })).toBeVisible({ timeout: 30_000 })
    await expect(page.getByRole('button', { name: 'Pinned facts' })).toBeVisible({ timeout: 30_000 })
    await expect(page.getByRole('button', { name: 'Quick preferences' })).toBeVisible({ timeout: 30_000 })
  })

  test('shell loads authenticated workbench controls', async ({ page }) => {
    await expect(page.getByRole('heading', { name: 'Workbench' })).toBeVisible()
    await expect(page.getByRole('button', { name: /Switch to (dark|light) theme/ })).toBeVisible()
  })

  test('facts modal opens and closes', async ({ page }) => {
    await page.getByRole('button', { name: 'Pinned facts' }).click()
    await expect(page.getByText('Pinned facts').last()).toBeVisible({ timeout: 10_000 })
    await page.keyboard.press('Escape')
    await expect(page.getByText('Pinned facts').last()).not.toBeVisible({ timeout: 10_000 })
  })

  test('facts API creates, updates, lists, and deletes a pinned fact', async ({ page }) => {
    const api = await liveApiContext(page)
    const entityId = `smoke-fact-${Date.now()}`
    const factKey = 'preferred-language'

    await apiFetch<FactResponse>(api, '/api/facts', {
      method: 'PUT',
      body: JSON.stringify({
        entity_id: entityId,
        entity_type: 'workspace',
        fact_key: factKey,
        fact_value: 'Rust',
      }),
    })

    const updated = await apiFetch<FactResponse>(api, '/api/facts', {
      method: 'PUT',
      body: JSON.stringify({
        entity_id: entityId,
        entity_type: 'workspace',
        fact_key: factKey,
        fact_value: 'TypeScript',
      }),
    })
    expect(updated.fact_value).toBe('TypeScript')

    const facts = await apiFetch<FactResponse[]>(api, '/api/facts?entity_type=workspace')
    expect(facts.some(fact => fact.entity_id === entityId && fact.fact_key === factKey && fact.fact_value === 'TypeScript')).toBe(true)

    const deleteResponse = await apiRequest(
      api,
      `/api/facts/${encodeURIComponent(entityId)}/${encodeURIComponent(factKey)}`,
      { method: 'DELETE' },
    )
    expect(deleteResponse.status).toBe(204)

    const afterDelete = await apiFetch<FactResponse[]>(api, '/api/facts?entity_type=workspace')
    expect(afterDelete.some(fact => fact.entity_id === entityId && fact.fact_key === factKey)).toBe(false)
  })

  test('quick preferences opens and closes', async ({ page }) => {
    await page.getByRole('button', { name: 'Quick preferences' }).click()
    await expect(page.getByText(/preference|theme|model|tone/i).last()).toBeVisible({ timeout: 10_000 })
    await page.getByRole('button', { name: 'Close' }).click()
  })

  test('one cheap direct turn completes', async ({ page }) => {
    const runId = Date.now()
    const prompt = `Live smoke ${runId}: What is 2 + 2? Reply with only the number 4.`
    const api = await liveApiContext(page)
    let workspaceId: string | null = null

    try {
      const workspace = await apiFetch<WorkspaceResponse>(api, '/workspaces', {
        method: 'POST',
        body: JSON.stringify({ name: `Fronei Smoke ${runId}` }),
      })
      workspaceId = workspace.id

      const conversation = await apiFetch<ConversationResponse>(api, `/workspaces/${workspace.id}/conversations`, {
        method: 'POST',
        body: JSON.stringify({ title: `Smoke ${runId}` }),
      })

      const started = await apiFetch<StartedTurnResponse>(api, '/turns', {
        method: 'POST',
        body: JSON.stringify({
          message: prompt,
          conversation_id: conversation.id,
          quality_mode: 'draft',
          output_format: 'chat',
          research_level: 'auto',
          comparison_mode: false,
        }),
      })

      const completed = await pollTurnCompletion(api, started.turn_id)
      expect(completed.turn?.answer || '').toContain('4')
    } finally {
      if (workspaceId) {
        await apiFetch<unknown>(api, `/workspaces/${workspaceId}`, { method: 'DELETE' }).catch(error => {
          console.warn(`Could not delete live smoke workspace ${workspaceId}: ${String(error)}`)
        })
      }
    }
  })
})

async function liveApiContext(page: Page): Promise<LiveApiContext> {
  const captured = await captureAuthFromAppRequest(page).catch(() => null)
  if (captured) return captured

  const token = await tokenFromClerk(page)
  if (!token) throw new Error('Could not read or capture a Clerk bearer token from the authenticated browser session.')

  const apiBase = await inferApiBase(page)
  if (apiBase === DEFAULT_LOCAL_API_BASE && new URL(page.url()).hostname !== 'localhost') {
    throw new Error('Could not infer the production API base URL from browser network activity.')
  }

  return { apiBase, token }
}

async function tokenFromClerk(page: Page): Promise<string | null> {
  return await page.evaluate(async () => {
    const clerk = (window as unknown as {
      Clerk?: {
        session?: {
          getToken?: () => Promise<string | null>
        }
        loaded?: boolean
        load?: () => Promise<void>
        client?: {
          sessions?: Array<{
            getToken?: () => Promise<string | null>
          }>
        }
      }
    }).Clerk

    if (!clerk) return null
    if (!clerk.loaded && clerk.load) await clerk.load().catch(() => undefined)

    const activeToken = await clerk.session?.getToken?.().catch(() => null)
    if (activeToken) return activeToken

    for (const session of clerk.client?.sessions || []) {
      const sessionToken = await session.getToken?.().catch(() => null)
      if (sessionToken) return sessionToken
    }

    return null
  })
}

async function captureAuthFromAppRequest(page: Page): Promise<LiveApiContext> {
  const requestPromise = page.waitForRequest(request => {
    const auth = request.headers().authorization
    if (!auth?.startsWith('Bearer ')) return false
    try {
      return new URL(request.url()).pathname === '/workspaces'
    } catch {
      return false
    }
  }, { timeout: 30_000 })

  await page.reload()
  await expect(page.getByPlaceholder('Give Fronei a task...')).toBeVisible({ timeout: 120_000 })

  const request = await requestPromise
  const auth = request.headers().authorization || ''
  const token = auth.replace(/^Bearer\s+/i, '').trim()
  if (!token) throw new Error('Could not capture an Authorization header from the app workspaces request.')

  return {
    apiBase: new URL(request.url()).origin,
    token,
  }
}

async function inferApiBase(page: Page): Promise<string> {
  const explicit = process.env.LIVE_E2E_API_BASE_URL || process.env.NEXT_PUBLIC_API_BASE_URL
  if (explicit) return explicit.replace(/\/$/, '')

  const observed = await page.evaluate(() => {
    const apiResource = performance
      .getEntriesByType('resource')
      .map(entry => entry.name)
      .find(name => /\/(?:workspaces|turns|facts|profile)\b/.test(name))
    if (!apiResource) return null
    return new URL(apiResource).origin
  })
  if (observed) return observed.replace(/\/$/, '')

  return DEFAULT_LOCAL_API_BASE
}

async function apiFetch<T>(api: LiveApiContext, path: string, init: RequestInit = {}): Promise<T> {
  const response = await apiRequest(api, path, init)
  const text = await response.text()
  if (!response.ok) {
    throw new Error(`Live API ${init.method || 'GET'} ${path} failed (${response.status}): ${text}`)
  }
  return (text ? JSON.parse(text) : null) as T
}

async function apiRequest(api: LiveApiContext, path: string, init: RequestInit = {}): Promise<Response> {
  const url = `${api.apiBase}${path}`
  const headers = {
    ...(init.body ? { 'Content-Type': 'application/json' } : {}),
    Authorization: `Bearer ${api.token}`,
    ...(init.headers || {}),
  }
  let response: Response
  try {
    response = await fetch(url, { ...init, headers })
  } catch (error) {
    throw new Error(`Live API ${init.method || 'GET'} ${url} could not be reached: ${String(error)}`)
  }
  return response
}

async function pollTurnCompletion(api: LiveApiContext, turnId: string): Promise<TurnStatusResponse> {
  const deadline = Date.now() + TURN_TIMEOUT
  let lastStatus: TurnStatusResponse | null = null

  while (Date.now() < deadline) {
    lastStatus = await apiFetch<TurnStatusResponse>(api, `/turns/${turnId}/status`)
    if (lastStatus.status === 'completed') return lastStatus
    if (['failed', 'cancelled', 'paused'].includes(lastStatus.status)) {
      throw new Error(`Live smoke turn ended with status ${lastStatus.status}: ${lastStatus.error_message || 'no error message'}`)
    }
    await new Promise(resolve => setTimeout(resolve, 1000))
  }

  throw new Error(`Live smoke turn did not complete within ${TURN_TIMEOUT / 1000}s. Last status: ${JSON.stringify(lastStatus)}`)
}
