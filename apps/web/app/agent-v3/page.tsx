'use client'

import { useAuth } from '@clerk/nextjs'
import {
  Archive,
  ArrowUpRight,
  BookOpen,
  CheckCircle2,
  ChevronDown,
  Clock3,
  Download,
  FileText,
  Library,
  Loader2,
  PanelRight,
  Search,
  Send,
  Sparkles,
} from 'lucide-react'
import type { LucideIcon } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'

const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL || 'http://localhost:8000'
const LIBRARY_KEY = 'fronei-agent-v3-studio-library'

type QualityMode = 'draft' | 'standard' | 'executive'
type OutputFormat = 'chat' | 'markdown' | 'docx'
type MobileView = 'work' | 'library' | 'context'
type MobileNavItem = [MobileView, LucideIcon, string]

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

type Source = {
  title?: string
  url?: string
  snippet?: string
  content?: string
}

type AgentResult = {
  turn_id: string
  answer: string
  route: string
  model_used?: string
  latency_ms?: number
  sources?: Source[]
  artifacts?: Artifact[]
}

type WorkItem = {
  id: string
  title: string
  route: string
  createdAt: string
  artifacts: Artifact[]
  sourceCount: number
}

const SUGGESTIONS = [
  'Research RBI digital lending guidelines and create a concise briefing note.',
  'Compare You.com, Tavily, and Nimble as search providers for source-grounded research.',
  'Create a DOCX report on Agent v3 architecture progress and remaining risks.',
]

export default function AgentV3Page() {
  const { getToken, isLoaded, isSignedIn } = useAuth()
  const [message, setMessage] = useState('Research the latest enterprise AI governance trends and create a concise report.')
  const [qualityMode, setQualityMode] = useState<QualityMode>('standard')
  const [outputFormat, setOutputFormat] = useState<OutputFormat>('docx')
  const [events, setEvents] = useState<ProgressEvent[]>([])
  const [result, setResult] = useState<AgentResult | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [running, setRunning] = useState(false)
  const [library, setLibrary] = useState<WorkItem[]>([])
  const [mobileView, setMobileView] = useState<MobileView>('work')
  const [traceOpen, setTraceOpen] = useState(false)

  const canRun = useMemo(() => isLoaded && isSignedIn && message.trim().length > 0 && !running, [isLoaded, isSignedIn, message, running])
  const activeEvents = useMemo(() => events.filter(event => !['tool_selection', 'tool_result'].includes(event.stage)), [events])
  const confidenceCues = useMemo(() => buildConfidenceCues(events, result), [events, result])
  const latestArtifact = result?.artifacts?.[0] || library.find(item => item.artifacts.length)?.artifacts[0]
  const sources = result?.sources || []

  useEffect(() => {
    try {
      const raw = localStorage.getItem(LIBRARY_KEY)
      if (raw) setLibrary(JSON.parse(raw))
    } catch {}
  }, [])

  useEffect(() => {
    try {
      localStorage.setItem(LIBRARY_KEY, JSON.stringify(library.slice(0, 20)))
    } catch {}
  }, [library])

  async function run() {
    if (!canRun) return
    setEvents([])
    setResult(null)
    setError(null)
    setRunning(true)
    setMobileView('work')
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
        for (const frame of frames) handleFrame(frame)
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
      const next = data as AgentResult
      setResult(next)
      setLibrary(prev => [
        {
          id: next.turn_id,
          title: titleFromMessage(message),
          route: next.route,
          createdAt: new Date().toISOString(),
          artifacts: next.artifacts || [],
          sourceCount: next.sources?.length || 0,
        },
        ...prev.filter(item => item.id !== next.turn_id),
      ].slice(0, 20))
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
    <main className="min-h-screen bg-[#f4f1ea] text-[#162033]">
      <div className="mx-auto flex min-h-screen w-full max-w-[1500px] flex-col lg:grid lg:grid-cols-[280px_minmax(0,1fr)_340px]">
        <MobileTopBar mobileView={mobileView} setMobileView={setMobileView} running={running} />

        <aside className={`${mobileView === 'library' ? 'flex' : 'hidden'} min-h-[calc(100vh-64px)] flex-col border-r border-[#ded6c9] bg-[#fbfaf6] px-4 py-4 lg:flex lg:min-h-screen lg:px-5 lg:py-6`}>
          <StudioLibrary library={library} latestArtifact={latestArtifact} downloadArtifact={downloadArtifact} />
        </aside>

        <section className={`${mobileView === 'work' ? 'flex' : 'hidden'} min-h-[calc(100vh-64px)] flex-col lg:flex lg:min-h-screen`}>
          <WorkbenchHeader running={running} result={result} />
          <div className="flex min-h-0 flex-1 flex-col gap-4 px-4 pb-28 pt-4 sm:px-6 lg:overflow-y-auto lg:px-8 lg:pb-6">
            <Composer
              message={message}
              setMessage={setMessage}
              qualityMode={qualityMode}
              setQualityMode={setQualityMode}
              outputFormat={outputFormat}
              setOutputFormat={setOutputFormat}
              running={running}
              canRun={canRun}
              run={run}
            />
            {error && <div className="border-l-4 border-[#d95b43] bg-[#fff7f3] px-4 py-3 text-sm text-[#7a2e22]">{error}</div>}
            <SuggestionStrip suggestions={SUGGESTIONS} setMessage={setMessage} />
            <Timeline
              events={activeEvents}
              running={running}
              result={result}
              confidenceCues={confidenceCues}
              traceOpen={traceOpen}
              setTraceOpen={setTraceOpen}
              eventChips={eventChips}
              downloadArtifact={downloadArtifact}
            />
          </div>
        </section>

        <aside className={`${mobileView === 'context' ? 'flex' : 'hidden'} min-h-[calc(100vh-64px)] flex-col border-l border-[#ded6c9] bg-[#f8f6f0] px-4 py-4 lg:flex lg:min-h-screen lg:px-5 lg:py-6`}>
          <ContextRail result={result} events={events} sources={sources} latestArtifact={latestArtifact} downloadArtifact={downloadArtifact} />
        </aside>
      </div>
    </main>
  )
}

