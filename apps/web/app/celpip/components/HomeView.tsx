'use client'

import { CalendarClock, Loader2, PlayCircle, RefreshCw, Sparkles, TriangleAlert } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import type { HomePayload, Profile } from '../types'
import type { useCelpip } from '../hooks/useCelpip'
import { OnboardingCard } from './OnboardingCard'
import {
  ApproximateNote,
  BUTTON,
  BUTTON_QUIET,
  CARD,
  EmptyState,
  ErrorNote,
  LevelBadge,
  SectionHeading,
  formatDate,
} from './ui'

type Api = ReturnType<typeof useCelpip>

const SIGNAL_LABELS: Record<string, string> = {
  component_levels: 'Component levels vs target',
  full_test: 'Full tests sat',
  consistency: 'Consistency across attempts',
  timing: 'Answers given inside the time limit',
  coverage: 'Official task types attempted',
  recency: 'How recent your practice is',
}

const SIGNAL_ADVICE: Record<string, string> = {
  component_levels: 'Sit a component test in whichever skill has no measured level yet.',
  full_test: 'Sit a full simulation — nothing else measures stamina across three hours.',
  consistency: 'Repeat a component you have already done; one score is not a level.',
  timing: 'Practise in Timed mode rather than Learn mode.',
  coverage: 'Some official task types have never been attempted.',
  recency: 'Practise today. Older results decay out of this score.',
}

