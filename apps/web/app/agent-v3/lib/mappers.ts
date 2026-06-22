import type { AgentResult, ApiConversation, ApiWorkspace, Conversation, QualityMode, Workspace, WorkItem } from '../types'
import { titleFromMessage } from './format'

export function mapWorkspace(workspace: ApiWorkspace): Workspace {
  return {
    id: workspace.id,
    name: workspace.name,
    createdAt: workspace.created_at,
    updatedAt: workspace.updated_at,
    conversations: (workspace.conversations || []).map(mapConversation),
  }
}

export function mapConversation(conversation: ApiConversation): Conversation {
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

export function mapTurn(result: AgentResult): WorkItem {
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
