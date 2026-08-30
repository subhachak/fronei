'use client'

import { Check, Ban, Loader2, RefreshCw, Sparkles, Volume2 } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import type { BankCoverage, BankItem, GenerationRun, Spec } from '../types'
import type { useCelpip } from '../hooks/useCelpip'
import { BUTTON, BUTTON_QUIET, CARD, ErrorNote, INPUT, LABEL, SectionHeading, formatDate } from './ui'

type Api = ReturnType<typeof useCelpip>

const STATUS_TONE: Record<string, string> = {
  ready: 'bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-300',
  awaiting_assets: 'bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-300',
  draft: 'bg-neutral-100 text-neutral-600 dark:bg-neutral-800 dark:text-neutral-300',
  disabled: 'bg-neutral-200 text-neutral-500 dark:bg-neutral-800',
  rejected: 'bg-rose-100 text-rose-800 dark:bg-rose-950 dark:text-rose-300',
  retired: 'bg-neutral-200 text-neutral-500 dark:bg-neutral-800',
}

export function QuestionBankView({ api }: { api: Api }) {
  const [spec, setSpec] = useState<Spec | null>(null)
  const [items, setItems] = useState<BankItem[]>([])
  const [coverage, setCoverage] = useState<BankCoverage[]>([])
  const [runs, setRuns] = useState<GenerationRun[]>([])
  const [filter, setFilter] = useState('')
  const [taskKey, setTaskKey] = useState('')
  const [count, setCount] = useState(3)
  const [difficulty, setDifficulty] = useState(9)
  const [topic, setTopic] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [detail, setDetail] = useState<BankItem | null>(null)

  const load = useCallback(async () => {
    try {
      const [s, bank, runList] = await Promise.all([
        api.getJson<Spec>('/admin/celpip/spec'),
        api.getJson<{ questions: BankItem[]; coverage: BankCoverage[] }>(
          `/admin/celpip/bank${filter ? `?task_key=${filter}` : ''}`,
        ),
        api.getJson<{ runs: GenerationRun[] }>('/admin/celpip/bank/runs/list'),
      ])
      setSpec(s)
      setItems(bank.questions)
      setCoverage(bank.coverage)
      setRuns(runList.runs)
      if (!taskKey && bank.coverage.length) setTaskKey(bank.coverage[0].task_key)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not load the bank.')
    }
  }, [api, filter, taskKey])

  useEffect(() => {
    void load()
  }, [load])

  // Generation runs on a background job, so the page polls while anything is
  // still queued or running.
  useEffect(() => {
    if (!runs.some(r => r.status === 'queued' || r.status === 'running')) return
    const id = window.setTimeout(() => void load(), 6000)
    return () => window.clearTimeout(id)
  }, [runs, load])

  const generate = useCallback(async () => {
    setBusy(true)
    setError('')
    try {
      await api.postJson('/admin/celpip/bank/generate', {
        task_key: taskKey, count, difficulty, topic_hint: topic,
      })
      setTopic('')
      await load()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not start generation.')
    } finally {
      setBusy(false)
    }
  }, [api, taskKey, count, difficulty, topic, load])

  const act = useCallback(
    async (id: string, action: string) => {
      try {
        await api.postJson(`/admin/celpip/bank/${id}/action`, { action })
        await load()
      } catch (err) {
        setError(err instanceof Error ? err.message : 'That did not work.')
      }
    },
    [api, load],
  )

  if (!spec) {
    return (
      <div className="flex items-center justify-center gap-2 py-24 text-sm text-neutral-400">
        {error ? <ErrorNote message={error} /> : (<><Loader2 size={16} className="animate-spin" /> Loading…</>)}
      </div>
    )
  }

  if (detail) {
    return (
      <div className="space-y-4">
        <button type="button" className={BUTTON_QUIET} onClick={() => setDetail(null)}>
          Back to bank
        </button>
        <h2 className="text-lg font-bold text-neutral-900 dark:text-neutral-50">
          {detail.label} — {detail.title}
        </h2>
        <div className={CARD}>
          <p className="mb-2 text-[11px] font-bold uppercase tracking-wide text-neutral-400">Validation</p>
          <pre className="max-h-80 overflow-auto whitespace-pre-wrap break-words text-[12px] text-neutral-600 dark:text-neutral-300">
            {JSON.stringify(detail.validation, null, 2)}
          </pre>
        </div>
        <div className={CARD}>
          <p className="mb-2 text-[11px] font-bold uppercase tracking-wide text-neutral-400">Item payload</p>
          <pre className="max-h-[32rem] overflow-auto whitespace-pre-wrap break-words text-[12px] text-neutral-600 dark:text-neutral-300">
            {JSON.stringify(detail.payload, null, 2)}
          </pre>
        </div>
      </div>
    )
  }

  return (
    <div className="space-y-6">
      {error && <ErrorNote message={error} />}

      <div className={CARD}>
        <SectionHeading
          title="Generate items"
          hint="Each candidate is schema-checked, then answered blind by a second model. Anything ambiguous is discarded."
        />
        <div className="grid gap-3 sm:grid-cols-4">
          <div className="sm:col-span-2">
            <label className={LABEL} htmlFor="bank-task">Task type</label>
            <select id="bank-task" className={INPUT} value={taskKey} onChange={e => setTaskKey(e.target.value)}>
              {coverage.map(c => (
                <option key={c.task_key} value={c.task_key}>
                  {c.skill} · {c.label} ({c.ready} ready)
                </option>
              ))}
            </select>
          </div>
          <div>
            <label className={LABEL} htmlFor="bank-count">How many</label>
            <input id="bank-count" type="number" min={1} max={10} className={INPUT} value={count} onChange={e => setCount(Number(e.target.value))} />
          </div>
          <div>
            <label className={LABEL} htmlFor="bank-diff">Target level</label>
            <input id="bank-diff" type="number" min={1} max={12} className={INPUT} value={difficulty} onChange={e => setDifficulty(Number(e.target.value))} />
          </div>
          <div className="sm:col-span-4">
            <label className={LABEL} htmlFor="bank-topic">Topic (optional)</label>
            <input id="bank-topic" className={INPUT} value={topic} onChange={e => setTopic(e.target.value)} placeholder="e.g. a delayed furniture delivery" />
          </div>
        </div>
        <button type="button" disabled={busy || !taskKey} className={`${BUTTON} mt-3`} onClick={() => void generate()}>
          <Sparkles size={14} /> {busy ? 'Queuing…' : 'Generate'}
        </button>
      </div>

      <div>
        <SectionHeading title="Coverage" hint="Servable items per official task type." />
        <div className="grid gap-2 sm:grid-cols-3 lg:grid-cols-5">
          {coverage.map(c => (
            <button
              key={c.task_key}
              type="button"
              onClick={() => setFilter(filter === c.task_key ? '' : c.task_key)}
              className={`rounded-lg border p-2 text-left ${
                filter === c.task_key ? 'border-neutral-900 dark:border-white' : 'border-neutral-200 dark:border-neutral-800'
              }`}
            >
              <p className="truncate text-[11px] text-neutral-500">{c.label}</p>
              <p className={`text-lg font-bold tabular-nums ${c.ready === 0 ? 'text-rose-500' : 'text-neutral-900 dark:text-neutral-50'}`}>
                {c.ready}
              </p>
            </button>
          ))}
        </div>
      </div>

      {runs.length > 0 && (
        <div>
          <SectionHeading
            title="Generation runs"
            hint="Rejections are the useful half — a task type that keeps failing is a prompt problem."
            action={
              <button type="button" className={BUTTON_QUIET} onClick={() => void load()}>
                <RefreshCw size={14} /> Refresh
              </button>
            }
          />
          <ul className="space-y-2">
            {runs.slice(0, 8).map(run => (
              <li key={run.id} className={CARD}>
                <div className="flex items-center justify-between gap-3">
                  <p className="text-sm font-semibold text-neutral-900 dark:text-neutral-50">{run.label}</p>
                  <span className="text-[12px] text-neutral-500">
                    {run.status === 'running' || run.status === 'queued' ? (
                      <span className="flex items-center gap-1"><Loader2 size={11} className="animate-spin" /> {run.status}</span>
                    ) : (
                      `${run.accepted} kept · ${run.rejected} rejected`
                    )}
                  </span>
                </div>
                {run.rejections.length > 0 && (
                  <ul className="mt-1.5 space-y-0.5 text-[12px] text-neutral-500">
                    {run.rejections.slice(0, 4).map((r, i) => (
                      <li key={i}>
                        <span className="font-semibold">{r.reason}</span> — {r.detail}
                      </li>
                    ))}
                  </ul>
                )}
                {run.error && <p className="mt-1 text-[12px] text-rose-600">{run.error}</p>}
              </li>
            ))}
          </ul>
        </div>
      )}

      <div>
        <SectionHeading title={filter ? `Items — ${filter}` : 'Items'} hint={`${items.length} shown`} />
        <ul className="divide-y divide-neutral-200 overflow-hidden rounded-xl border border-neutral-200 dark:divide-neutral-800 dark:border-neutral-800">
          {items.map(item => (
            <li key={item.id} className="flex items-center justify-between gap-3 bg-white p-3 dark:bg-neutral-900">
              <button
                type="button"
                onClick={() => void api.getJson<BankItem>(`/admin/celpip/bank/${item.id}`).then(setDetail).catch(() => undefined)}
                className="min-w-0 flex-1 text-left"
              >
                <p className="truncate text-sm font-semibold text-neutral-900 dark:text-neutral-50">
                  {item.title || item.topic || item.label}
                </p>
                <p className="text-[12px] text-neutral-500">
                  {item.label} · level {item.difficulty} · served {item.times_served}× · {formatDate(item.created_at)}
                </p>
              </button>
              <span className={`flex-shrink-0 rounded-full px-2 py-0.5 text-[11px] font-semibold ${STATUS_TONE[item.status] ?? ''}`}>
                {item.status}
              </span>
              <div className="flex flex-shrink-0 gap-1">
                {item.status === 'awaiting_assets' && (
                  <button
                    type="button"
                    title="Rebuild audio or image"
                    onClick={() => void api.postJson(`/admin/celpip/bank/${item.id}/rebuild-assets`).then(load)}
                    className="rounded-lg border border-neutral-200 p-1.5 text-neutral-400 dark:border-neutral-700"
                  >
                    <Volume2 size={13} />
                  </button>
                )}
                <button
                  type="button"
                  title={item.approved_at ? 'Already approved' : 'Approve (a quality signal, not a gate)'}
                  onClick={() => void act(item.id, 'approve')}
                  className={`rounded-lg border p-1.5 ${item.approved_at ? 'border-emerald-300 text-emerald-600' : 'border-neutral-200 text-neutral-400 dark:border-neutral-700'}`}
                >
                  <Check size={13} />
                </button>
                <button
                  type="button"
                  title={item.status === 'disabled' ? 'Re-enable' : 'Disable'}
                  onClick={() => void act(item.id, item.status === 'disabled' ? 'enable' : 'disable')}
                  className="rounded-lg border border-neutral-200 p-1.5 text-neutral-400 dark:border-neutral-700"
                >
                  <Ban size={13} />
                </button>
              </div>
            </li>
          ))}
        </ul>
      </div>
    </div>
  )
}
