import { act, renderHook } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { useTurnRunner } from './useTurnRunner'

function response(body: unknown, init?: ResponseInit) {
  return new Response(typeof body === 'string' ? body : JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': typeof body === 'string' ? 'text/event-stream' : 'application/json' },
    ...init,
  })
}

describe('useTurnRunner', () => {
  it('completes a turn from the authenticated SSE stream', async () => {
    const appendTurn = vi.fn()
    const authorizedFetch = vi.fn()
      .mockResolvedValueOnce(response({ turn_id: 'turn_1', conversation_id: 'conv_1', status: 'running' }))
      .mockResolvedValueOnce(response([
        'id: event_1',
        'event: progress',
        'data: {"event_id":"event_1","stage":"planning","message":"Planning"}',
        '',
        'event: turn',
        'data: {"turn_id":"turn_1","status":"completed","turn":{"turn_id":"turn_1","answer":"Done","route":"direct","events":[{"event_id":"event_1","stage":"planning","message":"Planning"}],"sources":[],"artifacts":[]}}',
        '',
        '',
      ].join('\n')))

    const { result } = renderHook(() => useTurnRunner({
      authorizedFetch,
      isLoaded: true,
      isSignedIn: true,
      message: 'Do the task',
      setMessage: vi.fn(),
      qualityMode: 'standard',
      outputFormat: 'chat',
      researchLevel: 'auto',
      selectedTemplateId: '',
      selectedTemplateExists: false,
      attachedFile: null,
      clearAttachment: vi.fn(),
      isAdmin: false,
      modelOverride: '',
      ensureActiveConversation: async () => 'conv_1',
      appendTurn,
    }))

    await act(async () => {
      await result.current.run()
    })

    expect(result.current.result?.answer).toBe('Done')
    expect(result.current.events).toHaveLength(1)
    expect(appendTurn).toHaveBeenCalledOnce()
    expect(authorizedFetch).toHaveBeenNthCalledWith(2, '/turns/turn_1/stream', { headers: {} })
  })

  it('deduplicates replayed event IDs', async () => {
    const authorizedFetch = vi.fn()
      .mockResolvedValueOnce(response({ turn_id: 'turn_1', conversation_id: 'conv_1', status: 'running' }))
      .mockResolvedValueOnce(response([
        'id: event_1',
        'event: progress',
        'data: {"event_id":"event_1","stage":"planning","message":"Planning"}',
        '',
        'id: event_1',
        'event: progress',
        'data: {"event_id":"event_1","stage":"planning","message":"Planning"}',
        '',
        'event: turn',
        'data: {"turn_id":"turn_1","status":"completed","turn":{"turn_id":"turn_1","answer":"Done","route":"direct","sources":[],"artifacts":[]}}',
        '',
        '',
      ].join('\n')))
    const { result } = renderHook(() => useTurnRunner(baseOptions(authorizedFetch)))

    await act(async () => {
      await result.current.run()
    })

    expect(result.current.events).toHaveLength(1)
    expect(result.current.events[0].event_id).toBe('event_1')
  })

  it('falls back to polling after repeated stream failures', async () => {
    vi.useFakeTimers()
    try {
      const authorizedFetch = vi.fn()
        .mockResolvedValueOnce(response({ turn_id: 'turn_1', conversation_id: 'conv_1', status: 'running' }))
        .mockRejectedValueOnce(new Error('stream unavailable'))
        .mockRejectedValueOnce(new Error('stream unavailable'))
        .mockRejectedValueOnce(new Error('stream unavailable'))
        .mockResolvedValueOnce(response({
          turn_id: 'turn_1',
          status: 'completed',
          turn: {
            turn_id: 'turn_1',
            answer: 'Recovered',
            route: 'direct',
            events: [],
            sources: [],
            artifacts: [],
          },
        }))
      const { result } = renderHook(() => useTurnRunner(baseOptions(authorizedFetch)))

      await act(async () => {
        const runPromise = result.current.run()
        await vi.runAllTimersAsync()
        await runPromise
      })

      expect(result.current.result?.answer).toBe('Recovered')
      expect(authorizedFetch).toHaveBeenCalledTimes(5)
    } finally {
      vi.useRealTimers()
    }
  })
})

function baseOptions(authorizedFetch: (path: string, init?: RequestInit) => Promise<Response>) {
  return {
    authorizedFetch,
    isLoaded: true,
    isSignedIn: true,
    message: 'Do the task',
    setMessage: vi.fn(),
    qualityMode: 'standard' as const,
    outputFormat: 'chat' as const,
    researchLevel: 'auto' as const,
    selectedTemplateId: '',
    selectedTemplateExists: false,
    attachedFile: null,
    clearAttachment: vi.fn(),
    isAdmin: false,
    modelOverride: '',
    ensureActiveConversation: async () => 'conv_1',
    appendTurn: vi.fn(),
  }
}
