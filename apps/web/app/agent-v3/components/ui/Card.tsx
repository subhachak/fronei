import * as React from 'react'
import { cn } from '../../lib/cn'

export function Card({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn(
        'rounded-xl border border-neutral-200 bg-white dark:border-neutral-800 dark:bg-neutral-900',
        className,
      )}
      {...props}
    />
  )
}

export function CardHeader({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn('flex items-center gap-2 text-neutral-500 dark:text-neutral-400', className)} {...props} />
}

export function Badge({ className, tone = 'neutral', ...props }: React.HTMLAttributes<HTMLSpanElement> & { tone?: 'neutral' | 'success' | 'danger' }) {
  const tones: Record<string, string> = {
    neutral: 'bg-neutral-100 text-neutral-600 dark:bg-neutral-800 dark:text-neutral-300',
    success: 'bg-emerald-50 text-emerald-700 dark:bg-emerald-500/10 dark:text-emerald-400',
    danger: 'bg-red-50 text-red-700 dark:bg-red-500/10 dark:text-red-400',
  }
  return (
    <span
      className={cn('inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-semibold whitespace-nowrap', tones[tone], className)}
      {...props}
    />
  )
}
