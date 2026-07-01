import { act, renderHook } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { AgentResult } from '../types'
import { INITIAL_VISIBLE_TURNS, useWorkspaces } from './useWorkspaces'

describe('useWorkspaces', () => {
  beforeEach(() => {
    const storage: Record<string, string> = {}
    Object.defineProperty(window, 'localStorage', {
      configurable: true,
      value: {
        getItem: vi.fn((key: string) => storage[key] ?? null),
        setItem: vi.fn((key: string, value: string) => {
          storage[key] = value
        }),
      },
    })
  })

  it('creates a workspace optimistically and replaces the draft with the saved row', async () => {
    const authorizedFetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      id: 'ws_saved',
      name: 'New workspace',
      created_at: '2026-06-24T00:00:00Z',
      updated_at: '2026-06-24T00:00:00Z',
      conversations: [],
    }), { status: 200 }))
    const { result } = renderHook(() => useWorkspaces({
      authorizedFetch,
      isRunning: () => false,
      setMessage: vi.fn(),
      onTurnState: vi.fn(),
      onResetTurn: vi.fn(),
      onError: vi.fn(),
    }))

    await act(async () => {
      await result.current.createWorkspace()
    })

    expect(result.current.workspaces).toHaveLength(1)
    expect(result.current.workspaces[0].id).toBe('ws_saved')
    expect(result.current.workspaces[0].isDraft).toBeUndefined()
    expect(result.current.activeWorkspace?.id).toBe('ws_saved')
  })

  it('loads older turns before the first loaded turn and prepends them', async () => {
    const authorizedFetch = vi.fn(async path => {
      if (path === '/workspaces') {
        return jsonResponse({
          workspaces: [{
            id: 'ws_1',
            name: 'Workspace',
            created_at: '2026-06-24T00:00:00Z',
            updated_at: '2026-06-24T00:00:00Z',
            conversations: [{
              id: 'conv_1',
              workspace_id: 'ws_1',
              title: 'Conversation',
              created_at: '2026-06-24T00:00:00Z',
              updated_at: '2026-06-24T00:00:00Z',
              turn_count: 8,
            }],
          }],
        })
      }
      if (path === `/conversations/conv_1/turns?limit=${INITIAL_VISIBLE_TURNS}`) {
        return jsonResponse({ turns: ['turn_3', 'turn_4', 'turn_5', 'turn_6', 'turn_7', 'turn_8'].map(apiTurn) })
      }
      if (path === `/conversations/conv_1/turns?limit=${INITIAL_VISIBLE_TURNS}&before=turn_3`) {
        return jsonResponse({ turns: ['turn_1', 'turn_2'].map(apiTurn) })
      }
      return new Response('not found', { status: 404 })
    })
    const { result } = renderHook(() => useWorkspaces({
      authorizedFetch,
      isRunning: () => false,
      setMessage: vi.fn(),
      onTurnState: vi.fn(),
      onResetTurn: vi.fn(),
      onError: vi.fn(),
    }))

    await act(async () => {
      await result.current.loadWorkspaces()
    })

    expect(result.current.visibleTurns.map(turn => turn.id)).toEqual([
      'turn_3',
      'turn_4',
      'turn_5',
      'turn_6',
      'turn_7',
      'turn_8',
    ])

    await act(async () => {
      await result.current.loadOlderTurns()
    })

    expect(authorizedFetch).toHaveBeenCalledWith(`/conversations/conv_1/turns?limit=${INITIAL_VISIBLE_TURNS}&before=turn_3`)
    expect(result.current.visibleTurns.map(turn => turn.id)).toEqual([
      'turn_1',
      'turn_2',
      'turn_3',
      'turn_4',
      'turn_5',
      'turn_6',
      'turn_7',
      'turn_8',
    ])
  })

  it('sorts workspaces and conversations by most recent update after loading', async () => {
    const authorizedFetch = vi.fn(async path => {
      if (path === '/workspaces') {
        return jsonResponse({
          workspaces: [
            {
              id: 'ws_old',
              name: 'Old workspace',
              created_at: '2026-06-20T00:00:00Z',
              updated_at: '2026-06-20T00:00:00Z',
              conversations: [],
            },
            {
              id: 'ws_recent',
              name: 'Recent workspace',
              created_at: '2026-06-21T00:00:00Z',
              updated_at: '2026-06-25T00:00:00Z',
              conversations: [
                {
                  id: 'conv_old',
                  workspace_id: 'ws_recent',
                  title: 'Older conversation',
                  created_at: '2026-06-22T00:00:00Z',
                  updated_at: '2026-06-22T00:00:00Z',
                  turn_count: 1,
                },
                {
                  id: 'conv_recent',
                  workspace_id: 'ws_recent',
                  title: 'Recent conversation',
                  created_at: '2026-06-23T00:00:00Z',
                  updated_at: '2026-06-26T00:00:00Z',
                  turn_count: 2,
                },
              ],
            },
          ],
        })
      }
      if (path === `/conversations/conv_recent/turns?limit=${INITIAL_VISIBLE_TURNS}`) {
        return jsonResponse({ turns: [] })
      }
      return new Response('not found', { status: 404 })
    })
    const { result } = renderHook(() => useWorkspaces({
      authorizedFetch,
      isRunning: () => false,
      setMessage: vi.fn(),
      onTurnState: vi.fn(),
      onResetTurn: vi.fn(),
      onError: vi.fn(),
    }))

    await act(async () => {
      await result.current.loadWorkspaces()
    })

    expect(result.current.workspaces.map(workspace => workspace.id)).toEqual(['ws_recent', 'ws_old'])
    expect(result.current.activeWorkspace?.id).toBe('ws_recent')
    expect(result.current.activeConversation?.id).toBe('conv_recent')
    expect(result.current.workspaces[0].conversations.map(conversation => conversation.id)).toEqual([
      'conv_recent',
      'conv_old',
    ])
  })
})

function jsonResponse(body: unknown) {
  return new Response(JSON.stringify(body), { status: 200 })
}

function apiTurn(id: string): AgentResult {
  return {
    turn_id: id,
    goal: { objective: id },
    answer: `Answer ${id}`,
    route: 'direct',
    sources: [],
    artifacts: [],
    events: [],
    created_at: '2026-06-24T00:00:00Z',
  }
}
