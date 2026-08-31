'use client'

import { AlertTriangle, Info, RefreshCw } from 'lucide-react'
import { useState, type ReactNode } from 'react'

export const CARD =
  'rounded-2xl border border-neutral-200/80 bg-white p-4 shadow-sm shadow-neutral-950/[0.025] dark:border-neutral-800 dark:bg-neutral-900 dark:shadow-none'
export const INPUT =
  'w-full rounded-lg border border-neutral-200 bg-white px-3 py-2 text-sm text-neutral-900 focus:outline-none focus:ring-2 focus:ring-neutral-400 dark:border-neutral-700 dark:bg-neutral-800 dark:text-neutral-50'
export const LABEL = 'mb-1 block text-xs font-semibold text-neutral-500'
export const BUTTON =
  'inline-flex min-h-11 items-center gap-1.5 rounded-xl bg-neutral-900 px-4 py-2.5 text-sm font-semibold text-white shadow-sm transition-all hover:-translate-y-px hover:bg-neutral-800 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-amber-400 focus-visible:ring-offset-2 disabled:translate-y-0 disabled:opacity-40 dark:bg-white dark:text-neutral-900 dark:hover:bg-neutral-100'
export const BUTTON_QUIET =
  'inline-flex min-h-11 items-center gap-1.5 rounded-xl border border-neutral-200 px-4 py-2.5 text-sm font-semibold text-neutral-700 transition-colors hover:border-neutral-300 hover:bg-neutral-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-amber-400 focus-visible:ring-offset-2 disabled:opacity-40 dark:border-neutral-700 dark:text-neutral-200 dark:hover:bg-neutral-800'

export const SKILL_TONE: Record<string, string> = {
  listening: 'border-violet-200 bg-violet-50 text-violet-900 dark:border-violet-900 dark:bg-violet-950/35 dark:text-violet-200',
  reading: 'border-sky-200 bg-sky-50 text-sky-900 dark:border-sky-900 dark:bg-sky-950/35 dark:text-sky-200',
  writing: 'border-amber-200 bg-amber-50 text-amber-900 dark:border-amber-900 dark:bg-amber-950/35 dark:text-amber-200',
  speaking: 'border-teal-200 bg-teal-50 text-teal-900 dark:border-teal-900 dark:bg-teal-950/35 dark:text-teal-200',
}

/**
 * A refresh control that shows it did something.
 *
 * Every one of these was wired to a real refetch and none of them said so: no
 * pending state, no timestamp. Since the data usually has not changed between
 * clicks -- question generation takes minutes -- a working refresh looked
 * exactly like a dead button, which is what prompted someone to ask whether
 * they did anything at all.
 */
export function RefreshButton({
  onRefresh,
  label = 'Refresh',
}: {
  onRefresh: () => Promise<unknown>
  label?: string
}) {
  const [pending, setPending] = useState(false)
  const [refreshedAt, setRefreshedAt] = useState<Date | null>(null)

  const run = async () => {
    setPending(true)
    try {
      await onRefresh()
      setRefreshedAt(new Date())
    } catch {
      // Swallowed rather than rethrown: every caller's loader already catches
      // and renders its own error, and letting it escape here only produced an
      // unhandled rejection. The timestamp is not set, so a failed refresh
      // never claims to have updated anything.
    } finally {
      setPending(false)
    }
  }

  return (
    <span className="flex items-center gap-2">
      {refreshedAt && !pending && (
        <span className="text-[11px] tabular-nums text-neutral-400">
          Updated {refreshedAt.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })}
        </span>
      )}
      <button type="button" disabled={pending} className={BUTTON_QUIET} onClick={() => void run()}>
        <RefreshCw size={14} className={pending ? 'animate-spin' : undefined} />
        {pending ? 'Refreshing…' : label}
      </button>
    </span>
  )
}

export function SectionHeading({ title, hint, action }: { title: string; hint?: string; action?: ReactNode }) {
  return (
    <div className="mb-3 flex items-end justify-between gap-4">
      <div>
        <h2 className="text-lg font-bold tracking-tight text-neutral-900 dark:text-neutral-50">{title}</h2>
        {hint && <p className="mt-0.5 text-sm leading-relaxed text-neutral-500">{hint}</p>}
      </div>
      {action}
    </div>
  )
}

/**
 * Every level shown anywhere in this app is an estimate, and saying so once in
 * a footnote is not enough -- the caveat travels with the number.
 */
export function LevelBadge({
  low,
  high,
  size = 'md',
}: {
  low: number | null | undefined
  high: number | null | undefined
  size?: 'sm' | 'md' | 'lg'
}) {
  if (low === null || low === undefined) {
    return <span className="text-sm text-neutral-400">Not measured</span>
  }
  const label = high && high !== low ? `${low}–${high}` : `${low}`
  const classes =
    size === 'lg' ? 'text-3xl px-3 py-1' : size === 'sm' ? 'text-xs px-1.5 py-0.5' : 'text-lg px-2 py-0.5'
  const tone =
    low >= 9
      ? 'bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-300'
      : low >= 7
        ? 'bg-sky-100 text-sky-800 dark:bg-sky-950 dark:text-sky-300'
        : low >= 5
          ? 'bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-300'
          : 'bg-rose-100 text-rose-800 dark:bg-rose-950 dark:text-rose-300'
  return (
    <span
      className={`inline-flex items-center rounded-lg font-bold tabular-nums ${classes} ${tone}`}
      title="Approximate. Official score conversion is not published and varies by test form."
    >
      {label}
    </span>
  )
}

export function ApproximateNote({ children }: { children?: ReactNode }) {
  return (
    <p className="mt-2 flex items-start gap-1.5 text-[11px] leading-relaxed text-neutral-500">
      <Info size={12} className="mt-0.5 flex-shrink-0" />
      <span>
        {children ??
          'Levels are approximate. The official raw-score conversion is not published and varies by test form.'}
      </span>
    </p>
  )
}

export function ErrorNote({ message }: { message: string }) {
  if (!message) return null
  return (
    <div className="flex items-start gap-2 rounded-lg border border-rose-200 bg-rose-50 p-3 text-[13px] text-rose-800 dark:border-rose-900 dark:bg-rose-950/40 dark:text-rose-300">
      <AlertTriangle size={14} className="mt-0.5 flex-shrink-0" />
      <span className="whitespace-pre-wrap">{message}</span>
    </div>
  )
}

export function EmptyState({ title, hint, action }: { title: string; hint?: string; action?: ReactNode }) {
  return (
    <div className="rounded-xl border border-dashed border-neutral-300 p-8 text-center dark:border-neutral-700">
      <p className="text-sm font-semibold text-neutral-700 dark:text-neutral-200">{title}</p>
      {hint && <p className="mx-auto mt-1 max-w-md text-[13px] leading-relaxed text-neutral-500">{hint}</p>}
      {action && <div className="mt-4 flex justify-center">{action}</div>}
    </div>
  )
}

export function formatClock(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return '--:--'
  const safe = Math.max(0, Math.floor(seconds))
  const m = Math.floor(safe / 60)
  const s = safe % 60
  return `${m}:${String(s).padStart(2, '0')}`
}

export function formatDate(value: string | null | undefined): string {
  if (!value) return '—'
  try {
    return new Date(value).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
  } catch {
    return '—'
  }
}
