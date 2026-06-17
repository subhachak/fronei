'use client'

import { useAuth } from '@clerk/nextjs'
import DOMPurify from 'dompurify'
import {
  ArrowUpRight,
  BookOpen,
  CheckCircle2,
  ChevronDown,
  Clock3,
  Download,
  FileText,
  Folder,
  Library,
  Loader2,
  MessageSquare,
  PanelRight,
  Plus,
  Search,
  Send,
  Sparkles,
  Trash2,
} from 'lucide-react'
import type { LucideIcon } from 'lucide-react'
import { marked } from 'marked'
import { useEffect, useMemo, useRef, useState } from 'react'
import styles from './page.module.css'

const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL || 'http://localhost:8000'
const INITIAL_VISIBLE_TURNS = 6

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
  id?: string
  filename: string
  mime_type: string
  base64_data?: string
  download_url?: string
  size_bytes?: number
}

type Source = {
  title?: string
  url?: string
  snippet?: string
  content?: string
}

type AgentResult = {
  turn_id: string
  goal?: {
    objective?: string
    quality_mode?: string
  }
  answer: string
  route: string
  model_used?: string
  latency_ms?: number
  sources?: Source[]
  artifacts?: Artifact[]
  events?: ProgressEvent[]
  created_at?: string
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

type Conversation = {
  id: string
  title: string
  createdAt: string
  updatedAt: string
  turns: WorkItem[]
  turnCount?: number
  artifactCount?: number
  sourceCount?: number
  totalLatencyMs?: number
  totalCostUsd?: number
}

type Workspace = {
  id: string
  name: string
  createdAt: string
  updatedAt: string
  conversations: Conversation[]
}

type ApiConversation = {
  id: string
  workspace_id: string
  title: string
  created_at: string
  updated_at: string
  turn_count?: number
  artifact_count?: number
  source_count?: number
  total_latency_ms?: number
  total_cost_usd?: number
}

type ApiWorkspace = {
  id: string
  name: string
  created_at: string
  updated_at: string
  conversations: ApiConversation[]
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
  const [workspaces, setWorkspaces] = useState<Workspace[]>([])
  const [activeWorkspaceId, setActiveWorkspaceId] = useState<string | null>(null)
  const [activeConversationId, setActiveConversationId] = useState<string | null>(null)
  const [mobileView, setMobileView] = useState<MobileView>('work')
  const [traceOpen, setTraceOpen] = useState(false)
  const [visibleTurnCount, setVisibleTurnCount] = useState(INITIAL_VISIBLE_TURNS)
  const eventsRef = useRef<ProgressEvent[]>([])
  const chatScrollRef = useRef<HTMLDivElement | null>(null)
  const activeRunConversationIdRef = useRef<string | null>(null)

  const canRun = useMemo(() => isLoaded && isSignedIn && message.trim().length > 0 && !running, [isLoaded, isSignedIn, message, running])
  const activeEvents = useMemo(() => events.filter(event => !['tool_selection', 'tool_result'].includes(event.stage)), [events])
  const confidenceCues = useMemo(() => buildConfidenceCues(events, result), [events, result])
  const activeWorkspace = useMemo(() => workspaces.find(workspace => workspace.id === activeWorkspaceId) || workspaces[0] || null, [activeWorkspaceId, workspaces])
  const activeConversation = useMemo(
    () => activeWorkspace?.conversations.find(conversation => conversation.id === activeConversationId) || activeWorkspace?.conversations[0] || null,
    [activeConversationId, activeWorkspace],
  )
  const activeTurns = activeConversation?.turns || []
  const visibleTurns = activeTurns.slice(Math.max(0, activeTurns.length - visibleTurnCount))
  const canLoadOlder = Boolean(activeConversation && (activeConversation.turnCount || activeTurns.length) > activeTurns.length)
  const latestTurn = activeTurns.at(-1) || null
  const latestArtifact = result?.artifacts?.[0] || latestTurn?.artifacts?.[0]
  const sources = result?.sources || []

  useEffect(() => {
    if (!isLoaded || !isSignedIn) return
    void loadWorkspaces().catch(err => {
      setError(err instanceof Error ? err.message : 'Could not load Agent v3 workspaces')
    })
  }, [isLoaded, isSignedIn])

  useEffect(() => {
    chatScrollRef.current?.scrollTo({ top: chatScrollRef.current.scrollHeight, behavior: 'smooth' })
  }, [visibleTurns.length, running, result?.turn_id, events.length])

  async function authorizedFetch(path: string, init: RequestInit = {}) {
    const token = await getToken()
    return fetch(`${API_BASE}${path}`, {
      ...init,
      headers: {
        ...(init.body ? { 'Content-Type': 'application/json' } : {}),
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...(init.headers || {}),
      },
    })
  }

  async function loadWorkspaces(selectConversationId?: string) {
    const response = await authorizedFetch('/agent-v3/workspaces')
    if (!response.ok) throw new Error(await response.text() || 'Could not load workspaces')
    const payload = await response.json() as { workspaces: ApiWorkspace[] }
    const next = payload.workspaces.map(mapWorkspace)
    setWorkspaces(next)
    const selectedWorkspace = next.find(workspace => workspace.conversations.some(conversation => conversation.id === selectConversationId)) || next[0] || null
    const selectedConversation = selectedWorkspace?.conversations.find(conversation => conversation.id === selectConversationId) || selectedWorkspace?.conversations[0] || null
    setActiveWorkspaceId(selectedWorkspace?.id || null)
    setActiveConversationId(selectedConversation?.id || null)
    if (selectedConversation) await loadConversationTurns(selectedConversation.id, INITIAL_VISIBLE_TURNS)
  }

  async function loadConversationTurns(conversationId: string, limit = visibleTurnCount) {
    const response = await authorizedFetch(`/agent-v3/conversations/${conversationId}/turns?limit=${limit}`)
    if (!response.ok) throw new Error(await response.text() || 'Could not load conversation turns')
    const payload = await response.json() as { turns: AgentResult[] }
    const turns = payload.turns.map(mapTurn)
    setWorkspaces(prev => prev.map(workspace => ({
      ...workspace,
      conversations: workspace.conversations.map(conversation => (
        conversation.id === conversationId ? { ...conversation, turns } : conversation
      )),
    })))
    const latest = turns.at(-1)
    eventsRef.current = latest?.events || []
    setEvents(eventsRef.current)
    setResult(latest?.result || null)
  }

  async function run() {
    if (!canRun) return
    setEvents([])
    eventsRef.current = []
    setResult(null)
    setError(null)
    setRunning(true)
    setTraceOpen(false)
    setMobileView('work')
    try {
      const conversationId = await ensureActiveConversation(message)
      activeRunConversationIdRef.current = conversationId
      const response = await authorizedFetch('/agent-v3/turns/stream', {
        method: 'POST',
        body: JSON.stringify({
          message,
          conversation_id: conversationId,
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
      activeRunConversationIdRef.current = null
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
      appendTurnToActiveConversation({
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
      }, activeRunConversationIdRef.current || activeConversationId)
    } else if (eventType === 'error') {
      setError(data.message || 'Agent v3 failed')
    }
  }

  async function selectConversation(workspaceId: string, conversationId: string) {
    if (running) return
    const workspace = workspaces.find(item => item.id === workspaceId)
    const conversation = workspace?.conversations.find(item => item.id === conversationId)
    setActiveWorkspaceId(workspaceId)
    setActiveConversationId(conversationId)
    setVisibleTurnCount(INITIAL_VISIBLE_TURNS)
    setMessage('')
    eventsRef.current = []
    setEvents([])
    setResult(null)
    setError(null)
    setTraceOpen(false)
    setMobileView('work')
    if (conversation) await loadConversationTurns(conversationId, INITIAL_VISIBLE_TURNS)
  }

  async function createWorkspace() {
    const name = window.prompt('Workspace name', 'New workspace')?.trim()
    if (!name) return
    const response = await authorizedFetch('/agent-v3/workspaces', {
      method: 'POST',
      body: JSON.stringify({ name }),
    })
    if (!response.ok) {
      setError(await response.text() || 'Could not create workspace')
      return
    }
    const workspace = mapWorkspace({ ...(await response.json()), conversations: [] })
    setWorkspaces(prev => [workspace, ...prev])
    setActiveWorkspaceId(workspace.id)
    await createConversation(workspace.id, 'New conversation')
  }

  async function deleteWorkspace(workspaceId: string) {
    if (workspaces.length <= 1) return
    if (!window.confirm('Delete this workspace and all conversations inside it?')) return
    const response = await authorizedFetch(`/agent-v3/workspaces/${workspaceId}`, { method: 'DELETE' })
    if (!response.ok) {
      setError(await response.text() || 'Could not delete workspace')
      return
    }
    await loadWorkspaces()
  }

  async function createConversation(workspaceId: string, titleOverride?: string) {
    const title = titleOverride || window.prompt('Conversation title', 'New conversation')?.trim()
    if (!title) return
    const response = await authorizedFetch(`/agent-v3/workspaces/${workspaceId}/conversations`, {
      method: 'POST',
      body: JSON.stringify({ title }),
    })
    if (!response.ok) {
      setError(await response.text() || 'Could not create conversation')
      return
    }
    const conversation = mapConversation(await response.json())
    setWorkspaces(prev => prev.map(workspace => (
      workspace.id === workspaceId
        ? { ...workspace, updatedAt: conversation.updatedAt, conversations: [conversation, ...workspace.conversations] }
        : workspace
    )))
    setActiveWorkspaceId(workspaceId)
    setActiveConversationId(conversation.id)
    setVisibleTurnCount(INITIAL_VISIBLE_TURNS)
    setMessage('')
    setEvents([])
    eventsRef.current = []
    setResult(null)
  }

  async function deleteConversation(workspaceId: string, conversationId: string) {
    if (!window.confirm('Delete this conversation?')) return
    const response = await authorizedFetch(`/agent-v3/conversations/${conversationId}`, { method: 'DELETE' })
    if (!response.ok) {
      setError(await response.text() || 'Could not delete conversation')
      return
    }
    setWorkspaces(prev => prev.map(workspace => (
      workspace.id === workspaceId
        ? { ...workspace, updatedAt: new Date().toISOString(), conversations: workspace.conversations.filter(conversation => conversation.id !== conversationId) }
        : workspace
    )))
    if (activeConversationId === conversationId) {
      const workspace = workspaces.find(item => item.id === workspaceId)
      const nextConversation = workspace?.conversations.find(item => item.id !== conversationId)
      setActiveConversationId(nextConversation?.id || null)
      if (nextConversation) await loadConversationTurns(nextConversation.id, INITIAL_VISIBLE_TURNS)
      else {
        eventsRef.current = []
        setEvents([])
        setResult(null)
      }
    }
  }

  async function ensureActiveConversation(seedMessage: string): Promise<string> {
    if (activeConversation) return activeConversation.id
    let workspace = activeWorkspace
    if (!workspace) {
      const response = await authorizedFetch('/agent-v3/workspaces', {
        method: 'POST',
        body: JSON.stringify({ name: 'Personal workspace' }),
      })
      if (!response.ok) throw new Error(await response.text() || 'Could not create workspace')
      workspace = mapWorkspace({ ...(await response.json()), conversations: [] })
      setWorkspaces(prev => [workspace as Workspace, ...prev])
      setActiveWorkspaceId(workspace.id)
    }
    const response = await authorizedFetch(`/agent-v3/workspaces/${workspace.id}/conversations`, {
      method: 'POST',
      body: JSON.stringify({ title: titleFromMessage(seedMessage) }),
    })
    if (!response.ok) throw new Error(await response.text() || 'Could not create conversation')
    const conversation = mapConversation(await response.json())
    setWorkspaces(prev => prev.map(item => (
      item.id === workspace.id
        ? { ...item, updatedAt: conversation.updatedAt, conversations: [conversation, ...item.conversations] }
        : item
    )))
    setActiveConversationId(conversation.id)
    return conversation.id
  }

  function appendTurnToActiveConversation(turn: WorkItem, conversationId: string | null) {
    setWorkspaces(prev => {
      return prev.map(workspace => {
        if (!workspace.conversations.some(conversation => conversation.id === conversationId)) return workspace
        return {
          ...workspace,
          updatedAt: turn.completedAt || turn.createdAt,
          conversations: workspace.conversations.map(conversation => (
            conversation.id === conversationId
              ? {
                ...conversation,
                title: conversation.turns.length || conversation.turnCount ? conversation.title : turn.title,
                updatedAt: turn.completedAt || turn.createdAt,
                turnCount: (conversation.turnCount || conversation.turns.length) + 1,
                artifactCount: (conversation.artifactCount || 0) + turn.artifacts.length,
                sourceCount: (conversation.sourceCount || 0) + turn.sourceCount,
                totalLatencyMs: (conversation.totalLatencyMs || 0) + (turn.result?.latency_ms || 0),
                turns: [...conversation.turns.filter(item => item.id !== turn.id), turn],
              }
              : conversation
          )),
        }
      })
    })
  }

  async function downloadArtifact(artifact: Artifact) {
    if (artifact.download_url) {
      const response = await authorizedFetch(artifact.download_url)
      if (!response.ok) {
        setError(await response.text() || 'Could not download artifact')
        return
      }
      const blob = await response.blob()
      triggerDownload(blob, artifact.filename)
      return
    }
    if (!artifact.base64_data) return
    const byteString = atob(artifact.base64_data)
    const bytes = new Uint8Array(byteString.length)
    for (let i = 0; i < byteString.length; i += 1) bytes[i] = byteString.charCodeAt(i)
    const blob = new Blob([bytes], { type: artifact.mime_type })
    triggerDownload(blob, artifact.filename)
  }

  function triggerDownload(blob: Blob, filename: string) {
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = filename
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
            workspaces={workspaces}
            activeWorkspaceId={activeWorkspace?.id || null}
            activeConversationId={activeConversation?.id || null}
            onCreateWorkspace={createWorkspace}
            onDeleteWorkspace={deleteWorkspace}
            onCreateConversation={createConversation}
            onDeleteConversation={deleteConversation}
            onSelectConversation={selectConversation}
          />
        </aside>

        <section className={`${styles.workPane} ${mobileView === 'work' ? styles.mobileVisible : styles.mobileHidden}`}>
          <WorkbenchHeader running={running} result={result} />
          <div className={styles.workScroll} ref={chatScrollRef}>
            {canLoadOlder && (
              <button
                type="button"
                className={styles.loadOlderButton}
                onClick={() => {
                  const nextCount = visibleTurnCount + INITIAL_VISIBLE_TURNS
                  setVisibleTurnCount(nextCount)
                  if (activeConversationId) void loadConversationTurns(activeConversationId, nextCount)
                }}
              >
                Load older turns
              </button>
            )}
            <Timeline
              draftMessage={message}
              turns={visibleTurns}
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
            {!result && !running && activeTurns.length === 0 && <SuggestionStrip suggestions={SUGGESTIONS} setMessage={setMessage} />}
          </div>
          <div className={styles.composerDock}>
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
          </div>
        </section>

        <aside className={`${styles.contextPane} ${mobileView === 'context' ? styles.mobileVisible : styles.mobileHidden}`}>
          <ContextRail
            result={result}
            events={events}
            sources={sources}
            latestArtifact={latestArtifact}
            activeConversation={activeConversation}
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
  workspaces,
  activeWorkspaceId,
  activeConversationId,
  onCreateWorkspace,
  onDeleteWorkspace,
  onCreateConversation,
  onDeleteConversation,
  onSelectConversation,
}: {
  workspaces: Workspace[]
  activeWorkspaceId: string | null
  activeConversationId: string | null
  onCreateWorkspace: () => void
  onDeleteWorkspace: (workspaceId: string) => void
  onCreateConversation: (workspaceId: string) => void
  onDeleteConversation: (workspaceId: string, conversationId: string) => void
  onSelectConversation: (workspaceId: string, conversationId: string) => void
}) {
  return (
    <>
      <div className={styles.sectionHeader}>
        <div>
          <p className={styles.overline}>Studio</p>
          <h1 className={styles.sectionTitle}>Workspaces</h1>
        </div>
        <button type="button" className={styles.iconButton} onClick={onCreateWorkspace} aria-label="Create workspace">
          <Plus size={16} />
        </button>
      </div>

      <div className={styles.libraryList}>
        {workspaces.length === 0 && (
          <div className={styles.emptyState}>
            Create a workspace to begin.
          </div>
        )}
        {workspaces.map((workspace, index) => {
          const expanded = workspace.id === activeWorkspaceId || index === 0
          const turnCount = workspace.conversations.reduce((total, conversation) => total + (conversation.turnCount || conversation.turns.length), 0)
          return (
            <details key={workspace.id} className={styles.workspaceGroup} open={expanded}>
              <summary className={styles.workspaceSummary}>
                <span className={styles.workspaceSummaryMain}>
                  <Folder size={15} />
                  <span className={styles.workspaceName}>{workspace.name}</span>
                </span>
                <span className={styles.workspaceMeta}>{workspace.conversations.length} conv | {turnCount} turns</span>
              </summary>
              <div className={styles.workspaceActions}>
                <button type="button" onClick={() => onCreateConversation(workspace.id)}>
                  <Plus size={13} /> Conversation
                </button>
                {workspaces.length > 1 && (
                  <button type="button" onClick={() => onDeleteWorkspace(workspace.id)}>
                    <Trash2 size={13} /> Delete
                  </button>
                )}
              </div>
              <div className={styles.conversationList}>
                {workspace.conversations.map(conversation => (
                  <div key={conversation.id} className={`${styles.conversationItemWrap} ${conversation.id === activeConversationId ? styles.conversationItemActive : ''}`}>
                    <button
                      type="button"
                      className={styles.conversationItem}
                      onClick={() => onSelectConversation(workspace.id, conversation.id)}
                    >
                      <MessageSquare size={15} />
                      <span className={styles.conversationText}>
                        <span>{conversation.title}</span>
                        <small>{conversation.turnCount || conversation.turns.length} turns | {formatRelativeTime(conversation.updatedAt)}</small>
                      </span>
                    </button>
                    <button
                      type="button"
                      className={styles.deleteConversationButton}
                      onClick={() => onDeleteConversation(workspace.id, conversation.id)}
                      aria-label="Delete conversation"
                    >
                      <Trash2 size={13} />
                    </button>
                  </div>
                ))}
              </div>
            </details>
          )
        })}
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
  draftMessage,
  turns,
  events,
  running,
  result,
  confidenceCues,
  traceOpen,
  setTraceOpen,
  eventChips,
  downloadArtifact,
}: {
  draftMessage: string
  turns: WorkItem[]
  events: ProgressEvent[]
  running: boolean
  result: AgentResult | null
  confidenceCues: string[]
  traceOpen: boolean
  setTraceOpen: (open: boolean) => void
  eventChips: (event: ProgressEvent) => string[]
  downloadArtifact: (artifact: Artifact) => void | Promise<void>
}) {
  return (
    <section className={styles.chatThread}>
      {turns.length === 0 && !running && !result && (
        <div className={styles.assistantBubble}>
          <div className={styles.assistantHeader}>
            <span className={styles.companionMark}><Sparkles size={16} /></span>
            <div>
              <p className={styles.companionTitle}>Fronei</p>
              <p className={styles.companionText}>Start a task and I will keep the work visible here.</p>
            </div>
          </div>
          <p className={styles.emptyAssistantText}>This conversation is empty.</p>
        </div>
      )}

      {turns.map(turn => (
        <TurnPair key={turn.id} turn={turn} downloadArtifact={downloadArtifact} />
      ))}

      {running && (
        <LiveTurn message={draftMessage} events={events} eventChips={eventChips} />
      )}

      {!running && result && turns.length === 0 && (
        <TurnPair
          turn={{
            id: result.turn_id,
            title: titleFromMessage(draftMessage),
            route: result.route,
            createdAt: new Date().toISOString(),
            message: draftMessage,
            events,
            result,
            artifacts: result.artifacts || [],
            sourceCount: result.sources?.length || 0,
          }}
          confidenceCues={confidenceCues}
          downloadArtifact={downloadArtifact}
        />
      )}

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

function TurnPair({
  turn,
  confidenceCues,
  downloadArtifact,
}: {
  turn: WorkItem
  confidenceCues?: string[]
  downloadArtifact: (artifact: Artifact) => void | Promise<void>
}) {
  return (
    <>
      <div className={styles.userBubble}>
        <p className={styles.bubbleLabel}>You</p>
        <p className={styles.userText}>{turn.message || turn.title}</p>
      </div>
      <div className={styles.assistantBubble}>
        <div className={styles.assistantHeader}>
          <span className={styles.companionMark}><Sparkles size={16} /></span>
          <div>
            <p className={styles.companionTitle}>Fronei</p>
            <p className={styles.companionText}>Completed as {turn.route}.</p>
          </div>
        </div>
        {confidenceCues?.length ? (
          <div className={styles.cueGrid}>
            {confidenceCues.map(cue => (
              <div key={cue} className={styles.cueCard}>
                <CheckCircle2 size={16} />
                {cue}
              </div>
            ))}
          </div>
        ) : null}
        <MarkdownResult content={turn.result?.answer || ''} />
        {turn.artifacts.length ? (
          <div className={styles.artifactRow}>
            {turn.artifacts.map(artifact => (
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
      </div>
    </>
  )
}

function LiveTurn({ message, events, eventChips }: { message: string; events: ProgressEvent[]; eventChips: (event: ProgressEvent) => string[] }) {
  return (
    <>
      <div className={styles.userBubble}>
        <p className={styles.bubbleLabel}>You</p>
        <p className={styles.userText}>{message}</p>
      </div>
      <div className={styles.assistantBubble}>
        <div className={styles.assistantHeader}>
          <span className={styles.companionMark}><Sparkles size={16} /></span>
          <div>
            <p className={styles.companionTitle}>Fronei</p>
            <p className={styles.companionText}>Working through the route, tools, providers, and evidence.</p>
          </div>
        </div>
        <RollingCommentary events={events} eventChips={eventChips} />
      </div>
    </>
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
  activeConversation,
  currentMessage,
  downloadArtifact,
}: {
  result: AgentResult | null
  events: ProgressEvent[]
  sources: Source[]
  latestArtifact?: Artifact
  activeConversation: Conversation | null
  currentMessage: string
  downloadArtifact: (artifact: Artifact) => void | Promise<void>
}) {
  const providerEvents = events.filter(event => event.stage === 'search_worker_provider')
  const workSummary = buildWorkSummary({ result, events, sources, activeConversation, currentMessage })
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
  activeConversation,
  currentMessage,
}: {
  result: AgentResult | null
  events: ProgressEvent[]
  sources: Source[]
  activeConversation: Conversation | null
  currentMessage: string
}) {
  const cost = estimateCost(events)
  const latestTurn = activeConversation?.turns.at(-1) || null
  const timeMs = result?.latency_ms || estimateDurationMs(events, latestTurn)
  return {
    title: activeConversation?.title || titleFromMessage(currentMessage),
    turns: String(activeConversation?.turnCount || activeConversation?.turns.length || 0),
    route: result?.route || latestTurn?.route || 'not routed',
    time: timeMs ? formatDuration(timeMs) : activeConversation?.totalLatencyMs ? formatDuration(activeConversation.totalLatencyMs) : 'waiting',
    budget: cost ? `$${cost.toFixed(4)}` : activeConversation?.totalCostUsd ? `$${activeConversation.totalCostUsd.toFixed(4)}` : 'not reported',
    sources: String(sources.length || latestTurn?.sourceCount || activeConversation?.sourceCount || 0),
    events: String(events.length || latestTurn?.events?.length || 0),
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

function mapWorkspace(workspace: ApiWorkspace): Workspace {
  return {
    id: workspace.id,
    name: workspace.name,
    createdAt: workspace.created_at,
    updatedAt: workspace.updated_at,
    conversations: (workspace.conversations || []).map(mapConversation),
  }
}

function mapConversation(conversation: ApiConversation): Conversation {
  return {
    id: conversation.id,
    title: conversation.title,
    createdAt: conversation.created_at,
    updatedAt: conversation.updated_at,
    turns: [],
    turnCount: conversation.turn_count || 0,
    artifactCount: conversation.artifact_count || 0,
    sourceCount: conversation.source_count || 0,
    totalLatencyMs: conversation.total_latency_ms || 0,
    totalCostUsd: conversation.total_cost_usd || 0,
  }
}

function mapTurn(result: AgentResult): WorkItem {
  const message = result?.goal?.objective || ''
  return {
    id: result.turn_id,
    title: titleFromMessage(message || result.answer || result.route),
    route: result.route,
    createdAt: result.created_at || new Date().toISOString(),
    completedAt: result.created_at || undefined,
    message,
    qualityMode: result?.goal?.quality_mode as QualityMode | undefined,
    outputFormat: undefined,
    events: result.events || [],
    result,
    artifacts: result.artifacts || [],
    sourceCount: result.sources?.length || 0,
  }
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
