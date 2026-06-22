import { BookOpen, Clock3, Library, Send, Sparkles } from 'lucide-react'
import type { FollowUpOption, ResearchPlanPreview } from '../types'

export function ResearchPlanCard({
  preview,
  followUpOptions,
  onFollowUp,
}: {
  preview: ResearchPlanPreview
  followUpOptions: FollowUpOption[]
  onFollowUp?: (option: FollowUpOption) => void
}) {
  const startOption = followUpOptions.find(option => option.confirm_deep_research) || followUpOptions[0]
  const regularOption = followUpOptions.find(option => option.research_level === 'regular')
  const directOption = followUpOptions.find(option => option.force_route === 'direct')

  return (
    <div className="grid gap-4 rounded-xl border border-neutral-200 bg-neutral-50 p-4 dark:border-neutral-800 dark:bg-neutral-900/60 sm:p-5">
      <div className="flex items-start justify-between gap-3">
        <div>
          <p className="text-[11px] font-bold uppercase tracking-wider text-neutral-400">Research plan</p>
          <h3 className="mt-0.5 text-lg font-bold text-neutral-900 dark:text-neutral-50">{preview.title || 'Deep research'}</h3>
        </div>
        <span className="inline-flex flex-shrink-0 items-center gap-1.5 rounded-full border border-neutral-200 px-2.5 py-1 text-xs font-semibold text-neutral-500 dark:border-neutral-700 dark:text-neutral-400">
          <Clock3 size={13} /> {preview.estimated_duration || 'Ready in a few minutes'}
        </span>
      </div>

      {preview.goal && <p className="text-sm leading-relaxed text-neutral-600 dark:text-neutral-300">{preview.goal}</p>}

      {(preview.workflow || []).length > 0 && (
        <div className="grid gap-3 border-l-2 border-neutral-200 pl-4 dark:border-neutral-700">
          {(preview.workflow || []).slice(0, 4).map((step, index) => {
            const Icon = index === 0 ? BookOpen : index === 1 ? Library : Sparkles
            return (
              <div key={`${step.label}-${index}`} className="relative grid grid-cols-[28px_minmax(0,1fr)] gap-3">
                <span className="-ml-[34px] grid h-7 w-7 place-items-center rounded-full border border-neutral-200 bg-white text-emerald-600 dark:border-neutral-700 dark:bg-neutral-900 dark:text-emerald-400">
                  <Icon size={14} />
                </span>
                <div>
                  <h4 className="text-xs font-bold uppercase tracking-wide text-emerald-700 dark:text-emerald-400">{step.label || `Step ${index + 1}`}</h4>
                  {step.description && <p className="mt-1 text-xs leading-relaxed text-neutral-600 dark:text-neutral-400">{step.description}</p>}
                </div>
              </div>
            )
          })}
        </div>
      )}

      {!!preview.investigate?.length && (
        <div className="grid gap-2">
          <h4 className="text-xs font-bold uppercase tracking-wide text-emerald-700 dark:text-emerald-400">I’ll investigate</h4>
          <ol className="grid gap-1.5 pl-5 text-sm text-neutral-600 dark:text-neutral-300">
            {preview.investigate.slice(0, 8).map((item, index) => <li key={`${item}-${index}`} className="list-decimal">{item}</li>)}
          </ol>
        </div>
      )}

      {!!preview.source_strategy?.length && (
        <div className="grid gap-2">
          <h4 className="text-xs font-bold uppercase tracking-wide text-emerald-700 dark:text-emerald-400">I’ll use</h4>
          <ul className="grid gap-1.5 pl-5 text-sm text-neutral-600 dark:text-neutral-300">
            {preview.source_strategy.slice(0, 8).map((item, index) => <li key={`${item}-${index}`} className="list-disc">{item}</li>)}
          </ul>
        </div>
      )}

      <div className="grid grid-cols-3 gap-2">
        <div className="grid gap-0.5 rounded-lg border border-neutral-200 bg-white p-2.5 dark:border-neutral-800 dark:bg-neutral-900">
          <span className="text-[10px] font-bold uppercase tracking-wide text-neutral-400">Coverage</span>
          <strong className="truncate text-sm text-neutral-900 dark:text-neutral-50">{preview.coverage?.required_cells ?? 'planned'}</strong>
        </div>
        <div className="grid gap-0.5 rounded-lg border border-neutral-200 bg-white p-2.5 dark:border-neutral-800 dark:bg-neutral-900">
          <span className="text-[10px] font-bold uppercase tracking-wide text-neutral-400">Workers</span>
          <strong className="truncate text-sm text-neutral-900 dark:text-neutral-50">{preview.workers?.length || 'planned'}</strong>
        </div>
        <div className="grid gap-0.5 rounded-lg border border-neutral-200 bg-white p-2.5 dark:border-neutral-800 dark:bg-neutral-900">
          <span className="text-[10px] font-bold uppercase tracking-wide text-neutral-400">Depth</span>
          <strong className="truncate text-sm text-neutral-900 dark:text-neutral-50">{preview.research_level || 'deep'}</strong>
        </div>
      </div>

      {onFollowUp && (
        <div className="flex flex-wrap justify-end gap-2">
          {directOption && (
            <button type="button" onClick={() => onFollowUp(directOption)} className="inline-flex h-9 items-center gap-1.5 rounded-full border border-neutral-200 px-3.5 text-xs font-bold text-neutral-600 dark:border-neutral-700 dark:text-neutral-300">
              Answer directly
            </button>
          )}
          {regularOption && (
            <button type="button" onClick={() => onFollowUp(regularOption)} className="inline-flex h-9 items-center gap-1.5 rounded-full border border-neutral-200 bg-white px-3.5 text-xs font-bold text-neutral-900 dark:border-neutral-700 dark:bg-neutral-900 dark:text-neutral-50">
              Use regular research
            </button>
          )}
          {startOption && (
            <button type="button" onClick={() => onFollowUp(startOption)} className="inline-flex h-9 items-center gap-1.5 rounded-full bg-neutral-900 px-3.5 text-xs font-bold text-white dark:bg-white dark:text-neutral-900">
              <Send size={13} /> Start research
            </button>
          )}
        </div>
      )}
    </div>
  )
}
