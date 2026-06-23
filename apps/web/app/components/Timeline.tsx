'use client'

import { CheckCircle2, Download, Sparkles } from 'lucide-react'
import { assistantTurnCopyText, buildConfidenceCues, plainCommentary } from '../lib/commentary'
import type { Artifact, FollowUpOption, ProgressEvent, WorkItem } from '../types'
import { CopyButton } from './ui/CopyButton'
import { MarkdownResult } from './MarkdownResult'
import { ResearchPlanCard } from './ResearchPlanCard'

export function Timeline({
  draftMessage,
  turns,
  events,
  running,
  copiedKey,
  onCopyText,
  downloadArtifact,
  onFollowUp,
}: {
  draftMessage: string
  turns: WorkItem[]
  events: ProgressEvent[]
  running: boolean
  copiedKey: string | null
  onCopyText: (value: string, key: string) => void | Promise<void>
  downloadArtifact: (artifact: Artifact) => void | Promise<void>
  onFollowUp: (option: FollowUpOption) => void
}) {
  if (turns.length === 0 && !running) {
    return (
      <div className="w-full max-w-[860px] rounded-2xl rounded-bl-md border border-neutral-200 bg-white p-4 shadow-sm dark:border-neutral-800 dark:bg-neutral-900">
        <div className="mb-2 flex items-start gap-3">
          <span className="grid h-9 w-9 flex-shrink-0 place-items-center rounded-full bg-neutral-900 text-white dark:bg-white dark:text-neutral-900">
            <Sparkles size={16} />
          </span>
          <div>
            <p className="text-sm font-bold text-neutral-900 dark:text-neutral-50">Fronei</p>
            <p className="mt-0.5 text-sm leading-relaxed text-neutral-500 dark:text-neutral-400">Start a task and I will keep the work visible here.</p>
          </div>
        </div>
        <p className="text-sm text-neutral-400 dark:text-neutral-500">This conversation is empty.</p>
      </div>
    )
  }

  return (
    <div className="flex flex-1 flex-col gap-6">
      {turns.map(turn => (
        <TurnPair key={turn.id} turn={turn} downloadArtifact={downloadArtifact} onFollowUp={onFollowUp} copiedKey={copiedKey} onCopyText={onCopyText} />
      ))}
      {running && <LiveTurn message={draftMessage} events={events} copiedKey={copiedKey} onCopyText={onCopyText} />}
    </div>
  )
}

