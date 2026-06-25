'use client'

import { useMemo, useRef, useState } from 'react'
import { readErrorBody } from '../lib/api'
import { draftConversationId, draftWorkspaceId, titleFromMessage, uniqueWorkspaceName } from '../lib/format'
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
  onResetTurn: () => void
  onError: (message: string | null) => void
}

export const INITIAL_VISIBLE_TURNS = 6

export function useWorkspaces(options: WorkspaceOptions) {
  const { authorizedFetch, isRunning, setMessage, onTurnState, onResetTurn, onError } = options
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
  const pendingWorkspaceCreateRef = useRef<Record<string, Promise<Workspace>>>({})

  const activeWorkspace = useMemo(
    () => workspaces.find(workspace => workspace.id === activeWorkspaceId) || workspaces[0] || null,
    [activeWorkspaceId, workspaces],
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
  const canLoadOlder = Boolean(activeConversation && (activeConversation.turnCount || activeTurns.length) > activeTurns.length)
  const latestTurn = activeTurns.at(-1) || null

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
      onTurnState(latest?.result || null, latest?.events || [])
    } finally {
      if (showInitialPlaceholder) {
        setLoadingConversationId(current => current === conversationId ? null : current)
      }
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
      const selectedWorkspace = next.find(
        workspace => workspace.conversations.some(conversation => conversation.id === selectConversationId),
      ) || next[0] || null
      const selectedConversation = selectedWorkspace?.conversations.find(
        conversation => conversation.id === selectConversationId,
      ) || selectedWorkspace?.conversations[0] || null
      setExpandedWorkspaceIds(prev => {
        const nextExpanded = { ...prev }
        if (selectedWorkspace && nextExpanded[selectedWorkspace.id] === undefined) nextExpanded[selectedWorkspace.id] = true
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

  function appendTurn(turn: WorkItem, conversationId: string | null) {
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
      setWorkspaces(prev => prev.map(item => item.id === tempWorkspace.id
        ? { ...workspace, conversations: item.conversations }
        : item))
      setActiveWorkspaceId(current => current === tempWorkspace.id ? workspace.id : current)
      setExpandedWorkspaceIds(prev => {
        const { [tempWorkspace.id]: tempExpanded, ...rest } = prev
        return { ...rest, [workspace.id]: tempExpanded ?? true }
      })
      setEditingWorkspaceId(current => current === tempWorkspace.id ? workspace.id : current)
    } catch (err) {
      setWorkspaces(prev => prev.filter(item => item.id !== tempWorkspace.id))
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
    setWorkspaces(prev => prev.filter(workspace => workspace.id !== workspaceId))
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
      setWorkspaces(previousWorkspaces)
      onError(err instanceof Error ? err.message : 'Could not delete workspace')
    } finally {
      setWorkspaceAction('')
    }
  }

  function createConversation(workspaceId: string, titleOverride?: string) {
    const now = new Date().toISOString()
    const conversation = draftConversation(titleOverride || 'New conversation', now)
    setWorkspaces(prev => prev.map(workspace => workspace.id === workspaceId
      ? {
        ...workspace,
        updatedAt: conversation.updatedAt,
        conversations: [conversation, ...workspace.conversations.filter(item => !item.isDraft)],
      }
      : workspace))
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
      setWorkspaces(prev => prev.map(workspace => workspace.id === workspaceId
        ? { ...workspace, conversations: workspace.conversations.filter(item => item.id !== conversationId) }
        : workspace))
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
    setWorkspaces(prev => prev.map(workspace => workspace.id === workspaceId
      ? {
        ...workspace,
        updatedAt: new Date().toISOString(),
        conversations: workspace.conversations.filter(item => item.id !== conversationId),
      }
      : workspace))
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
      setWorkspaces(previousWorkspaces)
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
    setWorkspaces(prev => prev.map(item => item.id === workspaceId
      ? {
        ...item,
        name: updated.name,
        updatedAt: updated.updatedAt,
        conversations: updated.conversations.map(nextConversation => {
          const existing = item.conversations.find(conversation => conversation.id === nextConversation.id)
          return existing ? { ...nextConversation, turns: existing.turns } : nextConversation
        }),
      }
      : item))
  }

  function loadOlderTurns() {
    const nextCount = visibleTurnCount + INITIAL_VISIBLE_TURNS
    setVisibleTurnCount(nextCount)
    if (activeConversationId) void loadConversationTurns(activeConversationId, nextCount)
  }

  return {
    workspaces,
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
