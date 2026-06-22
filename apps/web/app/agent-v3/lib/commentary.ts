import type { AgentResult, Conversation, ProgressEvent, Source, WorkItem } from '../types'
import { formatDuration, titleFromMessage } from './format'

export function plainCommentaryForEvent(event: ProgressEvent): string | null {
  const data = event.data || {}
  switch (event.stage) {
    case 'route_decision':
    case 'routing':
      return 'I’ve chosen a path for this request and I’m setting up the work.'
    case 'background_job':
      return 'I’ve moved this into a background run so it can keep going safely.'
    case 'connection_recovering':
      return 'The browser connection is reconnecting. The background run is still being checked.'
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

export function plainCommentary(events: ProgressEvent[]): string[] {
  const messages = events
    .filter(event => !['tool_selection', 'tool_result'].includes(event.stage))
    .map(event => plainCommentaryForEvent(event))
    .filter(Boolean) as string[]
  return messages.filter((message, index) => message !== messages[index - 1])
}

export function buildConfidenceCues(events: ProgressEvent[], result: AgentResult | null): string[] {
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

export function eventChips(event: ProgressEvent): string[] {
  const data = event.data || {}
  const chips: string[] = []
  for (const key of ['provider', 'tool_name', 'status', 'route', 'source_count', 'worker_index', 'filename']) {
    const value = data[key]
    if (value !== undefined && value !== null && value !== '') chips.push(`${key.replace('_', ' ')}: ${String(value)}`)
  }
  return chips
}

export function assistantTurnCopyText(turn: WorkItem): string {
  const parts = [turn.result?.answer || '']
  if (turn.artifacts.length) {
    parts.push(`Artifacts:\n${turn.artifacts.map(artifact => `- ${artifact.filename}`).join('\n')}`)
  }
  if (turn.sourceCount) parts.push(`Sources: ${turn.sourceCount}`)
  return parts.filter(Boolean).join('\n\n')
}

export function eventCopyText(event: ProgressEvent): string {
  const parts = [`[${event.stage}] ${event.message}`]
  if (event.created_at) parts.push(`created_at: ${event.created_at}`)
  if (event.data && Object.keys(event.data).length) parts.push(JSON.stringify(event.data, null, 2))
  return parts.join('\n')
}

export function engineEventsCopyText(events: ProgressEvent[]): string {
  if (!events.length) return ''
  return events.map((event, index) => `#${index + 1} ${eventCopyText(event)}`).join('\n\n')
}

export function estimateCost(events: ProgressEvent[]): number {
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

export function estimateDurationMs(events: ProgressEvent[], selectedWork: WorkItem | null): number {
  if (selectedWork?.result?.latency_ms) return selectedWork.result.latency_ms
  const first = events.find(event => event.created_at)?.created_at
  const last = [...events].reverse().find(event => event.created_at)?.created_at
  if (!first || !last) return 0
  const start = new Date(first).getTime()
  const end = new Date(last).getTime()
  return Number.isFinite(start) && Number.isFinite(end) && end > start ? end - start : 0
}

export function buildWorkSummary({
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
