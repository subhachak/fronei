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
  tone?: 'default' | 'on-dark'
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={copied ? 'Copied' : label}
      title={copied ? 'Copied' : label}
      className={cn(
        'inline-flex h-7 w-7 flex-shrink-0 items-center justify-center rounded-full border transition-colors',
        tone === 'on-dark'
          ? 'border-white/20 bg-white/10 text-white/80 hover:bg-white/20'
          : 'border-neutral-200 bg-white text-neutral-500 hover:border-neutral-300 hover:text-neutral-900 dark:border-neutral-700 dark:bg-neutral-900 dark:text-neutral-400 dark:hover:text-neutral-100',
        copied && tone !== 'on-dark' && 'border-emerald-200 bg-emerald-50 text-emerald-700 dark:border-emerald-500/30 dark:bg-emerald-500/10 dark:text-emerald-400',
      )}
    >
      {copied ? <Check size={13} /> : <Copy size={13} />}
    </button>
  )
}
