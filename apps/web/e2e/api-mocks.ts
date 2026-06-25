import type { Page, Route } from '@playwright/test'

const now = '2026-06-22T12:00:00.000Z'

const workspace = {
  id: 'ws_e2e',
  name: 'E2E Workspace',
  created_at: now,
  updated_at: now,
  conversations: [
    {
      id: 'conv_e2e',
      workspace_id: 'ws_e2e',
      title: 'Launch plan',
      created_at: now,
      updated_at: now,
      turn_count: 0,
      artifact_count: 0,
      source_count: 0,
      total_latency_ms: 0,
      total_cost_usd: 0,
    },
  ],
}

const completedTurn = {
  turn_id: 'turn_e2e',
  answer: 'E2E answer from mocked API.',
  route: 'direct_answer',
  model_used: 'e2e-model',
  latency_ms: 42,
  sources: [],
  artifacts: [],
  events: [
    {
      stage: 'complete',
      message: 'Mocked run completed.',
      created_at: now,
    },
  ],
  created_at: now,
}

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({
    status,
    contentType: 'application/json',
    body: JSON.stringify(body),
  })
}

export async function mockFroneiApi(page: Page) {
  await page.route('http://127.0.0.1:8000/**', async route => {
    const request = route.request()
    const url = new URL(request.url())
    const path = url.pathname
    const method = request.method()

    if (method === 'GET' && path === '/workspaces') {
      return json(route, { workspaces: [workspace] })
    }

    if (method === 'GET' && path === '/conversations/conv_e2e/turns') {
      return json(route, { turns: [] })
    }

    if (method === 'GET' && path === '/documents/templates') {
      return json(route, {
        templates: [
          {
            id: 'tpl_default',
            name: 'Default presentation',
            recommended: true,
            user_template: false,
            design_mode: 'default',
            design_system: 'agentdeck_v1',
          },
        ],
      })
    }

    if (method === 'GET' && path === '/documents/supported') {
      return json(route, { types: ['.pdf', '.docx', '.pptx', '.csv', '.txt'] })
    }

    if (method === 'GET' && path === '/admin/me') {
      return json(route, { is_admin: true, user_id: 'user_e2e', email: 'e2e@fronei.test' })
    }

    if (method === 'GET' && path === '/admin/overview') {
      return json(route, {
        users: 1,
        requests_today: 3,
        spend_today: 0.12,
        errors_today: 0,
        running_research_runs: 0,
        total_conversations: 1,
        total_memories: 0,
        total_writing_samples: 0,
        total_research_runs: 0,
      })
    }

    if (method === 'GET' && path === '/admin/users') {
      return json(route, { items: [], total: 0, limit: 200, offset: 0 })
    }

    if (method === 'GET' && path === '/admin/model-policy') {
      return json(route, {
        roles: { orchestrator: 'gpt-4.1-mini' },
        fallback_models: ['gpt-4.1-mini'],
        defaults: { roles: { orchestrator: 'gpt-4.1-mini' }, fallback_models: ['gpt-4.1-mini'] },
        available_roles: ['orchestrator'],
      })
    }

    if (method === 'GET' && path === '/admin/usage') {
      return json(route, {
        range: url.searchParams.get('range') || '7d',
        summary: { total_cost: 0.12, requests: 3, tokens: 1200, users: 1 },
        cost_by_day: [],
        top_users: [],
        model_usage: [],
        task_distribution: [],
      })
    }

    if (method === 'GET' && path === '/admin/system') {
      return json(route, {
        app_env: 'test',
        database: 'sqlite',
        allowed_origins: ['http://127.0.0.1:3100'],
        default_profile: 'balanced',
        monthly_budget_usd: 10,
        planner_model: 'gpt-4.1-mini',
        planner_fallback_models: ['gpt-4.1-mini'],
        clerk_issuer_configured: true,
        clerk_audience_configured: false,
        admin_user_ids_configured: 1,
        admin_emails_configured: 1,
        sentry_configured: false,
        structured_logging: false,
        worker: { configured_concurrency: 2, live_threads: 2 },
        artifact_storage_backend: 'local',
        artifact_s3_bucket_configured: false,
      })
    }

    if (method === 'GET' && path === '/admin/providers') {
      return json(route, {
        providers: [],
        recent_error_counts: {},
      })
    }

    if (method === 'POST' && path === '/turns') {
      return json(route, {
        turn_id: 'turn_e2e',
        conversation_id: 'conv_e2e',
        status: 'running',
      })
    }

    if (method === 'GET' && path === '/turns/turn_e2e/status') {
      return json(route, {
        turn_id: 'turn_e2e',
        status: 'completed',
        error_message: null,
        turn: completedTurn,
      })
    }

    if (method === 'GET' && path === '/turns/turn_e2e/stream') {
      return route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        body: [
          'id: event_e2e',
          'event: progress',
          `data: ${JSON.stringify(completedTurn.events[0])}`,
          '',
          'id: terminal:turn_e2e:completed',
          'event: turn',
          `data: ${JSON.stringify({
            turn_id: 'turn_e2e',
            status: 'completed',
            error_message: null,
            turn: completedTurn,
          })}`,
          '',
          '',
        ].join('\n'),
      })
    }

    return json(route, { detail: `Unhandled E2E API mock: ${method} ${path}` }, 404)
  })
}
