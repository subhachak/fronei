'use client'

import type { RunnerQuestion } from '../../types'

/**
 * Renders the non-audio half of an item.
 *
 * Reading Part 2's diagram is rendered here from its structured rows rather
 * than being generated as an image: a real table is sharper, screen-readable,
 * and cannot drift from the data the questions were keyed against.
 */
export function StimulusView({ question, imageUrl }: { question: RunnerQuestion; imageUrl?: string | null }) {
  const s = question.stimulus || {}

  if (question.skill === 'listening') {
    // The script is never sent to the client — the audio is the test.
    return null
  }

  if (question.task_key === 'reading_correspondence') {
    return (
      <div className="space-y-4">
        <Panel title={s.message?.subject || 'Message'} meta={`From ${s.message?.from ?? '—'}`}>
          {s.message?.body}
        </Panel>
        <Panel title="Reply" meta="Complete the missing parts of this reply.">
          {s.reply?.body}
        </Panel>
      </div>
    )
  }

  if (question.task_key === 'reading_diagram') {
    const diagram = s.diagram || {}
    const columns: string[] =
      diagram.columns && diagram.columns.length
        ? diagram.columns
        : Array.from(new Set((diagram.entries || []).flatMap((e: Record<string, unknown>) => Object.keys(e))))
    return (
      <div className="space-y-4">
        <div className="overflow-hidden rounded-xl border border-neutral-200 dark:border-neutral-800">
          <div className="border-b border-neutral-200 bg-neutral-50 px-4 py-2.5 dark:border-neutral-800 dark:bg-neutral-900">
            <p className="text-[11px] font-bold uppercase tracking-wide text-neutral-400">{diagram.kind}</p>
            <p className="text-sm font-bold text-neutral-900 dark:text-neutral-50">{diagram.title}</p>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-left text-[13px]">
              <thead className="bg-neutral-50 text-neutral-500 dark:bg-neutral-900">
                <tr>
                  {columns.map(col => (
                    <th key={col} className="whitespace-nowrap px-3 py-2 font-semibold">{col}</th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-neutral-200 dark:divide-neutral-800">
                {(diagram.entries || []).map((entry: Record<string, unknown>, i: number) => (
                  <tr key={i} className="bg-white dark:bg-neutral-950">
                    {columns.map(col => (
                      <td key={col} className="px-3 py-2 text-neutral-700 dark:text-neutral-200">
                        {String(entry[col] ?? '—')}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {(diagram.footnotes || []).length > 0 && (
            <ul className="border-t border-neutral-200 bg-neutral-50 px-4 py-2 text-[12px] text-neutral-500 dark:border-neutral-800 dark:bg-neutral-900">
              {diagram.footnotes.map((note: string, i: number) => (
                <li key={i}>{note}</li>
              ))}
            </ul>
          )}
        </div>
        <Panel title="Email" meta={`From ${s.email?.from ?? '—'}`}>{s.email?.body}</Panel>
      </div>
    )
  }

  if (question.task_key === 'reading_information') {
    return (
      <div className="space-y-3">
        {(s.paragraphs || []).map((p: { label: string; text: string }) => (
          <div key={p.label} className="rounded-lg border border-neutral-200 p-3 dark:border-neutral-800">
            <p className="mb-1 text-xs font-bold text-neutral-400">Paragraph {p.label}</p>
            <p className="whitespace-pre-wrap text-[14px] leading-relaxed text-neutral-700 dark:text-neutral-200">
              {p.text}
            </p>
          </div>
        ))}
      </div>
    )
  }

  if (question.task_key === 'reading_viewpoints') {
    return (
      <div className="space-y-4">
        <Panel title={s.article?.title || 'Article'}>{s.article?.body}</Panel>
        <Panel title="Reader's comment" meta={s.comment?.author ? `From ${s.comment.author}` : undefined}>
          {s.comment?.body}
        </Panel>
      </div>
    )
  }

  // Writing and Speaking prompts.
  return (
    <div className="space-y-3">
      {imageUrl && (
        <img
          src={imageUrl}
          alt="The scene to describe"
          className="w-full rounded-xl border border-neutral-200 dark:border-neutral-800"
        />
      )}
      <p className="whitespace-pre-wrap text-[15px] leading-relaxed text-neutral-800 dark:text-neutral-100">
        {s.prompt}
      </p>
      {(s.option_a || s.option_b) && (
        <div className="grid gap-2 sm:grid-cols-2">
          {[s.option_a, s.option_b].filter(Boolean).map((option: string, i: number) => (
            <div key={i} className="rounded-lg border border-neutral-200 p-3 text-[14px] text-neutral-700 dark:border-neutral-800 dark:text-neutral-200">
              <span className="mr-1.5 font-bold text-neutral-400">{i === 0 ? 'A' : 'B'}</span>
              {option}
            </div>
          ))}
        </div>
      )}
      {(s.people || []).length > 0 && (
        <div className="grid gap-2 sm:grid-cols-2">
          {s.people.map((person: { name: string; role: string; why_awkward: string }, i: number) => (
            <div key={i} className="rounded-lg border border-neutral-200 p-3 dark:border-neutral-800">
              <p className="text-sm font-semibold text-neutral-900 dark:text-neutral-50">{person.name}</p>
              <p className="text-[12px] text-neutral-500">{person.role}</p>
              <p className="mt-1 text-[13px] text-neutral-600 dark:text-neutral-300">{person.why_awkward}</p>
            </div>
          ))}
        </div>
      )}
      {(s.bullets || []).length > 0 && (
        <div className="rounded-lg border border-neutral-200 bg-neutral-50 p-3 dark:border-neutral-800 dark:bg-neutral-900">
          <p className="mb-1.5 text-[11px] font-bold uppercase tracking-wide text-neutral-400">
            Your response must cover
          </p>
          <ul className="list-disc space-y-1 pl-5 text-[14px] text-neutral-700 dark:text-neutral-200">
            {s.bullets.map((b: string, i: number) => (
              <li key={i}>{b}</li>
            ))}
          </ul>
        </div>
      )}
      {s.recipient && (
        <p className="text-[13px] text-neutral-500">
          <span className="font-semibold">Write to:</span> {s.recipient}
          {s.register ? ` · ${s.register} register` : ''}
        </p>
      )}
    </div>
  )
}

function Panel({ title, meta, children }: { title?: string; meta?: string; children?: React.ReactNode }) {
  return (
    <div className="rounded-xl border border-neutral-200 dark:border-neutral-800">
      {(title || meta) && (
        <div className="border-b border-neutral-200 bg-neutral-50 px-4 py-2.5 dark:border-neutral-800 dark:bg-neutral-900">
          {title && <p className="text-sm font-bold text-neutral-900 dark:text-neutral-50">{title}</p>}
          {meta && <p className="text-[12px] text-neutral-500">{meta}</p>}
        </div>
      )}
      <p className="whitespace-pre-wrap px-4 py-3 text-[14px] leading-relaxed text-neutral-700 dark:text-neutral-200">
        {children}
      </p>
    </div>
  )
}
