'use client'

import { ChevronLeft, ChevronRight, Loader2, LogOut, Send, X } from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { AttemptState, RunnerQuestion, Skill } from '../../types'
import type { useCelpip } from '../../hooks/useCelpip'
import { BUTTON, BUTTON_QUIET, ErrorNote } from '../ui'
import { QuestionCard } from './QuestionCard'
import { SectionTimer } from './SectionTimer'

type Api = ReturnType<typeof useCelpip>

// How often the local countdown is re-anchored to the server's clock. Frequent
// enough that a sleeping laptop or an edited system clock is corrected within
// half a minute, cheap enough to run through a three-hour sitting.
const RESYNC_MS = 30_000

export function SessionRunner({
  api,
  attemptId,
  onExit,
  onFinished,
}: {
  api: Api
  attemptId: string
  onExit: () => void
  onFinished: (attemptId: string) => void
}) {
  const [state, setState] = useState<AttemptState | null>(null)
  const [question, setQuestion] = useState<RunnerQuestion | null>(null)
  const [index, setIndex] = useState(0)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [confirmSubmit, setConfirmSubmit] = useState(false)
  const [instructionsFor, setInstructionsFor] = useState<Skill | null>(null)
  const startedSections = useRef<Set<string>>(new Set())

  const loadState = useCallback(async () => {
    try {
      const next = await api.getJson<AttemptState>(`/admin/celpip/attempts/${attemptId}`)
      setState(next)
      return next
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not load this attempt.')
      return null
    }
  }, [api, attemptId])

  useEffect(() => {
    void loadState()
  }, [loadState])

  // The resync loop. Everything about timing correctness rests on this plus the
  // server-side deadline check on every write.
  useEffect(() => {
    const id = window.setInterval(() => void loadState(), RESYNC_MS)
    return () => window.clearInterval(id)
  }, [loadState])

  const items = state?.items ?? []
  const currentItem = items[index]
  const currentQuestionId = currentItem?.question_id ?? null
  const currentSkill = currentItem?.skill ?? null

  // Opening a section stamps its deadline server-side. Idempotent there, so a
  // remount cannot restart the clock.
  useEffect(() => {
    if (!state || !currentSkill) return
    if (state.status === 'submitted' || state.status === 'completed') return
    if (startedSections.current.has(currentSkill)) return
    startedSections.current.add(currentSkill)
    if (state.practice_mode !== 'learn' && !state.sections[currentSkill]?.started_at) {
      setInstructionsFor(currentSkill)
      return
    }
    api
      .postJson(`/admin/celpip/attempts/${attemptId}/sections/${currentSkill}/start`)
      .then(() => void loadState())
      .catch(err => setError(err instanceof Error ? err.message : 'Could not open this section.'))
  }, [state, currentSkill, api, attemptId, loadState])

  // Which question the runner has actually loaded. The effect below is allowed
  // to re-run for any reason -- a clock resync, an autosave, a token refresh
  // changing a dependency's identity -- and this is what stops it acting on
  // those. Reloading blanks `question`, which unmounts QuestionCard and
  // destroys the <audio> element with it, so a spurious reload cuts listening
  // audio off mid-playback. Guarding on effect dependencies alone was not
  // enough: it only takes one unstable reference upstream.
  const loadedQuestionId = useRef<string | null>(null)

  const loadQuestion = useCallback(
    async (questionId: string, { force = false }: { force?: boolean } = {}) => {
      if (!force && loadedQuestionId.current === questionId) return
      loadedQuestionId.current = questionId
      setQuestion(null)
      try {
        setQuestion(
          await api.getJson<RunnerQuestion>(
            `/admin/celpip/attempts/${attemptId}/questions/${questionId}`,
          ),
        )
      } catch (err) {
        // Clear the marker so the next render retries rather than sitting on a
        // question that never arrived.
        loadedQuestionId.current = null
        setError(err instanceof Error ? err.message : 'Could not load this question.')
      }
    },
    [api, attemptId],
  )

  useEffect(() => {
    if (instructionsFor || !currentQuestionId) return
    void loadQuestion(currentQuestionId)
  }, [currentQuestionId, instructionsFor, loadQuestion])

  const beginSection = useCallback(
    async (skill: Skill) => {
      setInstructionsFor(null)
      try {
        await api.postJson(`/admin/celpip/attempts/${attemptId}/sections/${skill}/start`)
        await loadState()
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Could not start this section.')
      }
    },
    [api, attemptId, loadState],
  )

  const submit = useCallback(async () => {
    setBusy(true)
    try {
      await api.postJson(`/admin/celpip/attempts/${attemptId}/submit`)
      onFinished(attemptId)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not submit this attempt.')
      setBusy(false)
    }
  }, [api, attemptId, onFinished])

  const closeSection = useCallback(
    async (skill: Skill, auto: boolean) => {
      try {
        await api.postJson(
          `/admin/celpip/attempts/${attemptId}/sections/${skill}/complete?auto=${auto}`,
        )
      } catch {
        /* the server enforces the deadline regardless of what the client does */
      }
    },
    [api, attemptId],
  )

  // Indices, not `position`: `position` is the assembly order stored on the
  // test, and comparing it against an array index only happens to work while
  // the two are identical.
  const nextSectionIndex = useMemo(
    () => items.findIndex((item, i) => i > index && item.skill !== currentSkill),
    [items, index, currentSkill],
  )

  // Moving to the next section is an explicit step, not something that happens
  // by drifting into another section's item. The server only allows one open
  // section at a time and requires them in order, so the previous one has to be
  // closed before the next will start.
  const advanceSection = useCallback(
    async (auto: boolean) => {
      if (!currentSkill) return
      await closeSection(currentSkill, auto)
      if (nextSectionIndex < 0) {
        await submit()
        return
      }
      const nextSkill = items[nextSectionIndex].skill
      startedSections.current.delete(nextSkill)
      setIndex(nextSectionIndex)
      await loadState()
    },
    [closeSection, currentSkill, items, loadState, nextSectionIndex, submit],
  )

  const handleExpiry = useCallback(() => {
    void advanceSection(true)
  }, [advanceSection])

  const sectionState = currentSkill ? state?.sections[currentSkill] : undefined
  const answeredCount = useMemo(() => items.filter(i => i.answered > 0).length, [items])
  // Timed and simulation runs cannot move between sections freely -- the server
  // refuses answers to a section that is not open -- so the strip and the
  // arrows stay inside the current one. Learn mode keeps free navigation.
  const enforced = state ? state.practice_mode !== 'learn' : true
  const navigable = useMemo(
    () =>
      items
        .map((item, i) => ({ item, i }))
        .filter(({ item }) => !enforced || item.skill === currentSkill),
    [items, enforced, currentSkill],
  )
  const firstNavigable = navigable.length ? navigable[0].i : 0
  const lastNavigable = navigable.length ? navigable[navigable.length - 1].i : 0

  if (!state) {
    return (
      <div className="flex h-full items-center justify-center gap-2 text-sm text-neutral-400">
        {error ? <ErrorNote message={error} /> : (<><Loader2 size={16} className="animate-spin" /> Loading attempt…</>)}
      </div>
    )
  }

  if (state.status === 'submitted' || state.status === 'evaluating' || state.status === 'completed') {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-3 p-8 text-center">
        <p className="text-lg font-bold text-neutral-900 dark:text-neutral-50">Submitted</p>
        <p className="max-w-sm text-[13px] leading-relaxed text-neutral-500">
          Scoring runs in the background — Writing and Speaking each get two independent evaluations, so it
          takes a few minutes. You can close this and come back.
        </p>
        <button type="button" className={BUTTON} onClick={() => onFinished(attemptId)}>
          View results
        </button>
      </div>
    )
  }

  if (instructionsFor) {
    const section = state.sections[instructionsFor]
    const minutes = section?.limit_seconds ? Math.round(section.limit_seconds / 60) : null
    return (
      <div className="flex h-full items-center justify-center bg-white p-6 dark:bg-neutral-950">
        <div className="max-w-lg">
          <p className="text-[11px] font-bold uppercase tracking-wide text-neutral-400">Section</p>
          <h2 className="mt-1 text-2xl font-bold capitalize text-neutral-900 dark:text-neutral-50">
            {instructionsFor}
          </h2>
          <ul className="mt-4 space-y-2 text-[14px] leading-relaxed text-neutral-600 dark:text-neutral-300">
            {minutes && <li>• You have {minutes} minutes for this section.</li>}
            {instructionsFor === 'listening' && (
              <li>• Audio plays once. You cannot replay it or return to an answered question.</li>
            )}
            {instructionsFor === 'reading' && (
              <li>• You control the pace. Budget your time across all four parts before you start.</li>
            )}
            {instructionsFor === 'writing' && <li>• Aim for 150–200 words per task.</li>}
            {instructionsFor === 'speaking' && (
              <li>• Each task gives you preparation time, then records for a fixed window.</li>
            )}
            <li>• Your answers save to the server as you go. A refresh will not lose them, and the clock keeps running.</li>
            {state.practice_mode === 'simulation' && (
              <li>• Exam simulation: no feedback until you submit, and answers cannot be changed once given.</li>
            )}
          </ul>
          <div className="mt-6 flex items-center gap-2">
            <button type="button" className={BUTTON} onClick={() => void beginSection(instructionsFor)}>
              Start section — the timer begins now
            </button>
            <button type="button" className={BUTTON_QUIET} onClick={onExit}>
              Not yet
            </button>
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className="flex h-full flex-col overflow-hidden bg-white dark:bg-neutral-950">
      <header className="flex flex-shrink-0 items-center justify-between gap-3 border-b border-neutral-200 px-4 py-3 dark:border-neutral-800 sm:px-6">
        <div className="flex min-w-0 items-center gap-3">
          <button
            type="button"
            onClick={onExit}
            title={state.practice_mode === 'simulation' ? 'Leave — the clock keeps running' : 'Leave'}
            className="grid h-11 w-11 flex-shrink-0 place-items-center rounded-xl border border-neutral-200 text-neutral-500 hover:bg-neutral-50 dark:border-neutral-800 dark:text-neutral-400 dark:hover:bg-neutral-900"
          >
            <LogOut size={14} />
          </button>
          <div className="min-w-0">
            <p className="truncate text-sm font-bold text-neutral-900 dark:text-neutral-50">{state.label}</p>
            <p className="text-xs capitalize text-neutral-500">
              {state.practice_mode} · item {index + 1} of {items.length} · {answeredCount} answered
            </p>
          </div>
        </div>
        <div className="flex flex-shrink-0 items-center gap-2">
          <SectionTimer
            serverSeconds={sectionState?.seconds_remaining ?? null}
            onExpire={() => void handleExpiry()}
          />
          <button type="button" className={BUTTON_QUIET} onClick={() => setConfirmSubmit(true)}>
            <Send size={13} /> Submit
          </button>
        </div>
      </header>

      <div className="flex flex-shrink-0 items-center gap-1 border-b border-neutral-100 bg-neutral-50 px-4 py-2 dark:border-neutral-900 dark:bg-neutral-950 sm:px-6">
        {state.components.map((skill, skillIndex) => {
          const sectionItems = items.filter(item => item.skill === skill)
          const active = skill === currentSkill
          // Completion comes from the section's own completed_at, not from
          // where the learner happens to be standing: moving past a section is
          // not the same as finishing it, and with currentSkill null the
          // position comparison marks everything unfinished.
          const done = Boolean(state.sections[skill]?.completed_at)
          const answered = sectionItems.filter(item => item.answered > 0).length
          return (
            <div key={skill} className="flex flex-1 items-center gap-2">
              <div className={`h-1.5 flex-1 rounded-full ${done ? 'bg-emerald-500' : active ? 'bg-amber-400' : 'bg-neutral-200 dark:bg-neutral-800'}`} />
              <span className={`hidden text-[11px] font-bold capitalize lg:block ${active ? 'text-neutral-900 dark:text-white' : 'text-neutral-400'}`}>{skill} {answered}/{sectionItems.length}</span>
              {skillIndex < state.components.length - 1 && <span className="sr-only">then</span>}
            </div>
          )
        })}
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto p-4 sm:p-8">
        <div className={`mx-auto w-full ${question?.skill === 'writing' ? 'max-w-6xl' : 'max-w-3xl'}`}>
          {error && <div className="mb-4"><ErrorNote message={error} /></div>}
          {question ? (
            <QuestionCard
              // Remount per question: QuestionCard holds editable answer state,
              // and reusing one instance across questions would carry answers
              // (and a writing task's text) from one item to the next.
              key={question.question_id}
              api={api}
              attemptId={attemptId}
              question={question}
              practiceMode={state.practice_mode}
              onSaved={() => void loadState()}
            />
          ) : (
            <div className="flex items-center justify-center gap-2 py-24 text-sm text-neutral-400">
              <Loader2 size={16} className="animate-spin" /> Loading question…
            </div>
          )}
        </div>
      </div>

      <footer className="flex flex-shrink-0 items-center justify-between gap-3 border-t border-neutral-200 px-4 py-3 dark:border-neutral-800 sm:px-6">
        <button
          type="button"
          disabled={index <= firstNavigable}
          onClick={() => setIndex(i => Math.max(firstNavigable, i - 1))}
          className={BUTTON_QUIET}
        >
          <ChevronLeft size={14} /> Previous
        </button>
        <div className="flex max-w-[55vw] flex-wrap justify-center gap-1.5 overflow-y-auto">
          {navigable.map(({ item, i }) => (
            <button
              key={item.question_id}
              type="button"
              onClick={() => setIndex(i)}
              title={item.task_key}
              aria-label={`Question ${i + 1}${item.answered > 0 ? ', answered' : ''}`}
              className={`grid h-9 min-w-9 place-items-center rounded-lg border px-2 text-xs font-bold transition-colors ${
                i === index
                  ? 'border-neutral-900 bg-neutral-900 text-white dark:border-white dark:bg-white dark:text-neutral-900'
                  : item.answered > 0
                    ? 'border-emerald-300 bg-emerald-50 text-emerald-700 dark:border-emerald-900 dark:bg-emerald-950 dark:text-emerald-300'
                    : 'border-neutral-200 bg-white text-neutral-500 dark:border-neutral-700 dark:bg-neutral-900'
              }`}
            >{i + 1}</button>
          ))}
        </div>
        {index < lastNavigable ? (
          <button type="button" onClick={() => setIndex(i => i + 1)} className={BUTTON}>
            Next <ChevronRight size={14} />
          </button>
        ) : nextSectionIndex >= 0 ? (
          <button type="button" onClick={() => void advanceSection(false)} className={BUTTON}>
            Finish {currentSkill} <ChevronRight size={14} />
          </button>
        ) : (
          <button type="button" onClick={() => setConfirmSubmit(true)} className={BUTTON}>
            <Send size={14} /> Finish
          </button>
        )}
      </footer>

      {confirmSubmit && (
        <div className="fixed inset-0 z-50 grid place-items-center bg-neutral-950/50 p-4">
          <div className="w-full max-w-sm rounded-xl border border-neutral-200 bg-white p-5 dark:border-neutral-800 dark:bg-neutral-900">
            <div className="flex items-start justify-between">
              <h3 className="text-base font-bold text-neutral-900 dark:text-neutral-50">Submit this attempt?</h3>
              <button type="button" onClick={() => setConfirmSubmit(false)} className="text-neutral-400">
                <X size={16} />
              </button>
            </div>
            <p className="mt-2 text-[13px] leading-relaxed text-neutral-500">
              {answeredCount} of {items.length} items have an answer.
              {answeredCount < items.length && ' Unanswered questions score zero — a guess is always worth more than a blank.'}
              {' '}Scoring starts as soon as you submit and cannot be undone.
            </p>
            <div className="mt-4 flex gap-2">
              <button type="button" disabled={busy} className={BUTTON} onClick={() => void submit()}>
                {busy ? 'Submitting…' : 'Submit and score'}
              </button>
              <button type="button" className={BUTTON_QUIET} onClick={() => setConfirmSubmit(false)}>
                Keep working
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
