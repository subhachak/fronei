import { expect, test, type Locator, type Page } from '@playwright/test'

const TURN_TIMEOUT = 12 * 60_000
const FAST_TURN_TIMEOUT = 90_000 // direct/clarify should land well under 90s
const COMPLETION_FOOTER_RE = /Took \d|Took under 1 sec/
const LIVE_CONNECTION_DROPPED_RE = /live connection dropped/i

// ──────────────────────────────────────────────────────────────────────────────
// Suite guard + shared setup
// ──────────────────────────────────────────────────────────────────────────────

test.describe('Fronei regression suite — live', () => {
  test.beforeAll(() => {
    if (process.env.LIVE_E2E !== '1' && process.env.LIVE_E2E !== 'true') {
      throw new Error(
        'Live UI evals make real backend/model/tool calls. Run through npm run test:e2e:live or set LIVE_E2E=1 explicitly.',
      )
    }
  })

  test.beforeEach(async ({ page }) => {
    await page.goto('/')
    await expect(page.getByPlaceholder('Give Fronei a task...')).toBeVisible({ timeout: 120_000 })

    // Ensure Clerk session is resolved (isSignedIn=true) before any test action.
    // Without this, useFacts.enabled = Boolean(null) = false and PUT requests are dropped.
    await page.getByPlaceholder('Give Fronei a task...').fill('x')
    await expect(
      page.getByRole('button', { name: 'Start', exact: true }),
    ).toBeEnabled({ timeout: 30_000 })
    await page.getByPlaceholder('Give Fronei a task...').fill('')

    // Start each test in a fresh 0-turn conversation.
    // Avoids context accumulation that pushes simple prompts into the research route,
    // and ensures no in-flight turn from a prior failing test keeps running=true.
    const openLibBtn = page.getByRole('button', { name: 'Open library' })
    if (await openLibBtn.isVisible({ timeout: 2_000 }).catch(() => false)) {
      await openLibBtn.click()
    }
    await page.getByRole('button', { name: 'New conversation' }).first().click()
    await expect(
      page.getByRole('button', { name: 'Start', exact: true }),
    ).toBeDisabled({ timeout: 10_000 })
  })

  // ────────────────────────────────────────────────────────────────────────────
  // Block A — fast-path routes (direct / clarify)
  // ────────────────────────────────────────────────────────────────────────────

  test('A1: direct — simple factual Q&A completes fast', async ({ page }) => {
    await runPromptAndWaitForCompletion(
      page,
      'Regression A1: What is the capital of France? Answer in one sentence.',
      { turnTimeout: FAST_TURN_TIMEOUT },
    )
    await expectLatestAssistantText(page, /Paris/i)
    await expectLatestAssistantText(page, /Completed as direct/i)
  })

  test('A2: direct — markdown output format renders without error', async ({ page }) => {
    await openTaskOptions(page)
    await page.getByLabel('Output').selectOption('markdown')

    await runPromptAndWaitForCompletion(
      page,
      'Regression A2: List the five largest planets in the solar system as a markdown bullet list.',
      { turnTimeout: FAST_TURN_TIMEOUT },
    )
    await expectLatestAssistantText(page, /Completed as direct/i)
  })

  test('A3: ambiguous prompt — model asks a clarifying question', async ({ page }) => {
    await runPromptAndWaitForCompletion(
      page,
      'Regression A3: Help me prepare.',
      { turnTimeout: FAST_TURN_TIMEOUT },
    )
    // Model should ask for clarification regardless of which route is selected
    await expectLatestAssistantText(page, /\?/)
    await expectLatestAssistantText(page, COMPLETION_FOOTER_RE)
  })

  test('A4: empty composer — Start button is disabled when input is blank', async ({ page }) => {
    const startBtn = page.getByRole('button', { name: 'Start', exact: true })
    await expect(startBtn).toBeDisabled()
  })

  // ────────────────────────────────────────────────────────────────────────────
  // Block B — research routes
  // ────────────────────────────────────────────────────────────────────────────

  test('B1: research — standard research turn completes', async ({ page }) => {
    await runPromptAndWaitForCompletion(
      page,
      'Regression B1: Research whether Anker Solix has a clearly higher hardware failure rate than EcoFlow and Bluetti. Distinguish confirmed failure rates from anecdotal complaint volume.',
    )
    await expectLatestAssistantText(page, COMPLETION_FOOTER_RE)
  })

  test('B2: research — deep research shows budget approval and then completes', async ({ page }) => {
    await openTaskOptions(page)
    await page.getByLabel('Research').selectOption('deep')

    const prevFooterCount = await page.getByText(COMPLETION_FOOTER_RE).count()
    await page.getByPlaceholder('Give Fronei a task...').fill(
      'Regression B2: Deep research on the competitive dynamics between OpenAI and Anthropic in the enterprise AI market as of 2025.',
    )
    await page.getByRole('button', { name: 'Start', exact: true }).click()

    const outcome = await waitForCompletionOrPause(page, prevFooterCount)
    if (outcome === 'paused') {
      await expect(page.getByText('Research paused — budget approval needed')).toBeVisible()
      await page.getByRole('button', { name: 'Approve and continue' }).click()
      await expect.poll(async () => {
        const count = await page.getByText(COMPLETION_FOOTER_RE).count()
        return count > prevFooterCount
      }, { timeout: TURN_TIMEOUT }).toBe(true)
    }
    await expect.poll(async () => {
      const count = await page.getByText(COMPLETION_FOOTER_RE).count()
      return count > prevFooterCount
    }, { timeout: TURN_TIMEOUT }).toBe(true)
  })

  test('B3: comparison matrix — completes and shows Took footer', async ({ page }) => {
    await openTaskOptions(page)
    await page.getByLabel('Format').selectOption('matrix')

    await runPromptAndWaitForCompletion(
      page,
      'Regression B3: Compare Epic, Meditech, and athenahealth for enterprise hospital deployment. Focus on interoperability, implementation complexity, and total cost risk.',
    )
    await expectLatestAssistantText(page, COMPLETION_FOOTER_RE)
  })

  // ────────────────────────────────────────────────────────────────────────────
  // Block C — document generation
  // ────────────────────────────────────────────────────────────────────────────

  test('C1: docx — document output turn completes', async ({ page }) => {
    await openTaskOptions(page)
    await page.getByLabel('Output').selectOption('docx')
    await page.getByLabel('Quality').selectOption('draft')

    await runPromptAndWaitForCompletion(
      page,
      'Regression C1: Write a one-paragraph executive summary of the AI industry in 2025.',
    )
    await expectLatestAssistantText(page, COMPLETION_FOOTER_RE)
    await expectLatestAssistantText(page, /\.docx|Download/i)
  })

  test('C2: pptx — presentation output turn completes', async ({ page }) => {
    await openTaskOptions(page)
    // Select Quality before Output — pptx adds a template select that shifts focus
    await page.getByLabel('Quality').selectOption('draft')
    await page.getByLabel('Output').selectOption('pptx')

    await runPromptAndWaitForCompletion(
      page,
      'Regression C2: Create a 3-slide deck on AI trends in healthcare 2025: one slide each on diagnosis, drug discovery, and administrative automation.',
    )
    await expectLatestAssistantText(page, COMPLETION_FOOTER_RE)
    await expectLatestAssistantText(page, /\.pptx|Download/i)
  })

  // ────────────────────────────────────────────────────────────────────────────
  // Block D — conversation continuity & context
  // ────────────────────────────────────────────────────────────────────────────

  test('D1: multi-turn — follow-up preserves topic from first turn', async ({ page }) => {
    await runPromptAndWaitForCompletion(
      page,
      'Regression D1: Give a concise answer about managing nighttime blood sugar for type 2 diabetes and end by offering to provide meal-planning details.',
      { turnTimeout: FAST_TURN_TIMEOUT },
    )
    await runPromptAndWaitForCompletion(page, 'yes', { turnTimeout: FAST_TURN_TIMEOUT })
    await expectLatestAssistantText(page, /meal|planning|blood sugar/i)
  })

  test('D2: L1 context — referential follow-up uses prior turn content', async ({ page }) => {
    await runPromptAndWaitForCompletion(
      page,
      'Regression D2: Explain three key differences between REST and GraphQL APIs.',
      { turnTimeout: FAST_TURN_TIMEOUT },
    )
    await runPromptAndWaitForCompletion(
      page,
      'Which of those differences matters most for mobile apps?',
      { turnTimeout: FAST_TURN_TIMEOUT },
    )
    // The follow-up should reference API / mobile context — verifies L1 context was used
    await expectLatestAssistantText(page, /mobile|REST|GraphQL|bandwidth|query/i)
  })

  test('D3: grounding canary — fresh conversation completes without model error', async ({ page }) => {
    // Verify a standalone turn on a fresh page completes normally.
    // We can't assert exact phrasing across model versions — just confirm the turn ends cleanly.
    await runPromptAndWaitForCompletion(
      page,
      'Regression D3: What did I ask you about in our last conversation? (This is a brand new conversation.)',
      { turnTimeout: FAST_TURN_TIMEOUT },
    )
    await expectLatestAssistantText(page, COMPLETION_FOOTER_RE)
  })

  test('D4: stop mid-turn — stop button cancels an in-flight turn', async ({ page }) => {
    await page.getByPlaceholder('Give Fronei a task...').fill(
      'Regression D4: Research the entire history of the Roman Empire from founding to fall in exhaustive detail.',
    )
    await page.getByRole('button', { name: 'Start', exact: true }).click()

    // Wait until Working state
    await expect(
      page.locator('span').filter({ hasText: /^Working$/ }).first(),
    ).toBeVisible({ timeout: 30_000 })

    await page.getByRole('button', { name: 'Stop' }).click()

    // After stop, Start button should return
    await expect(
      page.getByRole('button', { name: 'Start', exact: true }),
    ).toBeVisible({ timeout: 15_000 })
  })

  // ────────────────────────────────────────────────────────────────────────────
  // Block E — UI features & modals
  // ────────────────────────────────────────────────────────────────────────────

  test('E1: library panel — opens and shows workspace controls', async ({ page }) => {
    await ensureLibraryOpen(page)
    await expect(page.getByRole('button', { name: 'Create workspace' }).first()).toBeVisible({ timeout: 5_000 })
  })

  test('E2: facts modal — opens, shows panel, closes with Escape', async ({ page }) => {
    await page.getByRole('button', { name: 'Pinned facts' }).click()
    await expect(page.getByText('Pinned facts').last()).toBeVisible({ timeout: 5_000 })
    await page.keyboard.press('Escape')
    await expect(page.getByText('Pinned facts').last()).not.toBeVisible({ timeout: 5_000 })
  })

  test('E3: facts CRUD — add a fact, verify it appears, then delete it', async ({ page }) => {
    // entity_type must be 'workspace' — GET /api/facts defaults to entity_type=workspace
    const testEntity = `e2e-test-${Date.now()}`
    const testKey = 'regression-key'
    const testValue = 'regression-value'

    await page.getByRole('button', { name: 'Pinned facts' }).click()
    await expect(page.getByText('Pinned facts').last()).toBeVisible({ timeout: 5_000 })

    await page.getByRole('button', { name: 'Add' }).click()
    await page.getByPlaceholder('Entity, e.g. project').fill(testEntity)
    await page.getByPlaceholder('Type, e.g. workspace').fill('workspace')
    await page.getByPlaceholder('Key, e.g. stack').fill(testKey)
    await page.getByPlaceholder('Value, e.g. Next.js + FastAPI').fill(testValue)

    // Intercept PUT to verify it lands successfully
    const [putResponse] = await Promise.all([
      page.waitForResponse(
        resp => resp.url().includes('/api/facts') && resp.request().method() === 'PUT',
        { timeout: 15_000 },
      ),
      page.getByRole('button', { name: 'Save' }).click(),
    ])
    expect(putResponse.status(), `PUT /api/facts returned ${putResponse.status()} — check auth or validation`).toBe(200)

    // Fact should now appear in the refreshed list (scroll into view in case modal overflows)
    const factRow = page.getByText(`${testEntity} / ${testKey}`).last()
    await factRow.scrollIntoViewIfNeeded()
    await expect(factRow).toBeVisible({ timeout: 10_000 })

    await page.getByRole('button', { name: `Delete ${testEntity} ${testKey}` }).click()
    await expect(page.getByText(`${testEntity} / ${testKey}`).last()).not.toBeVisible({ timeout: 10_000 })
  })

  test('E4: facts inline edit — updates a fact value', async ({ page }) => {
    // entity_type must be 'workspace' — GET /api/facts defaults to entity_type=workspace
    const testEntity = `e2e-edit-${Date.now()}`
    const testKey = 'edit-key'

    await page.getByRole('button', { name: 'Pinned facts' }).click()
    await expect(page.getByText('Pinned facts').last()).toBeVisible({ timeout: 5_000 })

    // Create — intercept PUT to verify it lands
    await page.getByRole('button', { name: 'Add' }).click()
    await page.getByPlaceholder('Entity, e.g. project').fill(testEntity)
    await page.getByPlaceholder('Type, e.g. workspace').fill('workspace')
    await page.getByPlaceholder('Key, e.g. stack').fill(testKey)
    await page.getByPlaceholder('Value, e.g. Next.js + FastAPI').fill('original-value')

    const [createResponse] = await Promise.all([
      page.waitForResponse(
        resp => resp.url().includes('/api/facts') && resp.request().method() === 'PUT',
        { timeout: 15_000 },
      ),
      page.getByRole('button', { name: 'Save' }).click(),
    ])
    expect(createResponse.status(), `PUT /api/facts returned ${createResponse.status()}`).toBe(200)

    const factRow = page.getByText(`${testEntity} / ${testKey}`).last()
    await factRow.scrollIntoViewIfNeeded()
    await expect(factRow).toBeVisible({ timeout: 10_000 })

    // Edit — fill() replaces the value, no need to clear() first
    // (clear() would change the value to '' and break the [value=...] selector)
    await page.getByRole('button', { name: 'Edit fact' }).last().click()
    await page.locator(`input[value="original-value"]`).last().fill('updated-value')

    const [editResponse] = await Promise.all([
      page.waitForResponse(
        resp => resp.url().includes('/api/facts') && resp.request().method() === 'PUT',
        { timeout: 15_000 },
      ),
      page.getByRole('button', { name: 'Save fact' }).click(),
    ])
    expect(editResponse.status(), `PUT /api/facts returned ${editResponse.status()} on edit`).toBe(200)
    await expect(page.getByText('updated-value').last()).toBeVisible({ timeout: 10_000 })

    // Cleanup
    await page.getByRole('button', { name: `Delete ${testEntity} ${testKey}` }).click()
  })

  test('E5: quick preferences — popover opens and closes', async ({ page }) => {
    await page.getByRole('button', { name: 'Quick preferences' }).click()
    await expect(
      page.getByText(/preference|theme|model|tone/i).last(),
    ).toBeVisible({ timeout: 5_000 })
    await page.getByRole('button', { name: 'Close' }).click()
  })

  test('E6: current work modal — opens', async ({ page }) => {
    await page.getByRole('button', { name: 'Current work' }).click()
    await expect(
      page.getByText(/work|task|no (active|current)|nothing/i).last(),
    ).toBeVisible({ timeout: 5_000 })
    await page.keyboard.press('Escape')
  })

  test('E7: theme toggle — dark/light mode switches', async ({ page }) => {
    const toggleBtn = page.getByRole('button', { name: /Switch to (dark|light) theme/ })
    await expect(toggleBtn).toBeVisible()
    const labelBefore = await toggleBtn.getAttribute('aria-label')
    await toggleBtn.click()
    const labelAfter = await toggleBtn.getAttribute('aria-label')
    expect(labelAfter).not.toEqual(labelBefore)
    await toggleBtn.click() // restore
  })

  test('E8: workspace search — opens search input', async ({ page }) => {
    await ensureLibraryOpen(page)
    await page.getByRole('button', { name: 'Search workspaces' }).click()
    await expect(page.getByPlaceholder('Search workspaces...')).toBeVisible({ timeout: 5_000 })
  })

  test('E9: new conversation — composer resets after creating a conversation', async ({ page }) => {
    await ensureLibraryOpen(page)
    const newConvBtn = page.getByRole('button', { name: 'New conversation' }).first()
    await expect(newConvBtn).toBeVisible({ timeout: 5_000 })
    await newConvBtn.click()
    await expect(page.getByPlaceholder('Give Fronei a task...')).toBeVisible({ timeout: 10_000 })
    await expect(
      page.getByRole('button', { name: 'Start', exact: true }),
    ).toBeDisabled()
  })

  // ────────────────────────────────────────────────────────────────────────────
  // Block F — quality & research level options
  // ────────────────────────────────────────────────────────────────────────────

  test('F1: draft quality — fast turn with draft quality mode completes', async ({ page }) => {
    await openTaskOptions(page)
    await page.getByLabel('Quality').selectOption('draft')

    await runPromptAndWaitForCompletion(
      page,
      'Regression F1: Summarize the key benefits of containerization with Docker in three bullet points.',
      { turnTimeout: FAST_TURN_TIMEOUT },
    )
    await expectLatestAssistantText(page, /Completed as direct/i)
  })

  test('F2: auto route — factual question completes without budget approval', async ({ page }) => {
    // Let the router decide — a static factual question should route as direct,
    // not trigger research budget approval regardless of research level setting.
    await runPromptAndWaitForCompletion(
      page,
      'Regression F2: What year was the Python programming language first released?',
      { turnTimeout: FAST_TURN_TIMEOUT },
    )
    await expectLatestAssistantText(page, COMPLETION_FOOTER_RE)
    await expect(page.getByText('Research paused — budget approval needed')).not.toBeVisible()
    await expectLatestAssistantText(page, /Completed as direct/i)
  })

  // ────────────────────────────────────────────────────────────────────────────
  // Block H — Context OS integration
  // ────────────────────────────────────────────────────────────────────────────

  test('H1: workspace recall — second turn cites sentinel from first turn', async ({ page }) => {
    const sentinel = `quarzite-${Date.now()}`
    await runPromptAndWaitForCompletion(
      page,
      `Regression H1: The magic project codename is "${sentinel}". Please confirm you received it.`,
      { turnTimeout: FAST_TURN_TIMEOUT },
    )
    await expectLatestAssistantText(page, new RegExp(sentinel, 'i'))

    // This turn may route through research (context recall) so give it the full budget
    await runPromptAndWaitForCompletion(
      page,
      'What was the magic project codename I just mentioned?',
      { turnTimeout: TURN_TIMEOUT },
    )
    await expectLatestAssistantText(page, new RegExp(sentinel, 'i'))
  })

  test('H2: pinned facts — add workspace fact and verify it appears in facts panel', async ({ page }) => {
    // entity_type must be 'workspace' — GET /api/facts defaults to entity_type=workspace
    // L3 context also queries entity_type=workspace facts
    const factEntity = `h2-test-${Date.now()}`

    await page.getByRole('button', { name: 'Pinned facts' }).click()
    await expect(page.getByText('Pinned facts').last()).toBeVisible({ timeout: 5_000 })
    await page.getByRole('button', { name: 'Add' }).click()
    await page.getByPlaceholder('Entity, e.g. project').fill(factEntity)
    await page.getByPlaceholder('Type, e.g. workspace').fill('workspace')
    await page.getByPlaceholder('Key, e.g. stack').fill('preferred-language')
    await page.getByPlaceholder('Value, e.g. Next.js + FastAPI').fill('Rust')
    const [h2PutResp] = await Promise.all([
      page.waitForResponse(
        resp => resp.url().includes('/api/facts') && resp.request().method() === 'PUT',
        { timeout: 15_000 },
      ),
      page.getByRole('button', { name: 'Save' }).click(),
    ])
    expect(h2PutResp.status(), `PUT /api/facts returned ${h2PutResp.status()}`).toBe(200)
    const h2Row = page.getByText(`${factEntity} / preferred-language`).last()
    await h2Row.scrollIntoViewIfNeeded()
    await expect(h2Row).toBeVisible({ timeout: 10_000 })
    await page.keyboard.press('Escape')

    // Run a workspace-recall query — L3 fires on same_workspace_recall intent
    await runPromptAndWaitForCompletion(
      page,
      `Regression H2: Based on my workspace facts, what is the preferred language for "${factEntity}"?`,
      { turnTimeout: FAST_TURN_TIMEOUT },
    )
    await expectLatestAssistantText(page, /Rust/i)

    // Cleanup
    await page.getByRole('button', { name: 'Pinned facts' }).click()
    await page.getByRole('button', { name: `Delete ${factEntity} preferred-language` }).click()
    await page.keyboard.press('Escape')
  })

  test('H3: research then follow-up — second turn recalls from prior turn', async ({ page }) => {
    await runPromptAndWaitForCompletion(
      page,
      'Regression H3: Research the main advantages of Rust over C++ for systems programming. Keep it concise.',
    )
    await expectLatestAssistantText(page, COMPLETION_FOOTER_RE)

    await runPromptAndWaitForCompletion(
      page,
      'What was the top advantage you just mentioned?',
      { turnTimeout: FAST_TURN_TIMEOUT },
    )
    // The follow-up should echo content from the research turn
    await expectLatestAssistantText(page, /Rust|memory|safety|C\+\+/i)
  })
})

