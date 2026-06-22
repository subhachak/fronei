'use client'

import { Loader2 } from 'lucide-react'
import { useEffect, useState } from 'react'
import { readErrorBody } from '../../lib/api'
import type { AuthorizedFetch, ModelPolicy } from '../types'

export function ModelPolicyTab({ authorizedFetch }: { authorizedFetch: AuthorizedFetch }) {
  const [policy, setPolicy] = useState<ModelPolicy | null>(null)
  const [draft, setDraft] = useState<Record<string, string>>({})
  const [fallbackDraft, setFallbackDraft] = useState('')
  const [error, setError] = useState('')
  const [status, setStatus] = useState('')
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    load()
  }, [])

  function load() {
    authorizedFetch('/admin/model-policy')
      .then(async response => {
        if (!response.ok) throw new Error(await readErrorBody(response, 'Could not load model policy'))
        return response.json() as Promise<ModelPolicy>
      })
      .then(payload => {
        setPolicy(payload)
        setDraft({ ...payload.roles })
        setFallbackDraft(payload.fallback_models.join(', '))
        setError('')
      })
      .catch(err => setError(err instanceof Error ? err.message : 'Could not load model policy'))
  }

  async function save() {
    setSaving(true)
    setStatus('')
    try {
      const response = await authorizedFetch('/admin/model-policy', {
        method: 'PATCH',
        body: JSON.stringify({
          roles: draft,
          fallback_models: fallbackDraft.split(',').map(s => s.trim()).filter(Boolean),
        }),
      })
      if (!response.ok) throw new Error(await readErrorBody(response, 'Save failed'))
      const updated = await response.json() as { roles: Record<string, string>; fallback_models: string[] }
      setPolicy(prev => prev ? { ...prev, roles: updated.roles, fallback_models: updated.fallback_models } : prev)
      setStatus('Saved. Takes effect for new turns within ~20s, no restart needed.')
    } catch (err) {
      setStatus(err instanceof Error ? err.message : 'Save failed')
    } finally {
      setSaving(false)
    }
  }

  async function reset() {
    setSaving(true)
    setStatus('')
    try {
      const response = await authorizedFetch('/admin/model-policy/reset', { method: 'POST' })
      if (!response.ok) throw new Error(await readErrorBody(response, 'Reset failed'))
      const updated = await response.json() as { roles: Record<string, string>; fallback_models: string[] }
      setDraft({ ...updated.roles })
      setFallbackDraft(updated.fallback_models.join(', '))
      setPolicy(prev => prev ? { ...prev, roles: updated.roles, fallback_models: updated.fallback_models } : prev)
      setStatus('Reset to defaults.')
    } catch (err) {
      setStatus(err instanceof Error ? err.message : 'Reset failed')
    } finally {
      setSaving(false)
    }
  }

  if (error) return <p className="text-sm text-red-600 dark:text-red-400">{error}</p>
  if (!policy) return <p className="text-sm text-neutral-400">Loading…</p>

  return (
    <div className="grid gap-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="text-base font-bold text-neutral-900 dark:text-neutral-50">Agent v3 model policy</h2>
          <p className="mt-0.5 max-w-2xl text-sm text-neutral-500 dark:text-neutral-400">
            Which model handles each Agent v3 stage. This is the only place it&apos;s configured — there is no .env fallback.
            Clear a field and save to revert that role back to the default shown beside it.
          </p>
        </div>
        <div className="flex flex-shrink-0 items-center gap-2">
          <button type="button" onClick={reset} disabled={saving} className="h-9 rounded-lg border border-neutral-200 px-3.5 text-sm font-bold text-neutral-700 disabled:opacity-50 dark:border-neutral-700 dark:text-neutral-200">
            Reset to defaults
          </button>
          <button type="button" onClick={save} disabled={saving} className="flex h-9 items-center gap-1.5 rounded-lg bg-neutral-900 px-3.5 text-sm font-bold text-white disabled:opacity-50 dark:bg-white dark:text-neutral-900">
            {saving && <Loader2 size={14} className="animate-spin" />} Save changes
          </button>
        </div>
      </div>

      {status && <p className="text-sm font-medium text-emerald-600 dark:text-emerald-400">{status}</p>}

      <div className="overflow-hidden rounded-xl border border-neutral-200 dark:border-neutral-800">
        <table className="w-full text-left text-sm">
          <thead className="bg-neutral-50 text-[11px] font-bold uppercase tracking-wide text-neutral-400 dark:bg-neutral-900">
            <tr>
              <th className="px-3 py-2.5">Role</th>
              <th className="px-3 py-2.5">Model</th>
              <th className="px-3 py-2.5">Default</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-neutral-100 dark:divide-neutral-800">
            {policy.available_roles.map(role => (
              <tr key={role} className="bg-white dark:bg-neutral-950">
                <td className="px-3 py-2 font-mono text-xs text-neutral-600 dark:text-neutral-300">{role}</td>
                <td className="px-3 py-2">
                  <input
                    value={draft[role] ?? ''}
                    placeholder={policy.defaults.roles[role] ?? ''}
                    onChange={event => setDraft(prev => ({ ...prev, [role]: event.target.value }))}
                    className="h-8 w-full min-w-[200px] rounded-md border border-neutral-200 bg-neutral-50 px-2 text-sm text-neutral-900 outline-none dark:border-neutral-700 dark:bg-neutral-800 dark:text-neutral-50"
                  />
                </td>
                <td className="px-3 py-2 font-mono text-xs text-neutral-400">{policy.defaults.roles[role] ?? '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="rounded-xl border border-neutral-200 p-3.5 dark:border-neutral-800">
        <p className="text-sm font-bold text-neutral-900 dark:text-neutral-50">Fallback chain</p>
        <p className="mt-0.5 text-xs text-neutral-400">
          Tried in order, after the role&apos;s model, if a provider call fails. Comma-separated; include the litellm provider prefix (e.g. <code>gemini/gemini-2.5-flash</code>).
        </p>
        <input
          value={fallbackDraft}
          onChange={event => setFallbackDraft(event.target.value)}
          className="mt-2 h-9 w-full rounded-lg border border-neutral-200 bg-neutral-50 px-2.5 text-sm text-neutral-900 outline-none dark:border-neutral-700 dark:bg-neutral-800 dark:text-neutral-50"
        />
      </div>
    </div>
  )
}