export function HomeView({
  api,
  onOpenAttempt,
  onNavigate,
  onOpenResult,
}: {
  api: Api
  onOpenAttempt: (attemptId: string) => void
  onNavigate: (view: 'learn' | 'practice' | 'mocks' | 'results' | 'plan' | 'bank') => void
  onOpenResult: (attemptId: string) => void
}) {
  const [data, setData] = useState<HomePayload | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const load = useCallback(async () => {
    try {
      setError('')
      setData(await api.getJson<HomePayload>('/admin/celpip/home'))
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not load your dashboard.')
    }
  }, [api])

  useEffect(() => {
    void load()
  }, [load])

  const saveProfile = useCallback(
    async (profile: Partial<Profile>) => {
      setBusy(true)
      try {
        await api.getJson('/admin/celpip/profile', { method: 'PUT', body: JSON.stringify(profile) })
        await api.postJson('/admin/celpip/plan/regenerate')
        await load()
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Could not save your settings.')
      } finally {
        setBusy(false)
      }
    },
    [api, load],
  )

  if (!data) {
    return (
      <div className="flex items-center justify-center gap-2 py-24 text-sm text-neutral-400">
        {error ? <ErrorNote message={error} /> : (<><Loader2 size={16} className="animate-spin" /> Loading…</>)}
      </div>
    )
  }

  if (data.profile.onboarding_state === 'pending') {
    return <OnboardingCard busy={busy} onSave={saveProfile} onSkip={() => saveProfile({ onboarding_state: 'skipped' })} />
  }

  const { readiness, profile } = data
  const days = readiness.days_until_test

  return (
    <div className="space-y-6">
      {error && <ErrorNote message={error} />}

      <div className="grid gap-4 sm:grid-cols-3">
        <div className={CARD}>
          <p className="text-xs font-semibold uppercase tracking-wide text-neutral-400">Test date</p>
          <p className="mt-1 text-2xl font-bold text-neutral-900 dark:text-neutral-50">
            {days === null ? 'Not set' : days < 0 ? 'Past' : `${days} days`}
          </p>
          <p className="mt-0.5 text-[13px] text-neutral-500">
            {profile.test_date ? formatDate(profile.test_date) : 'Set a date to get a scheduled plan'}
            {' · '}
            {profile.test_type === 'general_ls' ? 'General LS' : 'General'}
          </p>
        </div>
        <div className={CARD}>
          <p className="text-xs font-semibold uppercase tracking-wide text-neutral-400">Target level</p>
          <p className="mt-1 text-2xl font-bold text-neutral-900 dark:text-neutral-50">CELPIP {profile.target_level}</p>
          <p className="mt-0.5 text-[13px] text-neutral-500">Applied to your lowest component, not your average</p>
        </div>
        <div className={CARD}>
          <p className="text-xs font-semibold uppercase tracking-wide text-neutral-400">Readiness</p>
          <p className="mt-1 text-2xl font-bold tabular-nums text-neutral-900 dark:text-neutral-50">
            {readiness.readiness}%
          </p>
          <p className="mt-0.5 text-[13px] text-neutral-500">Computed from six measured signals, below</p>
        </div>
      </div>

      <div>
        <SectionHeading
          title="Where you stand"
          hint="Latest estimate per component."
          action={
            <button type="button" className={BUTTON_QUIET} onClick={() => void load()}>
              <RefreshCw size={14} /> Refresh
            </button>
          }
        />
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          {profile.components.map(skill => {
            const level = readiness.component_levels[skill]
            const latest = level?.latest ?? null
            const short = latest !== null && latest < profile.target_level
            return (
              <div key={skill} className={CARD}>
                <div className="flex items-center justify-between">
                  <p className="text-sm font-bold capitalize text-neutral-900 dark:text-neutral-50">{skill}</p>
                  <LevelBadge low={latest === null ? null : Math.floor(latest)} high={latest === null ? null : Math.ceil(latest)} />
                </div>
                <p className="mt-1.5 text-[13px] text-neutral-500">
                  {latest === null
                    ? 'No scored attempt yet'
                    : short
                      ? `${(profile.target_level - latest).toFixed(1)} below target`
                      : 'At or above target'}
                </p>
                <p className="mt-0.5 text-[11px] text-neutral-400">{level?.attempts ?? 0} scored attempt(s)</p>
              </div>
            )
          })}
        </div>
        <ApproximateNote />
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <div>
          <SectionHeading title="Today" hint={`${data.today.length} scheduled ${data.today.length === 1 ? 'activity' : 'activities'}`} />
          {data.resume_attempt_id && (
            <button
              type="button"
              onClick={() => onOpenAttempt(data.resume_attempt_id!)}
              className={`${BUTTON} mb-3 w-full justify-center`}
            >
              <PlayCircle size={15} /> Resume attempt in progress
            </button>
          )}
          {data.today.length === 0 ? (
            <EmptyState
              title="Nothing scheduled today"
              hint="Generate a plan from your test date and available hours."
              action={
                <button type="button" className={BUTTON} onClick={() => onNavigate('plan')}>
                  Open study plan
                </button>
              }
            />
          ) : (
            <ul className="space-y-2">
              {data.today.map(item => (
                <li key={item.id} className={CARD}>
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0">
                      <p className="text-sm font-semibold text-neutral-900 dark:text-neutral-50">{item.title}</p>
                      <p className="mt-0.5 text-[13px] leading-relaxed text-neutral-500">{item.rationale}</p>
                    </div>
                    <span className="flex-shrink-0 rounded-full bg-neutral-100 px-2 py-0.5 text-[11px] font-semibold text-neutral-600 dark:bg-neutral-800 dark:text-neutral-300">
                      {item.estimated_minutes}m
                    </span>
                  </div>
                </li>
              ))}
            </ul>
          )}
          {data.overdue_count > 0 && (
            <div className="mt-3 flex items-center gap-2 rounded-lg border border-amber-200 bg-amber-50 p-3 text-[13px] text-amber-900 dark:border-amber-900 dark:bg-amber-950/40 dark:text-amber-300">
              <CalendarClock size={14} className="flex-shrink-0" />
              <span>{data.overdue_count} missed activities. The plan carries the important ones forward rather than doubling today.</span>
              <button
                type="button"
                className="ml-auto flex-shrink-0 font-semibold underline"
                onClick={() => onNavigate('plan')}
              >
                Review
              </button>
            </div>
          )}
        </div>

        <div>
          <SectionHeading title="What is holding the score back" hint="Ranked by how much readiness each is costing." />
          <ul className="space-y-2">
            {readiness.biggest_gaps.map(gap => (
              <li key={gap.signal} className={CARD}>
                <div className="flex items-center justify-between gap-3">
                  <p className="text-sm font-semibold text-neutral-900 dark:text-neutral-50">
                    {SIGNAL_LABELS[gap.signal] ?? gap.signal}
                  </p>
                  <span className="flex-shrink-0 text-[11px] font-semibold tabular-nums text-neutral-400">
                    −{Math.round(gap.unclaimed * 100)} pts
                  </span>
                </div>
                <p className="mt-0.5 text-[13px] leading-relaxed text-neutral-500">
                  {SIGNAL_ADVICE[gap.signal] ?? ''}
                </p>
              </li>
            ))}
          </ul>

          {data.weakest.length > 0 && (
            <>
              <SectionHeading title="Recurring weaknesses" hint="Tagged across your scored responses." />
              <ul className="space-y-1.5">
                {data.weakest.map(w => (
                  <li key={w.tag} className="flex items-start gap-2 text-[13px]">
                    <TriangleAlert size={13} className="mt-0.5 flex-shrink-0 text-amber-500" />
                    <span className="text-neutral-600 dark:text-neutral-300">
                      {w.label} <span className="text-neutral-400">({w.count}×)</span>
                    </span>
                  </li>
                ))}
              </ul>
            </>
          )}
        </div>
      </div>

      {!data.speech_configured && (
        <div className="flex items-start gap-2 rounded-lg border border-amber-200 bg-amber-50 p-3 text-[13px] text-amber-900 dark:border-amber-900 dark:bg-amber-950/40 dark:text-amber-300">
          <TriangleAlert size={14} className="mt-0.5 flex-shrink-0" />
          <span>
            No <code className="font-mono">OPENAI_API_KEY</code> is configured, so Listening audio cannot be
            generated and Speaking responses cannot be transcribed. Reading and Writing work without it.
          </span>
        </div>
      )}

      <div>
        <SectionHeading
          title="Recent attempts"
          action={
            <button type="button" className={BUTTON_QUIET} onClick={() => onNavigate('results')}>
              All results
            </button>
          }
        />
        {data.recent_attempts.length === 0 ? (
          <EmptyState
            title="No attempts yet"
            hint="A diagnostic gives every other number on this page something to stand on."
            action={
              <button type="button" className={BUTTON} onClick={() => onNavigate('mocks')}>
                <Sparkles size={14} /> Start a diagnostic
              </button>
            }
          />
        ) : (
          <ul className="divide-y divide-neutral-200 overflow-hidden rounded-xl border border-neutral-200 dark:divide-neutral-800 dark:border-neutral-800">
            {data.recent_attempts.map(attempt => (
              <li key={attempt.attempt_id} className="flex items-center justify-between gap-3 bg-white p-3 dark:bg-neutral-900">
                <div className="min-w-0">
                  <p className="truncate text-sm font-semibold text-neutral-900 dark:text-neutral-50">
                    {attempt.label || attempt.mode}
                  </p>
                  <p className="text-[12px] text-neutral-500">
                    {formatDate(attempt.created_at)} · {attempt.status.replace('_', ' ')}
                  </p>
                </div>
                {attempt.status === 'in_progress' ? (
                  <button type="button" className={BUTTON_QUIET} onClick={() => onOpenAttempt(attempt.attempt_id)}>
                    Resume
                  </button>
                ) : (
                  <button type="button" className={BUTTON_QUIET} onClick={() => onOpenResult(attempt.attempt_id)}>
                    View
                  </button>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  )
}
