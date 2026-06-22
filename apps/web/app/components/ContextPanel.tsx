'use client'

import { ArrowUpRight, BookOpen, Check, ChevronDown, Clock3, Download, FileText, List, Settings2, Sliders, Sparkles } from 'lucide-react'
import { useEffect, useState, type ReactNode } from 'react'
import { eventChips, engineEventsCopyText, buildWorkSummary } from '../lib/commentary'
import type { AgentResult, Artifact, Conversation, DocumentTemplateOption, OutputFormat, ProfileSettings, ProgressEvent, QualityMode, ResearchLevel, Source } from '../types'
import { CopyButton } from './ui/CopyButton'
import { SelectField } from './ui/Field'

const QUALITY_OPTIONS = [
  { value: 'draft', label: 'draft' },
  { value: 'standard', label: 'standard' },
  { value: 'executive', label: 'executive' },
]
const OUTPUT_OPTIONS = [
  { value: 'chat', label: 'chat' },
  { value: 'markdown', label: 'markdown' },
  { value: 'docx', label: 'docx' },
  { value: 'pptx', label: 'pptx' },
]
const RESEARCH_OPTIONS = [
  { value: 'auto', label: 'auto' },
  { value: 'easy', label: 'easy' },
  { value: 'regular', label: 'regular' },
  { value: 'deep', label: 'deep' },
]

export function ContextPanel({
  view,
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
  profileSettings,
  onUpdateProfileSettings,
  onRefreshTemplates,
}: {
  view: 'chat' | 'profile'
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
  profileSettings: ProfileSettings
  onUpdateProfileSettings: (settings: Partial<ProfileSettings>) => void | Promise<ProfileSettings>
  onRefreshTemplates: () => void | Promise<void>
}) {
  const workSummary = buildWorkSummary({ result, events, sources, activeConversation, currentMessage })
  const defaultTemplateId = profileSettings.default_template_id || ''
  const defaultTemplate = templates.find(template => template.id === defaultTemplateId)
  const hasWorkContext = Boolean(result || events.length || activeConversation || currentMessage.trim())

  return (
    <div className="flex h-full flex-col">
      <div className="mb-4">
        <p className="text-[11px] font-bold uppercase tracking-wider text-neutral-400">Context</p>
        <h2 className="mt-0.5 text-lg font-bold text-neutral-900 dark:text-neutral-50">Current work</h2>
      </div>

      <div className="flex-1 space-y-3 overflow-y-auto">
        <CollapsibleTile icon={Sparkles} title="Work summary" subtitle={workSummary.route} defaultOpen={view === 'chat' && hasWorkContext}>
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
        </CollapsibleTile>

        <CollapsibleTile icon={Settings2} title="Quick Profile Settings" subtitle={defaultTemplate?.name || 'Default template'} defaultOpen={view === 'profile'}>
          <div className="grid gap-2 border-t border-neutral-100 px-4 py-3.5 dark:border-neutral-800">
            <SelectField
              label="Quality"
              value={profileSettings.quality_mode || 'standard'}
              onChange={value => onUpdateProfileSettings({ quality_mode: value as QualityMode })}
              options={QUALITY_OPTIONS}
            />
            <SelectField
              label="Output"
              value={profileSettings.output_format || 'chat'}
              onChange={value => onUpdateProfileSettings({ output_format: value as OutputFormat })}
              options={OUTPUT_OPTIONS}
            />
            <SelectField
              label="Research"
              value={profileSettings.research_level || 'auto'}
              onChange={value => onUpdateProfileSettings({ research_level: value as ResearchLevel })}
              options={RESEARCH_OPTIONS}
            />
            <SelectField
              label="Default deck"
              value={defaultTemplateId}
              onChange={value => onUpdateProfileSettings({ default_template_id: value })}
              options={[{ value: '', label: 'Fronei default' }, ...templates.map(template => ({ value: template.id, label: template.name }))]}
            />
            {templateStatus && <p className="text-xs font-medium text-neutral-400">{templateStatus}</p>}
            {templateError && (
              <p className="rounded-md border-l-3 border-red-400 bg-red-50 px-2.5 py-1.5 text-xs font-medium text-red-700 dark:bg-red-500/10 dark:text-red-400">{templateError}</p>
            )}
            <details className="rounded-lg border border-neutral-200 bg-neutral-50 dark:border-neutral-800 dark:bg-neutral-950">
              <summary className="flex cursor-pointer list-none items-center justify-between gap-2 px-3 py-2 text-xs font-bold text-neutral-700 dark:text-neutral-200">
                Uploaded templates
                <List size={13} className="text-neutral-400" />
              </summary>
              <div className="border-t border-neutral-200 p-2 dark:border-neutral-800">
                {!templatesLoaded && <p className="text-sm text-neutral-400">Loading templates...</p>}
                {templatesLoaded && templates.length === 0 && <p className="text-sm text-neutral-400">No saved templates yet.</p>}
                {templates.map(template => (
                  <div key={template.id} className="flex items-center justify-between gap-2 border-t border-neutral-100 py-2 first:border-0 first:pt-0 dark:border-neutral-800">
                    <div className="min-w-0">
                      <strong className="block truncate text-[13px] font-bold text-neutral-900 dark:text-neutral-50">{template.name}</strong>
                      <span className="block truncate text-[11px] text-neutral-400">{template.user_template ? 'Uploaded template' : 'Built-in template'}</span>
                    </div>
                    {defaultTemplateId === template.id && <Check size={14} className="flex-shrink-0 text-emerald-600" />}
                  </div>
                ))}
              </div>
            </details>
            <button type="button" onClick={() => onRefreshTemplates()} className="h-8 rounded-lg border border-neutral-200 text-xs font-bold text-neutral-600 dark:border-neutral-700 dark:text-neutral-300">
              Refresh templates
            </button>
          </div>
        </CollapsibleTile>

        <CollapsibleTile icon={Clock3} title="Status" subtitle={result ? `Completed as ${result.route}` : events.length ? 'In progress' : 'Waiting'} defaultOpen={view === 'chat' && Boolean(result || events.length)}>
          <div className="border-t border-neutral-100 px-4 py-3.5 dark:border-neutral-800">
          <p className="text-sm leading-relaxed text-neutral-500 dark:text-neutral-400">
            {result ? `Completed as ${result.route}` : events.length ? 'In progress' : 'Waiting'}
          </p>
          {result?.model_used && <p className="mt-1 text-xs text-neutral-400">{result.model_used}</p>}
          </div>
        </CollapsibleTile>

        <CollapsibleTile icon={Sliders} title="Engine events" subtitle={`${events.length || 0} recorded`} defaultOpen={view === 'chat' && events.length > 0}>
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
        </CollapsibleTile>

        {latestArtifact && (
          <CollapsibleTile icon={FileText} title="Generated document" subtitle={latestArtifact.filename} defaultOpen={view === 'chat'} tone="amber">
            <div className="mb-2 flex items-center gap-2 text-amber-700 dark:text-amber-400">
              <FileText size={15} />
              <h3 className="text-sm font-bold text-neutral-900 dark:text-neutral-50">Generated document</h3>
            </div>
            <p className="truncate text-sm font-bold text-neutral-900 dark:text-neutral-50">{latestArtifact.filename}</p>
            <p className="mt-0.5 text-xs text-neutral-400">Saved with this work session.</p>
            <button type="button" onClick={() => downloadArtifact(latestArtifact)} className="mt-3 flex h-9 w-full items-center justify-center gap-2 rounded-lg bg-neutral-900 text-sm font-semibold text-white dark:bg-white dark:text-neutral-900">
              <Download size={15} /> Download
            </button>
          </CollapsibleTile>
        )}

        <CollapsibleTile icon={BookOpen} title="Sources" subtitle={`${sources.length} attached`} defaultOpen={view === 'chat' && sources.length > 0}>
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
        </CollapsibleTile>
      </div>
    </div>
  )
}

