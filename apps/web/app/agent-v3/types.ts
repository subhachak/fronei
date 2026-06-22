export type QualityMode = 'draft' | 'standard' | 'executive'
export type OutputFormat = 'chat' | 'markdown' | 'docx' | 'pptx'
export type ResearchLevel = 'auto' | 'easy' | 'regular' | 'deep'
export type MobileView = 'work' | 'library' | 'context'

export type PendingDelete =
  | { type: 'workspace'; workspaceId: string }
  | { type: 'conversation'; workspaceId: string; conversationId: string }
  | null

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
