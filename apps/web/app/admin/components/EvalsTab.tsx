'use client'

import {
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Loader2,
  Play,
  RefreshCw,
  Rocket,
  RotateCcw,
  XCircle,
} from 'lucide-react'
import { useCallback, useEffect, useRef, useState } from 'react'

const POLL_INTERVAL_MS = 3000
import { readErrorBody } from '../../lib/api'
import type {
  AuthorizedFetch,
  OrchestratorStatus,
  ParityCaseResult,
  ParityReport,
  ParityRunSummary,
} from '../types'
import { EvalHarnessTab } from './EvalHarnessTab'

// ---------------------------------------------------------------------------
// Shared helpers (parity sub-tab)
// ---------------------------------------------------------------------------

function fmtRatio(v: number | null | undefined) {
  if (v == null) return '–'
  return v.toFixed(2)
}

function fmtMs(v: number | null | undefined) {
  if (v == null) return '–'
  if (v < 1000) return `${v}ms`
  return `${(v / 1000).toFixed(1)}s`
}

function GateIcon({ pass }: { pass: boolean | null }) {
  if (pass === null) return <span className="text-neutral-400">–</span>
  return pass
    ? <CheckCircle2 size={14} className="text-emerald-500" />
    : <XCircle size={14} className="text-red-500" />
}

/** Derive a concise gate-diagnosis label for the row header. */
function gateDiagnosis(result: ParityCaseResult): { label: string; color: string } {
  if (!result.passes_structural_gate) return { label: 'Blocked: structural', color: 'text-red-600 dark:text-red-400' }
  if (!result.passes_answer_length_gate) return { label: 'Blocked: answer length', color: 'text-red-600 dark:text-red-400' }
  if (!result.passes_evidence_gate) return { label: 'Blocked: evidence', color: 'text-red-600 dark:text-red-400' }
  if (!result.passes_claim_gate) return { label: 'Blocked: claims', color: 'text-red-600 dark:text-red-400' }
  if (!result.passes_budget_gate) return { label: 'Blocked: budget', color: 'text-red-600 dark:text-red-400' }
  if (result.judge_verdict_agrees) return { label: 'Pass', color: 'text-emerald-600 dark:text-emerald-400' }
  if (result.langgraph_judge_verdict === 'pass' && result.legacy_judge_verdict !== 'pass')
    return { label: 'Pass · LG better', color: 'text-emerald-600 dark:text-emerald-400' }
  if (result.legacy_judge_verdict === 'pass' && result.langgraph_judge_verdict !== 'pass')
    return { label: 'Pass · LG worse', color: 'text-amber-600 dark:text-amber-400' }
  return { label: 'Pass · verdict differs', color: 'text-amber-600 dark:text-amber-400' }
}

function RatioBadge({ value, threshold, invert = false }: { value: number | null; threshold: number; invert?: boolean }) {
  if (value == null) return <span className="text-neutral-400 text-xs">–</span>
  const pass = invert ? value <= threshold : value >= threshold
  return (
    <span className={`text-xs font-mono font-semibold ${pass ? 'text-emerald-600 dark:text-emerald-400' : 'text-red-600 dark:text-red-400'}`}>
      {fmtRatio(value)}
    </span>
  )
}

function GateRow({
  label,
  pass,
  total,
  threshold,
}: {
  label: string
  pass: number
  total: number
  threshold: number
}) {
  const rate = total > 0 ? pass / total : 0
  const ok = rate >= threshold
  return (
    <div className="flex items-center justify-between py-2 border-b border-neutral-100 dark:border-neutral-800 last:border-0">
      <div className="flex items-center gap-2">
        {ok ? <CheckCircle2 size={14} className="text-emerald-500 flex-shrink-0" /> : <XCircle size={14} className="text-red-500 flex-shrink-0" />}
        <span className="text-sm text-neutral-700 dark:text-neutral-300">{label}</span>
      </div>
      <div className="flex items-center gap-3">
        <span className={`text-sm font-semibold ${ok ? 'text-emerald-600 dark:text-emerald-400' : 'text-red-600 dark:text-red-400'}`}>
          {pass}/{total}
        </span>
        <span className="text-xs text-neutral-400">{total > 0 ? `${Math.round(rate * 100)}%` : '–'}</span>
      </div>
    </div>
  )
}