function CollapsibleTile({
  icon: Icon,
  title,
  subtitle,
  defaultOpen,
  tone = 'neutral',
  children,
}: {
  icon: typeof Sparkles
  title: string
  subtitle?: string
  defaultOpen: boolean
  tone?: 'neutral' | 'amber'
  children: ReactNode
}) {
  const [open, setOpen] = useState(defaultOpen)

  useEffect(() => {
    setOpen(defaultOpen)
  }, [defaultOpen, title])

  return (
    <section className={`overflow-hidden rounded-xl border ${tone === 'amber' ? 'border-amber-200 bg-amber-50/60 dark:border-amber-500/20 dark:bg-amber-500/5' : 'border-neutral-200 bg-white dark:border-neutral-800 dark:bg-neutral-900'}`}>
      <button type="button" onClick={() => setOpen(value => !value)} className="flex w-full items-center justify-between gap-3 px-4 py-3 text-left">
        <span className="flex min-w-0 items-center gap-2.5">
          <Icon size={15} className={tone === 'amber' ? 'text-amber-600 dark:text-amber-400' : 'text-neutral-400'} />
          <span className="min-w-0">
            <span className="block truncate text-sm font-bold text-neutral-900 dark:text-neutral-50">{title}</span>
            {subtitle && <span className="mt-0.5 block truncate text-xs font-medium text-neutral-400">{subtitle}</span>}
          </span>
        </span>
        <ChevronDown size={16} className={`flex-shrink-0 text-neutral-400 transition-transform ${open ? 'rotate-180' : ''}`} />
      </button>
      {open && children}
    </section>
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
