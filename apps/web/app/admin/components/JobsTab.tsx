'use client'

import { Ban, RefreshCw } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import { readErrorBody } from '../../lib/api'
import type { AdminJobStatus, AdminJobsResponse, AuthorizedFetch } from '../types'

const FILTERS: Array<{ label: string; value: '' | AdminJobStatus }> = [
  { label: 'All', value: '' },
  { label: 'Queued', value: 'queued' },
  { label: 'Running', value: 'running' },
  { label: 'Failed', value: 'failed' },
  { label: 'Completed', value: 'completed' },
  { label: 'Cancelled', value: 'cancelled' },
]

const STATUS_STYLES: Record<AdminJobStatus, string> = {
  queued: 'bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-300',
  running: 'bg-blue-100 text-blue-800 dark:bg-blue-950 dark:text-blue-300',
  completed: 'bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-300',
  failed: 'bg-red-100 text-red-800 dark:bg-red-950 dark:text-red-300',
  cancelled: 'bg-neutral-200 text-neutral-700 dark:bg-neutral-800 dark:text-neutral-300',
}

function formatTime(value: string | null) {
  if (!value) return '—'
  return new Intl.DateTimeFormat(undefined, {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
    second: '2-digit',
  }).format(new Date(value))
}

export function JobsTab({ authorizedFetch }: { authorizedFetch: AuthorizedFetch }) {
  const [data, setData] = useState<AdminJobsResponse | null>(null)
  const [status, setStatus] = useState<'' | AdminJobStatus>('')
  const [error, setError] = useState('')
  const [refreshing, setRefreshing] = useState(false)
  const [cancelling, setCancelling] = useState<string | null>(null)

  const load = useCallback(async (quiet = false) => {
    if (!quiet) setRefreshing(true)
    try {
      const query = status ? `?status=${status}` : ''
      const response = await authorizedFetch(`/admin/jobs${query}`)
      if (!response.ok) throw new Error(await readErrorBody(response, 'Could not load jobs'))
      setData(await response.json() as AdminJobsResponse)
      setError('')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not load jobs')
    } finally {
      if (!quiet) setRefreshing(false)
    }
  }, [authorizedFetch, status])

  useEffect(() => {
    void load()
    const timer = window.setInterval(() => void load(true), 5000)
    return () => window.clearInterval(timer)
  }, [load])

  async function cancel(turnId: string) {
    setCancelling(turnId)
    try {
      const response = await authorizedFetch(`/admin/jobs/${encodeURIComponent(turnId)}/cancel`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ reason: 'Cancelled from admin job monitor' }),
      })
      if (!response.ok) throw new Error(await readErrorBody(response, 'Could not cancel job'))
      await load(true)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not cancel job')
    } finally {
      setCancelling(null)
    }
  }

  const summary = data?.summary
  const metrics = summary ? [
    ['Queued', summary.queued],
    ['Running', summary.running],
    ['Stale leases', summary.stale_leases],
    ['Retried', summary.retried_jobs],
    ['Failed', summary.failed],
    ['Workers', `${summary.worker.live_threads}/${summary.worker.configured_concurrency}`],
  ] : []

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
          aria-label="Refresh jobs"
          title="Refresh jobs"
        >
          <RefreshCw size={14} className={refreshing ? 'animate-spin' : ''} />
        </button>
      </div>

      {error && <p className="text-sm text-red-600 dark:text-red-400">{error}</p>}

      <div className="grid grid-cols-2 border-y border-neutral-200 sm:grid-cols-3 lg:grid-cols-6 dark:border-neutral-800">
        {metrics.map(([label, value]) => (
          <div key={label} className="border-b border-r border-neutral-200 px-3 py-3 last:border-r-0 sm:border-b-0 dark:border-neutral-800">
            <p className="text-[10px] font-bold uppercase text-neutral-400">{label}</p>
            <p className="mt-1 text-lg font-bold text-neutral-900 dark:text-neutral-50">{value}</p>
          </div>
        ))}
      </div>

      {!data ? (
        <div className="h-40 animate-pulse bg-neutral-100 dark:bg-neutral-900" />
      ) : data.items.length === 0 ? (
        <p className="py-16 text-center text-sm text-neutral-400">No jobs match this status.</p>
      ) : (
        <div className="overflow-x-auto border border-neutral-200 dark:border-neutral-800">
          <table className="w-full min-w-[980px] border-collapse text-left text-xs">
            <thead className="bg-neutral-50 text-[10px] uppercase text-neutral-400 dark:bg-neutral-900">
              <tr>
                <th className="px-3 py-2.5">Status</th>
                <th className="px-3 py-2.5">Turn</th>
                <th className="px-3 py-2.5">Route</th>
                <th className="px-3 py-2.5">Attempts</th>
                <th className="px-3 py-2.5">Heartbeat</th>
                <th className="px-3 py-2.5">Latency</th>
                <th className="px-3 py-2.5">Cost</th>
                <th className="w-10 px-3 py-2.5" />
              </tr>
            </thead>
            <tbody>
              {data.items.map(job => (
                <tr key={job.id} className="border-t border-neutral-200 align-top dark:border-neutral-800">
                  <td className="px-3 py-3">
                    <span className={`inline-flex rounded px-2 py-1 font-bold ${STATUS_STYLES[job.status]}`}>
                      {job.status}
                    </span>
                    {job.cancel_requested && <p className="mt-1 text-[10px] text-amber-600">cancel requested</p>}
                  </td>
                  <td className="max-w-[360px] px-3 py-3">
                    <p className="truncate font-mono text-[10px] text-neutral-400" title={job.id}>{job.id}</p>
                    <p className="mt-1 line-clamp-2 font-medium text-neutral-800 dark:text-neutral-200">{job.objective}</p>
                    <p className="mt-1 truncate text-[10px] text-neutral-400">{job.email || job.name || job.user_id}</p>
                    {job.error_message && <p className="mt-1 line-clamp-2 text-[10px] text-red-600 dark:text-red-400">{job.error_message}</p>}
                  </td>
                  <td className="px-3 py-3 text-neutral-600 dark:text-neutral-300">{job.route}</td>
                  <td className="px-3 py-3 tabular-nums text-neutral-600 dark:text-neutral-300">
                    {job.attempt_count}/{job.max_attempts}
                  </td>
                  <td className="px-3 py-3 text-neutral-500">{formatTime(job.heartbeat_at || job.updated_at)}</td>
                  <td className="px-3 py-3 tabular-nums text-neutral-500">
                    {job.latency_ms ? `${(job.latency_ms / 1000).toFixed(1)}s` : '—'}
                  </td>
                  <td className="px-3 py-3 tabular-nums text-neutral-500">${job.cost_usd.toFixed(4)}</td>
                  <td className="px-3 py-3">
                    {(job.status === 'queued' || job.status === 'running') && (
                      <button
                        type="button"
                        onClick={() => void cancel(job.id)}
                        disabled={cancelling === job.id}
                        className="grid h-7 w-7 place-items-center rounded-md text-neutral-400 hover:bg-red-50 hover:text-red-600 disabled:opacity-50 dark:hover:bg-red-950"
                        aria-label="Cancel job"
                        title="Cancel job"
                      >
                        <Ban size={14} />
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
