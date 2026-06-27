'use client'

import DOMPurify from 'dompurify'
import { marked } from 'marked'
import { CheckCircle2, Download, Sparkles } from 'lucide-react'
import { useEffect, useMemo, useRef } from 'react'
import { assistantTurnCopyText, buildConfidenceCues, plainCommentary } from '../lib/commentary'
import type { Artifact, FollowUpOption, ProgressEvent, WorkItem } from '../types'
import { CopyButton } from './ui/CopyButton'
import { MarkdownResult } from './MarkdownResult'
import { ResearchPlanCard } from './ResearchPlanCard'

// Fades in each newly-arrived chunk. Committed text renders as plain text nodes so
// there is no re-animation or DOM churn on previously-rendered content.
function StreamingText({ text }: { text: string }) {
  const prevLengthRef = useRef(0)
  const committed = text.slice(0, prevLengthRef.current)
  const incoming = text.slice(prevLengthRef.current)
  useEffect(() => { prevLengthRef.current = text.length })
  return (
    <>
      {committed}
      {incoming && <span key={text.length} className="av3-stream-in">{incoming}</span>}
    </>
  )
}

// Paragraph-split streaming markdown renderer.
//
// Strategy: split the accumulated answer at blank lines (\n\n).
//   • Completed paragraphs  → parsed as full markdown via marked + DOMPurify.
//     useMemo only re-runs when a NEW paragraph boundary arrives, so marked.parse
//     is not called on every tick — only at paragraph transitions.
//   • Active paragraph (last, still streaming) → plain StreamingText with fade-in.
//     Partial markdown syntax (**bold, # header) never flickers because it stays
//     as plain text until the paragraph is complete.
//
// Result: formatted output for everything the user has read, smooth animated text
// for what's currently arriving, and no raw markdown characters visible.
function StreamingMarkdown({ text }: { text: string }) {
  const parts = text.split(/\n\n/)
  const completedText = parts.slice(0, -1).join('\n\n')
  const activeText = parts.at(-1) ?? ''

  const completedHtml = useMemo(
    () => completedText ? DOMPurify.sanitize(marked.parse(completedText) as string) : '',
    [completedText],
  )

  return (
    <div className="av3-markdown">
      {completedHtml && <div dangerouslySetInnerHTML={{ __html: completedHtml }} />}
      {activeText && (
        <p className="whitespace-pre-wrap text-[15px] leading-relaxed [overflow-wrap:anywhere]">
          <StreamingText text={activeText} />
        </p>
      )}
    </div>
  )
}

export function Timeline({
  draftMessage,
  liveAnswer,
  turns,
  events,
  running,
  copiedKey,
  onCopyText,
  downloadArtifact,
  onFollowUp,
}: {
  draftMessage: string
  liveAnswer: string
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
      {running && <LiveTurn message={draftMessage} answer={liveAnswer} events={events} copiedKey={copiedKey} onCopyText={onCopyText} />}
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
  answer,
  events,
  copiedKey,
  onCopyText,
}: {
  message: string
  answer: string
  events: ProgressEvent[]
  copiedKey: string | null
  onCopyText: (value: string, key: string) => void | Promise<void>
}) {
  const commentary = plainCommentary(events)
  const latestMessage = commentary.at(-1) || 'I’m getting oriented and deciding the best way to handle this.'

  return (
    <div className="flex flex-col gap-2.5">
      <div className="self-end max-w-[min(88%,860px)] rounded-2xl rounded-br-md bg-neutral-900 px-4 py-3 text-white dark:bg-white dark:text-neutral-900">
        <div className="mb-1.5 flex items-center justify-between gap-3">
          <p className="text-[11px] font-bold uppercase tracking-wide text-white/55 dark:text-neutral-500">You</p>
          <CopyButton tone="on-inverted-bubble" copied={copiedKey === 'live:user'} label="Copy your message" onClick={() => onCopyText(message, 'live:user')} />
        </div>
        <p className="whitespace-pre-wrap text-[15px] leading-relaxed [overflow-wrap:anywhere]">{message}</p>
      </div>

      {answer ? (
        <div className="w-full max-w-[860px] rounded-2xl rounded-bl-md border border-neutral-200 bg-white p-4 shadow-sm dark:border-neutral-800 dark:bg-neutral-900">
          <div className="mb-3.5 flex items-start gap-3">
            <span className="grid h-9 w-9 flex-shrink-0 place-items-center rounded-full bg-neutral-900 text-white dark:bg-white dark:text-neutral-900">
              <Sparkles size={16} />
            </span>
            <div className="min-w-0 flex-1">
              <p className="text-sm font-bold text-neutral-900 dark:text-neutral-50">Fronei</p>
              <p className="mt-0.5 text-sm leading-relaxed text-neutral-500 dark:text-neutral-400">Writing the response…</p>
            </div>
            <CopyButton copied={copiedKey === 'live:assistant'} label="Copy current response" onClick={() => onCopyText(answer, 'live:assistant')} />
          </div>
          {/* StreamingMarkdown: completed paragraphs render as formatted markdown
              (marked.parse only re-runs at \\n\\n boundaries, not per-tick).
              Active paragraph streams as plain text with fade-in to avoid raw syntax flicker. */}
          <StreamingMarkdown text={answer} />
        </div>
      ) : (
      <div className="w-full max-w-[860px] rounded-2xl rounded-bl-md border border-neutral-200 bg-white p-4 shadow-sm dark:border-neutral-800 dark:bg-neutral-900">
        <div className="mb-3.5 flex items-start gap-3">
          <span className="av3-pulse-ring grid h-9 w-9 flex-shrink-0 place-items-center rounded-full bg-neutral-900 text-white dark:bg-white dark:text-neutral-900">
            <Sparkles size={16} />
          </span>
          <div className="min-w-0 flex-1">
            <p className="text-sm font-bold text-neutral-900 dark:text-neutral-50">Fronei</p>
            <p className="mt-0.5 text-sm leading-relaxed text-neutral-500 dark:text-neutral-400">{latestMessage}</p>
          </div>
          <CopyButton copied={copiedKey === 'live:assistant'} label="Copy current status" onClick={() => onCopyText(latestMessage, 'live:assistant')} />
        </div>

        <div aria-label="Fronei is actively working" className="av3-pulse-bars relative mb-4 ml-12 grid max-w-[180px] grid-cols-3 gap-1.5">
          <span className="h-1 rounded-full bg-emerald-500/70" />
          <span className="h-1 rounded-full bg-emerald-500/70" />
          <span className="h-1 rounded-full bg-emerald-500/70" />
        </div>
      </div>
      )}
    </div>
  )
}
