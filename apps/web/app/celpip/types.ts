export type Skill = 'listening' | 'reading' | 'writing' | 'speaking'
export type PracticeMode = 'learn' | 'timed' | 'simulation'
export type TestMode = 'full' | 'full_ls' | 'component' | 'custom' | 'diagnostic' | 'single_task'
export type AttemptStatus =
  | 'not_started' | 'in_progress' | 'submitted' | 'evaluating' | 'completed' | 'failed'

export type TaskSpec = {
  key: string
  part: number
  label: string
  question_count: number
  prep_seconds: number
  response_seconds: number
  word_range: [number, number] | null
  speakers: number
  description: string
  allows_answer_change: boolean
}

export type SectionSpec = {
  skill: Skill
  label: string
  limit_seconds: number
  scored_questions: number
  has_practice_task: boolean
  tasks: TaskSpec[]
}

export type Spec = {
  spec_version: string
  rubric_version: string
  sections: SectionSpec[]
  levels: Record<string, string>
  milestones: Record<string, string>
  dimensions: Record<string, string>
  weakness_tags: Record<string, string>
}

export type Profile = {
  test_type: 'general' | 'general_ls'
  test_date: string | null
  target_level: number
  weekday_hours: number
  weekend_hours: number
  self_reported_weaknesses: string[]
  onboarding_state: 'pending' | 'complete' | 'skipped'
  diagnostic_attempt_id: string | null
  components: Skill[]
}

export type ReadinessSignal = {
  score: number
  weight: number
  contribution: number
  detail: Record<string, unknown>
}

export type Readiness = {
  readiness: number
  target_level: number
  test_type: string
  days_until_test: number | null
  component_levels: Record<string, { latest: number | null; history: { at: string; level: number }[]; attempts: number }>
  signals: Record<string, ReadinessSignal>
  biggest_gaps: { signal: string; unclaimed: number }[]
  computed_at: string
}

export type PlanItem = {
  id: string
  scheduled_for: string
  week_index: number
  activity_type: string
  title: string
  rationale: string
  skill: Skill | null
  task_keys: string[]
  weakness_tags: string[]
  lesson_id: string | null
  estimated_minutes: number
  status: 'pending' | 'in_progress' | 'completed' | 'skipped' | 'deferred'
  attempt_id: string | null
  rescheduled: { from: string; to: string | null; reason: string }[]
}

export type HomePayload = {
  profile: Profile
  readiness: Readiness
  today: PlanItem[]
  overdue_count: number
  plan_progress: { completed: number; total: number }
  weakest: { tag: string; label: string; count: number }[]
  weakest_tasks: { task_key: string; label: string }[]
  recent_attempts: {
    attempt_id: string
    status: AttemptStatus
    label: string
    mode: string
    created_at: string
    completed_at: string | null
  }[]
  resume_attempt_id: string | null
  speech_configured: boolean
}

export type Lesson = {
  id: string
  slug: string
  title: string
  category: string
  skill: Skill | null
  task_key: string | null
  summary: string
  weakness_tags: string[]
  estimated_minutes: number
  sort_order: number
  body_markdown?: string
}

export type BankItem = {
  id: string
  skill: Skill
  task_key: string
  label: string
  part: number
  title: string
  topic: string
  difficulty: number
  status: string
  source: string
  times_served: number
  last_served_at: string | null
  approved_at: string | null
  generator_model: string
  validator_model: string
  created_at: string
  payload?: Record<string, any>
  validation?: Record<string, any>
  assets?: AssetPayload
}

export type BankCoverage = {
  task_key: string
  label: string
  skill: Skill
  part: number
  ready: number
}

export type GenerationRun = {
  id: string
  task_key: string
  label: string
  status: string
  requested: number
  accepted: number
  rejected: number
  rejections: { reason: string; detail: string }[]
  error: string | null
  created_at: string
  completed_at: string | null
}

export type AssetPayload = {
  audio: { id: string; segment_index: number; speaker_voice: string; duration_seconds: number; status: string }[]
  image: { id: string; status: string } | null
}

