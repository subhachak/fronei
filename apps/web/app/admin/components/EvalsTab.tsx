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
import { readErrorBody } from '../../lib/api'
import type {
  AuthorizedFetch,
  OrchestratorStatus,
  ParityCaseResult,
  ParityReport,
  ParityRunSummary,
} from '../types'
import { EvalsCasesTab } from './EvalsCasesTab'
import { EvalsRunsTab } from './EvalsRunsTab'

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
        <div className="flex items-center gap-4 text-xs text-neutral-500">
          <span title="Answer length ratio">ans {fmtRatio(result.answer_length_ratio)}</span>
          <span title="Evidence count ratio">evid {fmtRatio(result.evidence_count_ratio)}</span>
          <span title="Claim count ratio">claims {fmtRatio(result.claim_count_ratio)}</span>
          <span title={
            result.judge_verdict_agrees
              ? `Both agree: ${result.legacy_judge_verdict}`
              : `Disagree — legacy: ${result.legacy_judge_verdict}, LG: ${result.langgraph_judge_verdict}`
          }>
            {result.judge_verdict_agrees
              ? `✓ ${result.legacy_judge_verdict}`
              : result.langgraph_judge_verdict === 'pass' && result.legacy_judge_verdict !== 'pass'
                ? '↑ LG better'
                : result.legacy_judge_verdict === 'pass' && result.langgraph_judge_verdict !== 'pass'
                  ? '↓ LG worse'
                  : '≠ verdict'}
          </span>
        </div>
        {open ? <ChevronDown size={14} className="text-neutral-400 flex-shrink-0" /> : <ChevronRight size={14} className="text-neutral-400 flex-shrink-0" />}
      </button>

      {open && (
        <div className="border-t border-neutral-100 dark:border-neutral-800 bg-neutral-50 dark:bg-neutral-950 px-4 py-3">
          {(result.legacy_error || result.langgraph_error) && (
            <div className="mb-3 rounded-md bg-red-50 dark:bg-red-950/30 border border-red-200 dark:border-red-900 p-3 text-xs font-mono text-red-700 dark:text-red-400 whitespace-pre-wrap">
              {result.legacy_error && <p><strong>Legacy error:</strong> {result.legacy_error.slice(0, 400)}</p>}
              {result.langgraph_error && <p className="mt-1"><strong>LangGraph error:</strong> {result.langgraph_error.slice(0, 400)}</p>}
            </div>
          )}

          <div className="grid grid-cols-2 gap-4 text-xs">
            <div>
              <p className="font-semibold text-neutral-500 uppercase tracking-wider mb-1.5">Legacy</p>
              <dl className="space-y-1">
                <div className="flex justify-between"><dt className="text-neutral-500">Answer length</dt><dd className="font-mono">{result.legacy_answer_length} chars</dd></div>
                <div className="flex justify-between"><dt className="text-neutral-500">Evidence items</dt><dd className="font-mono">{result.legacy_evidence_count}</dd></div>
                <div className="flex justify-between"><dt className="text-neutral-500">Claims</dt><dd className="font-mono">{result.legacy_claim_count}</dd></div>
                <div className="flex justify-between"><dt className="text-neutral-500">Judge verdict</dt><dd className="font-mono">{result.legacy_judge_verdict}</dd></div>
                <div className="flex justify-between"><dt className="text-neutral-500">Latency</dt><dd className="font-mono">{fmtMs(result.legacy_ms)}</dd></div>
              </dl>
            </div>
            <div>
              <p className="font-semibold text-neutral-500 uppercase tracking-wider mb-1.5">LangGraph</p>
              <dl className="space-y-1">
                <div className="flex justify-between"><dt className="text-neutral-500">Answer length</dt><dd className="font-mono">{result.langgraph_answer_length} chars</dd></div>
                <div className="flex justify-between"><dt className="text-neutral-500">Evidence items</dt><dd className="font-mono">{result.langgraph_evidence_count}</dd></div>
                <div className="flex justify-between"><dt className="text-neutral-500">Claims</dt><dd className="font-mono">{result.langgraph_claim_count}</dd></div>
                <div className="flex justify-between"><dt className="text-neutral-500">Judge verdict</dt><dd className="font-mono">{result.langgraph_judge_verdict}</dd></div>
                <div className="flex justify-between"><dt className="text-neutral-500">Latency</dt><dd className="font-mono">{fmtMs(result.langgraph_ms)}</dd></div>
              </dl>
            </div>
          </div>

          <div className="mt-3 grid grid-cols-3 gap-2">
            {[
              { label: 'Ans ratio ≥0.70', value: result.answer_length_ratio, threshold: 0.70, pass: result.passes_answer_length_gate },
              { label: 'Evid ratio ≥0.80', value: result.evidence_count_ratio, threshold: 0.80, pass: result.passes_evidence_gate },
              { label: 'Claim ratio ≥0.70', value: result.claim_count_ratio, threshold: 0.70, pass: result.passes_claim_gate },
            ].map(g => (
              <div key={g.label} className={`rounded-md p-2 text-center text-xs border ${g.pass ? 'bg-emerald-50 dark:bg-emerald-950/30 border-emerald-200 dark:border-emerald-900' : 'bg-red-50 dark:bg-red-950/30 border-red-200 dark:border-red-900'}`}>
                <div className={`font-mono font-bold text-sm ${g.pass ? 'text-emerald-700 dark:text-emerald-400' : 'text-red-700 dark:text-red-400'}`}>
                  {fmtRatio(g.value)}
                </div>
                <div className="text-neutral-500 mt-0.5">{g.label}</div>
              </div>
            ))}
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
  const [streamLog, setStreamLog] = useState<string[]>([])
  const [promoting, setPromoting] = useState(false)
  const [reverting, setReverting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const eventSourceRef = useRef<EventSource | null>(null)

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
        const active = data.runs?.find((r: ParityRunSummary) => r.status === 'running')
        if (active && !activeRunId) {
          setActiveRunId(active.run_id)
          setRunStatus('running')
          startSSE(active.run_id)
        }
      }
    } catch { /* ignore on mount */ }
  }, [authorizedFetch, activeRunId]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    loadStatus()
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  function startSSE(runId: string) {
    if (eventSourceRef.current) {
      eventSourceRef.current.close()
    }
    const controller = new AbortController()

    async function streamViaFetch() {
      try {
        const resp = await authorizedFetch(`/admin/evals/parity/${runId}/stream`, {
          signal: controller.signal,
        })
        if (!resp.ok || !resp.body) return
        const reader = resp.body.getReader()
        const decoder = new TextDecoder()
        let buf = ''
        while (true) {
          const { done, value } = await reader.read()
          if (done) break
          buf += decoder.decode(value, { stream: true })
          const parts = buf.split('\n\n')
          buf = parts.pop() ?? ''
          for (const part of parts) {
            const line = part.trim()
            if (line.startsWith('event: close')) {
              reader.cancel()
              break
            }
            if (line.startsWith('event: heartbeat')) continue
            if (line.startsWith('data: ')) {
              try {
                const event = JSON.parse(line.slice(6))
                handleSSEEvent(event)
              } catch { /* malformed */ }
            }
          }
        }
      } catch (err: unknown) {
        if ((err as Error)?.name !== 'AbortError') {
          setRunStatus('error')
          setError('Lost connection to parity run stream.')
        }
      }
    }

    streamViaFetch()
    eventSourceRef.current = { close: () => controller.abort() } as unknown as EventSource
  }

  function handleSSEEvent(event: Record<string, unknown>) {
    const type = event.type as string
    if (type === 'started') {
      setStreamLog(l => [...l, `▶ Run started — ${event.total} case(s)`])
    } else if (type === 'case_start') {
      setStreamLog(l => [...l, `  [${(event.index as number) + 1}/${event.total}] Running ${event.case_id}…`])
    } else if (type === 'case_result') {
      const r = event.result as ParityCaseResult
      setCaseResults(prev => [...prev, r])
      const icon = r.overall_pass ? '✓' : '✗'
      setStreamLog(l => [...l, `  ${icon} ${event.case_id} — ans=${fmtRatio(r.answer_length_ratio)} evid=${fmtRatio(r.evidence_count_ratio)}`])
    } else if (type === 'complete') {
      setReport(event.report as ParityReport)
      setRunStatus('complete')
      setStreamLog(l => [...l, `✅ Run complete`])
      loadStatus()
    } else if (type === 'error') {
      setError(String(event.error))
      setRunStatus('error')
      setStreamLog(l => [...l, `❌ Error: ${event.error}`])
    }
  }

  useEffect(() => {
    return () => {
      eventSourceRef.current?.close()
    }
  }, [])

  async function handleStartRun() {
    setError(null)
    setReport(null)
    setCaseResults([])
    setStreamLog([])
    setRunStatus('running')

    try {
      const resp = await authorizedFetch('/admin/evals/parity/run', { method: 'POST' })
      if (!resp.ok) throw new Error(await readErrorBody(resp, 'Could not start parity run'))
      const data = await resp.json()
      const runId = data.run_id as string
      setActiveRunId(runId)
      if (data.status === 'already_running') {
        setStreamLog([`⚠ A run is already in progress (${runId}), attaching…`])
      } else {
        setStreamLog([`▶ Started run ${runId}`])
      }
      startSSE(runId)
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

      {streamLog.length > 0 && (
        <div className="rounded-xl border border-neutral-200 dark:border-neutral-800 bg-neutral-950 p-4 font-mono text-xs text-neutral-300 max-h-48 overflow-y-auto space-y-0.5">
          {streamLog.map((line, i) => (
            <div key={i}>{line}</div>
          ))}
          {runStatus === 'running' && (
            <div className="flex items-center gap-1.5 text-neutral-500 mt-1">
              <Loader2 size={10} className="animate-spin" /> processing…
            </div>
          )}
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
// Top-level EvalsTab — three sub-tabs
// ---------------------------------------------------------------------------

type EvalsSubTab = 'cases' | 'runs' | 'parity'

const SUB_TABS: { id: EvalsSubTab; label: string }[] = [
  { id: 'cases', label: 'Cases' },
  { id: 'runs', label: 'Runs' },
  { id: 'parity', label: 'Parity' },
]

export function EvalsTab({ authorizedFetch }: { authorizedFetch: AuthorizedFetch }) {
  const [subTab, setSubTab] = useState<EvalsSubTab>('cases')

  return (
    <div className="space-y-5 max-w-4xl">
      {/* Header */}
      <div>
        <h2 className="text-base font-bold text-neutral-900 dark:text-neutral-50">Evals</h2>
        <p className="mt-0.5 text-xs text-neutral-500">
          Manage eval cases, run both pipelines, and track parity for the LangGraph migration gate.
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
        {subTab === 'cases' && <EvalsCasesTab authorizedFetch={authorizedFetch} />}
        {subTab === 'runs' && <EvalsRunsTab authorizedFetch={authorizedFetch} />}
        {subTab === 'parity' && <ParitySubTab authorizedFetch={authorizedFetch} />}
      </div>
    </div>
  )
}
