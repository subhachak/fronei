'use client'

import { useEffect, useMemo, useRef, useState, type MouseEvent } from 'react'
import Link from 'next/link'
import { marked } from 'marked'
import { markedHighlight } from 'marked-highlight'
import hljs from 'highlight.js'
import DOMPurify from 'dompurify'
import { useAuth, useClerk, useUser } from '@clerk/nextjs'
import {
  Bar, BarChart, CartesianGrid, Cell, Line, LineChart,
  ResponsiveContainer, Tooltip, XAxis, YAxis,
} from 'recharts'
import Sidebar, { type ConversationSummary } from './components/Sidebar'

marked.use(markedHighlight({
  langPrefix: 'hljs language-',
  highlight(code, lang) {
    const language = hljs.getLanguage(lang) ? lang : 'plaintext'
    return hljs.highlight(code, { language }).value
  },
}))

// ── Types ─────────────────────────────────────────────────────────────────────

type Profile = 'cost_saver' | 'balanced' | 'best_quality'
type OutputMode =
  | 'raw' | 'default' | 'client_ready' | 'exec_ready'
  | 'email' | 'proposal' | 'architecture' | 'pushback'
type ResearchMode = 'quick' | 'deep' | 'expert'
type Quality = 'quick' | 'smart' | 'thorough'
type Range = '1d' | '7d' | '30d' | 'all'
type ArtifactType = 'adr' | 'solution_comparison' | 'trade_off_matrix' | 'exec_brief' | 'risk_register' | 'nfr_analysis' | 'steering_update'
type PersonaId = 'enterprise_architect' | 'product_manager' | 'software_engineer' | 'data_scientist' | 'custom'

type DailyStat       = { date: string; cost: number; requests: number }
type ModelUsageStat  = { model: string; requests: number; total_cost: number; avg_latency_ms: number }
type TaskStat        = { task_type: string; count: number }
type ModelDetailStat = {
  model: string; requests: number
  avg_latency_ms: number; p50_latency_ms: number; p95_latency_ms: number
  avg_prompt_tokens: number; avg_completion_tokens: number; total_cost: number
}
type Summary = { total_cost: number; total_requests: number; total_tokens: number; avg_latency_ms: number }
type AnalyticsData = {
  range: string; summary: Summary; cost_by_day: DailyStat[]
  model_usage: ModelUsageStat[]; task_distribution: TaskStat[]; model_stats: ModelDetailStat[]
}

type MemoryItem = {
  id: number
  content: string
  category: string
  scope: string
  confidence: number
  source: string
  seen_count: number
  last_seen_at: string | null
  importance: number
  pinned: boolean
  status: string
  created_at: string
  updated_at: string
}

type MemoryPatch = Partial<Pick<MemoryItem, 'content' | 'category' | 'scope' | 'confidence' | 'pinned' | 'status'>>
type PersonalContextProfile = {
  profile: Record<string, unknown>
  last_consolidated_at: string | null
}

const ARTIFACT_TYPES: { value: ArtifactType; label: string; icon: string; hint: string }[] = [
  { value: 'adr',                 label: 'ADR',             icon: 'ti-gavel',           hint: 'Architecture Decision Record' },
  { value: 'solution_comparison', label: 'Compare',         icon: 'ti-scale',           hint: 'Solution option comparison' },
  { value: 'trade_off_matrix',    label: 'Trade-offs',      icon: 'ti-table',           hint: 'Trade-off matrix' },
  { value: 'exec_brief',          label: 'Exec brief',      icon: 'ti-presentation',    hint: 'Executive briefing' },
  { value: 'risk_register',       label: 'Risk register',   icon: 'ti-alert-triangle',  hint: 'Risk register' },
  { value: 'nfr_analysis',        label: 'NFR analysis',    icon: 'ti-clipboard-check', hint: 'Non-functional requirements' },
  { value: 'steering_update',     label: 'Steering update', icon: 'ti-users',           hint: 'Steering committee update' },
]

const PERSONAS: { id: PersonaId; name: string; artifacts: ArtifactType[] }[] = [
  {
    id: 'enterprise_architect',
    name: 'Enterprise Architect',
    artifacts: ['adr', 'solution_comparison', 'trade_off_matrix', 'exec_brief', 'risk_register', 'nfr_analysis', 'steering_update'],
  },
  {
    id: 'product_manager',
    name: 'Product Manager',
    artifacts: ['exec_brief', 'risk_register', 'steering_update', 'trade_off_matrix'],
  },
  {
    id: 'software_engineer',
    name: 'Software Engineer',
    artifacts: ['adr', 'nfr_analysis', 'trade_off_matrix', 'risk_register'],
  },
  {
    id: 'data_scientist',
    name: 'Data Scientist',
    artifacts: ['exec_brief', 'risk_register', 'trade_off_matrix', 'solution_comparison'],
  },
  {
    id: 'custom',
    name: 'Custom',
    artifacts: [],
  },
]

type WorkbenchAction = {
  icon: string
  title: string
  desc: string
  prompt: string
}

type WorkbenchPersona = {
  kicker: string
  headline: string
  subhead: string
  railLabel: string
  actions: WorkbenchAction[]
}

const WORKBENCH_PERSONAS: Record<PersonaId, WorkbenchPersona> = {
  enterprise_architect: {
    kicker: 'Enterprise architecture workbench',
    headline: 'What are we shaping today?',
    subhead: 'Move from ambiguous asks to decisions, trade-offs, risks, and executive-ready narratives.',
    railLabel: 'EA mode',
    actions: [
      {
        icon: 'ti-microscope',
        title: 'Research brief',
        desc: 'Compare platforms, vendors, policies, and current facts.',
        prompt: 'Create a research brief for ',
      },
      {
        icon: 'ti-gavel',
        title: 'Architecture decision',
        desc: 'Capture recommendation, options, trade-offs, and risks.',
        prompt: 'Draft an architecture decision record for ',
      },
      {
        icon: 'ti-scale',
        title: 'Solution comparison',
        desc: 'Evaluate options across fit, cost, risk, and operability.',
        prompt: 'Compare solution options for ',
      },
      {
        icon: 'ti-alert-triangle',
        title: 'Challenge review',
        desc: 'Stress-test assumptions before they travel.',
        prompt: 'Challenge the assumptions in this architecture approach: ',
      },
      {
        icon: 'ti-presentation',
        title: 'Steering update',
        desc: 'Package decisions for executive attention.',
        prompt: 'Create a steering committee update for ',
      },
    ],
  },
  product_manager: {
    kicker: 'Product strategy workbench',
    headline: 'What product decision are we driving?',
    subhead: 'Turn customer signal, constraints, and roadmap tension into crisp product choices.',
    railLabel: 'PM mode',
    actions: [
      {
        icon: 'ti-target-arrow',
        title: 'Problem framing',
        desc: 'Clarify user pain, success metrics, and decision boundaries.',
        prompt: 'Frame the product problem, target user, success metrics, and constraints for ',
      },
      {
        icon: 'ti-road',
        title: 'Roadmap trade-off',
        desc: 'Compare scope, sequencing, dependency, and customer impact.',
        prompt: 'Analyze the roadmap trade-offs for ',
      },
      {
        icon: 'ti-chart-dots',
        title: 'Market scan',
        desc: 'Research competitors, positioning, and emerging expectations.',
        prompt: 'Create a market and competitor scan for ',
      },
      {
        icon: 'ti-users',
        title: 'Stakeholder brief',
        desc: 'Package the why, what changed, decision, and next step.',
        prompt: 'Draft a stakeholder brief for ',
      },
      {
        icon: 'ti-test-pipe',
        title: 'Experiment plan',
        desc: 'Define hypothesis, test design, guardrails, and readout.',
        prompt: 'Create an experiment plan for ',
      },
    ],
  },
  software_engineer: {
    kicker: 'Engineering workbench',
    headline: 'What should we build or debug?',
    subhead: 'Move from code, incidents, and design questions to implementation-ready decisions.',
    railLabel: 'Eng mode',
    actions: [
      {
        icon: 'ti-code',
        title: 'Implementation plan',
        desc: 'Break a change into files, risks, tests, and rollout steps.',
        prompt: 'Create an implementation plan for ',
      },
      {
        icon: 'ti-bug',
        title: 'Debug path',
        desc: 'Form hypotheses, checks, and likely root causes.',
        prompt: 'Build a debugging plan for ',
      },
      {
        icon: 'ti-git-pull-request',
        title: 'PR review',
        desc: 'Review behavior, edge cases, maintainability, and test gaps.',
        prompt: 'Review this change like a senior engineer: ',
      },
      {
        icon: 'ti-server',
        title: 'System design',
        desc: 'Clarify APIs, data flow, failure modes, and scaling shape.',
        prompt: 'Design the system for ',
      },
      {
        icon: 'ti-clipboard-check',
        title: 'Test strategy',
        desc: 'Map unit, integration, contract, and regression coverage.',
        prompt: 'Create a test strategy for ',
      },
    ],
  },
  data_scientist: {
    kicker: 'Data science workbench',
    headline: 'What signal are we trying to prove?',
    subhead: 'Shape messy questions into evidence, experiments, models, and decision-ready findings.',
    railLabel: 'DS mode',
    actions: [
      {
        icon: 'ti-chart-histogram',
        title: 'Analysis plan',
        desc: 'Define data needs, metrics, slices, and caveats.',
        prompt: 'Create an analysis plan for ',
      },
      {
        icon: 'ti-brain',
        title: 'Model approach',
        desc: 'Compare features, methods, evaluation, and deployment risks.',
        prompt: 'Recommend a modeling approach for ',
      },
      {
        icon: 'ti-flask',
        title: 'Experiment design',
        desc: 'Specify hypothesis, assignment, power, guardrails, and readout.',
        prompt: 'Design an experiment to evaluate ',
      },
      {
        icon: 'ti-database-search',
        title: 'Data quality review',
        desc: 'Find missingness, bias, leakage, drift, and reliability risks.',
        prompt: 'Review the data quality risks for ',
      },
      {
        icon: 'ti-presentation-analytics',
        title: 'Insight brief',
        desc: 'Translate analysis into findings, confidence, and decision impact.',
        prompt: 'Draft an insight brief for ',
      },
    ],
  },
  custom: {
    kicker: 'Personal workbench',
    headline: 'What kind of work are we moving forward?',
    subhead: 'Use your saved voice and artifact choices to shape Fronei around today’s task.',
    railLabel: 'Custom mode',
    actions: [
      {
        icon: 'ti-sparkles',
        title: 'Refine my thinking',
        desc: 'Turn rough notes into clear, direct, usable output.',
        prompt: 'Refine this into my voice and make it useful: ',
      },
      {
        icon: 'ti-microscope',
        title: 'Research this',
        desc: 'Investigate the question, compare sources, and synthesize.',
        prompt: 'Research this deeply: ',
      },
      {
        icon: 'ti-mail',
        title: 'Write from me',
        desc: 'Draft a direct first-person message in your style.',
        prompt: 'Write this as a message from me: ',
      },
      {
        icon: 'ti-alert-triangle',
        title: 'Push back',
        desc: 'Challenge the weak assumptions and sharpen the recommendation.',
        prompt: 'Push back on this and identify weak assumptions: ',
      },
      {
        icon: 'ti-layout-grid',
        title: 'Structure it',
        desc: 'Turn the idea into an organized artifact.',
        prompt: 'Structure this into a useful artifact: ',
      },
    ],
  },
}

const QUALITY_PROFILE: Record<Quality, Profile> = {
  quick:    'cost_saver',
  smart:    'balanced',
  thorough: 'best_quality',
}

function buildRequestFields(
  quality: Quality,
  researchOn: boolean,
  webSearchOn: boolean,
): { profile: Profile; web_search: boolean; deep_research: boolean; research_mode: ResearchMode } {
  if (researchOn) return { profile: 'best_quality', web_search: true,  deep_research: true,  research_mode: 'expert' }
  return             { profile: QUALITY_PROFILE[quality], web_search: webSearchOn, deep_research: false, research_mode: 'quick' }
}

type RouteDecision = {
  task_type: string; complexity: string; profile: string
  primary_model: string; fallbacks: string[]; reason: string
}

type PipelineStage =
  | 'planning' | 'routing' | 'working'
  | 'sub_complete' | 'synthesising' | 'refining'
  | 'searching' | 'reading' | 'extracting' | 'checking' | 'verifying' | 'complete'

type PipelineStep = {
  stage: PipelineStage
  message: string
  ts: number
  route?: RouteDecision
  intent?: string
  turn_type?: string
  sub_queries?: { query: string; task_type: string | null; model_hint: string | null }[]
  queries?: string[]
  idx?: number
  model?: string
  task_type?: string | null
  latency_ms?: number
  cost_usd?: number | null
}

type AttachedDocument = {
  name: string; text: string; char_count: number
  pages_extracted: number; pages_total: number
  truncated: boolean; method: string; text_preview: string
}

type PendingFile = {
  id:   string
  file: File
  name: string
  size: number
}

type ResearchSourceLog = {
  id?: number
  title: string
  url: string
  provider?: string
  credibility_score?: number
  relevance_score?: number
  freshness_score?: number
  source_type?: string | null
}

type ResearchClaimLog = {
  id?: number
  claim: string
  quote?: string | null
  confidence?: string
  relevance_score?: number
  source_id?: number
  source_ref?: string
  source_title?: string | null
  source_url?: string | null
}

type ResearchFindingLog = {
  id?: number
  finding: string
  evidence?: {
    claim_id?: number
    source_id?: number
    source_ref?: string
    source_title?: string | null
    source_url?: string | null
    quote?: string | null
  }[]
  confidence?: string | null
}

type ResearchMeta = {
  run_id: number
  mode: ResearchMode | string
  sources: ResearchSourceLog[]
  claims?: ResearchClaimLog[]
  findings?: ResearchFindingLog[]
  questions: string[]
  gaps: string[]
  contradictions: string[]
  verifier_notes?: string | null
  confidence?: string | null
}

type ResearchRecommendation = {
  confidence: string
  reason: string
  risk_factors: string[]
  suggested_mode: ResearchMode
  source: string
  original_message?: string
  temp_user_id?: number
  temp_asst_id?: number
}

type MessageOut = {
  id: number; role: 'user' | 'assistant'; content: string
  route?: RouteDecision | null; task_type?: string | null
  complexity?: string | null; model_used?: string | null
  latency_ms?: number | null; prompt_tokens?: number | null
  completion_tokens?: number | null; estimated_cost_usd?: number | null
  execution_log?: ExecutionLog | null; created_at: string
  turn_type?: string | null; action?: string | null
  research_run_id?: number | null
  research?: ResearchMeta | null
  research_recommendation?: ResearchRecommendation | null
  attached_files?: { name: string; method: string; pages: number | null }[] | null
  document_preview?: GeneratedDocument | null
}

type GeneratedDocument = {
  title: string
  docType: string
  markdown: string
  filename: string
  docxBase64: string
  outputFormats?: DocumentOutputFormat[]
}

type DocumentOutputFormat = 'docx' | 'markdown'

type DocumentBrief = {
  title: string
  docType: string
  audience: string
  tone: string
  length: string
  outputFormats: DocumentOutputFormat[]
}

type DocumentResearchRecommendation = {
  reason: string
  risk_factors: string[]
  confidence: string
  suggested_mode: ResearchMode
}

type DocumentWebSearchRecommendation = {
  reason: string
  search_query: string
  confidence: string
}

type DocumentPlanCapabilities = {
  deepResearch: boolean
  webSearch: boolean
}

type DocumentPlanRecommendations = {
  deepResearch?: DocumentResearchRecommendation
  webSearch?: DocumentWebSearchRecommendation
}

class DocumentPlanRecommendationError extends Error {
  recommendations: DocumentPlanRecommendations

  constructor(recommendations: DocumentPlanRecommendations) {
    super('Fronei recommends updating the document plan.')
    this.name = 'DocumentPlanRecommendationError'
    this.recommendations = recommendations
  }
}

type ConversationDetail = ConversationSummary & { messages: MessageOut[] }

type WritingSample = {
  id: number; content: string; label: string | null; char_count: number; created_at: string
}

type Fingerprint = {
  sentence_length: string; formality: string; directness: string; hedging: string
  structure: string; technical_depth: string; preferred_phrases: string[]
  forbidden_phrases: string[]; avoid_patterns: string[]; signature_patterns: string[]
  tone_by_audience: Record<string, string>
}

type TwinProfile = {
  user_id: string; fingerprint: Fingerprint | null; rewrite_prompt: string | null
  prefs: Record<string, unknown>; extracted_at: string | null; sample_count: number
}

type RouteOption = { primary: string; fallback?: string[] }
type RoutingPolicy = {
  routes?: Record<string, Record<string, Record<Profile, RouteOption>>>
  default?: Record<Profile, RouteOption>
}

type PlannerSubQuery = { query: string; task_type: string | null; preferred_model: string | null }
type SubQueryLog = {
  query: string; task_type: string | null; model_requested: string | null
  model_used: string; fallback_error: string | null; cost_usd: number | null; latency_ms: number
}
type PlannerLog = {
  model: string; latency_ms: number; cost_usd: number; turn_type: string; action: string
  intent: string; enriched_prompt: string; needs_web_search: boolean; search_query: string | null
  sub_queries: PlannerSubQuery[]; context_summary: string
}
type WebContextLog = {
  enabled: boolean; provider: string; sources_count: number
  search_query: string | null; status: string
}
type WorkerLog = {
  model: string; latency_ms: number; prompt_tokens: number | null
  completion_tokens: number | null; cost_usd: number | null
  sub_queries_count: number; sub_query_logs: SubQueryLog[]
}
type ExecutionLog = {
  planner: PlannerLog; web_context: WebContextLog
  worker: WorkerLog; total_cost_usd: number; total_latency_ms: number
}

type ExecPanelData = {
  execLog: ExecutionLog | null
  route: RouteDecision | null
  model_used: string; latency_ms: number
  estimated_cost_usd?: number | null
  prompt_tokens?: number | null; completion_tokens?: number | null
  task_type?: string | null; complexity?: string | null
} | null

// ── Constants ─────────────────────────────────────────────────────────────────

const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL || 'http://localhost:8000'

const FALLBACK_MODEL_OPTIONS = [
  'claude-sonnet-4-6','claude-opus-4-8','claude-haiku-4-5-20251001',
  'gemini/gemini-2.5-flash','gemini/gemini-2.5-pro',
  'gpt-4.1','gpt-4.1-mini','o3',
  'openrouter/deepseek/deepseek-r1','openrouter/deepseek/deepseek-chat',
  'openrouter/deepseek/deepseek-coder-v2','openrouter/qwen/qwen3-235b-a22b',
  'openrouter/qwen/qwen2.5-max','openrouter/perplexity/sonar','openrouter/perplexity/sonar-pro',
]

const OUTPUT_MODES: { value: OutputMode; label: string; desc: string }[] = [
  { value: 'default',      label: 'Default',       desc: 'Light anti-slop pass'             },
  { value: 'raw',          label: 'Raw',            desc: 'Unrefined model output'           },
  { value: 'client_ready', label: 'Client-ready',   desc: 'Polished, professional'           },
  { value: 'exec_ready',   label: 'Exec-ready',     desc: 'Crisp, outcome-focused'           },
  { value: 'email',        label: 'Email from me',  desc: 'First-person, direct'             },
  { value: 'proposal',     label: 'Proposal',       desc: 'Structured, authoritative'        },
  { value: 'architecture', label: 'Architecture',   desc: 'Technical, trade-offs explicit'   },
  { value: 'pushback',     label: 'Pushback',       desc: 'Critical, challenges assumptions' },
]

const CHART_COLORS = ['#7c3aed','#4f46e5','#0ea5e9','#10b981','#f59e0b','#ef4444','#ec4899']
const RANGES: { label: string; value: Range }[] = [
  { label: 'Today',    value: '1d'  },
  { label: '7 days',   value: '7d'  },
  { label: '30 days',  value: '30d' },
  { label: 'All time', value: 'all' },
]

const TOOLTIP_STYLE = {
  background: 'var(--bg-modal)',
  border: '1px solid var(--bd2)',
  borderRadius: 10,
  fontSize: 12,
  color: 'var(--t2)',
}

type AccentTheme = 'default' | 'classic' | 'electric' | 'arctic' | 'warm'

const ACCENT_THEMES: { id: AccentTheme; name: string; dot: string; bg: string }[] = [
  { id: 'default',  name: 'Default',  dot: '#7c3aed', bg: '#1a1020' },
  { id: 'classic',  name: 'Classic',  dot: '#c9a447', bg: '#111828' },
  { id: 'electric', name: 'Electric', dot: '#00c8f0', bg: '#0c1519' },
  { id: 'arctic',   name: 'Arctic',   dot: '#0ea5e9', bg: '#0c1828' },
  { id: 'warm',     name: 'Warm',     dot: '#d97706', bg: '#181610' },
]

const FOLLOWUPS: Record<string, string[]> = {
  coding:        ['Explain this in more detail', 'Add error handling', 'Write tests for this'],
  architecture:  ['What are the trade-offs?', 'How would this scale?', 'Show me a sequence diagram'],
  writing:       ['Make it more concise', 'Adjust the tone', 'Expand on the key points'],
  research:      ['Go deeper on this', 'Compare the alternatives', 'Summarise for an executive'],
  summarization: ['What are the key takeaways?', 'Expand on the most important point', 'What is missing?'],
  planning:      ['Break this into tasks', 'What could go wrong?', 'What should I do first?'],
  default:       ['Tell me more', 'Give me an example', 'What should I do next?'],
}

function getFollowups(taskType: string | null | undefined): string[] {
  return FOLLOWUPS[taskType ?? ''] ?? FOLLOWUPS.default
}

