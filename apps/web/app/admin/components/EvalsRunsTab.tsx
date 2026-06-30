'use client'

import { ChevronDown, ChevronRight, ExternalLink, Play } from 'lucide-react'
import { useCallback, useEffect, useRef, useState } from 'react'
import { readErrorBody } from '../../lib/api'
import type { AuthorizedFetch, EvalCase, EvalCaseRunResult, EvalPipeline, EvalRunResult, EvalRunSummary } from '../types'

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
        {' — '}set <code className="bg-neutral-100 dark:bg-neutral-800 px-1 rounded">LANGSMITH_API_KEY</code> to enable experiment tracking and the LangSmith eval runner.
        Runs will use the in-process scorer instead.
      </div>
    )
  }

  return (
    <div className="rounded-lg border border-emerald-200 dark:border-emerald-900 bg-emerald-50 dark:bg-emerald-950/30 px-4 py-3 text-xs flex items-center gap-3">
      <span className="h-2 w-2 rounded-full bg-emerald-500 flex-shrink-0" />
      <div className="flex-1">
        <span className="font-semibold text-emerald-800 dark:text-emerald-300">LangSmith active</span>
        {' — '}project <code className="bg-emerald-100 dark:bg-emerald-900/50 px-1 rounded text-emerald-700 dark:text-emerald-400">{status.project}</code>
        {', dataset '}
        <code className="bg-emerald-100 dark:bg-emerald-900/50 px-1 rounded text-emerald-700 dark:text-emerald-400">{status.dataset_name}</code>
        {status.tracing_on && <span className="ml-2 text-emerald-600 dark:text-emerald-400">· tracing on</span>}
      </div>
      <a
        href="https://smith.langchain.com"
        target="_blank"
        rel="noopener noreferrer"
        className="flex items-center gap-1 text-emerald-700 dark:text-emerald-400 hover:underline font-semibold"
      >
        Open LangSmith <ExternalLink size={11} />
      </a>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Small helpers
// ---------------------------------------------------------------------------

function pct(v: number | null | undefined) {
  if (v == null) return '—'
  return `${Math.round(v * 100)}%`
}

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

function Pass({ ok }: { ok: boolean | undefined }) {
  if (ok == null) return <span className="text-neutral-300 text-[11px]">?</span>
  return ok
    ? <span className="text-[11px] font-bold text-green-600 dark:text-green-400">✓ pass</span>
    : <span className="text-[11px] font-bold text-red-600 dark:text-red-400">✗ fail</span>
}

// ---------------------------------------------------------------------------
// Case result card (expandable)
// ---------------------------------------------------------------------------

function CaseResultRow({ r }: { r: EvalCaseRunResult }) {
  const [open, setOpen] = useState(false)
  const data = r.run
  const pipelineLabel = r.pipeline === 'legacy' ? 'Legacy' : 'LangGraph'

  return (
    <div className="border border-neutral-200 dark:border-neutral-800 rounded-xl overflow-hidden">
      {/* Summary row */}
      <button
        type="button"
        onClick={() => setOpen(o => !o)}
        className="w-full flex items-center gap-3 px-4 py-3 text-left bg-white dark:bg-neutral-900 hover:bg-neutral-50 dark:hover:bg-neutral-800 transition-colors"
      >
        <span className="flex-shrink-0 text-neutral-400">
          {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        </span>
        <span className="flex-1 min-w-0">
          <span className="text-sm font-semibold text-neutral-900 dark:text-neutral-50 truncate block">{r.title}</span>
          <span className="text-xs text-neutral-400 truncate block">{r.query}</span>
        </span>
        <div className="flex items-center gap-3 flex-shrink-0">
          <Pass ok={r.overall_structural_pass} />
          <span className="text-xs text-neutral-400">{pipelineLabel}</span>
          <ScoreBadge score={data.criteria?.score} />
        </div>
      </button>

      {open && (
        <div className="px-4 pb-4 pt-2 bg-white dark:bg-neutral-900 border-t border-neutral-100 dark:border-neutral-800 space-y-4">
          {/* Pipeline run detail — graded against this case's expected_criteria (ground truth) */}
          <div className="rounded-lg border border-neutral-100 dark:border-neutral-800 p-3">
            <p className="text-xs font-bold text-neutral-600 dark:text-neutral-400 mb-2">{pipelineLabel}</p>
            {!data.ok ? (
              <p className="text-xs text-red-600 dark:text-red-400 font-mono whitespace-pre-wrap break-words">
                {data.error ?? 'Error'}
              </p>
            ) : (
              <dl className="space-y-1 text-xs">
                <div className="flex justify-between">
                  <dt className="text-neutral-500">Answer length</dt>
                  <dd className="font-semibold text-neutral-800 dark:text-neutral-200">{data.answer_length.toLocaleString()} chars</dd>
                </div>
                <div className="flex justify-between">
                  <dt className="text-neutral-500">Evidence</dt>
                  <dd className="font-semibold text-neutral-800 dark:text-neutral-200">{data.evidence_count} items</dd>
                </div>
                <div className="flex justify-between">
                  <dt className="text-neutral-500">Claims</dt>
                  <dd className="font-semibold text-neutral-800 dark:text-neutral-200">{data.claim_count}</dd>
                </div>
                <div className="flex justify-between">
                  <dt className="text-neutral-500">Latency</dt>
                  <dd className="font-semibold text-neutral-800 dark:text-neutral-200">{(data.latency_ms / 1000).toFixed(1)}s</dd>
                </div>
                {data.criteria && (
                  <>
                    <div className="flex justify-between">
                      <dt className="text-neutral-500">Criteria score</dt>
                      <dd><ScoreBadge score={data.criteria.score} /></dd>
                    </div>
                    {data.criteria.passed.length > 0 && (
                      <div>
                        <dt className="text-neutral-500 mb-0.5">Passed</dt>
                        <dd className="space-y-0.5">
                          {data.criteria.passed.map((p, i) => (
                            <p key={i} className="text-green-700 dark:text-green-400">✓ {p}</p>
                          ))}
                        </dd>
                      </div>
                    )}
                    {data.criteria.failed.length > 0 && (
                      <div>
                        <dt className="text-neutral-500 mb-0.5">Failed</dt>
                        <dd className="space-y-0.5">
                          {data.criteria.failed.map((p, i) => (
                            <p key={i} className="text-red-600 dark:text-red-400">✗ {p}</p>
                          ))}
                        </dd>
                      </div>
                    )}
                    <div>
                      <dt className="text-neutral-500">Explanation</dt>
                      <dd className="text-neutral-700 dark:text-neutral-300 mt-0.5">{data.criteria.explanation}</dd>
                    </div>
                  </>
                )}
              </dl>
            )}
          </div>

          {/* Structural checks */}
          <div>
            <p className="text-xs font-bold text-neutral-500 mb-2">Structural checks</p>
            <div className="grid grid-cols-2 gap-x-4 gap-y-1">
              {Object.entries(r.structural).map(([k, v]) => (
                <div key={k} className="flex items-center gap-1.5 text-xs">
                  <span className={v ? 'text-green-600 dark:text-green-400' : 'text-red-600 dark:text-red-400'}>
                    {v ? '✓' : '✗'}
                  </span>
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

// ---------------------------------------------------------------------------
// Runs tab — trigger + history
// ---------------------------------------------------------------------------

export function EvalsRunsTab({ authorizedFetch }: { authorizedFetch: AuthorizedFetch }) {
  const [cases, setCases] = useState<EvalCase[]>([])
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set())
  const [runStatus, setRunStatus] = useState<'idle' | 'running' | 'complete' | 'error'>('idle')
  const [log, setLog] = useState<string[]>([])
  // Always use the envelope type — cases[] for in-process, langsmith for LS runs.
  const [runResult, setRunResult] = useState<EvalRunResult | null>(null)
  const [runs, setRuns] = useState<EvalRunSummary[]>([])
  const [error, setError] = useState('')
  const [langsmithLinks, setLangsmithLinks] = useState<{ legacy?: string; langgraph?: string }>({})
  const [pipeline, setPipeline] = useState<EvalPipeline>('langgraph')
  const logRef = useRef<HTMLDivElement>(null)
  const abortRef = useRef<AbortController | null>(null)

  useEffect(() => {
    if (logRef.current) {
      logRef.current.scrollTop = logRef.current.scrollHeight
    }
  }, [log])

  const loadCases = useCallback(async () => {
    try {
      const resp = await authorizedFetch('/admin/evals/cases')
      if (!resp.ok) return
      const data = await resp.json()
      setCases(data.items ?? [])
    } catch {}
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

  function toggleCase(id: number) {
    setSelectedIds(prev => {
      const next = new Set(prev)
      next.has(id) ? next.delete(id) : next.add(id)
      return next
    })
  }

  async function startRun() {
    setRunStatus('running')
    setLog([])
    setRunResult(null)
    setError('')
    setLangsmithLinks({})

    const payload = selectedIds.size > 0
      ? { case_ids: Array.from(selectedIds), pipeline }
      : { pipeline }

    const startResp = await authorizedFetch('/admin/evals/runs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
    if (!startResp.ok) {
      const msg = await readErrorBody(startResp, 'Failed to start eval run')
      setError(msg)
      setRunStatus('error')
      return
    }
    const { run_id } = await startResp.json()

    const abort = new AbortController()
    abortRef.current = abort

    const streamResp = await authorizedFetch(`/admin/evals/runs/${run_id}/stream`, { signal: abort.signal })
    if (!streamResp.ok || !streamResp.body) {
      setError('Could not open SSE stream')
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
          try {
            const ev = JSON.parse(payload)
            handleSSEEvent(ev)
          } catch {}
        }
      }
    } catch (err: unknown) {
      if ((err as Error)?.name !== 'AbortError') {
        setError('Stream interrupted')
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
        const ok = r.run.ok ? '✓' : '✗'
        const score = pct(r.run.criteria?.score)
        setLog(l => [...l, `  → ${r.pipeline} ${ok} (criteria ${score})`])
        // Accumulate into the envelope's cases list
        setRunResult(prev => ({
          mode: 'in_process',
          pipeline: r.pipeline,
          cases: [...(prev?.cases ?? []), r],
          langsmith: null,
        }))
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
        if (url) {
          setLangsmithLinks(prev => ({ ...prev, [ev.pipeline as string]: url }))
        }
        setLog(l => [...l, `  ✓ ${ev.pipeline} done${ev.elapsed_s ? ` (${ev.elapsed_s}s)` : ''}${url ? ' — experiment ready' : ''}`])
        break
      }
      case 'langsmith_pipeline_error':
        setLog(l => [...l, `  ✗ ${ev.pipeline} error: ${ev.error}`])
        break
      case 'complete': {
        // The complete event carries the full envelope; use it as the final
        // source of truth (avoids any gap if a case_result event was missed).
        const envelope = ev.results as import('../types').EvalRunResult | undefined
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

  return (
    <div className="space-y-6">
      <LangSmithBanner authorizedFetch={authorizedFetch} />

      {/* Case selection + run trigger */}
      <div className="rounded-xl border border-neutral-200 dark:border-neutral-800 p-4 space-y-3">
        <div className="flex items-center justify-between">
          <div>
            <h3 className="text-sm font-bold text-neutral-900 dark:text-neutral-50">Run evals</h3>
            <p className="text-xs text-neutral-500 mt-0.5">
              {cases.length === 0
                ? 'No cases — add some on the Cases tab first.'
                : selectedIds.size === 0
                  ? `Will run all ${cases.length} case(s)`
                  : `${selectedIds.size} of ${cases.length} selected`}
            </p>
          </div>
          <div className="flex items-center gap-2">
            <select
              value={pipeline}
              onChange={e => setPipeline(e.target.value as EvalPipeline)}
              disabled={runStatus === 'running'}
              title="Each case is graded against its own expected_criteria (ground truth) for the selected pipeline. To compare legacy vs langgraph head-to-head, use the Parity tab instead."
              className="rounded-lg border border-neutral-200 dark:border-neutral-700 bg-white dark:bg-neutral-900 px-2 py-2 text-xs font-semibold text-neutral-700 dark:text-neutral-300 disabled:opacity-40"
            >
              <option value="langgraph">LangGraph</option>
              <option value="legacy">Legacy</option>
            </select>
            <button
              type="button"
              disabled={!canRun}
              onClick={startRun}
              className="flex items-center gap-1.5 rounded-lg bg-neutral-900 dark:bg-white px-3 py-2 text-xs font-semibold text-white dark:text-neutral-900 hover:bg-neutral-700 dark:hover:bg-neutral-200 disabled:opacity-40"
            >
              <Play size={12} /> {runStatus === 'running' ? 'Running…' : 'Run'}
            </button>
          </div>
        </div>

        {cases.length > 0 && (
          <div className="grid grid-cols-1 gap-1 max-h-36 overflow-y-auto">
            {cases.map(c => (
              <label key={c.id} className="flex items-center gap-2 cursor-pointer select-none rounded px-2 py-1 hover:bg-neutral-50 dark:hover:bg-neutral-800">
                <input
                  type="checkbox"
                  checked={selectedIds.has(c.id)}
                  onChange={() => toggleCase(c.id)}
                  className="rounded border-neutral-300 dark:border-neutral-600 text-neutral-900"
                />
                <span className="text-xs text-neutral-700 dark:text-neutral-300 truncate">{c.title}</span>
                {c.category && (
                  <span className="text-[10px] text-neutral-400 uppercase tracking-wide">{c.category}</span>
                )}
              </label>
            ))}
          </div>
        )}
      </div>

      {/* Live log */}
      {(log.length > 0 || runStatus === 'running') && (
        <div>
          <p className="text-xs font-bold text-neutral-500 mb-2">Progress</p>
          <div
            ref={logRef}
            className="rounded-xl bg-neutral-950 text-green-300 font-mono text-xs p-4 max-h-52 overflow-y-auto space-y-0.5"
          >
            {log.map((line, i) => <div key={i}>{line}</div>)}
            {runStatus === 'running' && <div className="animate-pulse">⋯</div>}
          </div>
          {error && <p className="mt-2 text-xs text-red-600 dark:text-red-400">{error}</p>}
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

      {/* Results — in-process per-case rows */}
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
      {/* Results — LangSmith mode note */}
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
                <span
                  className={`h-2 w-2 rounded-full flex-shrink-0 ${
                    r.status === 'complete' ? 'bg-green-500'
                    : r.status === 'error' ? 'bg-red-500'
                    : 'bg-yellow-400 animate-pulse'
                  }`}
                />
                <span className="flex-1 font-mono text-neutral-500 truncate">{r.run_id}</span>
                <span className="text-neutral-500">{r.case_count} case(s)</span>
                <span className={`font-semibold ${
                  r.status === 'complete' ? 'text-green-700 dark:text-green-400'
                  : r.status === 'error' ? 'text-red-600 dark:text-red-400'
                  : 'text-yellow-600 dark:text-yellow-400'
                }`}>{r.status}</span>
                {r.started_at && (
                  <span className="text-neutral-400">{new Date(r.started_at).toLocaleString()}</span>
                )}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
