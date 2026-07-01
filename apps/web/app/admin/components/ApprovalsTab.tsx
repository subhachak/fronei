'use client'

import { CheckCircle2, RefreshCw } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import { readErrorBody } from '../../lib/api'
import { formatAppDateTime } from '../../lib/format'
import type { AuthorizedFetch, LangGraphRunItem, LangGraphRunsResponse, LangGraphRunStatus } from '../types'

const FILTERS: Array<{ label: string; value: '' | LangGraphRunStatus }> = [
  { label: 'Paused', value: 'paused' },
  { label: 'All', value: '' },
  { label: 'Running', value: 'running' },
  { label: 'Completed', value: 'completed' },
  { label: 'Failed', value: 'failed' },
  { label: 'Orphaned', value: 'orphaned' },
]

const STATUS_STYLES: Record<string, string> = {
  paused: 'bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-300',
  running: 'bg-blue-100 text-blue-800 dark:bg-blue-950 dark:text-blue-300',
  resuming: 'bg-blue-100 text-blue-800 dark:bg-blue-950 dark:text-blue-300',
  completed: 'bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-300',
  failed: 'bg-red-100 text-red-800 dark:bg-red-950 dark:text-red-300',
  orphaned: 'bg-neutral-200 text-neutral-700 dark:bg-neutral-800 dark:text-neutral-300',
}

function formatTime(value: string | null) {
  return formatAppDateTime(value, { second: '2-digit' })
}

export function ApprovalsTab({ authorizedFetch }: { authorizedFetch: AuthorizedFetch }) {
  const [data, setData] = useState<LangGraphRunsResponse | null>(null)
  const [status, setStatus] = useState<'' | LangGraphRunStatus>('paused')
  const [error, setError] = useState('')
  const [refreshing, setRefreshing] = useState(false)
  const [approving, setApproving] = useState<string | null>(null)

  const load = useCallback(async (quiet = false) => {
    if (!quiet) setRefreshing(true)
    try {
      const query = status ? `?status=${status}` : ''
      const response = await authorizedFetch(`/admin/langgraph/runs${query}`)
      if (!response.ok) throw new Error(await readErrorBody(response, 'Could not load LangGraph runs'))
      setData(await response.json() as LangGraphRunsResponse)
      setError('')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not load LangGraph runs')
    } finally {
      if (!quiet) setRefreshing(false)
    }
  }, [authorizedFetch, status])

  useEffect(() => {
    void load()
    const timer = window.setInterval(() => void load(true), 5000)
    return () => window.clearInterval(timer)
  }, [load])

  async function approve(runId: string) {
    setApproving(runId)
    setError('')
    try {
      const response = await authorizedFetch(`/admin/langgraph/runs/${encodeURIComponent(runId)}/approve`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({}),
      })
      if (!response.ok) throw new Error(await readErrorBody(response, 'Could not approve this run'))
      await load(true)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not approve this run')
    } finally {
      setApproving(null)
    }
  }

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex flex-wrap gap-1">
          {FILTERS.map(filter => (
            <button
              key={filter.label}
              type="button"
              onClick={() => setStatus(filter.value)}
              className={`h-8 rounded-md px-3 text-xs font-semibold ${
                status === filter.value
                  ? 'bg-neutral-900 text-white dark:bg-white dark:text-neutral-900'
                  : 'border border-neutral-200 text-neutral-600 hover:bg-neutral-100 dark:border-neutral-800 dark:text-neutral-300 dark:hover:bg-neutral-900'
              }`}
            >
              {filter.label}
            </button>
          ))}
        </div>
        <button
          type="button"
          onClick={() => void load()}
          disabled={refreshing}
          className="grid h-8 w-8 place-items-center rounded-md border border-neutral-200 text-neutral-500 disabled:opacity-50 dark:border-neutral-800 dark:text-neutral-400"
          aria-label="Refresh"
        >
          <RefreshCw size={14} className={refreshing ? 'animate-spin' : ''} />
        </button>
      </div>

      {error && <p className="text-sm text-red-600 dark:text-red-400">{error}</p>}

      {!data ? (
        <div className="h-40 animate-pulse bg-neutral-100 dark:bg-neutral-900" />
      ) : data.items.length === 0 ? (
        <p className="py-16 text-center text-sm text-neutral-400">No runs match this status.</p>
      ) : (
        <div className="overflow-x-auto border border-neutral-200 dark:border-neutral-800">
          <table className="w-full min-w-[900px] border-collapse text-left text-xs">
            <thead className="bg-neutral-50 text-[10px] uppercase text-neutral-400 dark:bg-neutral-900">
              <tr>
                <th className="px-3 py-2.5">Status</th>
                <th className="px-3 py-2.5">Run</th>
                <th className="px-3 py-2.5">Reason</th>
                <th className="px-3 py-2.5">Updated</th>
                <th className="w-32 px-3 py-2.5" />
              </tr>
            </thead>
            <tbody>
              {data.items.map((run: LangGraphRunItem) => (
                <tr key={run.run_id} className="border-t border-neutral-200 align-top dark:border-neutral-800">
                  <td className="px-3 py-3">
                    <span className={`inline-flex rounded px-2 py-1 font-bold ${STATUS_STYLES[run.status] || ''}`}>
                      {run.status}
                    </span>
                  </td>
                  <td className="max-w-[360px] px-3 py-3">
                    <p className="truncate font-mono text-[10px] text-neutral-400" title={run.run_id}>{run.run_id}</p>
                    <p className="mt-1 line-clamp-2 font-medium text-neutral-800 dark:text-neutral-200">
                      {run.objective || '-'}
                    </p>
                    <p className="mt-1 truncate text-[10px] text-neutral-400">{run.user_id || '-'}</p>
                  </td>
                  <td className="px-3 py-3 text-neutral-600 dark:text-neutral-300">{run.pause_reason || '-'}</td>
                  <td className="px-3 py-3 text-neutral-500">{formatTime(run.updated_at)}</td>
                  <td className="px-3 py-3">
                    {run.status === 'paused' && (
                      <button
                        type="button"
                        onClick={() => void approve(run.run_id)}
                        disabled={approving === run.run_id}
                        className="inline-flex h-7 items-center gap-1 rounded-md bg-amber-600 px-2 text-[11px] font-semibold text-white hover:bg-amber-700 disabled:opacity-50"
                      >
                        <CheckCircle2 size={12} />
                        {approving === run.run_id ? 'Approving...' : 'Approve'}
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
