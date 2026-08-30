'use client'

import {
  GraduationCap, Layers, Loader2, Mic, RefreshCw, Target, Timer, Zap,
} from 'lucide-react'
import { useCallback, useEffect, useMemo, useState } from 'react'
import type { PracticeMode, Profile, Skill, Spec, StockReport } from '../types'
import type { useCelpip } from '../hooks/useCelpip'
import { BUTTON, BUTTON_QUIET, CARD, ErrorNote, SKILL_TONE, SectionHeading } from './ui'

type Api = ReturnType<typeof useCelpip>
type Scope = 'task' | 'component' | 'full' | 'diagnostic'

const SCOPES: { id: Scope; label: string; hint: string; icon: typeof Target }[] = [
  { id: 'task', label: 'One task type', hint: 'A single official task. The fastest way to drill a weakness.', icon: Target },
  { id: 'component', label: 'One component', hint: 'Every task in one skill, under its official section limit.', icon: Layers },
  { id: 'full', label: 'Full test', hint: 'All components in order — the whole sitting.', icon: GraduationCap },
  { id: 'diagnostic', label: 'Diagnostic', hint: 'A short pass across every component to place you.', icon: Zap },
]

const MODES: { id: PracticeMode; label: string; hint: string; icon: typeof Target }[] = [
  { id: 'learn', label: 'Learn', hint: 'Untimed, answers changeable, feedback after you submit.', icon: Target },
  { id: 'timed', label: 'Timed', hint: 'Official limits, no hints, feedback after the set.', icon: Timer },
  { id: 'simulation', label: 'Simulation', hint: 'Strict timing, one audio play, no answer changes.', icon: GraduationCap },
]

