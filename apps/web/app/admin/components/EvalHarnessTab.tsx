'use client'

/**
 * EvalHarnessTab — unified "Eval Harness" tab merging case management + eval runs.
 *
 * Layout:
 *   ┌─ Cases ────────────────────────────────── [+ Add] [↑ Upload JSON] ─┐
 *   │  Checkbox list — select to run or run all. Deactivate/edit inline.  │
 *   │  [Show N inactive ▾]                                                │
 *   └─────────────────────────────────────────────────────────────────────┘
 *   ┌─ Run ─────────────────────────────────────────────────────────────┐
 *   │  LangSmith banner                                                 │
 *   │  [▶ Run selected (N)] / [▶ Run all N]                            │
 *   │  Progress log  →  per-case results  →  run history               │
 *   └───────────────────────────────────────────────────────────────────┘
 */

import {
  ChevronDown,
  ChevronRight,
  Edit2,
  ExternalLink,
  EyeOff,
  Play,
  Plus,
  RotateCcw,
  Upload,
  X,
} from 'lucide-react'
import { useCallback, useEffect, useRef, useState } from 'react'
import { readErrorBody } from '../../lib/api'
import type {
  AuthorizedFetch,
  EvalCase,
  EvalCaseRunResult,
  EvalRunResult,
  EvalRunSummary,
} from '../types'

// ─────────────────────────────────────────────────────────────────────────────
// Constants + shared helpers
// ─────────────────────────────────────────────────────────────────────────────

const ROLE_OPTIONS = [
  '', 'official_policy', 'operational_reality', 'statistical_data',
  'expert_opinion', 'anecdotal', 'conflicting',
]

const DEFAULT_FORM = {
  title: '',
  query: '',
  category: '',
  expected_criteria_text: '',
  expected_primary_role: '',
  min_independent_sources: '',
  notes: '',
}
type FormState = typeof DEFAULT_FORM

function pct(v: number | null | undefined) {
  if (v == null) return '—'
  return `${Math.round(v * 100)}%`
}

// ─────────────────────────────────────────────────────────────────────────────
// LangSmith banner
// ─────────────────────────────────────────────────────────────────────────────

type LangSmithStatus = {
  configured: boolean
  project: string | null
  tracing_on: boolean
  dataset_name: string
}

