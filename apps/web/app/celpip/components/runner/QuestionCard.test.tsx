import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { RunnerQuestion } from '../../types'
import { QuestionCard } from './QuestionCard'

function mcQuestion(id: string, saved?: Record<string, any>): RunnerQuestion {
  return {
    question_id: id,
    skill: 'reading',
    task_key: 'reading_information',
    part: 3,
    label: 'Reading for Information',
    description: 'Match each statement to a paragraph.',
    prep_seconds: 0,
    response_seconds: 0,
    word_range: null,
    audio_replays: 0,
    allows_answer_change: true,
    stimulus: { paragraphs: [{ label: 'A', text: 'Paragraph A text.' }] },
    questions: [
      { index: 0, prompt: `Statement for ${id}`, options: { A: 'Alpha', B: 'Bravo' }, segment_index: 0 },
    ],
    assets: { audio: [], image: null },
    responses: saved ?? {},
  }
}

function writingQuestion(id: string, saved?: Record<string, any>): RunnerQuestion {
  return {
    ...mcQuestion(id, saved),
    skill: 'writing',
    task_key: 'writing_email',
    part: 1,
    label: 'Writing an Email',
    response_seconds: 1620,
    word_range: [150, 200],
    stimulus: { prompt: `Prompt for ${id}`, bullets: ['one', 'two'] },
    questions: [],
  }
}

function makeApi() {
  const posted: { path: string; body: any }[] = []
  return {
    posted,
    api: {
      authorizedFetch: vi.fn(),
      readErrorBody: vi.fn(),
      access: 'granted' as const,
      getJson: vi.fn(),
      postJson: vi.fn(async (path: string, body: any) => {
        posted.push({ path, body })
        return {}
      }),
    },
  }
}

describe('QuestionCard state is scoped to one question', () => {
  beforeEach(() => {
    vi.useRealTimers()
  })

  // This project's vitest config does not enable `globals`, so Testing
  // Library's automatic cleanup never registers. Without this, renders from
  // earlier tests stay in the document and queries match multiple elements.
  afterEach(() => {
    cleanup()
  })

  it('does not show a previous question’s selected answer on the next question', async () => {
    const { api } = makeApi()
    // Question 1 was answered "A" on the server; question 2 is unanswered.
    const first = mcQuestion('q1', { '0': { selected_option: 'A', response_text: '', has_audio: false, flagged: false } })
    const second = mcQuestion('q2')

    const { rerender } = render(
      <QuestionCard api={api as any} attemptId="a1" question={first} practiceMode="timed" onSaved={() => {}} />,
    )
    const optionA = () => screen.getByRole('button', { name: /Alpha/ })
    expect(optionA().className).toContain('border-neutral-900')

    rerender(
      <QuestionCard api={api as any} attemptId="a1" question={second} practiceMode="timed" onSaved={() => {}} />,
    )

    await waitFor(() => expect(screen.getByText(/Statement for q2/)).toBeTruthy())
    // A fresh question must render with nothing selected.
    expect(optionA().className).not.toContain('border-neutral-900')
  })

  it('never autosaves one writing task’s text against another task’s id', async () => {
    vi.useFakeTimers()
    const { api, posted } = makeApi()
    const task1 = writingQuestion('w1')
    const task2 = writingQuestion('w2')

    const { rerender } = render(
      <QuestionCard api={api as any} attemptId="a1" question={task1} practiceMode="timed" onSaved={() => {}} />,
    )

    const textarea = screen.getByPlaceholderText('Write your response here.') as HTMLTextAreaElement
    act(() => {
      textarea.focus()
      // Simulate the learner typing task 1's answer.
      Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value')!
        .set!.call(textarea, 'Dear manager, the lift has been broken for a week.')
      textarea.dispatchEvent(new Event('input', { bubbles: true }))
    })
    act(() => {
      vi.advanceTimersByTime(2000)
    })
    expect(posted.some(p => p.body.response_text?.startsWith('Dear manager'))).toBe(true)
    expect(posted.every(p => p.body.question_id === 'w1')).toBe(true)

    // Move to writing task 2, which has no saved response.
    rerender(
      <QuestionCard api={api as any} attemptId="a1" question={task2} practiceMode="timed" onSaved={() => {}} />,
    )
    act(() => {
      vi.advanceTimersByTime(5000)
    })

    // The regression: task 1's essay must never be posted against task 2.
    const leaked = posted.filter(
      p => p.body.question_id === 'w2' && String(p.body.response_text ?? '').startsWith('Dear manager'),
    )
    expect(leaked).toEqual([])

    const editor = screen.getByPlaceholderText('Write your response here.') as HTMLTextAreaElement
    expect(editor.value).toBe('')
    vi.useRealTimers()
  })

  it('restores each question’s own saved answer when navigating back', async () => {
    const { api } = makeApi()
    const first = mcQuestion('q1', { '0': { selected_option: 'B', response_text: '', has_audio: false, flagged: false } })
    const second = mcQuestion('q2')

    const { rerender } = render(
      <QuestionCard api={api as any} attemptId="a1" question={first} practiceMode="timed" onSaved={() => {}} />,
    )
    rerender(
      <QuestionCard api={api as any} attemptId="a1" question={second} practiceMode="timed" onSaved={() => {}} />,
    )
    rerender(
      <QuestionCard api={api as any} attemptId="a1" question={first} practiceMode="timed" onSaved={() => {}} />,
    )

    await waitFor(() => expect(screen.getByText(/Statement for q1/)).toBeTruthy())
    expect(screen.getByRole('button', { name: /Bravo/ }).className).toContain('border-neutral-900')
  })

  it('does not carry a simulation answer lock across questions', async () => {
    const { api } = makeApi()
    const locking = { ...mcQuestion('q1'), allows_answer_change: false }
    const next = { ...mcQuestion('q2'), allows_answer_change: false }

    const { rerender } = render(
      <QuestionCard api={api as any} attemptId="a1" question={locking} practiceMode="simulation" onSaved={() => {}} />,
    )
    act(() => {
      screen.getByRole('button', { name: /Alpha/ }).click()
    })
    await waitFor(() => expect(screen.getByText(/Locked/)).toBeTruthy())

    rerender(
      <QuestionCard api={api as any} attemptId="a1" question={next} practiceMode="simulation" onSaved={() => {}} />,
    )
    await waitFor(() => expect(screen.getByText(/Statement for q2/)).toBeTruthy())
    // A fresh question starts unlocked; the lock belongs to the answered one.
    expect(screen.queryByText(/Locked/)).toBeNull()
  })
})
