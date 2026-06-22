'use client'

import * as React from 'react'
import { X } from 'lucide-react'
import { cn } from '../../lib/cn'

export function Sheet({
  open,
  onClose,
  side = 'left',
  title,
  children,
}: {
  open: boolean
  onClose: () => void
  side?: 'left' | 'right'
  title: string
  children: React.ReactNode
}) {
  React.useEffect(() => {
    if (!open) return
    function onKey(event: KeyboardEvent) {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, onClose])

  if (!open) return null

  return (
    <div className="fixed inset-0 z-50 md:hidden" role="dialog" aria-modal="true" aria-label={title}>
      <button
        type="button"
        aria-label="Close panel"
        onClick={onClose}
        className="absolute inset-0 h-full w-full bg-black/40 backdrop-blur-[1px]"
      />
      <div
        className={cn(
          'absolute inset-y-0 flex w-[88%] max-w-sm flex-col bg-white shadow-2xl dark:bg-neutral-950',
          side === 'left' ? 'left-0 border-r border-neutral-200 dark:border-neutral-800' : 'right-0 border-l border-neutral-200 dark:border-neutral-800',
        )}
      >
        <div className="flex items-center justify-between border-b border-neutral-200 px-4 py-3 dark:border-neutral-800">
          <h2 className="text-sm font-semibold text-neutral-900 dark:text-neutral-50">{title}</h2>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="grid h-8 w-8 place-items-center rounded-full text-neutral-500 hover:bg-neutral-100 dark:text-neutral-400 dark:hover:bg-neutral-800"
          >
            <X size={16} />
          </button>
        </div>
        <div className="flex-1 overflow-y-auto px-3 py-3">{children}</div>
      </div>
    </div>
  )
}