function LangSmithBanner({ authorizedFetch }: { authorizedFetch: AuthorizedFetch }) {
  const [status, setStatus] = useState<LangSmithStatus | null>(null)

  useEffect(() => {
    authorizedFetch('/admin/evals/langsmith/status')
      .then(r => r.ok ? r.json() : null)
      .then(d => d && setStatus(d))
      .catch(() => {})
  }, [authorizedFetch])

  if (!status) return null
  if (!status.configured) {
    return (
      <div className="rounded-lg border border-neutral-200 dark:border-neutral-700 bg-neutral-50 dark:bg-neutral-900 px-4 py-3 text-xs text-neutral-500">
        <span className="font-semibold text-neutral-700 dark:text-neutral-300">LangSmith not configured</span>
        {' — '}set <code className="bg-neutral-100 dark:bg-neutral-800 px-1 rounded">LANGSMITH_API_KEY</code> to enable experiment tracking.
        Runs use the in-process scorer.
      </div>
    )
  }
  return (
    <div className="rounded-lg border border-emerald-200 dark:border-emerald-900 bg-emerald-50 dark:bg-emerald-950/30 px-4 py-3 text-xs flex items-center gap-3">
      <span className="h-2 w-2 rounded-full bg-emerald-500 flex-shrink-0" />
      <div className="flex-1">
        <span className="font-semibold text-emerald-800 dark:text-emerald-300">LangSmith active</span>
        {' — project '}
        <code className="bg-emerald-100 dark:bg-emerald-900/50 px-1 rounded text-emerald-700 dark:text-emerald-400">{status.project}</code>
        {', dataset '}
        <code className="bg-emerald-100 dark:bg-emerald-900/50 px-1 rounded text-emerald-700 dark:text-emerald-400">{status.dataset_name}</code>
        {status.tracing_on && <span className="ml-2 text-emerald-600 dark:text-emerald-400">· tracing on</span>}
      </div>
      <a href="https://smith.langchain.com" target="_blank" rel="noopener noreferrer"
        className="flex items-center gap-1 text-emerald-700 dark:text-emerald-400 hover:underline font-semibold">
        Open LangSmith <ExternalLink size={11} />
      </a>
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────────────────
// Case form modal
// ─────────────────────────────────────────────────────────────────────────────

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
          min_independent_sources: initial.min_independent_sources != null
            ? String(initial.min_independent_sources) : '',
          notes: initial.notes ?? '',
        }
      : DEFAULT_FORM,
  )
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')

  function set(k: keyof FormState, v: string) { setForm(f => ({ ...f, [k]: v })) }

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
            <input required value={form.title} onChange={e => set('title', e.target.value)}
              placeholder="H-4 EAD processing time anchor"
              className="w-full rounded-lg border border-neutral-200 dark:border-neutral-700 bg-white dark:bg-neutral-800 px-3 py-2 text-sm text-neutral-900 dark:text-neutral-50 focus:outline-none focus:ring-2 focus:ring-neutral-400" />
          </div>

          <div>
            <label className="block text-xs font-semibold text-neutral-500 mb-1">Query *</label>
            <textarea required rows={3} value={form.query} onChange={e => set('query', e.target.value)}
              placeholder="How long is H-4 EAD currently taking when filed with H-1B renewal on premium processing?"
              className="w-full rounded-lg border border-neutral-200 dark:border-neutral-700 bg-white dark:bg-neutral-800 px-3 py-2 text-sm text-neutral-900 dark:text-neutral-50 focus:outline-none focus:ring-2 focus:ring-neutral-400 resize-none" />
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-xs font-semibold text-neutral-500 mb-1">Category</label>
              <input value={form.category} onChange={e => set('category', e.target.value)}
                placeholder="immigration_operational"
                className="w-full rounded-lg border border-neutral-200 dark:border-neutral-700 bg-white dark:bg-neutral-800 px-3 py-2 text-sm text-neutral-900 dark:text-neutral-50 focus:outline-none focus:ring-2 focus:ring-neutral-400" />
            </div>
            <div>
              <label className="block text-xs font-semibold text-neutral-500 mb-1">Expected primary role</label>
              <select value={form.expected_primary_role} onChange={e => set('expected_primary_role', e.target.value)}
                className="w-full rounded-lg border border-neutral-200 dark:border-neutral-700 bg-white dark:bg-neutral-800 px-3 py-2 text-sm text-neutral-900 dark:text-neutral-50 focus:outline-none focus:ring-2 focus:ring-neutral-400">
                {ROLE_OPTIONS.map(r => <option key={r} value={r}>{r || '—'}</option>)}
              </select>
            </div>
          </div>

          <div>
            <label className="block text-xs font-semibold text-neutral-500 mb-1">
              Expected criteria <span className="font-normal text-neutral-400">(one per line — scored by LLM judge)</span>
            </label>
            <textarea rows={4} value={form.expected_criteria_text} onChange={e => set('expected_criteria_text', e.target.value)}
              placeholder={"Cites practitioner data as primary evidence\nIncludes official USCIS SLA as context\nGives a specific time range (not just 'varies')"}
              className="w-full rounded-lg border border-neutral-200 dark:border-neutral-700 bg-white dark:bg-neutral-800 px-3 py-2 text-sm font-mono text-neutral-900 dark:text-neutral-50 focus:outline-none focus:ring-2 focus:ring-neutral-400 resize-none" />
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-xs font-semibold text-neutral-500 mb-1">Min independent sources</label>
              <input type="number" min={1} value={form.min_independent_sources}
                onChange={e => set('min_independent_sources', e.target.value)}
                placeholder="2"
                className="w-full rounded-lg border border-neutral-200 dark:border-neutral-700 bg-white dark:bg-neutral-800 px-3 py-2 text-sm text-neutral-900 dark:text-neutral-50 focus:outline-none focus:ring-2 focus:ring-neutral-400" />
            </div>
          </div>

          <div>
            <label className="block text-xs font-semibold text-neutral-500 mb-1">Notes</label>
            <textarea rows={2} value={form.notes} onChange={e => set('notes', e.target.value)}
              className="w-full rounded-lg border border-neutral-200 dark:border-neutral-700 bg-white dark:bg-neutral-800 px-3 py-2 text-sm text-neutral-900 dark:text-neutral-50 focus:outline-none focus:ring-2 focus:ring-neutral-400 resize-none" />
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

// ─────────────────────────────────────────────────────────────────────────────
// JSON upload modal
// ─────────────────────────────────────────────────────────────────────────────

