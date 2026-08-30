'use client'

import { Clock } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { formatClock } from '../ui'

/**
 * Counts down locally for smoothness, but the number it starts from and
 * periodically snaps back to is the server's. `serverSeconds` changing is a
 * resync; the local tick between resyncs is cosmetic. A learner who edits their
 * system clock, sleeps the laptop, or reloads the page gets the same deadline.
 */
export function SectionTimer({
  serverSeconds,
  onExpire,
  label,
}: {
  serverSeconds: number | null
  onExpire?: () => void
  label?: string
}) {
  const [remaining, setRemaining] = useState<number | null>(serverSeconds)
  const fired = useRef(false)

  useEffect(() => {
    setRemaining(serverSeconds)
    if (serverSeconds !== null && serverSeconds > 0) fired.current = false
  }, [serverSeconds])

  useEffect(() => {
    if (remaining === null) return
    if (remaining <= 0) {
      if (!fired.current) {
        fired.current = true
        onExpire?.()
      }
      return
    }
    const id = window.setInterval(() => setRemaining(value => (value === null ? null : value - 1)), 1000)
    return () => window.clearInterval(id)
  }, [remaining, onExpire])

  if (remaining === null) {
    return (
      <span className="inline-flex items-center gap-1.5 rounded-full bg-neutral-100 px-2.5 py-1 text-xs font-semibold text-neutral-600 dark:bg-neutral-800 dark:text-neutral-300">
        <Clock size={12} /> Untimed
      </span>
    )
  }

  const urgent = remaining <= 60
  const warn = remaining <= 300
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-bold tabular-nums ${
        urgent
          ? 'bg-rose-100 text-rose-800 dark:bg-rose-950 dark:text-rose-300'
          : warn
            ? 'bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-300'
            : 'bg-neutral-100 text-neutral-700 dark:bg-neutral-800 dark:text-neutral-200'
      }`}
      aria-live={urgent ? 'assertive' : 'off'}
    >
      <Clock size={12} />
      {label ? `${label} ` : ''}{formatClock(remaining)}
    </span>
  )
}
