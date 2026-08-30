'use client'

import { ArrowLeft, Clock, Loader2 } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import { MarkdownResult } from '../../components/MarkdownResult'
import type { Lesson, Spec } from '../types'
import type { useCelpip } from '../hooks/useCelpip'
import { BUTTON_QUIET, CARD, ErrorNote, SectionHeading } from './ui'

type Api = ReturnType<typeof useCelpip>

const CATEGORY_ORDER = ['overview', 'format', 'scoring', 'strategy', 'vocabulary']
const CATEGORY_LABEL: Record<string, string> = {
  overview: 'Start here',
  format: 'The format',
  scoring: 'How scoring works',
  strategy: 'Task strategies',
  vocabulary: 'Vocabulary',
}

export function LearnView({ api }: { api: Api }) {
  const [lessons, setLessons] = useState<Lesson[] | null>(null)
  const [spec, setSpec] = useState<Spec | null>(null)
  const [open, setOpen] = useState<Lesson | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    Promise.all([
      api.getJson<{ lessons: Lesson[] }>('/admin/celpip/lessons'),
      api.getJson<Spec>('/admin/celpip/spec'),
    ])
      .then(([l, s]) => {
        setLessons(l.lessons)
        setSpec(s)
      })
      .catch(err => setError(err instanceof Error ? err.message : 'Could not load the library.'))
  }, [api])

  const openLesson = useCallback(
    async (slug: string) => {
      try {
        setOpen(await api.getJson<Lesson>(`/admin/celpip/lessons/${slug}`))
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Could not load that lesson.')
      }
    },
    [api],
  )

  if (error) return <ErrorNote message={error} />
  if (!lessons || !spec) {
    return (
      <div className="flex items-center justify-center gap-2 py-24 text-sm text-neutral-400">
        <Loader2 size={16} className="animate-spin" /> Loading…
      </div>
    )
  }

  if (open) {
    return (
      <article>
        <button type="button" className={`${BUTTON_QUIET} mb-4`} onClick={() => setOpen(null)}>
          <ArrowLeft size={14} /> All lessons
        </button>
        <h1 className="text-2xl font-bold text-neutral-900 dark:text-neutral-50">{open.title}</h1>
        <p className="mt-1 flex items-center gap-1.5 text-[13px] text-neutral-500">
          <Clock size={12} /> {open.estimated_minutes} min read
        </p>
        <div className="mt-5">
          <MarkdownResult content={open.body_markdown ?? ''} />
        </div>
      </article>
    )
  }

  const byCategory = new Map<string, Lesson[]>()
  lessons.forEach(lesson => {
    const list = byCategory.get(lesson.category) ?? []
    list.push(lesson)
    byCategory.set(lesson.category, list)
  })

  return (
    <div className="space-y-8">
      <div>
        <SectionHeading
          title="The test at a glance"
          hint="These timings are the ones the session runner actually enforces."
        />
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          {spec.sections.map(section => (
            <div key={section.skill} className={CARD}>
              <p className="text-sm font-bold text-neutral-900 dark:text-neutral-50">{section.label}</p>
              <p className="mt-0.5 text-[13px] text-neutral-500">
                ~{Math.round(section.limit_seconds / 60)} min
                {section.scored_questions ? ` · ${section.scored_questions} questions` : ` · ${section.tasks.length} tasks`}
              </p>
              <ul className="mt-2 space-y-0.5 text-[12px] text-neutral-500">
                {section.tasks.map(task => (
                  <li key={task.key}>
                    {task.part}. {task.label}
                    {task.question_count ? ` (${task.question_count})` : ''}
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </div>
      </div>

      {CATEGORY_ORDER.filter(c => byCategory.has(c)).map(category => (
        <div key={category}>
          <SectionHeading title={CATEGORY_LABEL[category] ?? category} />
          <div className="grid gap-3 sm:grid-cols-2">
            {(byCategory.get(category) ?? []).map(lesson => (
              <button
                key={lesson.id}
                type="button"
                onClick={() => void openLesson(lesson.slug)}
                className={`${CARD} text-left transition-colors hover:border-neutral-400 dark:hover:border-neutral-600`}
              >
                <p className="text-sm font-bold text-neutral-900 dark:text-neutral-50">{lesson.title}</p>
                <p className="mt-1 text-[13px] leading-relaxed text-neutral-500">{lesson.summary}</p>
                <p className="mt-2 text-[11px] text-neutral-400">{lesson.estimated_minutes} min</p>
              </button>
            ))}
          </div>
        </div>
      ))}
    </div>
  )
}
