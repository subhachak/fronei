'use client'

import { CalendarDays, Check, Loader2, RefreshCw, SkipForward } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import type { PlanItem } from '../types'
import type { useCelpip } from '../hooks/useCelpip'
import { BUTTON, BUTTON_QUIET, CARD, EmptyState, ErrorNote, SectionHeading, formatDate } from './ui'

type Api = ReturnType<typeof useCelpip>
type Plan = { weeks: { week: number; items: PlanItem[] }[]; total: number; completed: number }

const WEEK_FOCUS: Record<number, string> = {
  1: 'Diagnostic, format, and every task type once',
  2: 'Weak-skill drills, response structures, vocabulary, timing',
  3: 'Timed components, targeted correction, two full simulations',
  4: 'Full mocks, consistency, pacing, then a deliberate taper',
}

export function StudyPlanView({ api, onStart }: { api: Api; onStart: (attemptId: string) => void }) {
  const [plan, setPlan] = useState<Plan | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const load = useCallback(async () => {
    try {
      setPlan(await api.getJson<Plan>('/admin/celpip/plan'))
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not load your plan.')
    }
  }, [api])

  useEffect(() => {
    void load()
  }, [load])

  const act = useCallback(
    async (path: string, body?: unknown) => {
      setBusy(true)
      setError('')
      try {
        await api.postJson(path, body)
        await load()
      } catch (err) {
        setError(err instanceof Error ? err.message : 'That did not work.')
      } finally {
        setBusy(false)
      }
    },
    [api, load],
  )

  if (!plan) {
    return (
      <div className="flex items-center justify-center gap-2 py-24 text-sm text-neutral-400">
        {error ? <ErrorNote message={error} /> : (<><Loader2 size={16} className="animate-spin" /> Loading…</>)}
      </div>
    )
  }

  return (
    <div className="space-y-6">
      {error && <ErrorNote message={error} />}

      <SectionHeading
        title="Study plan"
        hint={`${plan.completed} of ${plan.total} activities done. Rebalanced from your measured weaknesses, not a fixed template.`}
        action={
          <div className="flex gap-2">
            <button type="button" disabled={busy} className={BUTTON_QUIET} onClick={() => void act('/admin/celpip/plan/roll-forward')}>
              <SkipForward size={14} /> Catch up
            </button>
            <button type="button" disabled={busy} className={BUTTON} onClick={() => void act('/admin/celpip/plan/regenerate')}>
              <RefreshCw size={14} /> Rebalance
            </button>
          </div>
        }
      />

      {plan.total === 0 ? (
        <EmptyState
          title="No plan yet"
          hint="Set a test date and available hours in onboarding, then rebalance to generate the schedule."
          action={
            <button type="button" className={BUTTON} onClick={() => void act('/admin/celpip/plan/regenerate')}>
              Generate plan
            </button>
          }
        />
      ) : (
        plan.weeks.map(week => (
          <div key={week.week}>
            <div className="mb-2 flex items-baseline gap-3">
              <h3 className="text-sm font-bold text-neutral-900 dark:text-neutral-50">Week {week.week}</h3>
              <p className="text-[13px] text-neutral-500">{WEEK_FOCUS[week.week] ?? ''}</p>
            </div>
            <ul className="space-y-2">
              {week.items.map(item => (
                <li
                  key={item.id}
                  className={`${CARD} ${item.status === 'completed' ? 'opacity-60' : ''} ${
                    item.status === 'skipped' ? 'opacity-40' : ''
                  }`}
                >
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0">
                      <p className="flex items-center gap-2 text-sm font-semibold text-neutral-900 dark:text-neutral-50">
                        <CalendarDays size={13} className="flex-shrink-0 text-neutral-400" />
                        {formatDate(item.scheduled_for)} · {item.title}
                      </p>
                      <p className="mt-0.5 text-[13px] leading-relaxed text-neutral-500">{item.rationale}</p>
                      {item.rescheduled.length > 0 && (
                        <p className="mt-1 text-[11px] text-amber-600">
                          Moved {item.rescheduled.length}× — {item.rescheduled[item.rescheduled.length - 1].reason}
                        </p>
                      )}
                    </div>
                    <div className="flex flex-shrink-0 items-center gap-2">
                      <span className="rounded-full bg-neutral-100 px-2 py-0.5 text-[11px] font-semibold text-neutral-600 dark:bg-neutral-800 dark:text-neutral-300">
                        {item.estimated_minutes}m
                      </span>
                      {item.status === 'pending' && (
                        <button
                          type="button"
                          title="Mark done"
                          onClick={() => void act(`/admin/celpip/plan/items/${item.id}/status`, { status: 'completed' })}
                          className="rounded-lg border border-neutral-200 p-1.5 text-neutral-400 hover:text-emerald-600 dark:border-neutral-700"
                        >
                          <Check size={13} />
                        </button>
                      )}
                      {item.status === 'completed' && <Check size={14} className="text-emerald-600" />}
                    </div>
                  </div>
                </li>
              ))}
            </ul>
          </div>
        ))
      )}
    </div>
  )
}