// ──────────────────────────────────────────────────────────────────────────────
// Helpers
// ──────────────────────────────────────────────────────────────────────────────

async function openTaskOptions(page: Page) {
  await page.getByRole('button', { name: 'Task options' }).click()
}

async function ensureLibraryOpen(page: Page) {
  const libraryBtn = page.getByRole('button', { name: 'Open library' })
  if (await libraryBtn.isVisible({ timeout: 2_000 }).catch(() => false)) {
    await libraryBtn.click()
  }
  // Both "Create workspace" and "Collapse library" may be visible simultaneously;
  // use first() to avoid strict mode violation
  await expect(
    page.getByRole('button', { name: 'Create workspace' }).first(),
  ).toBeVisible({ timeout: 5_000 })
}

function latestAssistantTurn(page: Page): Locator {
  return page.getByTestId('assistant-turn').last()
}

async function expectLatestAssistantText(page: Page, text: RegExp | string, timeout = 30_000) {
  await expect(latestAssistantTurn(page).getByText(text).last()).toBeVisible({ timeout })
}

async function runPromptAndWaitForCompletion(
  page: Page,
  prompt: string,
  options: { turnTimeout?: number } = {},
) {
  const turnTimeout = options.turnTimeout ?? TURN_TIMEOUT

  const prevFooterCount = await page.getByText(COMPLETION_FOOTER_RE).count()
  await page.getByPlaceholder('Give Fronei a task...').fill(prompt)
  await page.getByRole('button', { name: 'Start', exact: true }).click()

  // Wait for the user message to appear before checking route/answer assertions.
  // The completion baseline is captured before clicking Start so a very fast
  // direct turn cannot finish before we know how many prior footers existed.
  await expect(page.getByText(prompt.slice(0, 80)).last()).toBeVisible({ timeout: 30_000 })

  // Wait until the turn leaves "waiting" (may enter working or complete directly)
  await expect.poll(async () => {
    const footerCount = await page.getByText(COMPLETION_FOOTER_RE).count()
    if (footerCount > prevFooterCount) return 'completed'
    if (await page.getByText('Research paused — budget approval needed').isVisible()) return 'paused'
    if (await hasTerminalTurnError(page)) return 'failed'
    if (await page.locator('span').filter({ hasText: /^Working$/ }).isVisible()) return 'working'
    return 'waiting'
  }, { timeout: 30_000 }).not.toBe('waiting')

  await throwIfTerminalTurnError(page)

  const outcome = await waitForCompletionOrPause(page, prevFooterCount, turnTimeout)

  if (outcome === 'paused') {
    await page.getByRole('button', { name: 'Approve and continue' }).click()
    await expect.poll(async () => {
      const count = await page.getByText(COMPLETION_FOOTER_RE).count()
      return count > prevFooterCount
    }, { timeout: turnTimeout, message: 'turn should complete after budget approval' }).toBe(true)
  }
}

