'use client'

import { useEffect, useMemo, useRef, useState } from 'react'
import { readErrorBody } from '../lib/api'
import { appTimestampMs, draftConversationId, draftWorkspaceId, titleFromMessage, uniqueWorkspaceName } from '../lib/format'
import { mapConversation, mapTurn, mapWorkspace } from '../lib/mappers'
import type {
  AgentResult,
  ApiConversation,
  ApiWorkspace,
  Conversation,
  PendingDelete,
  ProgressEvent,
  Workspace,
  WorkItem,
} from '../types'

type AuthorizedFetch = (path: string, init?: RequestInit) => Promise<Response>

type WorkspaceOptions = {
  authorizedFetch: AuthorizedFetch
  isRunning: () => boolean
  setMessage: (value: string) => void
  onTurnState: (result: AgentResult | null, events: ProgressEvent[]) => void
  onResumeRunningTurn?: (turnId: string, conversationId: string, turnMessage: string) => void
  onResetTurn: () => void
  onError: (message: string | null) => void
}

export const INITIAL_VISIBLE_TURNS = 6
const WORKSPACE_CACHE_KEY = 'fronei.workspaceShell.v1'

type WorkspaceCache = {
  workspaces: Workspace[]
  activeWorkspaceId: string | null
  activeConversationId: string | null
  expandedWorkspaceIds: Record<string, boolean>
}

