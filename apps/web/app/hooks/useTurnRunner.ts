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
  const [liveAnswer, setLiveAnswer] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [running, setRunning] = useState(false)
  const eventsRef = useRef<ProgressEvent[]>([])
  const activeRunMessageRef = useRef<string | null>(null)
  // Token smoothing queue: SSE tokens land here without touching React state.
  // A small timer drains the queue at a steady cadence so bursty network/model
  // chunks appear as a continuous rolling response.
  const liveAnswerRef = useRef('')
  const tokenQueueRef = useRef('')
  const streamTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const streamPrimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const streamStartedRef = useRef(false)
  const STREAM_INITIAL_BUFFER_CHARS = 24
  const STREAM_INITIAL_BUFFER_MS = 80
  const STREAM_TICK_MS = 32
  const STREAM_LOW_WATER_THRESHOLD = 30
  const STREAM_LOW_WATER_CHARS = 1
  const STREAM_CHARS_PER_TICK = 2
  const STREAM_CATCHUP_THRESHOLD = 180
  const STREAM_CATCHUP_CHARS = 5
  const STREAM_SURGE_THRESHOLD = 700
  const STREAM_SURGE_CHARS = 14

  const activeEvents = useMemo(
    () => events.filter(event => !['tool_selection', 'tool_result', 'answer_delta', 'answer_complete'].includes(event.stage)),
    [events],
  )
  const canRun = isLoaded && isSignedIn && message.trim().length > 0 && !running

  function setTurnState(nextResult: AgentResult | null, nextEvents: ProgressEvent[] = []) {
    eventsRef.current = nextEvents
    setEvents(nextEvents)
    setResult(nextResult)
    const nextAnswer = nextResult?.answer || ''
    liveAnswerRef.current = nextAnswer
    setLiveAnswer(nextAnswer)
    setError(null)
  }

  function drainTokens() {
    streamTimerRef.current = null
    const pending = tokenQueueRef.current
    if (!pending) return
    const charsPerTick = pending.length > STREAM_SURGE_THRESHOLD
      ? STREAM_SURGE_CHARS
      : pending.length > STREAM_CATCHUP_THRESHOLD
        ? STREAM_CATCHUP_CHARS
        : pending.length < STREAM_LOW_WATER_THRESHOLD
          ? STREAM_LOW_WATER_CHARS
          : STREAM_CHARS_PER_TICK
    const chars = Math.min(pending.length, charsPerTick)
    liveAnswerRef.current += pending.slice(0, chars)
    tokenQueueRef.current = pending.slice(chars)
    setLiveAnswer(liveAnswerRef.current)
    if (tokenQueueRef.current) scheduleTokenDrain()
  }

  function scheduleTokenDrain(delayMs = STREAM_TICK_MS) {
    if (streamTimerRef.current !== null) return
    streamTimerRef.current = setTimeout(drainTokens, delayMs)
  }

  function startTokenDrain() {
    if (streamPrimerRef.current !== null) {
      clearTimeout(streamPrimerRef.current)
      streamPrimerRef.current = null
    }
    streamStartedRef.current = true
    scheduleTokenDrain(0)
  }

  function flushTokenQueue() {
    if (streamTimerRef.current !== null) {
      clearTimeout(streamTimerRef.current)
      streamTimerRef.current = null
    }
    if (streamPrimerRef.current !== null) {
      clearTimeout(streamPrimerRef.current)
      streamPrimerRef.current = null
    }
    const pending = tokenQueueRef.current
    if (!pending) return
    liveAnswerRef.current += pending
    tokenQueueRef.current = ''
    streamStartedRef.current = true
    setLiveAnswer(liveAnswerRef.current)
  }

  function clearStreamState() {
    tokenQueueRef.current = ''
    liveAnswerRef.current = ''
    streamStartedRef.current = false
    if (streamTimerRef.current !== null) {
      clearTimeout(streamTimerRef.current)
      streamTimerRef.current = null
    }
    if (streamPrimerRef.current !== null) {
      clearTimeout(streamPrimerRef.current)
      streamPrimerRef.current = null
    }
  }

  function resetTurnState() {
    clearStreamState()
    setTurnState(null, [])
  }

  function completeTurn(
    next: AgentResult,
    conversationId: string,
    turnMessage: string,
    option?: FollowUpOption,
  ) {
    // Discard any pending tokens — the completed turn's full answer takes precedence.
    clearStreamState()
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
      autoStartResearchPlan: Boolean(next.research_plan_preview),
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
            setError(null)
            // answer_delta / answer_complete only update liveAnswer via applyAnswerProgress.
            // They are already excluded from activeEvents and never displayed in the
            // progress log, so there is no reason to store them in eventsRef or call
            // setEvents — doing so would increment events.length on every token and
            // trigger unrelated effects (e.g. the scroll handler) for every token.
            if (nextEvent.stage === 'answer_delta' || nextEvent.stage === 'answer_complete') {
              applyAnswerProgress(nextEvent)
              continue
            }
            eventsRef.current = [...eventsRef.current, nextEvent]
            setEvents(eventsRef.current)
            applyAnswerProgress(nextEvent)
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
        const polledAnswer = payload.turn.answer || answerFromEvents(nextEvents)
        liveAnswerRef.current = polledAnswer
        setLiveAnswer(polledAnswer)
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

  async function resumeTurn(turnId: string, conversationId: string, turnMessage: string) {
    if (running) return
    setRunning(true)
    activeRunMessageRef.current = turnMessage
    try {
      const streamed = await streamTurnStatus(turnId, conversationId, turnMessage)
      if (!streamed) await pollTurnStatus(turnId, conversationId, turnMessage)
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
    liveAnswer,
    error,
    setError,
    running,
    canRun,
    run,
    resumeTurn,
    activeRunMessage: activeRunMessageRef.current,
    resetTurnState,
    setTurnState,
  }

  function applyAnswerProgress(event: ProgressEvent) {
    if (event.stage === 'answer_complete') {
      flushTokenQueue()
      return
    }
    if (event.stage !== 'answer_delta') return
    const delta = typeof event.data?.delta === 'string' ? event.data.delta : ''
    if (!delta) return
    tokenQueueRef.current += delta
    if (streamStartedRef.current) {
      scheduleTokenDrain()
      return
    }
    if (tokenQueueRef.current.length >= STREAM_INITIAL_BUFFER_CHARS) {
      startTokenDrain()
      return
    }
    if (streamPrimerRef.current === null) {
      streamPrimerRef.current = setTimeout(startTokenDrain, STREAM_INITIAL_BUFFER_MS)
    }
  }

  function answerFromEvents(sourceEvents: ProgressEvent[]) {
    let answer = ''
    for (const event of sourceEvents) {
      if (event.stage !== 'answer_delta') continue
      const delta = typeof event.data?.delta === 'string' ? event.data.delta : ''
      if (delta) answer += delta
    }
    return answer
  }
}
