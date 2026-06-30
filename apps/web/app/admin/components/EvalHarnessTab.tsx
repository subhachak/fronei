'use client'

import {
  ChevronDown,
  ChevronRight,
  ChevronsDownUp,
  ChevronsUpDown,
  Download,
  Edit2,
  ExternalLink,
  EyeOff,
  Loader2,
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
// Constants
// ─────────────────────────────────────────────────────────────────────────────

const POLL_MS = 3000

const ROLE_OPTIONS = [
  '', 'official_policy', 'operational_reality', 'statistical_data',
  'expert_opinion', 'anecdotal', 'conflicting',
]

const DEFAULT_FORM = {
  title: '', query: '', category: '', expected_criteria_text: '',
  expected_primary_role: '', min_independent_sources: '', notes: '',
}
type FormState = typeof DEFAULT_FORM

// ─────────────────────────────────────────────────────────────────────────────
// Helpers
// ─────────────────────────────────────────────────────────────────────────────

function avg(nums: (number | null | undefined)[]): number | null {
  const valid = nums.filter((n): n is number => n != null)
  return valid.length ? valid.reduce((a, b) => a + b, 0) / valid.length : null
}

function pctStr(v: number | null | undefined) {
  return v == null ? '—' : `${Math.round(v * 100)}%`
}

function fmtMs(ms: number | null | undefined) {
  if (ms == null) return '—'
  return ms < 1000 ? `${ms}ms` : `${(ms / 1000).toFixed(1)}s`
}

// ─────────────────────────────────────────────────────────────────────────────
// LangSmith banner
// ─────────────────────────────────────────────────────────────────────────────

type LangSmithStatus = { configured: boolean; project: string | null; tracing_on: boolean; dataset_name: string }

function LangSmithBanner({ authorizedFetch, onStatus }: {
  authorizedFetch: AuthorizedFetch
  onStatus?: (configured: boolean) => void
}) {
  const [status, setStatus] = useState<LangSmithStatus | null>(null)
  useEffect(() => {
    authorizedFetch('/admin/evals/langsmith/status')
      .then(r => r.ok ? r.json() : null)
      .then(d => {
        if (d) { setStatus(d); onStatus?.(d.configured) }
      })
      .catch(() => {})
  }, [authorizedFetch]) // eslint-disable-line react-hooks/exhaustive-deps
  if (!status) return null
  if (!status.configured) return (
    <div className="rounded-lg border border-neutral-200 dark:border-neutral-700 bg-neutral-50 dark:bg-neutral-900 px-4 py-3 text-xs text-neutral-500">
      <span className="font-semibold text-neutral-700 dark:text-neutral-300">LangSmith not configured</span>
      {' — set '}<code className="bg-neutral-100 dark:bg-neutral-800 px-1 rounded">LANGSMITH_API_KEY</code>{' to enable experiment tracking.'}
    </div>
  )
  return (
    <div className="rounded-lg border border-emerald-200 dark:border-emerald-900 bg-emerald-50 dark:bg-emerald-950/30 px-4 py-3 text-xs flex items-center gap-3">
      <span className="h-2 w-2 rounded-full bg-emerald-500 flex-shrink-0" />
      <div className="flex-1">
        <span className="font-semibold text-emerald-800 dark:text-emerald-300">LangSmith active</span>
        {' — project '}<code className="bg-emerald-100 dark:bg-emerald-900/50 px-1 rounded text-emerald-700 dark:text-emerald-400">{status.project}</code>
        {', dataset '}<code className="bg-emerald-100 dark:bg-emerald-900/50 px-1 rounded text-emerald-700 dark:text-emerald-400">{status.dataset_name}</code>
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
// Local report (post-run summary)
// ─────────────────────────────────────────────────────────────────────────────

function LocalReport({ cases, runResult }: { cases: EvalCaseRunResult[]; runResult: EvalRunResult }) {
  if (!cases.length) return null
  const structPass = cases.filter(r => r.overall_structural_pass).length
  const legAvg = avg(cases.map(r => r.legacy.criteria?.score))
  const lgAvg = avg(cases.map(r => r.langgraph.criteria?.score))
  const byCategory: Record<string, EvalCaseRunResult[]> = {}
  for (const r of cases) {
    const key = (r as Record<string, unknown>)['category'] as string | null ?? 'Uncategorized'
    ;(byCategory[key] ??= []).push(r)
  }
  return (
    <div className="rounded-xl border border-neutral-200 dark:border-neutral-800 overflow-hidden">
      <div className={`flex items-center justify-between px-4 py-3 border-b border-neutral-200 dark:border-neutral-800 ${
        structPass === cases.length ? 'bg-emerald-50 dark:bg-emerald-950/20' : 'bg-amber-50 dark:bg-amber-950/20'
      }`}>
        <span className="text-sm font-bold text-neutral-900 dark:text-neutral-50">
          {structPass}/{cases.length} structural pass
        </span>
        <div className="flex items-center gap-4 text-xs text-neutral-600 dark:text-neutral-400">
          {legAvg != null && <span>Legacy criteria <strong>{pctStr(legAvg)}</strong></span>}
          {lgAvg != null && <span>LG criteria <strong>{pctStr(lgAvg)}</strong></span>}
        </div>
      </div>
      {Object.keys(byCategory).length > 1 && (
        <div className="divide-y divide-neutral-100 dark:divide-neutral-800">
          {Object.entries(byCategory).map(([cat, rows]) => (
            <div key={cat} className="flex items-center gap-4 px-4 py-2 bg-white dark:bg-neutral-900 text-xs">
              <span className="flex-1 font-medium text-neutral-700 dark:text-neutral-300">{cat}</span>
              <span className="text-neutral-500">{rows.filter(r => r.overall_structural_pass).length}/{rows.length} pass</span>
              {avg(rows.map(r => r.legacy.criteria?.score)) != null && (
                <span className="text-neutral-400">Legacy {pctStr(avg(rows.map(r => r.legacy.criteria?.score)))}</span>
              )}
              {avg(rows.map(r => r.langgraph.criteria?.score)) != null && (
                <span className="text-neutral-400">LG {pctStr(avg(rows.map(r => r.langgraph.criteria?.score)))}</span>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────────────────
// Score badge
// ─────────────────────────────────────────────────────────────────────────────

function ScoreBadge({ score }: { score: number | null | undefined }) {
  if (score == null) return <span className="text-neutral-400 text-[11px]">—</span>
  const v = Math.round(score * 100)
  const cls = v >= 80 ? 'bg-green-50 text-green-700 dark:bg-green-950/40 dark:text-green-400'
    : v >= 50 ? 'bg-yellow-50 text-yellow-700 dark:bg-yellow-950/40 dark:text-yellow-400'
    : 'bg-red-50 text-red-700 dark:bg-red-950/40 dark:text-red-400'
  return <span className={`inline-block rounded px-1.5 py-0.5 text-[10px] font-semibold tabular-nums ${cls}`}>{v}%</span>
}

// ─────────────────────────────────────────────────────────────────────────────
// Case run-history panel (lazy-fetched)
// ─────────────────────────────────────────────────────────────────────────────

type CaseHistoryEntry = {
  run_id: string
  status: string
  started_at: string | null
  overall_structural_pass: boolean | null
  legacy: { ok: boolean | null; answer_length: number | null; evidence_count: number | null; claim_count: number | null; latency_ms: number | null; criteria_score: number | null; criteria_passed: string[]; criteria_failed: string[]; answer: string }
  langgraph: { ok: boolean | null; answer_length: number | null; evidence_count: number | null; claim_count: number | null; latency_ms: number | null; criteria_score: number | null; criteria_passed: string[]; criteria_failed: string[]; answer: string }
}

function CaseRunHistory({ caseId, authorizedFetch }: { caseId: number; authorizedFetch: AuthorizedFetch }) {
  const [loaded, setLoaded] = useState(false)
  const [loading, setLoading] = useState(false)
  const [history, setHistory] = useState<CaseHistoryEntry[]>([])
  const [error, setError] = useState('')
  const [expandedRun, setExpandedRun] = useState<string | null>(null)

  async function load() {
    if (loaded) return
    setLoading(true)
    try {
      const resp = await authorizedFetch(`/admin/evals/cases/${caseId}/history`)
      if (!resp.ok) throw new Error(await readErrorBody(resp, 'Failed to load history'))
      const data = await resp.json()
      setHistory(data.history ?? [])
      setLoaded(true)
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Load failed')
    } finally {
      setLoading(false)
    }
  }

  // Load on mount
  useEffect(() => { load() }, []) // eslint-disable-line react-hooks/exhaustive-deps

  if (loading) return <p className="text-xs text-neutral-400 py-2">Loading history…</p>
  if (error) return <p className="text-xs text-red-500 py-2">{error}</p>
  if (!history.length) return <p className="text-xs text-neutral-400 italic py-2">No previous runs found for this case.</p>

  return (
    <div className="space-y-1.5">
      {history.map(entry => {
        const isOpen = expandedRun === entry.run_id
        return (
          <div key={entry.run_id} className="rounded-lg border border-neutral-100 dark:border-neutral-800 overflow-hidden">
            {/* Row header */}
            <button type="button" onClick={() => setExpandedRun(isOpen ? null : entry.run_id)}
              className="w-full flex items-center gap-2 px-3 py-2 bg-white dark:bg-neutral-900 hover:bg-neutral-50 dark:hover:bg-neutral-800 text-xs text-left transition-colors">
              {isOpen ? <ChevronDown size={12} className="text-neutral-400 flex-shrink-0" /> : <ChevronRight size={12} className="text-neutral-400 flex-shrink-0" />}
              <span className="font-mono text-neutral-500 truncate flex-1">{entry.run_id}</span>
              {entry.started_at && (
                <span className="text-neutral-400 flex-shrink-0">{new Date(entry.started_at).toLocaleString()}</span>
              )}
              <span className={`font-semibold flex-shrink-0 ${entry.overall_structural_pass ? 'text-green-600 dark:text-green-400' : 'text-red-600 dark:text-red-400'}`}>
                {entry.overall_structural_pass ? '✓' : '✗'}
              </span>
              <span className="flex-shrink-0"><ScoreBadge score={entry.legacy.criteria_score} /></span>
              <span className="text-neutral-400 flex-shrink-0 text-[10px]">→</span>
              <span className="flex-shrink-0"><ScoreBadge score={entry.langgraph.criteria_score} /></span>
            </button>

            {/* Expanded detail */}
            {isOpen && (
              <div className="border-t border-neutral-100 dark:border-neutral-800 px-3 pb-3 pt-2 bg-white dark:bg-neutral-900 space-y-3">
                <div className="grid grid-cols-2 gap-3">
                  {([['Legacy', entry.legacy], ['LangGraph', entry.langgraph]] as const).map(([label, data]) => (
                    <div key={label} className="rounded-md border border-neutral-100 dark:border-neutral-800 p-2">
                      <p className="text-[11px] font-bold text-neutral-500 uppercase tracking-wide mb-1.5">{label}</p>
                      {!data.ok ? (
                        <p className="text-[11px] text-red-500">Pipeline error</p>
                      ) : (
                        <dl className="space-y-1 text-[11px]">
                          <div className="flex justify-between"><dt className="text-neutral-400">Ans length</dt><dd className="font-mono">{data.answer_length?.toLocaleString()} chars</dd></div>
                          <div className="flex justify-between"><dt className="text-neutral-400">Evidence</dt><dd className="font-mono">{data.evidence_count} items</dd></div>
                          <div className="flex justify-between"><dt className="text-neutral-400">Claims</dt><dd className="font-mono">{data.claim_count}</dd></div>
                          <div className="flex justify-between"><dt className="text-neutral-400">Latency</dt><dd className="font-mono">{fmtMs(data.latency_ms)}</dd></div>
                          {data.criteria_score != null && (
                            <div className="flex justify-between"><dt className="text-neutral-400">Criteria</dt><dd><ScoreBadge score={data.criteria_score} /></dd></div>
                          )}
                          {data.criteria_passed.map((p, i) => <p key={i} className="text-green-600 dark:text-green-400">✓ {p}</p>)}
                          {data.criteria_failed.map((p, i) => <p key={i} className="text-red-500">✗ {p}</p>)}
                          {data.answer && (
                            <div>
                              <dt className="text-neutral-400 mt-1">Answer snippet</dt>
                              <dd className="mt-0.5 text-neutral-600 dark:text-neutral-300 line-clamp-4 leading-relaxed">{data.answer}</dd>
                            </div>
                          )}
                        </dl>
                      )}
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>
        )
      })}
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────────────────
// Per-case result row (expandable, with answer + history)
// ─────────────────────────────────────────────────────────────────────────────

function CaseResultRow({ r, authorizedFetch }: { r: EvalCaseRunResult; authorizedFetch: AuthorizedFetch }) {
  const [open, setOpen] = useState(false)
  const [tab, setTab] = useState<'current' | 'history'>('current')
  const leg = r.legacy
  const lg = r.langgraph

  return (
    <div className="border border-neutral-200 dark:border-neutral-800 rounded-xl overflow-hidden">
      {/* Summary row */}
      <button type="button" onClick={() => setOpen(o => !o)}
        className="w-full flex items-center gap-3 px-4 py-3 text-left bg-white dark:bg-neutral-900 hover:bg-neutral-50 dark:hover:bg-neutral-800 transition-colors">
        <span className="flex-shrink-0 text-neutral-400">{open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}</span>
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
        <div className="border-t border-neutral-100 dark:border-neutral-800 bg-white dark:bg-neutral-900">
          {/* Tab bar */}
          <div className="flex border-b border-neutral-100 dark:border-neutral-800 px-4 pt-2 gap-3">
            {(['current', 'history'] as const).map(t => (
              <button key={t} type="button" onClick={() => setTab(t)}
                className={`pb-2 text-xs font-semibold border-b-2 -mb-px transition-colors ${
                  tab === t ? 'border-neutral-900 dark:border-white text-neutral-900 dark:text-white'
                    : 'border-transparent text-neutral-400 hover:text-neutral-600 dark:hover:text-neutral-300'
                }`}>
                {t === 'current' ? 'This run' : 'Run history'}
              </button>
            ))}
          </div>

          {/* Current run detail */}
          {tab === 'current' && (
            <div className="px-4 pb-4 pt-3 space-y-4">
              {/* Side-by-side pipeline */}
              <div className="grid grid-cols-2 gap-4">
                {[{ label: 'Legacy', data: leg }, { label: 'LangGraph', data: lg }].map(({ label, data }) => (
                  <div key={label} className="rounded-lg border border-neutral-100 dark:border-neutral-800 p-3">
                    <p className="text-xs font-bold text-neutral-600 dark:text-neutral-400 mb-2">{label}</p>
                    {!data.ok ? (
                      <p className="text-xs text-red-600 dark:text-red-400 font-mono whitespace-pre-wrap break-words">{data.error ?? 'Error'}</p>
                    ) : (
                      <dl className="space-y-1.5 text-xs">
                        <div className="flex justify-between"><dt className="text-neutral-500">Answer length</dt><dd className="font-semibold">{data.answer_length.toLocaleString()} chars</dd></div>
                        <div className="flex justify-between"><dt className="text-neutral-500">Evidence</dt><dd className="font-semibold">{data.evidence_count} items</dd></div>
                        <div className="flex justify-between"><dt className="text-neutral-500">Claims</dt><dd className="font-semibold">{data.claim_count}</dd></div>
                        <div className="flex justify-between"><dt className="text-neutral-500">Latency</dt><dd className="font-semibold">{fmtMs(data.latency_ms)}</dd></div>
                        {data.criteria && (
                          <>
                            <div className="flex justify-between"><dt className="text-neutral-500">Criteria score</dt><dd><ScoreBadge score={data.criteria.score} /></dd></div>
                            {data.criteria.passed.map((p, i) => <p key={i} className="text-green-700 dark:text-green-400 text-[11px]">✓ {p}</p>)}
                            {data.criteria.failed.map((p, i) => <p key={i} className="text-red-600 dark:text-red-400 text-[11px]">✗ {p}</p>)}
                            {data.criteria.explanation && (
                              <div className="pt-1 border-t border-neutral-100 dark:border-neutral-800">
                                <dt className="text-neutral-500 mb-0.5">Explanation</dt>
                                <dd className="text-neutral-700 dark:text-neutral-300 leading-relaxed">{data.criteria.explanation}</dd>
                              </div>
                            )}
                          </>
                        )}
                        {data.answer && (
                          <div className="pt-1 border-t border-neutral-100 dark:border-neutral-800">
                            <dt className="text-neutral-500 mb-0.5">Answer</dt>
                            <dd className="text-neutral-700 dark:text-neutral-300 leading-relaxed whitespace-pre-wrap break-words line-clamp-6">{data.answer}</dd>
                          </div>
                        )}
                      </dl>
                    )}
                  </div>
                ))}
              </div>

              {/* Structural checks */}
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

          {/* Run history tab */}
          {tab === 'history' && (
            <div className="px-4 pb-4 pt-3">
              <CaseRunHistory caseId={r.case_id} authorizedFetch={authorizedFetch} />
            </div>
          )}
        </div>
      )}
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────────────────
// Case form modal
// ─────────────────────────────────────────────────────────────────────────────

function CaseModal({ initial, onSave, onClose }: {
  initial?: EvalCase; onSave: (d: FormState) => Promise<void>; onClose: () => void
}) {
  const [form, setForm] = useState<FormState>(
    initial ? {
      title: initial.title, query: initial.query, category: initial.category ?? '',
      expected_criteria_text: (initial.expected_criteria ?? []).join('\n'),
      expected_primary_role: initial.expected_primary_role ?? '',
      min_independent_sources: initial.min_independent_sources != null ? String(initial.min_independent_sources) : '',
      notes: initial.notes ?? '',
    } : DEFAULT_FORM,
  )
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  function set(k: keyof FormState, v: string) { setForm(f => ({ ...f, [k]: v })) }
  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault(); setSaving(true); setError('')
    try { await onSave(form); onClose() }
    catch (err: unknown) { setError(err instanceof Error ? err.message : 'Save failed') }
    finally { setSaving(false) }
  }
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4">
      <div className="w-full max-w-xl rounded-2xl bg-white dark:bg-neutral-900 shadow-2xl flex flex-col max-h-[90vh]">
        <div className="flex items-center justify-between px-5 py-4 border-b border-neutral-200 dark:border-neutral-800 flex-shrink-0">
          <h3 className="text-sm font-bold text-neutral-900 dark:text-neutral-50">{initial ? 'Edit eval case' : 'New eval case'}</h3>
          <button type="button" onClick={onClose} className="text-neutral-400 hover:text-neutral-700 dark:hover:text-neutral-200"><X size={16} /></button>
        </div>
        <form onSubmit={handleSubmit} className="overflow-y-auto flex-1 px-5 py-4 space-y-4">
          <div><label className="block text-xs font-semibold text-neutral-500 mb-1">Title *</label>
            <input required value={form.title} onChange={e => set('title', e.target.value)} placeholder="H-4 EAD processing time anchor"
              className="w-full rounded-lg border border-neutral-200 dark:border-neutral-700 bg-white dark:bg-neutral-800 px-3 py-2 text-sm text-neutral-900 dark:text-neutral-50 focus:outline-none focus:ring-2 focus:ring-neutral-400" /></div>
          <div><label className="block text-xs font-semibold text-neutral-500 mb-1">Query *</label>
            <textarea required rows={3} value={form.query} onChange={e => set('query', e.target.value)} placeholder="How long is H-4 EAD currently taking when filed with H-1B renewal?"
              className="w-full rounded-lg border border-neutral-200 dark:border-neutral-700 bg-white dark:bg-neutral-800 px-3 py-2 text-sm text-neutral-900 dark:text-neutral-50 focus:outline-none focus:ring-2 focus:ring-neutral-400 resize-none" /></div>
          <div className="grid grid-cols-2 gap-3">
            <div><label className="block text-xs font-semibold text-neutral-500 mb-1">Category</label>
              <input value={form.category} onChange={e => set('category', e.target.value)} placeholder="immigration_operational"
                className="w-full rounded-lg border border-neutral-200 dark:border-neutral-700 bg-white dark:bg-neutral-800 px-3 py-2 text-sm text-neutral-900 dark:text-neutral-50 focus:outline-none focus:ring-2 focus:ring-neutral-400" /></div>
            <div><label className="block text-xs font-semibold text-neutral-500 mb-1">Expected primary role</label>
              <select value={form.expected_primary_role} onChange={e => set('expected_primary_role', e.target.value)}
                className="w-full rounded-lg border border-neutral-200 dark:border-neutral-700 bg-white dark:bg-neutral-800 px-3 py-2 text-sm text-neutral-900 dark:text-neutral-50 focus:outline-none focus:ring-2 focus:ring-neutral-400">
                {ROLE_OPTIONS.map(r => <option key={r} value={r}>{r || '—'}</option>)}
              </select></div>
          </div>
          <div><label className="block text-xs font-semibold text-neutral-500 mb-1">Expected criteria <span className="font-normal text-neutral-400">(one per line)</span></label>
            <textarea rows={4} value={form.expected_criteria_text} onChange={e => set('expected_criteria_text', e.target.value)}
              placeholder={"Cites practitioner data\nIncludes official USCIS SLA\nGives specific time range"}
              className="w-full rounded-lg border border-neutral-200 dark:border-neutral-700 bg-white dark:bg-neutral-800 px-3 py-2 text-sm font-mono text-neutral-900 dark:text-neutral-50 focus:outline-none focus:ring-2 focus:ring-neutral-400 resize-none" /></div>
          <div className="grid grid-cols-2 gap-3">
            <div><label className="block text-xs font-semibold text-neutral-500 mb-1">Min independent sources</label>
              <input type="number" min={1} value={form.min_independent_sources} onChange={e => set('min_independent_sources', e.target.value)} placeholder="2"
                className="w-full rounded-lg border border-neutral-200 dark:border-neutral-700 bg-white dark:bg-neutral-800 px-3 py-2 text-sm text-neutral-900 dark:text-neutral-50 focus:outline-none focus:ring-2 focus:ring-neutral-400" /></div>
          </div>
          <div><label className="block text-xs font-semibold text-neutral-500 mb-1">Notes</label>
            <textarea rows={2} value={form.notes} onChange={e => set('notes', e.target.value)}
              className="w-full rounded-lg border border-neutral-200 dark:border-neutral-700 bg-white dark:bg-neutral-800 px-3 py-2 text-sm text-neutral-900 dark:text-neutral-50 focus:outline-none focus:ring-2 focus:ring-neutral-400 resize-none" /></div>
          {error && <p className="text-xs text-red-600 dark:text-red-400">{error}</p>}
        </form>
        <div className="flex justify-end gap-2 px-5 py-4 border-t border-neutral-200 dark:border-neutral-800 flex-shrink-0">
          <button type="button" onClick={onClose} className="rounded-lg border border-neutral-200 dark:border-neutral-700 px-4 py-2 text-sm font-semibold text-neutral-600 dark:text-neutral-400 hover:bg-neutral-50 dark:hover:bg-neutral-800">Cancel</button>
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
// Upload modal
// ─────────────────────────────────────────────────────────────────────────────

function UploadModal({ authorizedFetch, onDone, onClose }: {
  authorizedFetch: AuthorizedFetch; onDone: () => void; onClose: () => void
}) {
  const [text, setText] = useState('')
  const [uploading, setUploading] = useState(false)
  const [result, setResult] = useState<{ created: number; updated: number; reactivated: number; errors: { title: string; error: string }[] } | null>(null)
  const [error, setError] = useState('')

  async function handleUpload() {
    setError('')
    let parsed: unknown
    try { parsed = JSON.parse(text) } catch { setError('Invalid JSON — must be a valid JSON array.'); return }
    if (!Array.isArray(parsed)) { setError('JSON must be an array of case objects.'); return }
    setUploading(true)
    try {
      const resp = await authorizedFetch('/admin/evals/cases/upload', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(parsed) })
      if (!resp.ok) throw new Error(await readErrorBody(resp, 'Upload failed'))
      setResult(await resp.json()); onDone()
    } catch (err: unknown) { setError(err instanceof Error ? err.message : 'Upload failed') }
    finally { setUploading(false) }
  }

  const EXAMPLE = `[\n  {\n    "title": "Example case",\n    "query": "What is the processing time for X?",\n    "category": "immigration",\n    "expected_criteria": ["Cites practitioner data", "Gives specific time range"],\n    "expected_primary_role": "operational_reality",\n    "min_independent_sources": 2\n  }\n]`

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4">
      <div className="w-full max-w-2xl rounded-2xl bg-white dark:bg-neutral-900 shadow-2xl flex flex-col max-h-[90vh]">
        <div className="flex items-center justify-between px-5 py-4 border-b border-neutral-200 dark:border-neutral-800 flex-shrink-0">
          <h3 className="text-sm font-bold text-neutral-900 dark:text-neutral-50">Upload cases — JSON</h3>
          <button type="button" onClick={onClose} className="text-neutral-400 hover:text-neutral-700 dark:hover:text-neutral-200"><X size={16} /></button>
        </div>
        <div className="overflow-y-auto flex-1 px-5 py-4 space-y-4">
          <p className="text-xs text-neutral-500">Paste a JSON array or pick a .json file. Existing cases matched by title are updated; inactive cases are reactivated.</p>
          <label className="inline-flex items-center gap-2 cursor-pointer rounded-lg border border-neutral-200 dark:border-neutral-700 px-3 py-2 text-xs font-semibold text-neutral-600 dark:text-neutral-400 hover:bg-neutral-50 dark:hover:bg-neutral-800">
            <Upload size={13} /> Choose .json file
            <input type="file" accept=".json,application/json" className="hidden"
              onChange={e => { const f = e.target.files?.[0]; if (f) { const r = new FileReader(); r.onload = ev => setText(ev.target?.result as string ?? ''); r.readAsText(f) } }} />
          </label>
          <textarea rows={12} value={text} onChange={e => setText(e.target.value)} placeholder={EXAMPLE}
            className="w-full rounded-lg border border-neutral-200 dark:border-neutral-700 bg-neutral-50 dark:bg-neutral-800 px-3 py-2 text-xs font-mono text-neutral-900 dark:text-neutral-50 focus:outline-none focus:ring-2 focus:ring-neutral-400 resize-y" />
          {error && <p className="text-xs text-red-600 dark:text-red-400">{error}</p>}
          {result && (
            <div className="rounded-lg border border-emerald-200 dark:border-emerald-900 bg-emerald-50 dark:bg-emerald-950/30 px-4 py-3 text-xs">
              <span className="font-semibold text-emerald-800 dark:text-emerald-300">Done — </span>
              {result.created} created, {result.updated} updated, {result.reactivated} reactivated
              {result.errors.map((e, i) => <p key={i} className="text-red-600 dark:text-red-400 mt-1">✗ {e.title}: {e.error}</p>)}
            </div>
          )}
        </div>
        <div className="flex justify-end gap-2 px-5 py-4 border-t border-neutral-200 dark:border-neutral-800 flex-shrink-0">
          <button type="button" onClick={onClose} className="rounded-lg border border-neutral-200 dark:border-neutral-700 px-4 py-2 text-sm font-semibold text-neutral-600 dark:text-neutral-400 hover:bg-neutral-50 dark:hover:bg-neutral-800">{result ? 'Close' : 'Cancel'}</button>
          {!result && <button type="button" disabled={uploading || !text.trim()} onClick={handleUpload}
            className="rounded-lg bg-neutral-900 dark:bg-white px-4 py-2 text-sm font-semibold text-white dark:text-neutral-900 hover:bg-neutral-700 dark:hover:bg-neutral-200 disabled:opacity-50">{uploading ? 'Uploading…' : 'Upload'}</button>}
        </div>
      </div>
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────────────────
// Category group — controlled open state (parent drives collapse/expand all)
// ─────────────────────────────────────────────────────────────────────────────

function CategoryGroup({
  category, cases, open, onOpenChange, selectedIds, onToggle, onToggleAll,
  onEdit, onDeactivate, deactivating,
}: {
  category: string
  cases: EvalCase[]
  open: boolean
  onOpenChange: (v: boolean) => void
  selectedIds: Set<number>
  onToggle: (id: number) => void
  onToggleAll: (ids: number[], checked: boolean) => void
  onEdit: (c: EvalCase) => void
  onDeactivate: (id: number) => void
  deactivating: number | null
}) {
  const ids = cases.map(c => c.id)
  const allSelected = ids.length > 0 && ids.every(id => selectedIds.has(id))
  const someSelected = ids.some(id => selectedIds.has(id))

  return (
    <div>
      <button type="button" onClick={() => onOpenChange(!open)}
        className="w-full flex items-center gap-2 px-3 py-2 bg-neutral-50 dark:bg-neutral-950 hover:bg-neutral-100 dark:hover:bg-neutral-900 transition-colors border-b border-neutral-100 dark:border-neutral-800">
        {open ? <ChevronDown size={12} className="text-neutral-400 flex-shrink-0" /> : <ChevronRight size={12} className="text-neutral-400 flex-shrink-0" />}
        <span className="flex-1 text-xs font-semibold text-neutral-700 dark:text-neutral-300 text-left">{category}</span>
        <span className="text-[11px] text-neutral-400 tabular-nums">{cases.length}</span>
        <label className="flex items-center gap-1 ml-2 cursor-pointer" onClick={e => e.stopPropagation()}>
          <input type="checkbox" checked={allSelected}
            ref={el => { if (el) el.indeterminate = someSelected && !allSelected }}
            onChange={e => onToggleAll(ids, e.target.checked)}
            className="rounded border-neutral-300 dark:border-neutral-600" />
          <span className="text-[11px] text-neutral-400 select-none">all</span>
        </label>
      </button>

      {open && cases.map(c => (
        <div key={c.id} className="flex items-start gap-3 px-4 py-3 bg-white dark:bg-neutral-900 hover:bg-neutral-50 dark:hover:bg-neutral-800 transition-colors border-b border-neutral-100 dark:border-neutral-800 last:border-0">
          <input type="checkbox" checked={selectedIds.has(c.id)} onChange={() => onToggle(c.id)}
            className="mt-0.5 rounded border-neutral-300 dark:border-neutral-600 text-neutral-900" />
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2 flex-wrap">
              <span className="text-sm font-semibold text-neutral-900 dark:text-neutral-50 truncate">{c.title}</span>
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
            <button type="button" onClick={() => onEdit(c)} title="Edit"
              className="grid h-7 w-7 place-items-center rounded-lg text-neutral-400 hover:bg-neutral-100 dark:hover:bg-neutral-700 hover:text-neutral-700 dark:hover:text-neutral-200">
              <Edit2 size={13} />
            </button>
            <button type="button" onClick={() => onDeactivate(c.id)} disabled={deactivating === c.id} title="Deactivate"
              className="grid h-7 w-7 place-items-center rounded-lg text-neutral-400 hover:bg-amber-50 dark:hover:bg-amber-950/30 hover:text-amber-600 dark:hover:text-amber-400 disabled:opacity-40">
              <EyeOff size={13} />
            </button>
          </div>
        </div>
      ))}
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────────────────
// Main component
// ─────────────────────────────────────────────────────────────────────────────
// Expandable run-history row — fetches per-case detail lazily on first open
// ─────────────────────────────────────────────────────────────────────────────

type RunDetail = { cases: EvalCaseRunResult[]; log: string[] }

function RunHistoryRow({
  run,
  featured,
  defaultOpen,
  authorizedFetch,
}: {
  run: EvalRunSummary
  featured?: boolean
  defaultOpen?: boolean
  authorizedFetch: AuthorizedFetch
}) {
  const [open, setOpen] = useState(defaultOpen ?? false)
  const [detail, setDetail] = useState<RunDetail | null>(null)
  const [loading, setLoading] = useState(false)
  const [fetchError, setFetchError] = useState('')

  async function toggle() {
    const next = !open
    setOpen(next)
    if (next && !detail) {
      setLoading(true); setFetchError('')
      try {
        const resp = await authorizedFetch(`/admin/evals/runs/${run.run_id}/status`)
        if (!resp.ok) throw new Error(await readErrorBody(resp, 'Failed to load run'))
        const data = await resp.json()
        // progress[] is the per-case array from the status endpoint
        const cases: EvalCaseRunResult[] = data.progress ?? data.results?.cases ?? []
        setDetail({ cases, log: data.log ?? [] })
      } catch (err: unknown) {
        setFetchError(err instanceof Error ? err.message : 'Load failed')
      } finally {
        setLoading(false)
      }
    }
  }

  const statusColor = run.status === 'complete'
    ? 'bg-green-500' : run.status === 'error'
    ? 'bg-red-500' : 'bg-yellow-400 animate-pulse'
  const statusText = run.status === 'complete'
    ? 'text-green-700 dark:text-green-400' : run.status === 'error'
    ? 'text-red-600 dark:text-red-400' : 'text-yellow-600 dark:text-yellow-400'

  return (
    <div className={`overflow-hidden ${featured ? 'rounded-xl border-2 border-neutral-300 dark:border-neutral-600' : 'rounded-xl border border-neutral-200 dark:border-neutral-800'}`}>
      {/* Header row */}
      <button type="button" onClick={toggle}
        className={`w-full flex items-center gap-3 px-4 py-3 text-left transition-colors
          ${featured ? 'bg-neutral-50 dark:bg-neutral-800/60 hover:bg-neutral-100 dark:hover:bg-neutral-800'
            : 'bg-white dark:bg-neutral-900 hover:bg-neutral-50 dark:hover:bg-neutral-800'}`}>
        {open
          ? <ChevronDown size={13} className="text-neutral-400 flex-shrink-0" />
          : <ChevronRight size={13} className="text-neutral-400 flex-shrink-0" />}
        <span className={`h-2 w-2 rounded-full flex-shrink-0 ${statusColor}`} />
        {featured && <span className="text-[10px] font-bold uppercase tracking-widest text-neutral-400 flex-shrink-0">Latest</span>}
        <span className="flex-1 font-mono text-xs text-neutral-500 truncate">{run.run_id}</span>
        <span className="text-xs text-neutral-400 flex-shrink-0 tabular-nums">{run.case_count} case{run.case_count !== 1 ? 's' : ''}</span>
        <span className={`text-xs font-semibold flex-shrink-0 ${statusText}`}>{run.status}</span>
        {run.started_at && (
          <span className="text-xs text-neutral-400 flex-shrink-0 hidden sm:block">
            {new Date(run.started_at).toLocaleString()}
          </span>
        )}
        {loading && <Loader2 size={13} className="text-neutral-400 animate-spin flex-shrink-0" />}
      </button>

      {/* Expanded content */}
      {open && (
        <div className="border-t border-neutral-100 dark:border-neutral-800 bg-white dark:bg-neutral-900 px-4 pb-4 pt-3 space-y-3">
          {loading && <p className="text-xs text-neutral-400">Loading run details…</p>}
          {fetchError && <p className="text-xs text-red-500">{fetchError}</p>}
          {detail && detail.cases.length === 0 && !loading && (
            <p className="text-xs text-neutral-400 italic">No per-case data stored for this run (LangSmith-only run or data unavailable).</p>
          )}
          {detail && detail.cases.length > 0 && (
            <div className="space-y-2">
              {detail.cases.map(r => (
                <CaseResultRow key={r.case_id} r={r} authorizedFetch={authorizedFetch} />
              ))}
            </div>
          )}
          {run.error && (
            <p className="text-xs text-red-600 dark:text-red-400 font-mono">{run.error}</p>
          )}
        </div>
      )}
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────────────────

export function EvalHarnessTab({ authorizedFetch }: { authorizedFetch: AuthorizedFetch }) {
  // ── Cases ───────────────────────────────────────────────────────────────────
  const [cases, setCases] = useState<EvalCase[]>([])
  const [inactiveCases, setInactiveCases] = useState<EvalCase[]>([])
  const [showInactive, setShowInactive] = useState(false)
  const [loadingCases, setLoadingCases] = useState(true)
  const [casesError, setCasesError] = useState('')
  const [modal, setModal] = useState<'create' | 'upload' | EvalCase | null>(null)
  const [deactivating, setDeactivating] = useState<number | null>(null)
  const [restoring, setRestoring] = useState<number | null>(null)

  // Group-open state (lifted for collapse/expand all)
  const [groupsOpen, setGroupsOpen] = useState<Record<string, boolean>>({})

  // ── Run ─────────────────────────────────────────────────────────────────────
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set())
  const [runMode, setRunMode] = useState<'in_process' | 'langsmith' | 'both'>('in_process')
  const [lsConfigured, setLsConfigured] = useState(false)
  const [runStatus, setRunStatus] = useState<'idle' | 'running' | 'complete' | 'error'>('idle')
  const [log, setLog] = useState<string[]>([])
  const [progressTotal, setProgressTotal] = useState<number | null>(null)
  const [progressCompleted, setProgressCompleted] = useState(0)
  const [runResult, setRunResult] = useState<EvalRunResult | null>(null)
  const [runs, setRuns] = useState<EvalRunSummary[]>([])
  const [runsHasMore, setRunsHasMore] = useState(false)
  const [runsLoadingMore, setRunsLoadingMore] = useState(false)
  const [runError, setRunError] = useState('')
  const [langsmithLinks, setLangsmithLinks] = useState<Record<string, string>>({})
  const logRef = useRef<HTMLDivElement>(null)
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)

  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight
  }, [log])

  // ── Load ─────────────────────────────────────────────────────────────────────
  const loadCases = useCallback(async () => {
    setLoadingCases(true)
    try {
      const [activeResp, allResp] = await Promise.all([
        authorizedFetch('/admin/evals/cases'),
        authorizedFetch('/admin/evals/cases?include_inactive=true'),
      ])
      if (!activeResp.ok) throw new Error(await readErrorBody(activeResp, 'Could not load eval cases'))
      const activeData = await activeResp.json()
      const activeCases: EvalCase[] = activeData.items ?? []
      setCases(activeCases)
      // Initialise group open state for any new categories (default: open)
      setGroupsOpen(prev => {
        const next = { ...prev }
        for (const c of activeCases) {
          const key = c.category || 'Uncategorized'
          if (!(key in next)) next[key] = true
        }
        return next
      })
      if (allResp.ok) {
        const allData = await allResp.json()
        setInactiveCases((allData.items ?? []).filter((c: EvalCase) => !c.is_active))
      }
      setCasesError('')
    } catch (err: unknown) {
      setCasesError(err instanceof Error ? err.message : 'Load failed')
    } finally {
      setLoadingCases(false)
    }
  }, [authorizedFetch])

  const PAGE = 11 // fetch 11, display 10, use 11th to detect "has more"

  const loadRuns = useCallback(async () => {
    try {
      const resp = await authorizedFetch(`/admin/evals/runs?limit=${PAGE}&offset=0`)
      if (!resp.ok) return
      const data = await resp.json()
      const fetched: EvalRunSummary[] = data.runs ?? []
      setRunsHasMore(fetched.length >= PAGE)
      setRuns(fetched.slice(0, PAGE - 1)) // keep max 10
    } catch {}
  }, [authorizedFetch])

  async function loadMoreRuns() {
    setRunsLoadingMore(true)
    try {
      const offset = runs.length
      const resp = await authorizedFetch(`/admin/evals/runs?limit=${PAGE}&offset=${offset}`)
      if (!resp.ok) return
      const data = await resp.json()
      const fetched: EvalRunSummary[] = data.runs ?? []
      setRunsHasMore(fetched.length >= PAGE)
      setRuns(prev => [...prev, ...fetched.slice(0, PAGE - 1)])
    } catch {} finally {
      setRunsLoadingMore(false)
    }
  }

  useEffect(() => { loadCases(); loadRuns() }, [loadCases, loadRuns])

  // ── Case CRUD ────────────────────────────────────────────────────────────────
  async function handleSave(form: FormState, existing?: EvalCase) {
    const payload = {
      title: form.title, query: form.query, category: form.category || null,
      expected_criteria: form.expected_criteria_text.split('\n').map(s => s.trim()).filter(Boolean),
      expected_primary_role: form.expected_primary_role || null,
      min_independent_sources: form.min_independent_sources ? Number(form.min_independent_sources) : null,
      notes: form.notes || null,
    }
    const url = existing ? `/admin/evals/cases/${existing.id}` : '/admin/evals/cases'
    const resp = await authorizedFetch(url, { method: existing ? 'PUT' : 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) })
    if (!resp.ok) throw new Error(await readErrorBody(resp, 'Save failed'))
    await loadCases()
  }

  async function handleDeactivate(id: number) {
    if (!confirm('Deactivate this case? It will be hidden from runs but not deleted.')) return
    setDeactivating(id)
    try {
      const resp = await authorizedFetch(`/admin/evals/cases/${id}`, { method: 'DELETE' })
      if (!resp.ok) throw new Error(await readErrorBody(resp, 'Deactivate failed'))
      setSelectedIds(prev => { const next = new Set(prev); next.delete(id); return next })
      await loadCases()
    } catch (err: unknown) { setCasesError(err instanceof Error ? err.message : 'Deactivate failed') }
    finally { setDeactivating(null) }
  }

  async function handleRestore(id: number) {
    setRestoring(id)
    try {
      const resp = await authorizedFetch(`/admin/evals/cases/${id}/restore`, { method: 'POST' })
      if (!resp.ok) throw new Error(await readErrorBody(resp, 'Restore failed'))
      await loadCases()
    } catch (err: unknown) { setCasesError(err instanceof Error ? err.message : 'Restore failed') }
    finally { setRestoring(null) }
  }

  // ── Download JSON ────────────────────────────────────────────────────────────
  function downloadJSON() {
    const data = cases.map(c => ({
      title: c.title, query: c.query, category: c.category,
      expected_criteria: c.expected_criteria, expected_primary_role: c.expected_primary_role,
      min_independent_sources: c.min_independent_sources, notes: c.notes,
    }))
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a'); a.href = url; a.download = 'eval_cases.json'; a.click()
    URL.revokeObjectURL(url)
  }

  // ── Selection ────────────────────────────────────────────────────────────────
  function toggleCase(id: number) {
    setSelectedIds(prev => { const next = new Set(prev); next.has(id) ? next.delete(id) : next.add(id); return next })
  }
  function toggleAll(ids: number[], checked: boolean) {
    setSelectedIds(prev => { const next = new Set(prev); ids.forEach(id => checked ? next.add(id) : next.delete(id)); return next })
  }

  // ── Collapse / expand all ────────────────────────────────────────────────────
  const sortedCategories = [...new Set(cases.map(c => c.category || 'Uncategorized'))].sort(
    (a, b) => a === 'Uncategorized' ? 1 : b === 'Uncategorized' ? -1 : a.localeCompare(b)
  )
  const allOpen = sortedCategories.length > 0 && sortedCategories.every(k => groupsOpen[k] !== false)
  const allClosed = sortedCategories.every(k => groupsOpen[k] === false)

  function toggleAllGroups() {
    const value = !allOpen
    setGroupsOpen(prev => Object.fromEntries(Object.keys(prev).map(k => [k, value])))
  }

  // ── Polling ──────────────────────────────────────────────────────────────────
  function stopPolling() {
    if (pollRef.current != null) { clearInterval(pollRef.current); pollRef.current = null }
  }

  const applySnapshot = useCallback((data: Record<string, unknown>) => {
    setLog((data.log as string[]) ?? [])
    setProgressTotal((data.total as number | null) ?? null)
    setProgressCompleted((data.completed as number) ?? 0)
    if (data.langsmith_links) setLangsmithLinks(data.langsmith_links as Record<string, string>)
    const progress = data.progress as EvalCaseRunResult[] | undefined
    const results = data.results as EvalRunResult | null | undefined
    if (results?.mode) {
      setRunResult(results)
    } else if (progress?.length) {
      setRunResult({ mode: 'in_process', cases: progress, langsmith: null })
    }
    if (data.status === 'complete') { setRunStatus('complete'); stopPolling(); loadRuns() }
    else if (data.status === 'error') { setRunError((data.error as string) ?? 'Unknown error'); setRunStatus('error'); stopPolling() }
  }, [loadRuns]) // eslint-disable-line react-hooks/exhaustive-deps

  function startPolling(runId: string) {
    stopPolling()
    pollRef.current = setInterval(async () => {
      try {
        const resp = await authorizedFetch(`/admin/evals/runs/${runId}/status`)
        if (resp.ok) applySnapshot(await resp.json())
      } catch {}
    }, POLL_MS)
  }

  useEffect(() => () => stopPolling(), [])

  // ── Start run ────────────────────────────────────────────────────────────────
  async function startRun() {
    setRunStatus('running'); setLog([]); setRunResult(null); setRunError('')
    setLangsmithLinks({}); setProgressTotal(null); setProgressCompleted(0)
    const payload = {
      mode: runMode,
      ...(selectedIds.size > 0 ? { case_ids: Array.from(selectedIds) } : {}),
    }
    try {
      const startResp = await authorizedFetch('/admin/evals/runs', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
      })
      if (!startResp.ok) throw new Error(await readErrorBody(startResp, 'Failed to start eval run'))
      const { run_id } = await startResp.json()
      const snap = await authorizedFetch(`/admin/evals/runs/${run_id}/status`)
      if (snap.ok) applySnapshot(await snap.json())
      startPolling(run_id)
    } catch (err: unknown) {
      setRunError(err instanceof Error ? err.message : 'Failed to start run'); setRunStatus('error')
    }
  }

  // ── Category groups ──────────────────────────────────────────────────────────
  const categoryGroups: Record<string, EvalCase[]> = {}
  for (const c of cases) { const key = c.category || 'Uncategorized'; (categoryGroups[key] ??= []).push(c) }
  const canRun = cases.length > 0 && runStatus !== 'running'
  const runLabel = selectedIds.size > 0 ? `Run selected (${selectedIds.size})` : `Run all ${cases.length}`

  // ── Render ───────────────────────────────────────────────────────────────────
  return (
    <div className="space-y-6">

      {/* ═══ RUN SECTION ══════════════════════════════════════════════════════ */}
      <div className="space-y-4">
        <LangSmithBanner authorizedFetch={authorizedFetch} onStatus={setLsConfigured} />

        {/* Mode selector + run button */}
        <div className="flex items-center gap-3 flex-wrap">
          {/* Segmented mode control */}
          <div className="flex rounded-lg border border-neutral-200 dark:border-neutral-700 overflow-hidden text-xs font-semibold">
            {([
              { value: 'in_process', label: 'Local', title: 'Run both pipelines in-process; full per-case data stored locally' },
              { value: 'langsmith',  label: 'LangSmith', title: lsConfigured ? 'Run via LangSmith evaluate(); per-case data in LangSmith' : 'LangSmith not configured' },
              { value: 'both',       label: 'Both', title: lsConfigured ? 'In-process first (local data), then LangSmith experiments — ~2× runtime' : 'LangSmith not configured' },
            ] as const).map(({ value, label, title }) => {
              const disabled = !lsConfigured && value !== 'in_process'
              const active = runMode === value
              return (
                <button key={value} type="button"
                  disabled={disabled || runStatus === 'running'}
                  title={title}
                  onClick={() => setRunMode(value)}
                  className={`px-3 py-1.5 transition-colors border-r border-neutral-200 dark:border-neutral-700 last:border-0
                    ${active
                      ? 'bg-neutral-900 dark:bg-white text-white dark:text-neutral-900'
                      : disabled
                        ? 'text-neutral-300 dark:text-neutral-600 cursor-not-allowed bg-white dark:bg-neutral-900'
                        : 'text-neutral-600 dark:text-neutral-400 bg-white dark:bg-neutral-900 hover:bg-neutral-50 dark:hover:bg-neutral-800'
                    }`}>
                  {label}
                </button>
              )
            })}
          </div>

          <button type="button" disabled={!canRun} onClick={startRun}
            className="flex items-center gap-1.5 rounded-lg bg-neutral-900 dark:bg-white px-4 py-2 text-sm font-semibold text-white dark:text-neutral-900 hover:bg-neutral-700 dark:hover:bg-neutral-200 disabled:opacity-40">
            <Play size={13} />
            {runStatus === 'running' ? 'Running…' : runLabel}
          </button>
          {runStatus === 'running' && progressTotal != null && (
            <div className="flex items-center gap-2 flex-1">
              <div className="flex-1 h-1.5 rounded-full bg-neutral-200 dark:bg-neutral-800 overflow-hidden">
                <div className="h-full bg-blue-500 transition-all duration-500"
                  style={{ width: `${(progressCompleted / progressTotal) * 100}%` }} />
              </div>
              <span className="text-xs text-neutral-500 tabular-nums flex-shrink-0">{progressCompleted}/{progressTotal}</span>
            </div>
          )}
        </div>

        {(log.length > 0 || runStatus === 'running') && (
          <div ref={logRef}
            className="rounded-xl bg-neutral-950 text-green-300 font-mono text-xs p-4 max-h-44 overflow-y-auto space-y-0.5">
            {log.map((line, i) => <div key={i}>{line}</div>)}
            {runStatus === 'running' && <div className="animate-pulse text-neutral-500">⋯ polling…</div>}
          </div>
        )}
        {runError && <p className="text-xs text-red-600 dark:text-red-400">{runError}</p>}

        {Object.keys(langsmithLinks).length > 0 && (
          <div className="rounded-xl border border-emerald-200 dark:border-emerald-900 bg-emerald-50 dark:bg-emerald-950/30 px-4 py-3 space-y-1">
            <p className="text-xs font-bold text-emerald-800 dark:text-emerald-300 mb-2">LangSmith experiments</p>
            {Object.entries(langsmithLinks).map(([pipeline, url]) => (
              <a key={pipeline} href={url} target="_blank" rel="noopener noreferrer"
                className="flex items-center gap-1.5 text-xs text-emerald-700 dark:text-emerald-400 hover:underline">
                <ExternalLink size={11} /> {pipeline} pipeline experiment
              </a>
            ))}
          </div>
        )}

        {runResult && runStatus !== 'running' && (
          <LocalReport cases={runResult.cases ?? []} runResult={runResult} />
        )}

        {(runResult?.cases?.length ?? 0) > 0 && (
          <div>
            <p className="text-xs font-bold text-neutral-500 mb-2">Per-case results ({runResult!.cases.length})</p>
            <div className="space-y-2">
              {runResult!.cases.map(r => (
                <CaseResultRow key={r.case_id} r={r} authorizedFetch={authorizedFetch} />
              ))}
            </div>
          </div>
        )}
        {runResult?.mode === 'langsmith' && runStatus === 'complete' && (runResult.cases?.length ?? 0) === 0 && (
          <p className="text-xs text-neutral-500 italic">Per-case rows are in LangSmith. Use the experiment links above.</p>
        )}

        {runs.length > 0 && (
          <div className="space-y-2">
            <p className="text-xs font-bold text-neutral-500">Run history</p>

            {/* Latest run — featured at top */}
            <RunHistoryRow
              key={runs[0].run_id}
              run={runs[0]}
              featured
              authorizedFetch={authorizedFetch}
            />

            {/* Older runs — collapsed by default */}
            {runs.slice(1).map(r => (
              <RunHistoryRow key={r.run_id} run={r} authorizedFetch={authorizedFetch} />
            ))}

            {/* Load more */}
            {runsHasMore && (
              <button type="button" onClick={loadMoreRuns} disabled={runsLoadingMore}
                className="text-xs text-neutral-400 hover:text-neutral-600 dark:hover:text-neutral-300 flex items-center gap-1.5 py-1 disabled:opacity-50">
                {runsLoadingMore ? <Loader2 size={12} className="animate-spin" /> : <ChevronDown size={12} />}
                {runsLoadingMore ? 'Loading…' : 'Load more runs'}
              </button>
            )}
          </div>
        )}
      </div>

      {/* ═══ CASES SECTION ══════════════════════════════════════════════════════ */}
      <div>
        <div className="flex items-center justify-between mb-3">
          <div className="flex items-center gap-3">
            <h3 className="text-sm font-bold text-neutral-900 dark:text-neutral-50">
              Eval cases <span className="font-normal text-neutral-400">({cases.length} active)</span>
            </h3>
            {sortedCategories.length > 1 && (
              <button type="button" onClick={toggleAllGroups}
                className="flex items-center gap-1 text-xs text-neutral-400 hover:text-neutral-600 dark:hover:text-neutral-300 transition-colors"
                title={allOpen ? 'Collapse all' : 'Expand all'}>
                {allOpen
                  ? <><ChevronsDownUp size={13} /> Collapse all</>
                  : <><ChevronsUpDown size={13} /> Expand all</>}
              </button>
            )}
          </div>
          <div className="flex items-center gap-2">
            <button type="button" onClick={downloadJSON} disabled={cases.length === 0}
              className="flex items-center gap-1.5 rounded-lg border border-neutral-200 dark:border-neutral-700 px-3 py-2 text-xs font-semibold text-neutral-600 dark:text-neutral-400 hover:bg-neutral-50 dark:hover:bg-neutral-800 disabled:opacity-40">
              <Download size={12} /> Download JSON
            </button>
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
              className="mt-3 text-xs font-semibold text-neutral-700 dark:text-neutral-300 underline underline-offset-2">Create the first one</button>
          </div>
        ) : (
          <div className="rounded-xl border border-neutral-200 dark:border-neutral-800 overflow-hidden divide-y divide-neutral-200 dark:divide-neutral-800">
            {sortedCategories.map(cat => (
              <CategoryGroup
                key={cat}
                category={cat}
                cases={categoryGroups[cat]}
                open={groupsOpen[cat] !== false}
                onOpenChange={v => setGroupsOpen(prev => ({ ...prev, [cat]: v }))}
                selectedIds={selectedIds}
                onToggle={toggleCase}
                onToggleAll={toggleAll}
                onEdit={c => setModal(c)}
                onDeactivate={handleDeactivate}
                deactivating={deactivating}
              />
            ))}

            {inactiveCases.length > 0 && (
              <>
                <button type="button" onClick={() => setShowInactive(s => !s)}
                  className="w-full flex items-center gap-2 px-4 py-2 text-xs font-semibold text-neutral-400 hover:text-neutral-600 dark:hover:text-neutral-300 bg-neutral-50 dark:bg-neutral-950 hover:bg-neutral-100 dark:hover:bg-neutral-900 transition-colors">
                  {showInactive ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
                  {inactiveCases.length} inactive case{inactiveCases.length !== 1 ? 's' : ''}
                </button>
                {showInactive && inactiveCases.map(c => (
                  <div key={c.id} className="flex items-start gap-3 px-4 py-3 bg-neutral-50 dark:bg-neutral-900/50 opacity-60 border-b border-neutral-100 dark:border-neutral-800 last:border-0">
                    <div className="w-4 flex-shrink-0" />
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2">
                        <span className="text-sm font-semibold text-neutral-500 truncate line-through">{c.title}</span>
                        <span className="text-[10px] font-semibold text-neutral-400 bg-neutral-200 dark:bg-neutral-700 rounded px-1.5 py-0.5">inactive</span>
                      </div>
                      {c.category && <p className="text-xs text-neutral-400 mt-0.5">{c.category}</p>}
                    </div>
                    <button type="button" onClick={() => handleRestore(c.id)} disabled={restoring === c.id}
                      className="flex items-center gap-1 h-7 px-2 rounded-lg text-xs font-semibold text-neutral-400 hover:bg-neutral-200 dark:hover:bg-neutral-700 hover:text-neutral-700 dark:hover:text-neutral-200 disabled:opacity-40">
                      <RotateCcw size={12} /> Restore
                    </button>
                  </div>
                ))}
              </>
            )}
          </div>
        )}
      </div>

      {/* ═══ MODALS ════════════════════════════════════════════════════════════ */}
      {modal === 'create' && <CaseModal onSave={f => handleSave(f)} onClose={() => setModal(null)} />}
      {modal === 'upload' && <UploadModal authorizedFetch={authorizedFetch} onDone={loadCases} onClose={() => setModal(null)} />}
      {modal !== null && modal !== 'create' && modal !== 'upload' && (
        <CaseModal initial={modal as EvalCase} onSave={f => handleSave(f, modal as EvalCase)} onClose={() => setModal(null)} />
      )}
    </div>
  )
}