export function useWorkspaces(options: WorkspaceOptions) {
  const { authorizedFetch, isRunning, setMessage, onTurnState, onResumeRunningTurn, onResetTurn, onError } = options
  const [cachedWorkspaceState] = useState<WorkspaceCache | null>(() => readWorkspaceCache())
  const [workspaces, setWorkspaces] = useState<Workspace[]>(() => sortWorkspaces(cachedWorkspaceState?.workspaces || []))
  const [workspacesLoading, setWorkspacesLoading] = useState(true)
  const [workspaceAction, setWorkspaceAction] = useState('')
  const [loadingConversationId, setLoadingConversationId] = useState<string | null>(null)
  const [activeWorkspaceId, setActiveWorkspaceId] = useState<string | null>(cachedWorkspaceState?.activeWorkspaceId || null)
  const [activeConversationId, setActiveConversationId] = useState<string | null>(cachedWorkspaceState?.activeConversationId || null)
  const [visibleTurnCount, setVisibleTurnCount] = useState(INITIAL_VISIBLE_TURNS)
  const [loadingOlderTurns, setLoadingOlderTurns] = useState(false)
  const [expandedWorkspaceIds, setExpandedWorkspaceIds] = useState<Record<string, boolean>>(
    cachedWorkspaceState?.expandedWorkspaceIds || {},
  )
  const [editingWorkspaceId, setEditingWorkspaceId] = useState<string | null>(null)
  const [editingWorkspaceName, setEditingWorkspaceName] = useState('')
  const [pendingDelete, setPendingDelete] = useState<PendingDelete>(null)
  const pendingWorkspaceCreateRef = useRef<Record<string, Promise<Workspace>>>({})
  const sortedWorkspaces = useMemo(() => sortWorkspaces(workspaces), [workspaces])

  const activeWorkspace = useMemo(
    () => sortedWorkspaces.find(workspace => workspace.id === activeWorkspaceId) || sortedWorkspaces[0] || null,
    [activeWorkspaceId, sortedWorkspaces],
  )
  const activeConversation = useMemo(
    () => activeWorkspace?.conversations.find(conversation => conversation.id === activeConversationId)
      || activeWorkspace?.conversations[0]
      || null,
    [activeConversationId, activeWorkspace],
  )
  const activeTurns = activeConversation?.turns || []
  const conversationLoading = Boolean(
    loadingConversationId && activeConversationId === loadingConversationId && activeTurns.length === 0,
  )
  const visibleTurns = activeTurns.slice(Math.max(0, activeTurns.length - visibleTurnCount))
  const canLoadOlder = Boolean(
    activeConversation
      && !loadingOlderTurns
      && (activeConversation.turnCount || activeTurns.length) > activeTurns.length,
  )
  const latestTurn = activeTurns.at(-1) || null

  useEffect(() => {
    if (workspaces.length === 0 && workspacesLoading) return
    writeWorkspaceCache({ workspaces: sortedWorkspaces, activeWorkspaceId, activeConversationId, expandedWorkspaceIds })
  }, [activeConversationId, activeWorkspaceId, expandedWorkspaceIds, sortedWorkspaces, workspaces.length, workspacesLoading])

  async function loadConversationTurns(conversationId: string, limit = visibleTurnCount) {
    const showInitialPlaceholder = limit <= INITIAL_VISIBLE_TURNS
    if (showInitialPlaceholder) setLoadingConversationId(conversationId)
    try {
      const response = await authorizedFetch(`/conversations/${conversationId}/turns?limit=${limit}`)
      if (!response.ok) throw new Error(await readErrorBody(response, 'Could not load conversation turns'))
      const payload = await response.json() as { turns: AgentResult[] }
      const turns = payload.turns.map(mapTurn)
      setWorkspaces(prev => sortWorkspaces(prev.map(workspace => ({
        ...workspace,
        conversations: workspace.conversations.map(conversation => (
          conversation.id === conversationId ? { ...conversation, turns } : conversation
        )),
      }))))
      const latest = turns.at(-1)
      if (latest && (latest.turnStatus === 'running' || latest.turnStatus === 'queued')) {
        // Turn is still in-progress — don't show stale persisted result; resume live polling instead.
        onTurnState(null, [])
        onResumeRunningTurn?.(latest.id, conversationId, latest.message || '')
      } else {
        onTurnState(latest?.result || null, latest?.events || [])
      }
    } finally {
      if (showInitialPlaceholder) {
        setLoadingConversationId(current => current === conversationId ? null : current)
      }
    }
  }

  async function loadWorkspaces(selectConversationId?: string) {
    setWorkspacesLoading(true)

    // If we already know the target conversation (from cache or a caller hint), start
    // fetching its turns immediately in parallel with the workspace list request.
    // This cuts the serial waterfall (GET /workspaces → GET /turns) to a single round-trip.
    const preloadConversationId = selectConversationId || activeConversationId
    const turnsPreload = preloadConversationId
      ? loadConversationTurns(preloadConversationId, INITIAL_VISIBLE_TURNS).catch(() => {})
      : null

    try {
      const response = await authorizedFetch('/workspaces')
      if (!response.ok) throw new Error(await readErrorBody(response, 'Could not load workspaces'))
      const payload = await response.json() as { workspaces: ApiWorkspace[] }
      const next = sortWorkspaces(payload.workspaces.map(mapWorkspace))

      // Merge workspace list into state while preserving any turns already loaded
      // (either from the parallel prefetch above or from the localStorage cache).
      setWorkspaces(prev => {
        const cachedTurns = new Map(
          prev.flatMap(workspace => workspace.conversations.map(conversation => [conversation.id, conversation.turns]))
        )
        return sortWorkspaces(next.map(workspace => ({
          ...workspace,
          conversations: workspace.conversations.map(conversation => ({
            ...conversation,
            turns: cachedTurns.get(conversation.id) || [],
          })),
        })))
      })

      const preferredConversationId = selectConversationId || activeConversationId
      const selectedWorkspace = next.find(
        workspace => workspace.conversations.some(conversation => conversation.id === preferredConversationId),
      ) || next[0] || null
      const selectedConversation = selectedWorkspace?.conversations.find(
        conversation => conversation.id === preferredConversationId,
      ) || selectedWorkspace?.conversations[0] || null

      setExpandedWorkspaceIds(prev => {
        const nextExpanded = { ...prev }
        if (selectedWorkspace && nextExpanded[selectedWorkspace.id] === undefined) nextExpanded[selectedWorkspace.id] = true
        return nextExpanded
      })
      setActiveWorkspaceId(selectedWorkspace?.id || null)
      setActiveConversationId(selectedConversation?.id || null)

      if (selectedConversation) {
        if (selectedConversation.id === preloadConversationId) {
          // Turns already in flight — just await completion; no second request needed.
          await turnsPreload
        } else {
          // Selected conversation differs from what we preloaded (e.g. the cached ID was
          // stale). Load turns for the correct conversation now.
          await loadConversationTurns(selectedConversation.id, INITIAL_VISIBLE_TURNS)
        }
      }
    } finally {
      setWorkspacesLoading(false)
    }
  }

  async function ensureActiveConversation(seedMessage: string): Promise<string> {
    let workspace = activeWorkspace
    if (workspace?.isDraft) {
      const pendingWorkspace = pendingWorkspaceCreateRef.current[workspace.id]
      workspace = pendingWorkspace ? await pendingWorkspace : workspace
    }
    if (!workspace) {
      const response = await authorizedFetch('/workspaces', {
        method: 'POST',
        body: JSON.stringify({ name: 'Personal workspace' }),
      })
      if (!response.ok) throw new Error(await readErrorBody(response, 'Could not create workspace'))
      workspace = mapWorkspace({ ...(await response.json()), conversations: [] })
      setWorkspaces(prev => sortWorkspaces([workspace as Workspace, ...prev]))
      setActiveWorkspaceId(workspace.id)
    }
    if (activeConversation && !activeConversation.isDraft) return activeConversation.id
    const response = await authorizedFetch(`/workspaces/${workspace.id}/conversations`, {
      method: 'POST',
      body: JSON.stringify({ title: titleFromMessage(seedMessage) }),
    })
    if (!response.ok) throw new Error(await readErrorBody(response, 'Could not create conversation'))
    const conversation = mapConversation(await response.json())
    setWorkspaces(prev => sortWorkspaces(prev.map(item => (
      item.id === workspace!.id
        ? {
          ...item,
          updatedAt: conversation.updatedAt,
          conversations: activeConversation?.isDraft
            ? item.conversations.map(existing => existing.id === activeConversation.id ? conversation : existing)
            : [conversation, ...item.conversations],
        }
        : item
    ))))
    setActiveConversationId(conversation.id)
    return conversation.id
  }

  function appendTurn(turn: WorkItem, conversationId: string | null) {
    setWorkspaces(prev => sortWorkspaces(prev.map(workspace => {
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
    })))
  }

  async function selectConversation(workspaceId: string, conversationId: string) {
    if (isRunning()) return
    const workspace = workspaces.find(item => item.id === workspaceId)
    const conversation = workspace?.conversations.find(item => item.id === conversationId)
    if (conversation) setLoadingConversationId(conversationId)
    setActiveWorkspaceId(workspaceId)
    setActiveConversationId(conversationId)
    setVisibleTurnCount(INITIAL_VISIBLE_TURNS)
    setMessage('')
    onResetTurn()
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
      conversations: [draftConversation('New conversation', now)],
      isDraft: true,
    }
    onError(null)
    setWorkspaceAction('Saving workspace...')
    setWorkspaces(prev => sortWorkspaces([tempWorkspace, ...prev]))
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
      setWorkspaces(prev => sortWorkspaces(prev.map(item => item.id === tempWorkspace.id
        ? { ...workspace, conversations: item.conversations }
        : item)))
      setActiveWorkspaceId(current => current === tempWorkspace.id ? workspace.id : current)
      setExpandedWorkspaceIds(prev => {
        const { [tempWorkspace.id]: tempExpanded, ...rest } = prev
        return { ...rest, [workspace.id]: tempExpanded ?? true }
      })
      setEditingWorkspaceId(current => current === tempWorkspace.id ? workspace.id : current)
    } catch (err) {
      setWorkspaces(prev => sortWorkspaces(prev.filter(item => item.id !== tempWorkspace.id)))
      onError(err instanceof Error ? err.message : 'Could not create workspace')
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
    onError(null)
    setWorkspaceAction('Deleting workspace...')
    setPendingDelete(null)
    setWorkspaces(prev => sortWorkspaces(prev.filter(workspace => workspace.id !== workspaceId)))
    if (activeWorkspaceId === workspaceId) {
      setActiveWorkspaceId(nextWorkspace?.id || null)
      setActiveConversationId(nextWorkspace?.conversations[0]?.id || null)
      onResetTurn()
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
      setWorkspaces(sortWorkspaces(previousWorkspaces))
      onError(err instanceof Error ? err.message : 'Could not delete workspace')
    } finally {
      setWorkspaceAction('')
    }
  }

  function createConversation(workspaceId: string, titleOverride?: string) {
    const now = new Date().toISOString()
    const conversation = draftConversation(titleOverride || 'New conversation', now)
    setWorkspaces(prev => sortWorkspaces(prev.map(workspace => workspace.id === workspaceId
      ? {
        ...workspace,
        updatedAt: conversation.updatedAt,
        conversations: [conversation, ...workspace.conversations.filter(item => !item.isDraft)],
      }
      : workspace)))
    setActiveWorkspaceId(workspaceId)
    setActiveConversationId(conversation.id)
    setExpandedWorkspaceIds(prev => ({ ...prev, [workspaceId]: true }))
    setVisibleTurnCount(INITIAL_VISIBLE_TURNS)
    setMessage('')
    onResetTurn()
  }

  async function deleteConversation(workspaceId: string, conversationId: string) {
    const target = workspaces.find(item => item.id === workspaceId)?.conversations.find(
      item => item.id === conversationId,
    )
    if (target?.isDraft) {
      setPendingDelete(null)
      setWorkspaces(prev => sortWorkspaces(prev.map(workspace => workspace.id === workspaceId
        ? { ...workspace, conversations: workspace.conversations.filter(item => item.id !== conversationId) }
        : workspace)))
      if (activeConversationId === conversationId) {
        const next = workspaces.find(item => item.id === workspaceId)?.conversations.find(
          item => item.id !== conversationId,
        )
        setActiveConversationId(next?.id || null)
        onResetTurn()
      }
      return
    }
    const previousWorkspaces = workspaces
    onError(null)
    setWorkspaceAction('Deleting conversation...')
    setPendingDelete(null)
    setWorkspaces(prev => sortWorkspaces(prev.map(workspace => workspace.id === workspaceId
      ? {
        ...workspace,
        updatedAt: new Date().toISOString(),
        conversations: workspace.conversations.filter(item => item.id !== conversationId),
      }
      : workspace)))
    if (activeConversationId === conversationId) {
      const workspace = workspaces.find(item => item.id === workspaceId)
      const next = workspace?.conversations.find(item => item.id !== conversationId)
      setActiveConversationId(next?.id || null)
      if (next) {
        void loadConversationTurns(next.id, INITIAL_VISIBLE_TURNS).catch(err => {
          onError(err instanceof Error ? err.message : 'Could not load conversation turns')
        })
      } else {
        onResetTurn()
      }
    }
    try {
      const response = await authorizedFetch(`/conversations/${conversationId}`, { method: 'DELETE' })
      if (!response.ok) throw new Error(await readErrorBody(response, 'Could not delete conversation'))
    } catch (err) {
      setWorkspaces(sortWorkspaces(previousWorkspaces))
      onError(err instanceof Error ? err.message : 'Could not delete conversation')
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
    const name = uniqueWorkspaceName(
      editingWorkspaceName || workspace.name,
      workspaces.filter(item => item.id !== workspaceId).map(item => item.name),
    )
    setEditingWorkspaceId(null)
    setEditingWorkspaceName('')
    if (name === workspace.name) return
    const response = await authorizedFetch(`/workspaces/${workspaceId}`, {
      method: 'PATCH',
      body: JSON.stringify({ name }),
    })
    if (!response.ok) {
      onError(await readErrorBody(response, 'Could not rename workspace'))
      return
    }
    const updated = mapWorkspace(await response.json())
    setWorkspaces(prev => sortWorkspaces(prev.map(item => item.id === workspaceId
      ? {
        ...item,
        name: updated.name,
        updatedAt: updated.updatedAt,
        conversations: updated.conversations.map(nextConversation => {
          const existing = item.conversations.find(conversation => conversation.id === nextConversation.id)
          return existing ? { ...nextConversation, turns: existing.turns } : nextConversation
        }),
      }
      : item)))
  }

  async function loadOlderTurns() {
    if (!activeConversation || loadingOlderTurns) return
    const firstTurn = activeConversation.turns[0]
    if (!firstTurn) {
      try {
        await loadConversationTurns(activeConversation.id, INITIAL_VISIBLE_TURNS)
      } catch (err) {
        onError(err instanceof Error ? err.message : 'Could not load older turns')
      }
      return
    }
    setLoadingOlderTurns(true)
    try {
      const response = await authorizedFetch(
        `/conversations/${activeConversation.id}/turns?limit=${INITIAL_VISIBLE_TURNS}&before=${encodeURIComponent(firstTurn.id)}`,
      )
      if (!response.ok) throw new Error(await readErrorBody(response, 'Could not load older turns'))
      const payload = await response.json() as { turns: AgentResult[] }
      const olderTurns = payload.turns.map(mapTurn)
      setVisibleTurnCount(count => count + olderTurns.length)
      setWorkspaces(prev => sortWorkspaces(prev.map(workspace => ({
        ...workspace,
        conversations: workspace.conversations.map(conversation => {
          if (conversation.id !== activeConversation.id) return conversation
          const existingIds = new Set(conversation.turns.map(turn => turn.id))
          const uniqueOlderTurns = olderTurns.filter(turn => !existingIds.has(turn.id))
          return { ...conversation, turns: [...uniqueOlderTurns, ...conversation.turns] }
        }),
      }))))
    } catch (err) {
      onError(err instanceof Error ? err.message : 'Could not load older turns')
    } finally {
      setLoadingOlderTurns(false)
    }
  }

  return {
    workspaces: sortedWorkspaces,
    workspacesLoading,
    setWorkspacesLoading,
    workspaceAction,
    conversationLoading,
    activeWorkspace,
    activeConversation,
    activeConversationId,
    visibleTurns,
    canLoadOlder,
    latestTurn,
    expandedWorkspaceIds,
    editingWorkspaceId,
    editingWorkspaceName,
    setEditingWorkspaceName,
    pendingDelete,
    setPendingDelete,
    loadWorkspaces,
    ensureActiveConversation,
    appendTurn,
    selectConversation,
    createWorkspace,
    deleteWorkspace,
    createConversation,
    deleteConversation,
    toggleWorkspace,
    startEditingWorkspace,
    saveWorkspaceName,
    loadOlderTurns,
  }
}

