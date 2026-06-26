'use client'

import { useEffect, useMemo, useRef, useState } from 'react'
import { useFroneiAuth } from '../lib/auth'
import { createApiClient, readErrorBody } from '../lib/api'
import { copyToClipboard } from '../lib/format'
import { useAttachment } from './useAttachment'
import { useProfileSettings } from './useProfileSettings'
import { useTemplates } from './useTemplates'
import { useTurnRunner } from './useTurnRunner'
import { useWorkspaces } from './useWorkspaces'
import type {
  AgentResult,
  Artifact,
  OutputFormat,
  ProgressEvent,
  QualityMode,
  ResearchLevel,
} from '../types'

export function useAgent() {
  const { getToken, isLoaded, isSignedIn } = useFroneiAuth()
  const { authorizedFetch } = useMemo(() => createApiClient(getToken), [getToken])
  const [message, setMessage] = useState('')
  const [qualityMode, setQualityMode] = useState<QualityMode>('standard')
  const [outputFormat, setOutputFormat] = useState<OutputFormat>('chat')
  const [researchLevel, setResearchLevel] = useState<ResearchLevel>('auto')
  const [copiedKey, setCopiedKey] = useState<string | null>(null)
  const [isAdmin, setIsAdmin] = useState(false)
  const [modelOverride, setModelOverride] = useState('')
  const composerSettingsDirtyRef = useRef(false)
  const runningRef = useRef(false)
  const turnStateRef = useRef({
    setTurnState: (_result: AgentResult | null, _events: ProgressEvent[]) => {},
    resetTurnState: () => {},
    setError: (_message: string | null) => {},
  })

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

  const workspaceHook = useWorkspaces({
    authorizedFetch,
    isRunning: () => runningRef.current,
    setMessage,
    onTurnState: (result, events) => turnStateRef.current.setTurnState(result, events),
    onResetTurn: () => turnStateRef.current.resetTurnState(),
    onError: message => turnStateRef.current.setError(message),
  })

  const turnRunner = useTurnRunner({
    authorizedFetch,
    isLoaded,
    isSignedIn: Boolean(isSignedIn),
    message,
    setMessage,
    qualityMode,
    outputFormat,
    researchLevel,
    selectedTemplateId,
    selectedTemplateExists: Boolean(templateHook.selectedTemplateExists),
    attachedFile: attachmentHook.attachedFile,
    clearAttachment: attachmentHook.clearAttachment,
    isAdmin,
    modelOverride,
    ensureActiveConversation: workspaceHook.ensureActiveConversation,
    appendTurn: workspaceHook.appendTurn,
  })
  runningRef.current = turnRunner.running
  turnStateRef.current = {
    setTurnState: turnRunner.setTurnState,
    resetTurnState: turnRunner.resetTurnState,
    setError: turnRunner.setError,
  }

  const latestArtifact = turnRunner.result?.artifacts?.[0] || workspaceHook.latestTurn?.artifacts?.[0]
  const sources = turnRunner.result?.sources || []

  useEffect(() => {
    if (!isLoaded) return
    if (!isSignedIn) {
      workspaceHook.setWorkspacesLoading(false)
      return
    }
    composerSettingsDirtyRef.current = false
    void workspaceHook.loadWorkspaces().catch(err => {
      turnRunner.setError(err instanceof Error ? err.message : 'Could not load Fronei workspaces')
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

  async function downloadArtifact(artifact: Artifact) {
    if (artifact.download_url) {
      const response = await authorizedFetch(withQueryParam(artifact.download_url, 'redirect', 'false'))
      if (!response.ok) {
        turnRunner.setError(await readErrorBody(response, 'Could not download artifact'))
        return
      }
      const contentType = response.headers.get('Content-Type') || ''
      if (contentType.includes('application/json')) {
        const payload = await response.json().catch(() => null) as { download_url?: string } | null
        if (payload?.download_url) {
          triggerUrlDownload(payload.download_url, artifact.filename)
          return
        }
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
    triggerUrlDownload(url, filename)
    window.setTimeout(() => URL.revokeObjectURL(url), 1000)
  }

  function triggerUrlDownload(url: string, filename: string) {
    const link = document.createElement('a')
    link.href = url
    link.download = filename
    link.rel = 'noopener'
    document.body.appendChild(link)
    link.click()
    link.remove()
  }

  function withQueryParam(url: string, key: string, value: string) {
    const separator = url.includes('?') ? '&' : '?'
    return `${url}${separator}${encodeURIComponent(key)}=${encodeURIComponent(value)}`
  }

  async function copyText(value: string, key: string) {
    try {
      const ok = await copyToClipboard(value)
      if (!ok) throw new Error('Clipboard is unavailable')
      setCopiedKey(key)
      window.setTimeout(() => setCopiedKey(current => current === key ? null : current), 1600)
    } catch (err) {
      turnRunner.setError(err instanceof Error ? err.message : 'Could not copy text')
    }
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
    events: turnRunner.events,
    activeEvents: turnRunner.activeEvents,
    result: turnRunner.result,
    liveAnswer: turnRunner.liveAnswer,
    error: turnRunner.error,
    setError: turnRunner.setError,
    running: turnRunner.running,
    workspaces: workspaceHook.workspaces,
    workspacesLoading: workspaceHook.workspacesLoading,
    workspaceAction: workspaceHook.workspaceAction,
    conversationLoading: workspaceHook.conversationLoading,
    activeWorkspace: workspaceHook.activeWorkspace,
    activeConversation: workspaceHook.activeConversation,
    activeConversationId: workspaceHook.activeConversationId,
    visibleTurns: workspaceHook.visibleTurns,
    canLoadOlder: workspaceHook.canLoadOlder,
    loadOlderTurns: workspaceHook.loadOlderTurns,
    latestArtifact,
    sources,
    canRun: turnRunner.canRun,
    run: turnRunner.run,
    expandedWorkspaceIds: workspaceHook.expandedWorkspaceIds,
    editingWorkspaceId: workspaceHook.editingWorkspaceId,
    editingWorkspaceName: workspaceHook.editingWorkspaceName,
    setEditingWorkspaceName: workspaceHook.setEditingWorkspaceName,
    pendingDelete: workspaceHook.pendingDelete,
    setPendingDelete: workspaceHook.setPendingDelete,
    copiedKey,
    copyText,
    downloadArtifact,
    selectConversation: workspaceHook.selectConversation,
    createWorkspace: workspaceHook.createWorkspace,
    deleteWorkspace: workspaceHook.deleteWorkspace,
    createConversation: workspaceHook.createConversation,
    deleteConversation: workspaceHook.deleteConversation,
    toggleWorkspace: workspaceHook.toggleWorkspace,
    startEditingWorkspace: workspaceHook.startEditingWorkspace,
    saveWorkspaceName: workspaceHook.saveWorkspaceName,
    activeRunMessage: turnRunner.activeRunMessage,
    isAdmin,
    modelOverride,
    setModelOverride,
    ...templateHook,
    ...attachmentHook,
    profileSettings: profileHook.profileSettings,
    updateProfileSettings: profileHook.updateProfileSettings,
  }
}