function UploadModal({
  authorizedFetch,
  onDone,
  onClose,
}: {
  authorizedFetch: AuthorizedFetch
  onDone: () => void
  onClose: () => void
}) {
  const [text, setText] = useState('')
  const [uploading, setUploading] = useState(false)
  const [result, setResult] = useState<{ created: number; updated: number; reactivated: number; errors: { title: string; error: string }[] } | null>(null)
  const [error, setError] = useState('')

  const EXAMPLE = JSON.stringify([
    {
      title: "Example case title",
      query: "What is the current processing time for X?",
      category: "immigration_operational",
      expected_criteria: ["Cites practitioner data", "Gives specific time range"],
      expected_primary_role: "operational_reality",
      min_independent_sources: 2,
      notes: "Optional notes",
    }
  ], null, 2)

  async function handleUpload() {
    setError('')
    let parsed: unknown
    try {
      parsed = JSON.parse(text)
    } catch {
      setError('Invalid JSON — must be a valid JSON array.')
      return
    }
    if (!Array.isArray(parsed)) {
      setError('JSON must be an array of case objects.')
      return
    }
    setUploading(true)
    try {
      const resp = await authorizedFetch('/admin/evals/cases/upload', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(parsed),
      })
      if (!resp.ok) throw new Error(await readErrorBody(resp, 'Upload failed'))
      setResult(await resp.json())
      onDone()
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Upload failed')
    } finally {
      setUploading(false)
    }
  }

  function handleFile(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0]
    if (!file) return
    const reader = new FileReader()
    reader.onload = ev => setText(ev.target?.result as string ?? '')
    reader.readAsText(file)
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4">
      <div className="w-full max-w-2xl rounded-2xl bg-white dark:bg-neutral-900 shadow-2xl flex flex-col max-h-[90vh]">
        <div className="flex items-center justify-between px-5 py-4 border-b border-neutral-200 dark:border-neutral-800 flex-shrink-0">
          <h3 className="text-sm font-bold text-neutral-900 dark:text-neutral-50">Upload cases — JSON</h3>
          <button type="button" onClick={onClose} className="text-neutral-400 hover:text-neutral-700 dark:hover:text-neutral-200">
            <X size={16} />
          </button>
        </div>

        <div className="overflow-y-auto flex-1 px-5 py-4 space-y-4">
          <div className="text-xs text-neutral-500">
            Paste a JSON array of case objects, or select a <code>.json</code> file.
            Existing cases matched by title are updated; inactive cases are reactivated.
          </div>

          {/* File picker */}
          <div>
            <label className="inline-flex items-center gap-2 cursor-pointer rounded-lg border border-neutral-200 dark:border-neutral-700 px-3 py-2 text-xs font-semibold text-neutral-600 dark:text-neutral-400 hover:bg-neutral-50 dark:hover:bg-neutral-800">
              <Upload size={13} /> Choose .json file
              <input type="file" accept=".json,application/json" className="hidden" onChange={handleFile} />
            </label>
          </div>

          {/* Textarea */}
          <textarea
            rows={12}
            value={text}
            onChange={e => setText(e.target.value)}
            placeholder={EXAMPLE}
            className="w-full rounded-lg border border-neutral-200 dark:border-neutral-700 bg-neutral-50 dark:bg-neutral-800 px-3 py-2 text-xs font-mono text-neutral-900 dark:text-neutral-50 focus:outline-none focus:ring-2 focus:ring-neutral-400 resize-y"
          />

          {error && <p className="text-xs text-red-600 dark:text-red-400">{error}</p>}

          {result && (
            <div className="rounded-lg border border-emerald-200 dark:border-emerald-900 bg-emerald-50 dark:bg-emerald-950/30 px-4 py-3 text-xs">
              <span className="font-semibold text-emerald-800 dark:text-emerald-300">Done —</span>
              {' '}{result.created} created, {result.updated} updated, {result.reactivated} reactivated
              {result.errors.length > 0 && (
                <div className="mt-2 space-y-0.5">
                  {result.errors.map((e, i) => (
                    <p key={i} className="text-red-600 dark:text-red-400">✗ {e.title}: {e.error}</p>
                  ))}
                </div>
              )}
            </div>
          )}
        </div>

        <div className="flex justify-end gap-2 px-5 py-4 border-t border-neutral-200 dark:border-neutral-800 flex-shrink-0">
          <button type="button" onClick={onClose}
            className="rounded-lg border border-neutral-200 dark:border-neutral-700 px-4 py-2 text-sm font-semibold text-neutral-600 dark:text-neutral-400 hover:bg-neutral-50 dark:hover:bg-neutral-800">
            {result ? 'Close' : 'Cancel'}
          </button>
          {!result && (
            <button type="button" disabled={uploading || !text.trim()} onClick={handleUpload}
              className="rounded-lg bg-neutral-900 dark:bg-white px-4 py-2 text-sm font-semibold text-white dark:text-neutral-900 hover:bg-neutral-700 dark:hover:bg-neutral-200 disabled:opacity-50">
              {uploading ? 'Uploading…' : 'Upload'}
            </button>
          )}
        </div>
      </div>
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────────────────
// Eval run result — case result row (expandable)
// ─────────────────────────────────────────────────────────────────────────────

function ScoreBadge({ score }: { score: number | null | undefined }) {
  if (score == null) return <span className="text-neutral-400 text-[11px]">—</span>
  const pctVal = Math.round(score * 100)
  const cls =
    pctVal >= 80 ? 'bg-green-50 text-green-700 dark:bg-green-950/40 dark:text-green-400'
    : pctVal >= 50 ? 'bg-yellow-50 text-yellow-700 dark:bg-yellow-950/40 dark:text-yellow-400'
    : 'bg-red-50 text-red-700 dark:bg-red-950/40 dark:text-red-400'
  return (
    <span className={`inline-block rounded px-1.5 py-0.5 text-[10px] font-semibold tabular-nums ${cls}`}>
      {pctVal}%
    </span>
  )
}

