'use client'

import { Loader2, Search, Shield, ShieldOff, X } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { readErrorBody } from '../../lib/api'
import { formatAppDate } from '../../lib/format'
import type { AdminUserDetail, AdminUserRow, AdminUsersResponse, AuthorizedFetch, UserRole, UserStatus } from '../types'

const STATUS_STYLES: Record<UserStatus, string> = {
  active: 'bg-emerald-50 text-emerald-700 dark:bg-emerald-500/10 dark:text-emerald-400',
  pending: 'bg-amber-50 text-amber-700 dark:bg-amber-500/10 dark:text-amber-400',
  suspended: 'bg-red-50 text-red-700 dark:bg-red-500/10 dark:text-red-400',
}

export function UsersTab({ authorizedFetch }: { authorizedFetch: AuthorizedFetch }) {
  const [items, setItems] = useState<AdminUserRow[]>([])
  const [total, setTotal] = useState(0)
  const [query, setQuery] = useState('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [selectedUserId, setSelectedUserId] = useState<string | null>(null)

  const pendingCount = items.filter(item => item.status === 'pending').length

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    const handle = window.setTimeout(() => {
      authorizedFetch(`/admin/users?limit=200${query.trim() ? `&query=${encodeURIComponent(query.trim())}` : ''}`)
        .then(async response => {
          if (!response.ok) throw new Error(await readErrorBody(response, 'Could not load users'))
          return response.json() as Promise<AdminUsersResponse>
        })
        .then(payload => {
          if (cancelled) return
          setItems(payload.items)
          setTotal(payload.total)
          setError('')
        })
        .catch(err => {
          if (!cancelled) setError(err instanceof Error ? err.message : 'Could not load users')
        })
        .finally(() => {
          if (!cancelled) setLoading(false)
        })
    }, 250)
    return () => {
      cancelled = true
      window.clearTimeout(handle)
    }
  }, [query])

  const sorted = useMemo(() => {
    const statusOrder: Record<UserStatus, number> = { pending: 0, active: 1, suspended: 2 }
    return [...items].sort((a, b) => statusOrder[a.status] - statusOrder[b.status])
  }, [items])

  function refresh() {
    authorizedFetch(`/admin/users?limit=200${query.trim() ? `&query=${encodeURIComponent(query.trim())}` : ''}`)
      .then(async response => {
        if (!response.ok) throw new Error(await readErrorBody(response, 'Could not load users'))
        return response.json() as Promise<AdminUsersResponse>
      })
      .then(payload => {
        setItems(payload.items)
        setTotal(payload.total)
      })
      .catch(() => {})
  }

  return (
    <div className="grid gap-3">
      <div className="flex flex-wrap items-center gap-2">
        <div className="flex h-9 min-w-0 flex-1 items-center gap-2 rounded-lg border border-neutral-200 bg-white px-3 dark:border-neutral-800 dark:bg-neutral-900 sm:max-w-xs">
          <Search size={14} className="text-neutral-400" />
          <input
            value={query}
            onChange={event => setQuery(event.target.value)}
            placeholder="Search name, email, or user id…"
            className="min-w-0 flex-1 bg-transparent text-sm text-neutral-900 outline-none placeholder:text-neutral-400 dark:text-neutral-100"
          />
        </div>
        {pendingCount > 0 && (
          <span className="inline-flex items-center gap-1.5 rounded-full bg-amber-50 px-2.5 py-1 text-xs font-bold text-amber-700 dark:bg-amber-500/10 dark:text-amber-400">
            {pendingCount} pending approval
          </span>
        )}
        <span className="text-xs font-medium text-neutral-400">{total} total</span>
        {loading && <Loader2 size={14} className="animate-spin text-neutral-400" />}
      </div>

      {error && <p className="text-sm text-red-600 dark:text-red-400">{error}</p>}

      <div className="overflow-hidden rounded-xl border border-neutral-200 dark:border-neutral-800">
        <div className="overflow-x-auto">
          <table className="w-full text-left text-sm">
            <thead className="bg-neutral-50 text-[11px] font-bold uppercase tracking-wide text-neutral-400 dark:bg-neutral-900">
              <tr>
                <th className="px-3 py-2.5">User</th>
                <th className="px-3 py-2.5">Status</th>
                <th className="px-3 py-2.5">Role</th>
                <th className="px-3 py-2.5">Spend (mo)</th>
                <th className="px-3 py-2.5">Tokens</th>
                <th className="px-3 py-2.5">Conversations</th>
                <th className="px-3 py-2.5">Last seen</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-neutral-100 dark:divide-neutral-800">
              {sorted.map(row => (
                <tr
                  key={row.user_id}
                  onClick={() => setSelectedUserId(row.user_id)}
                  className="cursor-pointer bg-white hover:bg-neutral-50 dark:bg-neutral-950 dark:hover:bg-neutral-900"
                >
                  <td className="min-w-[160px] px-3 py-2.5">
                    <p className="truncate font-semibold text-neutral-900 dark:text-neutral-50">{row.name || row.email || row.user_id}</p>
                    {row.email && row.name && <p className="truncate text-xs text-neutral-400">{row.email}</p>}
                  </td>
                  <td className="px-3 py-2.5">
                    <span className={`inline-flex rounded-full px-2 py-0.5 text-xs font-bold ${STATUS_STYLES[row.status]}`}>{row.status}</span>
                  </td>
                  <td className="px-3 py-2.5">
                    {row.role === 'admin' ? (
                      <span className="inline-flex items-center gap-1 text-xs font-bold text-neutral-700 dark:text-neutral-200"><Shield size={11} /> admin</span>
                    ) : (
                      <span className="text-xs text-neutral-400">user</span>
                    )}
                  </td>
                  <td className="px-3 py-2.5 text-neutral-600 dark:text-neutral-300">${row.month_spend.toFixed(2)}</td>
                  <td className="px-3 py-2.5 text-xs text-neutral-500 dark:text-neutral-400">
                    {(row.total_input_tokens + row.total_output_tokens).toLocaleString()}
                  </td>
                  <td className="px-3 py-2.5 text-neutral-600 dark:text-neutral-300">{row.conversation_count}</td>
                  <td className="px-3 py-2.5 text-xs text-neutral-400">{formatAppDate(row.last_seen_at)}</td>
                </tr>
              ))}
              {!loading && sorted.length === 0 && (
                <tr>
                  <td colSpan={7} className="px-3 py-8 text-center text-sm text-neutral-400">No matching users.</td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>

      {selectedUserId && (
        <UserDetailModal
          userId={selectedUserId}
          authorizedFetch={authorizedFetch}
          onClose={() => setSelectedUserId(null)}
          onSaved={refresh}
        />
      )}
    </div>
  )
}

function UserDetailModal({
  userId,
  authorizedFetch,
  onClose,
  onSaved,
}: {
  userId: string
  authorizedFetch: AuthorizedFetch
  onClose: () => void
  onSaved: () => void
}) {
  const [detail, setDetail] = useState<AdminUserDetail | null>(null)
  const [error, setError] = useState('')
  const [status, setStatus] = useState<UserStatus>('active')
  const [budget, setBudget] = useState('')
  const [notes, setNotes] = useState('')
  const [saving, setSaving] = useState(false)
  const [savingRole, setSavingRole] = useState(false)
  const [saveStatus, setSaveStatus] = useState('')

  useEffect(() => {
    let cancelled = false
    authorizedFetch(`/admin/users/${encodeURIComponent(userId)}`)
      .then(async response => {
        if (!response.ok) throw new Error(await readErrorBody(response, 'Could not load user'))
        return response.json() as Promise<AdminUserDetail>
      })
      .then(payload => {
        if (cancelled) return
        setDetail(payload)
        setStatus(payload.control.status)
        setBudget(payload.control.monthly_budget_usd != null ? String(payload.control.monthly_budget_usd) : '')
        setNotes(payload.control.notes || '')
      })
      .catch(err => {
        if (!cancelled) setError(err instanceof Error ? err.message : 'Could not load user')
      })
    return () => {
      cancelled = true
    }
  }, [userId])

  async function saveControl() {
    setSaving(true)
    setSaveStatus('')
    try {
      const response = await authorizedFetch(`/admin/users/${encodeURIComponent(userId)}/control`, {
        method: 'PATCH',
        body: JSON.stringify({
          status,
          monthly_budget_usd: budget.trim() ? Number(budget) : null,
          notes: notes.trim() || null,
        }),
      })
      if (!response.ok) throw new Error(await readErrorBody(response, 'Save failed'))
      setSaveStatus('Saved.')
      onSaved()
    } catch (err) {
      setSaveStatus(err instanceof Error ? err.message : 'Save failed')
    } finally {
      setSaving(false)
    }
  }

  async function setRole(role: UserRole) {
    setSavingRole(true)
    try {
      const response = await authorizedFetch(`/admin/users/${encodeURIComponent(userId)}/role`, {
        method: 'PATCH',
        body: JSON.stringify({ role }),
      })
      if (!response.ok) throw new Error(await readErrorBody(response, 'Role update failed'))
      setDetail(prev => prev ? { ...prev, control: { ...prev.control, role } } : prev)
      onSaved()
    } catch (err) {
      setSaveStatus(err instanceof Error ? err.message : 'Role update failed')
    } finally {
      setSavingRole(false)
    }
  }

  return (
    <div className="fixed inset-0 z-40 flex items-end justify-center bg-black/40 p-0 sm:items-center sm:p-4" onClick={onClose}>
      <div
        onClick={event => event.stopPropagation()}
        className="max-h-[90vh] w-full overflow-y-auto rounded-t-2xl border border-neutral-200 bg-white p-4 shadow-2xl dark:border-neutral-800 dark:bg-neutral-900 sm:max-w-lg sm:rounded-2xl sm:p-5"
      >
        <div className="flex items-center justify-between gap-3">
          <div className="min-w-0">
            <p className="truncate text-base font-bold text-neutral-900 dark:text-neutral-50">{detail?.name || detail?.email || userId}</p>
            {detail?.email && <p className="truncate text-xs text-neutral-400">{detail.email}</p>}
          </div>
          <button type="button" onClick={onClose} aria-label="Close" className="grid h-8 w-8 flex-shrink-0 place-items-center rounded-full text-neutral-400 hover:bg-neutral-100 dark:hover:bg-neutral-800">
            <X size={16} />
          </button>
        </div>

        {error && <p className="mt-3 text-sm text-red-600 dark:text-red-400">{error}</p>}

        {!detail && !error && <p className="mt-4 text-sm text-neutral-400">Loading…</p>}

        {detail && (
          <div className="mt-4 grid gap-4">
            <div className="grid grid-cols-3 gap-2 text-center">
              <Stat label="Conversations" value={detail.counts.conversations} />
              <Stat label="Memories" value={detail.counts.memories} />
              <Stat label="Research runs" value={detail.counts.research_runs} />
            </div>
            <div className="grid grid-cols-2 gap-2 text-center">
              <Stat label="Input tokens" value={detail.counts.total_input_tokens} />
              <Stat label="Output tokens" value={detail.counts.total_output_tokens} />
            </div>

            <div className="grid gap-2.5 rounded-xl border border-neutral-200 p-3.5 dark:border-neutral-800">
              <div className="grid grid-cols-2 gap-2.5">
                <label className="grid gap-1 text-xs font-bold text-neutral-500">
                  Status
                  <select
                    value={status}
                    onChange={event => setStatus(event.target.value as UserStatus)}
                    className="h-9 rounded-lg border border-neutral-200 bg-neutral-50 px-2 text-sm font-semibold text-neutral-900 outline-none dark:border-neutral-700 dark:bg-neutral-800 dark:text-neutral-50"
                  >
                    <option value="pending">pending</option>
                    <option value="active">active</option>
                    <option value="suspended">suspended</option>
                  </select>
                </label>
                <label className="grid gap-1 text-xs font-bold text-neutral-500">
                  Monthly budget ($)
                  <input
                    value={budget}
                    onChange={event => setBudget(event.target.value)}
                    placeholder="org default"
                    inputMode="decimal"
                    className="h-9 rounded-lg border border-neutral-200 bg-neutral-50 px-2 text-sm font-semibold text-neutral-900 outline-none dark:border-neutral-700 dark:bg-neutral-800 dark:text-neutral-50"
                  />
                </label>
              </div>
              <label className="grid gap-1 text-xs font-bold text-neutral-500">
                Notes
                <textarea
                  value={notes}
                  onChange={event => setNotes(event.target.value)}
                  rows={2}
                  className="resize-none rounded-lg border border-neutral-200 bg-neutral-50 px-2 py-1.5 text-sm text-neutral-900 outline-none dark:border-neutral-700 dark:bg-neutral-800 dark:text-neutral-50"
                />
              </label>
              <div className="flex items-center justify-between gap-2">
                {saveStatus && <p className="truncate text-xs font-medium text-neutral-400">{saveStatus}</p>}
                <button
                  type="button"
                  onClick={saveControl}
                  disabled={saving}
                  className="ml-auto flex h-9 items-center gap-1.5 rounded-lg bg-neutral-900 px-3.5 text-sm font-bold text-white disabled:opacity-50 dark:bg-white dark:text-neutral-900"
                >
                  {saving && <Loader2 size={14} className="animate-spin" />} Save
                </button>
              </div>
            </div>

            <div className="flex items-center justify-between rounded-xl border border-neutral-200 p-3.5 dark:border-neutral-800">
              <div>
                <p className="text-sm font-bold text-neutral-900 dark:text-neutral-50">Admin role</p>
                <p className="text-xs text-neutral-400">Grants full admin access, independent of approval status.</p>
              </div>
              {detail.control.role === 'admin' ? (
                <button type="button" disabled={savingRole} onClick={() => setRole('user')} className="flex h-9 flex-shrink-0 items-center gap-1.5 rounded-lg border border-neutral-200 px-3 text-sm font-bold text-neutral-700 disabled:opacity-50 dark:border-neutral-700 dark:text-neutral-200">
                  <ShieldOff size={14} /> Revoke
                </button>
              ) : (
                <button type="button" disabled={savingRole} onClick={() => setRole('admin')} className="flex h-9 flex-shrink-0 items-center gap-1.5 rounded-lg border border-neutral-200 px-3 text-sm font-bold text-neutral-700 disabled:opacity-50 dark:border-neutral-700 dark:text-neutral-200">
                  <Shield size={14} /> Grant admin
                </button>
              )}
            </div>

            {detail.recent_turns.length > 0 && (
              <div className="rounded-xl border border-neutral-200 p-3.5 dark:border-neutral-800">
                <p className="text-xs font-bold uppercase tracking-wide text-neutral-400">Recent turns (tokens)</p>
                <div className="mt-2 grid gap-1.5">
                  {detail.recent_turns.slice(0, 5).map(turn => (
                    <div key={turn.id} className="flex items-center justify-between gap-2 text-xs">
                      <span className="truncate font-semibold text-neutral-600 dark:text-neutral-300">{turn.route}</span>
                      <span className="text-neutral-400">{turn.input_tokens.toLocaleString()} in / {turn.output_tokens.toLocaleString()} out</span>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {detail.recent_errors.length > 0 && (
              <div className="rounded-xl border border-neutral-200 p-3.5 dark:border-neutral-800">
                <p className="text-xs font-bold uppercase tracking-wide text-neutral-400">Recent errors</p>
                <div className="mt-2 grid gap-1.5">
                  {detail.recent_errors.slice(0, 3).map(item => (
                    <p key={item.id} className="truncate text-xs text-red-600 dark:text-red-400">{item.task_type}: {item.error}</p>
                  ))}
                </div>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  )
}

function Stat({ label, value }: { label: string; value: number }) {
  return (
    <div className="rounded-lg bg-neutral-50 p-2.5 dark:bg-neutral-800/60">
      <p className="text-lg font-bold text-neutral-900 dark:text-neutral-50">{value}</p>
      <p className="text-[10px] font-bold uppercase tracking-wide text-neutral-400">{label}</p>
    </div>
  )
}
