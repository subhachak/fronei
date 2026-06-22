export type QualityMode = 'draft' | 'standard' | 'executive'
export type OutputFormat = 'chat' | 'markdown' | 'docx' | 'pptx'
export type ResearchLevel = 'auto' | 'easy' | 'regular' | 'deep'
export type MobileView = 'work' | 'library' | 'context'

export type PendingDelete =
  | { type: 'workspace'; workspaceId: string }
  | { type: 'conversation'; workspaceId: string; conversationId: string }
  | null

export type AttachedFile = {
  name: string
  text: string
  charCount: number
  truncated: boolean
}

export type DocumentTemplateOption = {
  id: string
  name: string
  description?: string
  recommended?: boolean
  user_template?: boolean
  design_mode?: string
  design_system?: string
}

export type ProgressEvent = {
  stage: string
  message: string
  data?: Record<string, unknown>
  created_at?: string
}

export type Artifact = {
  id?: string
  filename: string
  mime_type: string
  base64_data?: string
  download_url?: string
  size_bytes?: number
}

export type Source = {
  title?: string
  url?: string
  snippet?: string
  content?: string
}

export type ResearchPlanPreview = {
  title?: string
  goal?: string
  audience?: string
  research_profile?: string
  research_level?: string
  output_format?: OutputFormat
  estimated_duration?: string
  workflow?: Array<{ label?: string; description?: string }>
  investigate?: string[]
  source_strategy?: string[]
  workers?: Array<{ question?: string; query?: string; rationale?: string; max_results?: number }>
  coverage?: { subjects?: string[]; dimensions?: string[]; required_cells?: number }
  budget?: Record<string, unknown>
  fallback_reasons?: string[]
}

export type FollowUpOption = {
  label: string
  message?: string
  force_route?: string
  research_level?: ResearchLevel
  confirm_deep_research?: boolean
  output_format?: OutputFormat
}

export type AgentResult = {
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
  follow_up_options?: FollowUpOption[]
  research_plan_preview?: ResearchPlanPreview | null
  created_at?: string
}

export type AgentTurnStatus = {
  turn_id: string
  status: 'running' | 'completed' | 'failed' | string
  error_message?: string | null
  turn: AgentResult
}

export type WorkItem = {
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

export type Conversation = {
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

export type Workspace = {
  id: string
  name: string
  createdAt: string
  updatedAt: string
  conversations: Conversation[]
}

export type ApiConversation = {
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

export type ApiWorkspace = {
  id: string
  name: string
  created_at: string
  updated_at: string
  conversations: ApiConversation[]
}

export type ProfileSettings = {
  quality_mode?: QualityMode
  output_format?: OutputFormat
  research_level?: ResearchLevel
}

export type ProfileMe = {
  user_id: string
  email?: string | null
  name?: string | null
  preferences: string[]
  preferences_updated_at?: string | null
  settings: ProfileSettings
}

export type ProfileWorkspace = {
  id: string
  name: string
  priorities: string[]
  priorities_updated_at?: string | null
  conversation_count: number
  turn_count: number
  total_cost_usd: number
  last_active_at?: string | null
  created_at: string
}

export type ProfileUsageSummary = {
  total_cost: number
  requests: number
  failed_requests: number
  failure_rate: number
  avg_latency_ms: number
  p95_latency_ms: number
  active_days: number
}

export type ProfileUsage = {
  range: string
  summary: ProfileUsageSummary
  cost_by_day: { date: string; cost: number; requests: number }[]
  route_distribution: { route: string; count: number }[]
  model_performance: {
    model: string
    requests: number
    cost: number
    avg_latency_ms: number
    p95_latency_ms: number
    failure_count: number
  }[]
}