function CaseRow({ result }: { result: ParityCaseResult }) {
  const [open, setOpen] = useState(false)

  return (
    <div className="border border-neutral-200 dark:border-neutral-800 rounded-lg overflow-hidden mb-2">
      <button
        type="button"
        onClick={() => setOpen(o => !o)}
        className="w-full flex items-center gap-3 px-4 py-2.5 bg-white dark:bg-neutral-900 hover:bg-neutral-50 dark:hover:bg-neutral-800 transition-colors text-left"
      >
        <GateIcon pass={result.overall_pass} />
        <span className="flex-1 text-sm font-mono text-neutral-700 dark:text-neutral-300 truncate">
          {result.case_id}
        </span>
        <div className="flex items-center gap-4 text-xs">
          {/* Ratios — coloured by gate pass/fail */}
          <span title="Answer length ratio (≥0.70)" className={result.passes_answer_length_gate ? 'text-emerald-600 dark:text-emerald-400' : 'text-red-600 dark:text-red-400'}>
            ans {fmtRatio(result.answer_length_ratio)}
          </span>
          <span title="Evidence count ratio (≥0.80)" className={result.passes_evidence_gate ? 'text-emerald-600 dark:text-emerald-400' : 'text-red-600 dark:text-red-400'}>
            evid {fmtRatio(result.evidence_count_ratio)}
          </span>
          <span title="Claim count ratio (≥0.60)" className={result.passes_claim_gate ? 'text-emerald-600 dark:text-emerald-400' : 'text-red-600 dark:text-red-400'}>
            claims {fmtRatio(result.claim_count_ratio)}
          </span>
          {/* Gate diagnosis replaces ambiguous binary icon */}
          {(() => { const d = gateDiagnosis(result); return <span className={`font-medium ${d.color}`}>{d.label}</span> })()}
        </div>
        {open ? <ChevronDown size={14} className="text-neutral-400 flex-shrink-0" /> : <ChevronRight size={14} className="text-neutral-400 flex-shrink-0" />}
      </button>

      {open && (
        <div className="border-t border-neutral-100 dark:border-neutral-800 bg-neutral-50 dark:bg-neutral-950 px-4 py-3 space-y-4">
          {(result.legacy_error || result.langgraph_error) && (
            <div className="rounded-md bg-red-50 dark:bg-red-950/30 border border-red-200 dark:border-red-900 p-3 text-xs font-mono text-red-700 dark:text-red-400 whitespace-pre-wrap">
              {result.legacy_error && <p><strong>Legacy error:</strong> {result.legacy_error.slice(0, 400)}</p>}
              {result.langgraph_error && <p className="mt-1"><strong>LangGraph error:</strong> {result.langgraph_error.slice(0, 400)}</p>}
            </div>
          )}

          {/* Pipeline side-by-side */}
          <div className="grid grid-cols-2 gap-4 text-xs">
            <div>
              <p className="font-semibold text-neutral-500 uppercase tracking-wider mb-1.5">Legacy</p>
              <dl className="space-y-1">
                <div className="flex justify-between"><dt className="text-neutral-500">Answer length</dt><dd className="font-mono">{result.legacy_answer_length} chars</dd></div>
                <div className="flex justify-between"><dt className="text-neutral-500">Evidence items</dt><dd className="font-mono">{result.legacy_evidence_count}</dd></div>
                <div className="flex justify-between"><dt className="text-neutral-500">Claims</dt><dd className="font-mono">{result.legacy_claim_count}</dd></div>
                <div className="flex justify-between">
                  <dt className="text-neutral-500">Judge verdict</dt>
                  <dd className={`font-mono ${result.legacy_judge_verdict === 'pass' ? 'text-emerald-600 dark:text-emerald-400' : 'text-red-600 dark:text-red-400'}`}>
                    {result.legacy_judge_verdict}
                  </dd>
                </div>
                <div className="flex justify-between"><dt className="text-neutral-500">Latency</dt><dd className="font-mono">{fmtMs(result.legacy_ms)}</dd></div>
              </dl>
            </div>
            <div>
              <p className="font-semibold text-neutral-500 uppercase tracking-wider mb-1.5">LangGraph</p>
              <dl className="space-y-1">
                <div className="flex justify-between"><dt className="text-neutral-500">Answer length</dt><dd className="font-mono">{result.langgraph_answer_length} chars</dd></div>
                <div className="flex justify-between"><dt className="text-neutral-500">Evidence items</dt><dd className="font-mono">{result.langgraph_evidence_count}</dd></div>
                <div className="flex justify-between"><dt className="text-neutral-500">Claims</dt><dd className="font-mono">{result.langgraph_claim_count}</dd></div>
                <div className="flex justify-between">
                  <dt className="text-neutral-500">Judge verdict</dt>
                  <dd className={`font-mono ${result.langgraph_judge_verdict === 'pass' ? 'text-emerald-600 dark:text-emerald-400' : 'text-red-600 dark:text-red-400'}`}>
                    {result.langgraph_judge_verdict}
                  </dd>
                </div>
                <div className="flex justify-between"><dt className="text-neutral-500">Latency</dt><dd className="font-mono">{fmtMs(result.langgraph_ms)}</dd></div>
              </dl>
            </div>
          </div>

          {/* All 6 gates */}
          <div className="grid grid-cols-3 gap-2">
            {[
              { label: 'Structural', pass: result.passes_structural_gate, value: null, suffix: '' },
              { label: 'Ans ≥0.70', pass: result.passes_answer_length_gate, value: result.answer_length_ratio, suffix: '' },
              { label: 'Evid ≥0.80', pass: result.passes_evidence_gate, value: result.evidence_count_ratio, suffix: '' },
              { label: 'Claims ≥0.60', pass: result.passes_claim_gate, value: result.claim_count_ratio, suffix: '' },
              { label: 'Budget ≤1.50×', pass: result.passes_budget_gate, value: result.cost_ratio, suffix: '×' },
              {
                label: result.judge_verdict_agrees ? 'Verdict agree' : 'Verdict differ',
                pass: result.judge_verdict_agrees ?? null,
                value: null,
                suffix: '',
                note: result.judge_verdict_agrees ? undefined :
                  result.langgraph_judge_verdict === 'pass' ? 'LG better' :
                  result.legacy_judge_verdict === 'pass' ? 'LG worse' : undefined,
                amber: !result.judge_verdict_agrees && result.overall_pass,
              },
            ].map(g => {
              const baseGreen = 'bg-emerald-50 dark:bg-emerald-950/30 border-emerald-200 dark:border-emerald-900'
              const baseRed = 'bg-red-50 dark:bg-red-950/30 border-red-200 dark:border-red-900'
              const baseAmber = 'bg-amber-50 dark:bg-amber-950/30 border-amber-200 dark:border-amber-900'
              const bg = g.pass === null || g.pass
                ? (g.amber ? baseAmber : baseGreen)
                : baseRed
              const textColor = g.pass === null || g.pass
                ? (g.amber ? 'text-amber-700 dark:text-amber-400' : 'text-emerald-700 dark:text-emerald-400')
                : 'text-red-700 dark:text-red-400'
              return (
                <div key={g.label} className={`rounded-md p-2 text-center text-xs border ${bg}`}>
                  <div className={`font-mono font-bold text-sm ${textColor}`}>
                    {g.value != null ? `${fmtRatio(g.value)}${g.suffix}` : (g.pass ? '✓' : g.pass === false ? '✗' : '–')}
                  </div>
                  <div className="text-neutral-500 mt-0.5">{g.label}</div>
                  {g.note && <div className={`text-[10px] mt-0.5 ${textColor}`}>{g.note}</div>}
                </div>
              )
            })}
          </div>
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Parity sub-tab (unchanged logic from the original EvalsTab)
// ---------------------------------------------------------------------------

function ParitySubTab({ authorizedFetch }: { authorizedFetch: AuthorizedFetch }) {
  const [orchestrator, setOrchestrator] = useState<OrchestratorStatus | null>(null)
  const [runs, setRuns] = useState<ParityRunSummary[]>([])
  const [activeRunId, setActiveRunId] = useState<string | null>(null)
  const [runStatus, setRunStatus] = useState<'idle' | 'running' | 'complete' | 'error'>('idle')
  const [report, setReport] = useState<ParityReport | null>(null)
  const [caseResults, setCaseResults] = useState<ParityCaseResult[]>([])
  const [progressLog, setProgressLog] = useState<string[]>([])
  const [progressTotal, setProgressTotal] = useState<number | null>(null)
  const [progressCompleted, setProgressCompleted] = useState(0)
  const [promoting, setPromoting] = useState(false)
  const [reverting, setReverting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const logRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight
  }, [progressLog])

  function stopPolling() {
    if (pollRef.current != null) {
      clearInterval(pollRef.current)
      pollRef.current = null
    }
  }

  const applyPollSnapshot = useCallback((data: Record<string, unknown>) => {
    setCaseResults((data.progress as ParityCaseResult[]) ?? [])
    setProgressLog((data.log as string[]) ?? [])
    setProgressTotal((data.total as number | null) ?? null)
    setProgressCompleted((data.completed as number) ?? 0)

    if (data.status === 'complete') {
      setReport(data.report as ParityReport)
      setRunStatus('complete')
      stopPolling()
      // Refresh orchestrator status + run list
      authorizedFetch('/admin/evals/parity/orchestrator').then(r => { if (r.ok) r.json().then(setOrchestrator) })
      authorizedFetch('/admin/evals/parity/runs').then(r => { if (r.ok) r.json().then((d) => setRuns(d.runs ?? [])) })
    } else if (data.status === 'error') {
      setError((data.error as string) ?? 'Unknown error')
      setRunStatus('error')
      stopPolling()
    }
  }, [authorizedFetch])

  function startPolling(runId: string) {
    stopPolling()
    pollRef.current = setInterval(async () => {
      try {
        const resp = await authorizedFetch(`/admin/evals/parity/${runId}/status`)
        if (!resp.ok) return
        applyPollSnapshot(await resp.json())
      } catch { /* network hiccup — retry next interval */ }
    }, POLL_INTERVAL_MS)
  }

  const loadStatus = useCallback(async () => {
    try {
      const [orchResp, runsResp] = await Promise.all([
        authorizedFetch('/admin/evals/parity/orchestrator'),
        authorizedFetch('/admin/evals/parity/runs'),
      ])
      if (orchResp.ok) setOrchestrator(await orchResp.json())
      if (runsResp.ok) {
        const data = await runsResp.json()
        setRuns(data.runs ?? [])
        const active = (data.runs as ParityRunSummary[])?.find(r => r.status === 'running')
        if (active && !activeRunId) {
          setActiveRunId(active.run_id)
          setRunStatus('running')
          startPolling(active.run_id)
        }
      }
    } catch { /* ignore on mount */ }
  }, [authorizedFetch, activeRunId]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    loadStatus()
    return () => stopPolling()
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  async function handleStartRun() {
    setError(null)
    setReport(null)
    setCaseResults([])
    setProgressLog([])
    setProgressTotal(null)
    setProgressCompleted(0)
    setRunStatus('running')

    try {
      const resp = await authorizedFetch('/admin/evals/parity/run', { method: 'POST' })
      if (!resp.ok) throw new Error(await readErrorBody(resp, 'Could not start parity run'))
      const data = await resp.json()
      const runId = data.run_id as string
      setActiveRunId(runId)
      startPolling(runId)
      // Fetch initial snapshot immediately (don't wait for first interval)
      const snapResp = await authorizedFetch(`/admin/evals/parity/${runId}/status`)
      if (snapResp.ok) applyPollSnapshot(await snapResp.json())
    } catch (err: unknown) {
      setRunStatus('error')
      setError(err instanceof Error ? err.message : 'Failed to start parity run')
    }
  }

  async function handlePromote() {
    setPromoting(true)
    setError(null)
    try {
      const resp = await authorizedFetch('/admin/evals/parity/promote', { method: 'POST' })
      if (!resp.ok) throw new Error(await readErrorBody(resp, 'Promote failed'))
      await loadStatus()
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Promote failed')
    } finally {
      setPromoting(false)
    }
  }

  async function handleRevert() {
    setReverting(true)
    setError(null)
    try {
      const resp = await authorizedFetch('/admin/evals/parity/promote', { method: 'DELETE' })
      if (!resp.ok) throw new Error(await readErrorBody(resp, 'Revert failed'))
      await loadStatus()
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Revert failed')
    } finally {
      setReverting(false)
    }
  }

  const canPromote =
    runStatus === 'complete' && report?.cutover_recommended === true && orchestrator?.effective_orchestrator !== 'langgraph'
  const isLangGraph = orchestrator?.effective_orchestrator === 'langgraph'

  return (
    <div className="space-y-6">
      <div>
        <h3 className="text-sm font-bold text-neutral-900 dark:text-neutral-50">Pipeline parity comparison</h3>
        <p className="mt-1 text-xs text-neutral-500 dark:text-neutral-400">
          Run LangGraph against legacy across all 25 golden-set cases. All six gates must pass before LangGraph can be promoted.
        </p>
      </div>

      {orchestrator && (
        <div className={`flex items-center justify-between gap-4 rounded-xl border px-4 py-3 ${
          isLangGraph
            ? 'border-emerald-200 bg-emerald-50 dark:border-emerald-900 dark:bg-emerald-950/30'
            : 'border-neutral-200 bg-neutral-50 dark:border-neutral-800 dark:bg-neutral-900'
        }`}>
          <div>
            <p className="text-sm font-semibold text-neutral-900 dark:text-neutral-50">
              Active orchestrator:{' '}
              <span className={isLangGraph ? 'text-emerald-600 dark:text-emerald-400' : 'text-neutral-700 dark:text-neutral-300'}>
                {orchestrator.effective_orchestrator}
              </span>
              {orchestrator.override_active && (
                <span className="ml-2 text-xs font-normal text-amber-600 dark:text-amber-400">(process-lifetime override)</span>
              )}
            </p>
            <p className="text-xs text-neutral-500 mt-0.5">
              {orchestrator.override_active
                ? `Override active. Env default: ${orchestrator.env_default}. Set FRONEI_ORCHESTRATOR=${orchestrator.override_value} to persist.`
                : `From env/config: FRONEI_ORCHESTRATOR=${orchestrator.env_default}`}
            </p>
          </div>
          {isLangGraph && (
            <button
              type="button"
              onClick={handleRevert}
              disabled={reverting}
              className="flex items-center gap-1.5 rounded-lg border border-neutral-300 dark:border-neutral-700 px-3 py-1.5 text-xs font-semibold text-neutral-700 dark:text-neutral-300 hover:bg-neutral-100 dark:hover:bg-neutral-800 disabled:opacity-50"
            >
              {reverting ? <Loader2 size={12} className="animate-spin" /> : <RotateCcw size={12} />}
              Revert to legacy
            </button>
          )}
        </div>
      )}

      {error && (
        <div className="flex items-start gap-2 rounded-lg border border-red-200 bg-red-50 dark:border-red-900 dark:bg-red-950/30 px-4 py-3 text-sm text-red-700 dark:text-red-400">
          <AlertTriangle size={14} className="mt-0.5 flex-shrink-0" />
          {error}
        </div>
      )}

      <div className="flex items-center gap-3">
        <button
          type="button"
          onClick={handleStartRun}
          disabled={runStatus === 'running'}
          className="flex items-center gap-2 rounded-lg bg-neutral-900 px-4 py-2 text-sm font-semibold text-white hover:bg-neutral-700 disabled:opacity-50 dark:bg-white dark:text-neutral-900 dark:hover:bg-neutral-200"
        >
          {runStatus === 'running'
            ? <><Loader2 size={14} className="animate-spin" /> Running…</>
            : <><Play size={14} /> Run parity comparison</>}
        </button>
        {runStatus !== 'idle' && (
          <button
            type="button"
            onClick={loadStatus}
            className="flex items-center gap-1.5 rounded-lg border border-neutral-200 dark:border-neutral-800 px-3 py-2 text-xs font-semibold text-neutral-600 dark:text-neutral-400 hover:bg-neutral-50 dark:hover:bg-neutral-900"
          >
            <RefreshCw size={12} /> Refresh
          </button>
        )}
        {canPromote && (
          <button
            type="button"
            onClick={handlePromote}
            disabled={promoting}
            className="flex items-center gap-2 rounded-lg bg-emerald-600 px-4 py-2 text-sm font-semibold text-white hover:bg-emerald-700 disabled:opacity-50"
          >
            {promoting ? <Loader2 size={14} className="animate-spin" /> : <Rocket size={14} />}
            Promote LangGraph
          </button>
        )}
      </div>

      {(progressLog.length > 0 || runStatus === 'running') && (
        <div>
          {runStatus === 'running' && progressTotal != null && (
            <div className="flex items-center gap-3 mb-2">
              <div className="flex-1 h-1.5 rounded-full bg-neutral-200 dark:bg-neutral-800 overflow-hidden">
                <div
                  className="h-full bg-blue-500 transition-all duration-500"
                  style={{ width: `${(progressCompleted / progressTotal) * 100}%` }}
                />
              </div>
              <span className="text-xs text-neutral-500 tabular-nums">
                {progressCompleted}/{progressTotal}
              </span>
            </div>
          )}
          <div
            ref={logRef}
            className="rounded-xl border border-neutral-200 dark:border-neutral-800 bg-neutral-950 p-4 font-mono text-xs text-neutral-300 max-h-48 overflow-y-auto space-y-0.5"
          >
            {progressLog.map((line, i) => (
              <div key={i}>{line}</div>
            ))}
            {runStatus === 'running' && (
              <div className="flex items-center gap-1.5 text-neutral-500 mt-1">
                <Loader2 size={10} className="animate-spin" /> polling…
              </div>
            )}
          </div>
        </div>
      )}

      {report && (
        <div className="rounded-xl border border-neutral-200 dark:border-neutral-800 overflow-hidden">
          <div className={`flex items-center justify-between px-4 py-3 border-b border-neutral-200 dark:border-neutral-800 ${
            report.cutover_recommended ? 'bg-emerald-50 dark:bg-emerald-950/30' : 'bg-red-50 dark:bg-red-950/30'
          }`}>
            <div className="flex items-center gap-2">
              {report.cutover_recommended
                ? <CheckCircle2 size={16} className="text-emerald-500" />
                : <XCircle size={16} className="text-red-500" />}
              <span className="text-sm font-bold text-neutral-900 dark:text-neutral-50">
                {report.cutover_recommended ? 'All gates pass — cutover recommended' : 'Gates not met — do not promote yet'}
              </span>
            </div>
            <span className="text-xs text-neutral-500">{report.overall_pass}/{report.total_cases} cases pass all gates</span>
          </div>

          {report.cutover_blockers.length > 0 && (
            <div className="px-4 py-3 border-b border-neutral-200 dark:border-neutral-800 bg-white dark:bg-neutral-900">
              {report.cutover_blockers.map((b, i) => (
                <div key={i} className="flex items-start gap-2 text-xs text-red-600 dark:text-red-400 py-0.5">
                  <XCircle size={12} className="mt-0.5 flex-shrink-0" /> {b}
                </div>
              ))}
            </div>
          )}

          <div className="px-4 py-2 bg-white dark:bg-neutral-900">
            <GateRow label="Structural (no crash)" pass={report.structural_pass} total={report.total_cases} threshold={1.0} />
            <GateRow label="Answer length ≥70% of legacy" pass={report.answer_length_gate_pass} total={report.total_cases} threshold={0.80} />
            <GateRow label="Evidence count ≥80% of legacy" pass={report.evidence_gate_pass} total={report.total_cases} threshold={0.80} />
            <GateRow label="Claim count ≥60% of legacy" pass={report.claim_gate_pass} total={report.total_cases} threshold={0.80} />
            <GateRow label="Budget ≤1.5× legacy" pass={report.budget_gate_pass} total={report.total_cases} threshold={0.80} />
            <GateRow label="Judge verdict agreement" pass={report.verdict_agree} total={report.total_cases} threshold={0.80} />
          </div>

          <div className="grid grid-cols-4 divide-x divide-neutral-100 dark:divide-neutral-800 border-t border-neutral-200 dark:border-neutral-800 bg-neutral-50 dark:bg-neutral-950">
            {[
              { label: 'Median ans ratio', value: report.median_answer_length_ratio, threshold: 0.70 },
              { label: 'Median evid ratio', value: report.median_evidence_count_ratio, threshold: 0.80 },
              { label: 'Median claim ratio', value: report.median_claim_count_ratio, threshold: 0.70 },
              { label: 'Median cost ratio', value: report.median_cost_ratio, threshold: 1.50, invert: true },
            ].map(m => (
              <div key={m.label} className="px-4 py-3 text-center">
                <RatioBadge value={m.value} threshold={m.threshold} invert={m.invert} />
                <p className="text-[11px] text-neutral-400 mt-0.5">{m.label}</p>
              </div>
            ))}
          </div>
        </div>
      )}

      {caseResults.length > 0 && (
        <div>
          <h3 className="text-sm font-semibold text-neutral-700 dark:text-neutral-300 mb-3">
            Per-case results ({caseResults.length}{report ? `/${report.total_cases}` : ''})
          </h3>
          {caseResults.map(r => (
            <CaseRow key={r.case_id} result={r} />
          ))}
        </div>
      )}

      {runs.length > 0 && runStatus === 'idle' && (
        <div>
          <h3 className="text-sm font-semibold text-neutral-700 dark:text-neutral-300 mb-2">Recent runs</h3>
          <div className="rounded-xl border border-neutral-200 dark:border-neutral-800 overflow-hidden divide-y divide-neutral-100 dark:divide-neutral-800">
            {runs.slice(0, 5).map(r => (
              <div key={r.run_id} className="flex items-center justify-between px-4 py-2.5 bg-white dark:bg-neutral-900 text-sm">
                <span className="font-mono text-xs text-neutral-500">{r.run_id}</span>
                <div className="flex items-center gap-3">
                  <span className={`text-xs font-semibold ${
                    r.status === 'complete' ? 'text-emerald-600 dark:text-emerald-400'
                    : r.status === 'error' ? 'text-red-600 dark:text-red-400'
                    : 'text-amber-600 dark:text-amber-400'
                  }`}>{r.status}</span>
                  {r.cutover_recommended != null && (
                    r.cutover_recommended
                      ? <CheckCircle2 size={12} className="text-emerald-500" />
                      : <XCircle size={12} className="text-red-500" />
                  )}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Top-level EvalsTab — two sub-tabs: Eval Harness | Parity
// ---------------------------------------------------------------------------

type EvalsSubTab = 'harness' | 'parity'

const SUB_TABS: { id: EvalsSubTab; label: string }[] = [
  { id: 'harness', label: 'Eval Harness' },
  { id: 'parity', label: 'Parity' },
]

export function EvalsTab({ authorizedFetch }: { authorizedFetch: AuthorizedFetch }) {
  const [subTab, setSubTab] = useState<EvalsSubTab>('harness')

  return (
    <div className="space-y-5 max-w-4xl">
      {/* Header */}
      <div>
        <h2 className="text-base font-bold text-neutral-900 dark:text-neutral-50">Evals</h2>
        <p className="mt-0.5 text-xs text-neutral-500">
          Manage eval cases, trigger runs, and track parity for the LangGraph migration gate.
        </p>
      </div>

      {/* Sub-tab nav */}
      <div className="flex gap-1 border-b border-neutral-200 dark:border-neutral-800">
        {SUB_TABS.map(t => (
          <button
            key={t.id}
            type="button"
            onClick={() => setSubTab(t.id)}
            className={`px-4 py-2 text-xs font-semibold border-b-2 -mb-px transition-colors ${
              subTab === t.id
                ? 'border-neutral-900 dark:border-white text-neutral-900 dark:text-white'
                : 'border-transparent text-neutral-500 hover:text-neutral-700 dark:hover:text-neutral-300'
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>

      {/* Sub-tab content */}
      <div>
        {subTab === 'harness' && <EvalHarnessTab authorizedFetch={authorizedFetch} />}
        {subTab === 'parity' && <ParitySubTab authorizedFetch={authorizedFetch} />}
      </div>
    </div>
  )
}
