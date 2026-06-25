import { act, renderHook } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { useWorkspaces } from './useWorkspaces'

describe('useWorkspaces', () => {
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
})