function CaseResultRow({ r }: { r: EvalCaseRunResult }) {
  const [open, setOpen] = useState(false)
  const leg = r.legacy
  const lg = r.langgraph

  return (
    <div className="border border-neutral-200 dark:border-neutral-800 rounded-xl overflow-hidden">
      <button type="button" onClick={() => setOpen(o => !o)}
        className="w-full flex items-center gap-3 px-4 py-3 text-left bg-white dark:bg-neutral-900 hover:bg-neutral-50 dark:hover:bg-neutral-800 transition-colors">
        <span className="flex-shrink-0 text-neutral-400">
          {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        </span>
        <span className="flex-1 min-w-0">
          <span className="text-sm font-semibold text-neutral-900 dark:text-neutral-50 truncate block">{r.title}</span>
          <span className="text-xs text-neutral-400 truncate block">{r.query}</span>
        </span>
        <div className="flex items-center gap-3 flex-shrink-0 text-xs">
          <span className={r.overall_structural_pass ? 'text-green-600 dark:text-green-400 font-semibold' : 'text-red-600 dark:text-red-400 font-semibold'}>
            {r.overall_structural_pass ? '✓ pass' : '✗ fail'}
          </span>
          <span className="text-neutral-400">Legacy</span><ScoreBadge score={leg.criteria?.score} />
          <span className="text-neutral-400">LG</span><ScoreBadge score={lg.criteria?.score} />
        </div>
      </button>

      {open && (
        <div className="px-4 pb-4 pt-2 bg-white dark:bg-neutral-900 border-t border-neutral-100 dark:border-neutral-800 space-y-4">
          <div className="grid grid-cols-2 gap-4">
            {[{ label: 'Legacy', data: leg }, { label: 'LangGraph', data: lg }].map(({ label, data }) => (
              <div key={label} className="rounded-lg border border-neutral-100 dark:border-neutral-800 p-3">
                <p className="text-xs font-bold text-neutral-600 dark:text-neutral-400 mb-2">{label}</p>
                {!data.ok ? (
                  <p className="text-xs text-red-600 dark:text-red-400 font-mono whitespace-pre-wrap break-words">{data.error ?? 'Error'}</p>
                ) : (
                  <dl className="space-y-1 text-xs">
                    <div className="flex justify-between"><dt className="text-neutral-500">Answer length</dt><dd className="font-semibold">{data.answer_length.toLocaleString()} chars</dd></div>
                    <div className="flex justify-between"><dt className="text-neutral-500">Evidence</dt><dd className="font-semibold">{data.evidence_count} items</dd></div>
                    <div className="flex justify-between"><dt className="text-neutral-500">Claims</dt><dd className="font-semibold">{data.claim_count}</dd></div>
                    <div className="flex justify-between"><dt className="text-neutral-500">Latency</dt><dd className="font-semibold">{(data.latency_ms / 1000).toFixed(1)}s</dd></div>
                    {data.criteria && (
                      <>
                        <div className="flex justify-between"><dt className="text-neutral-500">Criteria score</dt><dd><ScoreBadge score={data.criteria.score} /></dd></div>
                        {data.criteria.passed.map((p, i) => <p key={i} className="text-green-700 dark:text-green-400 text-[11px]">✓ {p}</p>)}
                        {data.criteria.failed.map((p, i) => <p key={i} className="text-red-600 dark:text-red-400 text-[11px]">✗ {p}</p>)}
                        <div><dt className="text-neutral-500">Explanation</dt><dd className="text-neutral-700 dark:text-neutral-300 mt-0.5">{data.criteria.explanation}</dd></div>
                      </>
                    )}
                  </dl>
                )}
              </div>
            ))}
          </div>
          <div>
            <p className="text-xs font-bold text-neutral-500 mb-2">Structural checks</p>
            <div className="grid grid-cols-2 gap-x-4 gap-y-1">
              {Object.entries(r.structural).map(([k, v]) => (
                <div key={k} className="flex items-center gap-1.5 text-xs">
                  <span className={v ? 'text-green-600 dark:text-green-400' : 'text-red-600 dark:text-red-400'}>{v ? '✓' : '✗'}</span>
                  <span className="text-neutral-600 dark:text-neutral-400 font-mono">{k}</span>
                </div>
              ))}
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────────────────
// Main: EvalHarnessTab
// ─────────────────────────────────────────────────────────────────────────────

export function EvalHarnessTab({ authorizedFetch }: { authorizedFetch: AuthorizedFetch }) {
  // ── Cases state ────────────────────────────────────────────────────────────
  const [cases, setCases] = useState<EvalCase[]>([])
  const [inactiveCases, setInactiveCases] = useState<EvalCase[]>([])
  const [showInactive, setShowInactive] = useState(false)
  const [loadingCases, setLoadingCases] = useState(true)
  const [casesError, setCasesError] = useState('')
  const [modal, setModal] = useState<'create' | 'upload' | EvalCase | null>(null)
  const [deactivating, setDeactivating] = useState<number | null>(null)
  const [restoring, setRestoring] = useState<number | null>(null)

  // ── Run state ──────────────────────────────────────────────────────────────
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set())
  const [runStatus, setRunStatus] = useState<'idle' | 'running' | 'complete' | 'error'>('idle')
  const [log, setLog] = useState<string[]>([])
  const [runResult, setRunResult] = useState<EvalRunResult | null>(null)
  const [runs, setRuns] = useState<EvalRunSummary[]>([])
  const [runError, setRunError] = useState('')
  const [langsmithLinks, setLangsmithLinks] = useState<{ legacy?: string; langgraph?: string }>({})
  const logRef = useRef<HTMLDivElement>(null)
  const abortRef = useRef<AbortController | null>(null)

  // ── Auto-scroll log ────────────────────────────────────────────────────────
  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight
  }, [log])

  // ── Load cases ─────────────────────────────────────────────────────────────
  const loadCases = useCallback(async () => {
    setLoadingCases(true)
    try {
      const [activeResp, allResp] = await Promise.all([
        authorizedFetch('/admin/evals/cases'),
        authorizedFetch('/admin/evals/cases?include_inactive=true'),
      ])
      if (!activeResp.ok) throw new Error(await readErrorBody(activeResp, 'Could not load eval cases'))
      const activeData = await activeResp.json()
      setCases(activeData.items ?? [])
      if (allResp.ok) {
        const allData = await allResp.json()
        const allItems: EvalCase[] = allData.items ?? []
        setInactiveCases(allItems.filter(c => !c.is_active))
      }
      setCasesError('')
    } catch (err: unknown) {
      setCasesError(err instanceof Error ? err.message : 'Load failed')
    } finally {
      setLoadingCases(false)
    }
  }, [authorizedFetch])

  const loadRuns = useCallback(async () => {
    try {
      const resp = await authorizedFetch('/admin/evals/runs')
      if (!resp.ok) return
      const data = await resp.json()
      setRuns(data.runs ?? [])
    } catch {}
  }, [authorizedFetch])

  useEffect(() => {
    loadCases()
    loadRuns()
  }, [loadCases, loadRuns])

  // ── Case CRUD ──────────────────────────────────────────────────────────────
  async function handleSave(form: FormState, existing?: EvalCase) {
    const payload = {
      title: form.title,
      query: form.query,
      category: form.category || null,
      expected_criteria: form.expected_criteria_text.split('\n').map(s => s.trim()).filter(Boolean),
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
    await loadCases()
  }

  async function handleDeactivate(id: number) {
    if (!confirm('Deactivate this eval case? It will be hidden from runs but not deleted.')) return
    setDeactivating(id)
    try {
      const resp = await authorizedFetch(`/admin/evals/cases/${id}`, { method: 'DELETE' })
      if (!resp.ok) throw new Error(await readErrorBody(resp, 'Deactivate failed'))
      setSelectedIds(prev => { const next = new Set(prev); next.delete(id); return next })
      await loadCases()
    } catch (err: unknown) {
      setCasesError(err instanceof Error ? err.message : 'Deactivate failed')
    } finally {
      setDeactivating(null)
    }
  }

  async function handleRestore(id: number) {
    setRestoring(id)
    try {
      const resp = await authorizedFetch(`/admin/evals/cases/${id}/restore`, { method: 'POST' })
      if (!resp.ok) throw new Error(await readErrorBody(resp, 'Restore failed'))
      await loadCases()
    } catch (err: unknown) {
      setCasesError(err instanceof Error ? err.message : 'Restore failed')
    } finally {
      setRestoring(null)
    }
  }

  // ── Selection helpers ──────────────────────────────────────────────────────
  function toggleCase(id: number) {
    setSelectedIds(prev => {
      const next = new Set(prev)
      next.has(id) ? next.delete(id) : next.add(id)
      return next
    })
  }

  function toggleAll() {
    if (selectedIds.size === cases.length) {
      setSelectedIds(new Set())
    } else {
      setSelectedIds(new Set(cases.map(c => c.id)))
    }
  }

  // ── Run ────────────────────────────────────────────────────────────────────
  async function startRun() {
    setRunStatus('running')
    setLog([])
    setRunResult(null)
    setRunError('')
    setLangsmithLinks({})

    const payload = selectedIds.size > 0 ? { case_ids: Array.from(selectedIds) } : {}
    const startResp = await authorizedFetch('/admin/evals/runs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
    if (!startResp.ok) {
      const msg = await readErrorBody(startResp, 'Failed to start eval run')
      setRunError(msg)
      setRunStatus('error')
      return
    }
    const { run_id } = await startResp.json()
    const abort = new AbortController()
    abortRef.current = abort
    const streamResp = await authorizedFetch(`/admin/evals/runs/${run_id}/stream`, { signal: abort.signal })
    if (!streamResp.ok || !streamResp.body) {
      setRunError('Could not open SSE stream')
      setRunStatus('error')
      return
    }
    const reader = streamResp.body.getReader()
    const decoder = new TextDecoder()
    let buf = ''
    try {
      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        buf += decoder.decode(value, { stream: true })
        const parts = buf.split('\n\n')
        buf = parts.pop() ?? ''
        for (const part of parts) {
          const dataLine = part.split('\n').find(l => l.startsWith('data:'))
          if (!dataLine) continue
          const payload = dataLine.slice('data:'.length).trim()
          if (!payload || payload === '{}') continue
          try { handleSSEEvent(JSON.parse(payload)) } catch {}
        }
      }
    } catch (err: unknown) {
      if ((err as Error)?.name !== 'AbortError') {
        setRunError('Stream interrupted')
        setRunStatus('error')
      }
    }
    await loadRuns()
  }

  function handleSSEEvent(ev: Record<string, unknown>) {
    switch (ev.type) {
      case 'started':
        setLog(l => [...l, `▶ Run started — ${ev.total} case(s)${ev.mode === 'langsmith' ? ' [LangSmith]' : ''}`])
        break
      case 'case_start':
        setLog(l => [...l, `  [${(ev.index as number) + 1}/${ev.total}] ${ev.title}`])
        break
      case 'case_result': {
        const r = ev.result as EvalCaseRunResult
        const legScore = pct(r.legacy.criteria?.score)
        const lgScore = pct(r.langgraph.criteria?.score)
        setLog(l => [...l, `  → Legacy ${r.legacy.ok ? '✓' : '✗'} (${legScore})  LG ${r.langgraph.ok ? '✓' : '✗'} (${lgScore})`])
        setRunResult(prev => ({ mode: 'in_process', cases: [...(prev?.cases ?? []), r], langsmith: null }))
        break
      }
      case 'langsmith_sync':
        setLog(l => [...l, `  ⟳ ${ev.message}`])
        break
      case 'langsmith_sync_done':
        setLog(l => [...l, `  ✓ Dataset synced`])
        break
      case 'langsmith_pipeline_start':
        setLog(l => [...l, `  ▶ Running ${ev.pipeline} pipeline via LangSmith…`])
        break
      case 'langsmith_pipeline_done': {
        const url = ev.experiment_url as string | undefined
        if (url) setLangsmithLinks(prev => ({ ...prev, [ev.pipeline as string]: url }))
        setLog(l => [...l, `  ✓ ${ev.pipeline} done${ev.elapsed_s ? ` (${ev.elapsed_s}s)` : ''}${url ? ' — experiment ready' : ''}`])
        break
      }
      case 'langsmith_pipeline_error':
        setLog(l => [...l, `  ✗ ${ev.pipeline} error: ${ev.error}`])
        break
      case 'complete': {
        const envelope = ev.results as EvalRunResult | undefined
        if (envelope) setRunResult(envelope)
        setLog(l => [...l, '✓ Run complete'])
        setRunStatus('complete')
        break
      }
      case 'error':
        setLog(l => [...l, `✗ Error: ${ev.error}`])
        setRunStatus('error')
        break
    }
  }

  const canRun = cases.length > 0 && runStatus !== 'running'
  const runLabel = selectedIds.size > 0
    ? `Run selected (${selectedIds.size})`
    : `Run all ${cases.length}`

  // ── Render ─────────────────────────────────────────────────────────────────
  return (
    <div className="space-y-6">

      {/* ─── Cases section ─────────────────────────────────────────────────── */}
      <div>
        <div className="flex items-center justify-between mb-3">
          <div>
            <h3 className="text-sm font-bold text-neutral-900 dark:text-neutral-50">
              Eval cases <span className="font-normal text-neutral-400">({cases.length} active)</span>
            </h3>
            <p className="text-xs text-neutral-500 mt-0.5">Select cases to run, or leave all unselected to run everything.</p>
          </div>
          <div className="flex items-center gap-2">
            <button type="button" onClick={() => setModal('upload')}
              className="flex items-center gap-1.5 rounded-lg border border-neutral-200 dark:border-neutral-700 px-3 py-2 text-xs font-semibold text-neutral-600 dark:text-neutral-400 hover:bg-neutral-50 dark:hover:bg-neutral-800">
              <Upload size={12} /> Upload JSON
            </button>
            <button type="button" onClick={() => setModal('create')}
              className="flex items-center gap-1.5 rounded-lg bg-neutral-900 dark:bg-white px-3 py-2 text-xs font-semibold text-white dark:text-neutral-900 hover:bg-neutral-700 dark:hover:bg-neutral-200">
              <Plus size={13} /> New case
            </button>
          </div>
        </div>

        {casesError && <p className="mb-3 text-xs text-red-600 dark:text-red-400">{casesError}</p>}

        {loadingCases ? (
          <p className="text-sm text-neutral-400">Loading…</p>
        ) : cases.length === 0 && inactiveCases.length === 0 ? (
          <div className="rounded-xl border border-dashed border-neutral-300 dark:border-neutral-700 p-8 text-center">
            <p className="text-sm text-neutral-500">No eval cases yet.</p>
            <button type="button" onClick={() => setModal('create')}
              className="mt-3 text-xs font-semibold text-neutral-700 dark:text-neutral-300 underline underline-offset-2">
              Create the first one
            </button>
          </div>
        ) : (
          <div className="rounded-xl border border-neutral-200 dark:border-neutral-800 overflow-hidden divide-y divide-neutral-100 dark:divide-neutral-800">
            {/* Select-all header */}
            {cases.length > 0 && (
              <div className="flex items-center gap-3 px-4 py-2 bg-neutral-50 dark:bg-neutral-950 text-xs text-neutral-500">
                <input type="checkbox"
                  checked={selectedIds.size === cases.length && cases.length > 0}
                  onChange={toggleAll}
                  className="rounded border-neutral-300 dark:border-neutral-600" />
                <span>{selectedIds.size > 0 ? `${selectedIds.size} selected` : 'Select all'}</span>
              </div>
            )}

            {/* Active cases */}
            {cases.map(c => (
              <div key={c.id} className="flex items-start gap-3 px-4 py-3 bg-white dark:bg-neutral-900 hover:bg-neutral-50 dark:hover:bg-neutral-800 transition-colors">
                <input type="checkbox" checked={selectedIds.has(c.id)} onChange={() => toggleCase(c.id)}
                  className="mt-0.5 rounded border-neutral-300 dark:border-neutral-600 text-neutral-900" />
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
                    className="grid h-7 w-7 place-items-center rounded-lg text-neutral-400 hover:bg-neutral-100 dark:hover:bg-neutral-700 hover:text-neutral-700 dark:hover:text-neutral-200"
                    title="Edit">
                    <Edit2 size={13} />
                  </button>
                  <button type="button" onClick={() => handleDeactivate(c.id)} disabled={deactivating === c.id}
                    className="grid h-7 w-7 place-items-center rounded-lg text-neutral-400 hover:bg-amber-50 dark:hover:bg-amber-950/30 hover:text-amber-600 dark:hover:text-amber-400 disabled:opacity-40"
                    title="Deactivate (soft delete)">
                    <EyeOff size={13} />
                  </button>
                </div>
              </div>
            ))}

            {/* Inactive toggle + rows */}
            {inactiveCases.length > 0 && (
              <>
                <button type="button" onClick={() => setShowInactive(s => !s)}
                  className="w-full flex items-center gap-2 px-4 py-2 text-xs font-semibold text-neutral-400 hover:text-neutral-600 dark:hover:text-neutral-300 bg-neutral-50 dark:bg-neutral-950 hover:bg-neutral-100 dark:hover:bg-neutral-900 transition-colors">
                  {showInactive ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
                  {inactiveCases.length} inactive case{inactiveCases.length !== 1 ? 's' : ''}
                </button>

                {showInactive && inactiveCases.map(c => (
                  <div key={c.id} className="flex items-start gap-3 px-4 py-3 bg-neutral-50 dark:bg-neutral-900/50 opacity-60">
                    <div className="w-4 flex-shrink-0" /> {/* spacer for checkbox column */}
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2">
                        <span className="text-sm font-semibold text-neutral-500 truncate line-through">{c.title}</span>
                        <span className="text-[10px] font-semibold text-neutral-400 bg-neutral-200 dark:bg-neutral-700 rounded px-1.5 py-0.5">inactive</span>
                      </div>
                      <p className="text-xs text-neutral-400 mt-0.5 line-clamp-1">{c.query}</p>
                    </div>
                    <button type="button" onClick={() => handleRestore(c.id)} disabled={restoring === c.id}
                      className="flex items-center gap-1 h-7 px-2 rounded-lg text-xs font-semibold text-neutral-400 hover:bg-neutral-200 dark:hover:bg-neutral-700 hover:text-neutral-700 dark:hover:text-neutral-200 disabled:opacity-40"
                      title="Restore">
                      <RotateCcw size={12} /> Restore
                    </button>
                  </div>
                ))}
              </>
            )}
          </div>
        )}
      </div>

      {/* ─── Run section ───────────────────────────────────────────────────── */}
      <div className="space-y-4">
        <LangSmithBanner authorizedFetch={authorizedFetch} />

        <div className="flex items-center gap-3">
          <button type="button" disabled={!canRun} onClick={startRun}
            className="flex items-center gap-1.5 rounded-lg bg-neutral-900 dark:bg-white px-4 py-2 text-sm font-semibold text-white dark:text-neutral-900 hover:bg-neutral-700 dark:hover:bg-neutral-200 disabled:opacity-40">
            <Play size={13} />
            {runStatus === 'running' ? 'Running…' : runLabel}
          </button>
          {selectedIds.size > 0 && (
            <button type="button" onClick={() => setSelectedIds(new Set())}
              className="text-xs text-neutral-400 hover:text-neutral-600 dark:hover:text-neutral-300 underline underline-offset-2">
              Clear selection
            </button>
          )}
        </div>

        {/* Progress log */}
        {(log.length > 0 || runStatus === 'running') && (
          <div>
            <p className="text-xs font-bold text-neutral-500 mb-2">Progress</p>
            <div ref={logRef}
              className="rounded-xl bg-neutral-950 text-green-300 font-mono text-xs p-4 max-h-52 overflow-y-auto space-y-0.5">
              {log.map((line, i) => <div key={i}>{line}</div>)}
              {runStatus === 'running' && <div className="animate-pulse">⋯</div>}
            </div>
            {runError && <p className="mt-2 text-xs text-red-600 dark:text-red-400">{runError}</p>}
          </div>
        )}

        {/* LangSmith experiment links */}
        {(langsmithLinks.legacy || langsmithLinks.langgraph) && (
          <div className="rounded-xl border border-emerald-200 dark:border-emerald-900 bg-emerald-50 dark:bg-emerald-950/30 px-4 py-3 space-y-1">
            <p className="text-xs font-bold text-emerald-800 dark:text-emerald-300 mb-2">LangSmith experiments</p>
            {langsmithLinks.legacy && (
              <a href={langsmithLinks.legacy} target="_blank" rel="noopener noreferrer"
                className="flex items-center gap-1.5 text-xs text-emerald-700 dark:text-emerald-400 hover:underline">
                <ExternalLink size={11} /> Legacy pipeline experiment
              </a>
            )}
            {langsmithLinks.langgraph && (
              <a href={langsmithLinks.langgraph} target="_blank" rel="noopener noreferrer"
                className="flex items-center gap-1.5 text-xs text-emerald-700 dark:text-emerald-400 hover:underline">
                <ExternalLink size={11} /> LangGraph pipeline experiment
              </a>
            )}
          </div>
        )}

        {/* Per-case results */}
        {(runResult?.cases?.length ?? 0) > 0 && (
          <div>
            <p className="text-xs font-bold text-neutral-500 mb-2">
              Results — {runResult!.cases.filter(r => r.overall_structural_pass).length}/{runResult!.cases.length} structural pass
            </p>
            <div className="space-y-2">
              {runResult!.cases.map(r => <CaseResultRow key={r.case_id} r={r} />)}
            </div>
          </div>
        )}
        {runResult?.mode === 'langsmith' && runStatus === 'complete' && (
          <p className="text-xs text-neutral-500 italic">
            Per-case rows are in LangSmith. Use the experiment links above to view detailed results.
          </p>
        )}

        {/* Run history */}
        {runs.length > 0 && (
          <div>
            <p className="text-xs font-bold text-neutral-500 mb-2">Run history</p>
            <div className="rounded-xl border border-neutral-200 dark:border-neutral-800 overflow-hidden divide-y divide-neutral-100 dark:divide-neutral-800">
              {runs.map(r => (
                <div key={r.run_id} className="flex items-center gap-3 px-4 py-3 bg-white dark:bg-neutral-900 text-xs">
                  <span className={`h-2 w-2 rounded-full flex-shrink-0 ${
                    r.status === 'complete' ? 'bg-green-500'
                    : r.status === 'error' ? 'bg-red-500'
                    : 'bg-yellow-400 animate-pulse'
                  }`} />
                  <span className="flex-1 font-mono text-neutral-500 truncate">{r.run_id}</span>
                  <span className="text-neutral-500">{r.case_count} case(s)</span>
                  <span className={`font-semibold ${
                    r.status === 'complete' ? 'text-green-700 dark:text-green-400'
                    : r.status === 'error' ? 'text-red-600 dark:text-red-400'
                    : 'text-yellow-600 dark:text-yellow-400'
                  }`}>{r.status}</span>
                  {r.started_at && <span className="text-neutral-400">{new Date(r.started_at).toLocaleString()}</span>}
                </div>
              ))}
            </div>
          </div>
        )}
      </div>

      {/* ─── Modals ─────────────────────────────────────────────────────────── */}
      {modal === 'create' && (
        <CaseModal
          onSave={form => handleSave(form)}
          onClose={() => setModal(null)}
        />
      )}
      {modal === 'upload' && (
        <UploadModal
          authorizedFetch={authorizedFetch}
          onDone={loadCases}
          onClose={() => setModal(null)}
        />
      )}
      {modal !== null && modal !== 'create' && modal !== 'upload' && (
        <CaseModal
          initial={modal as EvalCase}
          onSave={form => handleSave(form, modal as EvalCase)}
          onClose={() => setModal(null)}
        />
      )}
    </div>
  )
}
