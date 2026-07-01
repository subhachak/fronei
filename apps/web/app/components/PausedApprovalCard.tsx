'use client'

import { useState } from 'react'
import { AlertTriangle, CheckCircle2 } from 'lucide-react'
import { readErrorBody } from '../lib/api'
import type { AgentResult } from '../types'

type AuthorizedFetch = (path: string, init?: RequestInit) => Promise<Response>

export function PausedApprovalCard({
  result,
  isAdmin,
  authorizedFetch,
  onResolved,
}: {
  result: AgentResult
  isAdmin: boolean
  authorizedFetch: AuthorizedFetch
  onResolved: (updated: AgentResult) => void
}) {
  const [approving, setApproving] = useState(false)
  const [error, setError] = useState('')

  async function approve() {
    if (!result.langgraph_run_id) return
    setApproving(true)
    setError('')
    try {
      const response = await authorizedFetch(
        `/admin/langgraph/runs/${encodeURIComponent(result.langgraph_run_id)}/approve`,
        { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({}) },
      )
      if (!response.ok) throw new Error(await readErrorBody(response, 'Could not approve this run'))
      const statusResponse = await authorizedFetch(`/turns/${result.turn_id}/status`)
      if (!statusResponse.ok) throw new Error(await readErrorBody(statusResponse, 'Could not refresh this turn'))
      const payload = await statusResponse.json()
      onResolved(payload.turn as AgentResult)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not approve this run')
    } finally {
      setApproving(false)
    }
  }

  return (
    <div className="mx-auto w-full max-w-[860px] rounded-lg border border-amber-300 bg-amber-50 p-4 dark:border-amber-800 dark:bg-amber-950/40">
      <div className="flex items-start gap-3">
        <AlertTriangle size={18} className="mt-0.5 flex-shrink-0 text-amber-600 dark:text-amber-400" />
        <div className="min-w-0 flex-1 space-y-1">
          <p className="text-sm font-semibold text-amber-900 dark:text-amber-200">
            Research paused — budget approval needed
          </p>
          <p className="text-sm leading-relaxed text-amber-800 dark:text-amber-300">
            {result.pause_reason || 'This research run reached its cost limit before finishing.'}
          </p>
          {typeof result.required_additional_budget_usd === 'number' && (
            <p className="text-xs text-amber-700 dark:text-amber-400">
              Continuing requires authorizing up to ${result.required_additional_budget_usd.toFixed(2)} more.
            </p>
          )}
          {error && <p className="text-xs text-red-600 dark:text-red-400">{error}</p>}
        </div>
      </div>
      {isAdmin ? (
        <button
          type="button"
          onClick={() => void approve()}
          disabled={approving}
          className="mt-3 inline-flex h-8 items-center gap-1.5 rounded-md bg-amber-600 px-3 text-xs font-semibold text-white hover:bg-amber-700 disabled:opacity-50"
        >
          <CheckCircle2 size={14} />
          {approving ? 'Approving...' : 'Approve and continue'}
        </button>
      ) : (
        <p className="mt-3 text-xs text-amber-700 dark:text-amber-400">
          Waiting on an admin to approve additional budget.
        </p>
      )}
    </div>
  )
}
