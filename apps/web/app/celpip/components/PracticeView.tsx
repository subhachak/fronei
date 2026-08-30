'use client'

import { GraduationCap, Loader2, Target, Timer } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import type { BankCoverage, PracticeMode, Skill, Spec } from '../types'
import type { useCelpip } from '../hooks/useCelpip'
import { prepareAndCreateTest } from '../lib/prepareTest'
import { BUTTON, EmptyState, ErrorNote, SectionHeading, SKILL_TONE } from './ui'

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
  const [skill, setSkill] = useState<Skill>('listening')
  const [taskKey, setTaskKey] = useState('')
  const [error, setError] = useState('')
  const [starting, setStarting] = useState('')
  const [progress, setProgress] = useState('')

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

  useEffect(() => {
    const section = spec?.sections.find(item => item.skill === skill)
    if (section && !section.tasks.some(task => task.key === taskKey)) {
      setTaskKey(section.tasks[0]?.key ?? '')
    }
  }, [spec, skill, taskKey])

  const start = useCallback(
    async (taskKey: string) => {
      setStarting(taskKey)
      setError('')
      try {
        const test = await prepareAndCreateTest(api, {
          mode: 'single_task',
          practice_mode: mode,
          task_keys: [taskKey],
        }, [{ taskKey }], setProgress)
        onStart(test.attempt_id)
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Could not start this drill.')
      } finally {
        setStarting('')
        setProgress('')
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
  const activeSection = spec.sections.find(section => section.skill === skill) ?? spec.sections[0]
  const selectedTask = activeSection?.tasks.find(task => task.key === taskKey) ?? activeSection?.tasks[0]

  return (
    <div className="space-y-7">
      {error && <ErrorNote message={error} />}

      <div>
        <p className="text-xs font-bold uppercase tracking-[0.15em] text-amber-600 dark:text-amber-400">Focused practice</p>
        <h2 className="mt-1 text-3xl font-bold tracking-tight text-neutral-950 dark:text-white">What do you want to improve?</h2>
        <p className="mt-2 max-w-2xl text-sm leading-relaxed text-neutral-500">Choose one skill and task. We’ll keep the setup out of the way and take you straight into practice.</p>
      </div>

      {total === 0 && (
        <EmptyState title="Fresh questions are generated when you start" hint="No preparation is needed. Your previous questions stay in Test history only." />
      )}

      <section>
        <SectionHeading title="1. Choose a skill" />
        <div className="grid grid-cols-2 gap-2 lg:grid-cols-4">
          {spec.sections.map(section => (
            <button key={section.skill} type="button" onClick={() => { setSkill(section.skill); setTaskKey(section.tasks[0]?.key ?? '') }} className={`min-h-16 rounded-2xl border p-3 text-left text-sm font-bold capitalize transition-all focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-amber-400 ${skill === section.skill ? `${SKILL_TONE[section.skill]} ring-1 ring-current/20` : 'border-neutral-200 bg-white text-neutral-600 hover:border-neutral-400 dark:border-neutral-800 dark:bg-neutral-900 dark:text-neutral-300'}`}>
              {section.label}
              <span className="mt-1 block text-xs font-normal opacity-70">{section.tasks.length} task types</span>
            </button>
          ))}
        </div>
      </section>

      <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_minmax(320px,.8fr)]">
        <section>
          <SectionHeading title="2. Choose a task" />
          <div className="overflow-hidden rounded-2xl border border-neutral-200 bg-white dark:border-neutral-800 dark:bg-neutral-900">
            {activeSection?.tasks.map(task => {
              const ready = coverage[task.key] ?? 0
              const selected = selectedTask?.key === task.key
              return (
                <button key={task.key} type="button" disabled={ready === 0} onClick={() => setTaskKey(task.key)} className={`flex min-h-14 w-full items-center gap-3 border-b border-neutral-100 px-4 py-3 text-left last:border-0 disabled:opacity-40 dark:border-neutral-800 ${selected ? 'bg-neutral-950 text-white dark:bg-white dark:text-neutral-950' : 'hover:bg-neutral-50 dark:hover:bg-neutral-800'}`}>
                  <span className={`grid h-7 w-7 flex-shrink-0 place-items-center rounded-full border text-xs font-bold ${selected ? 'border-white/30' : 'border-neutral-200 dark:border-neutral-700'}`}>{task.part}</span>
                  <span className="min-w-0 flex-1 text-sm font-semibold">{task.label}</span>
                  {ready === 0 && <span className="text-xs">Created on demand</span>}
                </button>
              )
            })}
          </div>
        </section>

        <aside className="lg:sticky lg:top-4 lg:self-start">
          <SectionHeading title="3. Choose how to practise" />
          <div className="rounded-3xl border border-neutral-200 bg-neutral-50 p-4 dark:border-neutral-800 dark:bg-neutral-900 sm:p-5">
            {selectedTask && (
              <div className="mb-4">
                <p className="text-lg font-bold text-neutral-950 dark:text-white">{selectedTask.label}</p>
                <p className="mt-1 text-sm leading-relaxed text-neutral-500">{selectedTask.description}</p>
                <p className="mt-2 text-xs font-medium text-neutral-400">{selectedTask.question_count > 0 && `${selectedTask.question_count} questions · `}{selectedTask.response_seconds > 0 && `${selectedTask.response_seconds >= 120 ? Math.round(selectedTask.response_seconds / 60) + ' min' : selectedTask.response_seconds + ' sec'}`}{selectedTask.word_range && ` · ${selectedTask.word_range[0]}–${selectedTask.word_range[1]} words`}</p>
              </div>
            )}
            <div className="space-y-2">
              {MODES.map(item => (
                <button key={item.id} type="button" onClick={() => setMode(item.id)} className={`flex w-full items-start gap-3 rounded-xl border p-3 text-left ${mode === item.id ? 'border-neutral-950 bg-white ring-1 ring-neutral-950 dark:border-white dark:bg-neutral-800 dark:ring-white' : 'border-neutral-200 bg-white/60 dark:border-neutral-700 dark:bg-neutral-950/30'}`}>
                  <span className={`mt-0.5 grid h-5 w-5 place-items-center rounded-full border ${mode === item.id ? 'border-neutral-950 bg-neutral-950 text-white dark:border-white dark:bg-white dark:text-neutral-950' : 'border-neutral-300'}`}>{mode === item.id && '✓'}</span>
                  <span><span className="block text-sm font-bold text-neutral-950 dark:text-white">{item.label}</span><span className="mt-0.5 block text-xs leading-relaxed text-neutral-500">{item.hint}</span></span>
                </button>
              ))}
            </div>
            {progress && <p className="mt-4 flex items-center gap-2 text-xs font-medium text-amber-700 dark:text-amber-300"><Loader2 size={13} className="animate-spin" /> {progress}</p>}
            <button type="button" disabled={!selectedTask || Boolean(starting)} onClick={() => selectedTask && void start(selectedTask.key)} className={`${BUTTON} mt-5 w-full justify-center`}>
              {starting ? 'Preparing…' : `Start ${mode} practice`}
            </button>
          </div>
        </aside>
      </div>
    </div>
  )
}
