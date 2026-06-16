'use client'

import DOMPurify from 'dompurify'
import hljs from 'highlight.js'
import { marked } from 'marked'
import { markedHighlight } from 'marked-highlight'
import { IconCheck, IconCopy } from '@tabler/icons-react'
import { useEffect, useMemo, useRef, useState } from 'react'
import { Button } from './ui/button'
import { cn } from '@/v2/lib/utils'

marked.use(markedHighlight({
  langPrefix: 'hljs language-',
  highlight(code, lang) {
    const language = hljs.getLanguage(lang) ? lang : 'plaintext'
    return hljs.highlight(code, { language }).value
  },
}))

export type Message = {
  id: string
  role: 'user' | 'assistant'
  content: string
  created_at: string
  total_cost_usd?: number
}

interface MessageThreadProps {
  messages: Message[]
  isLoading: boolean
}

export function MessageThread({ messages, isLoading }: MessageThreadProps) {
  const bottomRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' })
  }, [messages, isLoading])

  return (
    <div className="min-h-0 flex-1 overflow-y-auto px-4 py-6">
      <div className="mx-auto flex w-full max-w-3xl flex-col gap-5">
        {messages.length === 0 && !isLoading && (
          <div className="flex min-h-[55vh] flex-col items-center justify-center text-center">
            <h1 className="text-2xl font-semibold tracking-normal">What are we working through?</h1>
            <p className="mt-2 max-w-md text-sm text-muted-foreground">Start a new Fronei session or open a recent conversation.</p>
          </div>
        )}
        {messages.map(message => (
          <MessageBubble key={message.id} message={message} />
        ))}
        {isLoading && <TypingIndicator />}
        <div ref={bottomRef} />
      </div>
    </div>
  )
}

function MessageBubble({ message }: { message: Message }) {
  const isUser = message.role === 'user'

  return (
    <article className={cn('flex w-full', isUser ? 'justify-end' : 'justify-start')}>
      <div
        className={cn(
          'max-w-[85%] rounded-lg px-4 py-3 text-sm leading-6 shadow-sm',
          isUser
            ? 'border border-[var(--user-bd)] bg-[var(--user-bg)] text-foreground'
            : 'border border-border bg-card text-card-foreground',
        )}
      >
        {isUser ? (
          <div className="whitespace-pre-wrap">{message.content}</div>
        ) : (
          <AssistantMarkdown content={message.content} />
        )}
      </div>
    </article>
  )
}

function AssistantMarkdown({ content }: { content: string }) {
  const containerRef = useRef<HTMLDivElement | null>(null)
  const [copiedIndex, setCopiedIndex] = useState<number | null>(null)
  const [codeTops, setCodeTops] = useState<number[]>([])
  const html = useMemo(() => DOMPurify.sanitize(marked.parse(content || '') as string), [content])

  useEffect(() => {
    if (!containerRef.current) return
    const preNodes = Array.from(containerRef.current.querySelectorAll('pre'))
    preNodes.forEach((pre, index) => {
      pre.setAttribute('data-code-index', String(index))
    })
    setCodeTops(preNodes.map(pre => pre.offsetTop + 8))
  }, [html])

  async function copyCode(index: number) {
    const code = containerRef.current?.querySelector(`pre[data-code-index="${index}"] code`)?.textContent || ''
    await navigator.clipboard.writeText(code)
    setCopiedIndex(index)
    window.setTimeout(() => setCopiedIndex(null), 1600)
  }

  return (
    <div className="relative">
      <div
        ref={containerRef}
        className="max-w-none [&_a]:text-primary [&_a]:underline [&_code]:rounded [&_code]:bg-[var(--code-bg)] [&_code]:px-1 [&_code]:py-0.5 [&_code]:text-[0.86em] [&_li]:my-1 [&_ol]:my-2 [&_ol]:list-decimal [&_ol]:pl-5 [&_p]:my-2 [&_pre]:my-3 [&_pre]:overflow-x-auto [&_pre]:rounded-md [&_pre]:border [&_pre]:border-[var(--code-bd)] [&_pre]:bg-[var(--pre-bg)] [&_pre]:p-4 [&_pre_code]:bg-transparent [&_pre_code]:p-0 [&_ul]:my-2 [&_ul]:list-disc [&_ul]:pl-5"
        dangerouslySetInnerHTML={{ __html: html }}
      />
      {codeTops.map((top, index) => (
        <Button
          key={index}
          variant="ghost"
          size="icon"
          type="button"
          className="absolute right-2 h-7 w-7 bg-background/75"
          style={{ top }}
          aria-label="Copy code"
          onClick={() => copyCode(index)}
        >
          {copiedIndex === index ? <IconCheck className="h-4 w-4" /> : <IconCopy className="h-4 w-4" />}
        </Button>
      ))}
    </div>
  )
}

function TypingIndicator() {
  return (
    <div className="flex justify-start">
      <div className="flex items-center gap-1 rounded-lg border border-border bg-card px-4 py-3">
        {[0, 1, 2].map(i => (
          <span
            key={i}
            className="h-2 w-2 animate-bounce rounded-full bg-muted-foreground"
            style={{ animationDelay: `${i * 120}ms` }}
          />
        ))}
      </div>
    </div>
  )
}
