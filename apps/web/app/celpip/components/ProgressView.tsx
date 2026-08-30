'use client'

import {
  ArrowDownRight, ArrowRight, ArrowUpRight, BookOpen, ChevronDown, Loader2,
  Minus, Sparkles, Target,
} from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import type { CategoryProgress, Coaching, ProgressReport, TaskProgress } from '../types'
import type { useCelpip } from '../hooks/useCelpip'
import {
  ApproximateNote, BUTTON, BUTTON_QUIET, CARD, ErrorNote, LevelBadge, SKILL_TONE,
  SectionHeading, formatDate,
} from './ui'

type Api = ReturnType<typeof useCelpip>

const TREND_ICON = {
  improving: ArrowUpRight,
  slipping: ArrowDownRight,
  steady: Minus,
  unknown: Minus,
} as const

const TREND_TONE = {
  improving: 'text-emerald-600',
  slipping: 'text-rose-600',
  steady: 'text-neutral-400',
  unknown: 'text-neutral-300',
} as const

export function ProgressView({
  api,
  onOpenLesson,
}: {
  api: Api
  onOpenLesson: (slug: string) => void
}) {
  const [report, setReport] = useState<ProgressReport | null>(null)
  const [coaching, setCoaching] = useState<Coaching | null>(null)
  const [coachBusy, setCoachBusy] = useState(false)
  const [error, setError] = useState('')
  const [open, setOpen] = useState<string | null>(null)

  useEffect(() => {
    api
      .getJson<ProgressReport>('/admin/celpip/progress')
      .then(setReport)
      .catch(err => setError(err instanceof Error ? err.message : 'Could not load your progress.'))
  }, [api])

  const coach = useCallback(async () => {
    setCoachBusy(true)
    setError('')
    try {
      setCoaching(await api.postJson<Coaching>('/admin/celpip/coach'))
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not generate a coaching summary.')
    } finally {
      setCoachBusy(false)
    }
  }, [api])

  if (!report) {
    return (
      <div className="flex items-center justify-center gap-2 py-24 text-sm text-neutral-400">
        {error ? <ErrorNote message={error} /> : (<><Loader2 size={16} className="animate-spin" /> Loading…</>)}
      </div>
    )
  }

  return (
    <div className="space-y-7">
      {error && <ErrorNote message={error} />}

      <div>
        <p className="text-xs font-bold uppercase tracking-[0.15em] text-amber-600 dark:text-amber-400">
          Progress
        </p>
        <h2 className="mt-1 text-3xl font-bold tracking-tight text-neutral-950 dark:text-white">
          Where you stand, task by task
        </h2>
        <p className="mt-2 max-w-2xl text-sm leading-relaxed text-neutral-500">
          {report.total_sittings === 0
            ? 'Nothing measured yet. Sit a diagnostic and this fills in.'
            : `${report.total_sittings} recorded sittings across ${report.categories.length} components. Target level ${report.target_level}.`}
        </p>
      </div>

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        {report.categories.map(category => (
          <div key={category.skill} className={`${CARD} ${SKILL_TONE[category.skill] ?? ''}`}>
            <div className="flex items-start justify-between gap-2">
              <div>
                <p className="text-sm font-bold capitalize text-neutral-900 dark:text-neutral-50">
                  {category.label}
                </p>
                <p className="mt-0.5 text-xs text-neutral-500">
                  {category.tasks_attempted}/{category.tasks_total} task types tried
                </p>
              </div>
              <LevelBadge
                low={category.level === null ? null : Math.floor(category.level)}
                high={category.level === null ? null : Math.ceil(category.level)}
              />
            </div>
            <p className="mt-2 flex items-center gap-1 text-xs text-neutral-500">
              <Trend trend={category.trend} />
              {category.sittings} sitting{category.sittings === 1 ? '' : 's'}
            </p>
          </div>
        ))}
      </div>
      <ApproximateNote />

      <section>
        <SectionHeading
          title="Where to spend your time next"
          hint="Ranked by distance from target, then by how long since you practised it."
        />
        <div className="space-y-2">
          {report.focus.map(item => (
            <div key={item.task_key} className={CARD}>
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div className="min-w-0">
                  <p className="text-sm font-bold text-neutral-900 dark:text-neutral-50">
                    <span className="capitalize text-neutral-400">{item.skill} · </span>
                    {item.label}
                  </p>
                  <p className="mt-0.5 text-sm text-neutral-500">{item.reason}</p>
                </div>
                <LevelBadge
                  low={item.level === null ? null : Math.floor(item.level)}
                  high={item.level === null ? null : Math.ceil(item.level)}
                  size="sm"
                />
              </div>
              {item.tips.length > 0 && (
                <div className="mt-2 flex flex-wrap gap-1.5">
                  {item.tips.map(tip => (
                    <button
                      key={tip.slug}
                      type="button"
                      onClick={() => onOpenLesson(tip.slug)}
                      className="inline-flex items-center gap-1.5 rounded-full border border-neutral-200 px-2.5 py-1 text-xs font-semibold text-neutral-600 hover:bg-neutral-50 dark:border-neutral-700 dark:text-neutral-300 dark:hover:bg-neutral-800"
                    >
                      <BookOpen size={11} /> {tip.title}
                    </button>
                  ))}
                </div>
              )}
            </div>
          ))}
        </div>
      </section>

      <section>
        <SectionHeading
          title="Coaching"
          hint="A read of the numbers above, written for you. Generated on request, not on every load."
          action={
            <button type="button" disabled={coachBusy} className={BUTTON_QUIET} onClick={() => void coach()}>
              {coachBusy ? <Loader2 size={14} className="animate-spin" /> : <Sparkles size={14} />}
              {coachBusy ? 'Reading your results…' : coaching ? 'Refresh' : 'Coach me'}
            </button>
          }
        />
        {coaching ? (
          <div className={CARD}>
            <p className="text-base font-semibold text-neutral-900 dark:text-neutral-50">
              {coaching.headline}
            </p>
            {coaching.this_week.length > 0 && (
              <ol className="mt-3 space-y-2">
                {coaching.this_week.map((item, index) => (
                  <li key={index} className="rounded-lg border border-neutral-200 p-3 dark:border-neutral-800">
                    <div className="flex items-start justify-between gap-3">
                      <p className="text-sm font-bold text-neutral-900 dark:text-neutral-50">{item.focus}</p>
                      <span className="flex-shrink-0 rounded-full bg-neutral-100 px-2 py-0.5 text-xs font-semibold text-neutral-600 dark:bg-neutral-800 dark:text-neutral-300">
                        {item.minutes}m
                      </span>
                    </div>
                    <p className="mt-1 text-sm text-neutral-500">{item.why}</p>
                    <p className="mt-1 flex items-start gap-1.5 text-sm text-neutral-700 dark:text-neutral-200">
                      <ArrowRight size={13} className="mt-1 flex-shrink-0" /> {item.action}
                    </p>
                  </li>
                ))}
              </ol>
            )}
            {coaching.watch_out.length > 0 && (
              <ul className="mt-3 list-disc space-y-1 pl-5 text-sm text-amber-700 dark:text-amber-400">
                {coaching.watch_out.map((risk, index) => <li key={index}>{risk}</li>)}
              </ul>
            )}
            {coaching.encouragement && (
              <p className="mt-3 text-sm italic text-neutral-500">{coaching.encouragement}</p>
            )}
          </div>
        ) : (
          <p className="text-sm text-neutral-500">
            {report.total_sittings === 0
              ? 'Sit something first — coaching on no data is just guessing.'
              : 'Ask for a personalised read of your results.'}
          </p>
        )}
      </section>

      {report.categories.map(category => (
        <CategoryBlock
          key={category.skill}
          category={category}
          open={open}
          onToggle={key => setOpen(current => (current === key ? null : key))}
          onOpenLesson={onOpenLesson}
        />
      ))}
    </div>
  )
}

