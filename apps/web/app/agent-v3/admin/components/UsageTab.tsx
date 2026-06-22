'use client'

import { useEffect, useState } from 'react'
import { Area, AreaChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { readErrorBody } from '../../lib/api'
import type { AdminUsage, AuthorizedFetch } from '../types'

const RANGES = [
  { value: '1d', label: '24h' },
  { value: '7d', label: '7d' },
  { value: '30d', label: '30d' },
  { value: 'all', label: 'All' },
]

export function UsageTab({ authorizedFetch }: { authorizedFetch: AuthorizedFetch }) {
  const [range, setRange] = useState('7d')
  const [data, setData] = useState<AdminUsage | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    let cancelled = false
    authorizedFetch(`/admin/usage?range=${range}`)
      .then(async response => {
        if (!response.ok) throw new Error(await readErrorBody(response, 'Could not load usage'))
        return response.json() as Promise<AdminUsage>
      })
      .then(payload => {
        if (!cancelled) {
          setData(payload)
          setError('')
        }
      })
      .catch(err => {
        if (!cancelled) setError(err instanceof Error ? err.message : 'Could not load usage')
      })
    return () => {
      cancelled = true
    }
  }, [range])

  return (
    <div className="grid gap-4">
      <div className="flex items-center gap-1.5 rounded-full bg-neutral-100 p-1 dark:bg-neutral-800/60" style={{ width: 'fit-content' }}>
        {RANGES.map(item => (
          <button
            key={item.value}
            type="button"
            onClick={() => setRange(item.value)}
            className={`rounded-full px-3 py-1 text-xs font-bold ${range === item.value ? 'bg-neutral-900 text-white dark:bg-white dark:text-neutral-900' : 'text-neutral-500 dark:text-neutral-400'}`}
          >
            {item.label}
          </button>
        ))}
      </div>

      {error && <p className="text-sm text-red-600 dark:text-red-400">{error}</p>}
      {!data && !error && <p className="text-sm text-neutral-400">Loading…</p>}

      {data && (
        <>
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            <Stat label="Total cost" value={`$${data.summary.total_cost.toFixed(2)}`} />
            <Stat label="Requests" value={data.summary.requests.toLocaleString()} />
            <Stat label="Tokens" value={data.summary.tokens.toLocaleString()} />
            <Stat label="Active users" value={data.summary.users.toLocaleString()} />
          </div>

          <div className="rounded-xl border border-neutral-200 p-3.5 dark:border-neutral-800">
            <p className="mb-2 text-sm font-bold text-neutral-900 dark:text-neutral-50">Cost by day</p>
            {data.cost_by_day.length === 0 ? (
              <p className="text-sm text-neutral-400">No spend in this range.</p>
            ) : (
              <div className="h-56 w-full">
                <ResponsiveContainer width="100%" height="100%">
                  <AreaChart data={data.cost_by_day}>
                    <defs>
                      <linearGradient id="costFill" x1="0" y1="0" x2="0" y2="1">
                        <stop offset="0%" stopColor="#10b981" stopOpacity={0.35} />
                        <stop offset="100%" stopColor="#10b981" stopOpacity={0} />
                      </linearGradient>
                    </defs>
                    <XAxis dataKey="date" tick={{ fontSize: 11, fill: '#a3a3a3' }} axisLine={false} tickLine={false} />
                    <YAxis tick={{ fontSize: 11, fill: '#a3a3a3' }} axisLine={false} tickLine={false} width={48} tickFormatter={value => `$${value}`} />
                    <Tooltip formatter={value => [`$${Number(value).toFixed(4)}`, 'Cost']} contentStyle={{ fontSize: 12, borderRadius: 8 }} />
                    <Area type="monotone" dataKey="cost" stroke="#10b981" strokeWidth={2} fill="url(#costFill)" />
                  </AreaChart>
                </ResponsiveContainer>
              </div>
            )}
          </div>

          <div className="grid gap-4 sm:grid-cols-2">
            <div className="overflow-hidden rounded-xl border border-neutral-200 dark:border-neutral-800">
              <p className="border-b border-neutral-200 bg-neutral-50 px-3 py-2 text-xs font-bold uppercase tracking-wide text-neutral-400 dark:border-neutral-800 dark:bg-neutral-900">Model usage</p>
              <table className="w-full text-left text-sm">
                <tbody className="divide-y divide-neutral-100 dark:divide-neutral-800">
                  {data.model_usage.map(row => (
                    <tr key={row.model} className="bg-white dark:bg-neutral-950">
                      <td className="truncate px-3 py-2 font-mono text-xs text-neutral-700 dark:text-neutral-200">{row.model}</td>
                      <td className="px-3 py-2 text-right text-xs text-neutral-400">{row.requests} req</td>
                      <td className="px-3 py-2 text-right text-xs font-semibold text-neutral-700 dark:text-neutral-200">${row.cost.toFixed(4)}</td>
                    </tr>
                  ))}
                  {data.model_usage.length === 0 && (
                    <tr><td className="px-3 py-6 text-center text-sm text-neutral-400" colSpan={3}>No model usage yet.</td></tr>
                  )}
                </tbody>
              </table>
            </div>

            <div className="overflow-hidden rounded-xl border border-neutral-200 dark:border-neutral-800">
              <p className="border-b border-neutral-200 bg-neutral-50 px-3 py-2 text-xs font-bold uppercase tracking-wide text-neutral-400 dark:border-neutral-800 dark:bg-neutral-900">Task distribution</p>
              <div className="grid gap-2 p-3">
                {data.task_distribution.map(row => {
                  const max = Math.max(...data.task_distribution.map(item => item.count), 1)
                  return (
                    <div key={row.task_type} className="grid grid-cols-[100px_1fr_40px] items-center gap-2 text-xs">
                      <span className="truncate font-semibold text-neutral-600 dark:text-neutral-300">{row.task_type}</span>
                      <span className="h-2 overflow-hidden rounded-full bg-neutral-100 dark:bg-neutral-800">
                        <span className="block h-full rounded-full bg-neutral-900 dark:bg-white" style={{ width: `${(row.count / max) * 100}%` }} />
                      </span>
                      <span className="text-right text-neutral-400">{row.count}</span>
                    </div>
                  )
                })}
                {data.task_distribution.length === 0 && <p className="text-sm text-neutral-400">No requests yet.</p>}
              </div>
            </div>
          </div>
        </>
      )}
    </div>
  )
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-xl border border-neutral-200 bg-white p-3.5 dark:border-neutral-800 dark:bg-neutral-900">
      <p className="text-[11px] font-bold uppercase tracking-wide text-neutral-400">{label}</p>
      <p className="mt-1 text-xl font-bold text-neutral-900 dark:text-neutral-50">{value}</p>
    </div>
  )
}
