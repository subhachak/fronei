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
