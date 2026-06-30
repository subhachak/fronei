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

export type AdminMaintenanceJob = {
  id: string
  job_type: string
  status: Exclude<AdminJobStatus, 'cancelled'>
  attempt_count: number
  max_attempts: number
  lease_owner: string | null
  lease_expires_at: string | null
  heartbeat_at: string | null
  result: {
    outcome?: 'success' | 'partial_success'
    consolidated?: number
    failed?: number
    skipped?: number
  }
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
    maintenance: {
      queued: number
      running: number
      completed: number
      failed: number
      worker: {
        configured_concurrency: number
        live_threads: number
      }
    }
  }
  items: AdminJob[]
  maintenance_items: AdminMaintenanceJob[]
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
  clerk_authorized_parties_configured: boolean
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
  circuit?: {
    consecutive_failures: number
    open: boolean
    half_open: boolean
    probe_in_flight: boolean
    cooldown_remaining_s: number
  }
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

// ---------------------------------------------------------------------------
// Parity eval types
// ---------------------------------------------------------------------------

export type ParityCaseResult = {
  case_id: string
  legacy_ok: boolean
  langgraph_ok: boolean
  legacy_error: string | null
  langgraph_error: string | null
  legacy_answer_length: number
  langgraph_answer_length: number
  legacy_evidence_count: number
  langgraph_evidence_count: number
  legacy_claim_count: number
  langgraph_claim_count: number
  legacy_judge_verdict: string
  langgraph_judge_verdict: string
  legacy_cost_usd: number
  langgraph_cost_usd: number
  legacy_ms?: number
  langgraph_ms?: number
  answer_length_ratio: number | null
  evidence_count_ratio: number | null
  claim_count_ratio: number | null
  cost_ratio: number | null
  judge_verdict_agrees: boolean | null
  passes_structural_gate: boolean | null
  passes_answer_length_gate: boolean | null
  passes_evidence_gate: boolean | null
  passes_claim_gate: boolean | null
  passes_budget_gate: boolean | null
  overall_pass: boolean
}

export type ParityReport = {
  total_cases: number
  structural_pass: number
  structural_fail: number
  answer_length_gate_pass: number
  evidence_gate_pass: number
  claim_gate_pass: number
  budget_gate_pass: number
  verdict_agree: number
  overall_pass: number
  overall_fail: number
  median_answer_length_ratio: number | null
  median_evidence_count_ratio: number | null
  median_claim_count_ratio: number | null
  median_cost_ratio: number | null
  cutover_recommended: boolean
  cutover_blockers: string[]
  per_case: ParityCaseResult[]
}

export type ParityRunSummary = {
  run_id: string
  status: 'running' | 'complete' | 'error'
  started_at: number
  completed_at: number | null
  cutover_recommended: boolean | null
  overall_pass: number | null
  total_cases: number | null
}

export type OrchestratorStatus = {
  effective_orchestrator: 'legacy' | 'langgraph'
  override_active: boolean
  override_value: string | null
  env_default: string
}

// ---------------------------------------------------------------------------
// General eval case / run types
// ---------------------------------------------------------------------------

export type EvalCase = {
  id: number
  title: string
  query: string
  category: string | null
  expected_criteria: string[]
  expected_primary_role: string | null
  min_independent_sources: number | null
  notes: string | null
  is_active: boolean
  created_by: string | null
  created_at: string | null
  updated_at: string | null
}

export type EvalPipelineResult = {
  ok: boolean
  error: string | null
  answer: string
  answer_length: number
  evidence_count: number
  claim_count: number
  judge_score: number | null
  latency_ms: number
  criteria: {
    score: number | null
    passed: string[]
    failed: string[]
    explanation: string
  } | null
}

export type EvalPipeline = 'langgraph' | 'legacy'

export type EvalCaseRunResult = {
  case_id: number
  title: string
  query: string
  /** Which single pipeline this case ran against — regular evals run one
   *  pipeline graded against expected_criteria (ground truth), not the other
   *  pipeline's output. Use the parity runner to compare legacy vs langgraph. */
  pipeline: EvalPipeline
  run: EvalPipelineResult
  structural: Record<string, boolean>
  overall_structural_pass: boolean
}

export type LangSmithExperiment = {
  mode: 'langsmith'
  dataset_id?: string
  legacy_experiment_url?: string
  langgraph_experiment_url?: string
  pipelines?: Record<string, unknown>
}

/** Consistent envelope returned by /runs/{run_id}/result regardless of eval mode. */
export type EvalRunResult = {
  mode: 'langsmith' | 'in_process' | 'error'
  /** Which single pipeline this run exercised. */
  pipeline: EvalPipeline
  /** Per-case results — populated for in_process runs; empty for LangSmith runs. */
  cases: EvalCaseRunResult[]
  /** LangSmith experiment summary — populated for LangSmith runs; null otherwise. */
  langsmith: LangSmithExperiment | null
}

export type EvalRunSummary = {
  run_id: string
  status: 'running' | 'complete' | 'stopped' | 'error'
  started_by: string | null
  case_count: number
  started_at: string | null
  completed_at: string | null
  error: string | null
  live: boolean
}
