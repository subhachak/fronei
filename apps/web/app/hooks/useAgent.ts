'use client'

import { useEffect, useMemo, useRef, useState } from 'react'
import { useFroneiAuth } from '../lib/auth'
import { createApiClient, readErrorBody } from '../lib/api'
import { copyToClipboard, draftConversationId, draftWorkspaceId, sleep, streamErrorMessage, titleFromMessage, uniqueWorkspaceName } from '../lib/format'
import { mapConversation, mapTurn, mapWorkspace } from '../lib/mappers'
import { readSse } from '../lib/sse'
import { useAttachment } from './useAttachment'
import { useProfileSettings } from './useProfileSettings'
import { useTemplates } from './useTemplates'
import type {
  AgentResult,
  AgentTurnStatus,
  ApiConversation,
  ApiWorkspace,
  Artifact,
  Conversation,
  FollowUpOption,
  OutputFormat,
  PendingDelete,
  ProgressEvent,
  QualityMode,
  ResearchLevel,
  Workspace,
  WorkItem,
} from '../types'

const INITIAL_VISIBLE_TURNS = 6
const TURN_POLL_INTERVAL_MS = 1200
const TURN_POLL_RECOVERY_WINDOW_MS = 20 * 60 * 1000
const TURN_STREAM_RECONNECT_ATTEMPTS = 3

// Mirrors app/services/agent/model_policy.py:MODEL_ROLES on the backend.
// The per-turn override is admin-only and intentionally blanket: it applies
// the chosen model to every role for this one turn, so "what if this whole
// task ran on Opus" is a single click rather than per-role micromanagement.
// The org-wide default (what everyone else always gets) lives in the
// DB-backed model policy, editable at /admin -> Model policy.
const MODEL_OVERRIDE_ROLES = [
  'fast_router',
  'orchestrator',
  'direct_answer',
  'research_brief',
  'coverage_contract',
  'research_planner',
  'reflection',
  'citation_verifier',
  'repair',
  'document_planner',
  'document_writer',
  'synthesis',
  'synthesis_executive',
] as const