function CategoryBlock({
  category, open, onToggle, onOpenLesson,
}: {
  category: CategoryProgress
  open: string | null
  onToggle: (key: string) => void
  onOpenLesson: (slug: string) => void
}) {
  return (
    <section>
      <SectionHeading
        title={`${category.label} — ${category.tasks.length} task types`}
        hint={`${category.sittings} sittings recorded.`}
      />
      <div className="overflow-hidden rounded-xl border border-neutral-200 dark:border-neutral-800">
        {category.tasks.map(task => (
          <TaskRow
            key={task.task_key}
            task={task}
            expanded={open === task.task_key}
            onToggle={() => onToggle(task.task_key)}
            onOpenLesson={onOpenLesson}
          />
        ))}
      </div>
    </section>
  )
}

function TaskRow({
  task, expanded, onToggle, onOpenLesson,
}: {
  task: TaskProgress
  expanded: boolean
  onToggle: () => void
  onOpenLesson: (slug: string) => void
}) {
  return (
    <div className="border-b border-neutral-100 last:border-0 dark:border-neutral-800">
      <button
        type="button"
        onClick={onToggle}
        className="flex w-full items-center gap-3 px-4 py-3 text-left hover:bg-neutral-50 dark:hover:bg-neutral-900"
      >
        <span className="grid h-7 w-7 flex-shrink-0 place-items-center rounded-lg bg-neutral-100 text-xs font-bold text-neutral-600 dark:bg-neutral-800 dark:text-neutral-300">
          {task.part}
        </span>
        <span className="min-w-0 flex-1">
          <span className="block text-sm font-semibold text-neutral-900 dark:text-neutral-50">
            {task.label}
          </span>
          <span className="block text-xs text-neutral-500">
            {task.sittings === 0
              ? 'Never attempted'
              : `${task.sittings} sitting${task.sittings === 1 ? '' : 's'} · last ${formatDate(task.last_attempted)}`}
            {task.total > 0 ? ` · ${task.correct}/${task.total} correct` : ''}
          </span>
        </span>
        <Trend trend={task.trend} />
        <LevelBadge
          low={task.level === null ? null : Math.floor(task.level)}
          high={task.level === null ? null : Math.ceil(task.level)}
          size="sm"
        />
        <ChevronDown
          size={15}
          className={`flex-shrink-0 text-neutral-400 transition-transform ${expanded ? 'rotate-180' : ''}`}
        />
      </button>

      {expanded && (
        <div className="space-y-3 border-t border-neutral-100 bg-neutral-50 px-4 py-3 dark:border-neutral-800 dark:bg-neutral-900">
          <p className="text-sm leading-relaxed text-neutral-600 dark:text-neutral-300">
            {task.description}
          </p>

          {task.history.length > 1 && <Sparkline history={task.history} />}

          {task.weakness_tags.length > 0 && (
            <div>
              <p className="mb-1.5 text-xs font-bold uppercase tracking-wide text-neutral-400">
                What kept costing marks
              </p>
              <ul className="space-y-1 text-sm text-neutral-600 dark:text-neutral-300">
                {task.weakness_tags.map(tag => (
                  <li key={tag.tag}>
                    {tag.label} <span className="text-neutral-400">({tag.count}×)</span>
                  </li>
                ))}
              </ul>
            </div>
          )}

          {task.tips.length > 0 && (
            <div>
              <p className="mb-1.5 flex items-center gap-1.5 text-xs font-bold uppercase tracking-wide text-neutral-400">
                <Target size={11} /> Tips that address this
              </p>
              <div className="flex flex-wrap gap-1.5">
                {task.tips.map(tip => (
                  <button
                    key={tip.slug}
                    type="button"
                    onClick={() => onOpenLesson(tip.slug)}
                    className="inline-flex items-center gap-1.5 rounded-full border border-neutral-200 bg-white px-2.5 py-1 text-xs font-semibold text-neutral-600 hover:bg-neutral-50 dark:border-neutral-700 dark:bg-neutral-950 dark:text-neutral-300"
                  >
                    <BookOpen size={11} /> {tip.title} · {tip.estimated_minutes}m
                  </button>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

/** Level over time. Bars rather than a line: with two or three sittings a line
 *  implies a trajectory the data cannot support. */
function Sparkline({ history }: { history: TaskProgress['history'] }) {
  const points = history.map(entry =>
    entry.level ?? (entry.accuracy !== undefined ? entry.accuracy * 12 : 0),
  )
  const max = 12
  return (
    <div>
      <p className="mb-1.5 text-xs font-bold uppercase tracking-wide text-neutral-400">
        Each sitting
      </p>
      <div className="flex h-16 items-end gap-1">
        {points.map((value, index) => (
          <div key={index} className="flex flex-1 flex-col items-center gap-1">
            <div
              className="w-full rounded-t bg-neutral-900 dark:bg-white"
              style={{ height: `${Math.max(4, (value / max) * 100)}%` }}
              title={`Level ${value.toFixed(1)}`}
            />
            <span className="text-[10px] tabular-nums text-neutral-400">{value.toFixed(0)}</span>
          </div>
        ))}
      </div>
    </div>
  )
}

function Trend({ trend }: { trend: TaskProgress['trend'] }) {
  const Icon = TREND_ICON[trend]
  return (
    <span className={`flex flex-shrink-0 items-center gap-1 text-xs font-semibold ${TREND_TONE[trend]}`}>
      <Icon size={13} />
      <span className="hidden sm:inline">{trend === 'unknown' ? '—' : trend}</span>
    </span>
  )
}
