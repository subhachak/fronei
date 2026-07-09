'use client'

import Link from 'next/link'
import { useFroneiAuth } from '../../lib/auth'
import { cn } from '../../lib/cn'

const BASE =
  'inline-flex items-center justify-center whitespace-nowrap rounded-lg font-semibold transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-stone-400 focus-visible:ring-offset-2'

const VARIANTS = {
  primary: 'bg-stone-900 text-stone-50 hover:bg-stone-800 px-5 py-2.5 text-sm',
  compact: 'bg-stone-900 text-stone-50 hover:bg-stone-800 px-4 py-2 text-xs',
}

export function AuthCta({ variant = 'primary', className }: { variant?: keyof typeof VARIANTS; className?: string }) {
  const { isSignedIn } = useFroneiAuth()

  if (isSignedIn) {
    return (
      <Link href="/app" className={cn(BASE, VARIANTS[variant], className)}>
        Go to app
      </Link>
    )
  }

  return (
    <Link href="/sign-up" className={cn(BASE, VARIANTS[variant], className)}>
      Request access
    </Link>
  )
}
