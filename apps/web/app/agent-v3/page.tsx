'use client'

import { useAuth } from '@clerk/nextjs'
import { useMemo, useState } from 'react'

const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL || 'http://localhost:8000'

type ProgressEvent = {
  stage: string
  message: string
  data?: Record<string, unknown>
  created_at?: string
}

type Artifact = {
  filename: string
  mime_type: string
  base64_data: string
}

type AgentResult = {
  turn_id: string
  answer: string
  route: string
  model_used?: string
  latency_ms?: number
  sources?: Array<{ title?: string; url?: string; snippet?: string }>
  artifacts?: Artifact[]
}

export default function AgentV3Page() {
  const { getToken, isLoaded, isSignedIn } = useAuth()
  const [message, setMessage] = useState('Research the latest enterprise AI governance trends and create a concise report.')
  const [qualityMode, setQualityMode] = useState<'draft' | 'standard' | 'executive'>('standard')
  const [outputFormat, setOutputFormat] = useState<'chat' | 'markdown' | 'docx'>('docx')
  const [events, setEvents] = useState<ProgressEvent[]>([])
  const [result, setResult] = useState<AgentResult | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [running, setRunning] = useState(false)

  const canRun = useMemo(() => isLoaded && isSignedIn && message.trim().length > 0 && !running, [isLoaded, isSignedIn, message, running])

  async function run() {
    if (!canRun) return
    setEvents([])
    setResult(null)
    setError(null)
    setRunning(true)
    try {
      const token = await getToken()
      const response = await fetch(`${API_BASE}/agent-v3/turns/stream`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
        },
        body: JSON.stringify({
          message,
          quality_mode: qualityMode,
          output_format: outputFormat,
        }),
      })
      if (!response.ok || !response.body) {
        const body = await response.text()
        throw new Error(body || 'Agent v3 request failed')
      }
      const reader = response.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''
      while (true) {
        const { value, done } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        const frames = buffer.split('\n\n')
        buffer = frames.pop() || ''
        for (const frame of frames) {
          handleFrame(frame)
        }
      }
      if (buffer.trim()) handleFrame(buffer)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unknown Agent v3 error')
    } finally {
      setRunning(false)
    }
  }

  function handleFrame(frame: string) {
    const eventLine = frame.split('\n').find(line => line.startsWith('event: '))
    const dataLine = frame.split('\n').find(line => line.startsWith('data: '))
    const eventType = eventLine?.replace('event: ', '').trim()
    const data = dataLine ? JSON.parse(dataLine.replace('data: ', '')) : {}
    if (eventType === 'progress') {
      setEvents(prev => [...prev, data as ProgressEvent])
    } else if (eventType === 'result') {
      setResult(data as AgentResult)
    } else if (eventType === 'error') {
      setError(data.message || 'Agent v3 failed')
    }
  }

  function downloadArtifact(artifact: Artifact) {
    const byteString = atob(artifact.base64_data)
    const bytes = new Uint8Array(byteString.length)
    for (let i = 0; i < byteString.length; i += 1) bytes[i] = byteString.charCodeAt(i)
    const blob = new Blob([bytes], { type: artifact.mime_type })
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = artifact.filename
    link.click()
    URL.revokeObjectURL(url)
  }

  function eventChips(event: ProgressEvent): string[] {
    const data = event.data || {}
    const chips: string[] = []
    for (const key of ['provider', 'tool_name', 'status', 'route', 'source_count', 'worker_index', 'filename']) {
      const value = data[key]
      if (value !== undefined && value !== null && value !== '') chips.push(`${key.replace('_', ' ')}: ${String(value)}`)
    }
    return chips
  }

  return (
    <main className="min-h-screen bg-[#f7f4ed] text-[#17213a]">
      <section className="mx-auto flex w-full max-w-6xl flex-col gap-6 px-6 py-10">
        <header className="flex flex-col gap-2 border-b border-[#d8d2c5] pb-6">
          <p className="text-xs font-semibold uppercase tracking-[0.18em] text-[#7c879a]">Fresh Runtime</p>
          <h1 className="text-3xl font-semibold">Agent v3 isolated lab</h1>
          <p className="max-w-3xl text-sm leading-6 text-[#536071]">
            This page talks only to the new /agent-v3 runtime. It does not use the legacy chat pipeline,
            hybrid turn graph, old research orchestrator, or old document generator.
          </p>
        </header>

        <div className="grid gap-6 lg:grid-cols-[minmax(0,0.95fr)_minmax(0,1.05fr)]">
          <section className="flex flex-col gap-4 rounded border border-[#d8d2c5] bg-white p-5">
            <label className="text-sm font-semibold" htmlFor="agent-v3-message">Prompt</label>
            <textarea
              id="agent-v3-message"
              value={message}
              onChange={event => setMessage(event.target.value)}
              className="min-h-48 resize-y rounded border border-[#ccd3df] p-3 text-sm leading-6 outline-none focus:border-[#4169e1]"
            />
            <div className="grid gap-3 sm:grid-cols-2">
              <label className="flex flex-col gap-2 text-sm font-semibold">
                Quality
                <select
                  value={qualityMode}
                  onChange={event => setQualityMode(event.target.value as typeof qualityMode)}
                  className="rounded border border-[#ccd3df] bg-white p-2 font-normal"
                >
                  <option value="draft">Draft</option>
                  <option value="standard">Standard</option>
                  <option value="executive">Executive</option>
                </select>
              </label>
              <label className="flex flex-col gap-2 text-sm font-semibold">
                Output
                <select
                  value={outputFormat}
                  onChange={event => setOutputFormat(event.target.value as typeof outputFormat)}
                  className="rounded border border-[#ccd3df] bg-white p-2 font-normal"
                >
                  <option value="chat">Chat</option>
                  <option value="markdown">Markdown</option>
                  <option value="docx">DOCX</option>
                </select>
              </label>
            </div>
            <button
              type="button"
              onClick={run}
              disabled={!canRun}
              className="rounded bg-[#17213a] px-4 py-3 text-sm font-semibold text-white disabled:cursor-not-allowed disabled:bg-[#9aa5b5]"
            >
              {running ? 'Running Agent v3...' : 'Run fresh runtime'}
            </button>
            {error && <p className="rounded border border-[#f3b7b7] bg-[#fff1f1] p-3 text-sm text-[#9f1d1d]">{error}</p>}
          </section>

          <section className="flex flex-col gap-4 rounded border border-[#d8d2c5] bg-white p-5">
            <h2 className="text-lg font-semibold">Live trace</h2>
            <div className="flex min-h-56 flex-col gap-3">
              {events.length === 0 && <p className="text-sm text-[#7c879a]">No v3 events yet.</p>}
              {events.map((event, index) => (
                <div key={`${event.stage}-${index}`} className="rounded border border-[#e2e7ef] p-3">
                  <p className="text-xs font-semibold uppercase tracking-[0.12em] text-[#7c879a]">{event.stage}</p>
                  <p className="mt-1 text-sm">{event.message}</p>
                  {eventChips(event).length ? (
                    <div className="mt-2 flex flex-wrap gap-2">
                      {eventChips(event).map(chip => (
                        <span key={chip} className="rounded border border-[#d8d2c5] bg-[#f7f4ed] px-2 py-1 text-[11px] font-medium text-[#536071]">
                          {chip}
                        </span>
                      ))}
                    </div>
                  ) : null}
                </div>
              ))}
            </div>

            {result && (
              <div className="flex flex-col gap-4 border-t border-[#e2e7ef] pt-4">
                <div>
                  <p className="text-xs font-semibold uppercase tracking-[0.12em] text-[#7c879a]">
                    {result.route} · {result.latency_ms ?? 0}ms · {result.model_used || 'model unavailable'}
                  </p>
                  <p className="mt-2 whitespace-pre-wrap text-sm leading-6">{result.answer}</p>
                </div>
                {result.artifacts?.length ? (
                  <div className="flex flex-wrap gap-2">
                    {result.artifacts.map(artifact => (
                      <button
                        key={artifact.filename}
                        type="button"
                        onClick={() => downloadArtifact(artifact)}
                        className="rounded border border-[#17213a] px-3 py-2 text-sm font-semibold"
                      >
                        Download {artifact.filename}
                      </button>
                    ))}
                  </div>
                ) : null}
                {result.sources?.length ? (
                  <div className="flex flex-col gap-2">
                    <h3 className="text-sm font-semibold">Sources</h3>
                    {result.sources.map((source, index) => (
                      <a key={`${source.url}-${index}`} href={source.url} target="_blank" className="text-sm text-[#2352c4] underline">
                        {source.title || source.url}
                      </a>
                    ))}
                  </div>
                ) : null}
              </div>
            )}
          </section>
        </div>
      </section>
    </main>
  )
}
