export type AuthorizedFetch = (path: string, init?: RequestInit) => Promise<Response>

export type AdminOverview = {
  users: number
  requests_today: number
  spend_today: number
  errors_today: number
  running_research_runs: number
  total_conversations: number
  total_memories: number
  total_writing_samples: number
  total_research_runs: number
}

export type AdminJobStatus = 'queued' | 'running' | 'completed' | 'failed' | 'cancelled'

export type AdminJob = {
  id: string
  user_id: string
  email: string | null
  name: string | null
  conversation_id: string | null
  objective: string
  route: string
  quality_mode: string
  status: AdminJobStatus
  attempt_count: number
  max_attempts: number
  lease_owner: string | null
  lease_expires_at: string | null
  heartbeat_at: string | null
  cancel_requested: boolean
  model_used: string
  latency_ms: number
  cost_usd: number
  error_message: string | null
  created_at: string | null
  updated_at: string | null
  completed_at: string | null
}

export type AdminJobsResponse = {
  summary: {
    queued: number
    running: number
    completed: number
    failed: number
    cancelled: number
    stale_leases: number
    retried_jobs: number
    retry_exhausted: number
    oldest_queued_at: string | null
    worker: {
      configured_concurrency: number
      live_threads: number
    }
  }
  items: AdminJob[]
  total: number
  limit: number
  offset: number
}

export type UserStatus = 'active' | 'pending' | 'suspended'
export type UserRole = 'user' | 'admin'

export type AdminUserRow = {
  user_id: string
  email: string | null
  name: string | null
  status: UserStatus
  role: UserRole
  monthly_budget_usd: number | null
  month_spend: number
  conversation_count: number
  request_count: number
  total_spend: number
  memory_count: number
  writing_sample_count: number
  research_run_count: number
  last_seen_at: string | null
}

export type AdminUsersResponse = {
  items: AdminUserRow[]
  total: number
  limit: number
  offset: number
}

export type AdminUserDetail = {
  user_id: string
  email: string | null
  name: string | null
  control: {
    status: UserStatus
    role: UserRole
    monthly_budget_usd: number | null
    notes: string | null
    updated_at: string | null
  }
  month_spend: number
  counts: {
    conversations: number
    messages: number
    memories: number
    user_profiles: number
    writing_samples: number
    twin_profiles: number
    research_runs: number
  }
  recent_conversations: Array<{ id: string; title: string; profile: string; message_count: number; updated_at: string | null }>
  recent_research_runs: Array<{ id: string; query: string; mode: string; status: string; source_count: number; claim_count: number; confidence: number | null; updated_at: string | null }>
  recent_errors: Array<{ id: string; created_at: string | null; task_type: string; selected_model: string; error: string }>
}

export type AdminUsage = {
  range: string
  summary: { total_cost: number; requests: number; tokens: number; users: number }
  cost_by_day: Array<{ date: string; cost: number; requests: number }>
  top_users: Array<{ user_id: string; email: string | null; name: string | null; cost: number; requests: number }>
  model_usage: Array<{ model: string; cost: number; requests: number; avg_latency_ms: number }>
  task_distribution: Array<{ task_type: string; count: number }>
}

export type AdminSystem = {
  app_env: string
  database: 'sqlite' | 'postgres'
  allowed_origins: string[]
  default_profile: string
  monthly_budget_usd: number | null
  planner_model: string
  planner_fallback_models: string[]
  clerk_issuer_configured: boolean
  clerk_audience_configured: boolean
  admin_user_ids_configured: number
  admin_emails_configured: number
  sentry_configured: boolean
  structured_logging: boolean
  worker: {
    configured_concurrency: number
    live_threads: number
  }
  artifact_storage_backend: string
  artifact_s3_bucket_configured: boolean
}

export type AdminProvider = {
  name: string
  key: string
  configured: boolean
  key_hint: string | null
  testable: boolean
  circuit?: { consecutive_failures: number; open: boolean; cooldown_remaining_s: number }
}

export type AdminProvidersResponse = {
  providers: AdminProvider[]
  recent_error_counts: Record<string, number>
}

export type ModelPolicy = {
  roles: Record<string, string>
  fallback_models: string[]
  defaults: { roles: Record<string, string>; fallback_models: string[] }
  available_roles: string[]
}
