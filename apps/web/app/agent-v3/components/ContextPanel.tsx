'use client'

import { ArrowUpRight, BookOpen, ChevronDown, Clock3, Download, FileText, Trash2, Upload } from 'lucide-react'
import { eventChips, engineEventsCopyText, buildWorkSummary } from '../lib/commentary'
import type { AgentResult, Artifact, Conversation, DocumentTemplateOption, ProgressEvent, Source } from '../types'
import { Card, CardHeader } from './ui/Card'
import { CopyButton } from './ui/CopyButton'

export function ContextPanel({
  result,
  events,
  sources,
  latestArtifact,
  activeConversation,
  currentMessage,
  downloadArtifact,
  traceOpen,
  setTraceOpen,
  copiedKey,
  onCopyText,
  templates,
  templatesLoaded,
  templateStatus,
  templateError,
  templateDeleteId,
  onUploadTemplate,
  onRefreshTemplates,
  onRequestDeleteTemplate,
  onCancelDeleteTemplate,
  onDeleteTemplate,
}: {
  result: AgentResult | null
  events: ProgressEvent[]
  sources: Source[]
  latestArtifact?: Artifact
  activeConversation: Conversation | null
  currentMessage: string
  downloadArtifact: (artifact: Artifact) => void | Promise<void>
  traceOpen: boolean
  setTraceOpen: (open: boolean) => void
  copiedKey: string | null
  onCopyText: (value: string, key: string) => void | Promise<void>
  templates: DocumentTemplateOption[]
  templatesLoaded: boolean
  templateStatus: string
  templateError: string
  templateDeleteId: string | null
  onUploadTemplate: () => void
  onRefreshTemplates: () => void | Promise<void>
  onRequestDeleteTemplate: (templateId: string) => void
  onCancelDeleteTemplate: () => void
  onDeleteTemplate: (templateId: string) => void | Promise<void>
}) {
  const workSummary = buildWorkSummary({ result, events, sources, activeConversation, currentMessage })

  return (
    <div className="flex h-full flex-col">
      <div className="mb-4">
        <p className="text-[11px] font-bold uppercase tracking-wider text-neutral-400">Context</p>
        <h2 className="mt-0.5 text-lg font-bold text-neutral-900 dark:text-neutral-50">Current work</h2>
      </div>

      <div className="flex-1 space-y-3 overflow-y-auto">
        <details open className="overflow-hidden rounded-xl border border-neutral-200 bg-white dark:border-neutral-800 dark:bg-neutral-900">
          <summary className="cursor-pointer list-none px-4 py-3 text-sm font-bold text-neutral-900 dark:text-neutral-50">Work summary</summary>
          <div className="border-t border-neutral-100 px-4 py-3.5 dark:border-neutral-800">
            <p className="mb-3 line-clamp-3 text-sm font-bold text-neutral-900 dark:text-neutral-50">{workSummary.title}</p>
            <div className="grid grid-cols-[minmax(0,1fr)_auto] gap-y-2 gap-x-3 text-xs">
              <SummaryRow label="Turns" value={workSummary.turns} />
              <SummaryRow label="Route" value={workSummary.route} />
              <SummaryRow label="Time" value={workSummary.time} />
              <SummaryRow label="Budget" value={workSummary.budget} />
              <SummaryRow label="Sources" value={workSummary.sources} />
              <SummaryRow label="Events" value={workSummary.events} />
            </div>
          </div>
        </details>

        <section className="rounded-xl border border-neutral-200 bg-white p-4 dark:border-neutral-800 dark:bg-neutral-900">
          <div className="mb-2.5 flex items-center gap-2 text-amber-600 dark:text-amber-400">
            <Upload size={15} />
            <h3 className="text-sm font-bold text-neutral-900 dark:text-neutral-50">Profile templates</h3>
          </div>
          <p className="text-sm leading-relaxed text-neutral-500 dark:text-neutral-400">
            Upload PowerPoint templates once, then use them from any conversation.
          </p>
          <div className="mt-3 grid grid-cols-[minmax(0,1fr)_auto] gap-2">
            <button type="button" onClick={onUploadTemplate} className="flex h-9 items-center justify-center gap-2 rounded-lg bg-neutral-900 text-sm font-semibold text-white dark:bg-white dark:text-neutral-900">
              <Upload size={15} /> Upload PPTX
            </button>
            <button type="button" onClick={() => onRefreshTemplates()} className="h-9 rounded-lg border border-neutral-200 px-3 text-sm font-semibold text-neutral-600 dark:border-neutral-700 dark:text-neutral-300">
              Refresh
            </button>
          </div>
          {templateStatus && <p className="mt-2 text-xs font-medium text-neutral-400">{templateStatus}</p>}
          {templateError && (
            <p className="mt-2 rounded-md border-l-3 border-red-400 bg-red-50 px-2.5 py-1.5 text-xs font-medium text-red-700 dark:bg-red-500/10 dark:text-red-400">{templateError}</p>
          )}
          {!templatesLoaded && <p className="mt-2 text-sm text-neutral-400">Loading templates...</p>}
          {templatesLoaded && templates.length === 0 && <p className="mt-2 text-sm text-neutral-400">No saved templates yet.</p>}
          {templates.length > 0 && (
            <div className="mt-3 grid gap-2">
              {templates.map(template => (
                <div key={template.id} className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-2 border-t border-neutral-100 pt-2 dark:border-neutral-800">
                  <div className="min-w-0">
                    <strong className="block truncate text-[13px] font-bold text-neutral-900 dark:text-neutral-50">{template.name}</strong>
                    <span className="block truncate text-[11px] text-neutral-400">{template.user_template ? 'Uploaded template' : 'Built-in template'}</span>
                  </div>
                  {template.user_template && (
                    templateDeleteId === template.id ? (
                      <div className="flex gap-1.5">
                        <button type="button" onClick={() => onDeleteTemplate(template.id)} className="rounded-md border border-red-200 px-2 py-1 text-[11px] font-bold text-red-600 dark:border-red-500/30 dark:text-red-400">Delete</button>
                        <button type="button" onClick={onCancelDeleteTemplate} className="rounded-md border border-neutral-200 px-2 py-1 text-[11px] font-bold text-neutral-600 dark:border-neutral-700 dark:text-neutral-300">Keep</button>
                      </div>
                    ) : (
                      <button
                        type="button"
                        onClick={() => onRequestDeleteTemplate(template.id)}
                        aria-label={`Delete ${template.name}`}
                        className="grid h-7 w-7 place-items-center rounded-full text-neutral-400 hover:bg-neutral-100 dark:hover:bg-neutral-800"
                      >
                        <Trash2 size={13} />
                      </button>
                    )
                  )}
                </div>
              ))}
            </div>
          )}
        </section>

        <Card className="p-4">
          <CardHeader className="mb-2 text-amber-600 dark:text-amber-400">
            <Clock3 size={15} />
            <h3 className="text-sm font-bold text-neutral-900 dark:text-neutral-50">Status</h3>
          </CardHeader>
          <p className="text-sm leading-relaxed text-neutral-500 dark:text-neutral-400">
            {result ? `Completed as ${result.route}` : events.length ? 'In progress' : 'Waiting'}
          </p>
          {result?.model_used && <p className="mt-1 text-xs text-neutral-400">{result.model_used}</p>}
        </Card>

        <section className="rounded-xl border border-neutral-200 bg-white p-4 dark:border-neutral-800 dark:bg-neutral-900">
          <div className="flex items-center justify-between gap-2">
            <button type="button" onClick={() => setTraceOpen(!traceOpen)} className="min-w-0 flex-1 text-left">
              <span className="flex items-center justify-between gap-2">
                <span>
                  <span className="block text-sm font-bold text-neutral-900 dark:text-neutral-50">Engine events</span>
                  <span className="mt-0.5 block text-xs font-medium text-neutral-400">{events.length || 0} recorded</span>
                </span>
                <ChevronDown size={16} className={`flex-shrink-0 text-neutral-400 transition-transform ${traceOpen ? 'rotate-180' : ''}`} />
              </span>
            </button>
            <CopyButton copied={copiedKey === 'events:all'} label="Copy all engine events" onClick={() => onCopyText(engineEventsCopyText(events), 'events:all')} />
          </div>
          {traceOpen && (
            <div className="mt-3.5 max-h-[360px] space-y-0 overflow-y-auto border-l-2 border-neutral-200 pl-3 dark:border-neutral-700">
              {events.length === 0 && <p className="py-2 text-sm text-neutral-400">No events yet.</p>}
              {events.map((event, index) => (
                <div key={`${event.stage}-${index}`} className="relative border-b border-neutral-100 py-2.5 last:border-0 dark:border-neutral-800">
                  <span className="absolute -left-[17px] top-[15px] h-2 w-2 rounded-full border-2 border-white bg-emerald-600 dark:border-neutral-900" />
                  <p className="text-[11px] font-bold uppercase tracking-wide text-neutral-400">{event.stage}</p>
                  <p className="mt-1 text-sm leading-relaxed text-neutral-700 dark:text-neutral-300">{event.message}</p>
                  {eventChips(event).length > 0 && (
                    <div className="mt-2 flex flex-wrap gap-1.5">
                      {eventChips(event).map(chip => (
                        <span key={chip} className="rounded-md bg-amber-50 px-2 py-0.5 text-[11px] font-semibold text-amber-700 dark:bg-amber-500/10 dark:text-amber-400">{chip}</span>
                      ))}
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}
        </section>

        {latestArtifact && (
          <section className="rounded-xl border border-amber-200 bg-amber-50/60 p-4 dark:border-amber-500/20 dark:bg-amber-500/5">
            <div className="mb-2 flex items-center gap-2 text-amber-700 dark:text-amber-400">
              <FileText size={15} />
              <h3 className="text-sm font-bold text-neutral-900 dark:text-neutral-50">Generated document</h3>
            </div>
            <p className="truncate text-sm font-bold text-neutral-900 dark:text-neutral-50">{latestArtifact.filename}</p>
            <p className="mt-0.5 text-xs text-neutral-400">Saved with this work session.</p>
            <button type="button" onClick={() => downloadArtifact(latestArtifact)} className="mt-3 flex h-9 w-full items-center justify-center gap-2 rounded-lg bg-neutral-900 text-sm font-semibold text-white dark:bg-white dark:text-neutral-900">
              <Download size={15} /> Download
            </button>
          </section>
        )}

        <section className="rounded-xl border border-neutral-200 bg-white p-4 dark:border-neutral-800 dark:bg-neutral-900">
          <div className="mb-2.5 flex items-center gap-2 text-amber-600 dark:text-amber-400">
            <BookOpen size={15} />
            <h3 className="text-sm font-bold text-neutral-900 dark:text-neutral-50">Sources</h3>
          </div>
          {sources.length === 0 && <p className="text-sm text-neutral-400">No sources attached.</p>}
          <div className="grid">
            {sources.map((source, index) => (
              <a
                key={`${source.url}-${index}`}
                href={source.url}
                target="_blank"
                rel="noreferrer"
                className="border-t border-neutral-100 py-3 first:border-0 first:pt-0 dark:border-neutral-800"
              >
                <span className="flex items-start justify-between gap-2">
                  <span className="line-clamp-2 text-sm font-bold text-neutral-900 dark:text-neutral-50">{source.title || source.url}</span>
                  <ArrowUpRight size={14} className="mt-0.5 flex-shrink-0 text-neutral-400" />
                </span>
                {source.url && <span className="mt-1 block truncate text-xs text-neutral-400">{source.url}</span>}
              </a>
            ))}
          </div>
        </section>
      </div>
    </div>
  )
}

function SummaryRow({ label, value }: { label: string; value: string }) {
  return (
    <>
      <span className="text-neutral-400">{label}</span>
      <strong className="text-right font-bold text-neutral-900 dark:text-neutral-50">{value}</strong>
    </>
  )
}
