'use client'

import { useMemo, useRef, useState } from 'react'
import { readErrorBody } from '../lib/api'
import { sleep, streamErrorMessage, titleFromMessage } from '../lib/format'
import { readSse } from '../lib/sse'
import type {
  AgentResult,
  AgentTurnStatus,
  AttachedFile,
  FollowUpOption,
  OutputFormat,
  ProgressEvent,
  QualityMode,
  ResearchLevel,
  WorkItem,
} from '../types'

type AuthorizedFetch = (path: string, init?: RequestInit) => Promise<Response>

type TurnRunnerOptions = {
  authorizedFetch: AuthorizedFetch
  isLoaded: boolean
  isSignedIn: boolean
  message: string
  setMessage: (value: string) => void
  qualityMode: QualityMode
  outputFormat: OutputFormat
  researchLevel: ResearchLevel
  selectedTemplateId: string
  selectedTemplateExists: boolean
  attachedFile: AttachedFile | null
  clearAttachment: () => void
  isAdmin: boolean
  modelOverride: string
  ensureActiveConversation: (seedMessage: string) => Promise<string>
  appendTurn: (turn: WorkItem, conversationId: string | null) => void
}

const TURN_POLL_INTERVAL_MS = 1200
const TURN_POLL_RECOVERY_WINDOW_MS = 20 * 60 * 1000
const TURN_STREAM_RECONNECT_ATTEMPTS = 3

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

export function useTurnRunner(options: TurnRunnerOptions) {
  const {
    authorizedFetch,
    isLoaded,
    isSignedIn,
    message,
    setMessage,
    qualityMode,
    outputFormat,
    researchLevel,
    selectedTemplateId,
    selectedTemplateExists,
    attachedFile,
    clearAttachment,
    isAdmin,
    modelOverride,
    ensureActiveConversation,
    appendTurn,
  } = options
  const [events, setEvents] = useState<ProgressEvent[]>([])
  const [result, setResult] = useState<AgentResult | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [running, setRunning] = useState(false)
  const eventsRef = useRef<ProgressEvent[]>([])
  const activeRunMessageRef = useRef<string | null>(null)

  const activeEvents = useMemo(
    () => events.filter(event => !['tool_selection', 'tool_result'].includes(event.stage)),
    [events],
  )
  const canRun = isLoaded && isSignedIn && message.trim().length > 0 && !running

  function setTurnState(nextResult: AgentResult | null, nextEvents: ProgressEvent[] = []) {
    eventsRef.current = nextEvents
    setEvents(nextEvents)
    setResult(nextResult)
    setError(null)
  }

  function resetTurnState() {
    setTurnState(null, [])
  }

  function completeTurn(
    next: AgentResult,
    conversationId: string,
    turnMessage: string,
    option?: FollowUpOption,
  ) {
    const nextEvents = next.events || eventsRef.current
    setTurnState(next, nextEvents)
    appendTurn({
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
      setError(payload.error_message || "I couldn't complete this request. Please try again.")
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
        for await (const streamMessage of readSse(response)) {
          if (streamMessage.id) lastEventId = streamMessage.id
          if (streamMessage.event === 'progress') {
            const progress = JSON.parse(streamMessage.data) as ProgressEvent
            const eventId = progress.event_id || streamMessage.id
            if (eventId && seenEventIds.has(eventId)) continue
            if (eventId) seenEventIds.add(eventId)
            const nextEvent = eventId ? { ...progress, event_id: eventId } : progress
            eventsRef.current = [...eventsRef.current, nextEvent]
            setEvents(eventsRef.current)
            setError(null)
          }
          if (streamMessage.event === 'turn') {
            const payload = JSON.parse(streamMessage.data) as AgentTurnStatus
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

  async function pollTurnStatus(
    turnId: string,
    conversationId: string,
    turnMessage: string,
    option?: FollowUpOption,
  ) {
    let transientFailures = 0
    const startedAt = Date.now()
    while (true) {
      try {
        const response = await authorizedFetch(`/turns/${turnId}/status`)
        if (!response.ok) throw new Error(await readErrorBody(response, 'Could not load turn status'))
        const payload = await response.json() as AgentTurnStatus
        const nextEvents = payload.turn.events || []
        transientFailures = 0
        setError(null)
        eventsRef.current = nextEvents
        setEvents(nextEvents)
        if (applyTerminalStatus(payload, conversationId, turnMessage, option)) return
      } catch {
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

  async function run(option?: FollowUpOption) {
    if (!isLoaded || !isSignedIn || running) return
    const runMessage = (option?.message || message).trim()
    if (!runMessage) return
    const fileForThisTurn = attachedFile
    activeRunMessageRef.current = runMessage
    resetTurnState()
    setRunning(true)
    setMessage('')
    clearAttachment()
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
          template_id: selectedTemplateExists ? selectedTemplateId || undefined : undefined,
          research_level: option?.research_level || researchLevel,
          confirm_deep_research: Boolean(option?.confirm_deep_research),
          force_route: option?.force_route || undefined,
          model_overrides: modelOverrides,
          attachment_context: attachmentContext,
        }),
      })
      if (!response.ok) throw new Error(await readErrorBody(response, 'Fronei job could not start'))
      const started = await response.json() as { turn_id: string; conversation_id: string; status: string }
      const activeConversation = started.conversation_id || conversationId
      const streamed = await streamTurnStatus(started.turn_id, activeConversation, runMessage, option)
      if (!streamed) await pollTurnStatus(started.turn_id, activeConversation, runMessage, option)
    } catch (err) {
      setError(streamErrorMessage(err))
    } finally {
      setRunning(false)
      activeRunMessageRef.current = null
    }
  }

  return {
    events,
    activeEvents,
    result,
    error,
    setError,
    running,
    canRun,
    run,
    activeRunMessage: activeRunMessageRef.current,
    resetTurnState,
    setTurnState,
  }
}