// Suggest relevant artifact types based on a completed assistant message
function suggestArtifacts(msg: MessageOut): ArtifactType[] {
  const c = msg.content.toLowerCase()
  const t = msg.task_type || ''
  const suggestions: ArtifactType[] = []
  if (c.includes('decision') || c.includes('we will') || c.includes('we recommend') || c.includes('we should adopt'))
    suggestions.push('adr')
  if (c.includes('option') || c.includes('compare') || c.includes(' vs ') || c.includes('versus') || t === 'architecture')
    suggestions.push('solution_comparison')
  if (c.includes('trade-off') || c.includes('pros') || c.includes('cons') || c.includes('advantage') || c.includes('disadvantage'))
    suggestions.push('trade_off_matrix')
  if (c.includes('risk') || c.includes('mitigation') || c.includes('threat') || c.includes('vulnerability'))
    suggestions.push('risk_register')
  if (c.includes('executive') || c.includes('stakeholder') || c.includes('board') || c.includes('c-suite') || c.includes('investment'))
    suggestions.push('exec_brief')
  if (c.includes('performance') || c.includes('availability') || c.includes('scalability') || c.includes('security requirement'))
    suggestions.push('nfr_analysis')
  return [...new Set(suggestions)].slice(0, 3)
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function getProvider(model: string): { name: string; color: string; bg: string } {
  if (model.startsWith('claude'))                        return { name: 'Anthropic', color: '#fb923c', bg: 'rgba(251,146,60,0.12)' }
  if (model.startsWith('gpt') || model === 'o3')        return { name: 'OpenAI',    color: '#34d399', bg: 'rgba(52,211,153,0.12)'  }
  if (model.startsWith('gemini'))                        return { name: 'Google',    color: '#60a5fa', bg: 'rgba(96,165,250,0.12)'  }
  if (model.includes('deepseek'))                        return { name: 'DeepSeek',  color: '#22d3ee', bg: 'rgba(34,211,238,0.12)'  }
  if (model.includes('perplexity'))                      return { name: 'Perplexity',color: '#a78bfa', bg: 'rgba(167,139,250,0.12)' }
  if (model.includes('qwen'))                            return { name: 'Qwen',      color: '#a78bfa', bg: 'rgba(167,139,250,0.12)' }
  return { name: 'OpenRouter', color: '#a78bfa', bg: 'rgba(167,139,250,0.12)' }
}

function extractModelOptions(policy: RoutingPolicy): string[] {
  const models = new Set<string>()
  const add = (r?: RouteOption) => { if (!r) return; models.add(r.primary); r.fallback?.forEach(m => models.add(m)) }
  Object.values(policy.default ?? {}).forEach(add)
  Object.values(policy.routes ?? {}).forEach(tr => Object.values(tr).forEach(cr => Object.values(cr).forEach(add)))
  return [...models].sort()
}

const PROFILE_ORDER: Profile[] = ['cost_saver', 'balanced', 'best_quality']
const PROFILE_LABELS: Record<Profile, string> = {
  cost_saver: 'Cost saver', balanced: 'Balanced', best_quality: 'Best quality',
}
const COMPLEXITY_ORDER = ['low', 'medium', 'high']

function RouteCell({ route }: { route?: RouteOption }) {
  if (!route) return <span className="muted-text">—</span>
  const p = getProvider(route.primary)
  return (
    <div className="route-cell">
      <span className="provider-badge" style={{ background: p.bg, color: p.color }}>{p.name}</span>
      <span className="route-cell-primary">{route.primary}</span>
      {route.fallback && route.fallback.length > 0 && (
        <div className="fallback-list">
          {route.fallback.map(m => <span key={m} className="fallback-item">{m}</span>)}
        </div>
      )}
    </div>
  )
}

function RoutingPolicyMatrix({ policy }: { policy: RoutingPolicy }) {
  const [tier, setTier] = useState<string>('medium')
  const routes = policy.routes ?? {}
  const taskTypes = Object.keys(routes).sort()
  const tiersPresent = COMPLEXITY_ORDER.filter(t => taskTypes.some(tt => routes[tt]?.[t]))
  const activeTier = tiersPresent.includes(tier) ? tier : tiersPresent[0]
  const rows = taskTypes.filter(tt => routes[tt]?.[activeTier])

  return (
    <div className="routing-matrix">
      <div className="settings-tabs" style={{ marginBottom: 10 }}>
        {tiersPresent.map(t => (
          <button
            key={t}
            type="button"
            className={`settings-tab-btn ${activeTier === t ? 'active' : ''}`}
            onClick={() => setTier(t)}
          >
            {t[0].toUpperCase() + t.slice(1)} complexity
          </button>
        ))}
      </div>
      <div className="admin-table-wrap">
        <table className="admin-table routing-table">
          <thead>
            <tr>
              <th>Task type</th>
              {PROFILE_ORDER.map(p => <th key={p}>{PROFILE_LABELS[p]}</th>)}
            </tr>
          </thead>
          <tbody>
            {rows.map(tt => (
              <tr key={tt}>
                <td className="mono">{tt}</td>
                {PROFILE_ORDER.map(p => (
                  <td key={p}><RouteCell route={routes[tt]?.[activeTier]?.[p]} /></td>
                ))}
              </tr>
            ))}
            {rows.length === 0 && (
              <tr><td colSpan={PROFILE_ORDER.length + 1} className="muted-text">No routes defined for this complexity tier.</td></tr>
            )}
          </tbody>
        </table>
      </div>
      {policy.default && (
        <>
          <div className="chart-card-title" style={{ marginTop: 16 }}>Default (no matching route)</div>
          <div className="admin-table-wrap">
            <table className="admin-table routing-table">
              <thead><tr>{PROFILE_ORDER.map(p => <th key={p}>{PROFILE_LABELS[p]}</th>)}</tr></thead>
              <tbody>
                <tr>
                  {PROFILE_ORDER.map(p => <td key={p}><RouteCell route={policy.default?.[p]} /></td>)}
                </tr>
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  )
}

function conversationMarkdown(title: string, msgs: MessageOut[]) {
  const lines: string[] = [`# ${title}\n`]
  for (const m of msgs) {
    lines.push(`## ${m.role === 'user' ? 'You' : 'Assistant'}${m.model_used ? ` (${m.model_used})` : ''}`)
    lines.push(m.content)
    lines.push('')
  }
  return lines.join('\n')
}

function safeDownloadName(title: string, ext: string) {
  const stem = title.replace(/[^a-z0-9]+/gi, '-').replace(/^-+|-+$/g, '').toLowerCase() || 'fronei-document'
  return `${stem}.${ext}`
}

function inferDocumentTitle(prompt: string) {
  const cleaned = prompt
    .replace(/\b(generate|create|write|draft|prepare|build|produce|put together)\b/gi, '')
    .replace(/\b(a|an|the|report|document|doc|proposal|brief|memo|letter|one-pager|one pager)\b/gi, '')
    .replace(/\s+/g, ' ')
    .trim()
  return (cleaned || 'Fronei document').slice(0, 90)
}

const DOCUMENT_DOC_TYPES = [
  'executive_report',
  'proposal',
  'memo',
  'technical_spec',
  'meeting_notes',
  'one_pager',
  'letter',
  'resume',
] as const

const DOCUMENT_AUDIENCES = ['Client', 'Executive', 'Technical team', 'Internal team', 'Recruiter', 'General reader']
const DOCUMENT_TONES = ['Client-ready', 'Formal', 'Concise', 'Persuasive', 'Technical', 'Warm']
const DOCUMENT_LENGTHS = ['One page', 'Short', 'Standard', 'Detailed']

function inferDocumentType(prompt: string): string {
  const text = ` ${prompt.toLowerCase()} `
  if (/(resume|résumé| cv |curriculum vitae)/.test(text)) return 'resume'
  if (/(meeting notes|meeting minutes|minutes of the meeting|meeting recap|attendees|agenda)/.test(text)) return 'meeting_notes'
  if (/(technical spec|technical specification|implementation spec|architecture spec|requirements document|design doc)/.test(text)) return 'technical_spec'
  if (/(proposal|propose|statement of work| sow |sow document|commercial offer)/.test(text)) return 'proposal'
  if (/(one pager|one-pager|1 pager|1-pager|single page|single-page)/.test(text)) return 'one_pager'
  if (/(memo|memorandum|internal note)/.test(text)) return 'memo'
  if (/(cover letter|recommendation letter|formal letter|letter to)/.test(text)) return 'letter'
  return 'executive_report'
}

function detectDocumentPrompt(prompt: string): string | null {
  const text = ` ${prompt.toLowerCase()} `
  const directType = inferDocumentType(prompt)
  if (directType !== 'executive_report') return directType
  if (/(executive report|board report|status report|client report)/.test(text)) return 'executive_report'
  const hasAction = /(write|draft|create|generate|prepare|produce|build|put together)/.test(text)
  const hasDocNoun = /(document| doc |report|write-up|writeup|word file|word doc|\.docx|downloadable)/.test(text)
  return hasAction && hasDocNoun ? 'executive_report' : null
}

function defaultDocumentBrief(prompt: string, forced = false): DocumentBrief | null {
  const docType = forced ? inferDocumentType(prompt) : detectDocumentPrompt(prompt)
  if (!docType) return null
  const defaults: Record<string, Omit<DocumentBrief, 'title' | 'docType'>> = {
    executive_report: { audience: 'Client', tone: 'Client-ready', length: 'Standard', outputFormats: ['docx'] },
    proposal:         { audience: 'Client', tone: 'Persuasive', length: 'Detailed', outputFormats: ['docx'] },
    memo:             { audience: 'Internal team', tone: 'Concise', length: 'Short', outputFormats: ['markdown'] },
    technical_spec:   { audience: 'Technical team', tone: 'Technical', length: 'Detailed', outputFormats: ['markdown'] },
    meeting_notes:    { audience: 'Internal team', tone: 'Concise', length: 'Short', outputFormats: ['markdown'] },
    one_pager:        { audience: 'Executive', tone: 'Concise', length: 'One page', outputFormats: ['docx'] },
    letter:           { audience: 'Client', tone: 'Formal', length: 'Short', outputFormats: ['docx'] },
    resume:           { audience: 'Recruiter', tone: 'Formal', length: 'Standard', outputFormats: ['docx'] },
  }
  return {
    title: '',
    docType,
    ...defaults[docType],
  }
}

function base64ToArrayBuffer(base64: string): ArrayBuffer {
  const byteChars = atob(base64)
  const bytes = new Uint8Array(byteChars.length)
  for (let i = 0; i < byteChars.length; i++) bytes[i] = byteChars.charCodeAt(i)
  return bytes.buffer
}

function base64ToBlob(base64: string, mimeType: string): Blob {
  const byteChars = atob(base64)
  const byteArrays: BlobPart[] = []
  for (let offset = 0; offset < byteChars.length; offset += 1024) {
    const slice = byteChars.slice(offset, offset + 1024)
    const bytes = new Uint8Array(slice.length)
    for (let i = 0; i < slice.length; i++) bytes[i] = slice.charCodeAt(i)
    byteArrays.push(bytes.buffer)
  }
  return new Blob(byteArrays, { type: mimeType })
}

function downloadBlob(blob: Blob, filename: string) {
  const href = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = href
  a.download = filename
  a.click()
  URL.revokeObjectURL(href)
}

const DOCX_MIME = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'

function fmtTime(iso: string): string {
  const d = new Date(iso)
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

function shortModel(m: string): string {
  const parts = m.split('/')
  return parts[parts.length - 1]
}

function fmt$(n: number, digits = 4): string {
  return `$${n.toFixed(digits)}`
}

// ── Exec log component ────────────────────────────────────────────────────────

function KV({ label, value, mono, dim }: { label: string; value: string | null | undefined; mono?: boolean; dim?: boolean }) {
  if (!value) return null
  return (
    <div className="exec-kv">
      <span className="exec-kv-k">{label}</span>
      <span className={`exec-kv-v${mono ? ' mono' : ''}${dim ? ' dim' : ''}`}>{value}</span>
    </div>
  )
}

function ExecLogView({ data }: { data: NonNullable<ExecPanelData> }) {
  if (!data.execLog) {
    const p = data.model_used ? getProvider(data.model_used) : null
    return (
      <>
        <div className="flow">
          {data.route && <>
            <div className="flow-node">
              <div className="flow-node-label">Task</div>
              <div className="flow-node-value">{data.route.task_type}
                <span className={`complexity-badge complexity-${data.route.complexity}`}>{data.route.complexity}</span>
              </div>
            </div>
            <div className="flow-connector">↓</div>
          </>}
          <div className="flow-node">
            <div className="flow-node-label">Model used</div>
            <div className="flow-node-value">
              {p && <span className="provider-badge" style={{ background: p.bg, color: p.color }}>{p.name}</span>}
              {data.model_used}
            </div>
          </div>
          {data.route && data.route.fallbacks.length > 0 && (
            <div style={{ padding: '6px 2px' }}>
              <div className="exec-kv-k" style={{ fontSize: 10, textTransform: 'uppercase', letterSpacing: '0.08em', marginBottom: 4 }}>Fallbacks</div>
              <div className="fallback-list">{data.route.fallbacks.map(m => <span key={m} className="fallback-item">{m}</span>)}</div>
            </div>
          )}
        </div>
        <div className="divider" />
        <div className="stats-grid">
          <div className="stat-box blue"><div className="stat-box-label">Latency</div><div className="stat-box-value">{data.latency_ms}<span className="stat-box-unit">ms</span></div></div>
          <div className="stat-box green"><div className="stat-box-label">Cost</div><div className="stat-box-value" style={{ fontSize: 14 }}>{data.estimated_cost_usd != null ? `$${data.estimated_cost_usd.toFixed(4)}` : '—'}</div></div>
          <div className="stat-box"><div className="stat-box-label">Input</div><div className="stat-box-value">{data.prompt_tokens ?? '—'}<span className="stat-box-unit">tok</span></div></div>
          <div className="stat-box"><div className="stat-box-label">Output</div><div className="stat-box-value">{data.completion_tokens ?? '—'}<span className="stat-box-unit">tok</span></div></div>
        </div>
        {data.route && <p className="routing-reason">{data.route.reason}</p>}
      </>
    )
  }

  const { planner, web_context: wc, worker } = data.execLog
  const wp = getProvider(worker.model)
  const pp = getProvider(planner.model)

  return (
    <div className="exec-log">
      <div className="exec-section">
        <div className="exec-dot" />
        <div className="exec-tag">Planner</div>
        <div className="exec-head">
          <span className="exec-head-model" style={{ color: pp.color }}>{planner.model}</span>
          <span className="exec-head-timing">{planner.latency_ms} ms</span>
          <span className="exec-head-cost">${planner.cost_usd.toFixed(5)}</span>
        </div>
        <div style={{ display: 'flex', gap: 4, marginBottom: 5 }}>
          <span className="exec-pill turn-pill" data-type={planner.turn_type}>{planner.turn_type}</span>
          <span className="exec-pill action-pill" data-action={planner.action}>{planner.action}</span>
        </div>
        <div className="exec-details">
          <KV label="Intent" value={planner.intent} />
          <KV label="Enriched" value={planner.enriched_prompt} mono />
          <KV label="Web search" value={planner.needs_web_search ? `Yes — "${planner.search_query ?? ''}"` : 'No'} />
          {planner.sub_queries.length > 1 && (
            <div className="exec-kv">
              <span className="exec-kv-k">Sub-queries</span>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 5 }}>
                {planner.sub_queries.map((sq, i) => (
                  <div key={i} style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
                    <span className="exec-kv-v mono">{i + 1}. {sq.query}</span>
                    <div style={{ display: 'flex', gap: 3, marginLeft: 12, flexWrap: 'wrap' }}>
                      {sq.task_type && <span className="exec-pill">{sq.task_type}</span>}
                      {sq.preferred_model && <span className="exec-pill" style={{ color: getProvider(sq.preferred_model).color }}>hint: {sq.preferred_model}</span>}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}
          <KV label="Context" value={planner.context_summary || null} dim />
        </div>
      </div>

      <div className="exec-section">
        <div className="exec-dot" />
        <div className="exec-tag">Web Context</div>
        {wc.enabled ? (
          <div className="exec-details">
            <KV label="Provider" value={wc.provider || 'URL crawl'} />
            <KV label="Sources"  value={wc.sources_count > 0 ? String(wc.sources_count) : null} />
            <KV label="Query"    value={wc.search_query} mono />
            <KV label="Status"   value={wc.sources_count === 0 ? wc.status : null} dim />
          </div>
        ) : (
          <div className="exec-kv"><span className="exec-kv-v dim">Not requested</span></div>
        )}
      </div>

      <div className="exec-section">
        <div className="exec-dot" />
        <div className="exec-tag">Routing</div>
        {data.route ? (
          <div className="exec-details">
            <div className="exec-kv">
              <span className="exec-kv-k">Task</span>
              <span className="exec-kv-v">
                {data.route.task_type}
                <span className={`complexity-badge complexity-${data.route.complexity}`} style={{ marginLeft: 5 }}>{data.route.complexity}</span>
              </span>
            </div>
            <KV label="Primary" value={data.route.primary_model} mono />
            {data.route.fallbacks.length > 0 && (
              <div className="exec-kv">
                <span className="exec-kv-k">Fallbacks</span>
                <div className="exec-pills">{data.route.fallbacks.map(m => <span key={m} className="exec-pill">{m}</span>)}</div>
              </div>
            )}
            <KV label="Reason" value={data.route.reason} dim />
          </div>
        ) : (
          <div className="exec-kv"><span className="exec-kv-v dim">Not available</span></div>
        )}
      </div>

      <div className="exec-section">
        <div className="exec-dot" />
        <div className="exec-tag">{worker.sub_queries_count > 1 ? `Worker × ${worker.sub_queries_count} + Synthesis` : 'Worker'}</div>
        <div className="exec-head">
          <span className="exec-head-model" style={{ color: wp.color }}>{worker.model}</span>
          <span className="exec-head-timing">{worker.latency_ms} ms</span>
          {worker.cost_usd != null && <span className="exec-head-cost">${worker.cost_usd.toFixed(5)}</span>}
        </div>
        <div className="exec-details">
          {worker.sub_query_logs.length > 0 && (
            <div className="exec-kv">
              <span className="exec-kv-k">Sub-workers</span>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                {worker.sub_query_logs.map((sq, i) => {
                  const sp = getProvider(sq.model_used)
                  const wasFallback = sq.model_requested && sq.model_requested !== sq.model_used
                  return (
                    <div key={i} style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
                      <div style={{ display: 'flex', gap: 5, alignItems: 'center', flexWrap: 'wrap' }}>
                        <span className="exec-kv-v dim">{i + 1}.</span>
                        <span className="exec-kv-v" style={{ color: sp.color }}>{sq.model_used}</span>
                        {sq.task_type && <span className="exec-pill">{sq.task_type}</span>}
                        <span className="exec-kv-v dim">{sq.latency_ms} ms</span>
                        {sq.cost_usd != null && <span className="exec-kv-v dim">${sq.cost_usd.toFixed(5)}</span>}
                      </div>
                      {wasFallback && (
                        <span style={{ fontSize: 10, color: '#f59e0b', marginLeft: 12 }}>
                          ⚠ {sq.model_requested} failed → fell back
                        </span>
                      )}
                    </div>
                  )
                })}
                <div style={{ display: 'flex', gap: 5, alignItems: 'center', marginTop: 2 }}>
                  <span className="exec-kv-v dim">Synthesis →</span>
                  <span className="exec-kv-v" style={{ color: wp.color }}>{worker.model}</span>
                </div>
              </div>
            </div>
          )}
          {(worker.prompt_tokens != null || worker.completion_tokens != null) && (
            <div className="exec-kv">
              <span className="exec-kv-k">Tokens</span>
              <span className="exec-kv-v">{worker.prompt_tokens ?? '—'} in → {worker.completion_tokens ?? '—'} out</span>
            </div>
          )}
        </div>
      </div>

      <div className="exec-total">
        <span className="exec-total-label">Total</span>
        <div className="exec-total-values">
          <span className="exec-total-latency">{data.execLog.total_latency_ms} ms</span>
          <span className="exec-total-cost">${data.execLog.total_cost_usd.toFixed(5)}</span>
        </div>
      </div>
    </div>
  )
}

function parseVerifierNotes(raw?: string | null): {
  notes: string
  unsupported_claims: string[]
  citation_issues: string[]
  stale_or_overconfident_claims: string[]
} | null {
  if (!raw) return null
  try {
    const parsed = JSON.parse(raw) as {
      notes?: string
      unsupported_claims?: string[]
      citation_issues?: string[]
      stale_or_overconfident_claims?: string[]
    }
    return {
      notes: parsed.notes ?? raw,
      unsupported_claims: Array.isArray(parsed.unsupported_claims) ? parsed.unsupported_claims : [],
      citation_issues: Array.isArray(parsed.citation_issues) ? parsed.citation_issues : [],
      stale_or_overconfident_claims: Array.isArray(parsed.stale_or_overconfident_claims) ? parsed.stale_or_overconfident_claims : [],
    }
  } catch {
    return { notes: raw, unsupported_claims: [], citation_issues: [], stale_or_overconfident_claims: [] }
  }
}

function scorePct(v?: number): string {
  return v == null ? '—' : `${Math.round(v * 100)}`
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function profileValueText(value: unknown): string {
  if (value == null || value === '') return 'Not set'
  if (Array.isArray(value)) return value.length ? value.map(profileValueText).join(', ') : 'Not set'
  if (isRecord(value)) {
    const parts = Object.entries(value)
      .filter(([, v]) => v != null && v !== '' && (!Array.isArray(v) || v.length > 0))
      .slice(0, 6)
      .map(([k, v]) => `${k.replace(/_/g, ' ')}: ${profileValueText(v)}`)
    return parts.length ? parts.join('; ') : 'Not set'
  }
  return String(value)
}

function profileList(value: unknown): string[] {
  if (Array.isArray(value)) return value.map(profileValueText).filter(v => v && v !== 'Not set').slice(0, 8)
  if (typeof value === 'string' && value.trim()) return [value.trim()]
  return []
}

function memoryConfidenceLabel(value: number): string {
  if (value >= 0.8) return 'high'
  if (value >= 0.5) return 'medium'
  return 'low'
}

function renderMarkdownWithCitations(content: string, research?: ResearchMeta | null): string {
  let html = DOMPurify.sanitize(marked.parse(content) as string)
  if (!research || research.sources.length === 0) return html
  const validRefs = new Set(research.sources.map((_, i) => `S${i + 1}`))
  html = html.replace(/\[S(\d+)\]/g, (match, n: string) => {
    const ref = `S${n}`
    if (!validRefs.has(ref)) return match
    return `<button type="button" class="citation-chip" data-source-ref="${ref}" aria-label="Show source ${ref}">${ref}</button>`
  })
  return html
}

function ResearchEvidence({
  research,
  activeSourceRef,
}: {
  research: ResearchMeta
  activeSourceRef?: string | null
}) {
  const [open, setOpen] = useState(false)
  const verifier = parseVerifierNotes(research.verifier_notes)
  const verifierIssueCount =
    (verifier?.unsupported_claims.length ?? 0) +
    (verifier?.citation_issues.length ?? 0) +
    (verifier?.stale_or_overconfident_claims.length ?? 0)
  const claims = research.claims ?? []
  const findings = research.findings ?? []

  useEffect(() => {
    if (!activeSourceRef) return
    setOpen(true)
    const id = window.setTimeout(() => {
      const el = document.querySelector<HTMLElement>(`[data-research-source="${activeSourceRef}"]`)
      el?.scrollIntoView({ block: 'nearest', behavior: 'smooth' })
    }, 80)
    return () => window.clearTimeout(id)
  }, [activeSourceRef])

  return (
    <div className="research-evidence">
      <button
        className="research-evidence-toggle"
        onClick={() => setOpen(v => !v)}
        aria-expanded={open}
        type="button"
      >
        <span className="research-evidence-title">
          <i className="ti ti-microscope" aria-hidden="true" />
          Research evidence
        </span>
        <span className="research-evidence-summary">
          {research.sources.length} sources · {claims.length} claims · {research.confidence ?? 'unknown'} confidence
        </span>
        <i className={`ti ti-chevron-down research-evidence-chevron${open ? ' open' : ''}`} aria-hidden="true" />
      </button>

      {open && (
        <div className="research-evidence-body">
          <div className="research-metrics">
            <div><span>Mode</span><strong>{research.mode}</strong></div>
            <div><span>Questions</span><strong>{research.questions.length}</strong></div>
            <div><span>Gaps</span><strong>{research.gaps.length}</strong></div>
            <div><span>Verifier</span><strong>{verifierIssueCount === 0 ? 'clean' : `${verifierIssueCount} issues`}</strong></div>
          </div>

          {research.questions.length > 0 && (
            <details className="research-section" open>
              <summary>Research questions</summary>
              <ol className="research-list">
                {research.questions.map((q, i) => <li key={i}>{q}</li>)}
              </ol>
            </details>
          )}

          {findings.length > 0 && (
            <details className="research-section" open>
              <summary>Key findings</summary>
              <div className="research-finding-list">
                {findings.map((finding, i) => (
                  <div key={finding.id ?? i} className="research-finding-card">
                    <div className="research-finding-head">
                      <span>Finding {i + 1}</span>
                      {finding.confidence && <strong>{finding.confidence}</strong>}
                    </div>
                    <p>{finding.finding}</p>
                    {(finding.evidence?.length ?? 0) > 0 && (
                      <div className="research-finding-evidence">
                        {finding.evidence?.slice(0, 3).map((ev, j) => (
                          <span key={`${ev.claim_id ?? j}-${ev.source_ref ?? 'S?'}`}>
                            {ev.source_ref ?? 'S?'}{ev.source_title ? ` · ${ev.source_title}` : ''}
                          </span>
                        ))}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            </details>
          )}

          <details className="research-section" open>
            <summary>Sources</summary>
            <div className="research-source-list">
              {research.sources.map((s, i) => {
                const ref = `S${i + 1}`
                return (
                <a
                  key={`${s.url}-${i}`}
                  className={`research-source-card${activeSourceRef === ref ? ' active' : ''}`}
                  href={s.url}
                  target="_blank"
                  rel="noreferrer"
                  data-research-source={ref}
                >
                  <span className="research-source-ref">S{i + 1}</span>
                  <span className="research-source-main">
                    <span className="research-source-title">{s.title || s.url}</span>
                    <span className="research-source-url">{s.url}</span>
                  </span>
                  <span className="research-source-scores">
                    <span>{s.source_type ?? 'web'}</span>
                    <span>C {scorePct(s.credibility_score)}</span>
                    <span>R {scorePct(s.relevance_score)}</span>
                  </span>
                </a>
                )
              })}
            </div>
          </details>

          {claims.length > 0 && (
            <details className="research-section">
              <summary>Extracted claims</summary>
              <div className="research-claim-list">
                {claims.slice(0, 24).map((c, i) => (
                  <div
                    key={c.id ?? i}
                    className={`research-claim-card${activeSourceRef && c.source_ref === activeSourceRef ? ' active' : ''}`}
                  >
                    <div className="research-claim-head">
                      <span>{c.source_ref ?? 'S?'}</span>
                      <span>{c.confidence ?? 'medium'}</span>
                      <span>R {scorePct(c.relevance_score)}</span>
                    </div>
                    <p>{c.claim}</p>
                    {c.quote && <blockquote>{c.quote}</blockquote>}
                    {c.source_url && (
                      <a href={c.source_url} target="_blank" rel="noreferrer">{c.source_title ?? c.source_url}</a>
                    )}
                  </div>
                ))}
              </div>
            </details>
          )}

          {(research.gaps.length > 0 || research.contradictions.length > 0 || verifier) && (
            <details className="research-section">
              <summary>Gaps, conflicts, verifier</summary>
              {research.gaps.length > 0 && (
                <div className="research-note-block">
                  <h4>Gaps</h4>
                  <ul>{research.gaps.map((g, i) => <li key={i}>{g}</li>)}</ul>
                </div>
              )}
              {research.contradictions.length > 0 && (
                <div className="research-note-block">
                  <h4>Contradictions</h4>
                  <ul>{research.contradictions.map((c, i) => <li key={i}>{c}</li>)}</ul>
                </div>
              )}
              {verifier && (
                <div className="research-note-block">
                  <h4>Verifier</h4>
                  <p>{verifier.notes}</p>
                  {[...verifier.unsupported_claims, ...verifier.citation_issues, ...verifier.stale_or_overconfident_claims].length > 0 && (
                    <ul>
                      {[...verifier.unsupported_claims, ...verifier.citation_issues, ...verifier.stale_or_overconfident_claims]
                        .map((issue, i) => <li key={i}>{issue}</li>)}
                    </ul>
                  )}
                </div>
              )}
            </details>
          )}
        </div>
      )}
    </div>
  )
}

function CitationSourceRow({
  research,
  onSelect,
}: {
  research: ResearchMeta
  onSelect: (ref: string) => void
}) {
  if (!research.sources.length) return null
  return (
    <div className="citation-source-row" aria-label="Sources cited in this answer">
      {research.sources.slice(0, 8).map((source, i) => {
        const ref = `S${i + 1}`
        return (
          <button
            key={`${source.url}-${i}`}
            className="citation-source-pill"
            onClick={() => onSelect(ref)}
            type="button"
            title={source.title || source.url}
          >
            <span>{ref}</span>
            <strong>{source.source_type ?? 'web'}</strong>
          </button>
        )
      })}
    </div>
  )
}

function AssistantContent({ message }: { message: MessageOut }) {
  const [activeSourceRef, setActiveSourceRef] = useState<string | null>(null)
  const html = useMemo(
    () => renderMarkdownWithCitations(message.content, message.research),
    [message.content, message.research],
  )

  function onMarkdownClick(e: MouseEvent<HTMLDivElement>) {
    const target = e.target as HTMLElement
    const button = target.closest<HTMLButtonElement>('[data-source-ref]')
    if (!button) return
    setActiveSourceRef(button.dataset.sourceRef ?? null)
  }

  return (
    <>
      <div
        className="markdown-body"
        onClick={onMarkdownClick}
        dangerouslySetInnerHTML={{ __html: html }}
      />
      {message.research && (
        <>
          <CitationSourceRow research={message.research} onSelect={setActiveSourceRef} />
          <ResearchEvidence research={message.research} activeSourceRef={activeSourceRef} />
        </>
      )}
    </>
  )
}

type SettingsTab = 'general' | 'dashboard' | 'guide' | 'voice' | 'memory' | 'workspace' | 'models' | 'account' | 'admin'
type UserGuidePageId = 'start' | 'chat' | 'modes' | 'workbench' | 'research' | 'documents' | 'memory' | 'dashboard' | 'adminOps' | 'difference' | 'shortcuts' | 'settings' | 'api' | 'tips'

const USER_GUIDE_PAGES: {
  id: UserGuidePageId
  label: string
  icon: string
  title: string
  summary: string
  sections: { title: string; body: string; items?: string[] }[]
}[] = [
  {
    id: 'start',
    label: 'Start here',
    icon: 'ti-compass',
    title: 'Start here: what Fronei is for',
    summary: 'Fronei is a personal AI workbench for thinking, writing, research, decisions, and reusable work products. It is designed to learn how you work, route each task to the right model path, and turn conversation into durable context and structured deliverables.',
    sections: [
      {
        title: 'The core idea',
        body: 'Use Fronei as an operating layer over frontier models, not just another blank chat box. A normal AI chat tool answers the prompt in front of it. Fronei adds product behavior around that model call: conversations, memory, writing voice, role-based workbench actions, output modes, research, artifacts, analytics, budget controls, and admin visibility. The goal is to reduce repeated prompting and make high-quality work repeatable.',
        items: [
          'Use chat for open-ended thinking, writing, and follow-up refinement.',
          'Use Workbench when you want a guided path to a known deliverable.',
          'Use Research when freshness, citations, contradiction checks, or source coverage matters.',
          'Use Memory and Voice so Fronei gets more personalized over time instead of treating every session as a fresh start.',
          'Use Dashboard and Settings to understand cost, usage, routing, and operational health.',
        ],
      },
      {
        title: 'Your first setup pass',
        body: 'A good first setup takes only a few minutes and pays off across every conversation. Set the defaults once, then override them per task when needed.',
        items: [
          'Open Settings → General and choose Classic or Workbench. Classic is best when you want a simple chat surface. Workbench is best when you want guided actions and artifacts.',
          'Set your default Quality. Smart is the best starting point for most users; Quick is useful when you are cost-sensitive; Thorough is for heavier reasoning.',
          'Choose a default Output mode if you repeatedly create the same kind of work, such as exec-ready summaries, client-ready wording, or architecture analysis.',
          'Open Settings → Voice and add writing samples that represent how you want Fronei to sound.',
          'Open Settings → Memory and periodically review what Fronei remembers, pin important facts, and scrub anything you do not want reused.',
        ],
      },
      {
        title: 'How to ask for better work',
        body: 'The strongest prompts describe the desired outcome, audience, constraints, and decision criteria. Fronei can infer a lot, but explicit context still improves quality and reduces revisions.',
        items: [
          'Say what you are trying to accomplish: decide, draft, explain, compare, challenge, summarize, or produce a reusable artifact.',
          'Name the audience: yourself, a customer, a technical team, executives, reviewers, or an approver.',
          'Add constraints: length, tone, risk tolerance, budget, deadline, must-use technology, or must-avoid options.',
          'Ask for a format: bullet memo, table, email, plan, ADR, trade-off matrix, risk register, or steering update.',
          'Iterate with precise feedback: too long, more skeptical, more technical, less formal, show assumptions, add risks, or convert to an action plan.',
        ],
      },
      {
        title: 'How a request is handled',
        body: 'For each request, Fronei combines your conversation history, relevant memories, writing voice, selected quality, output mode, workbench persona, artifact selection, and research/web settings. It then routes the request through the appropriate backend path and records usage so you can inspect cost and behavior later. Higher quality and research routes cost more and take longer, so reserve them for work where the depth pays off.',
      },
      {
        title: 'A simple operating rhythm',
        body: 'For everyday use, start in Smart quality and Default output mode. Turn on Thorough when the answer will influence a decision. Turn on Research when current facts or citations matter. Use Workbench for repeatable deliverables. Review Memory every so often so Fronei stays accurate and personal.',
      },
    ],
  },
  {
    id: 'chat',
    label: 'Chat',
    icon: 'ti-message-circle',
    title: 'Chat and conversations',
    summary: 'Chat is the main working surface. Use it for reasoning, drafting, critique, synthesis, planning, and follow-up refinement.',
    sections: [
      {
        title: 'Use one thread per goal',
        body: 'Each conversation carries its own context. Keep related work in one thread when you want Fronei to remember the evolution of the task, assumptions, and prior decisions. Start a new conversation when the objective changes, because unrelated context can steer responses in the wrong direction.',
        items: [
          'Good thread: "Q3 migration decision" with research, trade-offs, draft recommendation, and final memo.',
          'Good thread: "Rewrite my LinkedIn announcement" with tone iterations and final copy.',
          'Less useful thread: mixing travel planning, architecture review, and personal notes in one long conversation.',
        ],
      },
      {
        title: 'The best prompt shape',
        body: 'A strong prompt has four parts: task, context, constraints, and output. For example: "Compare Neon and Supabase for my personal Fronei deployment. Assume lowest cost, low ops overhead, Postgres compatibility, and easy backups. Output a decision memo with recommendation, risks, and next steps."',
      },
      {
        title: 'Iterate surgically',
        body: 'Treat the first response as a useful draft. Instead of regenerating everything, tell Fronei exactly what to change. This preserves the best parts of the answer and saves cost.',
        items: [
          'Ask for targeted edits: "keep the recommendation, but add an implementation risk section."',
          'Ask for audience changes: "make this more direct for an executive sponsor."',
          'Ask for structure changes: "convert this to a table with decision criteria and scores."',
          'Ask for critique: "what would a skeptical reviewer challenge here?"',
          'Ask for compression: "turn this into a 150-word Slack update."',
        ],
      },
      {
        title: 'When to use normal chat instead of Research',
        body: 'Normal chat is best when the answer mainly depends on the context you provide, Fronei memory, or general reasoning. Use it for drafts, analysis, brainstorming, planning, editing, strategy, code explanation, and internal decision support. Turn on Research when current public information, citations, source comparison, or contradiction checking is part of the job.',
      },
      {
        title: 'Conversation hygiene',
        body: 'Long conversations can become noisy. Ask for a recap when a thread gets long, correct wrong assumptions explicitly, and start a fresh thread when you pivot. If Fronei learns something you do not want remembered, remove it from Settings → Memory.',
      },
      {
        title: 'Useful chat commands',
        body: 'Fronei responds well to direct operating instructions embedded in the prompt.',
        items: [
          'Plan first, then wait for my approval before implementing.',
          'Ask clarifying questions only if the answer materially changes the solution.',
          'Give me the recommendation first, then the reasoning.',
          'Be skeptical and identify hidden risks.',
          'Write this in my voice, but make it more concise.',
          'Convert this into an artifact I can reuse.',
        ],
      },
    ],
  },
  {
    id: 'modes',
    label: 'Profiles & modes',
    icon: 'ti-adjustments',
    title: 'Quality, profiles, and output modes',
    summary: 'These controls determine how much effort Fronei spends and what shape the final answer takes.',
    sections: [
      {
        title: 'Quality levels',
        body: 'Quality is the main cost/depth control. Use the cheapest setting that is appropriate for the stakes of the work.',
        items: [
          'Quick — fastest and cheapest. Use for short rewrites, simple summaries, brainstorming, naming, and low-risk drafts.',
          'Smart — balanced default. Use for most daily work: writing, planning, comparisons, code explanation, and moderate reasoning.',
          'Thorough — highest effort. Use for architecture decisions, multi-factor trade-offs, nuanced writing, risk analysis, or work you will defend to others.',
        ],
      },
      {
        title: 'Model profiles',
        body: 'Behind the scenes, each Quality level maps to a model profile. Cost saver favors cheaper models, Balanced mixes cost and capability, and Best quality prefers the strongest configured route. Admins can inspect and tune model mappings in Settings → Models. This gives Fronei a practical advantage over a single fixed-model chat surface: routine tasks can stay cheap while hard tasks can still use stronger models.',
      },
      {
        title: 'Output modes',
        body: 'Output mode shapes the format, tone, and editorial posture of the answer. It is independent of Quality, so you can ask for a cheap exec-ready draft or a thorough architecture analysis.',
        items: [
          'Default — Fronei chooses a sensible format automatically.',
          'Raw — unrefined model output, useful when you want to post-process it yourself.',
          'Client-ready — polished, professional language suitable for external sharing.',
          'Exec-ready — crisp, outcome-first, minimal jargon for leadership audiences.',
          'Email — formatted as a ready-to-send email.',
          'Proposal — structured and authoritative, suited for proposals or business cases.',
          'Architecture — technical writing with explicit trade-offs and constraints.',
          'Pushback — actively critiques and stress-tests the input instead of agreeing with it.',
        ],
      },
      {
        title: 'Recommended combinations',
        body: 'Most work falls into a few repeatable patterns.',
        items: [
          'Smart + Default — general conversation, planning, drafting, and analysis.',
          'Quick + Email — quick reply drafts or tone cleanup.',
          'Smart + Client-ready — polished external language without over-spending.',
          'Thorough + Architecture — design decisions, ADRs, trade-off analysis, and technical recommendations.',
          'Thorough + Pushback — stress-testing a proposal before you commit to it.',
          'Research + Exec-ready — source-grounded leadership summaries.',
        ],
      },
      {
        title: 'Cost discipline',
        body: 'A useful habit is to draft in Quick or Smart, then use Thorough only on the version that is close to final. For research-heavy work, start with Quick Research to map the topic, then move to Deep or Expert only when you know the question is worth the extra cost.',
      },
    ],
  },
  {
    id: 'workbench',
    label: 'Personas & artifacts',
    icon: 'ti-layout-grid',
    title: 'Workbench mode, personas, and artifacts',
    summary: 'Workbench turns Fronei from a chat surface into a repeatable deliverable factory for common professional workflows.',
    sections: [
      {
        title: 'Workbench mode',
        body: 'Workbench replaces the plain composer with role-specific actions and artifact controls. It is useful when you know the kind of work product you need but do not want to manually craft the prompt every time. Switch between Classic and Workbench in Settings → General.',
        items: [
          'Use Classic for free-form conversation.',
          'Use Workbench for recurring work products like briefs, ADRs, comparisons, risk registers, and steering updates.',
          'Use a persona to bias the action rail toward your role and the kind of decisions you make.',
          'Use artifact controls when the output needs a consistent structure.',
        ],
      },
      {
        title: 'Personas',
        body: 'A persona changes the suggested actions and available artifacts. It does not prevent you from asking anything; it simply gives Fronei better defaults for the kind of work you are doing.',
        items: [
          'Enterprise Architect — research briefs, ADRs, solution comparisons, challenge reviews, and the full artifact set.',
          'Product Manager — exec briefs, risk registers, steering updates, and trade-off matrices.',
          'Software Engineer — ADRs, NFR analysis, trade-off matrices, and risk registers.',
          'Data Scientist — exec briefs, risk registers, trade-off matrices, and solution comparisons.',
          'Custom — a blank persona with no preset actions or artifacts, for ad hoc work.',
        ],
      },
      {
        title: 'Artifacts',
        body: 'Artifacts are structured deliverables. Use them when the output should be reviewed, shared, compared, or reused later. They reduce the need to repeatedly ask for sections and formatting.',
        items: [
          'ADR — Architecture Decision Record with context, decision, and consequences.',
          'Compare — side-by-side comparison of solution options.',
          'Trade-offs — a trade-off matrix scoring options against criteria.',
          'Exec brief — a short executive briefing focused on outcomes and asks.',
          'Risk register — structured list of risks, likelihood, impact, and mitigations.',
          'NFR analysis — non-functional requirements broken out and assessed.',
          'Steering update — a status update formatted for a steering committee.',
        ],
      },
      {
        title: 'Generating an artifact',
        body: 'Select a persona, choose an artifact type, and describe the subject, audience, and decision criteria. Fronei automatically applies a structured output style so the artifact is consistent regardless of your normal chat defaults.',
      },
      {
        title: 'Artifact examples',
        body: 'Artifacts work best when you give Fronei the real decision context.',
        items: [
          'ADR: "Create an ADR for choosing Neon Postgres for Fronei production. Include cost, operational overhead, backup risk, and rollback plan."',
          'Trade-off matrix: "Compare Railway, Fly.io, Render, and a single VPS for personal-use deployment. Score cost, setup time, operational overhead, reliability, and migration effort."',
          'Risk register: "Build a risk register for launching Fronei publicly with Clerk, Neon, Railway, and multiple model providers."',
          'Exec brief: "Summarize why we should implement global budget caps before adding more expensive research features."',
        ],
      },
      {
        title: 'Reviewing generated artifacts',
        body: 'Do not treat artifacts as final just because they are structured. Ask Fronei to identify assumptions, missing stakeholders, weak evidence, and risks. For technical artifacts, ask it to add migration steps, operational runbooks, and validation checks.',
      },
    ],
  },
  {
    id: 'research',
    label: 'Research',
    icon: 'ti-microscope',
    title: 'Research mode',
    summary: 'Research mode is for source-grounded work: current facts, cited answers, evidence trails, market scans, technical comparisons, and contradiction checks.',
    sections: [
      {
        title: 'When to use it',
        body: 'Use Research when the answer should be based on current or external information, not just reasoning from the conversation. Research mode searches, gathers sources, extracts claims, checks gaps, and synthesizes a cited answer.',
        items: [
          'Use it for product comparisons, pricing, recent announcements, regulations, competitor scans, and vendor due diligence.',
          'Use it when you need source coverage and evidence, not just a confident-sounding summary.',
          'Use it for technical choices where docs, changelogs, or current limitations matter.',
          'Prefer normal chat for private brainstorming, writing from your own context, or low-stakes internal drafts.',
        ],
      },
      {
        title: 'Research depth levels',
        body: 'Research depth is separate from normal Quality. Pick the depth based on how defensible the final answer needs to be.',
        items: [
          'Quick — fast fact check or initial topic map.',
          'Deep — broader source coverage, claim extraction, and gap-checking. This is the default for serious but routine research.',
          'Expert — most exhaustive path with stronger contradiction checks. Use when the decision is costly, visible, or hard to reverse.',
        ],
      },
      {
        title: 'How to write a research request',
        body: 'A good research request names the decision, scope, source preference, and output format.',
        items: [
          'Decision: "Should I deploy Fronei on Railway or a VPS?"',
          'Scope: "personal use, lowest cost, low overhead, Postgres, Clerk, nightly cron."',
          'Evidence requirement: "prioritize official docs, pricing pages, and recent changelogs."',
          'Output: "give me a recommendation, trade-off table, risks, and next actions."',
        ],
      },
      {
        title: 'Web search toggle',
        body: 'The web search toggle is lighter than full Research. Use it when a single answer needs current information but you do not need a full evidence workflow. Examples: "check the latest pricing", "what changed in this release", or "is this API still current?"',
      },
      {
        title: 'Reading research output',
        body: 'Research answers can include source pills, evidence sections, verifier notes, gaps, and contradictions. Use those details to decide whether the answer is strong enough to act on. For high-stakes work, inspect the sources directly and ask follow-up questions about weak evidence or conflicting claims.',
      },
      {
        title: 'Research best practices',
        body: 'Research quality improves when you constrain the question. Avoid asking for "everything about X" unless you truly want a broad survey. Ask for a decision-oriented answer, include what you already know, and tell Fronei what would change your mind.',
      },
    ],
  },
  {
    id: 'documents',
    label: 'Documents',
    icon: 'ti-paperclip',
    title: 'Documents and attachments',
    summary: 'Use attachments when Fronei should analyze, summarize, rewrite, or extract structured information from material you provide.',
    sections: [
      {
        title: 'Attach useful material',
        body: 'Document extraction turns supported uploads into context for the next request. Attach material when the answer should be grounded in a specific file rather than general model knowledge.',
        items: [
          'Attach source material that is directly relevant to the current task.',
          'For long PDFs, ask for a specific section, decision, clause, table, or risk area.',
          'For messy scans or unusual formatting, paste the most important excerpt directly if extraction is incomplete.',
          'Keep the prompt explicit: say whether you want summary, rewrite, critique, extraction, comparison, or action plan.',
        ],
      },
      {
        title: 'Common document workflows',
        body: 'A few patterns that work well once a file is attached.',
        items: [
          '"Summarize this in 5 bullets for an exec audience" — quick digest of a long document.',
          '"Compare this against our current approach and list gaps" — pairs well with the Compare or Trade-offs artifacts.',
          '"Extract every risk, owner, and deadline mentioned" — feeds directly into a risk register.',
          '"Rewrite section 3 in client-ready tone, keep the numbers unchanged" — targeted rewrites.',
        ],
      },
      {
        title: 'Document review pattern',
        body: 'For serious document review, use a three-pass flow: first ask for a neutral summary, then ask for risks and missing information, then ask for the final output in the format you need. This prevents the final answer from skipping over important context too early.',
      },
      {
        title: 'Combining documents with artifacts',
        body: 'Attachments pair especially well with artifacts. A project plan can become a risk register. A vendor proposal can become a trade-off matrix. A design doc can become an ADR. A status report can become an exec brief.',
      },
      {
        title: 'Privacy habit',
        body: 'Only attach files you are comfortable sending through the configured model and extraction pipeline. If a file contains sensitive material, redact it first or paste only the relevant excerpts.',
      },
    ],
  },
  {
    id: 'memory',
    label: 'Memory',
    icon: 'ti-brain',
    title: 'Memory, personalization, and voice',
    summary: 'Fronei can become more useful over time by remembering stable facts, preferences, work context, and communication style. You remain in control of what is kept.',
    sections: [
      {
        title: 'What memory is for',
        body: 'Memory is for durable context: preferences, background facts, work style, active projects, communication norms, and recurring constraints. The point is to avoid repeating the same personal context in every prompt.',
        items: [
          'Good memory: "prefers concise executive summaries with recommendation first."',
          'Good memory: "uses Railway, Neon, Clerk, and GitHub Actions for Fronei production."',
          'Good memory: "wants lowest-cost, lowest-overhead deployment choices for personal use."',
          'Poor memory: one-off temporary details that will not matter after the current task.',
        ],
      },
      {
        title: 'How memories are used',
        body: 'Fronei ranks active memories by recency, importance, confidence, repetition, and relevance to the current turn. Pinned memories are treated as more authoritative. Lower-confidence memories can be shown as uncertain so the model does not over-commit to them.',
      },
      {
        title: 'User control',
        body: 'Open Settings → Memory to review what Fronei remembers. You can pin important facts, edit incorrect facts, mark something as wrong, delete specific memories, or clear everything. This is the safety valve: Fronei can learn automatically, but you can scrub anything you do not want remembered.',
      },
      {
        title: 'Writing voice',
        body: 'Writing samples help Fronei learn tone, structure, directness, formality, and technical depth. Add samples that represent the style you actually want future outputs to resemble.',
        items: [
          'Use polished examples for the style you want repeated.',
          'Add samples from different contexts if you want range: email, memo, technical note, executive update.',
          'Delete samples that no longer represent your desired voice.',
          'Re-extract after adding or removing several samples.',
        ],
      },
      {
        title: 'Profile summary',
        body: 'Fronei also maintains a compact profile summary with bio, role, company, location, active projects, key preferences, constraints, and communication style. The profile gives the model a stable high-level picture before ranked memories are added.',
      },
      {
        title: 'Best practices',
        body: 'Let Fronei learn the stable parts of how you work, but keep temporary project details in the conversation thread. Review Memory after important sessions. Pin facts that should not be overwritten automatically. Scrub sensitive or stale information quickly.',
      },
    ],
  },
  {
    id: 'dashboard',
    label: 'Dashboard',
    icon: 'ti-chart-bar',
    title: 'Dashboard, usage, and cost visibility',
    summary: 'The Dashboard shows how Fronei is being used: spend, requests, tokens, latency, model usage, task distribution, and admin-only operational insights.',
    sections: [
      {
        title: 'Where it lives',
        body: 'The Dashboard is inside Settings → Dashboard. Regular users see their normal usage analytics. Admins see the same analytics plus operational controls and reports.',
      },
      {
        title: 'What the metrics mean',
        body: 'Usage analytics help you understand cost and performance over time.',
        items: [
          'Total spent — estimated model cost for the selected range.',
          'Requests — number of completed model-backed calls.',
          'Tokens — prompt and completion volume, useful for understanding why some tasks cost more.',
          'Average latency — how long requests take on average.',
          'Model usage — which models are used most and how much they cost.',
          'Task distribution — what kinds of work Fronei is doing.',
        ],
      },
      {
        title: 'Admin budget controls',
        body: 'Admins can configure a global monthly cap from the Dashboard. The cap is stored in the database, not Railway config, so it can be changed without a deploy. Admin override determines whether admins can continue using Fronei after the cap is reached.',
      },
      {
        title: 'Recommended actions',
        body: 'Admin insights summarize what needs attention: pending approvals, failed research runs, users near budget, recent errors, model spend, and budget pressure. Treat this as a lightweight operations checklist.',
      },
      {
        title: 'How to use it well',
        body: 'Check Dashboard after enabling new features, changing model routing, or running research-heavy sessions. Watch for cost spikes, slow model paths, repeated errors, and users nearing budget. Adjust quality defaults or model routing when spend does not match the value of the work.',
      },
    ],
  },
  {
    id: 'adminOps',
    label: 'Admin & ops',
    icon: 'ti-shield-lock',
    title: 'Admin operations',
    summary: 'Admin tools give trusted users control over access, costs, routing, providers, research runs, audits, and system health.',
    sections: [
      {
        title: 'Admin access',
        body: 'Admins are identified by configured allowlists and/or DB-assigned roles. Admin-only screens and endpoints are hidden from regular users and enforced by the backend.',
      },
      {
        title: 'User management',
        body: 'Admins can review users, change roles, suspend accounts, approve pending access, and inspect usage. Use suspension for immediate access control and role changes for durable permissions.',
      },
      {
        title: 'Cost and budget operations',
        body: 'Admins can inspect spend, users near budget, model usage, and global budget state. The global monthly cap prevents runaway cost. Admin override is useful for emergency maintenance or critical work, but should be used deliberately.',
      },
      {
        title: 'Provider and model routing',
        body: 'Provider settings and model routing determine which model paths Fronei can use for Quick, Smart, Thorough, Research, and specialized work. Change routing carefully, then monitor Dashboard for latency, errors, and cost impact.',
      },
      {
        title: 'Audit and system health',
        body: 'Audit logs record sensitive admin actions. System status and smoke checks help confirm that database tables, migrations, internal tasks, and required services are healthy before production traffic depends on them.',
      },
    ],
  },
  {
    id: 'difference',
    label: 'Why Fronei',
    icon: 'ti-git-compare',
    title: 'How Fronei is different from general AI chat products',
    summary: 'Fronei still uses frontier models, but the product is built around workflow, personalization, routing, cost control, and operational visibility rather than a single general-purpose chat surface.',
    sections: [
      {
        title: 'Frontier model vs product layer',
        body: 'Products like ChatGPT, Claude, Gemini, and similar tools are excellent general AI assistants. Fronei is different in emphasis: it wraps model capability in a workflow layer for your own recurring tasks, preferences, deployment needs, usage policies, and operational controls.',
      },
      {
        title: 'What Fronei adds',
        body: 'Fronei adds application behavior around the model call.',
        items: [
          'Persistent user memories with confidence, recency, importance, pinning, and user scrub controls.',
          'Writing voice extraction from samples and profile consolidation.',
          'Quality and output-mode routing so cheap tasks stay cheap and hard tasks can use stronger paths.',
          'Workbench personas and reusable artifacts for repeatable professional deliverables.',
          'Research mode with source gathering, evidence, gap checks, and contradiction awareness.',
          'Admin dashboard, budget caps, usage analytics, audit logs, and operational recommendations.',
          'Self-hostable production posture where you control database, auth, providers, and cost policy.',
        ],
      },
      {
        title: 'When a general chat tool is enough',
        body: 'A general chat product is often enough for casual questions, one-off writing help, quick brainstorming, or when you do not need custom memory, budget controls, admin workflows, or self-hosted operational ownership.',
      },
      {
        title: 'When Fronei is better',
        body: 'Fronei is better when you want a private, personal, configurable AI workspace that learns your context, produces repeatable work products, exposes cost and routing behavior, and can be operated like a small production system.',
      },
      {
        title: 'How to think about it',
        body: 'Use frontier chat products as powerful general assistants. Use Fronei as your tailored AI operating layer: opinionated defaults, personal context, structured outputs, research workflows, and cost-aware administration. The value is not only the model answer; it is the system around the answer.',
      },
    ],
  },
  {
    id: 'shortcuts',
    label: 'Shortcuts & dev mode',
    icon: 'ti-keyboard',
    title: 'Keyboard shortcuts and developer tools',
    summary: 'Use shortcuts for speed and developer mode to inspect how Fronei is behaving under the hood.',
    sections: [
      {
        title: 'Keyboard shortcuts',
        body: 'Available from anywhere in the app.',
        items: [
          'Cmd/Ctrl+K — start a new conversation.',
          'Cmd/Ctrl+E — toggle the execution/routing panel on the right.',
        ],
      },
      {
        title: 'Dev mode and the execution panel',
        body: 'Turning on dev mode reveals the execution log and routing panel for each response: model, profile, quality, timing, task type, and research steps when applicable. Use it to understand cost and latency trade-offs, diagnose unexpected behavior, and verify that a request used the path you intended.',
      },
      {
        title: 'When to enable dev mode',
        body: 'Enable dev mode when tuning model routing, debugging slow responses, checking whether Research actually ran, investigating cost, or preparing production hardening. Turn it off for a quieter daily-use interface.',
      },
    ],
  },
  {
    id: 'settings',
    label: 'Settings',
    icon: 'ti-adjustments-horizontal',
    title: 'Settings map',
    summary: 'Settings is where Fronei becomes yours: appearance, dashboard, guide, voice, memory, workspace, models, account, and admin controls.',
    sections: [
      {
        title: 'General',
        body: 'General controls theme, accent color, manual web search visibility, developer mode, default quality, default output mode, and UI mode. This is the best place to set the defaults you want every day.',
      },
      {
        title: 'Dashboard',
        body: 'Dashboard shows usage analytics. Admins also see global budget controls, pending tasks, insights, and recommended actions.',
      },
      {
        title: 'Voice and Memory',
        body: 'Voice manages writing samples and tone extraction. Memory manages remembered facts, preferences, profile summary, active projects, constraints, and scrub controls.',
      },
      {
        title: 'Workspace and Models',
        body: 'Workspace controls persona and artifact tooling. Models shows profile-to-model mappings and routing defaults so you can understand which model paths are available.',
      },
      {
        title: 'Admin',
        body: 'Admins see operational tools: user management and role assignment, usage and cost analytics, provider configuration, model routing rules, research run history, audit logs, system status, and budget controls.',
      },
    ],
  },
  {
    id: 'api',
    label: 'API reference',
    icon: 'ti-api',
    title: 'API reference',
    summary: 'For developers integrating with, testing, or operating the Fronei backend directly.',
    sections: [
      {
        title: 'Interactive docs',
        body: 'The FastAPI backend exposes an interactive Swagger UI at /docs (and a raw OpenAPI schema at /openapi.json) on the API host. A standalone, shareable copy is also checked into the repo at docs/fronei-api-spec.html, with the schema in docs/openapi.json — open either in a browser to browse every endpoint, request/response shape, and try requests directly.',
      },
      {
        title: 'What is covered',
        body: 'The spec documents all current API surfaces.',
        items: [
          'Chat and conversations — send messages, stream responses, manage conversation history.',
          'Research runs — kick off and inspect deep/expert research jobs.',
          'Documents — upload and extract content from supported file types.',
          'Memory and twin profile — manage saved memories, writing samples, and voice preferences.',
          'Models — view profile-to-model routing policy.',
          'Analytics — usage, cost, and latency metrics.',
          'Admin — user management and roles, providers, routing policy, audit logs, and system status (admin-only).',
        ],
      },
      {
        title: 'Authentication',
        body: 'API requests require the same Clerk-issued bearer token used by the web app. Admin endpoints additionally require the calling user to be an admin (via the env allowlist or the DB-assigned admin role).',
      },
      {
        title: 'Operational endpoints',
        body: 'Internal endpoints such as smoke checks and profile consolidation are intended for deployment automation, cron jobs, and health checks. Protect them with the configured internal secret and avoid exposing them as public user workflows.',
      },
      {
        title: 'Integration habit',
        body: 'When integrating directly, start from the OpenAPI schema, use Clerk bearer tokens, respect admin-only boundaries, and test against a local or scratch database before touching production.',
      },
    ],
  },
  {
    id: 'tips',
    label: 'Get the most out of Fronei',
    icon: 'ti-bulb',
    title: 'Getting the best out of Fronei',
    summary: 'Practical habits that consistently improve answer quality and save time.',
    sections: [
      {
        title: 'Be specific about the outcome',
        body: 'State what you want to do with the answer — decide, send, present, build — and to whom. "Draft an email to the steering committee recommending we delay the migration" gets a far better result than "tell me about the migration".',
      },
      {
        title: 'Match quality and mode to the stakes',
        body: 'Use Quick/Smart for exploration and routine writing, and reserve Thorough plus Research for decisions you will defend or share widely. Pick an output mode (exec-ready, client-ready, architecture, etc.) when the audience is fixed, so you are not reformatting the response yourself afterward.',
      },
      {
        title: 'Let artifacts do the structuring',
        body: 'For recurring deliverables — ADRs, trade-off matrices, risk registers, exec briefs — use the matching artifact type instead of asking for free-form text. The structure is consistent every time, which makes outputs easier to review and reuse.',
      },
      {
        title: 'Build context once, reuse it everywhere',
        body: 'Add writing samples and memories early so tone and preferences carry across conversations automatically. For a multi-step initiative, keep related work in one conversation thread so Fronei retains the relevant context.',
      },
      {
        title: 'Use Pushback to stress-test your own thinking',
        body: 'Before committing to a recommendation, ask Fronei to critique it in Pushback mode — it will challenge assumptions, surface risks, and point out what a skeptical reviewer would push on.',
      },
      {
        title: 'Verify before you act',
        body: 'Research mode and web search add freshness and citations, but always check sources for decisions with real consequences (financial, legal, contractual). Use the execution panel (dev mode) if you want to understand exactly how an answer was produced.',
      },
    ],
  },
]

function SettingsView({
  onClose,
  theme,
  accentTheme,
  onThemeChange,
  onAccentThemeChange,
  devMode,
  onDevModeChange,
  showWebSearch,
  onShowWebSearchChange,
  twinProfile,
  twinSamples,
  newSampleText,
  newSampleLabel,
  onNewSampleTextChange,
  onNewSampleLabelChange,
  onAddSample,
  onDeleteSample,
  onReExtract,
  sampleSubmitting,
  memories,
  memoriesLoaded,
  personalProfile,
  profileLoaded,
  onUpdateMemory,
  onDeleteMemory,
  onClearMemories,
  onSaveProfileOverrides,
  userName,
  userDomain,
  onUserNameChange,
  onUserDomainChange,
  onUserNameSave,
  onUserDomainSave,
  persona,
  visibleArtifacts,
  onPersonaChange,
  onArtifactToggle,
  outputMode,
  onOutputModeChange,
  quality,
  onQualityChange,
  isAdmin,
  apiFetch,
  initialTab,
}: {
  onClose: () => void
  theme: 'dark' | 'light'
  accentTheme: AccentTheme
  onThemeChange: (theme: 'dark' | 'light') => void
  onAccentThemeChange: (accent: AccentTheme) => void
  devMode: boolean
  onDevModeChange: (v: boolean) => void
  showWebSearch: boolean
  onShowWebSearchChange: (v: boolean) => void
  twinProfile: TwinProfile | null
  twinSamples: WritingSample[]
  newSampleText: string
  newSampleLabel: string
  onNewSampleTextChange: (v: string) => void
  onNewSampleLabelChange: (v: string) => void
  onAddSample: () => void
  onDeleteSample: (id: number) => void
  onReExtract: () => void
  sampleSubmitting: boolean
  memories: MemoryItem[]
  memoriesLoaded: boolean
  personalProfile: PersonalContextProfile | null
  profileLoaded: boolean
  onUpdateMemory: (id: number, patch: MemoryPatch) => Promise<void>
  onDeleteMemory: (id: number) => void
  onClearMemories: () => void
  onSaveProfileOverrides: (overrides: Record<string, unknown>) => Promise<void>
  userName: string
  userDomain: string
  onUserNameChange: (v: string) => void
  onUserDomainChange: (v: string) => void
  onUserNameSave: (v: string) => void
  onUserDomainSave: (v: string) => void
  persona: PersonaId
  visibleArtifacts: ArtifactType[]
  onPersonaChange: (id: PersonaId) => void
  onArtifactToggle: (type: ArtifactType) => void
  outputMode: OutputMode
  onOutputModeChange: (mode: OutputMode) => void
  quality: Quality
  onQualityChange: (quality: Quality) => void
  isAdmin: boolean
  apiFetch: (path: string, options?: RequestInit) => Promise<Response>
  initialTab?: SettingsTab
}) {
  const [tab, setTab] = useState<SettingsTab>(initialTab ?? 'general')
  const [guidePage, setGuidePage] = useState<UserGuidePageId>('start')
  const [memoryCategoryFilter, setMemoryCategoryFilter] = useState('all')
  const [showInactiveMemories, setShowInactiveMemories] = useState(false)
  const activeGuidePage = USER_GUIDE_PAGES.find(page => page.id === guidePage) ?? USER_GUIDE_PAGES[0]
  const profile = personalProfile?.profile ?? {}
  const profileOverrides = isRecord(profile.overrides) ? profile.overrides : {}
  const profileField = (key: string): unknown => (
    profileOverrides[key] !== undefined ? profileOverrides[key] : profile[key]
  )
  const profileSummary = [
    ['Role', profileField('role')],
    ['Company', profileField('company')],
    ['Location', profileField('location')],
    ['Bio', profileField('bio')],
  ]
  const activeProjects = profileList(profileField('active_projects'))
  const keyPreferences = profileList(profileField('key_preferences'))
  const constraints = profileList(profileField('constraints'))
  const communicationStyle = profileField('communication_style')
  const memoryCategories = Array.from(new Set(memories.map(m => m.category).filter(Boolean))).sort()
  const visibleMemories = memories
    .filter(m => showInactiveMemories || m.status === 'active')
    .filter(m => memoryCategoryFilter === 'all' || m.category === memoryCategoryFilter)

  async function editProfileOverride(key: string, label: string) {
    const current = profileValueText(profileField(key))
    const next = window.prompt(`Update ${label}`, current === 'Not set' ? '' : current)
    if (next == null) return
    await onSaveProfileOverrides({ [key]: next.trim() })
  }

  async function editMemory(memory: MemoryItem) {
    const next = window.prompt('Update memory', memory.content)
    if (next == null || !next.trim() || next.trim() === memory.content) return
    await onUpdateMemory(memory.id, { content: next.trim() })
  }

  const nav: { id: SettingsTab; label: string; icon: string }[] = [
    { id: 'general',   label: 'General',   icon: 'ti-settings' },
    { id: 'dashboard', label: 'Dashboard', icon: 'ti-chart-bar' },
    { id: 'guide',     label: 'Guide',     icon: 'ti-book' },
    { id: 'voice',     label: 'My voice',  icon: 'ti-sparkles' },
    { id: 'memory',    label: 'Memory',    icon: 'ti-brain' },
    { id: 'workspace', label: 'Workspace', icon: 'ti-layout-grid' },
    { id: 'models',    label: 'Models',    icon: 'ti-route' },
    { id: 'account',   label: 'Account',   icon: 'ti-user-circle' },
    ...(isAdmin ? [{ id: 'admin' as SettingsTab, label: 'Admin', icon: 'ti-shield-lock' }] : []),
  ]

  return (
    <div className="settings-page">
      <aside className="settings-nav">
        <button className="settings-close" onClick={onClose} aria-label="Close settings" type="button">
          <i className="ti ti-x" aria-hidden="true" />
        </button>
        <div className="settings-nav-list">
          {nav.map(item => (
            <button
              key={item.id}
              className={`settings-nav-item${tab === item.id ? ' active' : ''}`}
              onClick={() => setTab(item.id)}
              type="button"
            >
              <i className={`ti ${item.icon}`} aria-hidden="true" />
              <span>{item.label}</span>
            </button>
          ))}
        </div>
      </aside>

      <section className="settings-content">
        <div className="settings-content-inner">
          <h1>{nav.find(item => item.id === tab)?.label}</h1>

          {tab === 'general' && (
            <div className="settings-card-list">
              <div className="settings-line">
                <div><strong>Appearance</strong><span>Choose the application color mode.</span></div>
                <div className="theme-btn-group">
                  <button className={`theme-btn-opt${theme === 'dark' ? ' active' : ''}`} onClick={() => onThemeChange('dark')} type="button">Dark</button>
                  <button className={`theme-btn-opt${theme === 'light' ? ' active' : ''}`} onClick={() => onThemeChange('light')} type="button">Light</button>
                </div>
              </div>
              <div className="settings-line">
                <div><strong>Accent color</strong><span>Set the Fronei interface accent.</span></div>
                <div className="theme-swatch-group">
                  {ACCENT_THEMES.map(t => (
                    <button key={t.id} className={`theme-swatch${accentTheme === t.id ? ' active' : ''}`}
                      style={{ background: t.bg }} onClick={() => onAccentThemeChange(t.id)}
                      title={t.name} aria-label={`${t.name} theme`} type="button">
                      <div className="theme-swatch-inner" style={{ background: t.dot }} />
                    </button>
                  ))}
                </div>
              </div>
              <div className="settings-line">
                <div><strong>Manual web search</strong><span>Expose a force-web option under advanced composer controls. Auto mode still lets Fronei decide when web is needed.</span></div>
                <div className="theme-btn-group">
                  <button className={`theme-btn-opt${!showWebSearch ? ' active' : ''}`} onClick={() => onShowWebSearchChange(false)} type="button">Off</button>
                  <button className={`theme-btn-opt${showWebSearch ? ' active' : ''}`} onClick={() => onShowWebSearchChange(true)} type="button">On</button>
                </div>
              </div>
              <div className="settings-line">
                <div><strong>Developer mode</strong><span>Show execution logs and model routing details.</span></div>
                <div className="theme-btn-group">
                  <button className={`theme-btn-opt${!devMode ? ' active' : ''}`} onClick={() => onDevModeChange(false)} type="button">Off</button>
                  <button className={`theme-btn-opt${devMode ? ' active' : ''}`} onClick={() => onDevModeChange(true)} type="button">On</button>
                </div>
              </div>
            </div>
          )}

          {tab === 'guide' && (
            <div className="guide-layout">
              <nav className="guide-subnav" aria-label="User guide pages">
                {USER_GUIDE_PAGES.map(page => (
                  <button
                    key={page.id}
                    className={`guide-subnav-item${activeGuidePage.id === page.id ? ' active' : ''}`}
                    onClick={() => setGuidePage(page.id)}
                    type="button"
                  >
                    <i className={`ti ${page.icon}`} aria-hidden="true" />
                    <span>{page.label}</span>
                  </button>
                ))}
              </nav>

              <article className="guide-page">
                <div className="guide-page-head">
                  <span className="guide-page-kicker">User guide</span>
                  <h2>{activeGuidePage.title}</h2>
                  <p>{activeGuidePage.summary}</p>
                </div>
                <div className="guide-section-list">
                  {activeGuidePage.sections.map(section => (
                    <section key={section.title} className="guide-section">
                      <h3>{section.title}</h3>
                      <p>{section.body}</p>
                      {section.items && (
                        <ul>
                          {section.items.map(item => <li key={item}>{item}</li>)}
                        </ul>
                      )}
                    </section>
                  ))}
                </div>
              </article>
            </div>
          )}

          {tab === 'dashboard' && (
            <DashboardView apiFetch={apiFetch} embedded isAdmin={isAdmin} />
          )}

          {tab === 'voice' && (
            <div className="settings-card-list">
              {twinProfile?.fingerprint && (
                <div className="settings-card">
                  <div className="settings-card-head"><strong>Voice fingerprint</strong><button className="toggle-chip" onClick={onReExtract} type="button">Re-extract</button></div>
                  <div className="settings-grid">
                    <span>Formality <strong>{twinProfile.fingerprint.formality}</strong></span>
                    <span>Directness <strong>{twinProfile.fingerprint.directness}</strong></span>
                    <span>Structure <strong>{twinProfile.fingerprint.structure}</strong></span>
                    <span>Depth <strong>{twinProfile.fingerprint.technical_depth}</strong></span>
                  </div>
                </div>
              )}
              <div className="settings-card">
                <strong>Writing samples</strong>
                <div className="settings-sample-list">
                  {twinSamples.length === 0 && <span className="settings-muted">No samples yet.</span>}
                  {twinSamples.map(s => (
                    <div key={s.id} className="settings-sample">
                      <div><strong>{s.label ?? 'Sample'}</strong><span>{s.char_count} chars</span><p>{s.content.slice(0, 160)}...</p></div>
                      <button className="conv-action-btn danger" onClick={() => onDeleteSample(s.id)} type="button" aria-label="Delete sample"><i className="ti ti-x" /></button>
                    </div>
                  ))}
                </div>
                <input className="conv-search-input" placeholder="Label (optional)" value={newSampleLabel} onChange={e => onNewSampleLabelChange(e.target.value)} />
                <textarea className="conv-search-input settings-textarea" placeholder="Paste your writing (min 50 chars)..." value={newSampleText} onChange={e => onNewSampleTextChange(e.target.value)} />
                <button className="nav-chat-cta settings-primary" onClick={onAddSample} disabled={sampleSubmitting || newSampleText.trim().length < 50} type="button">
                  {sampleSubmitting ? 'Adding...' : 'Add writing sample'}
                </button>
              </div>
            </div>
          )}

          {tab === 'memory' && (
            <div className="settings-card-list">
              <div className="settings-card">
                <div className="settings-card-head">
                  <div>
                    <strong>Profile summary</strong>
                    <span>Fronei uses this compact profile before ranked memories.</span>
                  </div>
                  {!profileLoaded && <span className="settings-muted">Loading...</span>}
                </div>
                {profileLoaded && (
                  <>
                    <div className="settings-profile-grid">
                      {profileSummary.map(([label, value]) => (
                        <button key={label as string} className="settings-profile-cell" onClick={() => editProfileOverride(String(label).toLowerCase(), String(label))} type="button">
                          <span>{label as string}</span>
                          <strong>{profileValueText(value)}</strong>
                        </button>
                      ))}
                    </div>
                    <div className="settings-chip-section">
                      <span>Active projects</span>
                      <div>{activeProjects.length ? activeProjects.map(item => <span key={item} className="exec-pill">{item}</span>) : <span className="settings-muted">None yet</span>}</div>
                    </div>
                    <div className="settings-chip-section">
                      <span>Key preferences</span>
                      <div>{keyPreferences.length ? keyPreferences.map(item => <span key={item} className="exec-pill">{item}</span>) : <span className="settings-muted">None yet</span>}</div>
                    </div>
                    <div className="settings-chip-section">
                      <span>Constraints</span>
                      <div>{constraints.length ? constraints.map(item => <span key={item} className="exec-pill">{item}</span>) : <span className="settings-muted">None yet</span>}</div>
                    </div>
                  </>
                )}
              </div>

              <div className="settings-card">
                <div className="settings-card-head">
                  <div>
                    <strong>What Fronei remembers</strong>
                    <span>Review, pin, correct, or scrub remembered facts.</span>
                  </div>
                  {memories.length > 0 && <button className="toggle-chip danger" onClick={onClearMemories} type="button">Clear all</button>}
                </div>
                <div className="memory-toolbar">
                  <select className="c-select" value={memoryCategoryFilter} onChange={e => setMemoryCategoryFilter(e.target.value)}>
                    <option value="all">All categories</option>
                    {memoryCategories.map(category => <option key={category} value={category}>{category}</option>)}
                  </select>
                  <button className={`toggle-chip${showInactiveMemories ? ' on' : ''}`} onClick={() => setShowInactiveMemories(v => !v)} type="button">
                    {showInactiveMemories ? 'Hide inactive' : 'Show inactive'}
                  </button>
                </div>
                {!memoriesLoaded && <span className="settings-muted">Loading...</span>}
                {memoriesLoaded && visibleMemories.length === 0 && <span className="settings-muted">Nothing saved for this view.</span>}
                {visibleMemories.map(m => (
                  <div key={m.id} className={`settings-memory${m.status !== 'active' ? ' inactive' : ''}`}>
                    <div className="memory-main">
                      <div className="memory-meta">
                        <span className="exec-pill">{m.category}</span>
                        <span className="exec-pill">{m.scope}</span>
                        <span className="exec-pill">{m.source}</span>
                        <span className={`memory-confidence confidence-${memoryConfidenceLabel(m.confidence)}`}>{memoryConfidenceLabel(m.confidence)} confidence</span>
                        {m.status !== 'active' && <span className="exec-pill">{m.status}</span>}
                      </div>
                      <p>{m.content}</p>
                      <span className="memory-footnote">Seen {m.seen_count}x{m.last_seen_at ? ` · last ${new Date(m.last_seen_at).toLocaleDateString()}` : ''}</span>
                    </div>
                    <div className="memory-actions">
                      <button className={`conv-action-btn${m.pinned ? ' active' : ''}`} onClick={() => onUpdateMemory(m.id, { pinned: !m.pinned })} type="button" aria-label={m.pinned ? 'Unpin memory' : 'Pin memory'} title={m.pinned ? 'Unpin' : 'Pin'}>
                        <i className={`ti ${m.pinned ? 'ti-star-filled' : 'ti-star'}`} />
                      </button>
                      <button className="conv-action-btn" onClick={() => editMemory(m)} type="button" aria-label="Edit memory" title="Edit">
                        <i className="ti ti-pencil" />
                      </button>
                      <button className="conv-action-btn" onClick={() => onUpdateMemory(m.id, { status: 'archived', confidence: 0 })} type="button" aria-label="Mark memory as wrong" title="Mark wrong">
                        <i className="ti ti-ban" />
                      </button>
                      <button className="conv-action-btn danger" onClick={() => onDeleteMemory(m.id)} type="button" aria-label="Delete memory" title="Delete"><i className="ti ti-x" /></button>
                    </div>
                  </div>
                ))}
              </div>

              <div className="settings-card">
                <div className="settings-card-head">
                  <div>
                    <strong>Writing style</strong>
                    <span>Derived from writing samples and profile consolidation.</span>
                  </div>
                </div>
                {profileLoaded && profileValueText(communicationStyle) !== 'Not set' ? (
                  <div className="settings-grid">
                    {Object.entries(isRecord(communicationStyle) ? communicationStyle : { style: communicationStyle }).slice(0, 8).map(([key, value]) => (
                      <span key={key}>{key.replace(/_/g, ' ')} <strong>{profileValueText(value)}</strong></span>
                    ))}
                  </div>
                ) : (
                  <span className="settings-muted">Add writing samples or keep chatting to build this profile.</span>
                )}
              </div>
            </div>
          )}

          {tab === 'workspace' && (
            <div className="settings-card-list">
              <div className="settings-card">
                <strong>Role</strong>
                <div className="persona-pills settings-pills">
                  {PERSONAS.map(p => <button key={p.id} className={`persona-pill${persona === p.id ? ' active' : ''}`} onClick={() => onPersonaChange(p.id)} type="button">{p.name}</button>)}
                </div>
              </div>
              <div className="settings-card">
                <strong>Artifacts in toolbar</strong>
                <div className="artifact-checklist">
                  {ARTIFACT_TYPES.map(a => (
                    <label key={a.value} className="artifact-check-item">
                      <input type="checkbox" checked={visibleArtifacts.includes(a.value)} onChange={() => onArtifactToggle(a.value)} />
                      <i className={`ti ${a.icon}`} /><span>{a.label}</span>
                    </label>
                  ))}
                </div>
              </div>
            </div>
          )}

          {tab === 'models' && (
            <div className="settings-card-list">
              <div className="settings-line">
                <div><strong>Default quality</strong><span>Controls the composer default for non-research prompts.</span></div>
                <select className="c-select" value={quality} onChange={e => onQualityChange(e.target.value as Quality)}>
                  <option value="quick">Quick</option><option value="smart">Smart</option><option value="thorough">Thorough</option>
                </select>
              </div>
              <div className="settings-line">
                <div><strong>Default output mode</strong><span>Used when a voice profile is available.</span></div>
                <select className="c-select" value={outputMode} onChange={e => onOutputModeChange(e.target.value as OutputMode)}>
                  {OUTPUT_MODES.map(m => <option key={m.value} value={m.value}>{m.label}</option>)}
                </select>
              </div>
            </div>
          )}

          {tab === 'account' && (
            <div className="settings-card-list">
              <div className="settings-card">
                <strong>Profile</strong>
                <label className="settings-field">Name<input className="conv-search-input" value={userName} onChange={e => onUserNameChange(e.target.value)} onBlur={e => onUserNameSave(e.target.value)} /></label>
                <label className="settings-field">Work domain<input className="conv-search-input" value={userDomain} onChange={e => onUserDomainChange(e.target.value)} onBlur={e => onUserDomainSave(e.target.value)} /></label>
              </div>
            </div>
          )}

          {tab === 'admin' && isAdmin && (
            <AdminView apiFetch={apiFetch} embedded />
          )}
        </div>
      </section>
    </div>
  )
}

// ── Document preview modal ──────────────────────────────────────────────────

const DOC_TYPE_LABELS: Record<string, string> = {
  executive_report: 'Executive report',
  proposal:         'Proposal',
  memo:             'Memo',
  technical_spec:   'Technical spec',
  meeting_notes:    'Meeting notes',
  one_pager:        'One-pager',
  letter:           'Letter',
  resume:           'Resume',
}

function DocumentPlanModal({
  brief,
  detected,
  capabilities,
  recommendations,
  onChange,
  onCapabilitiesChange,
  onClose,
  onCancel,
  onSendAsChat,
  onGenerate,
}: {
  brief: DocumentBrief
  detected?: boolean
  capabilities: DocumentPlanCapabilities
  recommendations?: DocumentPlanRecommendations
  onChange: (brief: DocumentBrief) => void
  onCapabilitiesChange: (capabilities: DocumentPlanCapabilities) => void
  onClose: () => void
  onCancel: () => void
  onSendAsChat?: () => void
  onGenerate: (brief: DocumentBrief, capabilities: DocumentPlanCapabilities) => void
}) {
  const selectedFormat = brief.outputFormats[0] ?? 'docx'
  const setFormat = (format: DocumentOutputFormat) => {
    onChange({ ...brief, outputFormats: [format] })
  }
  const hasRecommendations = !!recommendations?.deepResearch || !!recommendations?.webSearch

  return (
    <div className="doc-preview-backdrop" onClick={onClose}>
      <div
        className="doc-brief-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="doc-brief-title"
        onClick={e => e.stopPropagation()}
      >
        <header className="doc-brief-header">
          <div>
            <span className="doc-preview-type">{hasRecommendations ? 'Plan recommended' : 'Document plan'}</span>
            <h2 id="doc-brief-title">Shape this document</h2>
          </div>
          <button className="action-btn" type="button" onClick={onClose} aria-label="Close document brief">
            <i className="ti ti-x" aria-hidden="true" />
          </button>
        </header>
        <div className="doc-brief-body">
          <div className="doc-brief-grid">
            <label className="doc-brief-field">
              <span>Document type</span>
              <select value={brief.docType} onChange={e => onChange({ ...brief, docType: e.target.value })}>
                {DOCUMENT_DOC_TYPES.map(type => (
                  <option key={type} value={type}>{DOC_TYPE_LABELS[type]}</option>
                ))}
              </select>
            </label>
            <label className="doc-brief-field">
              <span>Audience</span>
              <select value={brief.audience} onChange={e => onChange({ ...brief, audience: e.target.value })}>
                {DOCUMENT_AUDIENCES.map(option => <option key={option} value={option}>{option}</option>)}
              </select>
            </label>
            <label className="doc-brief-field">
              <span>Tone</span>
              <select value={brief.tone} onChange={e => onChange({ ...brief, tone: e.target.value })}>
                {DOCUMENT_TONES.map(option => <option key={option} value={option}>{option}</option>)}
              </select>
            </label>
            <label className="doc-brief-field">
              <span>Length</span>
              <select value={brief.length} onChange={e => onChange({ ...brief, length: e.target.value })}>
                {DOCUMENT_LENGTHS.map(option => <option key={option} value={option}>{option}</option>)}
              </select>
            </label>
          </div>

          <div className="doc-brief-field">
            <span>Output</span>
            <div className="doc-format-row" role="group" aria-label="Output formats">
              <button
                type="button"
                className={`doc-format-pill${selectedFormat === 'docx' ? ' active' : ''}`}
                onClick={() => setFormat('docx')}
                aria-pressed={selectedFormat === 'docx'}
              >
                <i className="ti ti-file-type-docx" aria-hidden="true" />
                Word
              </button>
              <button
                type="button"
                className={`doc-format-pill${selectedFormat === 'markdown' ? ' active' : ''}`}
                onClick={() => setFormat('markdown')}
                aria-pressed={selectedFormat === 'markdown'}
              >
                <i className="ti ti-markdown" aria-hidden="true" />
                Markdown
              </button>
            </div>
          </div>

          <div className="doc-brief-suggestion">
            Fronei suggests {DOC_TYPE_LABELS[brief.docType] ?? 'Document'} for {brief.audience.toLowerCase()}, {brief.tone.toLowerCase()} tone, {brief.length.toLowerCase()} depth.
          </div>

          <div className="doc-plan-section">
            <div className="doc-plan-section-title">Source plan</div>
            <button
              type="button"
              className={`doc-plan-option${capabilities.webSearch ? ' active' : ''}${recommendations?.webSearch ? ' recommended' : ''}`}
              onClick={() => onCapabilitiesChange({ ...capabilities, webSearch: !capabilities.webSearch })}
            >
              <span className="doc-plan-option-main">
                <i className="ti ti-world-search" aria-hidden="true" />
                <span>Web search</span>
                {recommendations?.webSearch && <em>Recommended</em>}
              </span>
              <span className="doc-plan-option-status">{capabilities.webSearch ? 'On' : 'Off'}</span>
              {recommendations?.webSearch && (
                <span className="doc-plan-option-reason">{recommendations.webSearch.reason}</span>
              )}
            </button>
            <button
              type="button"
              className={`doc-plan-option${capabilities.deepResearch ? ' active' : ''}${recommendations?.deepResearch ? ' recommended' : ''}`}
              onClick={() => onCapabilitiesChange({
                deepResearch: !capabilities.deepResearch,
                webSearch: capabilities.deepResearch ? capabilities.webSearch : false,
              })}
            >
              <span className="doc-plan-option-main">
                <i className="ti ti-microscope" aria-hidden="true" />
                <span>Deep research</span>
                {recommendations?.deepResearch && <em>Recommended</em>}
              </span>
              <span className="doc-plan-option-status">{capabilities.deepResearch ? 'On' : 'Off'}</span>
              {recommendations?.deepResearch && (
                <span className="doc-plan-option-reason">{recommendations.deepResearch.reason}</span>
              )}
            </button>
          </div>
        </div>
        <footer className="doc-brief-footer">
          {detected && onSendAsChat && (
            <button type="button" className="doc-brief-cancel" onClick={onSendAsChat}>Send as chat</button>
          )}
          <button type="button" className="doc-brief-cancel" onClick={onCancel}>Cancel</button>
          <button
            type="button"
            className="send-btn"
            onClick={() => onGenerate({
              ...brief,
              title: brief.title.trim(),
              outputFormats: [brief.outputFormats[0] ?? 'docx'],
            }, capabilities)}
          >
            <i className="ti ti-file-plus" aria-hidden="true" />
            Start
          </button>
        </footer>
      </div>
    </div>
  )
}

function DocumentPreviewModal({ doc, onClose }: { doc: GeneratedDocument; onClose: () => void }) {
  const [html, setHtml] = useState<string | null>(null)
  const [error, setError] = useState(false)

  useEffect(() => {
    let cancelled = false
    setHtml(null)
    setError(false)
    import('mammoth')
      .then(mammoth => mammoth.convertToHtml({ arrayBuffer: base64ToArrayBuffer(doc.docxBase64) }))
      .then(result => {
        if (!cancelled) setHtml(DOMPurify.sanitize(result.value))
      })
      .catch(() => {
        if (!cancelled) setError(true)
      })
    return () => { cancelled = true }
  }, [doc.docxBase64])

  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [onClose])

  return (
    <div className="doc-preview-backdrop" onClick={onClose}>
      <div
        className="doc-preview-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="doc-preview-title"
        onClick={e => e.stopPropagation()}
      >
        <header className="doc-preview-header">
          <div>
            <span className="doc-preview-type">{DOC_TYPE_LABELS[doc.docType] ?? 'Document'}</span>
            <h2 id="doc-preview-title">{doc.title}</h2>
          </div>
          <div className="doc-preview-actions">
            <button
              className="send-btn"
              type="button"
              onClick={() => downloadBlob(base64ToBlob(doc.docxBase64, DOCX_MIME), doc.filename)}
            >
              <i className="ti ti-file-download" aria-hidden="true" />
              Download .docx
            </button>
            {doc.outputFormats?.includes('markdown') && (
              <button
                className="action-btn"
                type="button"
                onClick={() => downloadBlob(new Blob([doc.markdown], { type: 'text/markdown;charset=utf-8' }), safeDownloadName(doc.title, 'md'))}
                title="Download Markdown"
              >
                <i className="ti ti-markdown" aria-hidden="true" />
              </button>
            )}
            <button className="action-btn" type="button" onClick={onClose} aria-label="Close preview">
              <i className="ti ti-x" aria-hidden="true" />
            </button>
          </div>
        </header>
        <div className="doc-preview-body">
          {error ? (
            <div className="markdown-body doc-preview-content" dangerouslySetInnerHTML={{ __html: DOMPurify.sanitize(marked.parse(doc.markdown) as string) }} />
          ) : html === null ? (
            <div className="thinking-state">
              <div className="typing-dot"><span /><span /><span /></div>
              <span className="thinking-label">Rendering document…</span>
            </div>
          ) : (
            <div className="docx-preview-content" dangerouslySetInnerHTML={{ __html: html }} />
          )}
        </div>
      </div>
    </div>
  )
}

// ── Onboarding modal ──────────────────────────────────────────────────────────

function OnboardingModal({ onComplete }: { onComplete: (name: string, domain: string) => void }) {
  const [name,   setName]   = useState('')
  const [domain, setDomain] = useState('')
  const domainRef = useRef<HTMLInputElement>(null)
  const btnRef    = useRef<HTMLButtonElement>(null)

  return (
    <div className="onboarding-backdrop">
      <div className="onboarding-card" role="dialog" aria-modal="true" aria-labelledby="ob-title">
        <div className="onboarding-logo">
          <img src="/fronei-logo.png" alt="Fronei" style={{ maxWidth: 150, width: '100%', height: 'auto', display: 'block' }} />
        </div>
        <h2 id="ob-title" className="onboarding-heading">Hi, I&apos;m Fronei.</h2>
        <p className="onboarding-sub">
          Let me learn a little about you so I can be more helpful from the start.
        </p>

        <div className="onboarding-field">
          <label className="onboarding-label" htmlFor="ob-name">What should I call you?</label>
          <input
            id="ob-name"
            className="onboarding-input"
            type="text"
            placeholder="Your name"
            value={name}
            onChange={e => setName(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter') domainRef.current?.focus() }}
            autoFocus
          />
        </div>

        <div className="onboarding-field">
          <label className="onboarding-label" htmlFor="ob-domain">What kind of work do you do?</label>
          <input
            id="ob-domain"
            ref={domainRef}
            className="onboarding-input"
            type="text"
            placeholder="e.g. software engineer, writer, researcher"
            value={domain}
            onChange={e => setDomain(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter') btnRef.current?.click() }}
          />
        </div>

        <button
          ref={btnRef}
          className="send-btn onboarding-cta"
          onClick={() => onComplete(name, domain)}
          type="button"
        >
          Get started →
        </button>

        <button
          className="onboarding-skip"
          onClick={() => onComplete('', '')}
          type="button"
        >
          Skip for now
        </button>
      </div>
    </div>
  )
}

function elapsed(fromTs: number): string {
  const s = (Date.now() - fromTs) / 1000
  return s < 60 ? `${s.toFixed(1)}s` : `${(s / 60).toFixed(1)}m`
}

const THINKING_FRAGMENTS = [
  'parsing intent', 'checking context', 'decomposing', 'evaluating',
  'routing decision', 'complexity analysis', 'selecting model',
  'prior context', 'sub-queries', 'web search', 'memory lookup',
  'task classification', 'planning approach', 'enriching prompt',
  'architecture', 'trade-offs', 'constraints', 'dependencies',
  'orchestration', 'synthesis', 'reasoning chain', 'confidence',
  'fallback strategy', 'cost estimate', 'latency profile',
]

function ThinkingTicker({ sourceText }: { sourceText: string }) {
  const [idx, setIdx] = useState(0)

  const words = useMemo(() => {
    const msgWords = sourceText
      .split(/\s+/)
      .map(w => w.replace(/[^a-zA-Z0-9]/g, '').toLowerCase())
      .filter(w => w.length > 3)
    const pool = [...new Set([...msgWords, ...THINKING_FRAGMENTS])]
    const seed = sourceText.length || 1
    return pool.sort((a, b) => ((a.charCodeAt(0) * seed) % 97) - ((b.charCodeAt(0) * seed) % 97))
  }, [sourceText])

  useEffect(() => {
    const id = setInterval(() => setIdx(i => (i + 1) % words.length), 110)
    return () => clearInterval(id)
  }, [words])

  return (
    <div className="thinking-ticker" aria-hidden="true">
      <span key={idx} className="ticker-word">{words[idx]}</span>
    </div>
  )
}

const ARTIFACT_TICKER_FRAGMENTS: Record<string, string[]> = {
  adr: ['weighing options', 'drafting decision', 'capturing context', 'listing consequences', 'checking alternatives'],
  solution_comparison: ['comparing options', 'scoring criteria', 'weighing trade-offs', 'drafting matrix', 'summarising fit'],
  trade_off_matrix: ['mapping dimensions', 'scoring options', 'building matrix', 'weighing trade-offs', 'drafting recommendation'],
  exec_brief: ['framing the ask', 'sizing impact', 'drafting recommendation', 'checking risks', 'tightening language'],
  risk_register: ['identifying risks', 'scoring impact', 'drafting mitigations', 'checking likelihood', 'prioritising risks'],
  nfr_analysis: ['reviewing requirements', 'scoring trade-offs', 'checking constraints', 'drafting analysis', 'summarising fit'],
  steering_update: ['summarising status', 'checking milestones', 'drafting update', 'flagging risks', 'next steps'],
}

function ArtifactTicker({ artifactType }: { artifactType: ArtifactType | null }) {
  const [idx, setIdx] = useState(0)
  const words = useMemo(() => {
    const base = (artifactType && ARTIFACT_TICKER_FRAGMENTS[artifactType]) || ['drafting document', 'structuring sections', 'checking details']
    return [...base, 'almost there']
  }, [artifactType])

  useEffect(() => {
    const id = setInterval(() => setIdx(i => (i + 1) % words.length), 1100)
    return () => clearInterval(id)
  }, [words])

  const label = artifactType ? ARTIFACT_TYPES.find(a => a.value === artifactType)?.label : null

  return (
    <div className="thinking-state artifact-ticker">
      <div className="typing-dot"><span /><span /><span /></div>
      <span className="thinking-label">
        {label ? `Building ${label}` : 'Building artifact'} — <span key={idx} className="ticker-word">{words[idx]}</span>…
      </span>
    </div>
  )
}

function PipelineLog({
  steps,
  startTs,
  sourceText,
  subCompletions = new Map(),
}: {
  steps: PipelineStep[]
  startTs: number
  sourceText: string
  subCompletions?: Map<number, PipelineStep>
}) {
  const stage_icons: Record<string, string> = {
    planning: 'ti-brain',
    routing: 'ti-git-branch',
    working: 'ti-adjustments-horizontal',
    synthesising: 'ti-layers-union',
    refining: 'ti-sparkles',
    searching: 'ti-world-search',
    reading: 'ti-file-search',
    extracting: 'ti-filter',
    checking: 'ti-shield-check',
    verifying: 'ti-rosette-discount-check',
    complete: 'ti-checks',
  }

  return (
    <div className="pipeline-log">
      {steps.map((step, i) => {
        const isLast = i === steps.length - 1
        const icon = stage_icons[step.stage] ?? 'ti-point'
        return (
          <div key={i} className={`pl-step${isLast ? ' pl-step-active' : ' pl-step-done'}`}>
            <div className="pl-dot">
              <i className={`ti ${icon}`} aria-hidden="true" />
            </div>
            <div className="pl-body">
              <div className="pl-row">
                <span className="pl-message">{step.message}</span>
                {!isLast && (
                  <span className="pl-elapsed">{elapsed(step.ts)}</span>
                )}
                {isLast && (
                  <span className="pl-elapsed pl-elapsed-live">
                    {elapsed(startTs)}
                  </span>
                )}
              </div>

              {step.stage === 'routing' && step.sub_queries && step.sub_queries.length > 0 && (
                <div className="pl-subqueries">
                  {step.sub_queries.map((sq, j) => (
                    <div key={j} className="pl-sq">
                      <span className="pl-sq-num">{j + 1}</span>
                      <span className="pl-sq-text">{sq.query}</span>
                      {sq.task_type && <span className="pl-sq-tag">{sq.task_type}</span>}
                    </div>
                  ))}
                </div>
              )}

              {step.stage === 'working' && step.queries && step.queries.length > 0 && (
                <div className="pl-subqueries pl-subqueries-parallel">
                  {step.queries.map((query, j) => {
                    const done = subCompletions.get(j)
                    const p = done?.model ? getProvider(done.model) : null
                    return (
                      <div
                        key={j}
                        className={`pl-sq-parallel${done ? ' pl-sq-parallel-done' : ' pl-sq-parallel-running'}`}
                      >
                        <span className="pl-sq-parallel-status">
                          {done
                            ? <i className="ti ti-check" aria-hidden="true" />
                            : <span className="pl-sq-spinner" />}
                        </span>
                        <span className="pl-sq-parallel-text">{query}</span>
                        {done && (
                          <span className="pl-sq-parallel-meta">
                            {p && (
                              <span style={{ color: p.color, fontSize: 10 }}>{p.name}</span>
                            )}
                            <span>{((done.latency_ms ?? 0) / 1000).toFixed(1)}s</span>
                            {done.cost_usd != null && (
                              <span>${done.cost_usd.toFixed(5)}</span>
                            )}
                          </span>
                        )}
                      </div>
                    )
                  })}
                </div>
              )}
            </div>
          </div>
        )
      })}

      {steps.length > 0 && steps[steps.length - 1].stage === 'planning' && (
        <ThinkingTicker sourceText={sourceText} />
      )}

      <div className="pl-working">
        <div className="typing-dot" style={{ padding: 0 }}>
          <span /><span /><span />
        </div>
      </div>
    </div>
  )
}

function DashboardView({
  apiFetch,
  embedded = false,
  isAdmin = false,
}: {
  apiFetch: (path: string, options?: RequestInit) => Promise<Response>
  embedded?: boolean
  isAdmin?: boolean
}) {
  const [range, setRange] = useState<Range>('7d')
  const [data, setData] = useState<AnalyticsData | null>(null)
  const [ops, setOps] = useState<any>(null)
  const [budgetInput, setBudgetInput] = useState('')
  const [adminOverrideEnabled, setAdminOverrideEnabled] = useState(true)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError('')
    apiFetch(`/analytics?range=${range}`)
      .then(r => { if (!r.ok) throw new Error('Failed to load'); return r.json() })
      .then((d: AnalyticsData) => {
        if (cancelled) return
        setData(d)
      })
      .catch(e => { if (!cancelled) setError(e instanceof Error ? e.message : 'Failed to load') })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  // apiFetch is intentionally omitted: it is recreated by Home on render.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [range])

  useEffect(() => {
    if (!isAdmin) return
    let cancelled = false
    apiFetch('/admin/ops-summary')
      .then(r => r.ok ? r.json() : null)
      .then(d => {
        if (cancelled || !d) return
        setOps(d)
        setBudgetInput(d.budget?.monthly_budget_usd == null ? '' : String(d.budget.monthly_budget_usd))
        setAdminOverrideEnabled(Boolean(d.budget?.admin_override_enabled))
      })
      .catch(() => {})
    return () => { cancelled = true }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isAdmin])

  async function saveGlobalBudget() {
    const amount = budgetInput.trim() ? Number(budgetInput) : null
    const res = await apiFetch('/admin/global-budget', {
      method: 'PATCH',
      body: JSON.stringify({
        monthly_budget_usd: Number.isFinite(amount) ? amount : null,
        admin_override_enabled: adminOverrideEnabled,
      }),
    })
    if (!res.ok) {
      setError('Failed to update global budget')
      return
    }
    const budget = await res.json()
    setOps((prev: any) => prev ? { ...prev, budget } : { budget, recommendations: [] })
  }

  const s = data?.summary

  return (
    <>
      {!embedded && <div className="topbar">
        <span className="topbar-title">Usage analytics</span>
      </div>}
      <div className={embedded ? 'dash-content dash-content-embedded' : 'dash-content'}>
        <div className="dash-header">
          <div>
            <div className="eyebrow"><span className="eyebrow-dot" />Analytics</div>
            {!embedded && <h1>Dashboard</h1>}
          </div>
          <div className="range-group">
            {RANGES.map(r => (
              <button key={r.value} className={`range-btn${range === r.value ? ' active' : ''}`} onClick={() => setRange(r.value)} type="button">
                {r.label}
              </button>
            ))}
          </div>
        </div>

        {error && <div className="error-bar" role="alert">{error}</div>}
        {loading && !data && <div className="dash-loading">Loading...</div>}

        {data && s && (
          <>
            {isAdmin && ops?.budget && (
              <div className="settings-card-list dashboard-admin-block">
                <div className="settings-card">
                  <div className="settings-card-head">
                    <div>
                      <strong>Global monthly budget</strong>
                      <span>Admins can configure the cap and override behavior from here.</span>
                    </div>
                    <span className={`exec-pill ${ops.budget.status === 'exceeded' ? 'danger-pill' : ops.budget.status === 'warning' ? 'warn-pill' : 'ok-pill'}`}>{ops.budget.status}</span>
                  </div>
                  <div className="dash-summary-grid">
                    <div className="card stat-summary-card"><div className="stat-summary-label">This month</div><div className="stat-summary-value green">{fmt$(ops.budget.month_spend ?? 0, 4)}</div></div>
                    <div className="card stat-summary-card"><div className="stat-summary-label">Global cap</div><div className="stat-summary-value">{ops.budget.monthly_budget_usd == null ? 'Off' : fmt$(ops.budget.monthly_budget_usd, 2)}</div></div>
                    <div className="card stat-summary-card"><div className="stat-summary-label">Used</div><div className="stat-summary-value blue">{ops.budget.percent_used == null ? '—' : `${ops.budget.percent_used}%`}</div></div>
                    <div className="card stat-summary-card"><div className="stat-summary-label">Admin override</div><div className="stat-summary-value">{ops.budget.admin_override_enabled ? 'On' : 'Off'}</div></div>
                  </div>
                  <div className="settings-line">
                    <div><strong>Monthly cap</strong><span>Leave blank to disable the global cap.</span></div>
                    <input className="conv-search-input budget-input" value={budgetInput} onChange={e => setBudgetInput(e.target.value)} placeholder="No cap" inputMode="decimal" />
                  </div>
                  <div className="settings-line">
                    <div><strong>Admin override</strong><span>When enabled, admins can continue after the global cap is reached.</span></div>
                    <div className="theme-btn-group">
                      <button className={`theme-btn-opt${adminOverrideEnabled ? ' active' : ''}`} onClick={() => setAdminOverrideEnabled(true)} type="button">On</button>
                      <button className={`theme-btn-opt${!adminOverrideEnabled ? ' active' : ''}`} onClick={() => setAdminOverrideEnabled(false)} type="button">Off</button>
                    </div>
                  </div>
                  <button className="nav-chat-cta settings-primary" onClick={saveGlobalBudget} type="button">Save budget</button>
                </div>

                <div className="settings-card">
                  <strong>Recommended actions</strong>
                  {ops.recommendations?.length ? ops.recommendations.map((rec: any, idx: number) => (
                    <div key={`${rec.title}-${idx}`} className="settings-memory">
                      <span className={`exec-pill ${rec.severity === 'high' ? 'danger-pill' : rec.severity === 'medium' ? 'warn-pill' : ''}`}>{rec.severity}</span>
                      <div className="memory-main">
                        <p><strong>{rec.title}</strong></p>
                        <p>{rec.detail}</p>
                        <span className="memory-footnote">{rec.action}</span>
                      </div>
                    </div>
                  )) : <span className="settings-muted">No recommended actions right now.</span>}
                </div>

                <div className="settings-card">
                  <strong>Pending tasks and insights</strong>
                  <div className="settings-grid">
                    <span>Pending approvals <strong>{ops.pending?.user_approvals ?? 0}</strong></span>
                    <span>Failed research <strong>{ops.pending?.failed_research_runs ?? 0}</strong></span>
                    <span>Users near budget <strong>{ops.pending?.users_near_budget?.length ?? 0}</strong></span>
                    <span>Recent errors <strong>{ops.recent_errors?.length ?? 0}</strong></span>
                  </div>
                </div>
              </div>
            )}

            <div className="dash-summary-grid">
              <div className="card stat-summary-card"><div className="stat-summary-label">Total spent</div><div className="stat-summary-value green">{fmt$(s.total_cost, 4)}</div></div>
              <div className="card stat-summary-card"><div className="stat-summary-label">Requests</div><div className="stat-summary-value">{s.total_requests.toLocaleString()}</div></div>
              <div className="card stat-summary-card"><div className="stat-summary-label">Tokens</div><div className="stat-summary-value">{s.total_tokens >= 1000 ? (s.total_tokens / 1000).toFixed(1) : s.total_tokens}<span className="stat-summary-unit">{s.total_tokens >= 1000 ? 'K' : ''}</span></div></div>
              <div className="card stat-summary-card"><div className="stat-summary-label">Avg latency</div><div className="stat-summary-value blue">{s.avg_latency_ms >= 1000 ? (s.avg_latency_ms / 1000).toFixed(1) : Math.round(s.avg_latency_ms)}<span className="stat-summary-unit">{s.avg_latency_ms >= 1000 ? 's' : 'ms'}</span></div></div>
            </div>

            <div className="card">
              <div className="chart-card-title">Cost over time</div>
              {data.cost_by_day.length > 0 ? (
                <ResponsiveContainer width="100%" height={200}>
                  <LineChart data={data.cost_by_day} margin={{ top: 4, right: 12, bottom: 0, left: 4 }}>
                    <CartesianGrid strokeDasharray="3 3" stroke="var(--bd)" />
                    <XAxis dataKey="date" stroke="var(--bd)" tick={{ fontSize: 11, fill: 'var(--t5)' }} />
                    <YAxis stroke="var(--bd)" tick={{ fontSize: 11, fill: 'var(--t5)' }} tickFormatter={v => fmt$(Number(v), 4)} width={76} />
                    <Tooltip contentStyle={TOOLTIP_STYLE} labelStyle={{ color: 'var(--t3)' }} formatter={v => [fmt$(Number(v ?? 0), 6), 'Cost']} />
                    <Line type="monotone" dataKey="cost" stroke="#7c3aed" strokeWidth={2} dot={{ r: 3, fill: '#7c3aed', strokeWidth: 0 }} activeDot={{ r: 5, fill: '#a78bfa' }} />
                  </LineChart>
                </ResponsiveContainer>
              ) : <div className="chart-empty">No cost data for this period.</div>}
            </div>

            <div className="dash-two-col">
              <div className="card">
                <div className="chart-card-title">Model usage</div>
                {data.model_usage.length > 0 ? (
                  <ResponsiveContainer width="100%" height={220}>
                    <BarChart data={data.model_usage.slice(0, 7)} margin={{ top: 4, right: 8, bottom: 42, left: 4 }}>
                      <CartesianGrid strokeDasharray="3 3" stroke="var(--bd)" vertical={false} />
                      <XAxis dataKey="model" stroke="var(--bd)" tick={{ fontSize: 10, fill: 'var(--t5)' }} tickFormatter={shortModel} angle={-35} textAnchor="end" interval={0} />
                      <YAxis stroke="var(--bd)" tick={{ fontSize: 11, fill: 'var(--t5)' }} allowDecimals={false} />
                      <Tooltip contentStyle={TOOLTIP_STYLE} labelStyle={{ color: 'var(--t3)' }} labelFormatter={(l: unknown) => shortModel(String(l))} formatter={(v, name) => [name === 'total_cost' ? fmt$(Number(v ?? 0), 6) : Number(v ?? 0), name === 'total_cost' ? 'Cost' : 'Requests']} />
                      <Bar dataKey="requests" name="requests" radius={[4,4,0,0]} maxBarSize={36}>
                        {data.model_usage.slice(0, 7).map((_, i) => <Cell key={i} fill={CHART_COLORS[i % CHART_COLORS.length]} />)}
                      </Bar>
                    </BarChart>
                  </ResponsiveContainer>
                ) : <div className="chart-empty">No data.</div>}
              </div>

              <div className="card">
                <div className="chart-card-title">Task distribution</div>
                {data.task_distribution.length > 0 ? (
                  <ResponsiveContainer width="100%" height={220}>
                    <BarChart data={data.task_distribution} layout="vertical" margin={{ top: 4, right: 16, bottom: 4, left: 56 }}>
                      <CartesianGrid strokeDasharray="3 3" stroke="var(--bd)" horizontal={false} />
                      <XAxis type="number" stroke="var(--bd)" tick={{ fontSize: 11, fill: 'var(--t5)' }} allowDecimals={false} />
                      <YAxis dataKey="task_type" type="category" stroke="var(--bd)" tick={{ fontSize: 11, fill: 'var(--t3)' }} width={56} />
                      <Tooltip contentStyle={TOOLTIP_STYLE} labelStyle={{ color: 'var(--t3)' }} formatter={v => [Number(v ?? 0), 'Requests']} />
                      <Bar dataKey="count" name="count" radius={[0,4,4,0]} maxBarSize={20}>
                        {data.task_distribution.map((_, i) => <Cell key={i} fill={CHART_COLORS[i % CHART_COLORS.length]} />)}
                      </Bar>
                    </BarChart>
                  </ResponsiveContainer>
                ) : <div className="chart-empty">No data.</div>}
              </div>
            </div>
          </>
        )}
      </div>
    </>
  )
}

type AdminUserRow = {
  user_id: string
  email: string | null
  name: string | null
  status: string
  role: string
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

type AdminTab = 'overview' | 'users' | 'usage' | 'providers' | 'routing' | 'research' | 'errors' | 'audit' | 'system'

function AdminView({
  apiFetch,
  embedded = false,
}: {
  apiFetch: (path: string, options?: RequestInit) => Promise<Response>
  embedded?: boolean
}) {
  const [tab, setTab] = useState<AdminTab>('overview')
  const [overview, setOverview] = useState<Record<string, number> | null>(null)
  const [users, setUsers] = useState<AdminUserRow[]>([])
  const [usage, setUsage] = useState<any>(null)
  const [providers, setProviders] = useState<any>(null)
  const [providerTestResults, setProviderTestResults] = useState<any>({})
  const [policy, setPolicy] = useState<any>(null)
  const [research, setResearch] = useState<any[]>([])
  const [errors, setErrors] = useState<any[]>([])
  const [audit, setAudit] = useState<any[]>([])
  const [system, setSystem] = useState<any>(null)
  const [selectedUser, setSelectedUser] = useState<any>(null)
  const [userModalOpen, setUserModalOpen] = useState(false)
  const [userLoading, setUserLoading] = useState(false)
  const [userModalError, setUserModalError] = useState('')
  const [userQuery, setUserQuery] = useState('')
  const [routeMessage, setRouteMessage] = useState('Create a production enterprise architecture for a model router')
  const [routeResult, setRouteResult] = useState<RouteDecision | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  const nav: { id: AdminTab; label: string; icon: string }[] = [
    { id: 'overview',  label: 'Overview',  icon: 'ti-layout-dashboard' },
    { id: 'users',     label: 'Users',     icon: 'ti-users' },
    { id: 'usage',     label: 'Usage',     icon: 'ti-chart-bar' },
    { id: 'providers', label: 'Providers', icon: 'ti-plug-connected' },
    { id: 'routing',   label: 'Routing',   icon: 'ti-route' },
    { id: 'research',  label: 'Research',  icon: 'ti-microscope' },
    { id: 'errors',    label: 'Errors',    icon: 'ti-alert-triangle' },
    { id: 'audit',     label: 'Audit',     icon: 'ti-clipboard-list' },
    { id: 'system',    label: 'System',    icon: 'ti-server-cog' },
  ]

  async function json(path: string, options: RequestInit = {}) {
    const res = await apiFetch(path, options)
    if (!res.ok) {
      const body = await res.json().catch(() => ({ detail: 'Admin request failed' }))
      throw new Error(body.detail || 'Admin request failed')
    }
    return res.json()
  }

  async function loadAll() {
    setBusy(true)
    setError('')
    const calls = [
      ['overview', json('/admin/overview')],
      ['users', json(`/admin/users${userQuery.trim() ? `?query=${encodeURIComponent(userQuery.trim())}` : ''}`)],
      ['usage', json('/admin/usage?range=7d')],
      ['providers', json('/admin/providers')],
      ['policy', json('/admin/routing/policy')],
      ['research', json('/admin/research-runs')],
      ['errors', json('/admin/errors')],
      ['audit', json('/admin/audit')],
      ['system', json('/admin/system')],
    ] as const
    const results = await Promise.allSettled(calls.map(([, promise]) => promise))
    const failures: string[] = []
    results.forEach((result, idx) => {
      const key = calls[idx][0]
      if (result.status === 'rejected') {
        failures.push(`${key}: ${result.reason instanceof Error ? result.reason.message : 'failed'}`)
        return
      }
      const value = result.value
      if (key === 'overview') setOverview(value)
      if (key === 'users') setUsers(value.items ?? [])
      if (key === 'usage') setUsage(value)
      if (key === 'providers') setProviders(value)
      if (key === 'policy') setPolicy(value)
      if (key === 'research') setResearch(value.items ?? [])
      if (key === 'errors') setErrors(value.items ?? [])
      if (key === 'audit') setAudit(value.items ?? [])
      if (key === 'system') setSystem(value)
    })
    if (failures.length > 0) {
      setError(failures.join(' · '))
    }
    setBusy(false)
  }

  useEffect(() => {
    loadAll()
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  function userLabel(userId: string, identity?: { name?: string | null; email?: string | null }): string {
    const u = identity ?? users.find(x => x.user_id === userId)
    return u?.name || u?.email || userId
  }

  async function loadUser(userId: string) {
    setUserModalOpen(true)
    setUserLoading(true)
    try {
      setSelectedUser(await json(`/admin/users/${encodeURIComponent(userId)}`))
    } finally {
      setUserLoading(false)
    }
  }

  function closeUserModal() {
    setUserModalOpen(false)
    setSelectedUser(null)
    setUserModalError('')
  }

  async function setUserStatus(userId: string, status: 'active' | 'suspended') {
    try {
      const existing = users.find(u => u.user_id === userId)
      await json(`/admin/users/${encodeURIComponent(userId)}/control`, {
        method: 'PATCH',
        body: JSON.stringify({
          status,
          monthly_budget_usd: existing?.monthly_budget_usd ?? null,
          notes: selectedUser?.control?.notes ?? null,
        }),
      })
      setUserModalError('')
      await loadAll()
      await loadUser(userId)
    } catch (e) {
      setUserModalError(e instanceof Error ? e.message : 'Status update failed')
    }
  }

  async function setMonthlyBudget(userId: string, value: string) {
    try {
      const amount = value.trim() ? Number(value) : null
      const existing = users.find(u => u.user_id === userId)
      await json(`/admin/users/${encodeURIComponent(userId)}/control`, {
        method: 'PATCH',
        body: JSON.stringify({
          status: existing?.status ?? 'active',
          monthly_budget_usd: Number.isFinite(amount) ? amount : null,
          notes: selectedUser?.control?.notes ?? null,
        }),
      })
      setUserModalError('')
      await loadAll()
      await loadUser(userId)
    } catch (e) {
      setUserModalError(e instanceof Error ? e.message : 'Budget update failed')
    }
  }

  async function setUserRole(userId: string, role: 'user' | 'admin') {
    try {
      await json(`/admin/users/${encodeURIComponent(userId)}/role`, {
        method: 'PATCH',
        body: JSON.stringify({ role }),
      })
      setUserModalError('')
      await loadAll()
      await loadUser(userId)
    } catch (e) {
      setUserModalError(e instanceof Error ? e.message : 'Role update failed')
    }
  }

  async function privacyDelete(userId: string, body: Record<string, boolean>) {
    try {
      const dryRun = await json(`/admin/users/${encodeURIComponent(userId)}/privacy-delete?dry_run=true`, {
        method: 'POST',
        body: JSON.stringify(body),
      })
      const selectedCounts = Object.entries(dryRun.counts ?? {})
        .filter(([, count]) => Number(count) > 0)
        .map(([name, count]) => `${count} ${name.replace(/_/g, ' ')}`)
        .join(', ') || 'no matching records'
      const typed = window.prompt(
        `This will permanently delete ${selectedCounts} for ${userId}.\n\nType the user id to confirm.`,
      )
      if (typed !== userId) return
      await json(`/admin/users/${encodeURIComponent(userId)}/privacy-delete`, {
        method: 'POST',
        body: JSON.stringify({ ...body, confirm_user_id: userId }),
      })
      setUserModalError('')
      await loadAll()
      await loadUser(userId)
    } catch (e) {
      setUserModalError(e instanceof Error ? e.message : 'Privacy action failed')
    }
  }

  async function testRoute() {
    setRouteResult(await json('/admin/routing/test', {
      method: 'POST',
      body: JSON.stringify({ message: routeMessage, profile: 'balanced' }),
    }))
  }

  async function testProvider(name: string) {
    setProviderTestResults((prev: any) => ({ ...prev, [name]: { loading: true } }))
    try {
      const result = await json('/admin/providers/test', {
        method: 'POST',
        body: JSON.stringify({ provider: name }),
      })
      setProviderTestResults((prev: any) => ({ ...prev, [name]: result }))
    } catch (e) {
      setProviderTestResults((prev: any) => ({
        ...prev,
        [name]: { success: false, error: e instanceof Error ? e.message : 'Test failed' },
      }))
    }
  }

  return (
    <>
      {!embedded && (
        <div className="topbar">
          <span className="topbar-title">Admin</span>
          <button className="topbar-icon-btn" onClick={loadAll} disabled={busy} title="Refresh admin data" aria-label="Refresh admin data">
            <i className={`ti ${busy ? 'ti-loader-2' : 'ti-refresh'}`} />
          </button>
        </div>
      )}
      <div className={embedded ? 'admin-content admin-content-embedded' : 'admin-content'}>
        <div className="admin-tabs">
          {nav.map(item => (
            <button key={item.id} className={`admin-tab${tab === item.id ? ' active' : ''}`} onClick={() => setTab(item.id)} type="button">
              <i className={`ti ${item.icon}`} aria-hidden="true" />
              <span>{item.label}</span>
            </button>
          ))}
          {embedded && (
            <button className="topbar-icon-btn" onClick={loadAll} disabled={busy} title="Refresh admin data" aria-label="Refresh admin data" type="button" style={{ marginLeft: 'auto' }}>
              <i className={`ti ${busy ? 'ti-loader-2' : 'ti-refresh'}`} />
            </button>
          )}
        </div>

        {error && <div className="error-bar" role="alert">{error}</div>}

        {busy && (
          <div className="admin-loading-overlay">
            <i className="ti ti-loader-2 spin" />
            <span>Loading…</span>
          </div>
        )}

        {tab === 'overview' && overview && (
          <>
            <div className="dash-summary-grid">
              <div className="card stat-summary-card"><div className="stat-summary-label">Spend today</div><div className="stat-summary-value green">{fmt$(overview.spend_today ?? 0, 4)}</div></div>
              <div className="card stat-summary-card"><div className="stat-summary-label">Requests today</div><div className="stat-summary-value">{overview.requests_today}</div></div>
              <div className="card stat-summary-card"><div className="stat-summary-label">Errors today</div><div className={`stat-summary-value${overview.errors_today > 0 ? ' warn' : ''}`}>{overview.errors_today}</div></div>
              <div className="card stat-summary-card"><div className="stat-summary-label">Running research</div><div className="stat-summary-value blue">{overview.running_research_runs}</div></div>
            </div>

            <div className="dash-two-col">
              <div className="card">
                <div className="chart-card-title">Cost — last 7 days</div>
                {usage?.cost_by_day?.length ? (
                  <ResponsiveContainer width="100%" height={180}>
                    <LineChart data={usage.cost_by_day} margin={{ top: 4, right: 12, bottom: 0, left: 4 }}>
                      <CartesianGrid strokeDasharray="3 3" stroke="var(--bd)" />
                      <XAxis dataKey="date" stroke="var(--bd)" tick={{ fontSize: 11, fill: 'var(--t5)' }} />
                      <YAxis stroke="var(--bd)" tick={{ fontSize: 11, fill: 'var(--t5)' }} tickFormatter={v => fmt$(Number(v), 4)} width={76} />
                      <Tooltip contentStyle={TOOLTIP_STYLE} labelStyle={{ color: 'var(--t3)' }} formatter={v => [fmt$(Number(v ?? 0), 6), 'Cost']} />
                      <Line type="monotone" dataKey="cost" stroke="#7c3aed" strokeWidth={2} dot={{ r: 3, fill: '#7c3aed', strokeWidth: 0 }} activeDot={{ r: 5, fill: '#a78bfa' }} />
                    </LineChart>
                  </ResponsiveContainer>
                ) : <div className="chart-empty">No cost data for this period.</div>}
              </div>

              <div className="card">
                <div className="chart-card-title">Provider health</div>
                <div className="provider-grid">
                  {providers?.providers?.filter((p: any) => p.circuit).map((p: any) => {
                    const errCount = providers.recent_error_counts?.[p.name] ?? 0
                    return (
                      <div key={p.key} className="provider-row">
                        <span>{p.name}</span>
                        {p.circuit?.open ? (
                          <span className="warn-text">Circuit open · retry in {p.circuit.cooldown_remaining_s}s</span>
                        ) : p.circuit?.consecutive_failures > 0 ? (
                          <span className="muted-text">{p.circuit.consecutive_failures} recent failure(s)</span>
                        ) : (
                          <span className="ok-text">Healthy</span>
                        )}
                        <span className={errCount > 0 ? 'warn-text' : 'muted-text'}>
                          {errCount > 0 ? `${errCount} error${errCount === 1 ? '' : 's'} (7d)` : 'no errors (7d)'}
                        </span>
                      </div>
                    )
                  })}
                  {!providers && <span className="muted-text">Loading…</span>}
                </div>
              </div>
            </div>

            <div className="dash-two-col">
              <div className="card admin-table-card">
                <div className="admin-card-head">
                  <div className="chart-card-title" style={{ marginBottom: 0 }}>Top users (7d)</div>
                  <button className="toggle-chip" onClick={() => setTab('users')} type="button">View all</button>
                </div>
                <div className="admin-table-wrap">
                  <table className="admin-table">
                    <thead><tr><th>User</th><th>Cost</th><th>Requests</th></tr></thead>
                    <tbody>
                      {usage?.top_users?.slice(0, 5).map((u: any) => (
                        <tr key={u.user_id}><td>{userLabel(u.user_id, u)}</td><td>{fmt$(u.cost, 4)}</td><td>{u.requests}</td></tr>
                      ))}
                      {usage && (!usage.top_users || usage.top_users.length === 0) && (
                        <tr><td colSpan={3} className="muted-text">No usage in this period.</td></tr>
                      )}
                    </tbody>
                  </table>
                </div>
              </div>

              <div className="card admin-table-card">
                <div className="admin-card-head">
                  <div className="chart-card-title" style={{ marginBottom: 0 }}>Recent errors</div>
                  <button className="toggle-chip" onClick={() => setTab('errors')} type="button">View all</button>
                </div>
                <div className="admin-table-wrap">
                  <table className="admin-table">
                    <thead><tr><th>Time</th><th>Model</th><th>Error</th></tr></thead>
                    <tbody>
                      {errors.slice(0, 5).map(e => (
                        <tr key={e.id}><td>{e.created_at ? fmtTime(e.created_at) : '—'}</td><td className="mono">{e.selected_model}</td><td>{e.error}</td></tr>
                      ))}
                      {errors.length === 0 && (
                        <tr><td colSpan={3} className="muted-text">No recent errors.</td></tr>
                      )}
                    </tbody>
                  </table>
                </div>
              </div>
            </div>

            <div className="dash-summary-grid">
              <div className="card stat-summary-card"><div className="stat-summary-label">Users</div><div className="stat-summary-value">{overview.users}</div></div>
              <div className="card stat-summary-card"><div className="stat-summary-label">Conversations</div><div className="stat-summary-value">{overview.total_conversations}</div></div>
              <div className="card stat-summary-card"><div className="stat-summary-label">Memories</div><div className="stat-summary-value">{overview.total_memories}</div></div>
              <div className="card stat-summary-card"><div className="stat-summary-label">Writing samples</div><div className="stat-summary-value">{overview.total_writing_samples}</div></div>
              <div className="card stat-summary-card"><div className="stat-summary-label">Research runs (total)</div><div className="stat-summary-value">{overview.total_research_runs}</div></div>
            </div>
          </>
        )}

        {tab === 'users' && (
          <div className="card admin-table-card users-table-card">
            <div className="admin-card-head">
              <strong>Users</strong>
              <div className="admin-search">
                <input className="conv-search-input" value={userQuery} onChange={e => setUserQuery(e.target.value)} onKeyDown={e => e.key === 'Enter' && loadAll()} placeholder="Search by name, email, or user id" />
                <button className="toggle-chip" onClick={loadAll} type="button">Search</button>
              </div>
            </div>
            <div className="admin-table-wrap">
              <table className="admin-table users-table">
                <thead><tr><th>User</th><th>Status</th><th>Role</th><th>This month</th><th>Total</th><th>Requests</th><th>Data</th></tr></thead>
                <tbody>
                  {users.map(u => (
                    <tr key={u.user_id} onClick={() => loadUser(u.user_id)}>
                      <td>
                        <div className="user-cell-name">{u.name || u.email || 'Unnamed user'}</div>
                        <div className="user-cell-email">{u.name && u.email ? u.email : u.user_id}</div>
                      </td>
                      <td><span className={`exec-pill ${u.status === 'suspended' ? 'danger-pill' : u.status === 'pending' ? 'warn-pill' : ''}`}>{u.status}</span></td>
                      <td><span className={`exec-pill ${u.role === 'admin' ? 'ok-pill' : ''}`}>{u.role}</span></td>
                      <td>{fmt$(u.month_spend, 4)}{u.role !== 'admin' ? ` / $${(u.monthly_budget_usd ?? 5).toFixed(2)}` : ''}</td>
                      <td>{fmt$(u.total_spend, 4)}</td>
                      <td>{u.request_count}</td>
                      <td>{u.memory_count} mem · {u.writing_sample_count} samples · {u.research_run_count} research</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}

        {tab === 'users' && userModalOpen && (
          <div className="modal-backdrop" onClick={closeUserModal}>
            <div className="routing-modal user-detail-modal" onClick={e => e.stopPropagation()}>
              {!selectedUser || userLoading ? (
                <div className="rp-empty" style={{ padding: '40px 0' }}>
                  <i className="ti ti-loader-2 spin" style={{ fontSize: 22 }} />
                  <span>Loading user…</span>
                </div>
              ) : (
              <>
              <div className="panel-header">
                <div>
                  <div className="panel-title">User detail</div>
                  <div style={{ marginTop: 4 }}>
                    <strong>{selectedUser.name || selectedUser.email || 'Unnamed user'}</strong>
                    {selectedUser.name && selectedUser.email && <span className="muted-text" style={{ marginLeft: 8 }}>{selectedUser.email}</span>}
                  </div>
                  <div className="mono" style={{ marginTop: 4, fontSize: 11, color: 'var(--t5)' }}>{selectedUser.user_id}</div>
                </div>
                <button className="modal-close-btn" onClick={closeUserModal} type="button" aria-label="Close">×</button>
              </div>

              {userModalError && <div className="error-bar" role="alert">{userModalError}</div>}

              <div className="settings-grid">
                {Object.entries(selectedUser.counts ?? {}).map(([k, v]) => <span key={k}>{k.replace(/_/g, ' ')} <strong>{String(v)}</strong></span>)}
              </div>
              <div className="settings-line">
                <div><strong>Status</strong><span>{selectedUser.control?.status ?? 'active'}</span></div>
                <div className="theme-btn-group">
                  {selectedUser.control?.status === 'pending' && (
                    <button className="theme-btn-opt active" onClick={() => setUserStatus(selectedUser.user_id, 'active')} type="button">Activate</button>
                  )}
                  <button className={`theme-btn-opt${selectedUser.control?.status === 'active' || (!selectedUser.control?.status) ? ' active' : ''}`} onClick={() => setUserStatus(selectedUser.user_id, 'active')} type="button">Active</button>
                  <button className={`theme-btn-opt${selectedUser.control?.status === 'suspended' ? ' active' : ''}`} onClick={() => setUserStatus(selectedUser.user_id, 'suspended')} type="button">Suspended</button>
                </div>
              </div>
              <div className="settings-line">
                <div><strong>Role</strong><span>{selectedUser.control?.role ?? 'user'}</span></div>
                <div className="theme-btn-group">
                  <button className={`theme-btn-opt${selectedUser.control?.role !== 'admin' ? ' active' : ''}`} onClick={() => setUserRole(selectedUser.user_id, 'user')} type="button">User</button>
                  <button className={`theme-btn-opt${selectedUser.control?.role === 'admin' ? ' active' : ''}`} onClick={() => setUserRole(selectedUser.user_id, 'admin')} type="button">Admin</button>
                </div>
              </div>
              {selectedUser.control?.role !== 'admin' && (
                <label className="settings-field">Monthly budget override
                  <input
                    className="conv-search-input"
                    defaultValue={selectedUser.control?.monthly_budget_usd ?? ''}
                    placeholder="Default $5.00"
                    onBlur={e => setMonthlyBudget(selectedUser.user_id, e.target.value)}
                  />
                  <span className="muted-text" style={{ fontSize: 12 }}>
                    Spent this month: {fmt$(selectedUser.month_spend ?? 0, 4)}
                  </span>
                </label>
              )}
              <div className="admin-danger-zone">
                <strong>Privacy actions</strong>
                <button className="toggle-chip" onClick={() => privacyDelete(selectedUser.user_id, { memories: true })} type="button">Delete memories</button>
                <button className="toggle-chip" onClick={() => privacyDelete(selectedUser.user_id, { writing_samples: true, twin_profile: true })} type="button">Delete voice profile</button>
                <button className="toggle-chip" onClick={() => privacyDelete(selectedUser.user_id, { research_runs: true })} type="button">Delete research</button>
                <button className="toggle-chip danger" onClick={() => privacyDelete(selectedUser.user_id, { conversations: true, memories: true, user_profile: true, writing_samples: true, twin_profile: true, research_runs: true })} type="button">Delete all user data</button>
              </div>
              </>
              )}
            </div>
          </div>
        )}

        {tab === 'usage' && usage && (
          <div className="admin-grid">
            <div className="card admin-table-card">
              <div className="chart-card-title">Top users</div>
              <div className="admin-table-wrap"><table className="admin-table"><thead><tr><th>User</th><th>Cost</th><th>Requests</th></tr></thead><tbody>
                {usage.top_users?.map((u: any) => <tr key={u.user_id}><td>{userLabel(u.user_id, u)}</td><td>{fmt$(u.cost, 4)}</td><td>{u.requests}</td></tr>)}
              </tbody></table></div>
            </div>
            <div className="card admin-table-card">
              <div className="chart-card-title">Models</div>
              <div className="admin-table-wrap"><table className="admin-table"><thead><tr><th>Model</th><th>Requests</th><th>Cost</th><th>Avg latency</th></tr></thead><tbody>
                {usage.model_usage?.map((m: any) => <tr key={m.model}><td className="mono">{m.model}</td><td>{m.requests}</td><td>{fmt$(m.cost, 4)}</td><td>{m.avg_latency_ms} ms</td></tr>)}
              </tbody></table></div>
            </div>
          </div>
        )}

        {tab === 'providers' && providers && (
          <div className="settings-card-list">
            <div className="settings-card">
              <strong>Provider keys</strong>
              <div className="provider-grid">
                {providers.providers?.map((p: any) => {
                  const result = providerTestResults[p.name]
                  const errCount = providers.recent_error_counts?.[p.name] ?? 0
                  return (
                    <div key={p.key} className="provider-row">
                      <span>{p.name}</span>
                      <strong className={p.configured ? 'ok-text' : 'warn-text'}>
                        {p.configured ? 'configured' : 'missing'}
                      </strong>
                      <code>{p.key_hint || p.key}</code>
                      <span className={errCount > 0 ? 'warn-text' : 'muted-text'}>
                        {errCount > 0 ? `${errCount} error${errCount === 1 ? '' : 's'} (7d)` : 'no errors (7d)'}
                      </span>
                      {p.circuit?.open ? (
                        <span className="warn-text">
                          Circuit open · retry in {p.circuit.cooldown_remaining_s}s (skipped in fallback chain)
                        </span>
                      ) : p.circuit?.consecutive_failures > 0 ? (
                        <span className="muted-text">{p.circuit.consecutive_failures} recent failure(s)</span>
                      ) : (
                        p.circuit && <span className="ok-text">Healthy</span>
                      )}
                      <button
                        className="nav-chat-cta settings-secondary"
                        type="button"
                        disabled={!p.configured || result?.loading}
                        onClick={() => testProvider(p.name)}
                      >
                        {result?.loading ? 'Testing…' : 'Test connection'}
                      </button>
                      {result && !result.loading && (
                        result.success ? (
                          <span className="ok-text">OK · {result.latency_ms} ms{result.model ? ` · ${result.model}` : ''}</span>
                        ) : (
                          <span className="warn-text">Failed: {result.error}</span>
                        )
                      )}
                    </div>
                  )
                })}
              </div>
            </div>
          </div>
        )}

        {tab === 'routing' && (
          <div className="admin-grid">
            <div className="card">
              <div className="chart-card-title">Route test</div>
              <textarea className="conv-search-input settings-textarea" value={routeMessage} onChange={e => setRouteMessage(e.target.value)} />
              <button className="nav-chat-cta settings-primary" onClick={testRoute} type="button">Test route</button>
              {routeResult && <ExecLogView data={{ execLog: null, route: routeResult, model_used: routeResult.primary_model, latency_ms: 0 }} />}
            </div>
            <div className="card">
              <div className="chart-card-title">Routing policy</div>
              {policy ? <RoutingPolicyMatrix policy={policy} /> : <span className="muted-text">Loading…</span>}
              {policy && (
                <details className="admin-code-card" style={{ marginTop: 14 }}>
                  <summary className="muted-text" style={{ cursor: 'pointer' }}>View raw YAML (JSON)</summary>
                  <pre>{JSON.stringify(policy, null, 2)}</pre>
                </details>
              )}
            </div>
          </div>
        )}

        {tab === 'research' && (
          <div className="card admin-table-card">
            <div className="chart-card-title">Research runs</div>
            <div className="admin-table-wrap"><table className="admin-table"><thead><tr><th>ID</th><th>User</th><th>Status</th><th>Mode</th><th>Evidence</th><th>Query</th></tr></thead><tbody>
              {research.map(r => <tr key={r.id}><td>{r.id}</td><td>{userLabel(r.user_id, r)}</td><td>{r.status}</td><td>{r.mode}</td><td>{r.source_count} src · {r.claim_count} claims</td><td>{r.query}</td></tr>)}
            </tbody></table></div>
          </div>
        )}

        {tab === 'errors' && (
          <div className="card admin-table-card">
            <div className="chart-card-title">Request errors</div>
            <div className="admin-table-wrap"><table className="admin-table"><thead><tr><th>Time</th><th>User</th><th>Model</th><th>Error</th></tr></thead><tbody>
              {errors.map(e => <tr key={e.id}><td>{e.created_at ? fmtTime(e.created_at) : '—'}</td><td>{userLabel(e.user_id, e)}</td><td className="mono">{e.selected_model}</td><td>{e.error}</td></tr>)}
            </tbody></table></div>
          </div>
        )}

        {tab === 'audit' && (
          <div className="card admin-table-card">
            <div className="chart-card-title">Admin audit</div>
            <div className="admin-table-wrap"><table className="admin-table"><thead><tr><th>Time</th><th>Admin</th><th>Action</th><th>Target</th></tr></thead><tbody>
              {audit.map(a => (
                <tr key={a.id}>
                  <td>{a.created_at ? fmtTime(a.created_at) : '—'}</td>
                  <td>{userLabel(a.admin_user_id, { name: a.admin_name, email: a.admin_email })}</td>
                  <td>{a.action}</td>
                  <td>{a.target_user_id ? userLabel(a.target_user_id, { name: a.target_name, email: a.target_email }) : '—'}</td>
                </tr>
              ))}
            </tbody></table></div>
          </div>
        )}

        {tab === 'system' && system && (
          <div className="settings-card-list">
            <div className="settings-card">
              <strong>System</strong>
              <div className="settings-grid">
                {Object.entries(system).map(([k, v]) => <span key={k}>{k.replace(/_/g, ' ')} <strong>{Array.isArray(v) ? v.join(', ') : String(v)}</strong></span>)}
              </div>
            </div>
          </div>
        )}
      </div>
    </>
  )
}

// ── Main component ────────────────────────────────────────────────────────────

export default function Home() {
  const { getToken, isLoaded: authLoaded, isSignedIn } = useAuth()
  const { user } = useUser()
  const { signOut } = useClerk()

  async function apiFetch(path: string, options: RequestInit = {}): Promise<Response> {
    const token = await getToken()
    return fetch(`${API_BASE}${path}`, {
      ...options,
      headers: {
        'Content-Type': 'application/json',
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...(options.headers as object ?? {}),
      },
    })
  }

  // Layout state
  const [rightPanelOpen, setRightPanelOpen]   = useState(false)
  const [rightPanelWidth, setRightPanelWidth] = useState(300)
  const [execPanelData, setExecPanelData]     = useState<ExecPanelData>(null)
  const [mobileNavOpen, setMobileNavOpen]     = useState(false)
  const [devMode, setDevMode]                 = useState(false)
  const [settingsViewOpen, setSettingsViewOpen] = useState(false)
  const [settingsInitialTab, setSettingsInitialTab] = useState<SettingsTab | undefined>(undefined)
  const [isAdmin, setIsAdmin]                 = useState(false)
  const [accountStatus, setAccountStatus]     = useState<string | null>(null)
  const [accountStatusLoaded, setAccountStatusLoaded] = useState(false)
  const [theme, setThemeState]                = useState<'dark' | 'light'>('dark')
  const [accentTheme, setAccentThemeState]    = useState<AccentTheme>('default')
  const rightPanelWidthRef = useRef(300)

  // Conversation state
  const [conversations, setConversations] = useState<ConversationSummary[]>([])
  const [activeConvId, setActiveConvId]   = useState<number | null>(null)
  const [messages, setMessages]           = useState<MessageOut[]>([])

  // Input state
  const [message, setMessage]             = useState('')
  const [quality, setQuality]             = useState<Quality>('smart')
  const [researchOn, setResearchOn]       = useState(false)
  const [webSearchOn, setWebSearchOn]     = useState(false)
  const [documentIntentOn, setDocumentIntentOn] = useState(false)
  const [previewDoc, setPreviewDoc]       = useState<GeneratedDocument | null>(null)
  const [documentBriefDraft, setDocumentBriefDraft] = useState<DocumentBrief | null>(null)
  const [documentBriefDetected, setDocumentBriefDetected] = useState(false)
  const [documentPlanCapabilities, setDocumentPlanCapabilities] = useState<DocumentPlanCapabilities>({
    deepResearch: false,
    webSearch: false,
  })
  const [documentPlanRecommendations, setDocumentPlanRecommendations] = useState<DocumentPlanRecommendations>({})
  const [forceModel, setForceModel]       = useState('')
  const [showWebSearch, setShowWebSearch] = useState(false)
  const [leftMenuOpen, setLeftMenuOpen]   = useState(false)
  const [optionsOpen,  setOptionsOpen]    = useState(false)
  const [outputMode, setOutputMode]       = useState<OutputMode>('default')
  const [persona, setPersona]                 = useState<PersonaId>('enterprise_architect')
  const [visibleArtifacts, setVisibleArtifacts] = useState<ArtifactType[]>(PERSONAS[0].artifacts)
  const [artifactType, setArtifactType]       = useState<ArtifactType | null>(null)
  const [artifactPickerOpen, setArtifactPickerOpen] = useState(false)
  const [dismissedSuggestions, setDismissedSuggestions] = useState<Set<number>>(new Set())
  const [modelOptions, setModelOptions]   = useState(FALLBACK_MODEL_OPTIONS)

  // File attachment
  const [pendingFiles, setPendingFiles] = useState<PendingFile[]>([])
  const [isExtracting, setIsExtracting] = useState(false)
  const [attachError, setAttachError]   = useState('')
  const [dragOver, setDragOver]         = useState(false)

  // Status
  const [loading, setLoading]     = useState(false)
  const [streaming, setStreaming] = useState(false)
  const [artifactGenerating, setArtifactGenerating] = useState(false)
  const [refining, setRefining]   = useState(false)
  const [error, setError]         = useState('')
  const [copied, setCopied]       = useState<number | null>(null)
  const [liveSteps, setLiveSteps] = useState<PipelineStep[]>([])
  const [subCompletions, setSubCompletions] = useState<Map<number, PipelineStep>>(new Map())
  const [pipelineTs, setPipelineTs] = useState<number>(0)
  const [liveAssistantId, setLiveAssistantId] = useState<number | null>(null)
  const [, setTick] = useState(0)

  // Memory
  const [memories, setMemories]             = useState<MemoryItem[]>([])
  const [memoriesLoaded, setMemoriesLoaded] = useState(false)
  const [memoriesOpen, setMemoriesOpen]     = useState(false)
  const [personalProfile, setPersonalProfile] = useState<PersonalContextProfile | null>(null)
  const [personalProfileLoaded, setPersonalProfileLoaded] = useState(false)
  const [hasProfile, setHasProfile]         = useState(false)

  // Twin profile
  const [twinProfile, setTwinProfile]             = useState<TwinProfile | null>(null)
  const [twinSamples, setTwinSamples]             = useState<WritingSample[]>([])
  const [twinPanelOpen, setTwinPanelOpen]         = useState(false)
  const [twinSamplesLoaded, setTwinSamplesLoaded] = useState(false)
  const [newSampleText, setNewSampleText]         = useState('')
  const [newSampleLabel, setNewSampleLabel]       = useState('')
  const [sampleSubmitting, setSampleSubmitting]   = useState(false)

  // Onboarding
  const [showOnboarding, setShowOnboarding] = useState(false)
  const [userName, setUserName]             = useState('')
  const [userDomain, setUserDomain]         = useState('')

  const threadRef      = useRef<HTMLDivElement>(null)
  const taRef          = useRef<HTMLTextAreaElement>(null)
  const fileInputRef   = useRef<HTMLInputElement>(null)
  const readerRef      = useRef<ReadableStreamDefaultReader<Uint8Array> | null>(null)
  const renderedIdsRef = useRef<Set<number>>(new Set())
  const lastSentRef    = useRef('')

  const activeConv = conversations.find(c => c.id === activeConvId)
  const isBusy = loading || streaming || refining || liveAssistantId !== null || isExtracting
  const workbench = WORKBENCH_PERSONAS[persona] ?? WORKBENCH_PERSONAS.enterprise_architect
  const canSeeAdmin = isAdmin

  // ── Init: data fetches ────────────────────────────────────────────────────

  useEffect(() => {
    const initialParams = new URLSearchParams(window.location.search)
    const initialView = initialParams.get('view')
    const initialSettings = initialParams.get('settings')
    const initialConvId = initialParams.get('c')
    const shouldAutoLoadConversation = initialView !== 'dashboard' && initialSettings !== '1' && !!initialConvId

    apiFetch('/conversations')
      .then(r => r.ok ? r.json() : [])
      .then((list: ConversationSummary[]) => {
        setConversations(list)
        if (shouldAutoLoadConversation) {
          const id = parseInt(initialConvId as string, 10)
          if (!Number.isNaN(id) && list.some(c => c.id === id)) loadConversation(id)
        }
      }).catch(() => {})

    apiFetch('/models/policy')
      .then(r => r.ok ? r.json() : null)
      .then((p: RoutingPolicy | null) => {
        if (!p) return
        const m = extractModelOptions(p)
        if (m.length > 0) setModelOptions(m)
      }).catch(() => {})

    apiFetch('/twin-profile')
      .then(r => r.ok ? r.json() : null)
      .then((p: TwinProfile | null) => {
        if (!p) return
        setTwinProfile(p)
        setHasProfile(!!p.fingerprint)
      })
      .catch(() => {})

    try {
      const rw = localStorage.getItem('md-rp-w')
      if (rw) { const w = parseInt(rw, 10); setRightPanelWidth(w); rightPanelWidthRef.current = w }

      const onboarded   = localStorage.getItem('md-onboarded')
      const savedName   = localStorage.getItem('md-user-name')
      const savedDomain = localStorage.getItem('md-user-domain')
      if (!onboarded) setShowOnboarding(true)
      if (savedName)   setUserName(savedName)
      if (savedDomain) setUserDomain(savedDomain)

      const savedQ = localStorage.getItem('md-quality') as Quality | null
      if (savedQ && ['quick','smart','thorough'].includes(savedQ)) setQuality(savedQ)
      const sp = localStorage.getItem('md-persona') as PersonaId | null
      const validPersonas: PersonaId[] = ['enterprise_architect','product_manager','software_engineer','data_scientist','custom']
      if (sp && validPersonas.includes(sp)) setPersona(sp)
      const sa = localStorage.getItem('md-visible-artifacts')
      if (sa) { try { setVisibleArtifacts(JSON.parse(sa)) } catch {} }
      const sw = localStorage.getItem('md-show-web-search')
      if (sw) setShowWebSearch(sw === '1')
      const st = localStorage.getItem('md-theme') as 'dark' | 'light' | null
      if (st === 'dark' || st === 'light') setThemeState(st)
      const sac = localStorage.getItem('md-accent') as AccentTheme | null
      if (sac && ['default','classic','electric','arctic','warm'].includes(sac)) {
        setAccentThemeState(sac)
      }
      if (initialView === 'dashboard') {
        setSettingsViewOpen(true)
        setSettingsInitialTab('dashboard')
        window.history.replaceState({}, '', window.location.pathname)
      }
      if (initialView === 'admin') {
        setSettingsViewOpen(true)
        setSettingsInitialTab('admin')
        window.history.replaceState({}, '', window.location.pathname)
      }
      if (initialSettings === '1') {
        openSettingsView()
        window.history.replaceState({}, '', window.location.pathname)
      }
    } catch { /* ignore */ }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    if (!authLoaded) return
    if (!isSignedIn) {
      setAccountStatusLoaded(true)
      return
    }
    let cancelled = false
    apiFetch('/me')
      .then(r => r.ok ? r.json() : null)
      .then(d => { if (!cancelled && d?.account_status) setAccountStatus(d.account_status) })
      .catch(() => {})
      .finally(() => { if (!cancelled) setAccountStatusLoaded(true) })
    return () => { cancelled = true }
  // apiFetch is recreated on render; auth state is the intended trigger.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [authLoaded, isSignedIn, user?.id])

  useEffect(() => {
    if (!authLoaded) return
    if (!isSignedIn) {
      setIsAdmin(false)
      return
    }
    let cancelled = false
    apiFetch('/admin/me')
      .then(r => {
        if (cancelled) return
        setIsAdmin(r.ok)
        if (!r.ok && process.env.NODE_ENV !== 'production') {
          // Admin tab is intentionally hidden on any non-OK response (403 for
          // non-admins, but also auth/network/config failures). Log the status
          // in dev so a misconfiguration doesn't look identical to "not an admin".
          // eslint-disable-next-line no-console
          console.warn(`[admin] /admin/me returned ${r.status} — Admin tab hidden`)
        }
      })
      .catch((err) => {
        if (cancelled) return
        setIsAdmin(false)
        if (process.env.NODE_ENV !== 'production') {
          // eslint-disable-next-line no-console
          console.warn('[admin] /admin/me request failed — Admin tab hidden', err)
        }
      })
    return () => { cancelled = true }
  // apiFetch is recreated on render; auth state is the intended trigger.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [authLoaded, isSignedIn, user?.id])

  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme)
    try { localStorage.setItem('md-theme', theme) } catch {}
  }, [theme])

  useEffect(() => {
    if (accentTheme === 'default') {
      document.documentElement.removeAttribute('data-accent')
    } else {
      document.documentElement.setAttribute('data-accent', accentTheme)
    }
    try { localStorage.setItem('md-accent', accentTheme) } catch {}
  }, [accentTheme])

  function setTheme(next: 'dark' | 'light') {
    setThemeState(next)
  }

  function setAccentTheme(next: AccentTheme) {
    setAccentThemeState(next)
  }

  // ── Auto-scroll ───────────────────────────────────────────────────────────

  const [showScrollBtn, setShowScrollBtn] = useState(false)
  const isNearBottom = useRef(true)

  function onThreadScroll() {
    const el = threadRef.current
    if (!el) return
    const distFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight
    isNearBottom.current = distFromBottom < 80
    setShowScrollBtn(distFromBottom > 200)
  }

  function scrollToBottom() {
    const el = threadRef.current
    if (el) el.scrollTo({ top: el.scrollHeight, behavior: 'smooth' })
  }

  useEffect(() => {
    if (isNearBottom.current) scrollToBottom()
  }, [messages, loading, liveSteps])

  useEffect(() => {
    if (liveSteps.length > 0 && !streaming) {
      const id = setInterval(() => setTick(t => t + 1), 500)
      return () => clearInterval(id)
    }
  }, [liveSteps.length, streaming])

  useEffect(() => {
    messages.forEach(m => renderedIdsRef.current.add(m.id))
  }, [messages])

  useEffect(() => {
    const container = threadRef.current
    if (!container) return
    container.querySelectorAll<HTMLElement>('pre:not([data-copy-attached])').forEach(pre => {
      pre.setAttribute('data-copy-attached', '1')
      pre.style.position = 'relative'
      const btn = document.createElement('button')
      btn.className = 'code-copy-btn'
      btn.setAttribute('aria-label', 'Copy code')
      btn.innerHTML = '<i class="ti ti-copy"></i>'
      btn.addEventListener('click', () => {
        const code = pre.querySelector<HTMLElement>('code')?.innerText ?? ''
        navigator.clipboard.writeText(code).then(() => {
          btn.innerHTML = '<i class="ti ti-check"></i>'
          setTimeout(() => { btn.innerHTML = '<i class="ti ti-copy"></i>' }, 2000)
        })
      })
      pre.appendChild(btn)
    })
  }, [messages])

  // ── Keyboard shortcuts ────────────────────────────────────────────────────

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      const meta = e.metaKey || e.ctrlKey
      if (meta && e.key === 'k') { e.preventDefault(); newConversation(); taRef.current?.focus() }
      if (meta && e.key === 'e') { e.preventDefault(); setRightPanelOpen(v => !v) }
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [])

  // ── Close exec panel when dev mode is disabled ────────────────────────────

  useEffect(() => {
    if (!devMode) setRightPanelOpen(false)
  }, [devMode])

  // ── Fetch memories when panel opens ──────────────────────────────────────

  useEffect(() => {
    if (!memoriesOpen || memoriesLoaded) return
    apiFetch('/memory?include_superseded=true')
      .then(r => r.ok ? r.json() : [])
      .then(m => { setMemories(m); setMemoriesLoaded(true) })
      .catch(() => { setMemoriesLoaded(true) })
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [memoriesOpen])

  useEffect(() => {
    if (!memoriesOpen || personalProfileLoaded) return
    apiFetch('/personal-context/profile')
      .then(r => r.ok ? r.json() : null)
      .then((p: PersonalContextProfile | null) => {
        setPersonalProfile(p)
        setPersonalProfileLoaded(true)
      })
      .catch(() => { setPersonalProfileLoaded(true) })
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [memoriesOpen])

  useEffect(() => {
    if (twinPanelOpen && !twinSamplesLoaded) loadTwinProfile()
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [twinPanelOpen])

  // ── Persist toolbar settings ──────────────────────────────────────────────

  useEffect(() => {
    try { localStorage.setItem('md-quality', quality) } catch {}
  }, [quality])

  useEffect(() => {
    try { localStorage.setItem('md-persona', persona) } catch {}
  }, [persona])

  useEffect(() => {
    try { localStorage.setItem('md-visible-artifacts', JSON.stringify(visibleArtifacts)) } catch {}
  }, [visibleArtifacts])

  function selectPersona(id: PersonaId) {
    setPersona(id)
    if (id !== 'custom') {
      const p = PERSONAS.find(x => x.id === id)
      if (p) setVisibleArtifacts(p.artifacts)
    }
  }

  function toggleArtifactVisibility(type: ArtifactType) {
    setPersona('custom')
    setVisibleArtifacts(prev =>
      prev.includes(type) ? prev.filter(t => t !== type) : [...prev, type]
    )
  }

  function openSettingsView() {
    setSettingsViewOpen(true)
    setMobileNavOpen(false)
    setMemoriesOpen(true)
    setTwinPanelOpen(true)
  }

  useEffect(() => {
    try { localStorage.setItem('md-show-web-search', showWebSearch ? '1' : '0') } catch {}
    if (!showWebSearch) setWebSearchOn(false)
  }, [showWebSearch])

  // close left menu and options popover on outside click
  useEffect(() => {
    if (!leftMenuOpen && !optionsOpen && !artifactPickerOpen) return
    const handler = (e: globalThis.MouseEvent) => {
      const t = e.target as Element
      if (!t.closest('.left-menu-popup') && !t.closest('[aria-label="Attach and more"]')) setLeftMenuOpen(false)
      if (!t.closest('.options-popover') && !t.closest('[aria-label="More options"]')) setOptionsOpen(false)
      if (!t.closest('.artifact-picker') && !t.closest('[aria-label="Format as artifact"]')) setArtifactPickerOpen(false)
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [leftMenuOpen, optionsOpen, artifactPickerOpen])

  // ── Data actions ──────────────────────────────────────────────────────────

  function setConvUrlParam(id: number | null) {
    try {
      const url = new URL(window.location.href)
      if (id != null) url.searchParams.set('c', String(id))
      else url.searchParams.delete('c')
      const qs = url.searchParams.toString()
      window.history.replaceState({}, '', url.pathname + (qs ? `?${qs}` : ''))
    } catch {}
  }

  async function loadConversation(id: number) {
    setMobileNavOpen(false)
    setSettingsViewOpen(false)
    setActiveConvId(id)
    setConvUrlParam(id)
    setMessages([])
    setExecPanelData(null)
    setPendingFiles([])
    setAttachError('')
    setArtifactType(null)
    setLiveSteps([])
    setSubCompletions(new Map())
    setLiveAssistantId(null)
    try {
      const detail: ConversationDetail = await apiFetch(`/conversations/${id}`).then(r => r.json())
      setMessages(detail.messages)
    } catch { setError('Failed to load conversation') }
  }

  function newConversation() {
    setMobileNavOpen(false)
    setSettingsViewOpen(false)
    setActiveConvId(null)
    setConvUrlParam(null)
    setMessages([])
    setExecPanelData(null)
    setMessage('')
    setError('')
    setPendingFiles([])
    setAttachError('')
    setArtifactType(null)
    setLiveSteps([])
    setSubCompletions(new Map())
    setLiveAssistantId(null)
  }

  const [deleteConfirmId, setDeleteConfirmId] = useState<number | null>(null)
  const [editingTitleId, setEditingTitleId]   = useState<number | null>(null)
  const [editingTitle, setEditingTitle]       = useState('')
  const [editingMsgId, setEditingMsgId]       = useState<number | null>(null)
  const [editText, setEditText]               = useState('')

  async function deleteConversation(e: MouseEvent, id: number) {
    e.stopPropagation()
    if (deleteConfirmId === id) {
      await apiFetch(`/conversations/${id}`, { method: 'DELETE' })
      setConversations(prev => prev.filter(c => c.id !== id))
      if (activeConvId === id) newConversation()
      setDeleteConfirmId(null)
    } else {
      setDeleteConfirmId(id)
      setTimeout(() => setDeleteConfirmId(null), 3000)
    }
  }

  async function renameConversation(id: number, title: string) {
    const trimmed = title.trim()
    if (!trimmed) { setEditingTitleId(null); return }
    await apiFetch(`/conversations/${id}`, {
      method: 'PATCH',
      body: JSON.stringify({ title: trimmed }),
    })
    setConversations(prev => prev.map(c => c.id === id ? { ...c, title: trimmed } : c))
    setEditingTitleId(null)
  }

  function saveUserName(v: string) {
    const trimmed = v.trim()
    setUserName(trimmed)
    try { localStorage.setItem('md-user-name', trimmed) } catch {}
  }

  function saveUserDomain(v: string) {
    const trimmed = v.trim()
    setUserDomain(trimmed)
    try { localStorage.setItem('md-user-domain', trimmed) } catch {}
  }

  function completeOnboarding(name: string, domain: string) {
    try {
      localStorage.setItem('md-onboarded', '1')
      if (name.trim())   localStorage.setItem('md-user-name',   name.trim())
      if (domain.trim()) localStorage.setItem('md-user-domain', domain.trim())
    } catch {}
    if (name.trim())   setUserName(name.trim())
    if (domain.trim()) setUserDomain(domain.trim())
    setShowOnboarding(false)
  }

  function addFiles(files: FileList | null) {
    if (!files) return
    setAttachError('')
    const incoming: PendingFile[] = Array.from(files).map(file => ({
      id:   `${Date.now()}-${Math.random().toString(36).slice(2)}`,
      file,
      name: file.name,
      size: file.size,
    }))
    setPendingFiles(prev => [...prev, ...incoming])
  }

  function removeFile(id: string) {
    setPendingFiles(prev => prev.filter(f => f.id !== id))
    setAttachError('')
  }

  async function deleteMemory(id: number) {
    await apiFetch(`/memory/${id}`, { method: 'DELETE' })
    setMemories(prev => prev.filter(m => m.id !== id))
  }

  async function updateMemory(id: number, patch: MemoryPatch) {
    const res = await apiFetch(`/memory/${id}`, {
      method: 'PATCH',
      body: JSON.stringify(patch),
    })
    if (!res.ok) return
    const updated: MemoryItem = await res.json()
    setMemories(prev => prev.map(m => m.id === id ? updated : m))
  }

  async function clearAllMemories() {
    await apiFetch('/memory?confirm=true', { method: 'DELETE' })
    setMemories([])
  }

  async function saveProfileOverrides(overrides: Record<string, unknown>) {
    const res = await apiFetch('/personal-context/profile', {
      method: 'PATCH',
      body: JSON.stringify({ overrides }),
    })
    if (!res.ok) return
    const updated: PersonalContextProfile = await res.json()
    setPersonalProfile(updated)
    setPersonalProfileLoaded(true)
  }

  async function loadTwinProfile() {
    const [profileRes, samplesRes] = await Promise.all([
      apiFetch('/twin-profile'),
      apiFetch('/twin-profile/samples'),
    ])
    if (profileRes.ok) {
      const p: TwinProfile = await profileRes.json()
      setTwinProfile(p)
      setHasProfile(!!p.fingerprint)
    }
    if (samplesRes.ok) setTwinSamples(await samplesRes.json())
    setTwinSamplesLoaded(true)
  }

  async function addSample() {
    if (!newSampleText.trim() || sampleSubmitting) return
    setSampleSubmitting(true)
    try {
      const res = await apiFetch('/twin-profile/samples', {
        method: 'POST',
        body: JSON.stringify({ content: newSampleText.trim(), label: newSampleLabel.trim() || null }),
      })
      if (res.ok) {
        const sample: WritingSample = await res.json()
        setTwinSamples(prev => [sample, ...prev])
        setNewSampleText('')
        setNewSampleLabel('')
        setTimeout(async () => {
          const r = await apiFetch('/twin-profile')
          if (r.ok) {
            const p: TwinProfile = await r.json()
            setTwinProfile(p)
            setHasProfile(!!p.fingerprint)
          }
        }, 3500)
      }
    } finally {
      setSampleSubmitting(false)
    }
  }

  async function deleteSample(id: number) {
    await apiFetch(`/twin-profile/samples/${id}`, { method: 'DELETE' })
    setTwinSamples(prev => {
      const next = prev.filter(s => s.id !== id)
      if (next.length === 0) {
        setTwinProfile(p => p ? { ...p, fingerprint: null, rewrite_prompt: null, sample_count: 0 } : p)
        setHasProfile(false)
      }
      return next
    })
    setTimeout(async () => {
      const r = await apiFetch('/twin-profile')
      if (r.ok) {
        const p: TwinProfile = await r.json()
        setTwinProfile(p)
        setHasProfile(!!p.fingerprint)
      }
    }, 1200)
  }

  async function reExtract() {
    await apiFetch('/twin-profile/extract', { method: 'POST' })
    setTimeout(async () => {
      const r = await apiFetch('/twin-profile')
      if (r.ok) {
        const p: TwinProfile = await r.json()
        setTwinProfile(p)
        setHasProfile(!!p.fingerprint)
      }
    }, 4000)
  }

  async function downloadDocx(title: string, content: string, subtitle?: string) {
    const res = await apiFetch('/documents/generate/docx', {
      method: 'POST',
      body: JSON.stringify({ title, content, subtitle }),
    })
    if (!res.ok) {
      setError('Failed to generate document')
      return
    }
    const blob = await res.blob()
    const href = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = href
    a.download = safeDownloadName(title, 'docx')
    a.click()
    URL.revokeObjectURL(href)
  }

  async function generateDocumentFromPrompt(
    prompt: string,
    attachedDocs: AttachedDocument[],
    brief?: DocumentBrief,
    opts: {
      deepResearch?: boolean
      allowResearchRecommendation?: boolean
      webSearch?: boolean
      allowWebSearchRecommendation?: boolean
    } = {},
  ): Promise<GeneratedDocument> {
    const title = brief?.title?.trim() || ''
    const res = await apiFetch('/documents/generate/from-prompt/docx', {
      method: 'POST',
      body: JSON.stringify({
        prompt,
        title: title || undefined,
        doc_type: brief?.docType,
        audience: brief?.audience,
        tone: brief?.tone,
        length: brief?.length,
        output_formats: brief?.outputFormats ?? ['docx'],
        profile: buildRequestFields(quality, false, false).profile,
        force_model: forceModel.trim() || null,
        deep_research: opts.deepResearch ?? false,
        research_mode: opts.deepResearch ? 'deep' : 'quick',
        allow_research_recommendation: opts.allowResearchRecommendation ?? true,
        web_search: opts.webSearch ?? false,
        allow_web_search_recommendation: opts.allowWebSearchRecommendation ?? true,
        attached_documents: attachedDocs.map(d => ({
          name:       d.name,
          text:       d.text,
          char_count: d.char_count,
          pages:      d.pages_extracted,
          method:     d.method,
        })),
      }),
    })
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: 'Document generation failed' }))
      const detail = (err as { detail: unknown }).detail
      if (
        res.status === 409
        && typeof detail === 'object'
        && detail !== null
        && (detail as { code?: string }).code === 'document_plan_recommended'
      ) {
        const rawRecommendations = (detail as { recommendations?: Record<string, unknown> }).recommendations ?? {}
        const nextRecommendations: DocumentPlanRecommendations = {}
        const rawResearch = rawRecommendations.deep_research
        if (rawResearch && typeof rawResearch === 'object') {
          const r = rawResearch as {
            reason?: string
            risk_factors?: string[]
            confidence?: string
            suggested_mode?: ResearchMode
          }
          nextRecommendations.deepResearch = {
            reason: r.reason || 'This document would likely be stronger with deep research.',
            risk_factors: Array.isArray(r.risk_factors) ? r.risk_factors : [],
            confidence: r.confidence || 'high',
            suggested_mode: r.suggested_mode || 'deep',
          }
        }
        const rawWeb = rawRecommendations.web_search
        if (rawWeb && typeof rawWeb === 'object') {
          const w = rawWeb as {
            reason?: string
            search_query?: string
            confidence?: string
          }
          nextRecommendations.webSearch = {
            reason: w.reason || 'This document may need current or external source context.',
            search_query: w.search_query || prompt,
            confidence: w.confidence || 'medium',
          }
        }
        throw new DocumentPlanRecommendationError(nextRecommendations)
      }
      throw new Error(typeof detail === 'string' ? detail : 'Document generation failed')
    }
    const data = await res.json() as {
      title: string; doc_type: string; markdown: string
      filename: string; docx_base64: string
    }
    return {
      title:      data.title,
      docType:    data.doc_type,
      markdown:   data.markdown,
      filename:   data.filename,
      docxBase64: data.docx_base64,
      outputFormats: brief?.outputFormats ?? ['docx'],
    }
  }

  function doExport(e: MouseEvent, conv: ConversationSummary) {
    e.stopPropagation()
    if (activeConvId === conv.id) {
      downloadDocx(conv.title, conversationMarkdown(conv.title, messages), 'Generated by Fronei')
    } else {
      apiFetch(`/conversations/${conv.id}`)
        .then(r => r.json())
        .then((d: ConversationDetail) => downloadDocx(d.title, conversationMarkdown(d.title, d.messages), 'Generated by Fronei'))
        .catch(() => {})
    }
  }

  async function saveEdit() {
    if (!editingMsgId || !editText.trim()) return
    const text = editText.trim()
    const original = messages.find(m => m.id === editingMsgId)?.content ?? ''
    if (activeConvId) {
      await apiFetch(
        `/conversations/${activeConvId}/messages/from/${editingMsgId}`,
        { method: 'DELETE' }
      ).catch(() => {})
    }
    setMessages(prev => {
      const idx = prev.findIndex(m => m.id === editingMsgId)
      return idx >= 0 ? prev.slice(0, idx) : prev
    })
    setEditingMsgId(null)
    setEditText('')
    if (text.length > 80 && original && text !== original) {
      apiFetch('/twin-profile/samples', {
        method: 'POST',
        body: JSON.stringify({
          content: text,
          label: 'edited response',
        }),
      }).catch(() => {})
    }
    submit(text)
  }

  function copyMessage(content: string, id: number) {
    navigator.clipboard.writeText(content).then(() => {
      setCopied(id)
      setTimeout(() => setCopied(null), 2000)
    })
  }

  function openExecPanel(m: MessageOut) {
    if (m.role !== 'assistant' || !m.model_used) return
    setExecPanelData({
      execLog:            m.execution_log ?? null,
      route:              m.route ?? null,
      model_used:         m.model_used,
      latency_ms:         m.latency_ms ?? 0,
      estimated_cost_usd: m.estimated_cost_usd,
      prompt_tokens:      m.prompt_tokens,
      completion_tokens:  m.completion_tokens,
      task_type:          m.task_type,
      complexity:         m.complexity,
    })
    setRightPanelOpen(true)
  }

  function onRightPanelResizeMouseDown(e: React.MouseEvent) {
    e.preventDefault()
    const startX = e.clientX
    const startW = rightPanelWidth
    function onMove(ev: globalThis.MouseEvent) {
      const newW = Math.max(240, Math.min(520, startW - (ev.clientX - startX)))
      setRightPanelWidth(newW)
      rightPanelWidthRef.current = newW
    }
    function onUp() {
      document.removeEventListener('mousemove', onMove)
      document.removeEventListener('mouseup', onUp)
      try { localStorage.setItem('md-rp-w', String(rightPanelWidthRef.current)) } catch {}
    }
    document.addEventListener('mousemove', onMove)
    document.addEventListener('mouseup', onUp)
  }

  function reformatAs(type: ArtifactType) {
    const artifact = ARTIFACT_TYPES.find(a => a.value === type)
    if (!artifact) return
    setArtifactType(type)
    setArtifactPickerOpen(false)
    submit(`Reformat your previous response as a ${artifact.hint.toLowerCase()}.`)
  }

  function abortStream() {
    readerRef.current?.cancel()
    readerRef.current = null
    setStreaming(false)
    setRefining(false)
    setLoading(false)
    setLiveSteps([])
    setSubCompletions(new Map())
    setLiveAssistantId(null)
  }

  // ── Submit ────────────────────────────────────────────────────────────────

  async function submit(
    overrideText?: string,
    opts: {
      forceResearch?: boolean
      suppressResearchRecommendation?: boolean
      suppressDocumentDetection?: boolean
      forceDocumentResearch?: boolean
      suppressDocumentResearchRecommendation?: boolean
      forceDocumentWebSearch?: boolean
      suppressDocumentWebSearchRecommendation?: boolean
      documentBrief?: DocumentBrief
    } = {},
  ) {
    const rawText = (overrideText !== undefined ? overrideText : message).trim()
    const sent = rawText || (documentIntentOn && pendingFiles.length > 0
      ? 'Generate a client-ready document from the attached files.'
      : '')
    if ((!sent && pendingFiles.length === 0) || isBusy) return

    if (!opts.documentBrief && !opts.suppressDocumentDetection) {
      const inferredBrief = defaultDocumentBrief(sent, documentIntentOn)
      if (inferredBrief) {
            setDocumentBriefDraft(inferredBrief)
            setDocumentBriefDetected(!documentIntentOn)
            setDocumentPlanCapabilities({ deepResearch: false, webSearch: false })
            setDocumentPlanRecommendations({})
            setDocumentIntentOn(true)
            setLeftMenuOpen(false)
            setOptionsOpen(false)
        return
      }
    }

    lastSentRef.current = sent
    setLoading(true)
    setStreaming(false)
    setRefining(false)
    setLiveSteps([])
    setSubCompletions(new Map())
    setPipelineTs(Date.now())
    setOptionsOpen(false)
    setLeftMenuOpen(false)
    setArtifactPickerOpen(false)
    setLiveAssistantId(null)
    setError('')
    setAttachError('')

    const tempUserId = -Date.now()
    const tempAsstId = -Date.now() - 1
    const wasNew = activeConvId === null

    setMessages(prev => [...prev, {
      id: tempUserId, role: 'user', content: sent, created_at: new Date().toISOString(),
      attached_files: pendingFiles.length > 0
        ? pendingFiles.map(pf => ({ name: pf.name, method: '', pages: null }))
        : null,
    }])
    if (overrideText === undefined) setMessage('')

    let startRoute: RouteDecision | null = null
    let startConvId = activeConvId
    let startTurnType: string | null = null
    let startAction: string | null = null

    const userCtx = [
      userName   && `User: ${userName}`,
      userDomain && `Domain: ${userDomain}`,
    ].filter(Boolean).join(' | ')
    const apiMessage = (wasNew && userCtx) ? `[Context: ${userCtx}]\n\n${sent}` : sent

    let extractedDocs: AttachedDocument[] = []
    let bubblePreCreated = false
    if (pendingFiles.length > 0) {
      // Pre-create the assistant bubble so the pipeline log shows immediately.
      setLiveAssistantId(tempAsstId)
      setMessages(prev => [
        ...prev,
        { id: tempAsstId, role: 'assistant' as const, content: '', created_at: new Date().toISOString() },
      ])
      setPipelineTs(Date.now())
      setLiveSteps([{
        stage:   'planning' as PipelineStage,
        message: pendingFiles.length === 1
          ? `Reading ${pendingFiles[0].name}…`
          : `Reading ${pendingFiles.length} files…`,
        ts: Date.now(),
      }])
      bubblePreCreated = true
      setIsExtracting(true)
      setLoading(false)

      try {
        const doExtractOne = async (pf: PendingFile): Promise<AttachedDocument> => {
          const tryOnce = async (): Promise<Response> => {
            const form  = new FormData()
            form.append('file', pf.file)
            const token = await getToken()
            return fetch(`${API_BASE}/documents/extract`, {
              method:  'POST',
              headers: token ? { Authorization: `Bearer ${token}` } : {},
              body:    form,
            })
          }
          let res = await tryOnce()
          if (res.status === 401) res = await tryOnce()
          if (!res.ok) {
            const err    = await res.json().catch(() => ({ detail: 'Upload failed' }))
            const detail = (err as { detail: string }).detail
            throw new Error(
              `${pf.name}: ${res.status === 401
                ? 'Session expired — please refresh the page'
                : detail}`
            )
          }
          const doc: AttachedDocument = await res.json()
          setLiveSteps(prev => [...prev, {
            stage:     'sub_complete' as PipelineStage,
            message:   pf.name,
            ts:        Date.now(),
            model:     doc.method,
            cost_usd:  null,
            latency_ms: undefined,
          }])
          return doc
        }

        extractedDocs = await Promise.all(pendingFiles.map(doExtractOne))

        setLiveSteps(prev => [...prev, {
          stage:   'routing' as PipelineStage,
          message: (documentIntentOn || opts.documentBrief) ? 'Files ready — generating document…' : 'Files ready — sending to Fronei…',
          ts:      Date.now(),
        }])

        setMessages(prev => prev.map(m => m.id === tempUserId ? {
          ...m,
          attached_files: extractedDocs.map(d => ({
            name:   d.name,
            method: d.method,
            pages:  d.pages_extracted > 1 ? d.pages_extracted : null,
          })),
        } : m))
      } catch (e) {
        setAttachError(e instanceof Error ? e.message : 'File extraction failed')
        setIsExtracting(false)
        setLoading(false)
        setLiveSteps([])
        setLiveAssistantId(null)
        setMessages(prev => prev.filter(m => m.id !== tempUserId && m.id !== tempAsstId))
        return
      }
      setIsExtracting(false)
      setLoading(true)
    }

    try {
      if (documentIntentOn || opts.documentBrief) {
        setResearchOn(false)
        if (!bubblePreCreated) {
          setMessages(prev => [
            ...prev,
            { id: tempAsstId, role: 'assistant' as const, content: '', created_at: new Date().toISOString() },
          ])
        }
        setLiveAssistantId(tempAsstId)
        setLiveSteps([{
          stage:   'routing' as PipelineStage,
          message: 'Generating document…',
          ts:      Date.now(),
        }])
        let generated: GeneratedDocument
        try {
          generated = await generateDocumentFromPrompt(apiMessage, extractedDocs, opts.documentBrief, {
            deepResearch: opts.forceDocumentResearch,
            allowResearchRecommendation: !opts.suppressDocumentResearchRecommendation,
            webSearch: opts.forceDocumentWebSearch,
            allowWebSearchRecommendation: !opts.suppressDocumentWebSearchRecommendation,
          })
        } catch (e) {
          if (e instanceof DocumentPlanRecommendationError && opts.documentBrief) {
            setDocumentBriefDraft(opts.documentBrief)
            setDocumentBriefDetected(false)
            setDocumentPlanRecommendations(e.recommendations)
            setDocumentPlanCapabilities({
              deepResearch: opts.forceDocumentResearch ?? false,
              webSearch: opts.forceDocumentWebSearch ?? false,
            })
            setDocumentIntentOn(true)
            setMessage(sent)
            setMessages(prev => prev.filter(m => m.id !== tempUserId && m.id !== tempAsstId))
            setLiveSteps([])
            setLiveAssistantId(null)
            setLoading(false)
            return
          }
          throw e
        }
        setMessages(prev => prev.map(m => m.id === tempAsstId ? {
          ...m,
          content: 'Generated a document from your prompt. Preview it or download the .docx when ready.',
          document_preview: generated,
          created_at: new Date().toISOString(),
        } : m))
        setPendingFiles([])
        setDocumentIntentOn(false)
        setDocumentBriefDraft(null)
        setDocumentBriefDetected(false)
        setDocumentPlanRecommendations({})
        setDocumentPlanCapabilities({ deepResearch: false, webSearch: false })
        setLiveSteps([])
        setLiveAssistantId(null)
        setLoading(false)
        return
      }

      const isArtifactRequest = !!artifactType

      const res = await apiFetch('/conversations/chat/stream', {
        method: 'POST',
        body: JSON.stringify({
          message: apiMessage,
          ...buildRequestFields(quality, opts.forceResearch ? true : researchOn, webSearchOn),
          force_model: forceModel.trim() || null,
          conversation_id: activeConvId,
          allow_research_recommendation: !opts.suppressResearchRecommendation,
          output_mode: artifactType ? 'architecture' : outputMode,
          artifact_type: artifactType,
          attached_documents: extractedDocs.map(d => ({
            name:       d.name,
            text:       d.text,
            char_count: d.char_count,
            pages:      d.pages_extracted,
            method:     d.method,
          })),
        }),
      })

      if (!res.ok || !res.body) {
        const err = await res.json().catch(() => ({ detail: 'Request failed' }))
        throw new Error((err as { detail: string }).detail || 'Request failed')
      }

      const reader = res.body.getReader()
      readerRef.current = reader
      const decoder = new TextDecoder()
      let buf = ''

      while (true) {
        const { done, value } = await reader.read()
        if (done) break

        buf += decoder.decode(value, { stream: true })
        const parts = buf.split('\n\n')
        buf = parts.pop() ?? ''

        for (const part of parts) {
          if (!part.trim()) continue
          let eventType = 'message', dataStr = ''
          for (const line of part.split('\n')) {
            if (line.startsWith('event: ')) eventType = line.slice(7).trim()
            else if (line.startsWith('data: ')) dataStr = line.slice(6)
          }
          if (!dataStr) continue
          const data = JSON.parse(dataStr)

          if (eventType === 'start') {
            startConvId = data.conversation_id as number
            setActiveConvId(startConvId)
            setConvUrlParam(startConvId)
            if (!bubblePreCreated) {
              setLiveAssistantId(tempAsstId)
              setMessages(prev => [...prev, { id: tempAsstId, role: 'assistant' as const, content: '', created_at: new Date().toISOString() }])
              setPipelineTs(Date.now())
            }
            setLiveSteps([])
            setLoading(false)

          } else if (eventType === 'pipeline_log') {
            const step: PipelineStep = {
              stage: data.stage as PipelineStage,
              message: data.message as string,
              ts: Date.now(),
              route: data.route as RouteDecision | undefined,
              intent: data.intent as string | undefined,
              turn_type: data.turn_type as string | undefined,
              sub_queries: data.sub_queries,
              queries: data.queries as string[] | undefined,
              idx: data.idx as number | undefined,
              model: data.model as string | undefined,
              task_type: data.task_type as string | null | undefined,
              latency_ms: data.latency_ms as number | undefined,
              cost_usd: data.cost_usd as number | null | undefined,
            }
            if (data.stage === 'sub_complete' && data.idx != null) {
              setSubCompletions(prev => new Map(prev).set(data.idx as number, step))
            } else {
              setLiveSteps(prev => [...prev, step])
              if (data.stage === 'routing' && data.route) {
                startRoute = data.route as RouteDecision
                startTurnType = (data.turn_type as string | null) ?? null
              }
            }

          } else if (eventType === 'research_recommendation') {
            const rec: ResearchRecommendation = {
              confidence: data.confidence as string,
              reason: data.reason as string,
              risk_factors: (data.risk_factors as string[]) ?? [],
              suggested_mode: (data.suggested_mode as ResearchMode) ?? 'deep',
              source: (data.source as string) ?? 'hybrid',
              original_message: sent,
              temp_user_id: tempUserId,
              temp_asst_id: tempAsstId,
            }
            if (wasNew) setActiveConvId(null)
            setLoading(false)
            setStreaming(false)
            setRefining(false)
            setLiveSteps([])
            setSubCompletions(new Map())
            setLiveAssistantId(null)
            setMessages(prev => prev.map(m =>
              m.id === tempAsstId
                ? { ...m, research_recommendation: rec }
                : m
            ))

          } else if (eventType === 'token') {
            setLiveSteps([])
            setSubCompletions(new Map())
            setStreaming(true)
            if (isArtifactRequest) {
              setArtifactGenerating(true)
            } else {
              setMessages(prev => prev.map(m => m.id === tempAsstId ? { ...m, content: m.content + (data.text as string) } : m))
            }

          } else if (eventType === 'refine_start') {
            setLiveSteps([])
            setSubCompletions(new Map())
            setRefining(true)
            setStreaming(false)
            setArtifactGenerating(false)
            setMessages(prev => prev.map(m => m.id === tempAsstId ? { ...m, content: '' } : m))

          } else if (eventType === 'refine_token') {
            setMessages(prev => prev.map(m => m.id === tempAsstId ? { ...m, content: m.content + (data.text as string) } : m))

          } else if (eventType === 'done') {
            if (!startRoute && data.route) startRoute = data.route as RouteDecision
            setStreaming(false)
            setRefining(false)
            setArtifactGenerating(false)
            setLiveSteps([])
            setSubCompletions(new Map())
            setLiveAssistantId(null)
            const execLog = (data.execution_log as ExecutionLog) ?? null
            const researchMeta = (data.research as ResearchMeta | undefined) ?? null
            startTurnType = execLog?.planner?.turn_type ?? null
            startAction   = execLog?.planner?.action ?? null
            setMessages(prev => prev.map(m => m.id === tempAsstId ? {
              ...m,
              id:                 data.message_id as number,
              content:            typeof data.answer === 'string' ? data.answer : m.content,
              route:              startRoute,
              task_type:          startRoute?.task_type ?? null,
              complexity:         startRoute?.complexity ?? null,
              model_used:         data.model_used as string,
              latency_ms:         data.latency_ms as number,
              estimated_cost_usd: data.estimated_cost_usd as number | null,
              prompt_tokens:      data.prompt_tokens as number | null,
              completion_tokens:  data.completion_tokens as number | null,
              execution_log:      execLog,
              turn_type:          startTurnType,
              action:             startAction,
              research_run_id:    (data.research_run_id as number | undefined) ?? researchMeta?.run_id ?? null,
              research:           researchMeta,
              document_preview:   data.document_preview
                ? {
                    title:      (data.document_preview as any).title,
                    docType:    (data.document_preview as any).doc_type,
                    markdown:   (data.document_preview as any).markdown,
                    filename:   (data.document_preview as any).filename,
                    docxBase64: (data.document_preview as any).docx_base64,
                  } as GeneratedDocument
                : null,
            } : m))

            if (wasNew && startConvId) {
              const list: ConversationSummary[] = await apiFetch('/conversations').then(r => r.json())
              setConversations(list)
            } else if (startConvId) {
              setConversations(prev => prev.map(c =>
                c.id === startConvId
                  ? { ...c, message_count: c.message_count + 2, updated_at: new Date().toISOString() }
                  : c
              ))
            }
            setPendingFiles([])
            setArtifactType(null)

          } else if (eventType === 'error') {
            throw new Error((data as { message: string }).message || 'Stream error')
          }
        }
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Unknown error')
      setMessages(prev => prev.filter(m => m.id !== tempUserId && m.id !== tempAsstId))
    } finally {
      setLoading(false)
      setStreaming(false)
      setRefining(false)
      setArtifactGenerating(false)
      setIsExtracting(false)
      setLiveSteps([])
      setSubCompletions(new Map())
      setLiveAssistantId(null)
    }
  }

  function actOnResearchRecommendation(rec: ResearchRecommendation, runResearch: boolean) {
    const original = rec.original_message?.trim()
    if (!original) return
    setMessages(prev => prev.filter(m => m.id !== rec.temp_user_id && m.id !== rec.temp_asst_id))
    if (runResearch) {
      submit(original, { forceResearch: true, suppressResearchRecommendation: true })
    } else {
      submit(original, { suppressResearchRecommendation: true })
    }
  }

  // ── Render ────────────────────────────────────────────────────────────────

  if (!accountStatusLoaded) {
    return (
      <div className="shell" style={{ alignItems: 'center', justifyContent: 'center', display: 'flex', minHeight: '100vh' }} />
    )
  }

  if (accountStatus === 'pending' && !isAdmin) {
    return (
      <div className="shell" style={{ alignItems: 'center', justifyContent: 'center', display: 'flex', minHeight: '100vh', padding: '24px', textAlign: 'center', position: 'relative' }}>
        <button
          className="toggle-chip"
          onClick={() => signOut(() => { window.location.href = '/' })}
          type="button"
          title="Log out"
          style={{
            position: 'absolute',
            top: 24,
            right: 24,
            zIndex: 50,
            padding: '8px 16px',
            fontSize: 13,
            background: 'var(--bg-s1)',
            borderColor: 'var(--bd2)',
            color: 'var(--t2)',
          }}
        >
          <i className="ti ti-logout" aria-hidden="true" /> Log out
        </button>
        <div className="card" style={{ maxWidth: 420, padding: '32px 28px' }}>
          <h2 style={{ marginTop: 0 }}>Account pending approval</h2>
          <p className="muted-text">
            Thanks for signing up for Fronei. An administrator needs to activate
            your account before you can start chatting. You&apos;ll be notified
            once it&apos;s approved — try refreshing this page later.
          </p>
        </div>
      </div>
    )
  }

  if (accountStatus === 'suspended' && !isAdmin) {
    return (
      <div className="shell" style={{ alignItems: 'center', justifyContent: 'center', display: 'flex', minHeight: '100vh', padding: '24px', textAlign: 'center', position: 'relative' }}>
        <button
          className="toggle-chip"
          onClick={() => signOut(() => { window.location.href = '/' })}
          type="button"
          title="Log out"
          style={{
            position: 'absolute',
            top: 24,
            right: 24,
            zIndex: 50,
            padding: '8px 16px',
            fontSize: 13,
            background: 'var(--bg-s1)',
            borderColor: 'var(--bd2)',
            color: 'var(--t2)',
          }}
        >
          <i className="ti ti-logout" aria-hidden="true" /> Log out
        </button>
        <div className="card" style={{ maxWidth: 420, padding: '32px 28px' }}>
          <h2 style={{ marginTop: 0 }}>Account suspended</h2>
          <p className="muted-text">
            This account has been suspended. Contact an administrator if you
            believe this is a mistake.
          </p>
        </div>
      </div>
    )
  }

  return (
    <div className="shell">

      {mobileNavOpen && (
        <div className="sidebar-overlay" onClick={() => setMobileNavOpen(false)} />
      )}

      <Sidebar
        activePage="chat"
        conversations={conversations}
        activeConvId={activeConvId}
        onLoadConversation={loadConversation}
        onNewConversation={newConversation}
        onDeleteConversation={deleteConversation}
        onExport={doExport}
        onDevModeChange={setDevMode}
        mobileNavOpen={mobileNavOpen}
        deleteConfirmId={deleteConfirmId}
        editingTitleId={editingTitleId}
        editingTitle={editingTitle}
        onEditingTitleChange={setEditingTitle}
        onStartEdit={(id, title) => { setEditingTitleId(id); setEditingTitle(title) }}
        onRenameConversation={renameConversation}
        onCancelEdit={() => setEditingTitleId(null)}
        onOpenSettings={openSettingsView}
        settingsActive={settingsViewOpen}
      />

      {/* ── Main area ── */}
      <div className="main-area workbench-ui">

        {settingsViewOpen ? (
          <SettingsView
            onClose={() => { setSettingsViewOpen(false); setSettingsInitialTab(undefined) }}
            theme={theme}
            accentTheme={accentTheme}
            onThemeChange={setTheme}
            onAccentThemeChange={setAccentTheme}
            devMode={devMode}
            onDevModeChange={setDevMode}
            showWebSearch={showWebSearch}
            onShowWebSearchChange={setShowWebSearch}
            twinProfile={twinProfile}
            twinSamples={twinSamples}
            newSampleText={newSampleText}
            newSampleLabel={newSampleLabel}
            onNewSampleTextChange={setNewSampleText}
            onNewSampleLabelChange={setNewSampleLabel}
            onAddSample={addSample}
            onDeleteSample={deleteSample}
            onReExtract={reExtract}
            sampleSubmitting={sampleSubmitting}
            memories={memories}
            memoriesLoaded={memoriesLoaded}
            personalProfile={personalProfile}
            profileLoaded={personalProfileLoaded}
            onUpdateMemory={updateMemory}
            onDeleteMemory={deleteMemory}
            onClearMemories={clearAllMemories}
            onSaveProfileOverrides={saveProfileOverrides}
            userName={userName}
            userDomain={userDomain}
            onUserNameChange={setUserName}
            onUserDomainChange={setUserDomain}
            onUserNameSave={saveUserName}
            onUserDomainSave={saveUserDomain}
            persona={persona}
            visibleArtifacts={visibleArtifacts}
            onPersonaChange={selectPersona}
            onArtifactToggle={toggleArtifactVisibility}
            outputMode={outputMode}
            onOutputModeChange={setOutputMode}
            quality={quality}
            onQualityChange={setQuality}
            isAdmin={canSeeAdmin}
            apiFetch={apiFetch}
            initialTab={settingsInitialTab}
          />
        ) : (
        <>
        {/* Topbar */}
        <div className="topbar">
          <span className="topbar-title">
            {activeConv ? activeConv.title : 'New chat with Fronei'}
          </span>
          <div className="topbar-controls">
            <div className="topbar-chip">
              <i className="ti ti-adjustments-horizontal" />
              {researchOn ? 'Research' : quality === 'quick' ? 'Quick' : quality === 'thorough' ? 'Thorough' : 'Smart'}
              {webSearchOn && !researchOn && ' · Web'}
            </div>
            {devMode && (
              <button
                className={`topbar-icon-btn${rightPanelOpen ? ' active' : ''}`}
                onClick={() => setRightPanelOpen(v => !v)}
                title={`${rightPanelOpen ? 'Close' : 'Open'} execution log (⌘E)`}
                aria-label="Toggle execution log panel"
              >
                <i className="ti ti-activity" />
              </button>
            )}
          </div>
        </div>

        {/* Workspace */}
        <div className="workspace">

          {/* Chat thread */}
          <div
            className={`chat-thread${dragOver ? ' drag-over' : ''}`}
            ref={threadRef}
            onScroll={onThreadScroll}
            role="log"
            aria-live="polite"
            aria-label="Conversation"
            onDragOver={e => { e.preventDefault(); setDragOver(true) }}
            onDragLeave={() => setDragOver(false)}
            onDrop={e => { e.preventDefault(); setDragOver(false); addFiles(e.dataTransfer.files) }}
          >
            {messages.length === 0 && !loading ? (
              <div className="workbench-home">
                <div className="workbench-kicker">{workbench.kicker}</div>
                <h1>{userName ? `${workbench.headline.replace(/\?$/, '')}, ${userName.split(' ')[0]}?` : workbench.headline}</h1>
                <p className="workbench-subhead">{workbench.subhead}</p>
                <div className="workbench-mode-grid">
                  {workbench.actions.map(action => (
                    <button
                      key={action.title}
                      className="workbench-mode-card"
                      type="button"
                      onClick={() => { setMessage(action.prompt); taRef.current?.focus() }}
                    >
                      <i className={`ti ${action.icon}`} aria-hidden="true" />
                      <strong>{action.title}</strong>
                      <span>{action.desc}</span>
                    </button>
                  ))}
                </div>
                <div className="workbench-rail">
                  <div><span>Role</span><strong>{workbench.railLabel}</strong></div>
                  <div><span>Mode</span><strong>{researchOn ? 'Research' : quality}</strong></div>
                  <div><span>Voice</span><strong>{hasProfile ? 'Active' : 'Not trained'}</strong></div>
                  <div><span>Artifacts</span><strong>{visibleArtifacts.length}</strong></div>
                </div>
              </div>
            ) : (
              <>
                {messages.map((m, i) => {
                  const isNew = !renderedIdsRef.current.has(m.id)
                  return (
                  <div key={m.id} className={`turn turn-${m.role === 'user' ? 'user' : 'asst'}${isNew ? '' : ' no-anim'}`}>
                    {m.role === 'assistant' && m.model_used ? (() => {
                      const p = getProvider(m.model_used)
                      return (
                        <div className="turn-header">
                          {devMode && <span className="model-badge" style={{ background: p.bg, color: p.color }}>{m.model_used}</span>}
                          {devMode && m.turn_type && <span className="turn-type-badge">{m.turn_type}{m.action ? ` · ${m.action}` : ''}</span>}
                        </div>
                      )
                    })() : (
                      m.role === 'user' && <span className="turn-role">You</span>
                    )}

                    {m.role === 'user' && m.attached_files && m.attached_files.length > 0 && (
                      <div className="attachment-badges">
                        {m.attached_files.map((f, fi) => (
                          <div key={fi} className="attachment-badge">
                            <i className={`ti ${f.method === 'vision' ? 'ti-eye' : 'ti-file-text'}`} aria-hidden="true" />
                            <span className="attachment-name">{f.name}</span>
                            <span className="attachment-meta">
                              {f.pages ? `${f.pages}p · ` : ''}
                              {f.method === 'vision' ? 'vision' : f.method ? 'parsed' : ''}
                            </span>
                          </div>
                        ))}
                      </div>
                    )}

                    <div className="turn-bubble">
                      {m.role === 'user' ? (
                        editingMsgId === m.id ? (
                          <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                            <textarea
                              className="composer-ta"
                              style={{ minHeight: 60, border: '1px solid var(--ac-bd)', borderRadius: 10, padding: '8px 12px' }}
                              value={editText}
                              onChange={e => setEditText(e.target.value)}
                              onKeyDown={e => {
                                if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); saveEdit() }
                                if (e.key === 'Escape') setEditingMsgId(null)
                              }}
                              onInput={(e) => {
                                const ta = e.currentTarget
                                ta.style.height = 'auto'
                                ta.style.height = Math.min(ta.scrollHeight, 180) + 'px'
                              }}
                              autoFocus
                            />
                            <div style={{ display: 'flex', gap: 6, justifyContent: 'flex-end' }}>
                              <button
                                className="toggle-chip"
                                onClick={() => setEditingMsgId(null)}
                                type="button"
                              >Cancel</button>
                              <button
                                className="send-btn"
                                style={{ padding: '5px 14px', fontSize: 12 }}
                                onClick={saveEdit}
                                disabled={!editText.trim()}
                                type="button"
                              >Save &amp; re-run</button>
                            </div>
                          </div>
                        ) : m.content
                      ) : (
                        m.research_recommendation
                          ? (
                            <div className="research-rec-card">
                              <div className="research-rec-icon">
                                <i className="ti ti-microscope" aria-hidden="true" />
                              </div>
                              <div className="research-rec-body">
                                <div className="research-rec-title">Deep research recommended</div>
                                <p>{m.research_recommendation.reason}</p>
                                {m.research_recommendation.risk_factors.length > 0 && (
                                  <div className="research-rec-tags">
                                    {m.research_recommendation.risk_factors.slice(0, 4).map(f => (
                                      <span key={f}>{f.replace(/_/g, ' ')}</span>
                                    ))}
                                  </div>
                                )}
                                <div className="research-rec-actions">
                                  <button
                                    className="send-btn"
                                    type="button"
                                    onClick={() => actOnResearchRecommendation(m.research_recommendation!, true)}
                                  >
                                    Run deep research
                                  </button>
                                  <button
                                    className="toggle-chip"
                                    type="button"
                                    onClick={() => actOnResearchRecommendation(m.research_recommendation!, false)}
                                  >
                                    Continue quick answer
                                  </button>
                                </div>
                              </div>
                            </div>
                          )
                          : m.id === liveAssistantId && artifactGenerating
                          ? (
                            <ArtifactTicker artifactType={artifactType} />
                          )
                          : m.content
                          ? (
                            <AssistantContent message={m} />
                          )
                          : m.id === liveAssistantId && liveSteps.length > 0
                            ? (
                              <PipelineLog
                                steps={liveSteps}
                                startTs={pipelineTs}
                                sourceText={lastSentRef.current}
                                subCompletions={subCompletions}
                              />
                            )
                            : !isExtracting
                              ? <div className="thinking-state">
                                  <div className="typing-dot"><span /><span /><span /></div>
                                  <span className="thinking-label">Fronei is thinking…</span>
                                </div>
                              : null
                      )}
                    </div>

                    {m.role === 'assistant' && m.document_preview && (
                      <div className="doc-generated-callout">
                        <div className="doc-generated-icon">
                          <i className="ti ti-file-text" aria-hidden="true" />
                        </div>
                        <div className="doc-generated-body">
                          <div className="doc-generated-title">Document generated</div>
                        </div>
                        <button
                          className="doc-generated-icon-btn"
                          type="button"
                          onClick={() => setPreviewDoc(m.document_preview!)}
                          title="Preview document"
                          aria-label="Preview document"
                        >
                          <i className="ti ti-eye" aria-hidden="true" />
                        </button>
                        <button
                          className="doc-generated-icon-btn doc-generated-download"
                          type="button"
                          onClick={() => downloadBlob(base64ToBlob(m.document_preview!.docxBase64, DOCX_MIME), m.document_preview!.filename)}
                          title="Download .docx"
                          aria-label="Download document"
                        >
                          <i className="ti ti-file-download" aria-hidden="true" />
                        </button>
                        {m.document_preview.outputFormats?.includes('markdown') && (
                          <button
                            className="doc-generated-icon-btn"
                            type="button"
                            onClick={() => downloadBlob(
                              new Blob([m.document_preview!.markdown], { type: 'text/markdown;charset=utf-8' }),
                              safeDownloadName(m.document_preview!.title, 'md')
                            )}
                            title="Download Markdown"
                            aria-label="Download Markdown"
                          >
                            <i className="ti ti-markdown" aria-hidden="true" />
                          </button>
                        )}
                      </div>
                    )}

                    {m.content && (
                      <div className="turn-actions">
                        {m.role === 'user' && !isBusy && (
                          <button
                            className="action-btn"
                            onClick={() => { setEditingMsgId(m.id); setEditText(m.content) }}
                            title="Edit message"
                            aria-label="Edit message"
                          >
                            <i className="ti ti-edit" aria-hidden="true" />
                          </button>
                        )}
                        <button
                          className={`action-btn${copied === m.id ? ' copied' : ''}`}
                          onClick={() => copyMessage(m.content, m.id)}
                          title={copied === m.id ? 'Copied!' : 'Copy'}
                          aria-label="Copy message"
                        >
                          <i className={`ti ${copied === m.id ? 'ti-check' : 'ti-copy'}`} aria-hidden="true" />
                        </button>
                        {m.role === 'assistant' && (
                          <button
                            className="action-btn"
                            onClick={() => downloadDocx(activeConv?.title ?? 'Fronei response', m.content, 'Generated by Fronei')}
                            title="Download as DOCX"
                            aria-label="Download as DOCX"
                          >
                            <i className="ti ti-file-download" aria-hidden="true" />
                          </button>
                        )}
                        {m.created_at && (
                          <span className="msg-time" title={new Date(m.created_at).toLocaleString()}>
                            {fmtTime(m.created_at)}
                          </span>
                        )}
                        {m.role === 'assistant' && m.model_used && (
                          <>
                            {devMode && m.latency_ms != null && <span className="latency-tag">{m.latency_ms} ms</span>}
                            <button
                              className="action-btn"
                              disabled={isBusy}
                              title="Retry"
                              aria-label="Retry message"
                              onClick={() => {
                                const userMsg = messages.slice(0, i).reverse().find(msg => msg.role === 'user')
                                if (!userMsg) return
                                setMessages(prev => {
                                  const userIdx = prev.findIndex(m => m.id === userMsg.id)
                                  return userIdx >= 0 ? prev.slice(0, userIdx) : prev
                                })
                                submit(userMsg.content)
                              }}
                            >
                              <i className="ti ti-refresh" aria-hidden="true" />
                            </button>
                            {devMode && (
                              <button
                                className="exec-log-btn"
                                onClick={() => openExecPanel(m)}
                                aria-label="View execution log"
                              >
                                <i className="ti ti-activity" aria-hidden="true" />
                                Log
                              </button>
                            )}
                          </>
                        )}
                      </div>
                    )}
                    {m.role === 'assistant' &&
                     m.model_used &&
                     !loading &&
                     !streaming &&
                     i === messages.length - 1 &&
                     !devMode && (
                      <div className="followup-chips">
                        {getFollowups(m.task_type).map(s => (
                          <button
                            key={s}
                            className="followup-chip"
                            onClick={() => { setMessage(s); taRef.current?.focus() }}
                            type="button"
                          >
                            {s}
                          </button>
                        ))}
                      </div>
                    )}
                    {/* Contextual artifact suggestion */}
                    {m.role === 'assistant' && m.content &&
                     !loading && !streaming &&
                     i === messages.length - 1 &&
                     !dismissedSuggestions.has(m.id) &&
                     suggestArtifacts(m).length > 0 && (
                      <div className="artifact-suggestion">
                        <span className="artifact-suggestion-label">Format as</span>
                        {suggestArtifacts(m).map(type => {
                          const a = ARTIFACT_TYPES.find(x => x.value === type)!
                          return (
                            <button
                              key={type}
                              className="artifact-suggestion-chip"
                              onClick={() => reformatAs(type)}
                              type="button"
                              title={a.hint}
                            >
                              <i className={`ti ${a.icon}`} aria-hidden="true" />
                              {a.label}
                            </button>
                          )
                        })}
                        <button
                          className="artifact-suggestion-dismiss"
                          onClick={() => setDismissedSuggestions(prev => new Set([...prev, m.id]))}
                          aria-label="Dismiss suggestions"
                          type="button"
                        >
                          <i className="ti ti-x" aria-hidden="true" />
                        </button>
                      </div>
                    )}
                  </div>
                  )
                })}

                {refining && (
                  <div className="turn turn-asst">
                    <div className="turn-bubble">
                      <div className="thinking-state">
                        <div className="typing-dot"><span /><span /><span /></div>
                        <span className="thinking-label refining-label">✦ Refining…</span>
                      </div>
                    </div>
                  </div>
                )}
              </>
            )}
          </div>

          {showScrollBtn && (
            <button
              className="scroll-bottom-btn"
              onClick={scrollToBottom}
              aria-label="Scroll to bottom"
            >
              <i className="ti ti-arrow-down" aria-hidden="true" />
            </button>
          )}

          {/* Right execution panel */}
          {rightPanelOpen && (
            <aside
              className="right-panel"
              style={{ width: rightPanelWidth, minWidth: rightPanelWidth }}
              aria-label="Execution log"
            >
              <div className="rp-resize-handle" onMouseDown={onRightPanelResizeMouseDown} title="Drag to resize" />
              <div className="rp-header">
                <span className="rp-title">Execution log</span>
                <button className="rp-close" onClick={() => setRightPanelOpen(false)} aria-label="Close panel">
                  <i className="ti ti-x" aria-hidden="true" />
                </button>
              </div>
              <div className="rp-body">
                {execPanelData ? (
                  <ExecLogView data={execPanelData} />
                ) : (
                  <div className="rp-empty">
                    <i className="ti ti-activity" aria-hidden="true" />
                    <span>Click the log button on any assistant message to inspect its execution.</span>
                  </div>
                )}
              </div>
            </aside>
          )}

        </div>

        {/* Error */}
        {error && (
          <div className="error-bar" role="alert">
            <span>{error}</span>
            <button
              className="error-dismiss"
              onClick={() => setError('')}
              aria-label="Dismiss error"
            >
              <i className="ti ti-x" aria-hidden="true" />
            </button>
          </div>
        )}

        {/* Composer */}
        <input
          ref={fileInputRef}
          type="file"
          multiple
          accept=".pdf,.docx,.pptx,.txt,.md,.csv,.tsv,.xlsx,.html,.htm,.json,.yaml,.yml,.xml,.svg,.jpg,.jpeg,.png,.gif,.webp,.tiff,.tif,.bmp"
          style={{ display: 'none' }}
          onChange={e => { addFiles(e.target.files); e.target.value = '' }}
        />
        <div className="composer-wrap">
          <div className="composer-box">
            {(pendingFiles.length > 0 || attachError) && (
              <div className="attach-strip">
                {pendingFiles.map(pf => (
                  <div key={pf.id} className={`attach-chip${isExtracting ? ' attach-extracting' : ''}`}>
                    <i className="ti ti-file" aria-hidden="true" />
                    <span className="attach-name">{pf.name}</span>
                    <span className="attach-meta">
                      {pf.size > 1024 * 1024
                        ? `${(pf.size / (1024 * 1024)).toFixed(1)} MB`
                        : `${Math.round(pf.size / 1024)} KB`}
                    </span>
                    {!isExtracting && (
                      <button className="attach-x" onClick={() => removeFile(pf.id)}
                        aria-label={`Remove ${pf.name}`}>
                        <i className="ti ti-x" aria-hidden="true" />
                      </button>
                    )}
                  </div>
                ))}
                {attachError && (
                  <div className="attach-chip attach-err">
                    <i className="ti ti-alert-circle" aria-hidden="true" />
                    <span>{attachError}</span>
                    <button className="attach-x" onClick={() => setAttachError('')}
                      aria-label="Dismiss error">
                      <i className="ti ti-x" aria-hidden="true" />
                    </button>
                  </div>
                )}
              </div>
            )}
            <textarea
              ref={taRef}
              className="composer-ta"
              value={message}
              onChange={e => setMessage(e.target.value)}
              placeholder="Ask Fronei anything…"
              onKeyDown={e => {
                if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); submit() }
              }}
              onInput={(e) => {
                const ta = e.currentTarget
                ta.style.height = 'auto'
                ta.style.height = Math.min(ta.scrollHeight, 180) + 'px'
              }}
              aria-label="Message input"
            />
            <div className="composer-toolbar">

              {/* Left: + button */}
              <button
                className={`composer-plus${leftMenuOpen ? ' active' : ''}${(researchOn || webSearchOn || documentIntentOn || pendingFiles.length > 0) ? ' has-active' : ''}`}
                onClick={() => setLeftMenuOpen(v => !v)}
                disabled={isBusy}
                type="button"
                aria-label="Attach and more"
              >
                <i className="ti ti-plus" aria-hidden="true" />
                {pendingFiles.length > 0 && <span className="plus-badge">{pendingFiles.length}</span>}
              </button>

              {researchOn && (
                <button
                  className="composer-mode-chip"
                  type="button"
                  onClick={() => setResearchOn(false)}
                  title="Cancel research mode"
                  aria-label="Cancel research mode"
                >
                  <i className="ti ti-microscope" aria-hidden="true" />
                  <span>Research</span>
                  <i className="ti ti-x" aria-hidden="true" />
                </button>
              )}

              {documentIntentOn && (
                <button
                  className="composer-mode-chip"
                  type="button"
                  onClick={() => {
                    setDocumentIntentOn(false)
                    setDocumentBriefDraft(null)
                    setDocumentBriefDetected(false)
                    setDocumentPlanRecommendations({})
                    setDocumentPlanCapabilities({ deepResearch: false, webSearch: false })
                  }}
                  title="Cancel document mode"
                  aria-label="Cancel document mode"
                >
                  <i className="ti ti-file-text" aria-hidden="true" />
                  <span>Document</span>
                  <i className="ti ti-x" aria-hidden="true" />
                </button>
              )}

              <div className="c-spacer" />

              {/* Format as artifact button — only shown when persona has artifacts configured */}
              {visibleArtifacts.length > 0 && <button
                className={`action-btn${artifactPickerOpen || artifactType ? ' active' : ''}`}
                onClick={() => setArtifactPickerOpen(v => !v)}
                disabled={isBusy}
                type="button"
                aria-label="Format as artifact"
                title={artifactType ? `Artifact: ${ARTIFACT_TYPES.find(a => a.value === artifactType)?.label}` : 'Format as artifact'}
              >
                <i className="ti ti-layout-grid" aria-hidden="true" />
                {artifactType && (
                  <span className="artifact-active-label">
                    {ARTIFACT_TYPES.find(a => a.value === artifactType)?.label}
                  </span>
                )}
              </button>}

              {/* Options popover trigger */}
              {(hasProfile || devMode || showWebSearch) && (
                <button
                  className={`action-btn${optionsOpen ? ' active' : ''}`}
                  onClick={() => setOptionsOpen(v => !v)}
                  disabled={isBusy}
                  type="button"
                  aria-label="More options"
                  title="Output mode and advanced options"
                >
                  <i className="ti ti-dots" aria-hidden="true" />
                </button>
              )}

              {/* Send / Stop */}
              {(streaming || refining || liveAssistantId !== null) ? (
                <button className="stop-btn" onClick={abortStream} type="button" aria-label="Stop generating">
                  <i className="ti ti-square" aria-hidden="true" />
                  Stop
                </button>
              ) : (
                <button
                  className="send-btn"
                  disabled={isBusy || (!message.trim() && pendingFiles.length === 0) || editingMsgId !== null}
                  onClick={() => submit()}
                  aria-label="Send message"
                >
                  <i className="ti ti-send" aria-hidden="true" />
                  {isExtracting
                    ? `Extracting${pendingFiles.length > 1 ? ` ${pendingFiles.length} files` : ''}…`
                    : loading ? 'Routing…'
                    : streaming ? 'Streaming…'
                    : 'Send'}
                </button>
              )}
            </div>
          </div>

          {/* Left menu popup — outside overflow:hidden */}
          {leftMenuOpen && (
            <div className="left-menu-popup">
              <button
                className={`left-menu-item${pendingFiles.length > 0 ? ' on' : ''}`}
                type="button"
                onClick={() => { fileInputRef.current?.click(); setLeftMenuOpen(false) }}
              >
                <i className="ti ti-paperclip" aria-hidden="true" />
                <span>Attach files</span>
                <span className="left-menu-status">{pendingFiles.length > 0 ? `${pendingFiles.length} pending` : 'Add'}</span>
              </button>
              <button
                className={`left-menu-item${researchOn ? ' on' : ''}`}
                type="button"
                onClick={() => {
                  setResearchOn(v => {
                    const next = !v
                    if (next) {
                      setDocumentIntentOn(false)
                      setDocumentBriefDraft(null)
                      setDocumentBriefDetected(false)
                      setDocumentPlanRecommendations({})
                      setDocumentPlanCapabilities({ deepResearch: false, webSearch: false })
                    }
                    return next
                  })
                  setLeftMenuOpen(false)
                }}
              >
                <i className="ti ti-microscope" aria-hidden="true" />
                <span>Research</span>
                <span className="left-menu-status">{researchOn ? 'On' : 'Off'}</span>
              </button>
              <button
                className={`left-menu-item${documentIntentOn ? ' on' : ''}`}
                type="button"
                onClick={() => {
                  const next = !documentIntentOn
                  setDocumentIntentOn(next)
                  setLeftMenuOpen(false)
                  if (next) {
                    setResearchOn(false)
                    setDocumentBriefDetected(false)
                    setDocumentPlanRecommendations({})
                    setDocumentPlanCapabilities({ deepResearch: false, webSearch: false })
                  } else {
                    setDocumentBriefDraft(null)
                    setDocumentBriefDetected(false)
                    setDocumentPlanRecommendations({})
                    setDocumentPlanCapabilities({ deepResearch: false, webSearch: false })
                  }
                }}
              >
                <i className="ti ti-file-text" aria-hidden="true" />
                <span>Document</span>
                <span className="left-menu-status">{documentIntentOn ? 'On' : 'Off'}</span>
              </button>
            </div>
          )}

          {/* Artifact picker — outside overflow:hidden */}
          {artifactPickerOpen && (
            <div className="artifact-picker">
              {ARTIFACT_TYPES.filter(a => visibleArtifacts.includes(a.value)).map(a => (
                <button
                  key={a.value}
                  className={`artifact-picker-item${artifactType === a.value ? ' active' : ''}`}
                  onClick={() => {
                    setArtifactType(prev => prev === a.value ? null : a.value)
                    setArtifactPickerOpen(false)
                  }}
                  type="button"
                >
                  <i className={`ti ${a.icon}`} aria-hidden="true" />
                  <div>
                    <div className="artifact-picker-label">{a.label}</div>
                    <div className="artifact-picker-hint">{a.hint}</div>
                  </div>
                  {artifactType === a.value && <i className="ti ti-check artifact-picker-check" aria-hidden="true" />}
                </button>
              ))}
              {artifactType && (
                <button
                  className="artifact-picker-clear"
                  onClick={() => { setArtifactType(null); setArtifactPickerOpen(false) }}
                  type="button"
                >
                  <i className="ti ti-x" aria-hidden="true" />
                  Clear format
                </button>
              )}
            </div>
          )}

          {/* Options popover — outside overflow:hidden */}
          {optionsOpen && (
            <div className="options-popover">
              {hasProfile && (
                <div className="options-row">
                  <span className="options-label">Output</span>
                  <select className="c-select" value={outputMode}
                    onChange={e => setOutputMode(e.target.value as OutputMode)} style={{ flex: 1 }}>
                    {OUTPUT_MODES.map(m => <option key={m.value} value={m.value}>{m.label}</option>)}
                  </select>
                </div>
              )}
              {showWebSearch && (
                <div className="options-row">
                  <span className="options-label">Source</span>
                  <div className="theme-btn-group options-segment">
                    <button
                      className={`theme-btn-opt${!webSearchOn ? ' active' : ''}`}
                      onClick={() => setWebSearchOn(false)}
                      type="button"
                    >
                      Auto
                    </button>
                    <button
                      className={`theme-btn-opt${webSearchOn ? ' active' : ''}`}
                      onClick={() => setWebSearchOn(true)}
                      type="button"
                    >
                      Force web
                    </button>
                  </div>
                </div>
              )}
              {devMode && (
                <div className="options-row">
                  <span className="options-label">Model</span>
                  <select className="c-select" value={forceModel}
                    onChange={e => setForceModel(e.target.value)} style={{ flex: 1, maxWidth: 180 }}>
                    <option value="">Auto route</option>
                    {modelOptions.map(m => <option key={m} value={m}>{m}</option>)}
                  </select>
                </div>
              )}
            </div>
          )}
        </div>
        </>
        )}

      </div>

      {/* ── Mobile bottom nav ── */}
      <div className="mobile-nav" role="navigation" aria-label="Mobile navigation">
        <button
          className={`mobile-nav-btn${mobileNavOpen ? ' active' : ''}`}
          onClick={() => setMobileNavOpen(v => !v)}
          aria-label="Open menu"
          aria-expanded={mobileNavOpen}
        >
          <i className="ti ti-menu-2" aria-hidden="true" />
          <span>Menu</span>
        </button>
        <button
          className="mobile-nav-btn"
          onClick={newConversation}
          aria-label="New chat"
        >
          <i className="ti ti-plus" aria-hidden="true" />
          <span>New</span>
        </button>
        <button
          className={`mobile-nav-btn${!settingsViewOpen ? ' active' : ''}`}
          onClick={() => {
            setSettingsViewOpen(false)
            taRef.current?.focus()
          }}
          aria-label="Chat"
          aria-current={!settingsViewOpen ? 'page' : undefined}
        >
          <i className="ti ti-message" aria-hidden="true" />
          <span>Chat</span>
        </button>
      </div>

      {showOnboarding && (
        <OnboardingModal onComplete={completeOnboarding} />
      )}

      {previewDoc && (
        <DocumentPreviewModal doc={previewDoc} onClose={() => setPreviewDoc(null)} />
      )}
      {documentBriefDraft && (
        <DocumentPlanModal
          brief={documentBriefDraft}
          detected={documentBriefDetected}
          capabilities={documentPlanCapabilities}
          recommendations={documentPlanRecommendations}
          onChange={setDocumentBriefDraft}
          onCapabilitiesChange={setDocumentPlanCapabilities}
          onClose={() => {
            setDocumentBriefDraft(null)
          }}
          onCancel={() => {
            setDocumentBriefDraft(null)
            setDocumentIntentOn(false)
            setDocumentBriefDetected(false)
            setDocumentPlanRecommendations({})
            setDocumentPlanCapabilities({ deepResearch: false, webSearch: false })
          }}
          onSendAsChat={() => {
            setDocumentBriefDraft(null)
            setDocumentIntentOn(false)
            setDocumentBriefDetected(false)
            setDocumentPlanRecommendations({})
            setDocumentPlanCapabilities({ deepResearch: false, webSearch: false })
            submit(undefined, { suppressDocumentDetection: true })
          }}
          onGenerate={(brief, capabilities) => {
            const hadResearchRecommendation = !!documentPlanRecommendations.deepResearch
            const hadWebRecommendation = !!documentPlanRecommendations.webSearch
            setDocumentBriefDraft(null)
            setDocumentBriefDetected(false)
            setDocumentPlanRecommendations({})
            submit(undefined, {
              documentBrief: brief,
              forceDocumentResearch: capabilities.deepResearch,
              forceDocumentWebSearch: capabilities.webSearch,
              suppressDocumentResearchRecommendation: capabilities.deepResearch || hadResearchRecommendation,
              suppressDocumentWebSearchRecommendation: capabilities.webSearch || hadWebRecommendation,
            })
          }}
        />
      )}

    </div>
  )
}
