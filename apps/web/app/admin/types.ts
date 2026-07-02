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

export type LangGraphRunStatus = 'running' | 'paused' | 'resuming' | 'completed' | 'failed' | 'orphaned'

export type LangGraphRunItem = {
  run_id: string
  status: LangGraphRunStatus
  created_at: string | null
  updated_at: string | null
  resumed_at: string | null
  resumed_by: string | null
  turn_id: string | null
  objective: string | null
  user_id: string | null
  pause_reason: string | null
}

export type LangGraphRunsResponse = { items: LangGraphRunItem[] }

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
// General eval case / run types
// ---------------------------------------------------------------------------

export type EvalRoute = 'direct' | 'clarify' | 'research' | 'document' | 'research_document'

export type EvalCase = {
  id: number
  title: string
  query: string
  category: string | null
  expected_criteria: string[]
  expected_primary_role: string | null
  min_independent_sources: number | null
  /** Structured benchmark thresholds — scored deterministically against the
   *  actual run, separate from the LLM-judged expected_criteria above. */
  min_evidence_items: number | null
  min_criteria_score: number | null
  /** Which orchestrator route this query SHOULD resolve to. Null = don't
   *  assert on routing, just grade whatever route the orchestrator picks. */
  expected_route: EvalRoute | null
  /** v2 scoring schema's optional nested sections (routing.expected_gate_fires/
   *  expected_gate_silent, retrieval_requirements, synthesis_requirements,
   *  document_requirements, cost_latency_budget, adversarial_properties,
   *  harness_integrity_checks) — see eval_case_schema.json case_template.
   *  Permissive shape since the schema is still evolving. */
  v2_spec: Record<string, any> | null
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
  independent_source_count: number | null
  judge_score: number | null
  latency_ms: number
  criteria: {
    score: number | null
    passed: string[]
    failed: string[]
    explanation: string
  } | null
}

// New runs are LangGraph-only. Keep "legacy" in the union so historical rows
// persisted before the runtime retirement can still render.
export type EvalPipeline = 'langgraph' | 'legacy'

export type EvalBenchmarkResult = { target: number; actual: number | null; pass: boolean }

export type EvalCaseRunResult = {
  case_id: number
  title: string
  query: string
  /** Which single pipeline this case ran against. New runs are LangGraph-only;
   *  historical rows may still say "legacy". */
  pipeline: EvalPipeline
  /** The route the orchestrator actually picked for this query (no force_route —
   *  the harness lets routing happen for real, so it can catch routing bugs too). */
  route: EvalRoute
  expected_route: EvalRoute | null
  /** null if the case didn't set expected_route (no routing assertion made). */
  route_correct: boolean | null
  /** Two-pass deep-research confirmation gate check — null unless this case's
   *  route required confirmation. Verifies the gate fires correctly (the
   *  unconfirmed first pass returns route=clarify with a real preview)
   *  BEFORE the confirmed second pass (graded under `run`) actually runs
   *  research. See ResearchPlanCard.tsx for the user-facing timed version
   *  of the same "Start research" follow-up this checks for. */
  deep_research_gate: { route: string | null; has_preview: boolean; resumes_research: boolean; pass: boolean; error?: string } | null
  run: EvalPipelineResult
  structural: Record<string, boolean>
  /** Deterministic pass/fail against the case's structured benchmark
   *  thresholds (min_evidence_items, min_independent_sources, min_criteria_score).
   *  Empty if the case has no structured benchmarks defined. */
  benchmarks: Record<string, EvalBenchmarkResult>
  overall_structural_pass: boolean
  overall_benchmark_pass: boolean | null
  /** Which research tier this case resolved to (only meaningful when route
   *  is "research" — null otherwise). */
  research_level: string | null
  /** scoring_spec.md §1.9 — false means the pipeline's judge_score disagreed
   *  structurally with the actual answer (e.g. a confident score against an
   *  empty answer). When false, this case's scores should not be trusted or
   *  averaged into any aggregate — see overall_status. */
  judge_structural_agreement: boolean
  /** Rolls up judge_structural_agreement + structural/benchmark/route/gate
   *  results. "harness_error" takes priority over pass/fail/partial — it
   *  means the result data itself is untrustworthy, not that the product
   *  failed. */
  overall_status: 'pass' | 'fail' | 'partial' | 'harness_error'
  /** scoring_spec.md §2 — true if this case is tagged is_canary in its v2_spec. */
  is_canary: boolean
  /** scoring_spec.md §2 — true if a canary's judge_score fell outside its
   *  expected_judge_score_band; null if not a canary or no judge_score. A
   *  drifted canary on a routine run (not tied to an intentional change)
   *  signals a scoring-pipeline regression, not a product change. */
  canary_drift: boolean | null
  /** scoring_spec.md §1.1-1.8 — independent programmatic/narrow-judge axes,
   *  each null if the case doesn't assert on that axis. Never collapsed
   *  into one number (see scoring_spec.md §0 on why v1's blended
   *  criteria.score hid real defects). */
  scores: {
    route_correct: boolean | null
    gate_correct: boolean | null
    retrieval_completeness: number | null
    retrieval_independence: boolean | null
    latency_pass: boolean
    synthesis_grounding: number | null
    gap_honesty: boolean | null
    conflict_handling: boolean | null
    must_not_recommend_ok: boolean | null
    answer_length_ok: boolean | null
    format_correct: boolean | null
  }
}

/** scoring_spec.md §3 — one axis's pass-rate (bool axes) or mean (float
 *  axes) for a single tier column. null if no cases in that tier asserted
 *  on this axis at all. */
export type EvalDashboardCell = { rate: number; n: number } | { mean: number; n: number } | null

export type EvalDashboardTier =
  | 'direct' | 'clarify' | 'research_easy' | 'research_regular' | 'research_deep'
  | 'document' | 'research_document'

export type EvalDashboardRow = {
  label: string
  by_tier: Record<EvalDashboardTier, EvalDashboardCell>
}

/** GET /admin/evals/runs/{run_id}/dashboard — scoring_spec.md §3/§6. Check
 *  integrity.ok BEFORE trusting `table` at all (the spec's explicit
 *  sequencing requirement) — a harness_error or canary drift means
 *  something in the scoring pipeline itself is suspect for that run, and
 *  harness_error cases are already excluded from `table`'s aggregates. */
export type EvalDashboard = {
  integrity: {
    ok: boolean
    harness_error_count: number
    harness_error_case_ids: number[]
    canary_drift_count: number
    canary_drift_case_ids: number[]
  }
  total_cases: number
  trustworthy_cases: number
  tiers: EvalDashboardTier[]
  table: Record<string, EvalDashboardRow>
}

export type LangSmithExperiment = {
  mode: 'langsmith'
  dataset_id?: string
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
