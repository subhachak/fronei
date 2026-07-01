import { Check, Copy } from 'lucide-react'
import { cn } from '../../lib/cn'

export function CopyButton({
  copied,
  label,
  onClick,
  tone = 'default',
}: {
  copied: boolean
  label: string
  onClick: () => void
  tone?: 'default' | 'on-dark' | 'on-inverted-bubble'
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={copied ? 'Copied' : label}
      title={copied ? 'Copied' : label}
      className={cn(
        'inline-flex h-7 w-7 flex-shrink-0 items-center justify-center rounded-full border transition-colors',
        tone === 'on-dark' && 'border-white/20 bg-white/10 text-white/80 hover:bg-white/20',
        // For bubbles that invert color with the theme (dark bubble on a
        // light page, light bubble on a dark page -- see the user-message
        // bubble in Timeline.tsx) rather than a bubble that's always dark.
        tone === 'on-inverted-bubble'
          && 'border-white/20 bg-white/10 text-white/80 hover:bg-white/20 dark:border-neutral-900/15 dark:bg-neutral-900/10 dark:text-neutral-900/70 dark:hover:bg-neutral-900/20',
        tone === 'default'
          && 'border-neutral-200 bg-white text-neutral-500 hover:border-neutral-300 hover:text-neutral-900 dark:border-neutral-700 dark:bg-neutral-900 dark:text-neutral-400 dark:hover:text-neutral-100',
        copied && tone === 'default' && 'border-emerald-200 bg-emerald-50 text-emerald-700 dark:border-emerald-500/30 dark:bg-emerald-500/10 dark:text-emerald-400',
        copied && tone !== 'default'
          && 'border-emerald-300/60 bg-emerald-400/20 text-emerald-100 hover:bg-emerald-400/25 dark:border-emerald-600/40 dark:bg-emerald-500/15 dark:text-emerald-700 dark:hover:bg-emerald-500/20',
      )}
    >
      {copied ? <Check size={13} /> : <Copy size={13} />}
    </button>
  )
}