export function PracticeView({ api, onStart }: { api: Api; onStart: (attemptId: string) => void }) {
  const [spec, setSpec] = useState<Spec | null>(null)
  const [profile, setProfile] = useState<Profile | null>(null)
  const [stock, setStock] = useState<StockReport | null>(null)
  const [scope, setScope] = useState<Scope>('task')
  const [mode, setMode] = useState<PracticeMode>('learn')
  const [skill, setSkill] = useState<Skill | null>(null)
  const [taskKey, setTaskKey] = useState<string | null>(null)
  const [error, setError] = useState('')
  const [preparing, setPreparing] = useState('')
  const [busy, setBusy] = useState(false)
  const [micOk, setMicOk] = useState<boolean | null>(null)

  const load = useCallback(async () => {
    try {
      const [s, p, stockReport] = await Promise.all([
        api.getJson<Spec>('/admin/celpip/spec'),
        api.getJson<Profile>('/admin/celpip/profile'),
        api.getJson<StockReport>('/admin/celpip/stock'),
      ])
      setSpec(s)
      setProfile(p)
      setStock(stockReport)
      setSkill(current => current ?? p.components[0])
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not load practice options.')
    }
  }, [api])

  useEffect(() => {
    void load()
  }, [load])

  // Questions are single-use, so a launch draws from a background-filled
  // buffer. While that buffer is short, poll so the launcher unlocks on its own
  // rather than making the learner guess when to retry.
  useEffect(() => {
    if (!stock || stock.can_launch) return
    const id = window.setTimeout(() => void load(), 8000)
    return () => window.clearTimeout(id)
  }, [stock, load])

  const tasksForSkill = useMemo(
    () => (skill && spec ? spec.sections.find(section => section.skill === skill)?.tasks ?? [] : []),
    [skill, spec],
  )

  const requiredTasks = useMemo(() => {
    if (!spec || !profile) return []
    if (scope === 'task') return taskKey ? [taskKey] : []
    if (scope === 'component') {
      return skill ? (spec.sections.find(s => s.skill === skill)?.tasks ?? []).map(t => t.key) : []
    }
    return profile.components.flatMap(
      component => (spec.sections.find(s => s.skill === component)?.tasks ?? []).map(t => t.key),
    )
  }, [scope, spec, profile, skill, taskKey])

  // The server decides the diagnostic's task subset; the client only needs to
  // know whether the buffer covers what it will ask for, so it checks the
  // whole component rather than restating that subset here.
  const notReady = useMemo(
    () => requiredTasks.filter(key => (stock?.ready[key] ?? 0) < 1),
    [requiredTasks, stock],
  )

  const needsMic = Boolean(
    profile?.components.includes('speaking') &&
    (scope === 'full' || scope === 'diagnostic' || skill === 'speaking'),
  )

  const checkMic = useCallback(async () => {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      stream.getTracks().forEach(track => track.stop())
      setMicOk(true)
    } catch {
      setMicOk(false)
    }
  }, [])

  const launch = useCallback(async () => {
    if (!profile) return
    setBusy(true)
    setError('')
    setPreparing('')
    const body: Record<string, unknown> =
      scope === 'task' ? { mode: 'single_task', practice_mode: mode, task_keys: [taskKey] }
      : scope === 'component' ? { mode: 'component', practice_mode: mode, components: [skill] }
      : scope === 'diagnostic' ? { mode: 'diagnostic', practice_mode: mode }
      : { mode: profile.test_type === 'general_ls' ? 'full_ls' : 'full', practice_mode: mode }

    try {
      const test = await api.postJson<{ attempt_id: string }>('/admin/celpip/tests', body)
      onStart(test.attempt_id)
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Could not start this test.'
      try {
        const detail = JSON.parse(message)?.detail
        if (detail?.preparing) {
          setPreparing(detail.hint)
          void load()
        } else {
          setError(detail?.message ?? message)
        }
      } catch {
        setError(message)
      }
    } finally {
      setBusy(false)
    }
  }, [api, profile, scope, mode, taskKey, skill, onStart, load])

  if (!spec || !profile || !stock) {
    return (
      <div className="flex items-center justify-center gap-2 py-24 text-sm text-neutral-400">
        {error ? <ErrorNote message={error} /> : (<><Loader2 size={16} className="animate-spin" /> Loading…</>)}
      </div>
    )
  }

  const canLaunch =
    notReady.length === 0 &&
    requiredTasks.length > 0 &&
    (!needsMic || micOk === true) &&
    !busy

  return (
    <div className="space-y-6">
      {error && <ErrorNote message={error} />}

      <div>
        <p className="text-xs font-bold uppercase tracking-[0.15em] text-amber-600 dark:text-amber-400">
          Practice
        </p>
        <h2 className="mt-1 text-3xl font-bold tracking-tight text-neutral-950 dark:text-white">
          What do you want to sit?
        </h2>
        <p className="mt-2 max-w-2xl text-sm leading-relaxed text-neutral-500">
          Every question is new — you have never seen it, and it is never reused. Retake a finished
          test from Results when you want the same questions again to measure improvement.
        </p>
      </div>

      <section>
        <SectionHeading title="1. How much" />
        <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
          {SCOPES.map(item => (
            <button
              key={item.id}
              type="button"
              onClick={() => setScope(item.id)}
              className={`rounded-xl border p-3 text-left transition-colors ${
                scope === item.id
                  ? 'border-neutral-950 bg-neutral-50 ring-1 ring-neutral-950 dark:border-white dark:bg-neutral-800 dark:ring-white'
                  : 'border-neutral-200 dark:border-neutral-700'
              }`}
            >
              <span className="flex items-center gap-1.5 text-sm font-bold text-neutral-900 dark:text-neutral-50">
                <item.icon size={14} /> {item.label}
              </span>
              <span className="mt-1 block text-xs leading-snug text-neutral-500">{item.hint}</span>
            </button>
          ))}
        </div>
      </section>

      {(scope === 'task' || scope === 'component') && (
        <section>
          <SectionHeading title="2. Which skill" />
          <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
            {profile.components.map(component => (
              <button
                key={component}
                type="button"
                onClick={() => { setSkill(component); setTaskKey(null) }}
                className={`rounded-xl border p-3 text-left capitalize transition-colors ${
                  skill === component
                    ? `border-neutral-950 ring-1 ring-neutral-950 dark:border-white dark:ring-white ${SKILL_TONE[component] ?? ''}`
                    : 'border-neutral-200 dark:border-neutral-700'
                }`}
              >
                <span className="text-sm font-bold text-neutral-900 dark:text-neutral-50">{component}</span>
                <span className="mt-0.5 block text-xs text-neutral-500">
                  {spec.sections.find(s => s.skill === component)?.tasks.length ?? 0} task types
                </span>
              </button>
            ))}
          </div>
        </section>
      )}

      {scope === 'task' && skill && (
        <section>
          <SectionHeading title="3. Which task type" />
          <div className="overflow-hidden rounded-xl border border-neutral-200 dark:border-neutral-800">
            {tasksForSkill.map(task => {
              const selected = taskKey === task.key
              const building = (stock.building[task.key] ?? 0) > 0
              const ready = (stock.ready[task.key] ?? 0) > 0
              return (
                <button
                  key={task.key}
                  type="button"
                  onClick={() => setTaskKey(task.key)}
                  className={`flex min-h-14 w-full items-center gap-3 border-b border-neutral-100 px-4 py-3 text-left last:border-0 dark:border-neutral-800 ${
                    selected
                      ? 'bg-neutral-950 text-white dark:bg-white dark:text-neutral-950'
                      : 'hover:bg-neutral-50 dark:hover:bg-neutral-800'
                  }`}
                >
                  <span className="grid h-7 w-7 flex-shrink-0 place-items-center rounded-lg bg-neutral-100 text-xs font-bold text-neutral-600 dark:bg-neutral-800 dark:text-neutral-300">
                    {task.part}
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="block text-sm font-semibold">{task.label}</span>
                    <span className={`block text-xs ${selected ? 'text-neutral-300 dark:text-neutral-600' : 'text-neutral-500'}`}>
                      {task.description}
                    </span>
                  </span>
                  {/* Stock is a readiness signal, never a gate on choosing:
                      a task the buffer has not reached yet is still selectable,
                      and the launch button explains the wait. */}
                  <span className={`flex-shrink-0 text-xs ${selected ? 'text-neutral-300 dark:text-neutral-600' : 'text-neutral-400'}`}>
                    {ready ? 'Ready' : building ? 'Preparing…' : 'Queued'}
                  </span>
                </button>
              )
            })}
          </div>
        </section>
      )}

      <section>
        <SectionHeading title={scope === 'task' ? '4. How to sit it' : '3. How to sit it'} />
        <div className="grid gap-2 sm:grid-cols-3">
          {MODES.map(item => (
            <button
              key={item.id}
              type="button"
              onClick={() => setMode(item.id)}
              className={`rounded-xl border p-3 text-left transition-colors ${
                mode === item.id
                  ? 'border-neutral-950 bg-neutral-50 ring-1 ring-neutral-950 dark:border-white dark:bg-neutral-800 dark:ring-white'
                  : 'border-neutral-200 dark:border-neutral-700'
              }`}
            >
              <span className="flex items-center gap-1.5 text-sm font-bold text-neutral-900 dark:text-neutral-50">
                <item.icon size={14} /> {item.label}
              </span>
              <span className="mt-1 block text-xs leading-snug text-neutral-500">{item.hint}</span>
            </button>
          ))}
        </div>
      </section>

      <div className={CARD}>
        {needsMic && (
          <div className="mb-3 flex flex-wrap items-center gap-3">
            <button type="button" className={BUTTON_QUIET} onClick={() => void checkMic()}>
              <Mic size={14} /> {micOk === true ? 'Microphone ready' : 'Test microphone'}
            </button>
            {micOk === false && (
              <span className="text-sm font-semibold text-rose-600">
                Blocked — allow microphone access, then retry.
              </span>
            )}
            {micOk === null && (
              <span className="text-sm text-neutral-500">
                Speaking tasks record audio. Check this before you start.
              </span>
            )}
          </div>
        )}

        {notReady.length > 0 && (
          <div className="mb-3 flex items-start gap-2 rounded-lg border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900 dark:border-amber-900 dark:bg-amber-950/40 dark:text-amber-300">
            <Loader2 size={15} className="mt-0.5 flex-shrink-0 animate-spin" />
            <span>
              Preparing {notReady.length} new {notReady.length === 1 ? 'question' : 'questions'} in
              the background. Each one is written and then independently checked before it can be
              served, so the first run after a deploy takes a few minutes. This unlocks on its own.
            </span>
          </div>
        )}
        {preparing && (
          <div className="mb-3 rounded-lg border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900 dark:border-amber-900 dark:bg-amber-950/40 dark:text-amber-300">
            {preparing}
          </div>
        )}

        <div className="flex flex-wrap items-center gap-3">
          <button type="button" disabled={!canLaunch} onClick={() => void launch()} className={BUTTON}>
            {busy ? 'Starting…' : `Start ${mode} ${scope === 'task' ? 'drill' : 'test'}`}
          </button>
          <button type="button" className={BUTTON_QUIET} onClick={() => void load()}>
            <RefreshCw size={14} /> Refresh
          </button>
          {requiredTasks.length === 0 && (
            <span className="text-sm text-neutral-500">Choose a task type to continue.</span>
          )}
        </div>
      </div>
    </div>
  )
}
