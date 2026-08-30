'use client'

import { GraduationCap, Loader2, Target, Timer } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import type { BankCoverage, PracticeMode, Spec } from '../types'
import type { useCelpip } from '../hooks/useCelpip'
import { BUTTON, CARD, EmptyState, ErrorNote, SectionHeading } from './ui'

type Api = ReturnType<typeof useCelpip>

const MODES: { id: PracticeMode; label: string; icon: typeof Target; hint: string }[] = [
  { id: 'learn', label: 'Learn', icon: Target, hint: 'Untimed, answers changeable, feedback after you submit.' },
  { id: 'timed', label: 'Timed', icon: Timer, hint: 'Official time limits, no hints, feedback after the set.' },
  { id: 'simulation', label: 'Simulation', icon: GraduationCap, hint: 'Strict timing, one audio play, no answer changes.' },
]

export function PracticeView({ api, onStart }: { api: Api; onStart: (attemptId: string) => void }) {
  const [spec, setSpec] = useState<Spec | null>(null)
  const [coverage, setCoverage] = useState<Record<string, number>>({})
  const [mode, setMode] = useState<PracticeMode>('learn')
  const [error, setError] = useState('')
  const [starting, setStarting] = useState('')

  const load = useCallback(async () => {
    try {
      const [s, bank] = await Promise.all([
        api.getJson<Spec>('/admin/celpip/spec'),
        api.getJson<{ coverage: BankCoverage[] }>('/admin/celpip/bank?limit=1'),
      ])
      setSpec(s)
      setCoverage(Object.fromEntries(bank.coverage.map(c => [c.task_key, c.ready])))
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not load practice options.')
    }
  }, [api])

  useEffect(() => {
    void load()
  }, [load])

  const start = useCallback(
    async (taskKey: string) => {
      setStarting(taskKey)
      setError('')
      try {
        const test = await api.postJson<{ attempt_id: string }>('/admin/celpip/tests', {
          mode: 'single_task',
          practice_mode: mode,
          task_keys: [taskKey],
        })
        onStart(test.attempt_id)
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Could not start this drill.')
      } finally {
        setStarting('')
      }
    },
    [api, mode, onStart],
  )

  if (!spec) {
    return (
      <div className="flex items-center justify-center gap-2 py-24 text-sm text-neutral-400">
        {error ? <ErrorNote message={error} /> : (<><Loader2 size={16} className="animate-spin" /> Loading…</>)}
      </div>
    )
  }

  const total = Object.values(coverage).reduce((sum, n) => sum + n, 0)

  return (
    <div className="space-y-6">
      {error && <ErrorNote message={error} />}

      <div>
        <SectionHeading title="Practice mode" hint="The same task type behaves differently in each." />
        <div className="grid gap-2 sm:grid-cols-3">
          {MODES.map(item => (
            <button
              key={item.id}
              type="button"
              onClick={() => setMode(item.id)}
              className={`rounded-xl border p-3 text-left transition-colors ${
                mode === item.id
                  ? 'border-neutral-900 bg-neutral-50 dark:border-white dark:bg-neutral-800'
                  : 'border-neutral-200 dark:border-neutral-700'
              }`}
            >
              <span className="flex items-center gap-1.5 text-sm font-bold text-neutral-900 dark:text-neutral-50">
                <item.icon size={14} /> {item.label}
              </span>
              <span className="mt-1 block text-[12px] leading-snug text-neutral-500">{item.hint}</span>
            </button>
          ))}
        </div>
      </div>

      {total === 0 && (
        <EmptyState
          title="The question bank is empty"
          hint="Generate items per task type in the Question Bank. Each one is validated by an independent second pass before it can be served."
        />
      )}

      {spec.sections.map(section => (
        <div key={section.skill}>
          <SectionHeading title={section.label} hint={`${section.tasks.length} official task types`} />
          <div className="grid gap-3 sm:grid-cols-2">
            {section.tasks.map(task => {
              const available = coverage[task.key] ?? 0
              return (
                <div key={task.key} className={CARD}>
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0">
                      <p className="text-sm font-bold text-neutral-900 dark:text-neutral-50">
                        Part {task.part} · {task.label}
                      </p>
                      <p className="mt-0.5 text-[13px] leading-relaxed text-neutral-500">{task.description}</p>
                      <p className="mt-1.5 text-[11px] text-neutral-400">
                        {task.question_count > 0 && `${task.question_count} questions · `}
                        {task.prep_seconds > 0 && `${task.prep_seconds}s prep · `}
                        {task.response_seconds > 0 &&
                          (task.response_seconds >= 120
                            ? `${Math.round(task.response_seconds / 60)} min`
                            : `${task.response_seconds}s`)}
                        {task.word_range && ` · ${task.word_range[0]}–${task.word_range[1]} words`}
                      </p>
                    </div>
                    <span
                      className={`flex-shrink-0 rounded-full px-2 py-0.5 text-[11px] font-semibold ${
                        available > 0
                          ? 'bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-300'
                          : 'bg-neutral-100 text-neutral-500 dark:bg-neutral-800'
                      }`}
                    >
                      {available} ready
                    </span>
                  </div>
                  <button
                    type="button"
                    disabled={available === 0 || starting === task.key}
                    onClick={() => void start(task.key)}
                    className={`${BUTTON} mt-3 w-full justify-center`}
                  >
                    {starting === task.key ? 'Starting…' : `Practise in ${mode} mode`}
                  </button>
                </div>
              )
            })}
          </div>
        </div>
      ))}
    </div>
  )
}
