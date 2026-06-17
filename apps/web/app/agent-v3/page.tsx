'use client'

import { useAuth } from '@clerk/nextjs'
import DOMPurify from 'dompurify'
import {
  ArrowUpRight,
  BookOpen,
  CheckCircle2,
  ChevronDown,
  ChevronsLeft,
  ChevronsRight,
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
import { useEffect, useMemo, useRef, useState, type PointerEvent as ReactPointerEvent } from 'react'
import styles from './page.module.css'

const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL || 'http://localhost:8000'
const INITIAL_VISIBLE_TURNS = 6
const MIN_LEFT_RAIL_WIDTH = 220
const MAX_LEFT_RAIL_WIDTH = 420
const MIN_RIGHT_RAIL_WIDTH = 260
const MAX_RIGHT_RAIL_WIDTH = 480
const MIN_COMPOSER_HEIGHT = 152
const MAX_COMPOSER_HEIGHT = 340

type QualityMode = 'draft' | 'standard' | 'executive'
type OutputFormat = 'chat' | 'markdown' | 'docx'
type MobileView = 'work' | 'library' | 'context'
type MobileNavItem = [MobileView, LucideIcon, string]
type PendingDelete =
  | { type: 'workspace'; workspaceId: string }
  | { type: 'conversation'; workspaceId: string; conversationId: string }
  | null

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
  isDraft?: boolean
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
  const [outputFormat, setOutputFormat] = useState<OutputFormat>('chat')
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
  const [expandedWorkspaceIds, setExpandedWorkspaceIds] = useState<Record<string, boolean>>({})
  const [editingWorkspaceId, setEditingWorkspaceId] = useState<string | null>(null)
  const [editingWorkspaceName, setEditingWorkspaceName] = useState('')
  const [pendingDelete, setPendingDelete] = useState<PendingDelete>(null)
  const [leftRailWidth, setLeftRailWidth] = useState(280)
  const [rightRailWidth, setRightRailWidth] = useState(340)
  const [composerHeight, setComposerHeight] = useState(168)
  const [leftRailCollapsed, setLeftRailCollapsed] = useState(false)
  const [rightRailCollapsed, setRightRailCollapsed] = useState(false)
  const eventsRef = useRef<ProgressEvent[]>([])
  const chatScrollRef = useRef<HTMLDivElement | null>(null)
  const activeRunConversationIdRef = useRef<string | null>(null)
  const activeRunMessageRef = useRef<string | null>(null)

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
    setExpandedWorkspaceIds(prev => {
      const nextExpanded = { ...prev }
      if (selectedWorkspace && nextExpanded[selectedWorkspace.id] === undefined) nextExpanded[selectedWorkspace.id] = true
      if (!selectedWorkspace && next[0] && nextExpanded[next[0].id] === undefined) nextExpanded[next[0].id] = true
      return nextExpanded
    })
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
    const runMessage = message.trim()
    if (!runMessage) return
    activeRunMessageRef.current = runMessage
    setEvents([])
    eventsRef.current = []
    setResult(null)
    setError(null)
    setRunning(true)
    setTraceOpen(false)
    setMobileView('work')
    setMessage('')
    try {
      const conversationId = await ensureActiveConversation(runMessage)
      activeRunConversationIdRef.current = conversationId
      const response = await authorizedFetch('/agent-v3/turns/stream', {
        method: 'POST',
        body: JSON.stringify({
          message: runMessage,
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
      activeRunMessageRef.current = null
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
      const turnMessage = activeRunMessageRef.current || message
      setResult(next)
      setTraceOpen(false)
      appendTurnToActiveConversation({
        id: next.turn_id,
        title: titleFromMessage(turnMessage),
        route: next.route,
        createdAt: new Date().toISOString(),
        completedAt: new Date().toISOString(),
        message: turnMessage,
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

  function beginHorizontalResize(kind: 'left' | 'right', event: ReactPointerEvent) {
    event.preventDefault()
    const startX = event.clientX
    const startWidth = kind === 'left' ? leftRailWidth : rightRailWidth
    const onMove = (moveEvent: PointerEvent) => {
      const delta = moveEvent.clientX - startX
      if (kind === 'left') {
        setLeftRailWidth(clamp(startWidth + delta, MIN_LEFT_RAIL_WIDTH, MAX_LEFT_RAIL_WIDTH))
      } else {
        setRightRailWidth(clamp(startWidth - delta, MIN_RIGHT_RAIL_WIDTH, MAX_RIGHT_RAIL_WIDTH))
      }
    }
    const onUp = () => {
      document.removeEventListener('pointermove', onMove)
      document.removeEventListener('pointerup', onUp)
    }
    document.addEventListener('pointermove', onMove)
    document.addEventListener('pointerup', onUp, { once: true })
  }

  function beginComposerResize(event: ReactPointerEvent) {
    event.preventDefault()
    const startY = event.clientY
    const startHeight = composerHeight
    const onMove = (moveEvent: PointerEvent) => {
      setComposerHeight(clamp(startHeight + startY - moveEvent.clientY, MIN_COMPOSER_HEIGHT, MAX_COMPOSER_HEIGHT))
    }
    const onUp = () => {
      document.removeEventListener('pointermove', onMove)
      document.removeEventListener('pointerup', onUp)
    }
    document.addEventListener('pointermove', onMove)
    document.addEventListener('pointerup', onUp, { once: true })
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
    const name = uniqueWorkspaceName('New workspace', workspaces)
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
    setExpandedWorkspaceIds(prev => ({ ...prev, [workspace.id]: true }))
    setEditingWorkspaceId(workspace.id)
    setEditingWorkspaceName(workspace.name)
    createConversation(workspace.id, 'New conversation')
  }

  async function deleteWorkspace(workspaceId: string) {
    if (workspaces.length <= 1) return
    const response = await authorizedFetch(`/agent-v3/workspaces/${workspaceId}`, { method: 'DELETE' })
    if (!response.ok) {
      setError(await response.text() || 'Could not delete workspace')
      return
    }
    setPendingDelete(null)
    await loadWorkspaces()
  }

  function createConversation(workspaceId: string, titleOverride?: string) {
    const now = new Date().toISOString()
    const conversation: Conversation = {
      id: draftConversationId(),
      title: titleOverride || 'New conversation',
      createdAt: now,
      updatedAt: now,
      turns: [],
      isDraft: true,
      turnCount: 0,
      artifactCount: 0,
      sourceCount: 0,
      totalLatencyMs: 0,
      totalCostUsd: 0,
    }
    setWorkspaces(prev => prev.map(workspace => (
      workspace.id === workspaceId
        ? {
          ...workspace,
          updatedAt: conversation.updatedAt,
          conversations: [
            conversation,
            ...workspace.conversations.filter(item => !item.isDraft),
          ],
        }
        : workspace
    )))
    setActiveWorkspaceId(workspaceId)
    setActiveConversationId(conversation.id)
    setExpandedWorkspaceIds(prev => ({ ...prev, [workspaceId]: true }))
    setVisibleTurnCount(INITIAL_VISIBLE_TURNS)
    setMessage('')
    setEvents([])
    eventsRef.current = []
    setResult(null)
  }

  async function deleteConversation(workspaceId: string, conversationId: string) {
    const target = workspaces
      .find(item => item.id === workspaceId)
      ?.conversations.find(item => item.id === conversationId)
    if (target?.isDraft) {
      setPendingDelete(null)
      setWorkspaces(prev => prev.map(workspace => (
        workspace.id === workspaceId
          ? { ...workspace, conversations: workspace.conversations.filter(conversation => conversation.id !== conversationId) }
          : workspace
      )))
      if (activeConversationId === conversationId) {
        const nextConversation = workspaces
          .find(item => item.id === workspaceId)
          ?.conversations.find(item => item.id !== conversationId)
        setActiveConversationId(nextConversation?.id || null)
        eventsRef.current = []
        setEvents([])
        setResult(null)
      }
      return
    }
    const response = await authorizedFetch(`/agent-v3/conversations/${conversationId}`, { method: 'DELETE' })
    if (!response.ok) {
      setError(await response.text() || 'Could not delete conversation')
      return
    }
    setPendingDelete(null)
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

  function toggleWorkspace(workspaceId: string) {
    setExpandedWorkspaceIds(prev => ({ ...prev, [workspaceId]: !prev[workspaceId] }))
  }

  function startEditingWorkspace(workspace: Workspace) {
    setEditingWorkspaceId(workspace.id)
    setEditingWorkspaceName(workspace.name)
    setExpandedWorkspaceIds(prev => ({ ...prev, [workspace.id]: true }))
  }

  async function saveWorkspaceName(workspaceId: string) {
    const workspace = workspaces.find(item => item.id === workspaceId)
    if (!workspace) return
    const name = uniqueWorkspaceName(editingWorkspaceName || workspace.name, workspaces, workspaceId)
    setEditingWorkspaceId(null)
    setEditingWorkspaceName('')
    if (name === workspace.name) return
    const response = await authorizedFetch(`/agent-v3/workspaces/${workspaceId}`, {
      method: 'PATCH',
      body: JSON.stringify({ name }),
    })
    if (!response.ok) {
      setError(await response.text() || 'Could not rename workspace')
      return
    }
    const updated = mapWorkspace(await response.json())
    setWorkspaces(prev => prev.map(item => (
      item.id === workspaceId
        ? { ...item, name: updated.name, updatedAt: updated.updatedAt, conversations: updated.conversations.map(nextConversation => {
          const existing = item.conversations.find(conversation => conversation.id === nextConversation.id)
          return existing ? { ...nextConversation, turns: existing.turns } : nextConversation
        }) }
        : item
    )))
  }

  async function ensureActiveConversation(seedMessage: string): Promise<string> {
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
    if (activeConversation && !activeConversation.isDraft) return activeConversation.id
    const response = await authorizedFetch(`/agent-v3/workspaces/${workspace.id}/conversations`, {
      method: 'POST',
      body: JSON.stringify({ title: titleFromMessage(seedMessage) }),
    })
    if (!response.ok) throw new Error(await response.text() || 'Could not create conversation')
    const conversation = mapConversation(await response.json())
    setWorkspaces(prev => prev.map(item => (
      item.id === workspace.id
        ? {
          ...item,
          updatedAt: conversation.updatedAt,
          conversations: activeConversation?.isDraft
            ? item.conversations.map(existing => existing.id === activeConversation.id ? conversation : existing)
            : [conversation, ...item.conversations],
        }
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
      <div
        className={styles.shell}
        style={{
          gridTemplateColumns: `${leftRailCollapsed ? 56 : leftRailWidth}px minmax(0, 1fr) ${rightRailCollapsed ? 56 : rightRailWidth}px`,
        }}
      >
        <MobileTopBar mobileView={mobileView} setMobileView={setMobileView} running={running} />

        <aside className={`${styles.libraryPane} ${leftRailCollapsed ? styles.railCollapsed : ''} ${mobileView === 'library' ? styles.mobileVisible : styles.mobileHidden}`}>
          {leftRailCollapsed ? (
            <CollapsedRailButton label="Library" icon={Library} onClick={() => setLeftRailCollapsed(false)} />
          ) : (
            <>
              <StudioLibrary
                workspaces={workspaces}
                activeWorkspaceId={activeWorkspace?.id || null}
                activeConversationId={activeConversation?.id || null}
                onCreateWorkspace={createWorkspace}
                onDeleteWorkspace={deleteWorkspace}
                onCreateConversation={createConversation}
                onDeleteConversation={deleteConversation}
                onSelectConversation={selectConversation}
                expandedWorkspaceIds={expandedWorkspaceIds}
                editingWorkspaceId={editingWorkspaceId}
                editingWorkspaceName={editingWorkspaceName}
                onToggleWorkspace={toggleWorkspace}
                onStartEditingWorkspace={startEditingWorkspace}
                onEditingWorkspaceNameChange={setEditingWorkspaceName}
                onSaveWorkspaceName={saveWorkspaceName}
                pendingDelete={pendingDelete}
                onRequestDeleteWorkspace={workspaceId => setPendingDelete({ type: 'workspace', workspaceId })}
                onRequestDeleteConversation={(workspaceId, conversationId) => setPendingDelete({ type: 'conversation', workspaceId, conversationId })}
                onCancelDelete={() => setPendingDelete(null)}
                onCollapse={() => setLeftRailCollapsed(true)}
              />
              <div
                className={`${styles.railResizeHandle} ${styles.railResizeHandleRight}`}
                role="separator"
                aria-label="Resize library rail"
                onPointerDown={event => beginHorizontalResize('left', event)}
              />
            </>
          )}
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
              draftMessage={running ? activeRunMessageRef.current || message : message}
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
          <div className={styles.composerDock} style={{ height: composerHeight }}>
            <div
              className={styles.composerResizeHandle}
              role="separator"
              aria-label="Resize composer"
              onPointerDown={beginComposerResize}
            />
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

        <aside className={`${styles.contextPane} ${rightRailCollapsed ? styles.railCollapsed : ''} ${mobileView === 'context' ? styles.mobileVisible : styles.mobileHidden}`}>
          {rightRailCollapsed ? (
            <CollapsedRailButton label="Context" icon={PanelRight} onClick={() => setRightRailCollapsed(false)} />
          ) : (
            <>
              <div
                className={`${styles.railResizeHandle} ${styles.railResizeHandleLeft}`}
                role="separator"
                aria-label="Resize context rail"
                onPointerDown={event => beginHorizontalResize('right', event)}
              />
              <ContextRail
                result={result}
                events={events}
                sources={sources}
                latestArtifact={latestArtifact}
                activeConversation={activeConversation}
                currentMessage={running ? activeRunMessageRef.current || message : message}
                downloadArtifact={downloadArtifact}
                onCollapse={() => setRightRailCollapsed(true)}
              />
            </>
          )}
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
  expandedWorkspaceIds,
  editingWorkspaceId,
  editingWorkspaceName,
  onToggleWorkspace,
  onStartEditingWorkspace,
  onEditingWorkspaceNameChange,
  onSaveWorkspaceName,
  pendingDelete,
  onRequestDeleteWorkspace,
  onRequestDeleteConversation,
  onCancelDelete,
  onCollapse,
}: {
  workspaces: Workspace[]
  activeWorkspaceId: string | null
  activeConversationId: string | null
  onCreateWorkspace: () => void
  onDeleteWorkspace: (workspaceId: string) => void
  onCreateConversation: (workspaceId: string) => void
  onDeleteConversation: (workspaceId: string, conversationId: string) => void
  onSelectConversation: (workspaceId: string, conversationId: string) => void
  expandedWorkspaceIds: Record<string, boolean>
  editingWorkspaceId: string | null
  editingWorkspaceName: string
  onToggleWorkspace: (workspaceId: string) => void
  onStartEditingWorkspace: (workspace: Workspace) => void
  onEditingWorkspaceNameChange: (value: string) => void
  onSaveWorkspaceName: (workspaceId: string) => void
  pendingDelete: PendingDelete
  onRequestDeleteWorkspace: (workspaceId: string) => void
  onRequestDeleteConversation: (workspaceId: string, conversationId: string) => void
  onCancelDelete: () => void
  onCollapse: () => void
}) {
  return (
    <>
      <div className={styles.sectionHeader}>
        <div>
          <p className={styles.overline}>Studio</p>
          <h1 className={styles.sectionTitle}>Workspaces</h1>
        </div>
        <div className={styles.headerActions}>
          <button type="button" className={styles.smallIconButton} onClick={onCollapse} aria-label="Collapse library" title="Collapse library">
            <ChevronsLeft size={15} />
          </button>
          <button type="button" className={styles.iconButton} onClick={onCreateWorkspace} aria-label="Create workspace" title="Create workspace">
            <Plus size={16} />
          </button>
        </div>
      </div>

      <div className={styles.libraryList}>
        {workspaces.length === 0 && (
          <div className={styles.emptyState}>
            Create a workspace to begin.
          </div>
        )}
        {workspaces.map((workspace, index) => {
          const expanded = expandedWorkspaceIds[workspace.id] ?? index === 0
          const turnCount = workspace.conversations.reduce((total, conversation) => total + (conversation.turnCount || conversation.turns.length), 0)
          return (
            <section key={workspace.id} className={`${styles.workspaceGroup} ${workspace.id === activeWorkspaceId ? styles.workspaceGroupActive : ''}`}>
              <div className={styles.workspaceSummary}>
                <button
                  type="button"
                  className={styles.workspaceToggle}
                  onClick={() => onToggleWorkspace(workspace.id)}
                  aria-label={expanded ? 'Collapse workspace' : 'Expand workspace'}
                  title={expanded ? 'Collapse workspace' : 'Expand workspace'}
                >
                  <ChevronDown size={16} className={expanded ? '' : styles.rotatedClosed} />
                </button>
                <div
                  className={styles.workspaceTitleButton}
                  onClick={() => onStartEditingWorkspace(workspace)}
                  title="Rename workspace"
                >
                  <Folder size={15} />
                  {editingWorkspaceId === workspace.id ? (
                    <input
                      value={editingWorkspaceName}
                      onChange={event => onEditingWorkspaceNameChange(event.target.value)}
                      onBlur={() => onSaveWorkspaceName(workspace.id)}
                      onKeyDown={event => {
                        if (event.key === 'Enter') event.currentTarget.blur()
                        if (event.key === 'Escape') event.currentTarget.blur()
                      }}
                      className={styles.workspaceNameInput}
                      autoFocus
                      onClick={event => event.stopPropagation()}
                    />
                  ) : (
                    <span className={styles.workspaceName}>{workspace.name}</span>
                  )}
                </div>
                <div className={styles.workspaceTileActions}>
                  <button type="button" onClick={() => onCreateConversation(workspace.id)} aria-label="New conversation" title="New conversation">
                    <MessageSquare size={14} />
                  </button>
                  <button type="button" onClick={() => onRequestDeleteWorkspace(workspace.id)} aria-label="Delete workspace" title="Delete workspace" disabled={workspaces.length <= 1}>
                    <Trash2 size={14} />
                  </button>
                </div>
                <span className={styles.workspaceMeta}>{workspace.conversations.length} conv | {turnCount} turns</span>
              </div>
              {pendingDelete?.type === 'workspace' && pendingDelete.workspaceId === workspace.id && (
                <InlineDeleteConfirm
                  title="Delete workspace?"
                  description="All conversations and artifacts inside it will be removed."
                  onCancel={onCancelDelete}
                  onConfirm={() => onDeleteWorkspace(workspace.id)}
                />
              )}
              {expanded && (
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
                        <small>
                          {conversation.isDraft
                            ? 'Draft | not saved yet'
                            : `${conversation.turnCount || conversation.turns.length} turns | ${formatRelativeTime(conversation.updatedAt)}`}
                        </small>
                      </span>
                    </button>
                    <button
                      type="button"
                      className={styles.deleteConversationButton}
                      onClick={() => onRequestDeleteConversation(workspace.id, conversation.id)}
                      aria-label="Delete conversation"
                    >
                      <Trash2 size={13} />
                    </button>
                    {pendingDelete?.type === 'conversation' && pendingDelete.conversationId === conversation.id && (
                      <InlineDeleteConfirm
                        title="Delete conversation?"
                        description="This removes the chat turns and any generated artifacts."
                        onCancel={onCancelDelete}
                        onConfirm={() => onDeleteConversation(workspace.id, conversation.id)}
                      />
                    )}
                  </div>
                ))}
              </div>
              )}
            </section>
          )
        })}
      </div>
    </>
  )
}

function InlineDeleteConfirm({
  title,
  description,
  onCancel,
  onConfirm,
}: {
  title: string
  description: string
  onCancel: () => void
  onConfirm: () => void
}) {
  return (
    <div className={styles.inlineConfirm} role="alertdialog" aria-label={title}>
      <div>
        <p className={styles.inlineConfirmTitle}>{title}</p>
        <p className={styles.inlineConfirmText}>{description}</p>
      </div>
      <div className={styles.inlineConfirmActions}>
        <button type="button" className={styles.inlineCancelButton} onClick={onCancel}>Cancel</button>
        <button type="button" className={styles.inlineDeleteButton} onClick={onConfirm}>Delete</button>
      </div>
    </div>
  )
}

function CollapsedRailButton({ label, icon: Icon, onClick }: { label: string; icon: LucideIcon; onClick: () => void }) {
  return (
    <button type="button" className={styles.collapsedRailButton} onClick={onClick} aria-label={`Expand ${label}`} title={`Expand ${label}`}>
      <Icon size={17} />
      <span>{label}</span>
    </button>
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
        onKeyDown={event => {
          if (event.key === 'Enter' && !event.shiftKey) {
            event.preventDefault()
            if (canRun) run()
          }
        }}
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
        <LiveTurn message={draftMessage} events={events} />
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
    <div className={styles.turnExchange}>
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
    </div>
  )
}

function LiveTurn({ message, events }: { message: string; events: ProgressEvent[] }) {
  return (
    <div className={styles.turnExchange}>
      <div className={styles.userBubble}>
        <p className={styles.bubbleLabel}>You</p>
        <p className={styles.userText}>{message}</p>
      </div>
      <div className={styles.assistantBubble}>
        <div className={styles.assistantHeader}>
          <span className={styles.companionMark}><Sparkles size={16} /></span>
          <div>
            <p className={styles.companionTitle}>Fronei</p>
            <p className={styles.companionText}>I’ll keep you posted in plain English while the work runs.</p>
          </div>
        </div>
        <RollingCommentary events={events} />
      </div>
    </div>
  )
}

function RollingCommentary({ events }: { events: ProgressEvent[] }) {
  const visibleEvents = plainCommentary(events).slice(-6)
  return (
    <div className={styles.rollingLog}>
      {visibleEvents.length === 0 && (
        <div className={styles.rollingEvent}>
          <span className={styles.liveDot} />
          <div>
            <p className={styles.rollingMessage}>I’m getting oriented and deciding the best way to handle this.</p>
          </div>
        </div>
      )}
      {visibleEvents.map((message, index) => (
        <div key={`${message}-${index}`} className={styles.rollingEvent}>
          <span className={styles.liveDot} />
          <div>
            <p className={styles.rollingMessage}>{message}</p>
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
  onCollapse,
}: {
  result: AgentResult | null
  events: ProgressEvent[]
  sources: Source[]
  latestArtifact?: Artifact
  activeConversation: Conversation | null
  currentMessage: string
  downloadArtifact: (artifact: Artifact) => void | Promise<void>
  onCollapse: () => void
}) {
  const providerEvents = events.filter(event => event.stage === 'search_worker_provider')
  const workSummary = buildWorkSummary({ result, events, sources, activeConversation, currentMessage })
  return (
    <>
      <div className={styles.sectionHeaderPlain}>
        <div className={styles.sectionHeader}>
          <div>
            <p className={styles.overline}>Context</p>
            <h2 className={styles.sectionTitle}>Current work</h2>
          </div>
          <button type="button" className={styles.smallIconButton} onClick={onCollapse} aria-label="Collapse context" title="Collapse context">
            <ChevronsRight size={15} />
          </button>
        </div>
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

function plainCommentary(events: ProgressEvent[]): string[] {
  const messages = events
    .filter(event => !['tool_selection', 'tool_result'].includes(event.stage))
    .map(event => plainCommentaryForEvent(event))
    .filter(Boolean) as string[]
  return messages.filter((message, index) => message !== messages[index - 1])
}

function plainCommentaryForEvent(event: ProgressEvent): string | null {
  const data = event.data || {}
  switch (event.stage) {
    case 'route_decision':
    case 'routing':
      return 'I’ve chosen a path for this request and I’m setting up the work.'
    case 'research_planner':
    case 'query_decomposition':
      return 'I’m breaking the question into focused research angles.'
    case 'search_worker_provider': {
      const provider = typeof data.provider === 'string' ? data.provider : ''
      return provider ? `I’m checking the web with ${provider}.` : 'I’m checking the web for current information.'
    }
    case 'source_selection': {
      const count = data.unique_count || data.source_count
      return count ? `I found ${String(count)} useful source candidates to work from.` : 'I’m narrowing the source list to the most useful material.'
    }
    case 'source_reader':
      return 'I’m reading the strongest sources now.'
    case 'evidence_binder': {
      const count = data.evidence_count || data.item_count
      return count ? `I’ve pulled out ${String(count)} evidence points that look useful.` : 'I’m turning the source material into usable evidence.'
    }
    case 'synthesis':
      return 'I’m drafting the answer from the evidence.'
    case 'document_planner':
      return 'I’m shaping the document structure before writing.'
    case 'document_writer':
      return 'I’m writing the main content now.'
    case 'artifact_builder':
    case 'document_artifact':
      return 'I’m packaging the finished work into a downloadable file.'
    case 'document_judge_result':
    case 'judge':
      return 'I’m doing a quality pass before handing it back.'
    case 'repair':
    case 'repair_loop':
      return 'I found something to improve, so I’m tightening it up.'
    case 'complete':
    case 'result':
      return 'The work is ready.'
    default:
      if (/search/i.test(event.stage)) return 'I’m checking the web for current information.'
      if (/source/i.test(event.stage)) return 'I’m reviewing source material.'
      if (/document|artifact/i.test(event.stage)) return 'I’m preparing the work product.'
      if (/judge|quality|verify/i.test(event.stage)) return 'I’m checking the quality before finishing.'
      return event.message && !/[{}_[\]]/.test(event.message) ? event.message : 'I’m making progress on the task.'
  }
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

function draftConversationId(): string {
  const random = typeof crypto !== 'undefined' && 'randomUUID' in crypto
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(16).slice(2)}`
  return `draft-${random}`
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value))
}

function uniqueWorkspaceName(baseName: string, workspaces: Workspace[], excludeWorkspaceId?: string): string {
  const base = baseName.replace(/\s+/g, ' ').trim() || 'New workspace'
  const existing = new Set(
    workspaces
      .filter(workspace => workspace.id !== excludeWorkspaceId)
      .map(workspace => workspace.name.toLowerCase()),
  )
  if (!existing.has(base.toLowerCase())) return base
  for (let index = 2; index < 1000; index += 1) {
    const candidate = `${base} ${index}`
    if (!existing.has(candidate.toLowerCase())) return candidate
  }
  return `${base} ${Date.now().toString().slice(-4)}`
}