function TurnPair({
  turn,
  downloadArtifact,
  onFollowUp,
  copiedKey,
  onCopyText,
}: {
  turn: WorkItem
  downloadArtifact: (artifact: Artifact) => void | Promise<void>
  onFollowUp?: (option: FollowUpOption) => void
  copiedKey: string | null
  onCopyText: (value: string, key: string) => void | Promise<void>
}) {
  const userCopy = turn.message || turn.title
  const assistantCopy = assistantTurnCopyText(turn)
  const confidenceCues = buildConfidenceCues(turn.events || [], turn.result || null)
  return (
    <div className="flex flex-col gap-2.5">
      <div className="self-end max-w-[min(88%,860px)] rounded-2xl rounded-br-md bg-neutral-900 px-4 py-3 text-white dark:bg-white dark:text-neutral-900">
        <div className="mb-1.5 flex items-center justify-between gap-3">
          <p className="text-[11px] font-bold uppercase tracking-wide text-white/55 dark:text-neutral-500">You</p>
          <CopyButton tone="on-inverted-bubble" copied={copiedKey === `${turn.id}:user`} label="Copy your message" onClick={() => onCopyText(userCopy, `${turn.id}:user`)} />
        </div>
        <p className="whitespace-pre-wrap text-[15px] leading-relaxed [overflow-wrap:anywhere]">{turn.message || turn.title}</p>
      </div>

      <div className="w-full max-w-[860px] rounded-2xl rounded-bl-md border border-neutral-200 bg-white p-4 shadow-sm dark:border-neutral-800 dark:bg-neutral-900">
        <div className="mb-3.5 flex items-start gap-3">
          <span className="grid h-9 w-9 flex-shrink-0 place-items-center rounded-full bg-neutral-900 text-white dark:bg-white dark:text-neutral-900">
            <Sparkles size={16} />
          </span>
          <div className="min-w-0 flex-1">
            <p className="text-sm font-bold text-neutral-900 dark:text-neutral-50">Fronei</p>
            <p className="mt-0.5 text-sm leading-relaxed text-neutral-500 dark:text-neutral-400">Completed as {turn.route}.</p>
          </div>
          <CopyButton copied={copiedKey === `${turn.id}:assistant`} label="Copy Fronei response" onClick={() => onCopyText(assistantCopy, `${turn.id}:assistant`)} />
        </div>

        {confidenceCues.length > 0 && (
          <div className="mb-3.5 grid gap-1.5 sm:grid-cols-2">
            {confidenceCues.map(cue => (
              <div key={cue} className="flex items-center gap-2 rounded-lg bg-emerald-50 px-3 py-2 text-[13px] font-medium text-emerald-800 dark:bg-emerald-500/10 dark:text-emerald-400">
                <CheckCircle2 size={15} className="flex-shrink-0" />
                {cue}
              </div>
            ))}
          </div>
        )}

        {turn.result?.research_plan_preview ? (
          <ResearchPlanCard preview={turn.result.research_plan_preview} followUpOptions={turn.result.follow_up_options || []} onFollowUp={onFollowUp} />
        ) : (
          <MarkdownResult content={turn.result?.answer || ''} />
        )}

        {!!turn.result?.follow_up_options?.length && onFollowUp && !turn.result?.research_plan_preview && (
          <div className="mt-3.5 flex flex-wrap gap-2">
            {turn.result.follow_up_options.map((option, index) => (
              <button
                key={option.label}
                type="button"
                onClick={() => onFollowUp(option)}
                className={
                  index === 0
                    ? 'rounded-lg bg-neutral-900 px-3 py-2 text-sm font-semibold text-white dark:bg-white dark:text-neutral-900'
                    : 'rounded-lg border border-neutral-200 bg-white px-3 py-2 text-sm font-semibold text-neutral-900 dark:border-neutral-700 dark:bg-neutral-900 dark:text-neutral-50'
                }
              >
                {option.label}
              </button>
            ))}
          </div>
        )}

        {turn.artifacts.length > 0 && (
          <div className="mt-3.5 flex flex-wrap gap-2">
            {turn.artifacts.map(artifact => (
              <button
                key={artifact.filename}
                type="button"
                onClick={() => downloadArtifact(artifact)}
                className="inline-flex items-center gap-2 rounded-lg bg-neutral-900 px-3 py-2 text-sm font-semibold text-white dark:bg-white dark:text-neutral-900"
              >
                <Download size={15} /> {artifact.filename}
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

function LiveTurn({
  message,
  events,
  copiedKey,
  onCopyText,
}: {
  message: string
  events: ProgressEvent[]
  copiedKey: string | null
  onCopyText: (value: string, key: string) => void | Promise<void>
}) {
  const commentary = plainCommentary(events)
  const latestMessage = commentary.at(-1) || 'I’m getting oriented and deciding the best way to handle this.'
  const liveCopy = commentary.join('\n') || latestMessage

  return (
    <div className="flex flex-col gap-2.5">
      <div className="self-end max-w-[min(88%,860px)] rounded-2xl rounded-br-md bg-neutral-900 px-4 py-3 text-white dark:bg-white dark:text-neutral-900">
        <div className="mb-1.5 flex items-center justify-between gap-3">
          <p className="text-[11px] font-bold uppercase tracking-wide text-white/55 dark:text-neutral-500">You</p>
          <CopyButton tone="on-inverted-bubble" copied={copiedKey === 'live:user'} label="Copy your message" onClick={() => onCopyText(message, 'live:user')} />
        </div>
        <p className="whitespace-pre-wrap text-[15px] leading-relaxed [overflow-wrap:anywhere]">{message}</p>
      </div>

      <div className="w-full max-w-[860px] rounded-2xl rounded-bl-md border border-neutral-200 bg-white p-4 shadow-sm dark:border-neutral-800 dark:bg-neutral-900">
        <div className="mb-3.5 flex items-start gap-3">
          <span className="av3-pulse-ring grid h-9 w-9 flex-shrink-0 place-items-center rounded-full bg-neutral-900 text-white dark:bg-white dark:text-neutral-900">
            <Sparkles size={16} />
          </span>
          <div className="min-w-0 flex-1">
            <p className="text-sm font-bold text-neutral-900 dark:text-neutral-50">Fronei</p>
            <p className="mt-0.5 text-sm leading-relaxed text-neutral-500 dark:text-neutral-400">{latestMessage}</p>
          </div>
          <CopyButton copied={copiedKey === 'live:assistant'} label="Copy current commentary" onClick={() => onCopyText(liveCopy, 'live:assistant')} />
        </div>

        <div aria-label="Fronei is actively working" className="av3-pulse-bars relative mb-4 ml-12 grid max-w-[180px] grid-cols-3 gap-1.5">
          <span className="h-1 rounded-full bg-emerald-500/70" />
          <span className="h-1 rounded-full bg-emerald-500/70" />
          <span className="h-1 rounded-full bg-emerald-500/70" />
        </div>

        <RollingCommentary events={events} />
      </div>
    </div>
  )
}

function RollingCommentary({ events }: { events: ProgressEvent[] }) {
  const visibleEvents = plainCommentary(events).slice(-6)
  const items = visibleEvents.length === 0 ? ['I’m getting oriented and deciding the best way to handle this.'] : visibleEvents

  return (
    <div className="ml-[17px] grid gap-3 border-l-2 border-neutral-200 pl-[18px] dark:border-neutral-700">
      {items.map((text, index) => {
        const isActive = index === items.length - 1
        return (
          <div key={`${text}-${index}`} className="grid grid-cols-[auto_minmax(0,1fr)] gap-2.5">
            <span className={`mt-[7px] h-[9px] w-[9px] rounded-full bg-emerald-500 ${isActive ? 'av3-pulse-dot' : ''}`} />
            <div>
              <p className="text-sm leading-relaxed text-neutral-600 dark:text-neutral-400">{text}</p>
              {isActive && (
                <span className="av3-ellipsis mt-1.5 inline-flex gap-1" aria-hidden="true">
                  <span className="h-1 w-1 rounded-full bg-emerald-500 opacity-40" />
                  <span className="h-1 w-1 rounded-full bg-emerald-500 opacity-40" />
                  <span className="h-1 w-1 rounded-full bg-emerald-500 opacity-40" />
                </span>
              )}
            </div>
          </div>
        )
      })}
    </div>
  )
}
