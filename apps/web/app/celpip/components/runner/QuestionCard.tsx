'use client'

import { Flag, Lightbulb, Loader2 } from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { PracticeMode, RunnerQuestion } from '../../types'
import type { useCelpip } from '../../hooks/useCelpip'
import { AudioSequence } from './AudioSequence'
import { SpeakingRecorder } from './SpeakingRecorder'
import { StimulusView } from './StimulusView'

type Api = ReturnType<typeof useCelpip>

const OPTION_ROW =
  'flex w-full items-start gap-3 rounded-lg border p-3 text-left text-[14px] transition-colors'

export function QuestionCard({
  api,
  attemptId,
  question,
  practiceMode,
  onSaved,
}: {
  api: Api
  attemptId: string
  question: RunnerQuestion
  practiceMode: PracticeMode
  onSaved: () => void
}) {
  // Answers are seeded from the server's saved responses for THIS question.
  // `useState` initialisers only run on first mount, so a component reused
  // across questions would keep the previous question's answers -- and the
  // writing autosave below would then post the previous task's essay onto this
  // one. SessionRunner keys this component by question_id so React remounts it,
  // and the effect below re-seeds anyway, so neither guard alone is
  // load-bearing.
  const seedAnswers = (q: RunnerQuestion): Record<number, string> => {
    const initial: Record<number, string> = {}
    Object.entries(q.responses).forEach(([index, value]) => {
      if (value.selected_option) initial[Number(index)] = value.selected_option
    })
    return initial
  }

  const [answers, setAnswers] = useState<Record<number, string>>(() => seedAnswers(question))
  const [text, setText] = useState(question.responses['0']?.response_text ?? '')
  const [flagged, setFlagged] = useState(question.responses['0']?.flagged ?? false)
  const [locked, setLocked] = useState<Record<number, boolean>>({})
  const [error, setError] = useState('')
  const [saving, setSaving] = useState(false)
  const [imageUrl, setImageUrl] = useState<string | null>(null)
  const [audioDone, setAudioDone] = useState(false)
  const openedAt = useRef(Date.now())
  // The question this component's editable state currently belongs to. The
  // writing autosave refuses to fire until it matches the question in props,
  // so a render with new props but not-yet-reset state can never post one
  // question's answer against another's id.
  const stateOwner = useRef(question.question_id)

  useEffect(() => {
    if (stateOwner.current === question.question_id) return
    stateOwner.current = question.question_id
    setAnswers(seedAnswers(question))
    setText(question.responses['0']?.response_text ?? '')
    setFlagged(question.responses['0']?.flagged ?? false)
    setLocked({})
    setError('')
    setSaving(false)
    openedAt.current = Date.now()
    setAudioDone(false)
  }, [question])

  // Images are fetched through the authorised media endpoint rather than being
  // a plain <img src>, because the endpoint requires a bearer token.
  useEffect(() => {
    const asset = question.assets.image
    if (!asset) {
      setImageUrl(null)
      return
    }
    let revoked: string | null = null
    let cancelled = false
    api
      .authorizedFetch(`/admin/celpip/media/${asset.id}`)
      .then(r => (r.ok ? r.blob() : Promise.reject(new Error('image'))))
      .then(blob => {
        if (cancelled) return
        revoked = URL.createObjectURL(blob)
        setImageUrl(revoked)
      })
      .catch(() => setImageUrl(null))
    return () => {
      cancelled = true
      if (revoked) URL.revokeObjectURL(revoked)
    }
  }, [api, question.assets.image])

  const save = useCallback(
    async (payload: Record<string, unknown>) => {
      setSaving(true)
      setError('')
      try {
        await api.postJson('/admin/celpip/attempts/' + attemptId + '/responses', {
          question_id: question.question_id,
          time_spent_ms: Date.now() - openedAt.current,
          ...payload,
        })
        onSaved()
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Could not save your answer.')
      } finally {
        setSaving(false)
      }
    },
    [api, attemptId, question.question_id, onSaved],
  )

  const choose = useCallback(
    (index: number, option: string) => {
      if (locked[index]) return
      setAnswers(prev => ({ ...prev, [index]: option }))
      if (practiceMode === 'simulation' && !question.allows_answer_change) {
        setLocked(prev => ({ ...prev, [index]: true }))
      }
      void save({ question_index: index, selected_option: option })
    },
    [locked, practiceMode, question.allows_answer_change, save],
  )

  // Autosave written responses on a debounce, so a crashed tab costs seconds
  // rather than a 200-word answer.
  useEffect(() => {
    if (question.skill !== 'writing') return
    // Never autosave while the editor still holds another question's text.
    if (stateOwner.current !== question.question_id) return
    if (text === (question.responses['0']?.response_text ?? '')) return
    const id = window.setTimeout(() => void save({ question_index: 0, response_text: text }), 1500)
    return () => window.clearTimeout(id)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [text, question.skill, question.question_id])

  const words = useMemo(() => text.trim().split(/\s+/).filter(Boolean).length, [text])
  const [low, high] = question.word_range ?? [0, 0]
  const segmented = question.task_key === 'listening_problem_solving'
  const segments = useMemo(() => {
    if (!segmented) return [{ index: 0, questions: question.questions }]
    const groups = new Map<number, RunnerQuestion['questions']>()
    question.questions.forEach(q => {
      const list = groups.get(q.segment_index) ?? []
      list.push(q)
      groups.set(q.segment_index, list)
    })
    return [...groups.entries()]
      .sort((a, b) => a[0] - b[0])
      .map(([index, questions]) => ({ index, questions }))
  }, [question.questions, segmented])

  const uploadAudio = useCallback(
    async (blob: Blob, duration: number) => {
      const form = new FormData()
      form.append('question_id', question.question_id)
      form.append('duration_seconds', String(duration))
      form.append('file', blob, 'response.webm')
      const response = await api.authorizedFetch(`/admin/celpip/attempts/${attemptId}/responses/audio`, {
        method: 'POST',
        body: form,
      })
      if (!response.ok) {
        throw new Error(await api.readErrorBody(response, 'Could not save your recording.'))
      }
      onSaved()
    },
    [api, attemptId, question.question_id, onSaved],
  )

  return (
    <div className="space-y-5">
      <div className="flex items-start justify-between gap-3">
        <div>
          <p className="text-[11px] font-bold uppercase tracking-wide text-neutral-400">
            Part {question.part} · {question.label}
          </p>
          <p className="mt-0.5 text-[13px] leading-relaxed text-neutral-500">{question.description}</p>
        </div>
        <button
          type="button"
          onClick={() => {
            const next = !flagged
            setFlagged(next)
            void save({ question_index: 0, flagged: next })
          }}
          title="Flag for review"
          className={`flex-shrink-0 rounded-lg border p-2 ${
            flagged
              ? 'border-amber-300 bg-amber-50 text-amber-700 dark:border-amber-800 dark:bg-amber-950/50'
              : 'border-neutral-200 text-neutral-400 dark:border-neutral-700'
          }`}
        >
          <Flag size={14} />
        </button>
      </div>

      {practiceMode === 'learn' && (
        <div className="flex items-start gap-2 rounded-lg border border-sky-200 bg-sky-50 p-3 text-[13px] text-sky-900 dark:border-sky-900 dark:bg-sky-950/40 dark:text-sky-300">
          <Lightbulb size={14} className="mt-0.5 flex-shrink-0" />
          <span>
            Learn mode: untimed, and you can change any answer. Feedback comes after you submit.
          </span>
        </div>
      )}

      {question.skill === 'listening' && (
        <AudioSequence
          api={api}
          assets={question.assets}
          maxPlays={practiceMode === 'simulation' ? 1 : practiceMode === 'timed' ? 2 : 0}
          onFinished={() => setAudioDone(true)}
        />
      )}

      <StimulusView question={question} imageUrl={imageUrl} />

      {question.skill === 'writing' && (
        <div>
          <textarea
            value={text}
            onChange={e => setText(e.target.value)}
            rows={16}
            spellCheck={practiceMode !== 'simulation'}
            placeholder="Write your response here."
            className="w-full rounded-lg border border-neutral-200 bg-white p-3 font-sans text-[15px] leading-relaxed text-neutral-900 focus:outline-none focus:ring-2 focus:ring-neutral-400 dark:border-neutral-700 dark:bg-neutral-900 dark:text-neutral-50"
          />
          <div className="mt-1.5 flex items-center justify-between text-[12px]">
            <span
              className={
                words < low
                  ? 'font-semibold text-amber-600'
                  : words > high
                    ? 'text-neutral-500'
                    : 'font-semibold text-emerald-600'
              }
            >
              {words} words {low ? `· target ${low}–${high}` : ''}
              {words < low && low ? ' · under length is penalised' : ''}
            </span>
            <span className="text-neutral-400">
              {saving ? 'Saving…' : 'Saved automatically'}
              {practiceMode === 'simulation' ? ' · spellcheck off, as in the test' : ''}
            </span>
          </div>
        </div>
      )}

      {question.skill === 'speaking' && (
        <SpeakingRecorder
          prepSeconds={question.prep_seconds}
          responseSeconds={question.response_seconds}
          allowRetake={practiceMode !== 'simulation'}
          alreadyRecorded={Boolean(question.responses['0']?.has_audio)}
          onUpload={uploadAudio}
        />
      )}

      {question.questions.length > 0 && (
        <div className="space-y-6">
          {segments.map(group => (
            <div key={group.index}>
              {segmented && (
                <p className="mb-2 text-[11px] font-bold uppercase tracking-wide text-neutral-400">
                  Segment {group.index + 1}
                </p>
              )}
              <ol className="space-y-5">
                {group.questions.map(q => (
                  <li key={q.index}>
                    <p className="mb-2 text-[14px] font-semibold text-neutral-900 dark:text-neutral-50">
                      {q.index + 1}. {q.prompt}
                    </p>
                    <div className="space-y-1.5">
                      {Object.entries(q.options).map(([key, label]) => {
                        const selected = answers[q.index] === key
                        return (
                          <button
                            key={key}
                            type="button"
                            disabled={locked[q.index] && !selected}
                            onClick={() => choose(q.index, key)}
                            className={`${OPTION_ROW} ${
                              selected
                                ? 'border-neutral-900 bg-neutral-50 dark:border-white dark:bg-neutral-800'
                                : 'border-neutral-200 hover:bg-neutral-50 disabled:opacity-50 dark:border-neutral-700 dark:hover:bg-neutral-800'
                            }`}
                          >
                            <span
                              className={`grid h-5 w-5 flex-shrink-0 place-items-center rounded-full border text-[11px] font-bold ${
                                selected
                                  ? 'border-neutral-900 bg-neutral-900 text-white dark:border-white dark:bg-white dark:text-neutral-900'
                                  : 'border-neutral-300 text-neutral-500 dark:border-neutral-600'
                              }`}
                            >
                              {key}
                            </span>
                            <span className="text-neutral-700 dark:text-neutral-200">{label}</span>
                          </button>
                        )
                      })}
                    </div>
                    {locked[q.index] && (
                      <p className="mt-1.5 text-[11px] text-neutral-400">
                        Locked — this answer cannot be changed in exam simulation.
                      </p>
                    )}
                  </li>
                ))}
              </ol>
            </div>
          ))}
        </div>
      )}

      {error && <p className="text-[13px] text-rose-600">{error}</p>}
      {saving && question.skill !== 'writing' && (
        <p className="flex items-center gap-1.5 text-[12px] text-neutral-400">
          <Loader2 size={12} className="animate-spin" /> Saving
        </p>
      )}
    </div>
  )
}
