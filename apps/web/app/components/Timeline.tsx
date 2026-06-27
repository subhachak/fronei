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

// Simple streaming text for code blocks, where raw text is the intended display.
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

function InlineMarkdown({ text }: { text: string }) {
  const html = useMemo(
    () => DOMPurify.sanitize(marked.parseInline(text) as string),
    [text],
  )
  return <span dangerouslySetInnerHTML={{ __html: html }} />
}

function StreamCursor() {
  return <span className="av3-stream-cursor" aria-hidden="true" />
}

// Block-aware live paragraph renderer.
//
// Markdown has two categories of formatting:
//   • Block-level (headers, lists, code fences, blockquotes) — detectable from the
//     FIRST characters of a line. Rendering them immediately prevents the jarring
//     "raw text → formatted HTML" flip that happens when a paragraph completes.
//   • Inline-level (bold, italic, code spans, links) — parsed on the live text too,
//     so completed inline markup does not wait for the paragraph to settle.
function LiveParagraph({ text, live }: { text: string; live: boolean }) {
  const lines = text.split('\n')
  const firstLine = lines[0]

  // ── Code fence ─────────────────────────────────────────────────────────────
  if (/^```/.test(firstLine)) {
    // Preserve everything after the opening fence; strip closing ``` if present.
    const body = lines.slice(1).join('\n').replace(/```\s*$/, '')
    return (
      <pre className="max-w-full overflow-x-auto rounded-lg bg-neutral-950 p-4 text-neutral-50 text-sm leading-relaxed font-mono [overflow-wrap:anywhere]">
        <code><StreamingText text={body} />{live && <StreamCursor />}</code>
      </pre>
    )
  }

  // ── Unordered list ──────────────────────────────────────────────────────────
  if (/^[-*+] /.test(firstLine)) {
    const items = lines.map(l => l.replace(/^[-*+] /, ''))
    return (
      <ul className="grid gap-1.5 pl-5 list-disc text-[15px] leading-relaxed [overflow-wrap:anywhere]">
        {items.slice(0, -1).map((item, i) => <li key={i}><InlineMarkdown text={item} /></li>)}
        {items.at(-1) !== undefined && (
          <li><InlineMarkdown text={items.at(-1)!} />{live && <StreamCursor />}</li>
        )}
      </ul>
    )
  }

  // ── Ordered list ────────────────────────────────────────────────────────────
  if (/^\d+\. /.test(firstLine)) {
    const items = lines.map(l => l.replace(/^\d+\. /, ''))
    return (
      <ol className="grid gap-1.5 pl-5 list-decimal text-[15px] leading-relaxed [overflow-wrap:anywhere]">
        {items.slice(0, -1).map((item, i) => <li key={i}><InlineMarkdown text={item} /></li>)}
        {items.at(-1) !== undefined && (
          <li><InlineMarkdown text={items.at(-1)!} />{live && <StreamCursor />}</li>
        )}
      </ol>
    )
  }

  // ── Blockquote ──────────────────────────────────────────────────────────────
  if (/^> /.test(firstLine)) {
    const content = lines.map(l => l.replace(/^> ?/, '')).join('\n')
    return (
      <blockquote className="border-l-[3px] border-neutral-300 pl-3 text-neutral-500 text-[15px] leading-relaxed dark:border-neutral-600 dark:text-neutral-400">
        <InlineMarkdown text={content} />{live && <StreamCursor />}
      </blockquote>
    )
  }

  // ── ATX headers (#, ##, …, ######) ─────────────────────────────────────────
  const hMatch = firstLine.match(/^(#{1,6}) (.*)/)
  if (hMatch) {
    const level = hMatch[1].length
    const headingClasses: Record<number, string> = {
      1: 'text-2xl font-bold leading-tight [overflow-wrap:anywhere]',
      2: 'text-xl font-bold leading-tight [overflow-wrap:anywhere]',
      3: 'text-base font-bold leading-tight [overflow-wrap:anywhere]',
      4: 'text-sm font-bold leading-tight [overflow-wrap:anywhere]',
      5: 'text-sm font-semibold leading-tight [overflow-wrap:anywhere]',
      6: 'text-sm font-medium leading-tight [overflow-wrap:anywhere]',
    }
    const cls = headingClasses[level]
    // Trailing lines after the heading (rare but possible) stream as text beneath.
    const tail = lines.slice(1).join('\n')
    return (
      <>
        {level === 1 && <h1 className={cls}><InlineMarkdown text={hMatch[2]} />{live && !tail && <StreamCursor />}</h1>}
        {level === 2 && <h2 className={cls}><InlineMarkdown text={hMatch[2]} />{live && !tail && <StreamCursor />}</h2>}
        {level === 3 && <h3 className={cls}><InlineMarkdown text={hMatch[2]} />{live && !tail && <StreamCursor />}</h3>}
        {level === 4 && <h4 className={cls}><InlineMarkdown text={hMatch[2]} />{live && !tail && <StreamCursor />}</h4>}
        {level === 5 && <h5 className={cls}><InlineMarkdown text={hMatch[2]} />{live && !tail && <StreamCursor />}</h5>}
        {level === 6 && <h6 className={cls}><InlineMarkdown text={hMatch[2]} />{live && !tail && <StreamCursor />}</h6>}
        {tail && (
          <p className="whitespace-pre-wrap text-[15px] leading-relaxed [overflow-wrap:anywhere]">
            <InlineMarkdown text={tail} />{live && <StreamCursor />}
          </p>
        )}
      </>
    )
  }

  // ── Default paragraph ────────────────────────────────────────────────────
  return (
    <p className="whitespace-pre-wrap text-[15px] leading-relaxed [overflow-wrap:anywhere]">
      <InlineMarkdown text={text} />{live && <StreamCursor />}
    </p>
  )
}

// Two-zone streaming renderer:
//   SETTLED zone — full marked.parse, memoized at paragraph boundaries (not per-tick).
//   LIVE zone    — LiveParagraph with structural block detection and inline markdown.
function StreamingMarkdown({ text, live = false }: { text: string; live?: boolean }) {
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
      {activeText && <LiveParagraph text={activeText} live={live} />}
      {live && !activeText && <p className="whitespace-pre-wrap text-[15px] leading-relaxed"><StreamCursor /></p>}
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
          <StreamingMarkdown text={answer} live />
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
