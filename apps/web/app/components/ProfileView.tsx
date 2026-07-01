'use client'

import {
  AlertTriangle,
  ArrowLeft,
  Check,
  Download,
  FileText,
  Loader2,
  Pencil,
  RefreshCw,
  Sliders,
  Sparkles,
  Trash2,
  Upload,
  X,
} from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { Area, AreaChart, Bar, BarChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { useProfile } from '../hooks/useProfile'
import { formatAppDateTime } from '../lib/format'
import type { DocumentTemplateOption, OutputFormat, ProfileSettings, ProfileWorkspace, QualityMode, ResearchLevel } from '../types'
import { Badge, Card } from './ui/Card'
import { SelectField } from './ui/Field'

const RANGE_OPTIONS = [
  { value: '7d', label: '7d' },
  { value: '30d', label: '30d' },
  { value: '90d', label: '90d' },
  { value: 'all', label: 'All' },
]
const QUALITY_OPTIONS = [
  { value: 'draft', label: 'draft' },
  { value: 'standard', label: 'standard' },
  { value: 'executive', label: 'executive' },
]
const OUTPUT_OPTIONS = [
  { value: 'chat', label: 'chat' },
  { value: 'markdown', label: 'markdown' },
  { value: 'docx', label: 'docx' },
  { value: 'pptx', label: 'pptx' },
]
const RESEARCH_OPTIONS = [
  { value: 'auto', label: 'auto' },
  { value: 'easy', label: 'easy' },
  { value: 'regular', label: 'regular' },
  { value: 'deep', label: 'deep' },
]

export function ProfileView({ onClose }: { onClose: () => void }) {
  const profile = useProfile()
  const uploadTemplateRef = useRef<HTMLInputElement | null>(null)
  const replaceTemplateRef = useRef<HTMLInputElement | null>(null)
  const [range, setRange] = useState('30d')
  const [activeTab, setActiveTab] = useState<'settings' | 'templates' | 'workspaces' | 'usage'>('settings')
  const [newPreference, setNewPreference] = useState('')
  const [workspacePriorityDrafts, setWorkspacePriorityDrafts] = useState<Record<string, string>>({})
  const [renamingTemplateId, setRenamingTemplateId] = useState<string | null>(null)
  const [templateNameDraft, setTemplateNameDraft] = useState('')
  const [replaceTemplateId, setReplaceTemplateId] = useState<string | null>(null)
  const [deleteTemplateId, setDeleteTemplateId] = useState<string | null>(null)
  const [deleteConfirmOpen, setDeleteConfirmOpen] = useState(false)
  const [deleteAcknowledged, setDeleteAcknowledged] = useState(false)
  const [deleting, setDeleting] = useState(false)
  const [deleted, setDeleted] = useState(false)
  const [exporting, setExporting] = useState(false)

  useEffect(() => {
    void profile.loadAll(range)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  function handleRangeChange(value: string) {
    setRange(value)
    void profile.loadUsage(value)
  }

  async function handleExport() {
    setExporting(true)
    try {
      const data = await profile.exportMyData()
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' })
      const url = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = url
      link.download = `fronei-my-data-${new Date().toISOString().slice(0, 10)}.json`
      link.click()
      URL.revokeObjectURL(url)
    } catch (err) {
      profile.setError(err instanceof Error ? err.message : 'Could not export data')
    } finally {
      setExporting(false)
    }
  }

  async function handleDelete() {
    setDeleting(true)
    try {
      await profile.deleteMyData()
      setDeleted(true)
      setDeleteConfirmOpen(false)
    } catch (err) {
      profile.setError(err instanceof Error ? err.message : 'Could not delete data')
    } finally {
      setDeleting(false)
    }
  }

  async function handleUploadTemplate(file: File | null) {
    const uploaded = await profile.uploadTemplate(file)
    if (uploaded && !profile.me?.settings.default_template_id) {
      void profile.updateSettings({ default_template_id: uploaded.id })
    }
  }

  async function handleReplaceTemplate(file: File | null) {
    if (!replaceTemplateId) return
    await profile.replaceTemplate(replaceTemplateId, file)
    setReplaceTemplateId(null)
  }

  async function handleDeleteTemplate(templateId: string) {
    const ok = await profile.deleteTemplate(templateId)
    if (ok) setDeleteTemplateId(null)
  }

  if (deleted) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-3 p-8 text-center">
        <Trash2 size={28} className="text-neutral-400" />
        <p className="text-lg font-bold text-neutral-900 dark:text-neutral-50">Your data has been deleted</p>
        <p className="max-w-sm text-sm text-neutral-500 dark:text-neutral-400">
          Workspaces, conversations, turns, templates, and your consolidated preferences are gone. Starting a new task will create a fresh workspace.
        </p>
        <button
          type="button"
          onClick={onClose}
          className="mt-2 rounded-lg bg-neutral-900 px-4 py-2 text-sm font-bold text-white dark:bg-white dark:text-neutral-900"
        >
          Back to chat
        </button>
      </div>
    )
  }

  return (
    <div className="flex h-full min-h-0 flex-col overflow-hidden bg-white dark:bg-neutral-950">
      <input
        ref={uploadTemplateRef}
        type="file"
        accept=".pptx"
        className="hidden"
        onChange={event => {
          void handleUploadTemplate(event.target.files?.[0] ?? null)
          event.target.value = ''
        }}
      />
      <input
        ref={replaceTemplateRef}
        type="file"
        accept=".pptx"
        className="hidden"
        onChange={event => {
          void handleReplaceTemplate(event.target.files?.[0] ?? null)
          event.target.value = ''
        }}
      />
      <header className="flex flex-shrink-0 items-center justify-between gap-4 border-b border-neutral-200 px-4 py-4 sm:px-8 dark:border-neutral-800">
        <div className="flex items-center gap-3">
          <button
            type="button"
            onClick={onClose}
            aria-label="Back to chat"
            title="Back to chat"
            className="grid h-8 w-8 flex-shrink-0 place-items-center rounded-full border border-neutral-200 text-neutral-500 hover:bg-neutral-100 dark:border-neutral-800 dark:text-neutral-400 dark:hover:bg-neutral-800"
          >
            <ArrowLeft size={15} />
          </button>
          <div>
            <p className="text-[11px] font-bold uppercase tracking-wider text-neutral-400">Your account</p>
            <h2 className="text-xl font-bold text-neutral-900 dark:text-neutral-50">Profile</h2>
          </div>
        </div>
        {profile.me && (
          <p className="hidden truncate text-sm text-neutral-500 dark:text-neutral-400 sm:block">
            {profile.me.name || profile.me.email || profile.me.user_id}
          </p>
        )}
      </header>

      <div className="flex flex-shrink-0 gap-1 overflow-x-auto border-b border-neutral-200 px-4 py-2 sm:px-8 dark:border-neutral-800">
        {[
          { id: 'settings', label: 'Settings' },
          { id: 'templates', label: 'Templates' },
          { id: 'workspaces', label: 'Workspaces' },
          { id: 'usage', label: 'Usage & privacy' },
        ].map(tab => (
          <button
            key={tab.id}
            type="button"
            onClick={() => setActiveTab(tab.id as typeof activeTab)}
            className={`h-8 rounded-lg px-3 text-xs font-bold ${activeTab === tab.id ? 'bg-neutral-900 text-white dark:bg-white dark:text-neutral-900' : 'text-neutral-500 hover:bg-neutral-100 dark:text-neutral-400 dark:hover:bg-neutral-900'}`}
          >
            {tab.label}
          </button>
        ))}
      </div>

      <div className="flex min-h-0 flex-1 flex-col gap-6 overflow-y-auto p-4 sm:px-8 sm:py-6">
        {profile.error && (
          <div className="rounded-lg border-l-4 border-red-400 bg-red-50 px-4 py-3 text-sm text-red-700 dark:bg-red-500/10 dark:text-red-400">
            {profile.error}
          </div>
        )}

        {profile.loading && !profile.me ? (
          <p className="text-sm text-neutral-400">Loading…</p>
        ) : (
          <>
            {activeTab === 'settings' && (
              <>
            {/* Preferences */}
            <Card className="p-4 sm:p-5">
              <CardTitle icon={Sparkles} title="Preferences" subtitle="What Fronei has learned about how you like responses, refreshed periodically from your recent activity. Remove anything that's wrong." />
              <div className="mt-3 flex flex-wrap gap-2">
                {(profile.me?.preferences || []).length === 0 && (
                  <p className="text-sm text-neutral-400">Nothing learned yet -- keep using Fronei and this will fill in.</p>
                )}
                {(profile.me?.preferences || []).map(item => (
                  <RemovableChip key={item} label={item} onRemove={() => void profile.removePreference(item)} />
                ))}
              </div>
              <div className="mt-3 flex gap-2">
                <input
                  value={newPreference}
                  onChange={event => setNewPreference(event.target.value)}
                  onKeyDown={event => {
                    if (event.key === 'Enter' && newPreference.trim()) {
                      event.preventDefault()
                      void profile.updatePreferences([...(profile.me?.preferences || []), newPreference.trim()])
                      setNewPreference('')
                    }
                  }}
                  placeholder="Add a preference Fronei should always remember…"
                  className="min-w-0 flex-1 rounded-lg border border-neutral-200 bg-neutral-50 px-3 py-2 text-sm text-neutral-900 outline-none dark:border-neutral-800 dark:bg-neutral-900 dark:text-neutral-100"
                />
              </div>
              {profile.me?.preferences_updated_at && (
                <p className="mt-2 text-[11px] text-neutral-400">Last refreshed {formatAppDateTime(profile.me.preferences_updated_at)}</p>
              )}
            </Card>

            {/* Default settings */}
            <Card className="p-4 sm:p-5">
              <CardTitle icon={Sliders} title="Default settings" subtitle="Applied to every new task unless you change it for that one task." />
              <div className="mt-3 grid grid-cols-1 gap-2 sm:grid-cols-3">
                <SelectField
                  label="Quality"
                  value={profile.me?.settings.quality_mode || 'standard'}
                  onChange={value => void profile.updateSettings({ quality_mode: value as QualityMode })}
                  options={QUALITY_OPTIONS}
                />
                <SelectField
                  label="Output"
                  value={profile.me?.settings.output_format || 'chat'}
                  onChange={value => void profile.updateSettings({ output_format: value as OutputFormat })}
                  options={OUTPUT_OPTIONS}
                />
                <SelectField
                  label="Research"
                  value={profile.me?.settings.research_level || 'auto'}
                  onChange={value => void profile.updateSettings({ research_level: value as ResearchLevel })}
                  options={RESEARCH_OPTIONS}
                />
              </div>
            </Card>
              </>
            )}

            {activeTab === 'templates' && (
              <TemplateManager
                templates={profile.templates}
                templatesLoaded={profile.templatesLoaded}
                templateStatus={profile.templateStatus}
                templateError={profile.templateError}
                settings={profile.me?.settings || {}}
                renamingTemplateId={renamingTemplateId}
                templateNameDraft={templateNameDraft}
                deleteTemplateId={deleteTemplateId}
                onUpload={() => uploadTemplateRef.current?.click()}
                onRefresh={() => void profile.loadTemplates()}
                onSetDefault={templateId => void profile.updateSettings({ default_template_id: templateId })}
                onStartRename={template => {
                  setRenamingTemplateId(template.id)
                  setTemplateNameDraft(template.name)
                }}
                onNameDraftChange={setTemplateNameDraft}
                onSaveRename={() => {
                  if (!renamingTemplateId) return
                  void profile.renameTemplate(renamingTemplateId, templateNameDraft)
                  setRenamingTemplateId(null)
                }}
                onCancelRename={() => setRenamingTemplateId(null)}
                onReplace={templateId => {
                  setReplaceTemplateId(templateId)
                  replaceTemplateRef.current?.click()
                }}
                onRequestDelete={setDeleteTemplateId}
                onCancelDelete={() => setDeleteTemplateId(null)}
                onDelete={templateId => void handleDeleteTemplate(templateId)}
              />
            )}

            {activeTab === 'workspaces' && (
              <>
            {/* Workspaces */}
            <Card className="p-4 sm:p-5">
              <CardTitle icon={Sparkles} title="Workspaces" subtitle="What's actively being worked on in each workspace, distinct from your global preferences above." />
              <div className="mt-3 grid gap-3">
                {(profile.workspaces || []).length === 0 && (
                  <p className="text-sm text-neutral-400">No workspaces yet.</p>
                )}
                {(profile.workspaces || []).map(workspace => (
                  <WorkspaceCard
                    key={workspace.id}
                    workspace={workspace}
                    draft={workspacePriorityDrafts[workspace.id] || ''}
                    onDraftChange={value => setWorkspacePriorityDrafts(prev => ({ ...prev, [workspace.id]: value }))}
                    onAddPriority={() => {
                      const value = (workspacePriorityDrafts[workspace.id] || '').trim()
                      if (!value) return
                      void profile.updateWorkspacePriorities(workspace.id, [...workspace.priorities, value])
                      setWorkspacePriorityDrafts(prev => ({ ...prev, [workspace.id]: '' }))
                    }}
                    onRemovePriority={item => void profile.removeWorkspacePriority(workspace.id, item)}
                  />
                ))}
              </div>
            </Card>
              </>
            )}

            {activeTab === 'usage' && (
              <>
            {/* Usage / BI report */}
            <Card className="p-4 sm:p-5">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <CardTitle icon={Sparkles} title="Your usage" subtitle="Cost, activity, and model/route performance for your own turns." />
                <div className="flex items-center gap-1.5 rounded-full bg-neutral-100 p-1 dark:bg-neutral-800/60">
                  {RANGE_OPTIONS.map(item => (
                    <button
                      key={item.value}
                      type="button"
                      onClick={() => handleRangeChange(item.value)}
                      className={`rounded-full px-3 py-1 text-xs font-bold ${range === item.value ? 'bg-neutral-900 text-white dark:bg-white dark:text-neutral-900' : 'text-neutral-500 dark:text-neutral-400'}`}
                    >
                      {item.label}
                    </button>
                  ))}
                </div>
              </div>

              {profile.usage && (
                <div className="mt-4 grid gap-4">
                  <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                    <Stat label="Total cost" value={`$${profile.usage.summary.total_cost.toFixed(2)}`} />
                    <Stat label="Requests" value={profile.usage.summary.requests.toLocaleString()} />
                    <Stat label="Active days" value={profile.usage.summary.active_days.toLocaleString()} />
                    <Stat
                      label="Failure rate"
                      value={`${(profile.usage.summary.failure_rate * 100).toFixed(1)}%`}
                      tone={profile.usage.summary.failure_rate > 0.1 ? 'danger' : 'neutral'}
                    />
                  </div>

                  <div className="rounded-xl border border-neutral-200 p-3.5 dark:border-neutral-800">
                    <p className="mb-2 text-sm font-bold text-neutral-900 dark:text-neutral-50">Cost &amp; activity over time</p>
                    {profile.usage.cost_by_day.length === 0 ? (
                      <p className="text-sm text-neutral-400">No activity in this range.</p>
                    ) : (
                      <div className="h-52 w-full">
                        <ResponsiveContainer width="100%" height="100%">
                          <AreaChart data={profile.usage.cost_by_day}>
                            <defs>
                              <linearGradient id="profileCostFill" x1="0" y1="0" x2="0" y2="1">
                                <stop offset="0%" stopColor="#10b981" stopOpacity={0.35} />
                                <stop offset="100%" stopColor="#10b981" stopOpacity={0} />
                              </linearGradient>
                            </defs>
                            <XAxis dataKey="date" tick={{ fontSize: 11, fill: '#a3a3a3' }} axisLine={false} tickLine={false} />
                            <YAxis tick={{ fontSize: 11, fill: '#a3a3a3' }} axisLine={false} tickLine={false} width={48} tickFormatter={value => `$${value}`} />
                            <Tooltip formatter={(value, name) => [name === 'cost' ? `$${Number(value).toFixed(4)}` : value, name === 'cost' ? 'Cost' : 'Requests']} contentStyle={{ fontSize: 12, borderRadius: 8 }} />
                            <Area type="monotone" dataKey="cost" stroke="#10b981" strokeWidth={2} fill="url(#profileCostFill)" />
                          </AreaChart>
                        </ResponsiveContainer>
                      </div>
                    )}
                  </div>

                  <div className="grid gap-4 sm:grid-cols-2">
                    <div className="overflow-hidden rounded-xl border border-neutral-200 dark:border-neutral-800">
                      <p className="border-b border-neutral-200 bg-neutral-50 px-3 py-2 text-xs font-bold uppercase tracking-wide text-neutral-400 dark:border-neutral-800 dark:bg-neutral-900">Route distribution</p>
                      <div className="h-40 w-full p-2">
                        {profile.usage.route_distribution.length === 0 ? (
                          <p className="grid h-full place-items-center text-sm text-neutral-400">No requests yet.</p>
                        ) : (
                          <ResponsiveContainer width="100%" height="100%">
                            <BarChart data={profile.usage.route_distribution}>
                              <XAxis dataKey="route" tick={{ fontSize: 10, fill: '#a3a3a3' }} axisLine={false} tickLine={false} />
                              <YAxis tick={{ fontSize: 10, fill: '#a3a3a3' }} axisLine={false} tickLine={false} width={28} />
                              <Tooltip contentStyle={{ fontSize: 12, borderRadius: 8 }} />
                              <Bar dataKey="count" fill="#6366f1" radius={[4, 4, 0, 0]} />
                            </BarChart>
                          </ResponsiveContainer>
                        )}
                      </div>
                    </div>

                    <div className="overflow-hidden rounded-xl border border-neutral-200 dark:border-neutral-800">
                      <p className="border-b border-neutral-200 bg-neutral-50 px-3 py-2 text-xs font-bold uppercase tracking-wide text-neutral-400 dark:border-neutral-800 dark:bg-neutral-900">Model performance</p>
                      <table className="w-full text-left text-sm">
                        <tbody className="divide-y divide-neutral-100 dark:divide-neutral-800">
                          {profile.usage.model_performance.map(row => (
                            <tr key={row.model} className="bg-white dark:bg-neutral-950">
                              <td className="truncate px-3 py-2 font-mono text-xs text-neutral-700 dark:text-neutral-200">{row.model}</td>
                              <td className="px-3 py-2 text-right text-xs text-neutral-400">{row.requests} req</td>
                              <td className="px-3 py-2 text-right text-xs text-neutral-400">p95 {row.p95_latency_ms}ms</td>
                              <td className="px-3 py-2 text-right text-xs font-semibold text-neutral-700 dark:text-neutral-200">${row.cost.toFixed(4)}</td>
                            </tr>
                          ))}
                          {profile.usage.model_performance.length === 0 && (
                            <tr><td className="px-3 py-6 text-center text-sm text-neutral-400" colSpan={4}>No model usage yet.</td></tr>
                          )}
                        </tbody>
                      </table>
                    </div>
                  </div>
                </div>
              )}
            </Card>

            {/* Privacy */}
            <Card className="border-red-200 p-4 dark:border-red-500/20 sm:p-5">
              <CardTitle icon={AlertTriangle} title="Privacy" subtitle="Download or permanently delete everything Fronei has stored about you." />
              <div className="mt-3 flex flex-wrap gap-2.5">
                <button
                  type="button"
                  onClick={handleExport}
                  disabled={exporting}
                  className="flex h-9 items-center gap-1.5 rounded-lg border border-neutral-200 px-3.5 text-sm font-bold text-neutral-700 disabled:opacity-50 dark:border-neutral-700 dark:text-neutral-200"
                >
                  {exporting ? <Loader2 size={14} className="animate-spin" /> : <Download size={14} />} Download my data
                </button>
                <button
                  type="button"
                  onClick={() => setDeleteConfirmOpen(true)}
                  className="flex h-9 items-center gap-1.5 rounded-lg border border-red-200 px-3.5 text-sm font-bold text-red-600 dark:border-red-500/30 dark:text-red-400"
                >
                  <Trash2 size={14} /> Delete my data
                </button>
              </div>
            </Card>
              </>
            )}
          </>
        )}
      </div>

      {deleteConfirmOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" onClick={() => setDeleteConfirmOpen(false)}>
          <div
            role="dialog"
            aria-modal="true"
            onClick={event => event.stopPropagation()}
            className="w-full max-w-sm rounded-xl border border-neutral-200 bg-white p-5 shadow-xl dark:border-neutral-700 dark:bg-neutral-900"
          >
            <div className="flex items-start justify-between gap-3">
              <p className="text-base font-bold text-neutral-900 dark:text-neutral-50">Delete all your data?</p>
              <button type="button" onClick={() => setDeleteConfirmOpen(false)} aria-label="Close" className="text-neutral-400 hover:text-neutral-700 dark:hover:text-neutral-200">
                <X size={16} />
              </button>
            </div>
            <p className="mt-2 text-sm text-neutral-500 dark:text-neutral-400">
              This permanently deletes every workspace, conversation, turn, document template, and your consolidated preferences. This cannot be undone.
            </p>
            <label className="mt-3 flex items-start gap-2 text-sm text-neutral-700 dark:text-neutral-200">
              <input
                type="checkbox"
                checked={deleteAcknowledged}
                onChange={event => setDeleteAcknowledged(event.target.checked)}
                className="mt-0.5"
              />
              I understand this is permanent and cannot be undone.
            </label>
            <div className="mt-4 flex justify-end gap-2">
              <button
                type="button"
                onClick={() => setDeleteConfirmOpen(false)}
                className="h-9 rounded-lg border border-neutral-200 px-3.5 text-sm font-bold text-neutral-700 dark:border-neutral-700 dark:text-neutral-200"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={handleDelete}
                disabled={!deleteAcknowledged || deleting}
                className="flex h-9 items-center gap-1.5 rounded-lg bg-red-600 px-3.5 text-sm font-bold text-white disabled:opacity-50"
              >
                {deleting && <Loader2 size={14} className="animate-spin" />} Delete everything
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

function CardTitle({ icon: Icon, title, subtitle }: { icon: typeof Sparkles; title: string; subtitle: string }) {
  return (
    <div className="flex items-start gap-2.5">
      <span className="mt-0.5 grid h-7 w-7 flex-shrink-0 place-items-center rounded-full bg-neutral-100 text-neutral-500 dark:bg-neutral-800 dark:text-neutral-400">
        <Icon size={14} />
      </span>
      <div>
        <p className="text-sm font-bold text-neutral-900 dark:text-neutral-50">{title}</p>
        <p className="mt-0.5 text-xs text-neutral-500 dark:text-neutral-400">{subtitle}</p>
      </div>
    </div>
  )
}

function Stat({ label, value, tone = 'neutral' }: { label: string; value: string; tone?: 'neutral' | 'danger' }) {
  return (
    <div className="rounded-xl border border-neutral-200 bg-white p-3.5 dark:border-neutral-800 dark:bg-neutral-900">
      <p className="text-[11px] font-bold uppercase tracking-wide text-neutral-400">{label}</p>
      <p className={`mt-1 text-xl font-bold ${tone === 'danger' ? 'text-red-600 dark:text-red-400' : 'text-neutral-900 dark:text-neutral-50'}`}>{value}</p>
    </div>
  )
}

function RemovableChip({ label, onRemove }: { label: string; onRemove: () => void }) {
  return (
    <span className="inline-flex max-w-full items-center gap-1.5 truncate rounded-full bg-neutral-100 px-2.5 py-1.5 text-xs font-semibold text-neutral-700 dark:bg-neutral-800 dark:text-neutral-200">
      <span className="truncate">{label}</span>
      <button type="button" onClick={onRemove} aria-label={`Remove "${label}"`} className="flex-shrink-0 text-neutral-400 hover:text-neutral-700 dark:hover:text-neutral-100">
        <X size={12} />
      </button>
    </span>
  )
}

function TemplateManager({
  templates,
  templatesLoaded,
  templateStatus,
  templateError,
  settings,
  renamingTemplateId,
  templateNameDraft,
  deleteTemplateId,
  onUpload,
  onRefresh,
  onSetDefault,
  onStartRename,
  onNameDraftChange,
  onSaveRename,
  onCancelRename,
  onReplace,
  onRequestDelete,
  onCancelDelete,
  onDelete,
}: {
  templates: DocumentTemplateOption[]
  templatesLoaded: boolean
  templateStatus: string
  templateError: string
  settings: ProfileSettings
  renamingTemplateId: string | null
  templateNameDraft: string
  deleteTemplateId: string | null
  onUpload: () => void
  onRefresh: () => void
  onSetDefault: (templateId: string) => void
  onStartRename: (template: DocumentTemplateOption) => void
  onNameDraftChange: (value: string) => void
  onSaveRename: () => void
  onCancelRename: () => void
  onReplace: (templateId: string) => void
  onRequestDelete: (templateId: string) => void
  onCancelDelete: () => void
  onDelete: (templateId: string) => void
}) {
  const defaultTemplateId = settings.default_template_id || ''
  const defaultTemplate = templates.find(template => template.id === defaultTemplateId)

  return (
    <Card className="p-4 sm:p-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <CardTitle
          icon={FileText}
          title="Document templates"
          subtitle="Manage uploaded PowerPoint templates and choose the default for new presentation tasks."
        />
        <div className="flex gap-2">
          <button
            type="button"
            onClick={onRefresh}
            className="flex h-9 items-center gap-1.5 rounded-lg border border-neutral-200 px-3 text-xs font-bold text-neutral-600 dark:border-neutral-700 dark:text-neutral-300"
          >
            <RefreshCw size={14} /> Refresh
          </button>
          <button
            type="button"
            onClick={onUpload}
            className="flex h-9 items-center gap-1.5 rounded-lg bg-neutral-900 px-3 text-xs font-bold text-white dark:bg-white dark:text-neutral-900"
          >
            <Upload size={14} /> Upload PPTX
          </button>
        </div>
      </div>

      <div className="mt-4 grid gap-2 rounded-xl border border-neutral-200 bg-neutral-50 p-3 dark:border-neutral-800 dark:bg-neutral-900/60">
        <SelectField
          label="Default deck"
          value={defaultTemplateId}
          onChange={onSetDefault}
          options={[{ value: '', label: 'Fronei default' }, ...templates.map(template => ({ value: template.id, label: template.name }))]}
        />
        <p className="text-xs text-neutral-500 dark:text-neutral-400">
          {defaultTemplate ? `${defaultTemplate.name} is used unless a task selects another template.` : 'Fronei default is used unless a task selects another template.'}
        </p>
      </div>

      {templateStatus && <p className="mt-3 text-xs font-medium text-emerald-600 dark:text-emerald-400">{templateStatus}</p>}
      {templateError && (
        <p className="mt-3 rounded-md border-l-3 border-red-400 bg-red-50 px-2.5 py-1.5 text-xs font-medium text-red-700 dark:bg-red-500/10 dark:text-red-400">{templateError}</p>
      )}

      <div className="mt-4 grid gap-3">
        {!templatesLoaded && <p className="text-sm text-neutral-400">Loading templates...</p>}
        {templatesLoaded && templates.length === 0 && (
          <div className="rounded-lg border border-dashed border-neutral-300 p-5 text-sm text-neutral-500 dark:border-neutral-700 dark:text-neutral-400">
            No templates uploaded yet.
          </div>
        )}
        {templates.map(template => {
          const isDefault = defaultTemplateId === template.id
          const isRenaming = renamingTemplateId === template.id
          const isDeleting = deleteTemplateId === template.id

          return (
            <div key={template.id} className={`rounded-xl border p-3.5 ${isDefault ? 'border-emerald-300 bg-emerald-50/70 dark:border-emerald-500/30 dark:bg-emerald-500/10' : 'border-neutral-200 bg-white dark:border-neutral-800 dark:bg-neutral-950'}`}>
              <div className="grid gap-3 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-start">
                <div className="min-w-0">
                  {isRenaming ? (
                    <input
                      value={templateNameDraft}
                      onChange={event => onNameDraftChange(event.target.value)}
                      onKeyDown={event => {
                        if (event.key === 'Enter') onSaveRename()
                        if (event.key === 'Escape') onCancelRename()
                      }}
                      autoFocus
                      className="w-full rounded-lg border border-neutral-200 bg-neutral-50 px-3 py-2 text-sm font-bold text-neutral-900 outline-none dark:border-neutral-800 dark:bg-neutral-900 dark:text-neutral-100"
                    />
                  ) : (
                    <div className="flex min-w-0 items-center gap-2">
                      <p className="truncate text-sm font-bold text-neutral-900 dark:text-neutral-50">{template.name}</p>
                      {isDefault && <Badge tone="success"><Check size={12} /> Default</Badge>}
                    </div>
                  )}
                  <p className="mt-1 text-xs text-neutral-400">{template.user_template ? 'Uploaded PowerPoint template' : 'Built-in template'}</p>
                  {template.description && <p className="mt-1 line-clamp-2 text-xs text-neutral-500 dark:text-neutral-400">{template.description}</p>}
                </div>

                <div className="flex flex-wrap gap-2 sm:justify-end">
                  {isRenaming ? (
                    <>
                      <button type="button" onClick={onSaveRename} className="h-8 rounded-lg bg-neutral-900 px-3 text-xs font-bold text-white dark:bg-white dark:text-neutral-900">Save</button>
                      <button type="button" onClick={onCancelRename} className="h-8 rounded-lg border border-neutral-200 px-3 text-xs font-bold text-neutral-600 dark:border-neutral-700 dark:text-neutral-300">Cancel</button>
                    </>
                  ) : (
                    <>
                      <button type="button" onClick={() => onSetDefault(template.id)} disabled={isDefault} className="h-8 rounded-lg border border-neutral-200 px-3 text-xs font-bold text-neutral-600 disabled:opacity-40 dark:border-neutral-700 dark:text-neutral-300">Make default</button>
                      {template.user_template && (
                        <>
                          <button type="button" onClick={() => onStartRename(template)} aria-label={`Rename ${template.name}`} className="grid h-8 w-8 place-items-center rounded-lg border border-neutral-200 text-neutral-500 dark:border-neutral-700 dark:text-neutral-300">
                            <Pencil size={13} />
                          </button>
                          <button type="button" onClick={() => onReplace(template.id)} className="h-8 rounded-lg border border-neutral-200 px-3 text-xs font-bold text-neutral-600 dark:border-neutral-700 dark:text-neutral-300">Replace</button>
                          {isDeleting ? (
                            <>
                              <button type="button" onClick={() => onDelete(template.id)} className="h-8 rounded-lg border border-red-200 px-3 text-xs font-bold text-red-600 dark:border-red-500/30 dark:text-red-400">Delete</button>
                              <button type="button" onClick={onCancelDelete} className="h-8 rounded-lg border border-neutral-200 px-3 text-xs font-bold text-neutral-600 dark:border-neutral-700 dark:text-neutral-300">Keep</button>
                            </>
                          ) : (
                            <button type="button" onClick={() => onRequestDelete(template.id)} aria-label={`Delete ${template.name}`} className="grid h-8 w-8 place-items-center rounded-lg border border-neutral-200 text-neutral-500 dark:border-neutral-700 dark:text-neutral-300">
                              <Trash2 size={13} />
                            </button>
                          )}
                        </>
                      )}
                    </>
                  )}
                </div>
              </div>
            </div>
          )
        })}
      </div>
    </Card>
  )
}

function WorkspaceCard({
  workspace,
  draft,
  onDraftChange,
  onAddPriority,
  onRemovePriority,
}: {
  workspace: ProfileWorkspace
  draft: string
  onDraftChange: (value: string) => void
  onAddPriority: () => void
  onRemovePriority: (item: string) => void
}) {
  return (
    <div className="rounded-xl border border-neutral-200 p-3.5 dark:border-neutral-800">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="font-bold text-neutral-900 dark:text-neutral-50">{workspace.name}</p>
        <div className="flex items-center gap-2">
          <Badge>{workspace.turn_count} turn{workspace.turn_count === 1 ? '' : 's'}</Badge>
          <Badge>${workspace.total_cost_usd.toFixed(2)}</Badge>
        </div>
      </div>
      <div className="mt-2.5 flex flex-wrap gap-2">
        {workspace.priorities.length === 0 && (
          <p className="text-xs text-neutral-400">Nothing active here yet.</p>
        )}
        {workspace.priorities.map(item => (
          <RemovableChip key={item} label={item} onRemove={() => onRemovePriority(item)} />
        ))}
      </div>
      <div className="mt-2.5 flex gap-2">
        <input
          value={draft}
          onChange={event => onDraftChange(event.target.value)}
          onKeyDown={event => {
            if (event.key === 'Enter' && draft.trim()) {
              event.preventDefault()
              onAddPriority()
            }
          }}
          placeholder="Add what's active in this workspace…"
          className="min-w-0 flex-1 rounded-lg border border-neutral-200 bg-neutral-50 px-3 py-1.5 text-xs text-neutral-900 outline-none dark:border-neutral-800 dark:bg-neutral-900 dark:text-neutral-100"
        />
      </div>
    </div>
  )
}
