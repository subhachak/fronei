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

function streamingResponse(chunks: Array<{ at: number; text: string }>) {
  const encoder = new TextEncoder()
  return new Response(new ReadableStream({
    start(controller) {
      for (const chunk of chunks) {
        setTimeout(() => {
          controller.enqueue(encoder.encode(chunk.text))
          if (chunk === chunks[chunks.length - 1]) controller.close()
        }, chunk.at)
      }
    },
  }), {
    status: 200,
    headers: { 'Content-Type': 'text/event-stream' },
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

  it('marks a freshly generated research plan as eligible for timed auto-start', async () => {
    const appendTurn = vi.fn()
    const authorizedFetch = vi.fn()
      .mockResolvedValueOnce(response({ turn_id: 'turn_plan', conversation_id: 'conv_1', status: 'running' }))
      .mockResolvedValueOnce(response([
        'event: turn',
        'data: {"turn_id":"turn_plan","status":"completed","turn":{"turn_id":"turn_plan","answer":"Plan ready","route":"clarify","research_plan_preview":{"title":"Deep research"},"follow_up_options":[{"label":"Start research","confirm_deep_research":true}],"sources":[],"artifacts":[]}}',
        '',
        '',
      ].join('\n')))
    const { result } = renderHook(() => useTurnRunner({
      ...baseOptions(authorizedFetch),
      appendTurn,
    }))

    await act(async () => {
      await result.current.run()
    })

    expect(appendTurn).toHaveBeenCalledWith(expect.objectContaining({
      id: 'turn_plan',
      autoStartResearchPlan: true,
    }), 'conv_1')
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

  it('stops streaming on a paused turn without appending a completed conversation item', async () => {
    const appendTurn = vi.fn()
    const authorizedFetch = vi.fn()
      .mockResolvedValueOnce(response({ turn_id: 'turn_pause', conversation_id: 'conv_1', status: 'running' }))
      .mockResolvedValueOnce(response([
        'event: turn',
        'data: {"turn_id":"turn_pause","status":"paused","turn":{"turn_id":"turn_pause","answer":"","route":"research","turn_status":"paused","langgraph_run_id":"lgrun_1","pause_reason":"Budget approval is required.","sources":[],"artifacts":[],"events":[]}}',
        '',
        '',
      ].join('\n')))
    const { result } = renderHook(() => useTurnRunner({ ...baseOptions(authorizedFetch), appendTurn }))

    await act(async () => {
      await result.current.run()
    })

    expect(result.current.running).toBe(false)
    expect(result.current.result?.turn_status).toBe('paused')
    expect(result.current.result?.langgraph_run_id).toBe('lgrun_1')
    expect(result.current.result?.pause_reason).toBe('Budget approval is required.')
    expect(appendTurn).not.toHaveBeenCalled()
  })

  it('cancels the active running turn and handles the cancelled terminal event', async () => {
    vi.useFakeTimers()
    try {
      const authorizedFetch = vi.fn()
        .mockResolvedValueOnce(response({ turn_id: 'turn_cancel', conversation_id: 'conv_1', status: 'running' }))
        .mockResolvedValueOnce(streamingResponse([
          {
            at: 500,
            text: [
              'event: turn',
              'data: {"turn_id":"turn_cancel","status":"cancelled","turn":{"turn_id":"turn_cancel","answer":"","route":"research","sources":[],"artifacts":[]}}',
              '',
              '',
            ].join('\n'),
          },
        ]))
        .mockResolvedValueOnce(response({ status: 'cancellation_requested' }))
      const { result } = renderHook(() => useTurnRunner(baseOptions(authorizedFetch)))

      let runPromise: Promise<void>
      await act(async () => {
        runPromise = result.current.run()
        await Promise.resolve()
      })

      await act(async () => {
        await result.current.cancel()
      })

      expect(authorizedFetch).toHaveBeenNthCalledWith(3, '/turns/turn_cancel/cancel', { method: 'POST' })

      await act(async () => {
        await vi.advanceTimersByTimeAsync(700)
        await runPromise
      })

      expect(result.current.running).toBe(false)
      expect(result.current.error).toBe('This turn was cancelled.')
    } finally {
      vi.useRealTimers()
    }
  })

  it('does not surface an error when cancel races with an already-finished turn', async () => {
    vi.useFakeTimers()
    try {
      const authorizedFetch = vi.fn()
        .mockResolvedValueOnce(response({ turn_id: 'turn_done', conversation_id: 'conv_1', status: 'running' }))
        .mockResolvedValueOnce(streamingResponse([
          {
            at: 500,
            text: [
              'event: turn',
              'data: {"turn_id":"turn_done","status":"completed","turn":{"turn_id":"turn_done","answer":"Done","route":"direct","sources":[],"artifacts":[]}}',
              '',
              '',
            ].join('\n'),
          },
        ]))
        .mockResolvedValueOnce(response({ detail: 'already finished' }, { status: 409 }))
      const { result } = renderHook(() => useTurnRunner(baseOptions(authorizedFetch)))

      let runPromise: Promise<void>
      await act(async () => {
        runPromise = result.current.run()
        await Promise.resolve()
      })

      await act(async () => {
        await result.current.cancel()
      })

      expect(result.current.error).toBeNull()

      await act(async () => {
        await vi.advanceTimersByTimeAsync(700)
        await runPromise
      })

      expect(result.current.result?.answer).toBe('Done')
    } finally {
      vi.useRealTimers()
    }
  })

  it('smoothly drains answer deltas before the final turn arrives', async () => {
    vi.useFakeTimers()
    try {
      const appendTurn = vi.fn()
      const authorizedFetch = vi.fn()
        .mockResolvedValueOnce(response({ turn_id: 'turn_1', conversation_id: 'conv_1', status: 'running' }))
        .mockResolvedValueOnce(streamingResponse([
          {
            at: 0,
            text: [
              'id: answer_1',
              'event: progress',
              'data: {"event_id":"answer_1","stage":"answer_delta","message":"Streaming","data":{"delta":"Smooth streamed answer text."}}',
              '',
              '',
            ].join('\n'),
          },
          {
            at: 1200,
            text: [
              'event: turn',
              'data: {"turn_id":"turn_1","status":"completed","turn":{"turn_id":"turn_1","answer":"Smooth streamed answer text.","route":"direct","sources":[],"artifacts":[]}}',
              '',
              '',
            ].join('\n'),
          },
        ]))
      const { result } = renderHook(() => useTurnRunner({ ...baseOptions(authorizedFetch), appendTurn }))

      let runPromise: Promise<void>
      await act(async () => {
        runPromise = result.current.run()
        await vi.advanceTimersByTimeAsync(300)
      })

      expect(result.current.liveAnswer.length).toBeGreaterThan(0)
      expect(result.current.liveAnswer.length).toBeLessThan('Smooth streamed answer text.'.length)
      expect(result.current.events).toHaveLength(0)

      await act(async () => {
        await vi.advanceTimersByTimeAsync(1500)
        await runPromise
      })

      expect(result.current.result?.answer).toBe('Smooth streamed answer text.')
      expect(appendTurn).toHaveBeenCalledOnce()
    } finally {
      vi.useRealTimers()
    }
  })

  it('stretches small answer bursts so the stream keeps moving between network chunks', async () => {
    vi.useFakeTimers()
    try {
      const firstBurst = 'This first burst should not disappear all at once. '
      const secondBurst = 'The later burst completes the answer.'
      const authorizedFetch = vi.fn()
        .mockResolvedValueOnce(response({ turn_id: 'turn_1', conversation_id: 'conv_1', status: 'running' }))
        .mockResolvedValueOnce(streamingResponse([
          {
            at: 0,
            text: [
              'id: answer_1',
              'event: progress',
              `data: ${JSON.stringify({ event_id: 'answer_1', stage: 'answer_delta', message: 'Streaming', data: { delta: firstBurst } })}`,
              '',
              '',
            ].join('\n'),
          },
          {
            at: 1500,
            text: [
              'id: answer_2',
              'event: progress',
              `data: ${JSON.stringify({ event_id: 'answer_2', stage: 'answer_delta', message: 'Streaming', data: { delta: secondBurst } })}`,
              '',
              '',
            ].join('\n'),
          },
          {
            at: 2200,
            text: [
              'event: turn',
              `data: ${JSON.stringify({ turn_id: 'turn_1', status: 'completed', turn: { turn_id: 'turn_1', answer: firstBurst + secondBurst, route: 'direct', sources: [], artifacts: [] } })}`,
              '',
              '',
            ].join('\n'),
          },
        ]))
      const { result } = renderHook(() => useTurnRunner(baseOptions(authorizedFetch)))

      let runPromise: Promise<void>
      await act(async () => {
        runPromise = result.current.run()
        await vi.advanceTimersByTimeAsync(650)
      })

      const midstreamLength = result.current.liveAnswer.length
      expect(midstreamLength).toBeGreaterThan(0)
      expect(midstreamLength).toBeLessThan(firstBurst.length)

      await act(async () => {
        await vi.advanceTimersByTimeAsync(500)
      })

      expect(result.current.liveAnswer.length).toBeGreaterThan(midstreamLength)
      expect(result.current.liveAnswer.length).toBeLessThan(firstBurst.length)

      await act(async () => {
        await vi.advanceTimersByTimeAsync(1400)
        await runPromise
      })

      expect(result.current.result?.answer).toBe(firstBurst + secondBurst)
    } finally {
      vi.useRealTimers()
    }
  })

  it('flushes buffered answer text when generation completes before the terminal turn event', async () => {
    vi.useFakeTimers()
    try {
      const fullAnswer = 'The server has finished this answer, so the client should stop smoothing.'
      const authorizedFetch = vi.fn()
        .mockResolvedValueOnce(response({ turn_id: 'turn_1', conversation_id: 'conv_1', status: 'running' }))
        .mockResolvedValueOnce(streamingResponse([
          {
            at: 0,
            text: [
              'id: answer_1',
              'event: progress',
              `data: ${JSON.stringify({ event_id: 'answer_1', stage: 'answer_delta', message: 'Streaming', data: { delta: fullAnswer } })}`,
              '',
              '',
            ].join('\n'),
          },
          {
            at: 120,
            text: [
              'id: answer_done',
              'event: progress',
              `data: ${JSON.stringify({ event_id: 'answer_done', stage: 'answer_complete', message: 'Answer stream complete.', data: { char_count: fullAnswer.length } })}`,
              '',
              '',
            ].join('\n'),
          },
          {
            at: 1800,
            text: [
              'event: turn',
              `data: ${JSON.stringify({ turn_id: 'turn_1', status: 'completed', turn: { turn_id: 'turn_1', answer: fullAnswer, route: 'direct', sources: [], artifacts: [] } })}`,
              '',
              '',
            ].join('\n'),
          },
        ]))
      const { result } = renderHook(() => useTurnRunner(baseOptions(authorizedFetch)))

      let runPromise: Promise<void>
      await act(async () => {
        runPromise = result.current.run()
        await vi.advanceTimersByTimeAsync(250)
      })

      expect(result.current.liveAnswer).toBe(fullAnswer)
      expect(result.current.result).toBeNull()

      await act(async () => {
        await vi.advanceTimersByTimeAsync(1800)
        await runPromise
      })

      expect(result.current.result?.answer).toBe(fullAnswer)
    } finally {
      vi.useRealTimers()
    }
  })

  it('clears the streamed draft when a repaired answer starts', async () => {
    vi.useFakeTimers()
    try {
      const draft = 'This draft answer is long enough to begin streaming before repair.'
      const repaired = 'This repaired answer replaces the draft.'
      const authorizedFetch = vi.fn()
        .mockResolvedValueOnce(response({ turn_id: 'turn_1', conversation_id: 'conv_1', status: 'running' }))
        .mockResolvedValueOnce(streamingResponse([
          {
            at: 0,
            text: [
              'id: answer_1',
              'event: progress',
              `data: ${JSON.stringify({ event_id: 'answer_1', stage: 'answer_delta', message: 'Streaming', data: { delta: draft, source_node: 'synthesize' } })}`,
              '',
              '',
            ].join('\n'),
          },
          {
            at: 700,
            text: [
              'id: answer_reset',
              'event: progress',
              `data: ${JSON.stringify({ event_id: 'answer_reset', stage: 'answer_reset', message: 'Revising the answer for accuracy.', data: { reason: 'repair', ephemeral_ui: true } })}`,
              '',
              '',
            ].join('\n'),
          },
          {
            at: 900,
            text: [
              'id: answer_2',
              'event: progress',
              `data: ${JSON.stringify({ event_id: 'answer_2', stage: 'answer_delta', message: 'Streaming', data: { delta: repaired, source_node: 'repair' } })}`,
              '',
              '',
            ].join('\n'),
          },
          {
            at: 1800,
            text: [
              'event: turn',
              `data: ${JSON.stringify({ turn_id: 'turn_1', status: 'completed', turn: { turn_id: 'turn_1', answer: repaired, route: 'research', sources: [], artifacts: [] } })}`,
              '',
              '',
            ].join('\n'),
          },
        ]))
      const { result } = renderHook(() => useTurnRunner(baseOptions(authorizedFetch)))

      let runPromise: Promise<void>
      await act(async () => {
        runPromise = result.current.run()
        await vi.advanceTimersByTimeAsync(500)
      })

      expect(result.current.liveAnswer.length).toBeGreaterThan(0)
      expect(draft).toContain(result.current.liveAnswer)

      await act(async () => {
        await vi.advanceTimersByTimeAsync(250)
      })

      expect(result.current.liveAnswer).toBe('')
      expect(result.current.events).toHaveLength(0)

      await act(async () => {
        await vi.advanceTimersByTimeAsync(350)
      })

      expect(result.current.liveAnswer.length).toBeGreaterThan(0)
      expect(repaired).toContain(result.current.liveAnswer)
      expect(result.current.liveAnswer).not.toContain(draft)

      await act(async () => {
        await vi.advanceTimersByTimeAsync(1000)
        await runPromise
      })

      expect(result.current.result?.answer).toBe(repaired)
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
