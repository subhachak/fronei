'use client'

import {
  ArrowLeft, Check, ChevronDown, ChevronRight, Loader2, RefreshCw, Sparkles, X,
} from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import type { Evaluation, ReceptiveItemReview, ResultsPayload } from '../types'
import type { useCelpip } from '../hooks/useCelpip'
import {
  ApproximateNote, BUTTON, BUTTON_QUIET, CARD, EmptyState, ErrorNote, LevelBadge, SKILL_TONE,
  SectionHeading, formatDate,
} from './ui'

type Api = ReturnType<typeof useCelpip>
type AttemptRow = {
  attempt_id: string
  label: string
  mode: string
  practice_mode: string
  status: string
  created_at: string
  completed_at: string | null
  levels: Record<string, { low?: number; high?: number }>
}

const DIMENSION_LABEL: Record<string, string> = {
  content_coherence: 'Content & Coherence',
  vocabulary: 'Vocabulary',
  readability: 'Readability',
  listenability: 'Listenability',
  task_fulfillment: 'Task Fulfilment',
}

export function ResultsView({
  api, initialAttemptId, onClear, onStart,
}: {
  api: Api
  initialAttemptId: string | null
  onClear: () => void
  onStart: (attemptId: string) => void
}) {
  const [rows, setRows] = useState<AttemptRow[] | null>(null)
  const [open, setOpen] = useState<string | null>(initialAttemptId)
  const [results, setResults] = useState<ResultsPayload | null>(null)
  const [error, setError] = useState('')

  const loadList = useCallback(async () => {
    try {
      const data = await api.getJson<{ attempts: AttemptRow[] }>('/admin/celpip/attempts')
      setRows(data.attempts)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not load your attempts.')
    }
  }, [api])

  const loadResults = useCallback(
    async (attemptId: string) => {
      try {
        setResults(await api.getJson<ResultsPayload>(`/admin/celpip/attempts/${attemptId}/results`))
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Could not load these results.')
      }
    },
    [api],
  )

  useEffect(() => {
    void loadList()
  }, [loadList])

  useEffect(() => {
    if (open) void loadResults(open)
    else setResults(null)
  }, [open, loadResults])

  // Scoring is a background job, so an attempt opened straight after submission
  // arrives mid-evaluation. Poll until it settles rather than showing an empty
  // results page.
  useEffect(() => {
    if (!results || !open) return
    const inFlight =
      results.status === 'submitted' ||
      results.status === 'evaluating' ||
      // Keep polling across a failed run that the worker will retry, otherwise
      // a successful retry never reaches the screen without a manual reload.
      Boolean(results.evaluation_pending)
    if (!inFlight) return
    const id = window.setTimeout(() => void loadResults(open), 5000)
    return () => window.clearTimeout(id)
  }, [results, open, loadResults])

  if (open && results) {
    return (
      <AttemptResults
        api={api}
        results={results}
        onBack={() => { setOpen(null); onClear() }}
        onRefresh={() => void loadResults(open)}
        onStart={onStart}
      />
    )
  }

  if (!rows) {
    return (
      <div className="flex items-center justify-center gap-2 py-24 text-sm text-neutral-400">
        {error ? <ErrorNote message={error} /> : (<><Loader2 size={16} className="animate-spin" /> Loading…</>)}
      </div>
    )
  }

  return (
    <div className="space-y-4">
      {error && <ErrorNote message={error} />}
      <SectionHeading title="Attempts" hint="Every sitting, with its component estimates." />
      {rows.length === 0 ? (
        <EmptyState title="No attempts yet" hint="Results appear here once you have submitted something." />
      ) : (
        <ul className="divide-y divide-neutral-200 overflow-hidden rounded-xl border border-neutral-200 dark:divide-neutral-800 dark:border-neutral-800">
          {rows.map(row => (
            <li key={row.attempt_id} className="bg-white p-3 dark:bg-neutral-900">
              <button type="button" onClick={() => setOpen(row.attempt_id)} className="flex w-full items-center justify-between gap-3 text-left">
                <div className="min-w-0">
                  <p className="truncate text-sm font-semibold text-neutral-900 dark:text-neutral-50">
                    {row.label || row.mode}
                  </p>
                  <p className="text-[12px] text-neutral-500">
                    {formatDate(row.created_at)} · {row.practice_mode} · {row.status.replace('_', ' ')}
                  </p>
                </div>
                <div className="flex flex-shrink-0 items-center gap-2">
                  {Object.entries(row.levels).map(([skill, level]) => (
                    <span key={skill} className="text-center">
                      <LevelBadge low={level.low ?? null} high={level.high ?? null} size="sm" />
                      <span className="mt-0.5 block text-[9px] uppercase text-neutral-400">{skill.slice(0, 4)}</span>
                    </span>
                  ))}
                  <ChevronRight size={16} className="text-neutral-400" />
                </div>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

function AttemptResults({
  api, results, onBack, onRefresh, onStart,
}: {
  api: Api
  results: ResultsPayload
  onBack: () => void
  onRefresh: () => void
  onStart: (attemptId: string) => void
}) {
  const [retaking, setRetaking] = useState(false)
  const [retakeProgress, setRetakeProgress] = useState('')
  const [retakeError, setRetakeError] = useState('')
  const scoring =
    results.status === 'submitted' ||
    results.status === 'evaluating' ||
    Boolean(results.evaluation_pending)
  const retrying =
    Boolean(results.evaluation_job?.active) && (results.evaluation_job?.attempt_count ?? 0) > 1
  const measured = Object.entries(results.components).filter(([, component]) => component.level.low > 0)
  const lowest = measured.sort((a, b) => a[1].level.low - b[1].level.low)[0]

  const retake = async () => {
    if (!results.retake_available) return
    setRetaking(true)
    setRetakeError('')
    try {
      setRetakeProgress('Resetting the original assessment…')
      const test = await api.postJson<{ attempt_id: string }>(
        `/admin/celpip/attempts/${results.attempt_id}/retake`,
      )
      onStart(test.attempt_id)
    } catch (error) {
      setRetakeError(error instanceof Error ? error.message : 'Could not prepare the retake.')
    } finally {
      setRetaking(false)
      setRetakeProgress('')
    }
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between gap-3">
        <button type="button" className={BUTTON_QUIET} onClick={onBack}>
          <ArrowLeft size={14} /> All results
        </button>
        <div className="flex gap-2">
          <button type="button" className={BUTTON_QUIET} onClick={onRefresh}>
            <RefreshCw size={14} /> Refresh
          </button>
          {results.retake_available && !scoring && (
            <button type="button" className={BUTTON} disabled={retaking} onClick={() => void retake()}>
              {retaking ? <Loader2 size={14} className="animate-spin" /> : <RefreshCw size={14} />}
              {retaking ? 'Preparing…' : 'Retake same questions'}
            </button>
          )}
        </div>
      </div>

      {retakeProgress && <div className="flex items-center gap-2 rounded-xl border border-amber-200 bg-amber-50 p-3 text-sm font-medium text-amber-900 dark:border-amber-900 dark:bg-amber-950/40 dark:text-amber-300"><Loader2 size={15} className="animate-spin" /> {retakeProgress}</div>}
      {retakeError && <ErrorNote message={retakeError} />}

      <div className="rounded-3xl bg-neutral-950 p-5 text-white sm:p-7">
        <p className="text-xs font-bold uppercase tracking-[0.15em] text-amber-300">Your result</p>
        <h2 className="mt-1 text-3xl font-bold tracking-tight">{results.label}</h2>
        <p className="mt-1 text-sm text-neutral-300">
          {results.practice_mode} · submitted {formatDate(results.submitted_at)}
        </p>
        {!scoring && lowest && (
          <div className="mt-5 rounded-2xl border border-white/10 bg-white/[0.07] p-4">
            <p className="text-xs font-bold uppercase tracking-wide text-amber-300">Best next move</p>
            <p className="mt-1 text-lg font-bold capitalize">Focus on {lowest[0]}</p>
            <p className="mt-1 text-sm text-neutral-300">This was your lowest estimated component at level {lowest[1].level.low}{lowest[1].level.high !== lowest[1].level.low ? `–${lowest[1].level.high}` : ''}. Review the feedback below, then retry a parallel task.</p>
          </div>
        )}
      </div>

      {scoring && (
        <div className="flex items-center gap-2 rounded-lg border border-sky-200 bg-sky-50 p-3 text-[13px] text-sky-900 dark:border-sky-900 dark:bg-sky-950/40 dark:text-sky-300">
          <Loader2 size={14} className="animate-spin" />
          {retrying ? (
            <>
              A scoring run failed and is being retried (attempt{' '}
              {results.evaluation_job?.attempt_count} of {results.evaluation_job?.max_attempts}).
              This page will update on its own.
            </>
          ) : (
            <>
              Scoring in progress. Writing and Speaking each get two independent evaluations plus a
              reconciliation pass, so this takes a few minutes.
            </>
          )}
        </div>
      )}
      {results.error && <ErrorNote message={results.error} />}

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        {Object.entries(results.components).map(([skill, component]) => (
          <div key={skill} className={`${CARD} ${SKILL_TONE[skill] ?? ''}`}>
            <div className="flex items-center justify-between">
              <p className="text-sm font-bold capitalize text-neutral-900 dark:text-neutral-50">{skill}</p>
              <LevelBadge low={component.level.low} high={component.level.high} />
            </div>
            {component.method === 'deterministic' ? (
              <>
                <p className="mt-1 text-[13px] text-neutral-500">
                  {component.raw_score} of {component.max_score} correct
                </p>
                {(component.late_excluded ?? 0) > 0 && (
                  <p className="mt-0.5 text-[12px] font-medium text-amber-600">
                    {component.late_excluded} answer(s) arrived after the section deadline and did
                    not count.
                  </p>
                )}
              </>
            ) : (
              <p className="mt-1 text-[13px] text-neutral-500">
                Confidence {Math.round((component.confidence ?? 0) * 100)}%
              </p>
            )}
            {component.weakness_tags.length > 0 && (
              <p className="mt-1.5 text-[11px] text-neutral-400">{component.weakness_tags.join(', ')}</p>
            )}
          </div>
        ))}
      </div>
      <ApproximateNote />

      {(results.series_history?.length ?? 0) > 1 && (
        <section className={CARD}>
          <SectionHeading title="Score history" hint="Every sitting of this exact assessment, oldest to newest." />
          <div className="mt-3 space-y-2">
            {results.series_history?.map((attempt, index) => (
              <div key={attempt.attempt_id} className="flex items-center justify-between gap-3 rounded-xl bg-neutral-50 px-3 py-2 dark:bg-neutral-800/60">
                <p className="text-sm font-semibold text-neutral-700 dark:text-neutral-200">Attempt {index + 1} · {formatDate(attempt.created_at)}</p>
                <div className="flex gap-2">
                  {Object.entries(attempt.levels).map(([skill, level]) => (
                    <span key={skill} className="text-center"><LevelBadge low={level.low ?? null} high={level.high ?? null} size="sm" /><span className="block text-[9px] uppercase text-neutral-400">{skill.slice(0, 4)}</span></span>
                  ))}
                </div>
              </div>
            ))}
          </div>
        </section>
      )}

      {Object.entries(results.components)
        .filter(([, c]) => c.method === 'deterministic')
        .map(([skill, component]) => (
          <div key={skill}>
            <SectionHeading
              title={`${skill[0].toUpperCase()}${skill.slice(1)} — question review`}
              hint="Every question, with the evidence for the key and why each distractor fails."
            />
            {component.accuracy_by_task && (
              <div className="mb-3 grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
                {Object.entries(component.accuracy_by_task).map(([task, counts]) => (
                  <div key={task} className="rounded-lg border border-neutral-200 p-2 dark:border-neutral-800">
                    <p className="text-[11px] text-neutral-500">{task}</p>
                    <p className="text-sm font-bold tabular-nums text-neutral-900 dark:text-neutral-50">
                      {counts.correct}/{counts.total}
                    </p>
                  </div>
                ))}
              </div>
            )}
            <div className="space-y-3">
              {(component.items ?? []).map(item => (
                <ReceptiveReview key={item.question_id} item={item} />
              ))}
            </div>
          </div>
        ))}

      {results.evaluations.length > 0 && (
        <div>
          <SectionHeading
            title="Written and spoken responses"
            hint="Each scored twice, independently, then reconciled."
          />
          <div className="space-y-4">
            {results.evaluations.map(evaluation => (
              <EvaluationCard key={evaluation.id} api={api} evaluation={evaluation} />
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

function ReceptiveReview({ item }: { item: ReceptiveItemReview }) {
  const [open, setOpen] = useState(false)
  return (
    <div className="overflow-hidden rounded-xl border border-neutral-200 dark:border-neutral-800">
      <button
        type="button"
        onClick={() => setOpen(o => !o)}
        className="flex w-full items-center justify-between gap-3 bg-neutral-50 px-4 py-2.5 text-left dark:bg-neutral-900"
      >
        <span className="text-sm font-semibold text-neutral-900 dark:text-neutral-50">{item.task_key}</span>
        <span className="flex items-center gap-2">
          <span className="text-sm font-bold tabular-nums text-neutral-600 dark:text-neutral-300">
            {item.correct}/{item.total}
          </span>
          <ChevronDown size={15} className={`text-neutral-400 transition-transform ${open ? 'rotate-180' : ''}`} />
        </span>
      </button>
      {open && (
        <ol className="divide-y divide-neutral-200 dark:divide-neutral-800">
          {item.questions.map(q => (
            <li key={q.index} className="p-4">
              <div className="flex items-start gap-2">
                {q.correct ? (
                  <Check size={15} className="mt-0.5 flex-shrink-0 text-emerald-600" />
                ) : (
                  <X size={15} className="mt-0.5 flex-shrink-0 text-rose-600" />
                )}
                <div className="min-w-0 flex-1">
                  <p className="text-[14px] font-semibold text-neutral-900 dark:text-neutral-50">
                    {q.index + 1}. {q.prompt}
                  </p>
                  <p className="mt-1 text-[13px] text-neutral-500">
                    {q.late ? (
                      <><strong>Answered after time</strong> — the real test would not have taken
                      it, so it did not count. </>
                    ) : q.answered ? (
                      <>You chose <strong>{q.chosen}</strong>. </>
                    ) : (
                      <><strong>Not answered</strong> — a guess is always worth more than a blank. </>
                    )}
                    Correct answer: <strong>{q.answer}</strong>
                  </p>
                  {q.evidence && (
                    <p className="mt-2 border-l-2 border-emerald-400 pl-3 text-[13px] italic leading-relaxed text-neutral-600 dark:text-neutral-300">
                      “{q.evidence}”
                    </p>
                  )}
                  {q.why_correct && (
                    <p className="mt-2 text-[13px] leading-relaxed text-neutral-600 dark:text-neutral-300">
                      <span className="font-semibold">Why {q.answer}: </span>{q.why_correct}
                    </p>
                  )}
                  {!q.correct && q.chosen && q.why_others_wrong[q.chosen] && (
                    <p className="mt-1 text-[13px] leading-relaxed text-rose-700 dark:text-rose-400">
                      <span className="font-semibold">Why {q.chosen} fails: </span>
                      {q.why_others_wrong[q.chosen]}
                    </p>
                  )}
                </div>
              </div>
            </li>
          ))}
        </ol>
      )}
    </div>
  )
}

function EvaluationCard({ api, evaluation }: { api: Api; evaluation: Evaluation }) {
  const [exemplar, setExemplar] = useState(evaluation.exemplar)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const feedback = evaluation.feedback ?? {}
  const metrics = evaluation.delivery_metrics ?? {}

  const buildExemplar = useCallback(async () => {
    setBusy(true)
    setError('')
    try {
      setExemplar(await api.postJson(`/admin/celpip/evaluations/${evaluation.id}/exemplar`))
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not build an improved response.')
    } finally {
      setBusy(false)
    }
  }, [api, evaluation.id])

  if (evaluation.status !== 'complete') {
    return (
      <div className={CARD}>
        <p className="text-sm font-bold text-neutral-900 dark:text-neutral-50">{evaluation.label}</p>
        <p className="mt-1 text-[13px] text-neutral-500">
          {evaluation.status === 'failed' ? `Could not be scored: ${evaluation.error}` : 'Scoring…'}
        </p>
      </div>
    )
  }

  return (
    <div className={CARD}>
      <div className="flex items-start justify-between gap-3">
        <div>
          <p className="text-sm font-bold text-neutral-900 dark:text-neutral-50">{evaluation.label}</p>
          <p className="mt-0.5 text-[12px] text-neutral-500">
            Confidence {Math.round(evaluation.confidence * 100)}%
            {(feedback.disagreements?.length ?? 0) > 0 &&
              ' · the two evaluators disagreed, so this range is wider'}
          </p>
        </div>
        <LevelBadge low={evaluation.level.low} high={evaluation.level.high} size="lg" />
      </div>

      {feedback.summary && (
        <p className="mt-3 text-[14px] leading-relaxed text-neutral-700 dark:text-neutral-200">{feedback.summary}</p>
      )}

      <div className="mt-4 space-y-3">
        {Object.entries(evaluation.dimensions).map(([dim, level]) => (
          <div key={dim} className="grid grid-cols-[minmax(130px,1fr)_3fr_2rem] items-center gap-3">
            <p className="text-xs font-semibold text-neutral-600 dark:text-neutral-300">{DIMENSION_LABEL[dim] ?? dim}</p>
            <div className="h-2 overflow-hidden rounded-full bg-neutral-100 dark:bg-neutral-800"><div className="h-full rounded-full bg-amber-400" style={{ width: `${Math.max(4, Math.min(100, (level / 12) * 100))}%` }} /></div>
            <p className="text-right text-sm font-bold tabular-nums text-neutral-900 dark:text-neutral-50">{level}</p>
          </div>
        ))}
      </div>

      {evaluation.skill === 'speaking' && Object.keys(metrics).length > 0 && (
        <div className="mt-4 rounded-lg border border-neutral-200 p-3 dark:border-neutral-800">
          <p className="mb-2 text-[11px] font-bold uppercase tracking-wide text-neutral-400">
            Delivery — measured from the audio, not judged
          </p>
          <div className="grid grid-cols-2 gap-2 text-[13px] sm:grid-cols-4">
            <Metric label="Pace" value={`${metrics.words_per_minute ?? 0} wpm`} />
            <Metric label="Time used" value={`${Math.round((metrics.time_used_ratio ?? 0) * 100)}%`} />
            <Metric label="Fillers" value={String(metrics.filler_count ?? 0)} />
            <Metric label="Long pauses" value={String(metrics.pause_count ?? 0)} />
          </div>
          {!metrics.has_word_timings && (
            <p className="mt-2 text-[11px] text-neutral-400">
              No word timings were returned for this recording, so pauses could not be measured.
            </p>
          )}
        </div>
      )}

      {(feedback.missing_requirements?.length ?? 0) > 0 && (
        <Block title="Requirements not addressed" tone="rose">
          <ul className="list-disc space-y-1 pl-5">
            {feedback.missing_requirements!.map((r, i) => <li key={i}>{r}</li>)}
          </ul>
        </Block>
      )}

      {(feedback.strengths?.length ?? 0) > 0 && (
        <Block title="What worked">
          <ul className="list-disc space-y-1 pl-5">
            {feedback.strengths!.map((s, i) => <li key={i}>{s}</li>)}
          </ul>
        </Block>
      )}

      {(feedback.corrections?.length ?? 0) > 0 && (
        <Block title="Corrections, most important first">
          <ul className="space-y-2">
            {feedback.corrections!.map((c, i) => (
              <li key={i} className="rounded-lg border border-neutral-200 p-2 dark:border-neutral-800">
                <p className="text-[11px] font-bold uppercase text-neutral-400">{c.severity}</p>
                <p className="text-[13px] text-rose-700 line-through dark:text-rose-400">{c.original}</p>
                <p className="text-[13px] text-emerald-700 dark:text-emerald-400">{c.corrected}</p>
                <p className="mt-0.5 text-[12px] text-neutral-500">{c.why}</p>
              </li>
            ))}
          </ul>
        </Block>
      )}

      {(feedback.patterns?.length ?? 0) > 0 && (
        <Block title="Recurring habits">
          <ul className="list-disc space-y-1 pl-5">
            {feedback.patterns!.map((p, i) => <li key={i}>{p}</li>)}
          </ul>
        </Block>
      )}

      {(feedback.outline?.length ?? 0) > 0 && (
        <Block title="What a stronger answer would have covered">
          <ol className="list-decimal space-y-1 pl-5">
            {feedback.outline!.map((o, i) => <li key={i}>{o}</li>)}
          </ol>
        </Block>
      )}

      <div className="mt-4 border-t border-neutral-200 pt-3 dark:border-neutral-800">
        {exemplar?.exemplar ? (
          <div>
            <p className="text-[11px] font-bold uppercase tracking-wide text-neutral-400">
              Improved version of your own answer{exemplar.target_level ? ` — level ${exemplar.target_level}` : ''}
            </p>
            <p className="mt-2 whitespace-pre-wrap rounded-lg border border-neutral-200 bg-neutral-50 p-3 text-[14px] leading-relaxed text-neutral-800 dark:border-neutral-800 dark:bg-neutral-900 dark:text-neutral-100">
              {exemplar.exemplar}
            </p>
            {(exemplar.changes?.length ?? 0) > 0 && (
              <ul className="mt-2 space-y-1 text-[13px] text-neutral-600 dark:text-neutral-300">
                {exemplar.changes!.map((c, i) => (
                  <li key={i}>
                    <span className="font-semibold">{c.change}</span> — {c.why}
                  </li>
                ))}
              </ul>
            )}
            {exemplar.retry_exercise && (
              <div className="mt-3 rounded-lg border border-sky-200 bg-sky-50 p-3 dark:border-sky-900 dark:bg-sky-950/40">
                <p className="text-[13px] font-bold text-sky-900 dark:text-sky-300">
                  {exemplar.retry_exercise.title} · {exemplar.retry_exercise.time_minutes} min
                </p>
                <p className="mt-1 text-[13px] leading-relaxed text-sky-900 dark:text-sky-300">
                  {exemplar.retry_exercise.instructions}
                </p>
              </div>
            )}
          </div>
        ) : (
          <button type="button" disabled={busy} className={BUTTON_QUIET} onClick={() => void buildExemplar()}>
            {busy ? <Loader2 size={14} className="animate-spin" /> : <Sparkles size={14} />}
            {busy ? 'Writing…' : 'Show a stronger version of my answer'}
          </button>
        )}
        {error && <p className="mt-2 text-[13px] text-rose-600">{error}</p>}
        <p className="mt-2 text-[11px] text-neutral-400">
          Scored by {evaluation.provenance.evaluator_a || '—'} and {evaluation.provenance.evaluator_b || '—'}
          {evaluation.provenance.reconciler ? `, reconciled by ${evaluation.provenance.reconciler}` : ''} ·
          rubric {evaluation.provenance.rubric_version}
        </p>
      </div>
    </div>
  )
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <p className="text-[11px] text-neutral-500">{label}</p>
      <p className="font-bold tabular-nums text-neutral-900 dark:text-neutral-50">{value}</p>
    </div>
  )
}

function Block({ title, tone, children }: { title: string; tone?: 'rose'; children: React.ReactNode }) {
  return (
    <div className="mt-4">
      <p
        className={`mb-1.5 text-[11px] font-bold uppercase tracking-wide ${
          tone === 'rose' ? 'text-rose-500' : 'text-neutral-400'
        }`}
      >
        {title}
      </p>
      <div className="text-[13px] leading-relaxed text-neutral-600 dark:text-neutral-300">{children}</div>
    </div>
  )
}