export type SectionState = {
  attempt_id: string
  skill: Skill
  status: AttemptStatus
  started_at: string | null
  deadline_at: string | null
  limit_seconds: number | null
  seconds_remaining: number | null
  expired: boolean
}

export type AttemptState = {
  attempt_id: string
  test_id: string
  label: string
  mode: TestMode
  practice_mode: PracticeMode
  status: AttemptStatus
  current_skill: Skill | null
  components: Skill[]
  sections: Record<string, SectionState>
  expired_sections: string[]
  items: {
    question_id: string
    skill: Skill
    task_key: string
    position: number
    is_practice_task: boolean
    answered: number
  }[]
  flagged: string[]
  started_at: string | null
  submitted_at: string | null
}

export type RunnerQuestion = {
  question_id: string
  skill: Skill
  task_key: string
  part: number
  label: string
  description: string
  prep_seconds: number
  response_seconds: number
  word_range: [number, number] | null
  audio_replays: number
  allows_answer_change: boolean
  stimulus: Record<string, any>
  questions: { index: number; prompt: string; options: Record<string, string>; segment_index: number }[]
  assets: AssetPayload
  responses: Record<string, {
    selected_option: string | null
    response_text: string
    has_audio: boolean
    flagged: boolean
  }>
}

export type Evaluation = {
  id: string
  skill: Skill
  task_key: string
  label: string
  status: string
  level: { low: number | null; high: number | null }
  dimensions: Record<string, number>
  confidence: number
  feedback: {
    summary?: string
    strengths?: string[]
    met_requirements?: string[]
    missing_requirements?: string[]
    corrections?: { severity: string; original: string; corrected: string; why: string }[]
    patterns?: string[]
    outline?: string[]
    disagreements?: { dimension: string; a: number; b: number; resolution?: string }[]
    dimension_comments?: Record<string, { level: number | null; a: any; b: any }>
  }
  delivery_metrics: Record<string, any>
  weakness_tags: string[]
  has_exemplar: boolean
  exemplar: {
    exemplar?: string
    target_level?: number
    changes?: { change: string; why: string }[]
    retry_exercise?: { title: string; instructions: string; time_minutes: number }
  }
  provenance: {
    evaluator_a: string
    evaluator_b: string
    reconciler: string
    rubric_version: string
    scored_at: string | null
  }
  response: {
    id: string
    text: string
    transcript: string
    has_audio: boolean
    duration_seconds: number
  } | null
  error: string | null
}

export type ComponentResult = {
  method: 'deterministic' | 'rubric'
  raw_score?: number
  max_score?: number
  late_excluded?: number
  level: { low: number; high: number; label?: string; note?: string }
  accuracy_by_task?: Record<string, { correct: number; total: number }>
  weakness_tags: string[]
  confidence?: number
  items?: ReceptiveItemReview[]
  rubric_version?: string
}

export type ReceptiveItemReview = {
  question_id: string
  task_key: string
  correct: number
  total: number
  late_excluded?: number
  questions: {
    index: number
    prompt: string
    options: Record<string, string>
    answer: string
    chosen: string | null
    correct: boolean
    answered: boolean
    late?: boolean
    evidence: string
    why_correct: string
    why_others_wrong: Record<string, string>
    time_spent_ms: number
  }[]
}

export type ResultsPayload = {
  attempt_id: string
  label: string
  mode: string
  practice_mode: PracticeMode
  status: AttemptStatus
  submitted_at: string | null
  completed_at: string | null
  components: Record<string, ComponentResult>
  evaluations: Evaluation[]
  error: string | null
  /** Scoring is still in flight, including between a failed run and its retry. */
  evaluation_pending?: boolean
  evaluation_job?: {
    job_id: string
    status: string
    attempt_count: number
    max_attempts: number
    retry_pending: boolean
    active: boolean
    error: string | null
  } | null
}

export type Progress = {
  readiness: Readiness
  component_levels: Readiness['component_levels']
  weaknesses: { tag: string; label: string; count: number }[]
  task_accuracy: { task_key: string; label: string; correct: number; total: number; accuracy: number | null }[]
  task_levels: { task_key: string; label: string; average_level: number; attempts: number }[]
}

export type AuthorizedFetch = (path: string, init?: RequestInit) => Promise<Response>
