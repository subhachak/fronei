'use client'

import { useEffect, useState } from 'react'
import { readErrorBody } from '../../lib/api'
import type { AdminOverview, AuthorizedFetch } from '../types'

export function OverviewTab({ authorizedFetch }: { authorizedFetch: AuthorizedFetch }) {
  const [data, setData] = useState<AdminOverview | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    let cancelled = false
    authorizedFetch('/admin/overview')
      .then(async response => {
        if (!response.ok) throw new Error(await readErrorBody(response, 'Could not load overview'))
        return response.json() as Promise<AdminOverview>
      })
      .then(payload => {
        if (!cancelled) setData(payload)
      })
      .catch(err => {
        if (!cancelled) setError(err instanceof Error ? err.message : 'Could not load overview')
      })
    return () => {
      cancelled = true
    }
  }, [])

  if (error) return <p className="text-sm text-red-600 dark:text-red-400">{error}</p>
  if (!data) return <p className="text-sm text-neutral-400">Loading…</p>

  const cards: { label: string; value: string }[] = [
    { label: 'Users', value: data.users.toLocaleString() },
    { label: 'Requests today', value: data.requests_today.toLocaleString() },
    { label: 'Spend today', value: `$${data.spend_today.toFixed(2)}` },
    { label: 'Errors today', value: data.errors_today.toLocaleString() },
    { label: 'Running research', value: data.running_research_runs.toLocaleString() },
    { label: 'Conversations', value: data.total_conversations.toLocaleString() },
    { label: 'Saved memories', value: data.total_memories.toLocaleString() },
    { label: 'Research runs', value: data.total_research_runs.toLocaleString() },
  ]

  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4">
      {cards.map(card => (
        <div key={card.label} className="rounded-xl border border-neutral-200 bg-white p-4 dark:border-neutral-800 dark:bg-neutral-900">
          <p className="text-[11px] font-bold uppercase tracking-wide text-neutral-400">{card.label}</p>
          <p className="mt-1.5 text-2xl font-bold text-neutral-900 dark:text-neutral-50">{card.value}</p>
        </div>
      ))}
    </div>
  )
}