async function waitForCompletionOrPause(page: Page, prevFooterCount: number, turnTimeout = TURN_TIMEOUT) {
  return expect.poll(async () => {
    const count = await page.getByText(COMPLETION_FOOTER_RE).count()
    if (count > prevFooterCount) return 'completed'
    if (await page.getByText('Research paused — budget approval needed').isVisible()) return 'paused'
    if (await hasTerminalTurnError(page)) return 'failed'
    return 'running'
  }, {
    timeout: turnTimeout,
    message: 'live turn should complete or pause for approval',
  }).not.toBe('running').then(async () => {
    await throwIfTerminalTurnError(page)
    if (await page.getByText('Research paused — budget approval needed').isVisible()) return 'paused' as const
    return 'completed' as const
  })
}

async function hasTerminalTurnError(page: Page): Promise<boolean> {
  const errorBanner = page.getByTestId('turn-error').last()
  if (await errorBanner.isVisible().catch(() => false)) return true
  return page.getByText(LIVE_CONNECTION_DROPPED_RE).isVisible().catch(() => false)
}

async function throwIfTerminalTurnError(page: Page): Promise<void> {
  const errorBanner = page.getByTestId('turn-error').last()
  if (await errorBanner.isVisible().catch(() => false)) {
    const message = (await errorBanner.innerText().catch(() => '')).trim()
    throw new Error(`Live turn failed: ${message || 'unknown turn error'}`)
  }
  const droppedMessage = page.getByText(LIVE_CONNECTION_DROPPED_RE).last()
  if (await droppedMessage.isVisible().catch(() => false)) {
    const message = (await droppedMessage.innerText().catch(() => '')).trim()
    throw new Error(`Live turn failed: ${message || 'live connection dropped'}`)
  }
}
