'use client'

import { Edit2, Plus, Trash2, X } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import { readErrorBody } from '../../lib/api'
import type { AuthorizedFetch, EvalCase } from '../types'

const ROLE_OPTIONS = [
  '', 'official_policy', 'operational_reality', 'statistical_data',
  'expert_opinion', 'anecdotal', 'conflicting',
]

const DEFAULT_FORM = {
  title: '',
  query: '',
  category: '',
  expected_criteria_text: '',   // newline-separated criteria
  expected_primary_role: '',
  min_independent_sources: '',
  notes: '',
}

type FormState = typeof DEFAULT_FORM

function CaseModal({
  initial,
  onSave,
  onClose,
}: {
  initial?: EvalCase
  onSave: (data: FormState) => Promise<void>
  onClose: () => void
}) {
  const [form, setForm] = useState<FormState>(
    initial
      ? {
          title: initial.title,
          query: initial.query,
          category: initial.category ?? '',
          expected_criteria_text: (initial.expected_criteria ?? []).join('\n'),
          expected_primary_role: initial.expected_primary_role ?? '',
          min_independent_sources: initial.min_independent_sources != null ? String(initial.min_independent_sources) : '',
          notes: initial.notes ?? '',
        }
      : DEFAULT_FORM,
  )
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')

  function set(k: keyof FormState, v: string) {
    setForm(f => ({ ...f, [k]: v }))
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    setSaving(true)
    setError('')
    try {
      await onSave(form)
      onClose()
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Save failed')
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4">
      <div className="w-full max-w-xl rounded-2xl bg-white dark:bg-neutral-900 shadow-2xl flex flex-col max-h-[90vh]">
        <div className="flex items-center justify-between px-5 py-4 border-b border-neutral-200 dark:border-neutral-800 flex-shrink-0">
          <h3 className="text-sm font-bold text-neutral-900 dark:text-neutral-50">
            {initial ? 'Edit eval case' : 'New eval case'}
          </h3>
          <button type="button" onClick={onClose} className="text-neutral-400 hover:text-neutral-700 dark:hover:text-neutral-200">
            <X size={16} />
          </button>
        </div>

        <form onSubmit={handleSubmit} className="overflow-y-auto flex-1 px-5 py-4 space-y-4">
          <div>
            <label className="block text-xs font-semibold text-neutral-500 mb-1">Title *</label>
            <input
              required
              value={form.title}
              onChange={e => set('title', e.target.value)}
              placeholder="H-4 EAD processing time anchor"
              className="w-full rounded-lg border border-neutral-200 dark:border-neutral-700 bg-white dark:bg-neutral-800 px-3 py-2 text-sm text-neutral-900 dark:text-neutral-50 focus:outline-none focus:ring-2 focus:ring-neutral-400"
            />
          </div>

          <div>
            <label className="block text-xs font-semibold text-neutral-500 mb-1">Query *</label>
            <textarea
              required
              rows={3}
              value={form.query}
              onChange={e => set('query', e.target.value)}
              placeholder="How long is H-4 EAD currently taking when filed with H-1B renewal on premium processing?"
              className="w-full rounded-lg border border-neutral-200 dark:border-neutral-700 bg-white dark:bg-neutral-800 px-3 py-2 text-sm text-neutral-900 dark:text-neutral-50 focus:outline-none focus:ring-2 focus:ring-neutral-400 resize-none"
            />
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-xs font-semibold text-neutral-500 mb-1">Category</label>
              <input
                value={form.category}
                onChange={e => set('category', e.target.value)}
                placeholder="immigration_operational"
                className="w-full rounded-lg border border-neutral-200 dark:border-neutral-700 bg-white dark:bg-neutral-800 px-3 py-2 text-sm text-neutral-900 dark:text-neutral-50 focus:outline-none focus:ring-2 focus:ring-neutral-400"
              />
            </div>
            <div>
              <label className="block text-xs font-semibold text-neutral-500 mb-1">Expected primary role</label>
              <select
                value={form.expected_primary_role}
                onChange={e => set('expected_primary_role', e.target.value)}
                className="w-full rounded-lg border border-neutral-200 dark:border-neutral-700 bg-white dark:bg-neutral-800 px-3 py-2 text-sm text-neutral-900 dark:text-neutral-50 focus:outline-none focus:ring-2 focus:ring-neutral-400"
              >
                {ROLE_OPTIONS.map(r => <option key={r} value={r}>{r || '—'}</option>)}
              </select>
            </div>
          </div>

          <div>
            <label className="block text-xs font-semibold text-neutral-500 mb-1">
              Expected criteria <span className="font-normal text-neutral-400">(one per line — scored by LLM judge)</span>
            </label>
            <textarea
              rows={4}
              value={form.expected_criteria_text}
              onChange={e => set('expected_criteria_text', e.target.value)}
              placeholder={"Cites practitioner data as primary evidence\nIncludes official USCIS SLA as context\nGives a specific time range (not just 'varies')"}
              className="w-full rounded-lg border border-neutral-200 dark:border-neutral-700 bg-white dark:bg-neutral-800 px-3 py-2 text-sm font-mono text-neutral-900 dark:text-neutral-50 focus:outline-none focus:ring-2 focus:ring-neutral-400 resize-none"
            />
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-xs font-semibold text-neutral-500 mb-1">Min independent sources</label>
              <input
                type="number"
                min={1}
                value={form.min_independent_sources}
                onChange={e => set('min_independent_sources', e.target.value)}
                placeholder="2"
                className="w-full rounded-lg border border-neutral-200 dark:border-neutral-700 bg-white dark:bg-neutral-800 px-3 py-2 text-sm text-neutral-900 dark:text-neutral-50 focus:outline-none focus:ring-2 focus:ring-neutral-400"
              />
            </div>
          </div>

          <div>
            <label className="block text-xs font-semibold text-neutral-500 mb-1">Notes</label>
            <textarea
              rows={2}
              value={form.notes}
              onChange={e => set('notes', e.target.value)}
              className="w-full rounded-lg border border-neutral-200 dark:border-neutral-700 bg-white dark:bg-neutral-800 px-3 py-2 text-sm text-neutral-900 dark:text-neutral-50 focus:outline-none focus:ring-2 focus:ring-neutral-400 resize-none"
            />
          </div>

          {error && <p className="text-xs text-red-600 dark:text-red-400">{error}</p>}
        </form>

        <div className="flex justify-end gap-2 px-5 py-4 border-t border-neutral-200 dark:border-neutral-800 flex-shrink-0">
          <button type="button" onClick={onClose}
            className="rounded-lg border border-neutral-200 dark:border-neutral-700 px-4 py-2 text-sm font-semibold text-neutral-600 dark:text-neutral-400 hover:bg-neutral-50 dark:hover:bg-neutral-800">
            Cancel
          </button>
          <button type="button" disabled={saving} onClick={handleSubmit as unknown as React.MouseEventHandler}
            className="rounded-lg bg-neutral-900 dark:bg-white px-4 py-2 text-sm font-semibold text-white dark:text-neutral-900 hover:bg-neutral-700 dark:hover:bg-neutral-200 disabled:opacity-50">
            {saving ? 'Saving…' : 'Save'}
          </button>
        </div>
      </div>
    </div>
  )
}

export function EvalsCasesTab({ authorizedFetch }: { authorizedFetch: AuthorizedFetch }) {
  const [cases, setCases] = useState<EvalCase[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [modal, setModal] = useState<'create' | EvalCase | null>(null)
  const [deleting, setDeleting] = useState<number | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const resp = await authorizedFetch('/admin/evals/cases')
      if (!resp.ok) throw new Error(await readErrorBody(resp, 'Could not load eval cases'))
      const data = await resp.json()
      setCases(data.items ?? [])
      setError('')
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Load failed')
    } finally {
      setLoading(false)
    }
  }, [authorizedFetch])

  useEffect(() => { load() }, [load])

  async function handleSave(form: FormState, existing?: EvalCase) {
    const payload = {
      title: form.title,
      query: form.query,
      category: form.category || null,
      expected_criteria: form.expected_criteria_text
        .split('\n')
        .map(s => s.trim())
        .filter(Boolean),
      expected_primary_role: form.expected_primary_role || null,
      min_independent_sources: form.min_independent_sources ? Number(form.min_independent_sources) : null,
      notes: form.notes || null,
    }
    const url = existing ? `/admin/evals/cases/${existing.id}` : '/admin/evals/cases'
    const method = existing ? 'PUT' : 'POST'
    const resp = await authorizedFetch(url, {
      method,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
    if (!resp.ok) throw new Error(await readErrorBody(resp, 'Save failed'))
    await load()
  }

  async function handleDelete(id: number) {
    if (!confirm('Delete this eval case?')) return
    setDeleting(id)
    try {
      const resp = await authorizedFetch(`/admin/evals/cases/${id}`, { method: 'DELETE' })
      if (!resp.ok) throw new Error(await readErrorBody(resp, 'Delete failed'))
      await load()
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Delete failed')
    } finally {
      setDeleting(null)
    }
  }

  return (
    <div>
      <div className="flex items-center justify-between mb-4">
        <div>
          <h3 className="text-sm font-bold text-neutral-900 dark:text-neutral-50">Eval cases</h3>
          <p className="text-xs text-neutral-500 mt-0.5">{cases.length} case(s) — each run tests all or selected cases through both pipelines.</p>
        </div>
        <button type="button" onClick={() => setModal('create')}
          className="flex items-center gap-1.5 rounded-lg bg-neutral-900 dark:bg-white px-3 py-2 text-xs font-semibold text-white dark:text-neutral-900 hover:bg-neutral-700 dark:hover:bg-neutral-200">
          <Plus size={13} /> New case
        </button>
      </div>

      {error && <p className="mb-3 text-sm text-red-600 dark:text-red-400">{error}</p>}

      {loading ? (
        <p className="text-sm text-neutral-400">Loading…</p>
      ) : cases.length === 0 ? (
        <div className="rounded-xl border border-dashed border-neutral-300 dark:border-neutral-700 p-8 text-center">
          <p className="text-sm text-neutral-500">No eval cases yet.</p>
          <button type="button" onClick={() => setModal('create')}
            className="mt-3 text-xs font-semibold text-neutral-700 dark:text-neutral-300 underline underline-offset-2">
            Create the first one
          </button>
        </div>
      ) : (
        <div className="rounded-xl border border-neutral-200 dark:border-neutral-800 overflow-hidden divide-y divide-neutral-100 dark:divide-neutral-800">
          {cases.map(c => (
            <div key={c.id} className="flex items-start gap-3 px-4 py-3 bg-white dark:bg-neutral-900 hover:bg-neutral-50 dark:hover:bg-neutral-800 transition-colors">
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2 flex-wrap">
                  <span className="text-sm font-semibold text-neutral-900 dark:text-neutral-50 truncate">{c.title}</span>
                  {c.category && (
                    <span className="text-[10px] font-semibold uppercase tracking-wider bg-neutral-100 dark:bg-neutral-800 text-neutral-500 rounded px-1.5 py-0.5">{c.category}</span>
                  )}
                  {c.expected_primary_role && (
                    <span className="text-[10px] font-semibold uppercase tracking-wider bg-blue-50 dark:bg-blue-950/40 text-blue-600 dark:text-blue-400 rounded px-1.5 py-0.5">{c.expected_primary_role}</span>
                  )}
                </div>
                <p className="text-xs text-neutral-500 mt-0.5 line-clamp-1">{c.query}</p>
                {c.expected_criteria.length > 0 && (
                  <p className="text-xs text-neutral-400 mt-0.5">{c.expected_criteria.length} criteria</p>
                )}
              </div>
              <div className="flex items-center gap-1 flex-shrink-0">
                <button type="button" onClick={() => setModal(c)}
                  className="grid h-7 w-7 place-items-center rounded-lg text-neutral-400 hover:bg-neutral-100 dark:hover:bg-neutral-700 hover:text-neutral-700 dark:hover:text-neutral-200">
                  <Edit2 size={13} />
                </button>
                <button type="button" onClick={() => handleDelete(c.id)} disabled={deleting === c.id}
                  className="grid h-7 w-7 place-items-center rounded-lg text-neutral-400 hover:bg-red-50 dark:hover:bg-red-950/30 hover:text-red-600 dark:hover:text-red-400 disabled:opacity-40">
                  <Trash2 size={13} />
                </button>
              </div>
            </div>
          ))}
        </div>
      )}

      {modal !== null && (
        <CaseModal
          initial={modal === 'create' ? undefined : modal as EvalCase}
          onSave={(form) => handleSave(form, modal === 'create' ? undefined : modal as EvalCase)}
          onClose={() => setModal(null)}
        />
      )}
    </div>
  )
}
