'use client'

import { CheckCircle2, XCircle } from 'lucide-react'
import { useEffect, useState } from 'react'
import { readErrorBody } from '../../lib/api'
import type { AdminProvidersResponse, AdminSystem, AuthorizedFetch } from '../types'

export function SystemTab({ authorizedFetch }: { authorizedFetch: AuthorizedFetch }) {
  const [system, setSystem] = useState<AdminSystem | null>(null)
  const [providers, setProviders] = useState<AdminProvidersResponse | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    let cancelled = false
    Promise.all([
      authorizedFetch('/admin/system').then(async response => {
        if (!response.ok) throw new Error(await readErrorBody(response, 'Could not load system info'))
        return response.json() as Promise<AdminSystem>
      }),
      authorizedFetch('/admin/providers').then(async response => {
        if (!response.ok) throw new Error(await readErrorBody(response, 'Could not load providers'))
        return response.json() as Promise<AdminProvidersResponse>
      }),
    ])
      .then(([systemPayload, providersPayload]) => {
        if (cancelled) return
        setSystem(systemPayload)
        setProviders(providersPayload)
      })
      .catch(err => {
        if (!cancelled) setError(err instanceof Error ? err.message : 'Could not load system info')
      })
    return () => {
      cancelled = true
    }
  }, [])

  if (error) return <p className="text-sm text-red-600 dark:text-red-400">{error}</p>
  if (!system || !providers) return <p className="text-sm text-neutral-400">Loading…</p>

  const configRows: { label: string; value: string }[] = [
    { label: 'Environment', value: system.app_env },
    { label: 'Database', value: system.database },
    { label: 'Default profile', value: system.default_profile },
    { label: 'Monthly budget default', value: system.monthly_budget_usd != null ? `$${system.monthly_budget_usd.toFixed(2)}` : 'unset' },
    { label: 'Planner model', value: system.planner_model },
    { label: 'Planner fallbacks', value: system.planner_fallback_models.join(', ') || '—' },
    { label: 'Admin IDs configured', value: String(system.admin_user_ids_configured) },
    { label: 'Admin emails configured', value: String(system.admin_emails_configured) },
    { label: 'Sentry', value: system.sentry_configured ? 'configured' : 'not configured' },
    { label: 'Structured logging', value: system.structured_logging ? 'JSON' : 'plain text' },
    { label: 'Turn workers', value: `${system.worker.live_threads}/${system.worker.configured_concurrency} live` },
    {
      label: 'Artifact storage',
      value: system.artifact_storage_backend === 's3'
        ? `S3-compatible${system.artifact_s3_bucket_configured ? '' : ' (bucket missing)'}`
        : 'local filesystem',
    },
  ]

  return (
    <div className="grid gap-4">
      <div className="grid gap-3 sm:grid-cols-2">
        <div className={`flex items-center gap-2 rounded-xl border p-3.5 ${system.clerk_issuer_configured ? 'border-emerald-200 dark:border-emerald-500/30' : 'border-red-200 dark:border-red-500/30'}`}>
          {system.clerk_issuer_configured ? <CheckCircle2 size={16} className="text-emerald-600 dark:text-emerald-400" /> : <XCircle size={16} className="text-red-600 dark:text-red-400" />}
          <span className="text-sm font-semibold text-neutral-700 dark:text-neutral-200">Clerk issuer configured</span>
        </div>
        <div className={`flex items-center gap-2 rounded-xl border p-3.5 ${system.clerk_audience_configured ? 'border-emerald-200 dark:border-emerald-500/30' : 'border-amber-200 dark:border-amber-500/30'}`}>
          {system.clerk_audience_configured ? <CheckCircle2 size={16} className="text-emerald-600 dark:text-emerald-400" /> : <XCircle size={16} className="text-amber-600 dark:text-amber-400" />}
          <span className="text-sm font-semibold text-neutral-700 dark:text-neutral-200">Clerk audience configured{!system.clerk_audience_configured && ' (required in production)'}</span>
        </div>
      </div>

      <div className="overflow-hidden rounded-xl border border-neutral-200 dark:border-neutral-800">
        <p className="border-b border-neutral-200 bg-neutral-50 px-3 py-2 text-xs font-bold uppercase tracking-wide text-neutral-400 dark:border-neutral-800 dark:bg-neutral-900">Configuration</p>
        <table className="w-full text-left text-sm">
          <tbody className="divide-y divide-neutral-100 dark:divide-neutral-800">
            {configRows.map(row => (
              <tr key={row.label} className="bg-white dark:bg-neutral-950">
                <td className="px-3 py-2 text-neutral-500 dark:text-neutral-400">{row.label}</td>
                <td className="px-3 py-2 text-right font-mono text-xs text-neutral-800 dark:text-neutral-100">{row.value}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="overflow-hidden rounded-xl border border-neutral-200 dark:border-neutral-800">
        <p className="border-b border-neutral-200 bg-neutral-50 px-3 py-2 text-xs font-bold uppercase tracking-wide text-neutral-400 dark:border-neutral-800 dark:bg-neutral-900">Providers</p>
        <table className="w-full text-left text-sm">
          <thead className="text-[11px] font-bold uppercase tracking-wide text-neutral-400">
            <tr>
              <th className="px-3 py-1.5">Provider</th>
              <th className="px-3 py-1.5">Configured</th>
              <th className="px-3 py-1.5">Circuit</th>
              <th className="px-3 py-1.5">Recent errors</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-neutral-100 dark:divide-neutral-800">
            {providers.providers.map(provider => (
              <tr key={provider.key} className="bg-white dark:bg-neutral-950">
                <td className="px-3 py-2 font-semibold text-neutral-800 dark:text-neutral-100">{provider.name}</td>
                <td className="px-3 py-2">
                  {provider.configured ? (
                    <span className="inline-flex items-center gap-1 text-xs font-bold text-emerald-600 dark:text-emerald-400"><CheckCircle2 size={13} /> {provider.key_hint || 'yes'}</span>
                  ) : (
                    <span className="inline-flex items-center gap-1 text-xs font-bold text-neutral-400"><XCircle size={13} /> not set</span>
                  )}
                </td>
                <td className="px-3 py-2 text-xs">
                  {provider.circuit ? (
                    provider.circuit.open ? (
                      <span className="font-bold text-red-600 dark:text-red-400">open · cooldown {provider.circuit.cooldown_remaining_s}s</span>
                    ) : (
                      <span className="text-neutral-400">closed{provider.circuit.consecutive_failures > 0 ? ` · ${provider.circuit.consecutive_failures} recent failures` : ''}</span>
                    )
                  ) : (
                    <span className="text-neutral-400">—</span>
                  )}
                </td>
                <td className="px-3 py-2 text-xs text-neutral-400">{providers.recent_error_counts[provider.key] || 0}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
