'use client'

import { Check, Copy, RefreshCw, ThumbsDown, ThumbsUp } from 'lucide-react'
import { cn } from '../../lib/cn'

type Feedback = 'positive' | 'negative' | null

function ActionButton({
  label,
  onClick,
  active = false,
  activeClass = '',
  children,
}: {
  label: string
  onClick: () => void
  active?: boolean
  activeClass?: string
  children: React.ReactNode
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={label}
      title={label}
      className={cn(
        'inline-flex h-7 w-7 flex-shrink-0 items-center justify-center rounded-full border transition-colors',
        active
          ? activeClass
          : 'border-neutral-200 bg-white text-neutral-500 hover:border-neutral-300 hover:text-neutral-900 dark:border-neutral-700 dark:bg-neutral-900 dark:text-neutral-400 dark:hover:text-neutral-100',
      )}
    >
      {children}
    </button>
  )
}

export function MessageActions({
  turnId,
  copyText,
  copied,
  onCopy,
  feedback,
  onFeedback,
  onRetry,
}: {
  turnId: string
  copyText: string
  copied: boolean
  onCopy: () => void
  feedback: Feedback
  onFeedback: (turnId: string, rating: 'positive' | 'negative') => void
  onRetry: () => void
}) {
  return (
    <div className="flex items-center gap-1">
      {/* Copy */}
      <ActionButton
        label={copied ? 'Copied' : 'Copy response'}
        onClick={onCopy}
        active={copied}
        activeClass="border-emerald-200 bg-emerald-50 text-emerald-700 dark:border-emerald-500/30 dark:bg-emerald-500/10 dark:text-emerald-400"
      >
        {copied ? <Check size={13} /> : <Copy size={13} />}
      </ActionButton>

      {/* Thumbs up */}
      <ActionButton
        label="Good response"
        onClick={() => onFeedback(turnId, 'positive')}
        active={feedback === 'positive'}
        activeClass="border-emerald-200 bg-emerald-50 text-emerald-700 dark:border-emerald-500/30 dark:bg-emerald-500/10 dark:text-emerald-400"
      >
        <ThumbsUp size={13} />
      </ActionButton>

      {/* Thumbs down */}
      <ActionButton
        label="Bad response"
        onClick={() => onFeedback(turnId, 'negative')}
        active={feedback === 'negative'}
        activeClass="border-red-200 bg-red-50 text-red-700 dark:border-red-500/30 dark:bg-red-500/10 dark:text-red-400"
      >
        <ThumbsDown size={13} />
      </ActionButton>

      {/* Retry */}
      <ActionButton label="Retry" onClick={onRetry}>
        <RefreshCw size={13} />
      </ActionButton>
    </div>
  )
}