function MobileTopBar({ mobileView, setMobileView, running }: { mobileView: MobileView; setMobileView: (view: MobileView) => void; running: boolean }) {
  const items: MobileNavItem[] = [
    ['library', Library, 'Library'],
    ['work', Sparkles, 'Workbench'],
    ['context', PanelRight, 'Context'],
  ]

  return (
    <header className="sticky top-0 z-20 border-b border-[#ded6c9] bg-[#fbfaf6]/95 px-3 py-3 backdrop-blur lg:hidden">
      <div className="mb-3 flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span className="grid h-9 w-9 place-items-center rounded-full bg-[#162033] text-white"><Sparkles className="h-4 w-4" /></span>
          <div>
            <p className="text-[11px] font-semibold uppercase text-[#6c766f]">Fronei Studio</p>
            <p className="text-sm font-semibold">Agent v3</p>
          </div>
        </div>
        {running && <span className="flex items-center gap-2 rounded-full bg-[#e5f2ef] px-3 py-1 text-xs font-medium text-[#146152]"><Loader2 className="h-3.5 w-3.5 animate-spin" /> Working</span>}
      </div>
      <nav className="grid grid-cols-3 gap-2 rounded-full bg-[#ebe4d8] p-1">
        {items.map(([id, Icon, label]) => (
          <button
            key={id}
            type="button"
            onClick={() => setMobileView(id)}
            className={`flex items-center justify-center gap-1.5 rounded-full px-2 py-2 text-xs font-semibold ${mobileView === id ? 'bg-[#162033] text-white' : 'text-[#59645f]'}`}
          >
            <Icon className="h-3.5 w-3.5" />
            {label}
          </button>
        ))}
      </nav>
    </header>
  )
}

function StudioLibrary({
  library,
  latestArtifact,
  downloadArtifact,
}: {
  library: WorkItem[]
  latestArtifact?: Artifact
  downloadArtifact: (artifact: Artifact) => void
}) {
  return (
    <>
      <div className="mb-5 flex items-center justify-between">
        <div>
          <p className="text-xs font-semibold uppercase text-[#79827c]">Studio</p>
          <h1 className="text-xl font-semibold">Work library</h1>
        </div>
        <span className="grid h-10 w-10 place-items-center rounded-full bg-[#162033] text-white"><Archive className="h-4 w-4" /></span>
      </div>

      {latestArtifact && (
        <button
          type="button"
          onClick={() => downloadArtifact(latestArtifact)}
          className="mb-5 flex w-full items-center gap-3 border border-[#d8cdbc] bg-[#fffdf8] p-3 text-left"
        >
          <span className="grid h-10 w-10 shrink-0 place-items-center rounded-full bg-[#e9f3ef] text-[#146152]"><Download className="h-4 w-4" /></span>
          <span className="min-w-0 flex-1">
            <span className="block truncate text-sm font-semibold">{latestArtifact.filename}</span>
            <span className="block text-xs text-[#79827c]">Latest artifact</span>
          </span>
        </button>
      )}

      <div className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto">
        {library.length === 0 && (
          <div className="border border-dashed border-[#d8cdbc] px-4 py-6 text-sm text-[#6f7973]">
            Finished work will appear here.
          </div>
        )}
        {library.map(item => (
          <div key={item.id} className="border border-[#ded6c9] bg-white/70 p-3">
            <div className="flex items-start gap-3">
              <span className="mt-0.5 grid h-8 w-8 shrink-0 place-items-center rounded-full bg-[#f1e7d8] text-[#8a5a18]"><FileText className="h-4 w-4" /></span>
              <div className="min-w-0 flex-1">
                <p className="line-clamp-2 text-sm font-semibold leading-5">{item.title}</p>
                <p className="mt-1 text-xs text-[#79827c]">{item.route} | {item.sourceCount} sources</p>
              </div>
            </div>
            {item.artifacts.length > 0 && (
              <button
                type="button"
                onClick={() => downloadArtifact(item.artifacts[0])}
                className="mt-3 flex w-full items-center justify-center gap-2 border border-[#c8bba8] px-3 py-2 text-xs font-semibold text-[#162033]"
              >
                <Download className="h-3.5 w-3.5" />
                Download
              </button>
            )}
          </div>
        ))}
      </div>
    </>
  )
}

function WorkbenchHeader({ running, result }: { running: boolean; result: AgentResult | null }) {
  return (
    <header className="hidden border-b border-[#ded6c9] bg-[#f4f1ea]/95 px-8 py-5 backdrop-blur lg:block">
      <div className="flex items-center justify-between gap-4">
        <div>
          <p className="text-xs font-semibold uppercase text-[#79827c]">Research and work-product studio</p>
          <h2 className="mt-1 text-2xl font-semibold">Workbench</h2>
        </div>
        <div className="flex items-center gap-3">
          {result && <span className="rounded-full bg-[#ebe4d8] px-3 py-1.5 text-xs font-semibold text-[#53615a]">{result.route} | {result.latency_ms ?? 0}ms</span>}
          <span className={`flex items-center gap-2 rounded-full px-3 py-1.5 text-xs font-semibold ${running ? 'bg-[#e5f2ef] text-[#146152]' : 'bg-[#fffdf8] text-[#53615a]'}`}>
            {running ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <CheckCircle2 className="h-3.5 w-3.5" />}
            {running ? 'Working' : 'Ready'}
          </span>
        </div>
      </div>
    </header>
  )
}

function Composer({
  message,
  setMessage,
  qualityMode,
  setQualityMode,
  outputFormat,
  setOutputFormat,
  running,
  canRun,
  run,
}: {
  message: string
  setMessage: (message: string) => void
  qualityMode: QualityMode
  setQualityMode: (mode: QualityMode) => void
  outputFormat: OutputFormat
  setOutputFormat: (format: OutputFormat) => void
  running: boolean
  canRun: boolean
  run: () => void
}) {
  return (
    <section className="border border-[#ded6c9] bg-[#fffdf8] p-3 shadow-[0_24px_70px_rgba(49,39,25,0.08)] sm:p-4">
      <textarea
        value={message}
        onChange={event => setMessage(event.target.value)}
        className="min-h-40 w-full resize-none bg-transparent p-2 text-base leading-7 text-[#162033] outline-none placeholder:text-[#9aa197] sm:min-h-32"
        placeholder="Give Fronei a task..."
      />
      <div className="mt-3 flex flex-col gap-3 border-t border-[#ebe4d8] pt-3 sm:flex-row sm:items-center sm:justify-between">
        <div className="grid grid-cols-2 gap-2 sm:flex">
          <StudioSelect label="Quality" value={qualityMode} onChange={value => setQualityMode(value as QualityMode)} options={['draft', 'standard', 'executive']} />
          <StudioSelect label="Output" value={outputFormat} onChange={value => setOutputFormat(value as OutputFormat)} options={['chat', 'markdown', 'docx']} />
        </div>
        <button
          type="button"
          onClick={run}
          disabled={!canRun}
          className="flex min-h-12 items-center justify-center gap-2 bg-[#162033] px-5 py-3 text-sm font-semibold text-white transition disabled:cursor-not-allowed disabled:bg-[#a8afa7]"
        >
          {running ? <Loader2 className="h-4 w-4 animate-spin" /> : <Send className="h-4 w-4" />}
          {running ? 'Working' : 'Start'}
        </button>
      </div>
    </section>
  )
}

function StudioSelect({ label, value, onChange, options }: { label: string; value: string; onChange: (value: string) => void; options: string[] }) {
  return (
    <label className="flex items-center justify-between gap-2 border border-[#ded6c9] bg-[#fbfaf6] px-3 py-2 text-xs font-semibold text-[#59645f] sm:min-w-36">
      <span>{label}</span>
      <select value={value} onChange={event => onChange(event.target.value)} className="bg-transparent text-right text-xs font-semibold text-[#162033] outline-none">
        {options.map(option => <option key={option} value={option}>{option}</option>)}
      </select>
    </label>
  )
}

function SuggestionStrip({ suggestions, setMessage }: { suggestions: string[]; setMessage: (message: string) => void }) {
  return (
    <div className="flex snap-x gap-2 overflow-x-auto pb-1">
      {suggestions.map(suggestion => (
        <button
          key={suggestion}
          type="button"
          onClick={() => setMessage(suggestion)}
          className="snap-start whitespace-nowrap border border-[#ded6c9] bg-[#fbfaf6] px-3 py-2 text-xs font-medium text-[#53615a]"
        >
          {suggestion}
        </button>
      ))}
    </div>
  )
}

function Timeline({
  events,
  running,
  result,
  confidenceCues,
  traceOpen,
  setTraceOpen,
  eventChips,
  downloadArtifact,
}: {
  events: ProgressEvent[]
  running: boolean
  result: AgentResult | null
  confidenceCues: string[]
  traceOpen: boolean
  setTraceOpen: (open: boolean) => void
  eventChips: (event: ProgressEvent) => string[]
  downloadArtifact: (artifact: Artifact) => void
}) {
  return (
    <section className="flex flex-col gap-4">
      <div className="border-l-2 border-[#d8cdbc] pl-4">
        <div className="mb-4 flex items-start gap-3">
          <span className="mt-1 grid h-9 w-9 shrink-0 place-items-center rounded-full bg-[#162033] text-white"><Sparkles className="h-4 w-4" /></span>
          <div>
            <p className="text-sm font-semibold">Fronei is with you.</p>
            <p className="mt-1 text-sm leading-6 text-[#647069]">
              {running ? 'I am turning the task into grounded work.' : result ? 'The work is ready.' : 'Start a task when you are ready.'}
            </p>
          </div>
        </div>

        {confidenceCues.length > 0 && (
          <div className="mb-4 grid gap-2 sm:grid-cols-2">
            {confidenceCues.map(cue => (
              <div key={cue} className="flex items-center gap-2 bg-[#eaf2ee] px-3 py-2 text-sm text-[#205d51]">
                <CheckCircle2 className="h-4 w-4 shrink-0" />
                {cue}
              </div>
            ))}
          </div>
        )}

        {result && (
          <div className="bg-[#fffdf8] px-4 py-4">
            <p className="mb-2 text-xs font-semibold uppercase text-[#79827c]">Result</p>
            <p className="whitespace-pre-wrap text-sm leading-7 text-[#273348]">{result.answer}</p>
            {result.artifacts?.length ? (
              <div className="mt-4 flex flex-wrap gap-2">
                {result.artifacts.map(artifact => (
                  <button
                    key={artifact.filename}
                    type="button"
                    onClick={() => downloadArtifact(artifact)}
                    className="flex items-center gap-2 bg-[#162033] px-4 py-2 text-sm font-semibold text-white"
                  >
                    <Download className="h-4 w-4" />
                    {artifact.filename}
                  </button>
                ))}
              </div>
            ) : null}
          </div>
        )}
      </div>

      <button
        type="button"
        onClick={() => setTraceOpen(!traceOpen)}
        className="flex items-center justify-between border border-[#ded6c9] bg-[#fbfaf6] px-4 py-3 text-left text-sm font-semibold"
      >
        <span>{events.length ? `${events.length} studio events` : 'Studio trace'}</span>
        <ChevronDown className={`h-4 w-4 transition ${traceOpen ? 'rotate-180' : ''}`} />
      </button>

      {traceOpen && (
        <div className="flex flex-col gap-2">
          {events.length === 0 && <p className="text-sm text-[#79827c]">No events yet.</p>}
          {events.map((event, index) => (
            <div key={`${event.stage}-${index}`} className="border border-[#e1d8ca] bg-white/70 p-3">
              <p className="text-[11px] font-semibold uppercase text-[#79827c]">{event.stage}</p>
              <p className="mt-1 text-sm">{event.message}</p>
              {eventChips(event).length ? (
                <div className="mt-2 flex flex-wrap gap-2">
                  {eventChips(event).map(chip => (
                    <span key={chip} className="bg-[#f1e7d8] px-2 py-1 text-[11px] font-medium text-[#6d5838]">{chip}</span>
                  ))}
                </div>
              ) : null}
            </div>
          ))}
        </div>
      )}
    </section>
  )
}

function ContextRail({
  result,
  events,
  sources,
  latestArtifact,
  downloadArtifact,
}: {
  result: AgentResult | null
  events: ProgressEvent[]
  sources: Source[]
  latestArtifact?: Artifact
  downloadArtifact: (artifact: Artifact) => void
}) {
  const providerEvents = events.filter(event => event.stage === 'search_worker_provider')
  return (
    <>
      <div className="mb-5">
        <p className="text-xs font-semibold uppercase text-[#79827c]">Context</p>
        <h2 className="mt-1 text-xl font-semibold">Current work</h2>
      </div>

      <div className="flex flex-col gap-4 overflow-y-auto">
        <section className="border border-[#ded6c9] bg-[#fffdf8] p-4">
          <div className="mb-3 flex items-center gap-2">
            <Clock3 className="h-4 w-4 text-[#ad7a2a]" />
            <h3 className="text-sm font-semibold">Status</h3>
          </div>
          <p className="text-sm text-[#59645f]">{result ? `Completed as ${result.route}` : events.length ? 'In progress' : 'Waiting'}</p>
          {result?.model_used && <p className="mt-2 text-xs text-[#79827c]">{result.model_used}</p>}
        </section>

        {providerEvents.length > 0 && (
          <section className="border border-[#ded6c9] bg-[#fffdf8] p-4">
            <div className="mb-3 flex items-center gap-2">
              <Search className="h-4 w-4 text-[#146152]" />
              <h3 className="text-sm font-semibold">Search providers</h3>
            </div>
            <div className="flex flex-col gap-2">
              {providerEvents.map((event, index) => (
                <div key={`${event.message}-${index}`} className="flex items-center justify-between gap-3 text-sm">
                  <span className="text-[#59645f]">Worker {String(event.data?.worker_index || index + 1)}</span>
                  <strong>{String(event.data?.provider || 'none')}</strong>
                </div>
              ))}
            </div>
          </section>
        )}

        {latestArtifact && (
          <section className="border border-[#ded6c9] bg-[#fffdf8] p-4">
            <div className="mb-3 flex items-center gap-2">
              <FileText className="h-4 w-4 text-[#8a5a18]" />
              <h3 className="text-sm font-semibold">Artifact</h3>
            </div>
            <p className="truncate text-sm font-semibold">{latestArtifact.filename}</p>
            <button
              type="button"
              onClick={() => downloadArtifact(latestArtifact)}
              className="mt-3 flex w-full items-center justify-center gap-2 bg-[#162033] px-3 py-2 text-sm font-semibold text-white"
            >
              <Download className="h-4 w-4" />
              Download
            </button>
          </section>
        )}

        <section className="border border-[#ded6c9] bg-[#fffdf8] p-4">
          <div className="mb-3 flex items-center gap-2">
            <BookOpen className="h-4 w-4 text-[#455f9a]" />
            <h3 className="text-sm font-semibold">Sources</h3>
          </div>
          {sources.length === 0 && <p className="text-sm text-[#79827c]">No sources attached.</p>}
          <div className="flex flex-col gap-3">
            {sources.map((source, index) => (
              <a key={`${source.url}-${index}`} href={source.url} target="_blank" rel="noreferrer" className="group border-t border-[#ebe4d8] pt-3 first:border-t-0 first:pt-0">
                <span className="flex items-start justify-between gap-2">
                  <span className="line-clamp-2 text-sm font-semibold text-[#162033]">{source.title || source.url}</span>
                  <ArrowUpRight className="h-3.5 w-3.5 shrink-0 text-[#79827c] group-hover:text-[#162033]" />
                </span>
                {source.url && <span className="mt-1 block truncate text-xs text-[#79827c]">{source.url}</span>}
              </a>
            ))}
          </div>
        </section>
      </div>
    </>
  )
}

function buildConfidenceCues(events: ProgressEvent[], result: AgentResult | null): string[] {
  const cues: string[] = []
  const providers = events
    .filter(event => event.stage === 'search_worker_provider' && event.data?.provider)
    .map(event => String(event.data?.provider))
  if (providers.length > 0) cues.push(`Searched with ${Array.from(new Set(providers)).join(', ')}`)
  const sourceEvent = [...events].reverse().find(event => event.stage === 'source_selection')
  if (sourceEvent?.data?.unique_count) cues.push(`${String(sourceEvent.data.unique_count)} source candidates selected`)
  const judge = [...events].reverse().find(event => event.stage === 'document_judge_result')
  if (judge?.data?.status) cues.push(`Document judge: ${String(judge.data.status)}`)
  if (result?.artifacts?.length) cues.push('Artifact saved to library')
  return cues.slice(0, 4)
}

function titleFromMessage(message: string): string {
  const cleaned = message.replace(/\s+/g, ' ').trim()
  return cleaned.length > 72 ? `${cleaned.slice(0, 72)}...` : cleaned || 'Untitled work'
}