export function useAgent() {
  const { getToken, isLoaded, isSignedIn } = useFroneiAuth()
  const { authorizedFetch } = useMemo(() => createApiClient(getToken), [getToken])

  const [message, setMessage] = useState('')
  const [qualityMode, setQualityMode] = useState<QualityMode>('standard')
  const [outputFormat, setOutputFormat] = useState<OutputFormat>('chat')
  const [researchLevel, setResearchLevel] = useState<ResearchLevel>('auto')
  const [events, setEvents] = useState<ProgressEvent[]>([])
  const [result, setResult] = useState<AgentResult | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [running, setRunning] = useState(false)
  const [workspaces, setWorkspaces] = useState<Workspace[]>([])
  const [workspacesLoading, setWorkspacesLoading] = useState(true)
  const [workspaceAction, setWorkspaceAction] = useState('')
  const [loadingConversationId, setLoadingConversationId] = useState<string | null>(null)
  const [activeWorkspaceId, setActiveWorkspaceId] = useState<string | null>(null)
  const [activeConversationId, setActiveConversationId] = useState<string | null>(null)
  const [visibleTurnCount, setVisibleTurnCount] = useState(INITIAL_VISIBLE_TURNS)
  const [expandedWorkspaceIds, setExpandedWorkspaceIds] = useState<Record<string, boolean>>({})
  const [editingWorkspaceId, setEditingWorkspaceId] = useState<string | null>(null)
  const [editingWorkspaceName, setEditingWorkspaceName] = useState('')
  const [pendingDelete, setPendingDelete] = useState<PendingDelete>(null)
  const [copiedKey, setCopiedKey] = useState<string | null>(null)
  const [isAdmin, setIsAdmin] = useState(false)
  const [modelOverride, setModelOverride] = useState('')

  const eventsRef = useRef<ProgressEvent[]>([])
  const activeRunMessageRef = useRef<string | null>(null)
  const composerSettingsDirtyRef = useRef(false)
  const pendingWorkspaceCreateRef = useRef<Record<string, Promise<Workspace>>>({})

  // ── Sub-hooks (extracted from this file — see TD-06 in BACKLOG.md) ────────

  const templateHook = useTemplates({ authorizedFetch })
  const { selectedTemplateId, setSelectedTemplateId } = templateHook

  const attachmentHook = useAttachment({ authorizedFetch })

  const profileHook = useProfileSettings({
    authorizedFetch,
    onQualityModeChange: setQualityMode,
    onOutputFormatChange: setOutputFormat,
    onResearchLevelChange: setResearchLevel,
    onDefaultTemplateChange: setSelectedTemplateId,
    composerSettingsDirtyRef,
  })

  const canRun = useMemo(() => isLoaded && isSignedIn && message.trim().length > 0 && !running, [isLoaded, isSignedIn, message, running])
  const activeEvents = useMemo(() => events.filter(event => !['tool_selection', 'tool_result'].includes(event.stage)), [events])
  const activeWorkspace = useMemo(() => workspaces.find(workspace => workspace.id === activeWorkspaceId) || workspaces[0] || null, [activeWorkspaceId, workspaces])
  const activeConversation = useMemo(
    () => activeWorkspace?.conversations.find(conversation => conversation.id === activeConversationId) || activeWorkspace?.conversations[0] || null,
    [activeConversationId, activeWorkspace],
  )
  const activeTurns = activeConversation?.turns || []
  const conversationLoading = Boolean(loadingConversationId && activeConversationId === loadingConversationId && activeTurns.length === 0)
  const visibleTurns = activeTurns.slice(Math.max(0, activeTurns.length - visibleTurnCount))
  const canLoadOlder = Boolean(activeConversation && (activeConversation.turnCount || activeTurns.length) > activeTurns.length)
  const latestTurn = activeTurns.at(-1) || null
  const latestArtifact = result?.artifacts?.[0] || latestTurn?.artifacts?.[0]
  const sources = result?.sources || []

  useEffect(() => {
    if (!isLoaded) return
    if (!isSignedIn) {
      setWorkspacesLoading(false)
      return
    }
    composerSettingsDirtyRef.current = false
    void loadWorkspaces().catch(err => {
      setError(err instanceof Error ? err.message : 'Could not load Agent v3 workspaces')
    })
    void templateHook.loadTemplates()
    void checkIsAdmin()
    void attachmentHook.loadSupportedAttachmentTypes()
    void profileHook.loadProfileSettings()
  }, [isLoaded, isSignedIn])

  function updateQualityMode(mode: QualityMode) {
    composerSettingsDirtyRef.current = true
    setQualityMode(mode)
  }

  function updateOutputFormat(format: OutputFormat) {
    composerSettingsDirtyRef.current = true
    setOutputFormat(format)
  }

  function updateResearchLevel(level: ResearchLevel) {
    composerSettingsDirtyRef.current = true
    setResearchLevel(level)
  }

  async function checkIsAdmin() {
    try {
      const response = await authorizedFetch('/admin/me')
      setIsAdmin(response.ok)
    } catch {
      setIsAdmin(false)
    }
  }

  async function loadWorkspaces(selectConversationId?: string) {
    setWorkspacesLoading(true)
    try {
      const response = await authorizedFetch('/workspaces')
      if (!response.ok) throw new Error(await readErrorBody(response, 'Could not load workspaces'))
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
      if (selectedConversation) setLoadingConversationId(selectedConversation.id)
      setActiveWorkspaceId(selectedWorkspace?.id || null)
      setActiveConversationId(selectedConversation?.id || null)
      if (selectedConversation) await loadConversationTurns(selectedConversation.id, INITIAL_VISIBLE_TURNS)
    } finally {
      setWorkspacesLoading(false)
    }
  }

  async function loadConversationTurns(conversationId: string, limit = visibleTurnCount) {
    const showInitialPlaceholder = limit <= INITIAL_VISIBLE_TURNS
    if (showInitialPlaceholder) setLoadingConversationId(conversationId)
    try {
      const response = await authorizedFetch(`/conversations/${conversationId}/turns?limit=${limit}`)
      if (!response.ok) throw new Error(await readErrorBody(response, 'Could not load conversation turns'))
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
    } finally {
      if (showInitialPlaceholder) {
        setLoadingConversationId(current => current === conversationId ? null : current)
      }
    }
  }

  async function ensureActiveConversation(seedMessage: string): Promise<string> {
    let workspace = activeWorkspace
    if (workspace?.isDraft) {
      const pendingWorkspace = pendingWorkspaceCreateRef.current[workspace.id]
      workspace = pendingWorkspace ? await pendingWorkspace : workspace
    }
    if (!workspace) {
      const response = await authorizedFetch('/workspaces', { method: 'POST', body: JSON.stringify({ name: 'Personal workspace' }) })
      if (!response.ok) throw new Error(await readErrorBody(response, 'Could not create workspace'))
      workspace = mapWorkspace({ ...(await response.json()), conversations: [] })
      setWorkspaces(prev => [workspace as Workspace, ...prev])
      setActiveWorkspaceId(workspace.id)
    }
    if (activeConversation && !activeConversation.isDraft) return activeConversation.id
    const response = await authorizedFetch(`/workspaces/${workspace.id}/conversations`, {
      method: 'POST',
      body: JSON.stringify({ title: titleFromMessage(seedMessage) }),
    })
    if (!response.ok) throw new Error(await readErrorBody(response, 'Could not create conversation'))
    const conversation = mapConversation(await response.json())
    setWorkspaces(prev => prev.map(item => (
      item.id === workspace!.id
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
    setWorkspaces(prev => prev.map(workspace => {
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
    }))
  }

  async function run(option?: FollowUpOption) {
    if (!isLoaded || !isSignedIn || running) return
    const runMessage = (option?.message || message).trim()
    if (!runMessage) return
    const fileForThisTurn = attachmentHook.attachedFile
    activeRunMessageRef.current = runMessage
    setEvents([])
    eventsRef.current = []
    setResult(null)
    setError(null)
    setRunning(true)
    setMessage('')
    attachmentHook.clearAttachment()
    try {
      const conversationId = await ensureActiveConversation(runMessage)
      const modelOverrides = isAdmin && modelOverride
        ? Object.fromEntries(MODEL_OVERRIDE_ROLES.map(role => [role, modelOverride]))
        : undefined
      const attachmentContext = fileForThisTurn
        ? `Attached file: ${fileForThisTurn.name}\n\n${fileForThisTurn.text}`
        : undefined
      const response = await authorizedFetch('/turns', {
        method: 'POST',
        body: JSON.stringify({
          message: runMessage,
          conversation_id: conversationId,
          quality_mode: qualityMode,
          output_format: option?.output_format || outputFormat,
          template_id: templateHook.selectedTemplateExists ? selectedTemplateId || undefined : undefined,
          research_level: option?.research_level || researchLevel,
          confirm_deep_research: Boolean(option?.confirm_deep_research),
          force_route: option?.force_route || undefined,
          model_overrides: modelOverrides,
          attachment_context: attachmentContext,
        }),
      })
      if (!response.ok) throw new Error(await readErrorBody(response, 'Agent v3 job could not start'))
      const started = await response.json() as { turn_id: string; conversation_id: string; status: string }
      const activeConversation = started.conversation_id || conversationId
      const streamed = await streamTurnStatus(started.turn_id, activeConversation, runMessage, option)
      if (!streamed) {
        await pollTurnStatus(started.turn_id, activeConversation, runMessage, option)
      }
    } catch (err) {
      setError(streamErrorMessage(err))
    } finally {
      setRunning(false)
      activeRunMessageRef.current = null
    }
  }

  function completeTurn(
    next: AgentResult,
    conversationId: string,
    turnMessage: string,
    option?: FollowUpOption,
  ) {
    const nextEvents = next.events || eventsRef.current
    eventsRef.current = nextEvents
    setEvents(nextEvents)
    setResult(next)
    appendTurnToActiveConversation({
      id: next.turn_id,
      title: titleFromMessage(turnMessage),
      route: next.route,
      createdAt: next.created_at || new Date().toISOString(),
      completedAt: new Date().toISOString(),
      message: turnMessage,
      qualityMode,
      outputFormat: option?.output_format || outputFormat,
      events: nextEvents,
      result: next,
      artifacts: next.artifacts || [],
      sourceCount: next.sources?.length || 0,
    }, conversationId)
  }

  function applyTerminalStatus(
    payload: AgentTurnStatus,
    conversationId: string,
    turnMessage: string,
    option?: FollowUpOption,
  ): boolean {
    if (payload.status === 'completed') {
      completeTurn(payload.turn, conversationId, turnMessage, option)
      return true
    }
    if (payload.status === 'failed') {
      setError(payload.error_message || 'Agent v3 failed')
      return true
    }
    if (payload.status === 'cancelled') {
      setError('This turn was cancelled.')
      return true
    }
    return false
  }

  async function streamTurnStatus(
    turnId: string,
    conversationId: string,
    turnMessage: string,
    option?: FollowUpOption,
  ): Promise<boolean> {
    let lastEventId = ''
    const seenEventIds = new Set(
      eventsRef.current.map(event => event.event_id).filter((value): value is string => Boolean(value)),
    )
    for (let attempt = 0; attempt < TURN_STREAM_RECONNECT_ATTEMPTS; attempt += 1) {
      try {
        const response = await authorizedFetch(`/turns/${turnId}/stream`, {
          headers: lastEventId ? { 'Last-Event-ID': lastEventId } : {},
        })
        if (!response.ok) throw new Error(await readErrorBody(response, 'Could not stream turn updates'))
        for await (const message of readSse(response)) {
          if (message.id) lastEventId = message.id
          if (message.event === 'progress') {
            const progress = JSON.parse(message.data) as ProgressEvent
            const eventId = progress.event_id || message.id
            if (eventId && seenEventIds.has(eventId)) continue
            if (eventId) seenEventIds.add(eventId)
            const nextEvent = eventId ? { ...progress, event_id: eventId } : progress
            eventsRef.current = [...eventsRef.current, nextEvent]
            setEvents(eventsRef.current)
            setError(null)
          }
          if (message.event === 'turn') {
            const payload = JSON.parse(message.data) as AgentTurnStatus
            return applyTerminalStatus(payload, conversationId, turnMessage, option)
          }
        }
        throw new Error('Turn update stream ended before completion.')
      } catch {
        if (attempt + 1 >= TURN_STREAM_RECONNECT_ATTEMPTS) return false
        const recoveringEvent: ProgressEvent = {
          stage: 'connection_recovering',
          message: 'The browser connection is reconnecting while Fronei keeps working in the background.',
          data: { ephemeral: true, failure_count: attempt + 1, turn_id: turnId },
          created_at: new Date().toISOString(),
        }
        eventsRef.current = [...eventsRef.current.filter(event => event.stage !== 'connection_recovering'), recoveringEvent]
        setEvents(eventsRef.current)
        await sleep(Math.min(5000, 750 * 2 ** attempt))
      }
    }
    return false
  }

  async function pollTurnStatus(turnId: string, conversationId: string, turnMessage: string, option?: FollowUpOption) {
    let transientFailures = 0
    const startedAt = Date.now()
    while (true) {
      try {
        const response = await authorizedFetch(`/turns/${turnId}/status`)
        if (!response.ok) throw new Error(await readErrorBody(response, 'Could not load turn status'))
        const payload = await response.json() as AgentTurnStatus
        const next = payload.turn
        const nextEvents = next.events || []
        transientFailures = 0
        setError(null)
        eventsRef.current = nextEvents
        setEvents(nextEvents)
        if (applyTerminalStatus(payload, conversationId, turnMessage, option)) return
      } catch (err) {
        transientFailures += 1
        const elapsed = Date.now() - startedAt
        const recoveringEvent: ProgressEvent = {
          stage: 'connection_recovering',
          message: 'The browser connection is reconnecting while Fronei keeps working in the background.',
          data: { ephemeral: true, failure_count: transientFailures, turn_id: turnId },
          created_at: new Date().toISOString(),
        }
        eventsRef.current = [...eventsRef.current.filter(event => event.stage !== 'connection_recovering'), recoveringEvent]
        setEvents(eventsRef.current)
        setError(null)
        if (elapsed >= TURN_POLL_RECOVERY_WINDOW_MS) {
          throw new Error(`I could not reconnect to this background job after ${Math.round(TURN_POLL_RECOVERY_WINDOW_MS / 60000)} minutes. Reopen this conversation to check whether it completed.`)
        }
        await sleep(Math.min(10000, TURN_POLL_INTERVAL_MS * Math.max(1, transientFailures)))
        continue
      }
      await sleep(TURN_POLL_INTERVAL_MS)
    }
  }

  async function selectConversation(workspaceId: string, conversationId: string) {
    if (running) return
    const workspace = workspaces.find(item => item.id === workspaceId)
    const conversation = workspace?.conversations.find(item => item.id === conversationId)
    if (conversation) setLoadingConversationId(conversationId)
    setActiveWorkspaceId(workspaceId)
    setActiveConversationId(conversationId)
    setVisibleTurnCount(INITIAL_VISIBLE_TURNS)
    setMessage('')
    eventsRef.current = []
    setEvents([])
    setResult(null)
    setError(null)
    if (conversation) await loadConversationTurns(conversationId, INITIAL_VISIBLE_TURNS)
  }

  async function createWorkspace() {
    const name = uniqueWorkspaceName('New workspace', workspaces.map(workspace => workspace.name))
    const now = new Date().toISOString()
    const tempWorkspace: Workspace = {
      id: draftWorkspaceId(),
      name,
      createdAt: now,
      updatedAt: now,
      conversations: [{
        id: draftConversationId(),
        title: 'New conversation',
        createdAt: now,
        updatedAt: now,
        turns: [],
        isDraft: true,
        turnCount: 0,
        artifactCount: 0,
        sourceCount: 0,
        totalLatencyMs: 0,
        totalCostUsd: 0,
      }],
      isDraft: true,
    }
    setError(null)
    setWorkspaceAction('Saving workspace...')
    setWorkspaces(prev => [tempWorkspace, ...prev])
    setActiveWorkspaceId(tempWorkspace.id)
    setActiveConversationId(tempWorkspace.conversations[0].id)
    setExpandedWorkspaceIds(prev => ({ ...prev, [tempWorkspace.id]: true }))
    setEditingWorkspaceId(tempWorkspace.id)
    setEditingWorkspaceName(tempWorkspace.name)

    const createPromise = (async () => {
      const response = await authorizedFetch('/workspaces', { method: 'POST', body: JSON.stringify({ name }) })
      if (!response.ok) throw new Error(await readErrorBody(response, 'Could not create workspace'))
      return mapWorkspace({ ...(await response.json()), conversations: tempWorkspace.conversations })
    })()
    pendingWorkspaceCreateRef.current[tempWorkspace.id] = createPromise

    try {
      const workspace = await createPromise
      setWorkspaces(prev => prev.map(item => (
        item.id === tempWorkspace.id
          ? { ...workspace, conversations: item.conversations }
          : item
      )))
      setActiveWorkspaceId(current => current === tempWorkspace.id ? workspace.id : current)
      setExpandedWorkspaceIds(prev => {
        const { [tempWorkspace.id]: tempExpanded, ...rest } = prev
        return { ...rest, [workspace.id]: tempExpanded ?? true }
      })
      setEditingWorkspaceId(current => current === tempWorkspace.id ? workspace.id : current)
    } catch (err) {
      setWorkspaces(prev => prev.filter(item => item.id !== tempWorkspace.id))
      setActiveWorkspaceId(current => current === tempWorkspace.id ? (workspaces[0]?.id || null) : current)
      setActiveConversationId(current => current === tempWorkspace.conversations[0].id ? (activeConversationId || null) : current)
      setError(err instanceof Error ? err.message : 'Could not create workspace')
    } finally {
      delete pendingWorkspaceCreateRef.current[tempWorkspace.id]
      setWorkspaceAction('')
    }
  }

  async function deleteWorkspace(workspaceId: string) {
    if (workspaces.length <= 1) return
    const previousWorkspaces = workspaces
    const deletedWorkspace = workspaces.find(workspace => workspace.id === workspaceId)
    const nextWorkspace = workspaces.find(workspace => workspace.id !== workspaceId) || null
    setError(null)
    setWorkspaceAction('Deleting workspace...')
    setPendingDelete(null)
    setWorkspaces(prev => prev.filter(workspace => workspace.id !== workspaceId))
    if (activeWorkspaceId === workspaceId) {
      setActiveWorkspaceId(nextWorkspace?.id || null)
      setActiveConversationId(nextWorkspace?.conversations[0]?.id || null)
      eventsRef.current = []
      setEvents([])
      setResult(null)
    }
    if (deletedWorkspace?.isDraft) {
      delete pendingWorkspaceCreateRef.current[workspaceId]
      setWorkspaceAction('')
      return
    }
    try {
      const response = await authorizedFetch(`/workspaces/${workspaceId}`, { method: 'DELETE' })
      if (!response.ok) throw new Error(await readErrorBody(response, 'Could not delete workspace'))
    } catch (err) {
      setWorkspaces(previousWorkspaces)
      setError(err instanceof Error ? err.message : 'Could not delete workspace')
    } finally {
      setWorkspaceAction('')
    }
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
        ? { ...workspace, updatedAt: conversation.updatedAt, conversations: [conversation, ...workspace.conversations.filter(item => !item.isDraft)] }
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
    const target = workspaces.find(item => item.id === workspaceId)?.conversations.find(item => item.id === conversationId)
    if (target?.isDraft) {
      setPendingDelete(null)
      setWorkspaces(prev => prev.map(workspace => (
        workspace.id === workspaceId
          ? { ...workspace, conversations: workspace.conversations.filter(conversation => conversation.id !== conversationId) }
          : workspace
      )))
      if (activeConversationId === conversationId) {
        const nextConversation = workspaces.find(item => item.id === workspaceId)?.conversations.find(item => item.id !== conversationId)
        setActiveConversationId(nextConversation?.id || null)
        eventsRef.current = []
        setEvents([])
        setResult(null)
      }
      return
    }
    const previousWorkspaces = workspaces
    setError(null)
    setWorkspaceAction('Deleting conversation...')
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
      if (nextConversation) {
        void loadConversationTurns(nextConversation.id, INITIAL_VISIBLE_TURNS).catch(err => {
          setError(err instanceof Error ? err.message : 'Could not load conversation turns')
        })
      } else {
        eventsRef.current = []
        setEvents([])
        setResult(null)
      }
    }
    try {
      const response = await authorizedFetch(`/conversations/${conversationId}`, { method: 'DELETE' })
      if (!response.ok) throw new Error(await readErrorBody(response, 'Could not delete conversation'))
    } catch (err) {
      setWorkspaces(previousWorkspaces)
      setError(err instanceof Error ? err.message : 'Could not delete conversation')
    } finally {
      setWorkspaceAction('')
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
    const name = uniqueWorkspaceName(editingWorkspaceName || workspace.name, workspaces.filter(item => item.id !== workspaceId).map(item => item.name))
    setEditingWorkspaceId(null)
    setEditingWorkspaceName('')
    if (name === workspace.name) return
    const response = await authorizedFetch(`/workspaces/${workspaceId}`, { method: 'PATCH', body: JSON.stringify({ name }) })
    if (!response.ok) {
      setError(await readErrorBody(response, 'Could not rename workspace'))
      return
    }
    const updated = mapWorkspace(await response.json())
    setWorkspaces(prev => prev.map(item => (
      item.id === workspaceId
        ? {
          ...item,
          name: updated.name,
          updatedAt: updated.updatedAt,
          conversations: updated.conversations.map(nextConversation => {
            const existing = item.conversations.find(conversation => conversation.id === nextConversation.id)
            return existing ? { ...nextConversation, turns: existing.turns } : nextConversation
          }),
        }
        : item
    )))
  }

  async function downloadArtifact(artifact: Artifact) {
    if (artifact.download_url) {
      const response = await authorizedFetch(artifact.download_url)
      if (!response.ok) {
        setError(await readErrorBody(response, 'Could not download artifact'))
        return
      }
      triggerDownload(await response.blob(), artifact.filename)
      return
    }
    if (!artifact.base64_data) return
    const byteString = atob(artifact.base64_data)
    const bytes = new Uint8Array(byteString.length)
    for (let i = 0; i < byteString.length; i += 1) bytes[i] = byteString.charCodeAt(i)
    triggerDownload(new Blob([bytes], { type: artifact.mime_type }), artifact.filename)
  }

  function triggerDownload(blob: Blob, filename: string) {
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = filename
    link.click()
    URL.revokeObjectURL(url)
  }

  async function copyText(value: string, key: string) {
    try {
      const ok = await copyToClipboard(value)
      if (!ok) throw new Error('Clipboard is unavailable')
      setCopiedKey(key)
      window.setTimeout(() => setCopiedKey(current => current === key ? null : current), 1600)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not copy text')
    }
  }

  function loadOlderTurns() {
    const nextCount = visibleTurnCount + INITIAL_VISIBLE_TURNS
    setVisibleTurnCount(nextCount)
    if (activeConversationId) void loadConversationTurns(activeConversationId, nextCount)
  }

  return {
    isLoaded,
    isSignedIn,
    message,
    setMessage,
    qualityMode,
    setQualityMode: updateQualityMode,
    outputFormat,
    setOutputFormat: updateOutputFormat,
    researchLevel,
    setResearchLevel: updateResearchLevel,
    events,
    activeEvents,
    result,
    error,
    setError,
    running,
    workspaces,
    workspacesLoading,
    workspaceAction,
    conversationLoading,
    activeWorkspace,
    activeConversation,
    activeConversationId,
    visibleTurns,
    canLoadOlder,
    loadOlderTurns,
    latestArtifact,
    sources,
    canRun,
    run,
    expandedWorkspaceIds,
    editingWorkspaceId,
    editingWorkspaceName,
    setEditingWorkspaceName,
    pendingDelete,
    setPendingDelete,
    copiedKey,
    copyText,
    downloadArtifact,
    selectConversation,
    createWorkspace,
    deleteWorkspace,
    createConversation,
    deleteConversation,
    toggleWorkspace,
    startEditingWorkspace,
    saveWorkspaceName,
    activeRunMessage: activeRunMessageRef.current,
    isAdmin,
    modelOverride,
    setModelOverride,
    // ── delegated to sub-hooks ────────────────────────────────────────────────
    ...templateHook,
    ...attachmentHook,
    profileSettings: profileHook.profileSettings,
    updateProfileSettings: profileHook.updateProfileSettings,
  }
}
