'use client'

import { ChevronDown, ChevronUp, Loader2, Send, Shield, Upload } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import type { DocumentTemplateOption, OutputFormat, QualityMode, ResearchLevel } from '../types'
import { SelectField, Textarea } from './ui/Field'

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
// Admin-only per-turn override. Kept short and curated; "Custom" covers
// anything else litellm supports.
const CURATED_MODEL_OPTIONS = [
  { value: '', label: 'Default (org policy)' },
  { value: 'gpt-4.1-mini', label: 'gpt-4.1-mini' },
  { value: 'gpt-4.1', label: 'gpt-4.1' },
  { value: 'claude-sonnet-4-6', label: 'claude-sonnet-4-6' },
  { value: 'claude-opus-4-8', label: 'claude-opus-4-8' },
  { value: 'gemini/gemini-2.5-flash', label: 'gemini/gemini-2.5-flash' },
  { value: '__custom__', label: 'Custom…' },
]

export function Composer({
  message,
  setMessage,
  qualityMode,
  setQualityMode,
  outputFormat,
  setOutputFormat,
  researchLevel,
  setResearchLevel,
  running,
  canRun,
  run,
  onUploadTemplate,
  templates,
  selectedTemplateId,
  setSelectedTemplateId,
  templateStatus,
  isAdmin,
  modelOverride,
  setModelOverride,
}: {
  message: string
  setMessage: (message: string) => void
  qualityMode: QualityMode
  setQualityMode: (mode: QualityMode) => void
  outputFormat: OutputFormat
  setOutputFormat: (format: OutputFormat) => void
  researchLevel: ResearchLevel
  setResearchLevel: (level: ResearchLevel) => void
  running: boolean
  canRun: boolean
  run: () => void
  onUploadTemplate: () => void
  templates: DocumentTemplateOption[]
  selectedTemplateId: string
  setSelectedTemplateId: (templateId: string) => void
  templateStatus: string
  isAdmin: boolean
  modelOverride: string
  setModelOverride: (model: string) => void
}) {
  const [optionsOpen, setOptionsOpen] = useState(false)
  const selectedTemplateName = templates.find(template => template.id === selectedTemplateId)?.name || 'Default'
  const popoverRef = useRef<HTMLDivElement | null>(null)
  const toggleRef = useRef<HTMLButtonElement | null>(null)
  const isCuratedModel = CURATED_MODEL_OPTIONS.some(option => option.value === modelOverride)
  const [modelSelectValue, setModelSelectValue] = useState(() => (modelOverride && !isCuratedModel ? '__custom__' : modelOverride))

  useEffect(() => {
    if (!optionsOpen) return
    function onPointerDown(event: PointerEvent) {
      const target = event.target as Node
      if (popoverRef.current?.contains(target) || toggleRef.current?.contains(target)) return
      setOptionsOpen(false)
    }
    function onKey(event: KeyboardEvent) {
      if (event.key === 'Escape') setOptionsOpen(false)
    }
    document.addEventListener('pointerdown', onPointerDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('pointerdown', onPointerDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [optionsOpen])

  return (
    <div className="relative flex h-full min-h-0 flex-col rounded-xl border border-neutral-200 bg-white shadow-sm dark:border-neutral-800 dark:bg-neutral-900">
      <div className="flex h-full min-h-0 flex-col overflow-hidden rounded-xl">
        <Textarea
          value={message}
          onChange={event => setMessage(event.target.value)}
          onKeyDown={event => {
            if (event.key === 'Enter' && !event.shiftKey) {
              event.preventDefault()
              if (canRun) run()
            }
          }}
          placeholder="Give Fronei a task..."
          className="min-h-[44px] px-3 pt-3"
        />

        <div className="flex flex-shrink-0 flex-col gap-2 border-t border-neutral-100 px-2.5 py-2.5 dark:border-neutral-800">
          <div className="grid grid-cols-[minmax(0,1fr)_40px_40px] items-center gap-2">
            <button
              ref={toggleRef}
              type="button"
              onClick={() => setOptionsOpen(open => !open)}
              aria-expanded={optionsOpen}
              className="flex h-10 min-w-0 items-center justify-start gap-2 overflow-hidden rounded-lg border border-neutral-200 bg-neutral-50 px-3 text-xs font-bold text-neutral-700 dark:border-neutral-800 dark:bg-neutral-800/60 dark:text-neutral-200"
            >
              {optionsOpen ? <ChevronUp size={15} /> : <ChevronDown size={15} />}
              <span className="truncate">{outputFormat}{researchLevel !== 'auto' ? ` · ${researchLevel}` : ''}</span>
              {isAdmin && modelOverride && (
                <span className="ml-auto inline-flex flex-shrink-0 items-center gap-1 rounded-full bg-amber-100 px-1.5 py-0.5 text-[10px] font-bold text-amber-800 dark:bg-amber-500/15 dark:text-amber-400">
                  <Shield size={10} /> {modelOverride}
                </span>
              )}
            </button>
            <button
              type="button"
              onClick={onUploadTemplate}
              title="Upload a PowerPoint template to your profile"
              aria-label="Upload a PowerPoint template to your profile"
              className="grid h-10 w-10 place-items-center rounded-lg border border-neutral-200 text-neutral-500 dark:border-neutral-800 dark:text-neutral-400"
            >
              <Upload size={16} />
            </button>
            <button
              type="button"
              onClick={run}
              disabled={!canRun}
              aria-label={running ? 'Working' : 'Start'}
              className="grid h-10 w-10 place-items-center rounded-lg bg-neutral-900 text-white disabled:bg-neutral-300 dark:bg-white dark:text-neutral-900 dark:disabled:bg-neutral-700"
            >
              {running ? <Loader2 size={16} className="animate-spin" /> : <Send size={16} />}
            </button>
          </div>
          {templateStatus && <p className="text-xs font-semibold text-emerald-600 dark:text-emerald-400">{templateStatus}</p>}
        </div>
      </div>

      {optionsOpen && (
        <div
          ref={popoverRef}
          className="absolute inset-x-0 bottom-full z-30 mb-2 grid grid-cols-2 gap-2 rounded-xl border border-neutral-200 bg-white p-3 shadow-xl dark:border-neutral-700 dark:bg-neutral-900 sm:grid-cols-4 sm:p-3.5"
        >
          <SelectField label="Quality" value={qualityMode} onChange={value => setQualityMode(value as QualityMode)} options={QUALITY_OPTIONS} />
          <SelectField label="Output" value={outputFormat} onChange={value => setOutputFormat(value as OutputFormat)} options={OUTPUT_OPTIONS} />
          <SelectField label="Research" value={researchLevel} onChange={value => setResearchLevel(value as ResearchLevel)} options={RESEARCH_OPTIONS} />
          <SelectField
            label="Template"
            value={selectedTemplateId}
            onChange={setSelectedTemplateId}
            options={[{ value: '', label: 'Default' }, ...templates.map(template => ({ value: template.id, label: template.name }))]}
          />
          <p className="col-span-full truncate text-xs font-medium text-neutral-400">Template: {selectedTemplateName}</p>

          {isAdmin && (
            <div className="col-span-full grid gap-2 border-t border-dashed border-amber-300 pt-2.5 dark:border-amber-500/30">
              <div className="flex items-center gap-1.5 text-[11px] font-bold uppercase tracking-wide text-amber-700 dark:text-amber-400">
                <Shield size={12} /> Admin: model for this turn only
              </div>
              <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
                <SelectField
                  label="Model"
                  value={modelSelectValue}
                  onChange={value => {
                    setModelSelectValue(value)
                    setModelOverride(value === '__custom__' ? modelOverride : value)
                  }}
                  options={CURATED_MODEL_OPTIONS}
                  className="col-span-2 sm:col-span-2"
                />
                {modelSelectValue === '__custom__' && (
                  <input
                    value={modelOverride}
                    onChange={event => setModelOverride(event.target.value)}
                    placeholder="e.g. openrouter/qwen/qwen3.7-max"
                    className="col-span-2 min-h-9 rounded-lg border border-neutral-200 bg-neutral-50 px-2.5 text-xs font-semibold text-neutral-900 outline-none dark:border-neutral-800 dark:bg-neutral-900 dark:text-neutral-100 sm:col-span-2"
                  />
                )}
              </div>
              <p className="text-[11px] leading-relaxed text-neutral-400">
                Overrides every stage for this turn only. Everyone else keeps the org default. Set by an admin in /admin → Model policy.
              </p>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
