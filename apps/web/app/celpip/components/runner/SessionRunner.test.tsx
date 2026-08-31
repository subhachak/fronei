import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { SessionRunner } from './SessionRunner'

const ATTEMPT = {
  attempt_id: 'a1',
  test_id: 't1',
  label: 'Listening drill',
  mode: 'single_task',
  practice_mode: 'learn',
  status: 'in_progress',
  current_skill: 'listening',
  components: ['listening'],
  sections: {
    listening: {
      attempt_id: 'a1', skill: 'listening', status: 'in_progress',
      started_at: '2026-08-30T10:00:00Z', deadline_at: null, limit_seconds: null,
      seconds_remaining: null, expired: false, completed_at: null, auto_submitted: false,
    },
  },
  expired_sections: [],
  items: [{
    question_id: 'q_news', skill: 'listening', task_key: 'listening_news',
    position: 0, is_practice_task: false, answered: 0,
  }],
  flagged: [],
  started_at: '2026-08-30T10:00:00Z',
  submitted_at: null,
}

const QUESTION = {
  question_id: 'q_news',
  skill: 'listening',
  task_key: 'listening_news',
  part: 4,
  label: 'Listening to a News Item',
  description: 'A short broadcast news report.',
  prep_seconds: 0,
  response_seconds: 0,
  word_range: null,
  audio_replays: 0,
  allows_answer_change: true,
  stimulus: {},
  questions: [
    { index: 0, prompt: 'What did council vote to do?', options: { A: 'Extend a route', B: 'Close a road' }, segment_index: 0 },
  ],
  assets: { audio: [{ id: 'asset_1', segment_index: 0, speaker_voice: 'nova', duration_seconds: 40, status: 'ready' }], image: null },
  responses: {},
}

/** A fresh api object each call, mirroring `useCelpip` before it was memoised
 *  and, more importantly, any future hook that re-renders the shell. */
function makeApi(counts: { state: number; question: number }) {
  return {
    access: 'granted' as const,
    authorizedFetch: vi.fn(),
    readErrorBody: vi.fn(),
    getJson: vi.fn(async (path: string) => {
      if (path.includes('/questions/')) {
        counts.question += 1
        return QUESTION as any
      }
      counts.state += 1
      // A new object every time, as a real deserialised response is.
      return JSON.parse(JSON.stringify(ATTEMPT))
    }),
    postJson: vi.fn(async () => ({})),
  }
}

describe('SessionRunner keeps the current question mounted', () => {
  beforeEach(() => vi.useRealTimers())
  afterEach(() => cleanup())

  it('does not reload the question when the api object identity changes', async () => {
    const counts = { state: 0, question: 0 }
    const { rerender } = render(
      <SessionRunner
        api={makeApi(counts) as any}
        attemptId="a1"
        onExit={() => {}}
        onFinished={() => {}}
      />,
    )

    await waitFor(() => expect(screen.getByText(/Listening to a News Item/)).toBeTruthy())
    expect(counts.question).toBe(1)

    // Clerk refreshing its session token re-renders the shell, which used to
    // hand down a brand-new api object and blank the question — taking the
    // <audio> element with it.
    for (let i = 0; i < 3; i += 1) {
      rerender(
        <SessionRunner
          api={makeApi(counts) as any}
          attemptId="a1"
          onExit={() => {}}
          onFinished={() => {}}
        />,
      )
      await act(async () => { await Promise.resolve() })
    }

    expect(counts.question).toBe(1)
    // The audio player must still be on screen, not replaced by the loader.
    expect(screen.getByText(/Play audio/)).toBeTruthy()
    expect(screen.queryByText(/Loading question/)).toBeNull()
  })

  it('survives a server clock resync without dropping the question', async () => {
    vi.useFakeTimers()
    const counts = { state: 0, question: 0 }
    render(
      <SessionRunner
        api={makeApi(counts) as any}
        attemptId="a1"
        onExit={() => {}}
        onFinished={() => {}}
      />,
    )
    await act(async () => { await Promise.resolve() })
    await act(async () => { await Promise.resolve() })
    expect(counts.question).toBe(1)

    // Two full resync intervals.
    await act(async () => { vi.advanceTimersByTime(31_000) })
    await act(async () => { await Promise.resolve() })
    await act(async () => { vi.advanceTimersByTime(31_000) })
    await act(async () => { await Promise.resolve() })

    expect(counts.state).toBeGreaterThan(1)
    expect(counts.question).toBe(1)
    vi.useRealTimers()
  })
})
