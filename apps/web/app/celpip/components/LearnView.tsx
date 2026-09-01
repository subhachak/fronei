'use client'

import { ArrowLeft, BookOpenText, Clock, Headphones, Loader2, Mic2, PenLine } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import { MarkdownResult } from '../../components/MarkdownResult'
import type { Lesson, Spec } from '../types'
import type { useCelpip } from '../hooks/useCelpip'
import { BUTTON_QUIET, CARD, ErrorNote, SectionHeading, SKILL_TONE } from './ui'

type Api = ReturnType<typeof useCelpip>

const CATEGORY_ORDER = ['overview', 'format', 'scoring', 'strategy', 'vocabulary']
const CATEGORY_LABEL: Record<string, string> = {
  overview: 'Start here',
  format: 'The format',
  scoring: 'How scoring works',
  strategy: 'Task strategies',
  vocabulary: 'Vocabulary',
}

const SKILL_ICON = {
  listening: Headphones,
  reading: BookOpenText,
  writing: PenLine,
  speaking: Mic2,
}

export function LearnView({ api, initialSlug, onConsumed }: {
  api: Api
  initialSlug?: string | null
  onConsumed?: () => void
}) {
  const [lessons, setLessons] = useState<Lesson[] | null>(null)
  const [spec, setSpec] = useState<Spec | null>(null)
  const [open, setOpen] = useState<Lesson | null>(null)
  const [error, setError] = useState('')

  // A tip on the Progress dashboard links straight to the lesson that
  // addresses it; without this the learner lands on the index and has to hunt.
  useEffect(() => {
    if (!initialSlug) return
    void openLesson(initialSlug)
    onConsumed?.()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialSlug])

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
    const section = spec.sections.find(item => item.skill === open.skill)
    const task = section?.tasks.find(item => item.key === open.task_key)
    return (
      <article className="mx-auto max-w-4xl">
        <button type="button" className={`${BUTTON_QUIET} mb-4`} onClick={() => setOpen(null)}>
          <ArrowLeft size={14} /> All lessons
        </button>
        <p className="text-xs font-bold uppercase tracking-[0.14em] text-amber-600 dark:text-amber-400">
          {open.skill ? `${open.skill} playbook` : CATEGORY_LABEL[open.category] ?? open.category}
        </p>
        <h1 className="mt-1 text-3xl font-bold tracking-tight text-neutral-900 dark:text-neutral-50">{open.title}</h1>
        <p className="mt-1 flex items-center gap-1.5 text-[13px] text-neutral-500">
          <Clock size={12} /> {open.estimated_minutes} min read
        </p>
        {task && section && (
          <div className={`mt-5 rounded-2xl border p-4 ${SKILL_TONE[section.skill] ?? ''}`}>
            <p className="text-xs font-bold uppercase tracking-wide opacity-70">Official task mechanics</p>
            <div className="mt-2 flex flex-wrap gap-2">
              <span className="rounded-full bg-white/70 px-3 py-1 text-xs font-bold dark:bg-neutral-950/40">Part {task.part}</span>
              {task.question_count > 0 && <span className="rounded-full bg-white/70 px-3 py-1 text-xs font-bold dark:bg-neutral-950/40">{task.question_count} questions</span>}
              {task.prep_seconds > 0 && <span className="rounded-full bg-white/70 px-3 py-1 text-xs font-bold dark:bg-neutral-950/40">{task.prep_seconds}s preparation</span>}
              {task.response_seconds > 0 && <span className="rounded-full bg-white/70 px-3 py-1 text-xs font-bold dark:bg-neutral-950/40">{Math.round(task.response_seconds / 60)} min response</span>}
              {task.word_range && <span className="rounded-full bg-white/70 px-3 py-1 text-xs font-bold dark:bg-neutral-950/40">{task.word_range[0]}–{task.word_range[1]} words</span>}
            </div>
            <p className="mt-3 text-sm leading-relaxed opacity-80">{task.description}</p>
          </div>
        )}
        {open.task_key && (
          <div className="mt-4 grid gap-2 sm:grid-cols-4">
            {['Understand the task', 'Follow the steps', 'Avoid the traps', 'Run the drill'].map((label, index) => (
              <div key={label} className="rounded-xl border border-neutral-200 bg-neutral-50 px-3 py-2 dark:border-neutral-800 dark:bg-neutral-900">
                <p className="text-[10px] font-bold uppercase text-neutral-400">Step {index + 1}</p>
                <p className="mt-0.5 text-xs font-semibold text-neutral-700 dark:text-neutral-200">{label}</p>
              </div>
            ))}
          </div>
        )}
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
      <div className="rounded-3xl bg-gradient-to-br from-amber-50 to-white p-6 dark:from-amber-950/30 dark:to-neutral-950 sm:p-8">
        <p className="text-xs font-bold uppercase tracking-[0.15em] text-amber-700 dark:text-amber-300">Learn the test</p>
        <h2 className="mt-1 text-3xl font-bold tracking-tight text-neutral-950 dark:text-white">Know the format. Practise the strategy.</h2>
        <p className="mt-2 max-w-2xl text-sm leading-relaxed text-neutral-600 dark:text-neutral-300">Start with the overview, then use task lessons immediately before a focused practice session.</p>
        {lessons[0] && <button type="button" onClick={() => void openLesson(lessons[0].slug)} className="mt-5 inline-flex min-h-11 items-center rounded-xl bg-neutral-950 px-4 py-2.5 text-sm font-bold text-white dark:bg-white dark:text-neutral-950">Start with {lessons[0].title}</button>}
      </div>
      <div>
        <SectionHeading
          title="The test at a glance"
          hint="These timings are the ones the session runner actually enforces."
        />
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          {spec.sections.map(section => (
            <div key={section.skill} className={`${CARD} ${SKILL_TONE[section.skill] ?? ''}`}>
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
          <SectionHeading
            title={CATEGORY_LABEL[category] ?? category}
            hint={category === 'strategy' ? 'Organized by component and official task order.' : undefined}
          />
          {category === 'strategy' ? (
            <div className="space-y-6">
              {spec.sections.map(section => {
                const sectionLessons = (byCategory.get(category) ?? [])
                  .filter(lesson => lesson.skill === section.skill)
                  .sort((a, b) => {
                    const taskOrder = new Map(section.tasks.map((task, index) => [task.key, index]))
                    const aOrder = a.task_key ? (taskOrder.get(a.task_key) ?? 999) + 1 : 0
                    const bOrder = b.task_key ? (taskOrder.get(b.task_key) ?? 999) + 1 : 0
                    return aOrder - bOrder || a.sort_order - b.sort_order
                  })
                if (sectionLessons.length === 0) return null
                const Icon = SKILL_ICON[section.skill]
                return (
                  <section key={section.skill} className={`overflow-hidden rounded-2xl border ${SKILL_TONE[section.skill] ?? 'border-neutral-200'}`}>
                    <div className="flex items-center justify-between gap-3 border-b border-current/10 px-4 py-3 sm:px-5">
                      <div className="flex items-center gap-2.5">
                        <span className="grid h-9 w-9 place-items-center rounded-xl bg-white/70 dark:bg-neutral-950/30"><Icon size={17} /></span>
                        <div>
                          <h3 className="text-base font-bold">{section.label}</h3>
                          <p className="text-xs opacity-70">Foundation plus detailed playbooks for Parts 1–{section.tasks.length}</p>
                        </div>
                      </div>
                      <span className="hidden rounded-full bg-white/60 px-2.5 py-1 text-xs font-bold dark:bg-neutral-950/30 sm:block">{sectionLessons.length} guides</span>
                    </div>
                    <div className="divide-y divide-current/10 bg-white/75 dark:bg-neutral-950/35">
                      {sectionLessons.map((lesson, index) => (
                        <button key={lesson.id} type="button" onClick={() => void openLesson(lesson.slug)} className="group flex w-full items-start gap-3 px-4 py-3.5 text-left transition-colors hover:bg-white dark:hover:bg-neutral-900 sm:px-5">
                          <span className="grid h-7 w-7 flex-shrink-0 place-items-center rounded-full border border-current/20 text-xs font-bold">{index + 1}</span>
                          <span className="min-w-0 flex-1">
                            <span className="block text-sm font-bold text-neutral-950 group-hover:text-amber-700 dark:text-white dark:group-hover:text-amber-300">{lesson.title}</span>
                            <span className="mt-0.5 block text-sm leading-relaxed text-neutral-500">{lesson.summary}</span>
                            {lesson.task_key && <span className="mt-1.5 flex flex-wrap gap-1.5"><span className="rounded-full bg-neutral-100 px-2 py-0.5 text-[10px] font-semibold text-neutral-500 dark:bg-neutral-800">Execution plan</span><span className="rounded-full bg-neutral-100 px-2 py-0.5 text-[10px] font-semibold text-neutral-500 dark:bg-neutral-800">Decision rules</span><span className="rounded-full bg-neutral-100 px-2 py-0.5 text-[10px] font-semibold text-neutral-500 dark:bg-neutral-800">Traps + drill</span></span>}
                          </span>
                          <span className="flex-shrink-0 pt-1 text-xs font-medium text-neutral-400">{lesson.estimated_minutes} min</span>
                        </button>
                      ))}
                    </div>
                  </section>
                )
              })}
            </div>
          ) : (
            <div className="grid gap-3 sm:grid-cols-2">
              {(byCategory.get(category) ?? []).map(lesson => (
                <button key={lesson.id} type="button" onClick={() => void openLesson(lesson.slug)} className={`${CARD} text-left transition-colors hover:border-neutral-400 dark:hover:border-neutral-600`}>
                  <p className="text-sm font-bold text-neutral-900 dark:text-neutral-50">{lesson.title}</p>
                  <p className="mt-1 text-[13px] leading-relaxed text-neutral-500">{lesson.summary}</p>
                  <p className="mt-2 text-[11px] text-neutral-400">{lesson.estimated_minutes} min</p>
                </button>
              ))}
            </div>
          )}
        </div>
      ))}
    </div>
  )
}
