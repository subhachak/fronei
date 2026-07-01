'use client'

import { FileText, Loader2, Paperclip, Send, Shield, SlidersHorizontal, Square, Upload, X } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import type { AttachedFile, DocumentTemplateOption, OutputFormat, QualityMode, ResearchLevel } from '../types'
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
  cancel,
  cancelling,
  onUploadTemplate,
  templates,
  selectedTemplateId,
  setSelectedTemplateId,
  templateStatus,
  isAdmin,
  modelOverride,
  setModelOverride,
  onAttachFile,
  attachedFile,
  attachingFile,
  attachmentError,
  onClearAttachment,
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
  cancel: () => void
  cancelling: boolean
  onUploadTemplate: () => void
  templates: DocumentTemplateOption[]
  selectedTemplateId: string
  setSelectedTemplateId: (templateId: string) => void
  templateStatus: string
  isAdmin: boolean
  modelOverride: string
  setModelOverride: (model: string) => void
  onAttachFile: () => void
  attachedFile: AttachedFile | null
  attachingFile: boolean
  attachmentError: string
  onClearAttachment: () => void
}) {
  const [optionsOpen, setOptionsOpen] = useState(false)
  const [docPopupOpen, setDocPopupOpen] = useState(false)
  const popoverRef = useRef<HTMLDivElement | null>(null)
  const toggleRef = useRef<HTMLButtonElement | null>(null)
  const docPopoverRef = useRef<HTMLDivElement | null>(null)
  const docToggleRef = useRef<HTMLButtonElement | null>(null)
  const isCuratedModel = CURATED_MODEL_OPTIONS.some(option => option.value === modelOverride)
  const [modelSelectValue, setModelSelectValue] = useState(() => (modelOverride && !isCuratedModel ? '__custom__' : modelOverride))
  const selectedTemplateName = templates.find(template => template.id === selectedTemplateId)?.name || 'Default'

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

  useEffect(() => {
    if (!docPopupOpen) return
    function onPointerDown(event: PointerEvent) {
      const target = event.target as Node
      if (docPopoverRef.current?.contains(target) || docToggleRef.current?.contains(target)) return
      setDocPopupOpen(false)
    }
    function onKey(event: KeyboardEvent) {
      if (event.key === 'Escape') setDocPopupOpen(false)
    }
    document.addEventListener('pointerdown', onPointerDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('pointerdown', onPointerDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [docPopupOpen])

  function handleOutputFormatChange(value: string) {
    setOutputFormat(value as OutputFormat)
    if (value === 'pptx') {
      setOptionsOpen(false)
      setDocPopupOpen(true)
    }
  }

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

        {(attachedFile || attachingFile || attachmentError) && (
          <div className="flex flex-shrink-0 items-center gap-2 px-3 pb-1.5">
            {attachingFile && (
              <span className="inline-flex items-center gap-1.5 text-[11px] font-medium text-neutral-400">
                <Loader2 size={12} className="animate-spin" /> Reading file…
              </span>
            )}
            {attachedFile && !attachingFile && (
              <span className="inline-flex max-w-full items-center gap-1.5 truncate rounded-full bg-neutral-100 px-2 py-1 text-[11px] font-semibold text-neutral-600 dark:bg-neutral-800 dark:text-neutral-300">
                <Paperclip size={11} className="flex-shrink-0" />
                <span className="truncate">{attachedFile.name}</span>
                {attachedFile.truncated && <span className="flex-shrink-0 text-amber-600 dark:text-amber-400">(truncated)</span>}
                <button type="button" onClick={onClearAttachment} aria-label="Remove attachment" className="flex-shrink-0 text-neutral-400 hover:text-neutral-700 dark:hover:text-neutral-100">
                  <X size={12} />
                </button>
              </span>
            )}
            {attachmentError && <span className="truncate text-[11px] font-medium text-red-600 dark:text-red-400">{attachmentError}</span>}
          </div>
        )}

        <div className="flex flex-shrink-0 items-center gap-1.5 px-2 py-1.5">
          <button
            ref={toggleRef}
            type="button"
            onClick={() => setOptionsOpen(open => !open)}
            aria-expanded={optionsOpen}
            aria-label="Task options"
            title="Task options"
            className="flex h-7 min-w-0 flex-shrink-0 items-center gap-1 rounded-md px-1.5 text-[11px] font-medium text-neutral-400 hover:bg-neutral-100 hover:text-neutral-600 dark:text-neutral-500 dark:hover:bg-neutral-800 dark:hover:text-neutral-300"
          >
            <SlidersHorizontal size={13} />
            {(outputFormat !== 'chat' || researchLevel !== 'auto') && (
              <span className="truncate">{outputFormat !== 'chat' ? outputFormat : ''}{researchLevel !== 'auto' ? ` · ${researchLevel}` : ''}</span>
            )}
          </button>
          {outputFormat === 'pptx' && (
            <button
              ref={docToggleRef}
              type="button"
              onClick={() => setDocPopupOpen(open => !open)}
              aria-expanded={docPopupOpen}
              aria-label="Presentation template"
              title="Presentation template"
              className="flex h-7 min-w-0 flex-shrink-0 items-center gap-1 truncate rounded-md px-1.5 text-[11px] font-medium text-neutral-400 hover:bg-neutral-100 hover:text-neutral-600 dark:text-neutral-500 dark:hover:bg-neutral-800 dark:hover:text-neutral-300"
            >
              <FileText size={13} />
              <span className="truncate">{selectedTemplateName}</span>
            </button>
          )}
          {isAdmin && modelOverride && (
            <span className="inline-flex flex-shrink-0 items-center gap-1 rounded-full bg-amber-100 px-1.5 py-0.5 text-[10px] font-bold text-amber-800 dark:bg-amber-500/15 dark:text-amber-400">
              <Shield size={9} /> {modelOverride}
            </span>
          )}
          {templateStatus && <p className="min-w-0 truncate text-[11px] font-medium text-emerald-600 dark:text-emerald-400">{templateStatus}</p>}
          <div className="ml-auto flex flex-shrink-0 items-center gap-1.5">
            <button
              type="button"
              onClick={onAttachFile}
              disabled={attachingFile}
              title="Attach a file or photo for context"
              aria-label="Attach a file or photo for context"
              className="grid h-9 w-9 place-items-center rounded-lg text-neutral-400 hover:bg-neutral-100 hover:text-neutral-600 disabled:opacity-50 dark:text-neutral-500 dark:hover:bg-neutral-800 dark:hover:text-neutral-300"
            >
              {attachingFile ? <Loader2 size={15} className="animate-spin" /> : <Paperclip size={15} />}
            </button>
            <button
              type="button"
              onClick={running ? cancel : run}
              disabled={running ? cancelling : !canRun}
              aria-label={running ? 'Stop' : 'Start'}
              title={running ? 'Stop this turn' : 'Start'}
              className={`grid h-9 w-9 place-items-center rounded-lg text-white disabled:opacity-50 ${
                running
                  ? 'bg-red-600 hover:bg-red-700 dark:bg-red-600 dark:hover:bg-red-500'
                  : 'bg-neutral-900 disabled:bg-neutral-300 dark:bg-white dark:text-neutral-900 dark:disabled:bg-neutral-700'
              }`}
            >
              {running ? (
                cancelling ? <Loader2 size={15} className="animate-spin" /> : <Square size={14} fill="currentColor" />
              ) : (
                <Send size={15} />
              )}
            </button>
          </div>
        </div>
      </div>

      {optionsOpen && (
        <div
          ref={popoverRef}
          className="absolute inset-x-0 bottom-full z-30 mb-2 grid grid-cols-2 gap-2 rounded-xl border border-neutral-200 bg-white p-3 shadow-xl dark:border-neutral-700 dark:bg-neutral-900 sm:grid-cols-4 sm:p-3.5"
        >
          <SelectField label="Quality" value={qualityMode} onChange={value => setQualityMode(value as QualityMode)} options={QUALITY_OPTIONS} />
          <SelectField label="Output" value={outputFormat} onChange={handleOutputFormatChange} options={OUTPUT_OPTIONS} />
          <SelectField label="Research" value={researchLevel} onChange={value => setResearchLevel(value as ResearchLevel)} options={RESEARCH_OPTIONS} />
          {isAdmin && (
            <SelectField
              label="Model"
              value={modelSelectValue}
              onChange={value => {
                setModelSelectValue(value)
                setModelOverride(value === '__custom__' ? modelOverride : value)
              }}
              options={CURATED_MODEL_OPTIONS}
              className="ring-1 ring-inset ring-amber-300 dark:ring-amber-500/40"
            />
          )}
          {isAdmin && modelSelectValue === '__custom__' && (
            <input
              value={modelOverride}
              onChange={event => setModelOverride(event.target.value)}
              placeholder="litellm model id, e.g. gpt-4.1"
              className="col-span-2 min-h-9 rounded-lg border border-neutral-200 bg-neutral-50 px-2.5 text-xs font-semibold text-neutral-900 outline-none dark:border-neutral-800 dark:bg-neutral-900 dark:text-neutral-100 sm:col-span-2"
            />
          )}
        </div>
      )}

      {docPopupOpen && (
        <div
          ref={docPopoverRef}
          className="absolute inset-x-0 bottom-full z-30 mb-2 grid gap-2.5 rounded-xl border border-neutral-200 bg-white p-3 shadow-xl dark:border-neutral-700 dark:bg-neutral-900 sm:p-3.5"
        >
          <div className="flex items-center gap-1.5 text-[11px] font-bold uppercase tracking-wide text-neutral-400">
            <FileText size={12} /> Presentation template
          </div>
          <div className="grid grid-cols-2 gap-2">
            <SelectField
              label="Template"
              value={selectedTemplateId}
              onChange={setSelectedTemplateId}
              options={[{ value: '', label: 'Default' }, ...templates.map(template => ({ value: template.id, label: template.name }))]}
              className="col-span-2 sm:col-span-1"
            />
            <button
              type="button"
              onClick={onUploadTemplate}
              className="col-span-2 flex h-9 items-center justify-center gap-1.5 rounded-lg border border-neutral-200 text-xs font-semibold text-neutral-600 dark:border-neutral-700 dark:text-neutral-300 sm:col-span-1"
            >
              <Upload size={14} /> Upload .pptx
            </button>
          </div>
          {templateStatus && <p className="text-[11px] font-medium text-emerald-600 dark:text-emerald-400">{templateStatus}</p>}
        </div>
      )}
    </div>
  )
}