function readWorkspaceCache(): WorkspaceCache | null {
  if (typeof window === 'undefined') return null
  try {
    const raw = window.localStorage.getItem(WORKSPACE_CACHE_KEY)
    if (!raw) return null
    const parsed = JSON.parse(raw) as WorkspaceCache
    if (!Array.isArray(parsed.workspaces)) return null
    return {
      workspaces: parsed.workspaces.map(workspace => ({
        ...workspace,
        conversations: (workspace.conversations || []).map(conversation => ({ ...conversation, turns: [] })),
      })),
      activeWorkspaceId: parsed.activeWorkspaceId || null,
      activeConversationId: parsed.activeConversationId || null,
      expandedWorkspaceIds: parsed.expandedWorkspaceIds || {},
    }
  } catch {
    return null
  }
}

function writeWorkspaceCache(cache: WorkspaceCache) {
  if (typeof window === 'undefined') return
  try {
    window.localStorage.setItem(
      WORKSPACE_CACHE_KEY,
      JSON.stringify({
        ...cache,
        workspaces: cache.workspaces.map(workspace => ({
          ...workspace,
          isDraft: undefined,
          conversations: workspace.conversations.map(conversation => ({
            ...conversation,
            isDraft: undefined,
            // Persist the last INITIAL_VISIBLE_TURNS for the active conversation so it
            // renders immediately on next load (stale-while-revalidate). All other
            // conversations are cached as shells only (turns: []) to keep the payload small.
            turns: conversation.id === cache.activeConversationId
              ? conversation.turns.slice(-INITIAL_VISIBLE_TURNS)
              : [],
          })),
        })),
      }),
    )
  } catch {
    // Best-effort UI cache only.
  }
}

function sortWorkspaces(workspaces: Workspace[]): Workspace[] {
  return [...workspaces]
    .map(workspace => ({
      ...workspace,
      conversations: sortConversations(workspace.conversations || []),
    }))
    .sort((a, b) => compareRecent(b.updatedAt, a.updatedAt) || compareRecent(b.createdAt, a.createdAt) || a.name.localeCompare(b.name))
}

function sortConversations(conversations: Conversation[]): Conversation[] {
  return [...conversations].sort(
    (a, b) => compareRecent(b.updatedAt, a.updatedAt) || compareRecent(b.createdAt, a.createdAt) || a.title.localeCompare(b.title),
  )
}

function compareRecent(left?: string, right?: string) {
  return timestamp(left) - timestamp(right)
}

function timestamp(value?: string) {
  return appTimestampMs(value)
}

function draftConversation(title: string, now: string): Conversation {
  return {
    id: draftConversationId(),
    title,
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
}
