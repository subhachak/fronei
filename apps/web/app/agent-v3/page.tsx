'use client'

import { useAuth } from '@clerk/nextjs'
import DOMPurify from 'dompurify'
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
import { marked } from 'marked'
import { useEffect, useMemo, useRef, useState } from 'react'
import styles from './page.module.css'

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
  completedAt?: string
  message?: string
  qualityMode?: QualityMode
  outputFormat?: OutputFormat
  events?: ProgressEvent[]
  result?: AgentResult
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
  const [selectedWorkId, setSelectedWorkId] = useState<string | null>(null)
  const eventsRef = useRef<ProgressEvent[]>([])

  const canRun = useMemo(() => isLoaded && isSignedIn && message.trim().length > 0 && !running, [isLoaded, isSignedIn, message, running])
  const activeEvents = useMemo(() => events.filter(event => !['tool_selection', 'tool_result'].includes(event.stage)), [events])
  const confidenceCues = useMemo(() => buildConfidenceCues(events, result), [events, result])
  const latestArtifact = result?.artifacts?.[0] || library.find(item => item.artifacts.length)?.artifacts[0]
  const sources = result?.sources || []
  const selectedWork = selectedWorkId ? library.find(item => item.id === selectedWorkId) || null : null

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
    eventsRef.current = []
    setResult(null)
    setError(null)
    setRunning(true)
    setSelectedWorkId(null)
    setTraceOpen(false)
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
      const nextEvent = data as ProgressEvent
      eventsRef.current = [...eventsRef.current, nextEvent]
      setEvents(eventsRef.current)
    } else if (eventType === 'result') {
      const next = data as AgentResult
      setResult(next)
      setTraceOpen(false)
      setLibrary(prev => [
        {
          id: next.turn_id,
          title: titleFromMessage(message),
          route: next.route,
          createdAt: new Date().toISOString(),
          completedAt: new Date().toISOString(),
          message,
          qualityMode,
          outputFormat,
          events: eventsRef.current,
          result: next,
          artifacts: next.artifacts || [],
          sourceCount: next.sources?.length || 0,
        },
        ...prev.filter(item => item.id !== next.turn_id),
      ].slice(0, 20))
    } else if (eventType === 'error') {
      setError(data.message || 'Agent v3 failed')
    }
  }

  function selectWork(item: WorkItem) {
    if (running) return
    setSelectedWorkId(item.id)
    setMessage(item.message || item.title)
    eventsRef.current = item.events || []
    setEvents(eventsRef.current)
    setResult(item.result || null)
    setError(null)
    setTraceOpen(false)
    setMobileView('work')
    if (item.qualityMode) setQualityMode(item.qualityMode)
    if (item.outputFormat) setOutputFormat(item.outputFormat)
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
    <main className={styles.root}>
      <div className={styles.shell}>
        <MobileTopBar mobileView={mobileView} setMobileView={setMobileView} running={running} />

        <aside className={`${styles.libraryPane} ${mobileView === 'library' ? styles.mobileVisible : styles.mobileHidden}`}>
          <StudioLibrary
            library={library}
            latestArtifact={latestArtifact}
            selectedWorkId={selectedWorkId}
            onSelectWork={selectWork}
            downloadArtifact={downloadArtifact}
          />
        </aside>

        <section className={`${styles.workPane} ${mobileView === 'work' ? styles.mobileVisible : styles.mobileHidden}`}>
          <WorkbenchHeader running={running} result={result} />
          <div className={styles.workScroll}>
            <Timeline
              message={message}
              events={activeEvents}
              running={running}
              result={result}
              confidenceCues={confidenceCues}
              traceOpen={traceOpen}
              setTraceOpen={setTraceOpen}
              eventChips={eventChips}
              downloadArtifact={downloadArtifact}
            />
            {error && <div className={styles.errorBox}>{error}</div>}
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
            {!result && !running && events.length === 0 && <SuggestionStrip suggestions={SUGGESTIONS} setMessage={setMessage} />}
          </div>
        </section>

        <aside className={`${styles.contextPane} ${mobileView === 'context' ? styles.mobileVisible : styles.mobileHidden}`}>
          <ContextRail
            result={result}
            events={events}
            sources={sources}
            latestArtifact={latestArtifact}
            selectedWork={selectedWork}
            currentMessage={message}
            downloadArtifact={downloadArtifact}
          />
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
    <header className={styles.mobileTop}>
      <div className={styles.mobileBrandRow}>
        <div className={styles.brandLockup}>
          <span className={styles.brandMark}><Sparkles size={16} /></span>
          <div>
            <p className={styles.overline}>Fronei Studio</p>
            <p className={styles.brandTitle}>Agent v3</p>
          </div>
        </div>
        {running && <span className={styles.runningPill}><Loader2 size={14} className={styles.spin} /> Working</span>}
      </div>
      <nav className={styles.mobileNav}>
        {items.map(([id, Icon, label]) => (
          <button
            key={id}
            type="button"
            onClick={() => setMobileView(id)}
            className={`${styles.mobileNavButton} ${mobileView === id ? styles.mobileNavButtonActive : ''}`}
          >
            <Icon size={14} />
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
  selectedWorkId,
  onSelectWork,
  downloadArtifact,
}: {
  library: WorkItem[]
  latestArtifact?: Artifact
  selectedWorkId: string | null
  onSelectWork: (item: WorkItem) => void
  downloadArtifact: (artifact: Artifact) => void
}) {
  return (
    <>
      <div className={styles.sectionHeader}>
        <div>
          <p className={styles.overline}>Studio</p>
          <h1 className={styles.sectionTitle}>Work library</h1>
        </div>
        <span className={styles.sectionIcon}><Archive size={16} /></span>
      </div>

      {latestArtifact && (
        <button
          type="button"
          onClick={() => downloadArtifact(latestArtifact)}
          className={styles.artifactButton}
        >
          <span className={styles.artifactIcon}><Download size={16} /></span>
          <span className={styles.minZero}>
            <span className={styles.truncateStrong}>{latestArtifact.filename}</span>
            <span className={styles.mutedSmall}>Latest artifact</span>
          </span>
        </button>
      )}

      <div className={styles.libraryList}>
        {library.length === 0 && (
          <div className={styles.emptyState}>
            Finished work will appear here.
          </div>
        )}
        {library.map(item => (
          <button
            key={item.id}
            type="button"
            onClick={() => onSelectWork(item)}
            className={`${styles.workCard} ${selectedWorkId === item.id ? styles.workCardActive : ''}`}
          >
            <div className={styles.cardRowTop}>
              <span className={styles.cardIcon}><FileText size={16} /></span>
              <div className={styles.minZero}>
                <p className={styles.cardTitle}>{item.title}</p>
                <p className={styles.cardMeta}>{item.route} | {item.sourceCount} sources | {formatRelativeTime(item.completedAt || item.createdAt)}</p>
              </div>
            </div>
            {item.artifacts.length > 0 && (
              <button
                type="button"
                onClick={event => {
                  event.stopPropagation()
                  downloadArtifact(item.artifacts[0])
                }}
                className={styles.secondaryButton}
              >
                <Download size={14} />
                Download
              </button>
            )}
          </button>
        ))}
      </div>
    </>
  )
}

function WorkbenchHeader({ running, result }: { running: boolean; result: AgentResult | null }) {
  return (
    <header className={styles.desktopHeader}>
      <div className={styles.headerInner}>
        <div>
          <p className={styles.overline}>Research and work-product studio</p>
          <h2 className={styles.headerTitle}>Workbench</h2>
        </div>
        <div className={styles.headerActions}>
          {result && <span className={styles.routePill}>{result.route} | {result.latency_ms ?? 0}ms</span>}
          <span className={`${styles.statusPill} ${running ? styles.statusActive : ''}`}>
            {running ? <Loader2 size={14} className={styles.spin} /> : <CheckCircle2 size={14} />}
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
    <section className={styles.composer}>
      <textarea
        value={message}
        onChange={event => setMessage(event.target.value)}
        className={styles.textarea}
        placeholder="Give Fronei a task..."
      />
      <div className={styles.composerFooter}>
        <div className={styles.selectGrid}>
          <StudioSelect label="Quality" value={qualityMode} onChange={value => setQualityMode(value as QualityMode)} options={['draft', 'standard', 'executive']} />
          <StudioSelect label="Output" value={outputFormat} onChange={value => setOutputFormat(value as OutputFormat)} options={['chat', 'markdown', 'docx']} />
        </div>
        <button
          type="button"
          onClick={run}
          disabled={!canRun}
          className={styles.primaryButton}
        >
          {running ? <Loader2 size={16} className={styles.spin} /> : <Send size={16} />}
          {running ? 'Working' : 'Start'}
        </button>
      </div>
    </section>
  )
}

function StudioSelect({ label, value, onChange, options }: { label: string; value: string; onChange: (value: string) => void; options: string[] }) {
  return (
    <label className={styles.studioSelect}>
      <span>{label}</span>
      <select value={value} onChange={event => onChange(event.target.value)} className={styles.selectInput}>
        {options.map(option => <option key={option} value={option}>{option}</option>)}
      </select>
    </label>
  )
}

function SuggestionStrip({ suggestions, setMessage }: { suggestions: string[]; setMessage: (message: string) => void }) {
  return (
    <div className={styles.suggestionStrip}>
      {suggestions.map(suggestion => (
        <button
          key={suggestion}
          type="button"
          onClick={() => setMessage(suggestion)}
          className={styles.suggestionButton}
        >
          {suggestion}
        </button>
      ))}
    </div>
  )
}

function Timeline({
  message,
  events,
  running,
  result,
  confidenceCues,
  traceOpen,
  setTraceOpen,
  eventChips,
  downloadArtifact,
}: {
  message: string
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
    <section className={styles.chatThread}>
      <div className={styles.userBubble}>
        <p className={styles.bubbleLabel}>You</p>
        <p className={styles.userText}>{message}</p>
      </div>

      <div className={styles.assistantBubble}>
        <div className={styles.assistantHeader}>
          <span className={styles.companionMark}><Sparkles size={16} /></span>
          <div>
            <p className={styles.companionTitle}>Fronei</p>
            <p className={styles.companionText}>
              {running ? 'Working through the route, tools, and evidence.' : result ? 'Here is the finished response.' : 'Ready when you are.'}
            </p>
          </div>
        </div>

        {running && (
          <RollingCommentary events={events} eventChips={eventChips} />
        )}

        {!running && result && (
          <>
            {confidenceCues.length > 0 && (
              <div className={styles.cueGrid}>
                {confidenceCues.map(cue => (
                  <div key={cue} className={styles.cueCard}>
                    <CheckCircle2 size={16} />
                    {cue}
                  </div>
                ))}
              </div>
            )}
            <MarkdownResult content={result.answer} />
            {result.artifacts?.length ? (
              <div className={styles.artifactRow}>
                {result.artifacts.map(artifact => (
                  <button
                    key={artifact.filename}
                    type="button"
                    onClick={() => downloadArtifact(artifact)}
                    className={styles.darkButton}
                  >
                    <Download size={16} />
                    {artifact.filename}
                  </button>
                ))}
              </div>
            ) : null}
          </>
        )}

        {!running && !result && (
          <p className={styles.emptyAssistantText}>Start a task and I will keep the work visible here.</p>
        )}
      </div>

      {(events.length > 0 || result) && (
        <button
          type="button"
          onClick={() => setTraceOpen(!traceOpen)}
          className={styles.traceToggle}
        >
          <span>{traceOpen ? 'Hide execution trace' : `${events.length || 0} engine events`}</span>
          <ChevronDown size={16} className={traceOpen ? styles.rotated : ''} />
        </button>
      )}

      {traceOpen && (
        <div className={styles.traceList}>
          {events.length === 0 && <p className={styles.mutedText}>No events yet.</p>}
          {events.map((event, index) => (
            <div key={`${event.stage}-${index}`} className={styles.traceEvent}>
              <p className={styles.traceStage}>{event.stage}</p>
              <p className={styles.traceMessage}>{event.message}</p>
              {eventChips(event).length ? (
                <div className={styles.chipRow}>
                  {eventChips(event).map(chip => (
                    <span key={chip} className={styles.traceChip}>{chip}</span>
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

function RollingCommentary({ events, eventChips }: { events: ProgressEvent[]; eventChips: (event: ProgressEvent) => string[] }) {
  const visibleEvents = events.slice(-6)
  return (
    <div className={styles.rollingLog}>
      {visibleEvents.length === 0 && (
        <div className={styles.rollingEvent}>
          <span className={styles.liveDot} />
          <div>
            <p className={styles.rollingStage}>Starting</p>
            <p className={styles.rollingMessage}>Preparing the route and available tools.</p>
          </div>
        </div>
      )}
      {visibleEvents.map((event, index) => (
        <div key={`${event.stage}-${index}`} className={styles.rollingEvent}>
          <span className={styles.liveDot} />
          <div>
            <p className={styles.rollingStage}>{humanizeStage(event.stage)}</p>
            <p className={styles.rollingMessage}>{event.message}</p>
            {eventChips(event).length ? (
              <div className={styles.chipRow}>
                {eventChips(event).map(chip => <span key={chip} className={styles.traceChip}>{chip}</span>)}
              </div>
            ) : null}
          </div>
        </div>
      ))}
    </div>
  )
}

function ContextRail({
  result,
  events,
  sources,
  latestArtifact,
  selectedWork,
  currentMessage,
  downloadArtifact,
}: {
  result: AgentResult | null
  events: ProgressEvent[]
  sources: Source[]
  latestArtifact?: Artifact
  selectedWork: WorkItem | null
  currentMessage: string
  downloadArtifact: (artifact: Artifact) => void
}) {
  const providerEvents = events.filter(event => event.stage === 'search_worker_provider')
  const workSummary = buildWorkSummary({ result, events, sources, selectedWork, currentMessage })
  return (
    <>
      <div className={styles.sectionHeaderPlain}>
        <p className={styles.overline}>Context</p>
        <h2 className={styles.sectionTitle}>Current work</h2>
      </div>

      <div className={styles.contextList}>
        <details className={styles.summaryDrawer} open>
          <summary>Work summary</summary>
          <div className={styles.summaryBody}>
            <p className={styles.summaryTitle}>{workSummary.title}</p>
            <div className={styles.summaryGrid}>
              <span>Turns</span><strong>{workSummary.turns}</strong>
              <span>Route</span><strong>{workSummary.route}</strong>
              <span>Time</span><strong>{workSummary.time}</strong>
              <span>Budget</span><strong>{workSummary.budget}</strong>
              <span>Sources</span><strong>{workSummary.sources}</strong>
              <span>Events</span><strong>{workSummary.events}</strong>
            </div>
          </div>
        </details>

        <section className={styles.contextCard}>
          <div className={styles.contextCardHeader}>
            <Clock3 size={16} />
            <h3>Status</h3>
          </div>
          <p className={styles.contextBody}>{result ? `Completed as ${result.route}` : events.length ? 'In progress' : 'Waiting'}</p>
          {result?.model_used && <p className={styles.mutedSmall}>{result.model_used}</p>}
        </section>

        {providerEvents.length > 0 && (
          <section className={styles.contextCard}>
            <div className={styles.contextCardHeader}>
              <Search size={16} />
              <h3>Search providers</h3>
            </div>
            <div className={styles.providerList}>
              {providerEvents.map((event, index) => (
                <div key={`${event.message}-${index}`} className={styles.providerRow}>
                  <span>Worker {String(event.data?.worker_index || index + 1)}</span>
                  <strong>{String(event.data?.provider || 'none')}</strong>
                </div>
              ))}
            </div>
          </section>
        )}

        {latestArtifact && (
          <section className={styles.artifactCard}>
            <div className={styles.contextCardHeader}>
              <FileText size={16} />
              <h3>Generated document</h3>
            </div>
            <p className={styles.truncateStrong}>{latestArtifact.filename}</p>
            <p className={styles.mutedSmall}>Saved with this work session.</p>
            <button
              type="button"
              onClick={() => downloadArtifact(latestArtifact)}
              className={styles.fullDarkButton}
            >
              <Download size={16} />
              Download
            </button>
          </section>
        )}

        <section className={styles.contextCard}>
          <div className={styles.contextCardHeader}>
            <BookOpen size={16} />
            <h3>Sources</h3>
          </div>
          {sources.length === 0 && <p className={styles.mutedText}>No sources attached.</p>}
          <div className={styles.sourceList}>
            {sources.map((source, index) => (
              <a key={`${source.url}-${index}`} href={source.url} target="_blank" rel="noreferrer" className={styles.sourceLink}>
                <span className={styles.sourceTitleRow}>
                  <span className={styles.sourceTitle}>{source.title || source.url}</span>
                  <ArrowUpRight size={14} />
                </span>
                {source.url && <span className={styles.sourceUrl}>{source.url}</span>}
              </a>
            ))}
          </div>
        </section>
      </div>
    </>
  )
}

function MarkdownResult({ content }: { content: string }) {
  const html = useMemo(() => DOMPurify.sanitize(marked.parse(content || '') as string), [content])
  return <div className={styles.markdownResult} dangerouslySetInnerHTML={{ __html: html }} />
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

function buildWorkSummary({
  result,
  events,
  sources,
  selectedWork,
  currentMessage,
}: {
  result: AgentResult | null
  events: ProgressEvent[]
  sources: Source[]
  selectedWork: WorkItem | null
  currentMessage: string
}) {
  const cost = estimateCost(events)
  const timeMs = result?.latency_ms || estimateDurationMs(events, selectedWork)
  return {
    title: selectedWork?.title || titleFromMessage(currentMessage),
    turns: result || selectedWork ? '1' : '0',
    route: result?.route || selectedWork?.route || 'not routed',
    time: timeMs ? formatDuration(timeMs) : 'waiting',
    budget: cost ? `$${cost.toFixed(4)}` : 'not reported',
    sources: String(sources.length || selectedWork?.sourceCount || 0),
    events: String(events.length || selectedWork?.events?.length || 0),
  }
}

function estimateCost(events: ProgressEvent[]): number {
  return events.reduce((total, event) => {
    const data = event.data || {}
    for (const key of ['cost_usd', 'estimated_cost_usd', 'total_cost_usd']) {
      const value = data[key]
      if (typeof value === 'number' && Number.isFinite(value)) return total + value
      if (typeof value === 'string') {
        const parsed = Number(value)
        if (Number.isFinite(parsed)) return total + parsed
      }
    }
    return total
  }, 0)
}

function estimateDurationMs(events: ProgressEvent[], selectedWork: WorkItem | null): number {
  if (selectedWork?.result?.latency_ms) return selectedWork.result.latency_ms
  const first = events.find(event => event.created_at)?.created_at
  const last = [...events].reverse().find(event => event.created_at)?.created_at
  if (!first || !last) return 0
  const start = new Date(first).getTime()
  const end = new Date(last).getTime()
  return Number.isFinite(start) && Number.isFinite(end) && end > start ? end - start : 0
}

function formatDuration(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)}ms`
  const seconds = Math.round(ms / 1000)
  if (seconds < 60) return `${seconds}s`
  const minutes = Math.floor(seconds / 60)
  return `${minutes}m ${seconds % 60}s`
}

function formatRelativeTime(value: string): string {
  const timestamp = new Date(value).getTime()
  if (!Number.isFinite(timestamp)) return 'recent'
  const seconds = Math.max(1, Math.round((Date.now() - timestamp) / 1000))
  if (seconds < 60) return `${seconds}s ago`
  const minutes = Math.round(seconds / 60)
  if (minutes < 60) return `${minutes}m ago`
  const hours = Math.round(minutes / 60)
  if (hours < 24) return `${hours}h ago`
  return `${Math.round(hours / 24)}d ago`
}

function humanizeStage(stage: string): string {
  return stage
    .replace(/_/g, ' ')
    .replace(/\b\w/g, char => char.toUpperCase())
}

function titleFromMessage(message: string): string {
  const cleaned = message.replace(/\s+/g, ' ').trim()
  return cleaned.length > 72 ? `${cleaned.slice(0, 72)}...` : cleaned || 'Untitled work'
}
